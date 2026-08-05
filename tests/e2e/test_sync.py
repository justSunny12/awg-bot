"""E2E: ядро синхронизации с awg-сервером.

poll_traffic (накопление дельт + откат счётчика + онлайн-счётчик),
reset_monthly_traffic (обнуление месяца, снятие трафик-причин, EXPIRY цел),
check_pauses (авто-выход из срочных пауз, admin_open не трогаем),
reconcile_peers (карантин чужих пиров + грациозное удаление пропавших).

fake_awg НЕ стабит show_dump/read_file/read_clients_table — их мы патчим
локально через monkeypatch (тот же объект awg, что видит services).
"""
import datetime

import pytest

from awgbot.core import config, models
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.infra import awg
from awgbot.util import timeutil

pytestmark = pytest.mark.e2e


# ── помощники ────────────────────────────────────────────────────────────────
def _dump_entry(pub, ip, rx, tx, hs=None):
    """Один пир в формате awg.parse_dump (то, что возвращает show_dump)."""
    return {"public_key": pub, "endpoint": None, "allowed_ips": f"{ip}/32",
            "address": ip, "last_handshake": hs, "rx": rx, "tx": tx}


def _set_dump(monkeypatch, entries):
    monkeypatch.setattr(awg, "show_dump", lambda: list(entries), raising=False)


def _conf_with(peers):
    """Живой awg0.conf с перечисленными пирами. peers: [(pubkey, ip), ...]."""
    header = "[Interface]\nPrivateKey = SRVPRIV\nListenPort = 43125"
    blocks = [f"[Peer]\nPublicKey = {pub}\nAllowedIPs = {ip}/32" for pub, ip in peers]
    body = ("\n\n" + "\n\n".join(blocks)) if blocks else ""
    return header + body + "\n"


def _set_live(monkeypatch, peers, table=None):
    """Патчит read_file (конфиг) и read_clients_table под reconcile_peers."""
    monkeypatch.setattr(awg, "read_file", lambda path: _conf_with(peers), raising=False)
    monkeypatch.setattr(awg, "read_clients_table", lambda: list(table or []), raising=False)


# ── poll_traffic ─────────────────────────────────────────────────────────────
def test_poll_first_sweep_sets_baseline_only(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=700)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    _set_dump(monkeypatch, [_dump_entry(dev.public_key, dev.address, 100, 50)])
    services.poll_traffic()
    fresh = services.db.get_device(dc.device_id)
    # первая сверка — только опорная выборка, трафик ещё не копится
    assert fresh.traffic_rx_month == 0
    assert fresh.traffic_tx_month == 0


def test_poll_accumulates_delta(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=701)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    pub, ip = dev.public_key, dev.address
    _set_dump(monkeypatch, [_dump_entry(pub, ip, 100, 50)])
    services.poll_traffic()                                  # база 100/50
    _set_dump(monkeypatch, [_dump_entry(pub, ip, 130, 70)])
    services.poll_traffic()                                  # +30 / +20
    fresh = services.db.get_device(dc.device_id)
    assert fresh.traffic_rx_month == 30
    assert fresh.traffic_tx_month == 20


def test_poll_counter_reset_uses_absolute(services, fake_awg, make_active_client, monkeypatch):
    # awg перезапустился → счётчик пира обнулился, дельта была бы отрицательной
    client = make_active_client(tg_id=702)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    pub, ip = dev.public_key, dev.address
    _set_dump(monkeypatch, [_dump_entry(pub, ip, 1000, 800)])
    services.poll_traffic()                                  # база 1000/800
    _set_dump(monkeypatch, [_dump_entry(pub, ip, 200, 150)])  # откат вниз
    services.poll_traffic()
    fresh = services.db.get_device(dc.device_id)
    assert fresh.traffic_rx_month == 200                    # взяли абсолют, не -800
    assert fresh.traffic_tx_month == 150


def test_poll_updates_online_count(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=703)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    now_ts = int(timeutil.now().timestamp())                # свежий handshake → онлайн
    _set_dump(monkeypatch, [_dump_entry(dev.public_key, dev.address, 10, 10, hs=now_ts)])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "1"


