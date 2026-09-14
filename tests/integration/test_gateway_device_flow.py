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
    monkeypatch.setattr(services, "_link_privkey", lambda: PRIV)
    services.modes = []
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: services.modes.append(mode))
    return admin, phone, pi


def _claim(services, pub):
    return services.gateway_claim("перешли: " + gwsign.sign(PRIV, "claim", pub, host="NASPi"))


def test_claim_marks_only_admin_device_and_only_once(gw, services, make_active_client):
    admin, phone, pi = gw
    dev = services.db.get_device(pi.device_id)
    res = _claim(services, dev.public_key)
    assert res["status"] == "marked" and services.db.gateway_device().id == dev.id
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
    with pytest.raises(ServiceError, match="Сменить шлюз"):
        _claim(services, d_ph.public_key)
    assert services.db.gateway_device().id == d_pi.id


def test_setup_existing_device_rekeys_only_when_asked(gw, services):
    admin, phone, pi = gw
    res = services.gateway_setup(pi.device_id)
    assert res["previous"] is None and not res["created"] and not res["rekeyed"]
    assert services.modes == [], "тот же ключ линка — скрипт не трогаем"
    res = services.gateway_setup(phone.device_id, rekey=True)
    assert res["previous"].id == pi.device_id and res["rekeyed"]
    assert services.db.gateway_device().id == phone.device_id
    assert services.db.get_device(pi.device_id).is_gateway == 0
    assert services.modes == ["--rekey"]
    assert services.gateway_setup(phone.device_id, rekey=True)["previous"] is None, "то же устройство — prev нет"
    with pytest.raises(ServiceError):
        services.gateway_setup(999999)


def test_setup_new_machine_creates_device_and_rekeys(gw, services):
    admin, phone, pi = gw
    before = {d.id for d in services.db.list_devices(admin.id)}
    res = services.gateway_setup(None)
    dev = res["device"]
    assert res["created"] and res["rekeyed"] and dev.name == "Шлюз" and dev.id not in before
    assert dev.is_gateway == 1 and services.db.gateway_device().id == dev.id
    assert services.modes == ["--rekey"]
    assert dev.id not in [d.id for d in services.gateway_candidates()]


def test_gateway_state_reflects_bundle_and_link(gw, services, monkeypatch):
    admin, phone, pi = gw
    from awgbot.bot import texts
    assert services.gateway_state()["device"] is None
    assert "не назначен" in texts.settings_routing_gateway_line(services.gateway_state())
    services.db.set_gateway(pi.device_id)
    monkeypatch.setattr(services, "routing_link_ok", lambda: False)
    st = services.gateway_state()
    assert st["device"].id == pi.device_id and not st["link_ok"]
    line = texts.settings_routing_gateway_line(st)
    assert "NASPi" in line and ("жду" in line or "не отвечает" in line)
    monkeypatch.setattr(services, "routing_link_ok", lambda: True)
    assert "работает" in texts.settings_routing_gateway_line(services.gateway_state())


def test_unique_index_forbids_two_gateways(gw, services):
    import sqlite3
    admin, phone, pi = gw
    services.db.set_gateway(pi.device_id)
    with pytest.raises(sqlite3.IntegrityError):
        with services.db._tx() as cur:
            cur.execute("UPDATE devices SET is_gateway = 1 WHERE id = ?", (phone.device_id,))


def test_gateway_is_locked_against_everything(gw, services, make_active_client):
    admin, phone, pi = gw
    services.db.set_gateway(pi.device_id)
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


def test_gateway_is_outside_limits_and_first_in_lists(gw, services, monkeypatch):
    admin, phone, pi = gw
    services.db.set_gateway(pi.device_id)
    services.db.add_traffic(pi.device_id, 10 ** 12, 10 ** 12)         # «весь РФ-трафик»
    services.db.add_traffic(phone.device_id, 5, 5)
    t = services.db.get_client_traffic(admin.id)
    assert t["rx_month"] == 5 and t["tx_month"] == 5, "трафик шлюза в профиле не считается"
    assert services.db.count_devices(admin.id) == 1, "шлюз не занимает слот"
    devs = services.db.list_devices(admin.id)
    assert [d.name for d in devs] == ["NASPi", "phone"], "шлюз первым"
    # лимит профиля не срабатывает от шлюза
    services.db.update_client_fields(admin.id, traffic_limit=1024)
    notes = services.check_traffic_limits()
    assert not any("NASPi" in n.text for n in notes)
    assert services.db.get_device(pi.device_id).block_reason == 0
    # онлайн: шлюз первым, в счёте его нет
    import time as _t
    now = int(_t.time())
    services.db.update_device_fields(pi.device_id, last_handshake=now)
    services.db.update_device_fields(phone.device_id, last_handshake=now)
    rows = services.online_devices()
    assert rows[0][0].name == "NASPi"
    from awgbot.bot import texts
    assert "онлайн (1)" in texts.online_devices_text(rows)
    assert "🛰" in texts.device_label(services.db.get_device(pi.device_id))


