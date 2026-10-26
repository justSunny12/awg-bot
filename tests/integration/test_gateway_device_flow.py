"""Устройство-шлюз в основном боте: единственность, наследование в переезде,
лимиты и порядок, пометка по пересланному сообщению, запреты, снятие."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.core import config, settings
from awgbot.core.blocks import DeviceBlock
from awgbot.domain.services import ServiceError
from awgbot.util import gwsign

PRIV = base64.b64encode(os.urandom(32)).decode()


@pytest.fixture()
def gw(services, fake_awg, make_active_client, monkeypatch):
    """Админ с двумя устройствами, второе — будущий шлюз; ключ линка подменён."""
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    monkeypatch.setattr(services, "_link_privkey", lambda gw=None: PRIV)
    services.modes = []
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: services.modes.append(mode))
    return admin, phone, pi


def _slot1(services, device_id):
    return services.db.gateway_add(device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)


def _gw_dev_id(services):
    gw = services.active_gateway()
    return gw.device_id if gw else None


def _claim(services, pub):
    return services.gateway_claim("перешли: " + gwsign.sign(PRIV, "claim", pub, host="NASPi"))


def test_the_claim_carries_the_capabilities_and_sends_no_file(gw, services):
    """caps в токене подписаны вместе с остальным (контракт канала); файла в
    ответ нет ни при каких caps — конфигурацию везёт канал."""
    _admin, _phone, pi = gw
    pub = services.db.get_device(pi.device_id).public_key
    assert "cfg1" not in _claim(services, pub)
    got = services.gateway_claim(gwsign.sign(PRIV, "claim", pub, caps=("cfg1", "token1")))
    assert got["status"] == "already" and "cfg1" not in got
    assert gwsign.verify(PRIV, gwsign.sign(PRIV, "claim", pub))["caps"] == []
    assert gwsign.verify(PRIV, gwsign.sign(PRIV, "claim", pub, caps=("cfg1",)))["caps"] == ["cfg1"]


def test_claim_marks_only_admin_device_and_only_once(gw, services, make_active_client):
    admin, phone, pi = gw
    dev = services.db.get_device(pi.device_id)
    res = _claim(services, dev.public_key)
    assert res["status"] == "marked" and _gw_dev_id(services) == dev.id
    assert services.db.get_device(dev.id).is_gateway == 1
    # тот же токен второй раз — отказ (nonce), новый токен — «уже шлюз»
    tok = gwsign.sign(PRIV, "claim", dev.public_key)
    services.gateway_claim(tok)
    with pytest.raises(ServiceError):
        services.gateway_claim(tok)
    assert _claim(services, dev.public_key)["status"] == "already"
    # чужой профиль — отказ
    client = make_active_client(name="Клиент", tg_id=777)
    other = services.add_device(client.id, "x")
    with pytest.raises(ServiceError):
        _claim(services, services.db.get_device(other.device_id).public_key)
    with pytest.raises(ServiceError):
        _claim(services, "NOSUCHKEY=")


def test_claim_refuses_when_another_gateway_is_assigned(gw, services):
    """Токен — запасной путь; смена шлюза только через настройки, и она
    меняет ключи, а не просит подтверждений и release."""
    admin, phone, pi = gw
    d_pi = services.db.get_device(pi.device_id); d_ph = services.db.get_device(phone.device_id)
    _claim(services, d_pi.public_key)
    with pytest.raises(ServiceError, match="Заменить устройство"):
        _claim(services, d_ph.public_key)
    assert _gw_dev_id(services) == d_pi.id


def test_gateway_state_reflects_bundle_and_link(gw, services, monkeypatch):
    """Экран «Шлюзы» и строка слота читают состояние, а не догадку: шлюза нет —
    «Шлюз не назначен»; назначен, но линк не отвечает — не зелёный; ожил —
    «🟢 Активен»."""
    admin, phone, pi = gw
    from awgbot.bot import texts
    assert services.gateway_states() == []
    assert "🇷🇺 РФ-доступ 🔴 шлюз не назначен\n" in texts.gateways_text([])
    _slot1(services, pi.device_id)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "down")
    monkeypatch.setattr(services, "routing_link_ok", lambda: False)
    st = services.gateway_states()[0]
    assert st["device"].id == pi.device_id and not st["link_ok"]
    line = texts.slot_line(services.gateway_states()[0])
    assert "NASPi" in line and "🟢" not in line and "Активен" in line, line
    monkeypatch.setattr(services, "routing_link_ok", lambda: True)
    assert "NASPi — 🟢 Активен" in texts.slot_line(services.gateway_states()[0])


def test_unique_index_forbids_two_preferred_slots(gw, services):
    """Предпочтительный — не более одного: держит БД, а не дисциплина в коде;
    одно устройство — не более чем в одном слоте."""
    import sqlite3
    admin, phone, pi = gw
    _slot1(services, pi.device_id)
    services.db.gateway_add(phone.device_id, "awglink2", 8443, "10.99.99.4/30")
    with pytest.raises(sqlite3.IntegrityError):
        with services.db._tx() as cur:
            cur.execute("UPDATE gateways SET preferred = 1 WHERE id = 2")
    with pytest.raises(sqlite3.IntegrityError):
        services.db.gateway_add(pi.device_id, "awglink3", 9443, "10.99.99.8/30")
    services.db.gateway_set_preferred(2)
    assert [g.id for g in services.db.gateways()] == [2, 1], "предпочтительный первым"


def test_gateway_is_locked_against_everything(gw, services, make_active_client):
    admin, phone, pi = gw
    _slot1(services, pi.device_id)
    for fn, args in ((services.block_device_manual, (pi.device_id, DeviceBlock.ADMIN_SILENT, False)),
                     (services.remove_device, (pi.device_id,)),
                     (services.make_device_friendly, (pi.device_id,)),
                     (services.generate_config, (pi.device_id,))):
        with pytest.raises(ServiceError):
            fn(*args)
    client = make_active_client(name="Клиент", tg_id=778)
    with pytest.raises(ServiceError):
        services.reassign_device(pi.device_id, client.id)
    assert services.generate_config(pi.device_id, for_bundle=True)["conf"], "бандлу конфиг выдаётся"
    # автоматические причины тоже не липнут
    services._device_set_block(pi.device_id, DeviceBlock.EXPIRY)
    assert services.db.get_device(pi.device_id).block_reason == 0
    # обычное устройство всё ещё блокируется
    services._device_set_block(phone.device_id, DeviceBlock.EXPIRY)
    assert services.db.get_device(phone.device_id).block_reason != 0


def test_gateway_traffic_is_not_the_profiles_but_the_device_is_listed_first(
        gw, services):
    """Трафик шлюза — это трафик ЧУЖИХ пользователей, идущий через него; в
    потреблении профиля админа и в его лимите ему не место. При этом само
    устройство — обычное: в списках первым, со своим значком. Счётчики —
    test_counters_agree_with_the_lists_they_head."""
    admin, phone, pi = gw
    _slot1(services, pi.device_id)
    services.db.add_traffic_bulk([(pi.device_id, 10 ** 12, 10 ** 12),          # «весь РФ-трафик»
                                  (phone.device_id, 5, 5)])
    t = services.db.get_client_traffic(admin.id)
    assert t["rx_month"] == 5 and t["tx_month"] == 5, "трафик шлюза в профиле не считается"
    # лимит профиля не срабатывает от шлюза
    services.db.update_client_fields(admin.id, traffic_limit=1024)
    notes = services.check_traffic_limits()
    assert not any("NASPi" in n.text for n in notes)
    assert services.db.get_device(pi.device_id).block_reason == 0

    devs = services.db.list_devices(admin.id)
    assert [d.name for d in devs] == ["NASPi", "phone"], "шлюз первым"
    import time as _t
    now = int(_t.time())
    services.db.update_device_fields(pi.device_id, last_handshake=now)
    services.db.update_device_fields(phone.device_id, last_handshake=now)
    rows = services.online_devices()
    assert rows[0][0].name == "NASPi", "шлюз первой строкой и среди онлайн"


def test_remove_unmarks_rekeys_and_disables_routing(gw, services, monkeypatch):
    admin, phone, pi = gw
    _slot1(services, pi.device_id)
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    prev = services.gateway_remove()
    assert prev.id == pi.device_id and _gw_dev_id(services) is None
    assert store.get("app.routing.enabled") is False
    assert services.modes == ["--rekey"], "прежняя машина теряет линк сама"
    assert services.gateway_remove() is None and services.modes == ["--rekey"]


def test_migration_hands_the_flag_to_the_twin(services, fake_awg, make_active_client, monkeypatch):
    from awgbot.infra import awg as infra_awg
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    peers = {}
    monkeypatch.setattr(infra_awg, "add_peer", lambda pub, psk, ip, iface=None: peers.__setitem__(pub, ip))
    monkeypatch.setattr(infra_awg, "remove_peer", lambda pub, iface=None: peers.pop(pub, None))
    monkeypatch.setattr(infra_awg, "read_occupied_ips", lambda iface=None: set())
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    pi = services.add_device(admin.id, "NASPi")
    _slot1(services, pi.device_id)
    import time as _t
    services.db.update_device_fields(pi.device_id, last_handshake=int(_t.time()))
    services.migration_start()
    twin_id = services.db.twins_by_origin()[pi.device_id]
    twin = services.db.get_device(twin_id)
    assert twin.is_gateway == 1, "двойник виден шлюзом уже в окне переезда"
    assert services.db.gateway_by_device(twin_id).device_id == pi.device_id, "слот — на исходной строке"
    assert services.db.twin_of_device(pi.device_id).id == twin_id
    assert services.db.count_devices(admin.id) == 1, \
        "пара шлюза — одна видимая строка, и она считается"
    services.db.update_device_fields(twin_id, last_handshake=int(_t.time()))
    services.migration_finish()
    assert services.db.get_device(pi.device_id) is None
    assert _gw_dev_id(services) == twin_id, "слот переехал к двойнику"


def test_agent_token_is_kept_on_the_server_until_the_channel_takes_it(gw, services, monkeypatch, tmp_path):
    """Токен бота шлюза спрашивается на сервере и хранится в его env (0600) —
    шлюзу его везёт канал после кода; не токен — отказ."""
    admin, phone, pi = gw
    env = tmp_path / "env"
    monkeypatch.setenv("AWG_BOT_ENV", str(env))
    assert services.gw_bot_token() == ""
    with pytest.raises(ServiceError):
        services.set_gw_bot_token("не токен")
    services.set_gw_bot_token("123456789:AA-token-value-long-enough-here")
    assert services.gw_bot_token().startswith("123456789:")
    assert oct(env.stat().st_mode)[-3:] == "600", "в файле токен — права 600"


def test_agent_token_write_keeps_the_other_secrets(gw, services, monkeypatch, tmp_path):
    """Файл секретов переписывается целиком. Пропади из него BOT_TOKEN или
    ADMIN_ID — бот не поднимется после ближайшего рестарта, и связь с ним
    останется только через SSH."""
    env = tmp_path / "env"
    env.write_text("BOT_TOKEN=111:aaa\n# комментарий\nADMIN_ID=42\n", encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(env))
    services.set_gw_bot_token("123456789:AA-token-value-long-enough-here")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "BOT_TOKEN=111:aaa" in lines and "ADMIN_ID=42" in lines
    assert "# комментарий" in lines
    assert sum(1 for l in lines if l.startswith("GW_BOT_TOKEN=")) == 1
    # повторная запись не плодит дублей
    services.set_gw_bot_token("987654321:BB-another-token-value-here")
    lines = env.read_text(encoding="utf-8").splitlines()
    assert sum(1 for l in lines if l.startswith("GW_BOT_TOKEN=")) == 1
    assert "BOT_TOKEN=111:aaa" in lines


def test_counters_agree_with_the_lists_they_head(gw, services):
    """Регресс, найденный глазами: «устройств добавлено 5» при шести строках в
    списке и «онлайн 5» при четырёх. Заголовок обязан совпадать с тем, что под
    ним, иначе это просто ложь."""
    from awgbot.bot import texts
    from awgbot.util import timeutil
    admin, phone, pi = gw
    _slot1(services, pi.device_id)
    now = int(timeutil.now().timestamp())
    for dev_id in (phone.device_id, pi.device_id):
        services.db.update_device_fields(dev_id, last_handshake=now)

    devices = services.db.list_devices(admin.id)
    used, _limit = services.device_quota(admin.id)
    assert used == len(devices), "счётчик слотов разошёлся со списком устройств"

    rows = services.online_devices()
    head = texts.online_devices_text(rows)
    assert head.startswith("📶 <b>Онлайн:</b> ") and f"профиль, {len(rows)} устройства\n\n" in head, head
    assert len([d for d, _ in rows]) == 2


def test_replacing_the_device_drops_the_channel_state(gw, services):
    """Замена устройства слота: снимок, сессия и возможности прежнего уходят —
    иначе новое их наследует, а доверенные адреса шлюзов считаются по обвязке
    прежнего."""
    import json
    from awgbot.util import gwlink, timeutil
    _admin, phone, pi = gw
    sid = _slot1(services, pi.device_id).id
    services.gwlink_session_opened(sid, "3.3.0", gwlink.PROTO, caps=("cfg1",))
    services.db.set_state(f"gwlink_snap_{sid}", json.dumps({"bundle": {"admin_ips": "10.8.1.3"}}))
    services.db.set_state(f"gwlink_snap_at_{sid}", timeutil.to_iso(timeutil.now()))
    assert services.gateway_trusted_ips() == {"10.8.1.3"}
    services.gateway_device_replace(sid, phone.device_id)              # замена устройства (верный код сопряжения)
    assert services.gwlink_snapshot(sid) == {}, "снимок прежнего устройства пережил замену"
    assert not services.db.get_state(f"gwlink_caps_{sid}"), "возможности прежнего агента пережили замену"
    assert services.gateway_trusted_ips() == set(), "адреса прежней обвязки пережили замену"


def test_forgetting_a_slot_clears_the_keys_of_the_removed_delivery(services):
    """Подтверждение настроек и итог применения файла — доставки, снятой в
    3.3.1: у слотов, заведённых раньше, ключи лежат, и снятие слота их чистит
    вместе с остальным канальным состоянием."""
    for key in ("gwlink_ack_2", "gwlink_applied_2"):
        services.db.set_state(key, '{"ok": true}')
    services.gwlink_forget(2)
    assert not services.db.get_state("gwlink_ack_2") and not services.db.get_state("gwlink_applied_2")