# ── reset_monthly_traffic ────────────────────────────────────────────────────
def test_monthly_reset_zeroes_counters_and_traffic_blocks(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=710)
    dc = services.add_device(client.id, "d")
    services.db.add_traffic(dc.device_id, 500, 400)
    # смешанные причины: трафик (должен уйти) + EXPIRY (должен остаться)
    services._device_set_block(dc.device_id, DeviceBlock.TRAFFIC_USER)
    services._device_set_block(dc.device_id, DeviceBlock.EXPIRY)
    services._client_set_block(client.id, ClientBlock.TRAFFIC_CLIENT)
    services._client_set_block(client.id, ClientBlock.EXPIRY)

    services.reset_monthly_traffic()

    dev = services.db.get_device(dc.device_id)
    fresh = services.db.get_client(client.id)
    assert dev.traffic_rx_month == 0 and dev.traffic_tx_month == 0
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER) == 0    # трафик снят
    assert int(dev.block_reason) & int(DeviceBlock.EXPIRY)              # EXPIRY цел
    assert int(fresh.block_reason) & int(ClientBlock.TRAFFIC_CLIENT) == 0
    assert int(fresh.block_reason) & int(ClientBlock.EXPIRY)


def test_monthly_reset_clears_bonus(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=711)
    services.db.update_client_fields(client.id, bonus_granted_month=1, bonus_bytes=12345)
    services.reset_monthly_traffic()
    fresh = services.db.get_client(client.id)
    assert fresh.bonus_granted_month == 0
    assert fresh.bonus_bytes == 0


# ── check_pauses ─────────────────────────────────────────────────────────────
def _rewind_pause(services, client_id, *, days_ago, reserved, mode, saved_end=None):
    """Перематывает active_since назад, не трогая прочие поля процесса."""
    past = timeutil.now() - datetime.timedelta(days=days_ago)
    c = services.db.get_client(client_id)
    services.db.save_pause(client_id, models.PauseState(
        active_since=timeutil.to_iso(past), reserved_days=reserved,
        mode=mode, saved_end=saved_end, used_days=int(c.pause_used_days)))


def test_check_pauses_auto_exits_expired_user_pause(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=720, period_kind="year")
    services.add_device(client.id, "d")
    ok, reserved, _, _ = services.enter_pause(client.id)
    assert ok and reserved > 0
    _rewind_pause(services, client.id, days_ago=reserved + 1, reserved=reserved, mode="user")

    services.check_pauses()

    fresh = services.db.get_client(client.id)
    assert not fresh.is_paused
    assert int(fresh.block_reason) & int(ClientBlock.PAUSED) == 0


def test_check_pauses_keeps_unexpired_pause(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=721, period_kind="year")
    ok, reserved, _, _ = services.enter_pause(client.id)
    assert ok
    services.check_pauses()                                 # active_since только что
    assert services.db.get_client(client.id).is_paused


def test_check_pauses_ignores_admin_open(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=722, period_kind="year")
    services.enter_admin_pause(client.id, 0)                # бессрочная (admin_open)
    saved = services.db.get_client(client.id).pause_saved_end
    _rewind_pause(services, client.id, days_ago=999, reserved=0,
                  mode="admin_open", saved_end=saved)
    services.check_pauses()                                 # админскую бессрочную не снимаем
    assert services.db.get_client(client.id).is_paused


# ── reconcile_peers ──────────────────────────────────────────────────────────
def test_reconcile_removes_peer_after_threshold(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=730)
    dc = services.add_device(client.id, "d")
    _set_live(monkeypatch, [])                              # пир исчез из живого конфига
    services.reconcile_peers()                              # порог=2: сверка 1 → счётчик
    assert services.db.get_device(dc.device_id).missing_count == 1
    services.reconcile_peers()                              # сверка 2 → удаление
    assert services.db.get_device(dc.device_id) is None