def test_remove_unmarks_rekeys_and_disables_routing(gw, services, monkeypatch):
    admin, phone, pi = gw
    services.db.set_gateway(pi.device_id)
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    prev = services.gateway_remove()
    assert prev.id == pi.device_id and services.db.gateway_device() is None
    assert store.get("app.routing.enabled") is False
    assert services.modes == ["--rekey"], "прежняя машина теряет линк сама"
    assert services.gateway_remove() is None and services.modes == ["--rekey"]


def test_bundle_env_carries_gateway_key_and_uplink_conf(gw, services, monkeypatch, tmp_path):
    import builtins, os as _os, subprocess as _sp
    admin, phone, pi = gw
    services.db.set_gateway(pi.device_id)
    dev = services.db.get_device(pi.device_id)
    (tmp_path / "gw-awglink.conf").write_text("[Interface]\nPrivateKey = " + PRIV + "\n")
    (tmp_path / "awg-gw-bundle.sh").write_bytes(b"#!/bin/sh\n#__GW_SETUP_BELOW__\n")
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: real_open(
        tmp_path / _os.path.basename(str(p)) if str(p).startswith("/root/") else p, *a, **k))
    seen = {}
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: seen.update(env or {}))
    services.gw_bundle_encrypted()
    assert seen["GATEWAY_PUBKEY"] == dev.public_key and seen["GATEWAY_PREV_PUBKEY"] == ""
    conf = base64.b64decode(seen["UPLINK_B64"]).decode()
    assert "Table = off" in conf and "DNS" not in conf and dev.private_key in conf


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
    services.db.set_gateway(pi.device_id)
    import time as _t
    services.db.update_device_fields(pi.device_id, last_handshake=int(_t.time()))
    services.migration_start()
    twin_id = services.db.twins_by_origin()[pi.device_id]
    twin = services.db.get_device(twin_id)
    assert twin.is_gateway == 1, "двойник виден шлюзом уже в окне переезда"
    assert services.db.gateway_device().id == pi.device_id, "настоящий флаг — на исходной строке"
    assert services.db.twin_of_device(pi.device_id).id == twin_id
    assert services.db.count_devices(admin.id) == 0, "пара шлюза не занимает слот"
    services.db.update_device_fields(twin_id, last_handshake=int(_t.time()))
    services.migration_finish()
    assert services.db.get_device(pi.device_id) is None
    assert services.db.gateway_device().id == twin_id, "флаг переехал к двойнику"


def test_new_gateway_machine_ignores_the_admin_device_limit(gw, services):
    """Шлюз из лимита исключён по смыслу — через него идёт трафик всех. Но флаг
    ставится строкой позже, и админ с выбранным лимитом не мог завести себе
    шлюз вовсе: отказ приходил раньше, чем устройство успевало им стать."""
    admin, phone, pi = gw
    from awgbot.domain.services import LimitReached
    services.db.update_client_fields(admin.id, device_limit=2)
    with pytest.raises(LimitReached):
        services.add_device(admin.id, "третье")            # лимит работает как работал
    res = services.gateway_setup(None)
    assert res["created"] and services.db.gateway_device().id == res["device"].id


def test_agent_token_travels_inside_the_first_run_bundle(gw, services, monkeypatch, tmp_path):
    """Токен агента и ADMIN_ID уезжают в файл первого применения — ради этого
    установка на шлюзе и не задаёт вопросов. Строки лежат ПОСЛЕ exec: при
    запуске бандла они данные, а не команды."""
    admin, phone, pi = gw
    env = tmp_path / "env"
    monkeypatch.setenv("AWG_BOT_ENV", str(env))
    assert services.gw_bot_token() == ""
    with pytest.raises(ServiceError):
        services.set_gw_bot_token("не токен")
    services.set_gw_bot_token("123456789:AA-token-value-long-enough-here")
    assert services.gw_bot_token().startswith("123456789:")
    assert oct(env.stat().st_mode)[-3:] == "600", "в файле токен — права 600"

    plain = b"#!/bin/sh\nexec /opt/awg-gw/routing-gw-setup.sh\n#__GW_SETUP_BELOW__\n# script\n"
    out = services._bundle_with_agent(plain)
    text = out.decode()
    assert 'AGENT_BOT_TOKEN="123456789:AA-token-value-long-enough-here"' in text
    assert f'AGENT_ADMIN_ID="{config.ADMIN_ID}"' in text
    assert text.index("exec ") < text.index("AGENT_BOT_TOKEN"), \
        "строки попали в исполняемую часть бандла"
    assert text.index("AGENT_BOT_TOKEN") < text.index("#__GW_SETUP_BELOW__")


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


def test_bundle_without_a_token_stays_as_it_was(gw, services, monkeypatch, tmp_path):
    """Токена нет — файл первого применения собирается без него, и установка на
    шлюзе честно вернётся к вопросам, а не получит пустой токен."""
    monkeypatch.setenv("AWG_BOT_ENV", str(tmp_path / "пусто"))
    plain = b"#!/bin/sh\nexec x\n#__GW_SETUP_BELOW__\n"
    assert services._bundle_with_agent(plain) == plain
