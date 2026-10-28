"""Пиры-зонды кворума на интерфейсе пользователей: сверка пиров и опрос трафика.

Выпуск с кворумом кладёт на `awg0` служебные пиры-зонды голосующих
(169.254.88.0/24) и второй `Address` из того же блока. Откат на выпуск без
кворума оставляет их в конфиге и в ядре. Цена ошибки: сверка, не знающая
блока, заводит каждый зонд в карантин служебного профиля и шлёт
«🚨 Чужой пир в конфиге сервера» со звуком после каждого отката; опрос,
не знающий блока, показывает «📶 Онлайн» больше на число голосующих. И
наоборот — правило, задевшее настоящих пиров, прячет чужой доступ.

Реальная временная БД; конфиг и дамп — текстом в том виде, как их пишет и
отдаёт сервер, через тот же разбор, что в работе.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import awg
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

# ключи зондов — заглушки: сверке и опросу формат ключа не важен
PROBES = [("DUMMY-probe-4", "169.254.88.4"), ("DUMMY-probe-5", "169.254.88.5"),
          ("DUMMY-probe-6", "169.254.88.6")]
PROBE_KEYS = {pub for pub, _ in PROBES}


def _conf_42(users, *, extra=(), comma=False):
    """awg0.conf как его пишет выпуск с кворумом: пользователи, второй адрес
    интерфейса из блока зондов и пиры-зонды .4 .5 .6 (с PSK и keepalive, как
    служебные пиры)."""
    addr = ("Address = 10.8.1.1/24, 169.254.88.1/26" if comma
            else "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26")
    header = f"[Interface]\nPrivateKey = DUMMY\n{addr}\nListenPort = 43125\nJc = 4"
    blocks = [f"[Peer]\nPublicKey = {pub}\nPresharedKey = DUMMY\nAllowedIPs = {ip}/32"
              for pub, ip in list(users) + list(extra)]
    blocks += [f"[Peer]\n# зонд кворума\nPublicKey = {pub}\nPresharedKey = DUMMY\n"
               f"AllowedIPs = {ip}/32\nPersistentKeepalive = 25" for pub, ip in PROBES]
    return header + "\n\n" + "\n\n".join(blocks) + "\n"


def _set_conf(monkeypatch, text):
    monkeypatch.setattr(awg, "read_file", lambda path: text, raising=False)


def _users(services, make_active_client, n=2, tg_id=7100):
    client = make_active_client(tg_id=tg_id, device_limit=5)
    ids = [services.add_device(client.id, f"d{i}").device_id for i in range(n)]
    return client, [services.db.get_device(i) for i in ids]


# ── сверка пиров ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("comma", [False, True], ids=["two_address_lines", "address_with_comma"])
def test_probe_peers_are_not_quarantined_and_raise_no_alarm(services, fake_awg, make_active_client,
                                                            monkeypatch, comma):
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client)
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs], comma=comma))
    before = {d.public_key for d in services.db.list_all_devices()}

    for sweep in range(3):                                 # и повтор ничего не меняет
        notes = services.reconcile_peers()
        assert notes == [], f"сверка {sweep + 1}: уведомления на зонды: {[n.text for n in notes]}"

    after = {d.public_key for d in services.db.list_all_devices()}
    assert not after & PROBE_KEYS, "пир-зонд заведён в базу (карантин)"
    assert after == before, f"состав устройств изменился: +{after - before} −{before - after}"
    sid = services.db.get_service_client_id()
    assert services.db.count_devices(sid) == 0, "в служебном профиле появились карантинные записи"
    assert not fake_awg.blocked & {ip for _, ip in PROBES}, "на адрес зонда наложен DROP"


def test_device_missing_count_does_not_grow_next_to_probes(services, fake_awg, make_active_client,
                                                           monkeypatch):
    """Зонды не должны сбить сопоставление: устройства, которые в конфиге
    есть, не копят пропажи и через порог не удаляются молча."""
    _client, devs = _users(services, make_active_client, n=3, tg_id=7101)
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs]))
    for _ in range(config.MISSING_SWEEPS_THRESHOLD + 1):
        services.reconcile_peers()
    for d in devs:
        fresh = services.db.get_device(d.id)
        assert fresh is not None, f"устройство {d.name} удалено сверкой"
        assert fresh.missing_count == 0, f"устройство {d.name}: missing_count={fresh.missing_count}"


def test_a_device_gone_from_the_conf_is_still_counted_missing_next_to_probes(
        services, fake_awg, make_active_client, monkeypatch):
    """Контроль обратной стороны: зонды в конфиге не маскируют пропажу
    настоящего устройства."""
    _client, devs = _users(services, make_active_client, n=2, tg_id=7102)
    kept, gone = devs
    _set_conf(monkeypatch, _conf_42([(kept.public_key, kept.address)]))
    services.reconcile_peers()
    assert services.db.get_device(gone.id).missing_count == 1, "пропажа устройства рядом с зондами не замечена"
    assert services.db.get_device(kept.id).missing_count == 0


def test_a_stranger_outside_the_block_still_raises_the_alarm_once(services, fake_awg, make_active_client,
                                                                  monkeypatch):
    """Рельс не должен стать лазейкой: чужой пир вне блока зондов — карантин и
    тревога со звуком, ровно одна на пир; зонды рядом в тревогу не попадают."""
    services.ensure_admin_client()
    _client, devs = _users(services, make_active_client, n=1, tg_id=7103)
    stranger = ("DUMMY-stranger", "10.8.1.77")
    _set_conf(monkeypatch, _conf_42([(d.public_key, d.address) for d in devs], extra=[stranger]))

    notes = [n for n in services.reconcile_peers() if n.tg_id == config.ADMIN_ID]
    assert len(notes) == 1, f"ожидалась одна тревога, пришло: {[n.text for n in notes]}"
    assert "10.8.1.77" in notes[0].text and notes[0].force_sound
    assert "169.254.88" not in notes[0].text
    q = next(d for d in services.db.list_all_devices() if d.public_key == stranger[0])
    assert q.client_id == services.db.get_service_client_id(), "чужой пир не в карантине"

    assert services.reconcile_peers() == [], "тревога о том же пире повторилась на второй сверке"


# ── опрос трафика ────────────────────────────────────────────────────────────

def _dump(rows):
    """Текст `awg show awg0 dump`: строка интерфейса и пиры в полном формате."""
    lines = ["DUMMY-priv\tDUMMY-pub\t43125\toff"]
    for pub, ip, hs, rx, tx in rows:
        lines.append(f"{pub}\t(none)\t198.51.100.9:5000\t{ip}/32\t{hs}\t{rx}\t{tx}\t25")
    return "\n".join(lines) + "\n"


def _set_dump(monkeypatch, rows):
    text = _dump(rows)
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: awg.parse_dump(text), raising=False)


def test_online_count_does_not_include_probes_with_a_fresh_handshake(services, fake_awg, make_active_client,
                                                                     monkeypatch):
    _client, devs = _users(services, make_active_client, n=2, tg_id=7110)
    on, off = devs
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(on.public_key, on.address, now - 10, 100, 50),
                            (off.public_key, off.address, 0, 0, 0)]
              + [(pub, ip, now - 5, 999, 999) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "1", \
        f"онлайн посчитал зонды: {services.db.get_state('online_count')}"


def test_only_probes_online_gives_zero(services, fake_awg, make_active_client, monkeypatch):
    """Ни одного устройства на связи, а зонды живы — в панели «0», а не число
    голосующих."""
    _users(services, make_active_client, n=1, tg_id=7111)
    services.db.set_state("online_count", "5")             # прежняя цифра должна смениться
    now = int(timeutil.now().timestamp())
    _set_dump(monkeypatch, [(pub, ip, now - 5, 1, 1) for pub, ip in PROBES])
    services.poll_traffic()
    assert services.db.get_state("online_count") == "0"


def test_device_counters_are_untouched_by_probes(services, fake_awg, make_active_client, monkeypatch):
    """Трафик и рукопожатия устройств считаются как без зондов; зонды не
    оставляют в базе ни устройств, ни опорных выборок."""
    _client, devs = _users(services, make_active_client, n=2, tg_id=7112)
    a, b = devs
    now = int(timeutil.now().timestamp())
    devices_before = {d.id for d in services.db.list_all_devices()}

    _set_dump(monkeypatch, [(a.public_key, a.address, now - 30, 100, 50),
                            (b.public_key, b.address, 0, 0, 0)]
              + [(pub, ip, now - 5, 10_000, 20_000) for pub, ip in PROBES])
    services.poll_traffic()
    _set_dump(monkeypatch, [(a.public_key, a.address, now - 20, 130, 70),
                            (b.public_key, b.address, 0, 0, 0)]
              + [(pub, ip, now - 3, 90_000, 80_000) for pub, ip in PROBES])
    services.poll_traffic()

    fa, fb = services.db.get_device(a.id), services.db.get_device(b.id)
    assert (fa.traffic_rx_month, fa.traffic_tx_month) == (30, 20), "трафик устройства сбит зондами"
    assert (fb.traffic_rx_month, fb.traffic_tx_month) == (0, 0)
    assert fa.last_handshake == now - 20 and not fb.last_handshake
    assert {d.id for d in services.db.list_all_devices()} == devices_before, "опрос завёл записи на зонды"
    assert set(services.db.get_samples_all()) <= devices_before