def test_reconcile_missing_count_resets_on_return(services, fake_awg, make_active_client, monkeypatch):
    client = make_active_client(tg_id=731)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    _set_live(monkeypatch, [])                              # пропал
    services.reconcile_peers()
    assert services.db.get_device(dc.device_id).missing_count == 1
    _set_live(monkeypatch, [(dev.public_key, dev.address)])  # вернулся до порога
    services.reconcile_peers()
    assert services.db.get_device(dc.device_id).missing_count == 0


def test_reconcile_survives_address_reuse_by_the_app(
        services, fake_awg, make_active_client, monkeypatch):
    """Приложение переиспользует адрес удалённого пира — сверка не встаёт колом.

    devices.address UNIQUE, а приложение выдаёт «первый свободный» по тому же
    правилу, что и бот. Удалили пир в приложении, следом завели новый — он
    получает освободившийся адрес, которым в БД ещё владеет уходящая запись.
    Прежний порядок проходов (сначала усыновление) падал на UNIQUE ДО прохода
    по пропавшим, поэтому держатель адреса не удалялся никогда: реконсиляция
    ломалась насовсем, вместе с подхватом всех остальных чужих пиров.
    """
    client = make_active_client(tg_id=732)
    dc = services.add_device(client.id, "d")
    addr = services.db.get_device(dc.device_id).address

    # старого пира в конфиге больше нет, его адрес занял новый чужой пир
    _set_live(monkeypatch, [("appREUSE", addr)],
              table=[{"clientId": "appREUSE", "userData": {"clientName": "Phone"}}])

    services.reconcile_peers()                 # сверка 1: порог не выбран
    assert services.db.get_device(dc.device_id).missing_count == 1

    services.reconcile_peers()                 # сверка 2: удаление + усыновление
    assert services.db.get_device(dc.device_id) is None
    adopted = next(d for d in services.db.list_all_devices()
                   if d.public_key == "appREUSE")
    assert adopted.address == addr


def test_unknown_peer_is_quarantined_with_an_alarm(services, fake_awg, monkeypatch):
    """Пир, которого нет в базе, — событие безопасности, а не находка.

    Приложение Amnezia с серверной стороны снято, создавать пиры больше некому,
    кроме бота. Значит запись в конфиге, которой нет у нас, это ручная правка
    или чужое вмешательство. Прежнее поведение — молча усыновить и прислать
    информационную строчку — узаконивало бы чужой доступ.
    """
    services.ensure_admin_client()
    _set_live(monkeypatch, [("strangerPUB", "10.8.0.77")])
    notes = services.reconcile_peers()

    dev = next(d for d in services.db.list_all_devices()
               if d.public_key == "strangerPUB")
    assert dev.client_id == services.db.get_service_client_id(), "карантин"

    note = next(n for n in notes if n.tg_id == config.ADMIN_ID)
    assert "10.8.0.77" in note.text
    assert note.force_sound, "тревога обязана звучать и в тихие часы"


def test_bootstrap_creates_admin_device_once(services, fake_awg):
    """Без первого устройства админ не достучится до бота, а завести его
    снаружи больше нельзя — карантин. Значит бот обязан выдать его сам.

    Ровно один раз: повторный старт не плодит устройства, а сознательно
    удалённое админом не возвращается.
    """
    created = services.bootstrap_admin_device()
    assert created is not None
    admin_cid = services.ensure_admin_client()
    assert services.db.count_devices(admin_cid) == 1

    assert services.bootstrap_admin_device() is None          # второй старт
    assert services.db.count_devices(admin_cid) == 1

    # админ удалил всё у себя — бот не возвращает
    for d in services.db.list_devices(admin_cid):
        services.remove_device(d.id)
    assert services.bootstrap_admin_device() is None
    assert services.db.count_devices(admin_cid) == 0


def test_bootstrap_skips_when_admin_already_has_devices(services, fake_awg):
    """Работающая установка: метки в state нет, а устройства есть — лишнего
    не заводим."""
    admin_cid = services.ensure_admin_client()
    services.add_device(admin_cid, "уже есть")
    assert services.bootstrap_admin_device() is None
    assert services.db.count_devices(admin_cid) == 1
