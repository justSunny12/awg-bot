"""Устройство-шлюз в чате: назначение из настроек (своё устройство / новая
машина), смена и снятие без токенов, запасной путь через пересланный claim,
отчёт агента после применения. Экраны этих путей — в эталонах
(tests/screens/admin.txt, gateway.txt); здесь — что меняется в БД, ключах линка
и выданных файлах, и ветки, которых в эталонах нет."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot.callbacks import DeviceCB, GwMarkCB, SetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from awgbot.util import gwsign
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


@pytest.fixture()
def gwsetup(services, fake_awg, make_active_client, monkeypatch):
    """Админ с двумя устройствами; ключ линка подменён; скрипт линка не
    запускается — режимы копятся в списке; бандлы — заглушки."""
    # Профиль админа безлимитный, как в жизни: шлюз считается устройством и
    # занимает слот, поэтому фиксированный лимит фикстуры мешал бы его завести.
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    modes = []
    monkeypatch.setattr(services, "_link_privkey", lambda gw=None: PRIV)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: modes.append(mode))
    def _issued(blob, name):
        from awgbot.util import timeutil
        services.db.set_state(services._GW_BUNDLE_ISSUED_KEY + "_1", timeutil.to_iso(timeutil.now()))
        return blob, name
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda slot=None: _issued(b"ENC", "awg-gw-bundle.enc"))
    monkeypatch.setattr(services, "gw_bundle_plain", lambda slot=None: _issued(b"PLAIN", "awg-gw-bundle.sh"))
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    # Токен агента живёт в /etc/awg-bot/env — в тестах держим его в памяти.
    token = {"v": ""}
    monkeypatch.setattr(services, "gw_bot_token", lambda slot=None: token["v"])
    monkeypatch.setattr(services, "set_gw_bot_token", lambda t, slot=None: token.__setitem__("v", t))
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True if k == "app.routing.enabled" else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "routing_link_ok", lambda: False)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "down")
    monkeypatch.setattr(services, "gateway_ping", lambda slot: None)
    services.modes = modes
    return admin, services.db.get_device(phone.device_id), services.db.get_device(pi.device_id)


def _slot1(services, device_id):
    return services.db.gateway_add(device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)


def _gw_dev_id(services):
    gw = services.active_gateway()
    return gw.device_id if gw else None


def _docs(nav):
    return [s for s in nav.sent if s[0] == "document"]


async def test_settings_assign_existing_device_goes_the_full_way(services, fake_bot, gwsetup):
    """Своё устройство → подтверждение → токен агента: до токена ничего не
    назначено, после — пометка с новыми ключами линка и файл первого
    применения (устройство линка не знает, поэтому путь всегда полный, как у
    нового); в «Шлюзах» новый шлюз не зелёный, пока не поднялся."""
    _, phone, pi = gwsetup
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi.id), services)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi.id), services, st)
    assert _gw_dev_id(services) is None, "назначено до токена"
    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    assert _gw_dev_id(services) == pi.id
    assert services.modes == ["--rekey"], "ключи линка новые: устройство линка не знает"
    assert len(_docs(msg)) == 1, f"открытый файл, руками: {msg.sent}"
    text, _ = await sh._screen("rt", services)
    assert "NASPi — 🟡 Активен, проверка связи" in text or "NASPi — 🔴 Активен" in text, \
        "новый шлюз ещё не поднялся — не зелёный"


async def test_settings_change_gateway_rekeys_and_gives_plain_first_run_file(services, fake_bot, gwsetup):
    """Шлюз был, выбрали другое устройство: ключи линка новые, файл первого
    применения открытый; никаких токенов старому шлюзу."""
    _, phone, pi = gwsetup
    _slot1(services, pi.id)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=phone.id, slot=1), services)
    # Со сменой ключей файл едет открытым и ставится с нуля — значит нужен
    # токен агента, как и для новой машины.
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=phone.id, slot=1), services, st)
    assert _gw_dev_id(services) == pi.id, "до токена шлюз прежний"
    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    nav = msg
    assert _gw_dev_id(services) == phone.id
    assert services.db.get_device(pi.id).is_gateway == 0
    assert services.modes == ["--rekey"]
    assert len(_docs(nav)) == 1, nav.sent
    assert not any("GW1:" in (s[1] or "") for s in nav.sent), "токенов в новой схеме нет"


async def test_settings_new_machine_asks_for_the_agent_token_once(services, fake_bot, gwsetup, monkeypatch):
    """Токен бота-агента спрашивается ЗДЕСЬ и один раз: он уедет внутрь файла
    первого применения, и установка на шлюзе не задаст ни одного вопроса."""
    _, phone, pi = gwsetup
    stored: dict = {}
    monkeypatch.setattr(services, "gw_bot_token", lambda slot=None: stored.get("t", ""))
    monkeypatch.setattr(services, "set_gw_bot_token",
                        lambda t, slot=None: stored.__setitem__("t", t))
    # новый слот — сразу к токену, без промежуточного подтверждения
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask"), services, st)
    assert not any("Новое устройство" in s[1] for s in nav.sent if s[0] == "edit_text"), "лишнее подтверждение"
    assert any(s[1].startswith("🤖 <b>Токен бота шлюза</b> — ") for s in nav.sent if s[0] == "edit_text")
    assert _gw_dev_id(services) is None, "без токена ничего не создаём"

    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    gw = services.db.get_device(_gw_dev_id(services) or 0)
    assert gw is not None and gw.name == "Шлюз" and gw.id not in (phone.id, pi.id)
    assert services.modes == ["--rekey"]
    assert stored["t"].startswith("123456789:")
    assert len(_docs(msg)) == 1, msg.sent

    # Токен уже есть — второй раз не спрашиваем
    services.db.gateway_delete(1)
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes"), services, FakeState())
    assert not any("Токен бота шлюза" in s[1] for s in nav.sent if s[0] == "edit_text")
    assert _gw_dev_id(services) is not None, nav.sent


async def test_remove_gateway_from_settings_and_card(services, fake_bot, gwsetup):
    """«Убрать шлюз» (единственный): флаг снят, ключи сменены, маршрутизация
    выключена; карточка устройства снова обычная; повторное снятие —
    сообщение, а не падение."""
    _, phone, pi = gwsetup
    _slot1(services, pi.id)
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, GwMarkCB(action="remove_yes"), services)
    assert _gw_dev_id(services) is None
    assert services.modes == ["--rekey"]
    assert not any("GW1:" in (s[1] or "") for s in nav.sent)
    assert any(s[1].startswith("🛑 NASPi больше не шлюз · РФ-доступ выключен") for s in nav.sent
               if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services, FakeState())
    _, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert any("Удалить" in b.text for row in markup.inline_keyboard for b in row)
    # повторное снятие — сообщение, а не падение
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, GwMarkCB(action="remove_yes"), services)
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and "и так не назначен" in shown[-1][1]
    back = shown[-1][2].inline_keyboard[-1][0]
    assert back.text == "⬅️ Назад" and back.callback_data == SetCB(sec="rt").pack(), "назад — в «Шлюзы»"


async def test_forwarded_claim_is_fallback_only(services, fake_bot, gwsetup):
    """Пересланный claim: помечает, когда шлюза нет, и отдаёт конфигурацию;
    при назначенном другом шлюзе — отказ с отсылкой в настройки."""
    _, phone, pi = gwsetup
    msg = _amsg(fake_bot, "Перешли основному боту:\n" + gwsign.sign(PRIV, "claim", pi.public_key))
    await ah.gateway_claim_message(msg, services)
    assert _gw_dev_id(services) == pi.id
    assert any(s[0] == "document" for s in msg.sent), "конфигурация сразу"
    msg = _amsg(fake_bot, gwsign.sign(PRIV, "claim", phone.public_key))
    await ah.gateway_claim_message(msg, services)
    assert _gw_dev_id(services) == pi.id
    assert any("не принято" in s[1] and "Заменить устройство" in s[1] for s in msg.sent if s[0] == "answer")
    bad = _amsg(fake_bot, "GW1:abc.def")
    await ah.gateway_claim_message(bad, services)
    assert any("не принято" in s[1] for s in bad.sent if s[0] == "answer")
    assert ah.has_gw_token("пояснение\n\n" + gwsign.sign(PRIV, "claim", "K=")) and not ah.has_gw_token(None)


def test_gateway_mark_rules(services, gwsetup, make_active_client):
    from awgbot.domain.services import ServiceError as SE
    _, phone, pi = gwsetup
    client = make_active_client(name="Клиент", tg_id=779)
    foreign = services.add_device(client.id, "x")
    with pytest.raises(SE):
        services.gateway_setup(foreign.device_id)
    res = services.gateway_setup(pi.id)
    assert res["previous"] is None and _gw_dev_id(services) == pi.id
    assert services.gateway_setup(pi.id, slot_id=1)["previous"] is None, "повтор — без изменений"
    res = services.gateway_setup(phone.id, slot_id=1)
    assert res["previous"].id == pi.id and _gw_dev_id(services) == phone.id
    assert [d.id for d in services.gateway_candidates()] == [pi.id], "текущий шлюз в кандидатах не нужен"


async def _agent_apply(fake_bot, monkeypatch, tmp_path, status: dict):
    import asyncio
    from awgbot.bot.handlers import gateway as gh
    from awgbot.bot.callbacks import GwCB
    from awgbot.domain import gateway as gw
    from awgbot.domain.gateway import GatewayServices, GwStatus
    from awgbot.infra import gwguard
    from awgbot.infra.db import Database
    monkeypatch.setattr(gwguard, "script_status", lambda: status)
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", "K="))
    monkeypatch.setattr(gw.base, "pathlib_read", lambda p: "[Interface]\nPrivateKey = " + PRIV + "\n")
    real_sleep = asyncio.sleep

    async def _no_wait(_s):                       # ожидание канала после пометки — без секунд
        await real_sleep(0)
    monkeypatch.setattr(gh.asyncio, "sleep", _no_wait)
    db = Database(tmp_path / f"gw-{status.get('GW_STATUS', 'x')}.db"); db.init_schema()
    svc = GatewayServices(db)
    monkeypatch.setattr(svc, "apply_bundle", lambda blob, ow=False: (True, "хвост вывода скрипта"))
    monkeypatch.setattr(svc, "status", lambda: GwStatus())
    cb, nav = _acb(fake_bot)
    st = FakeState(); await st.update_data(bundle=base64.b64encode(b"x").decode())
    try:
        await gh.gw_bundle_apply(cb, GwCB(action="apply"), svc, st)
    finally:
        db.close()
    return nav


async def test_agent_claims_when_unmarked_without_a_channel(fake_bot, monkeypatch, tmp_path):
    """Шлюз не назначен, а канала нет — агент отдаёт подписанное сообщение для
    пересылки основному боту ровно одним сообщением с кнопкой: иначе назначить
    шлюз нечем."""
    nav = await _agent_apply(fake_bot, monkeypatch, tmp_path, {"GW_STATUS": "unmarked"})
    claims = [s for s in nav.sent if s[0] == "answer" and "GW1:" in s[1]]
    assert len(claims) == 1 and claims[0][2] is not None and "перешли" in claims[0][1].lower()


async def test_migration_finish_sends_bundle_when_gateway_assigned(services, fake_bot, gwsetup, monkeypatch):
    """Финал переезда: двойник шлюза получил флаг и новые ключи — файл сразу."""
    _, phone, pi = gwsetup
    _slot1(services, pi.id)
    monkeypatch.setattr(services, "migration_available", lambda: True)
    monkeypatch.setattr(services, "migration_running", lambda: True)
    monkeypatch.setattr(services, "migration_finish", lambda: (1, 0, []))
    cb, nav = _acb(fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)
    assert any(s[0] == "document" for s in nav.sent), "после финала переезда — конфигурация шлюза"
    monkeypatch.setattr(services, "migration_finish", lambda: (0, 0, ["x"]))
    cb, nav = _acb(fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)
    assert not any(s[0] == "document" for s in nav.sent), "с ошибками финала файл не выдаём"


# ── шлюз и РФ-доступ ─────────────────────────────────────────────────────────

def test_gateway_device_is_out_of_ru_access(services, gwsetup, monkeypatch):
    """Шлюзу РФ-доступ не нужен никогда: его российский трафик на ВПС вернулся
    бы по линку на ту же малину. Назначение снимает флаг, а список
    переключателей, счётчики и набор маркировки шлюз не видят — даже если флаг
    остался от прежней версии, где шлюз в списке был (и первым)."""
    admin, phone, pi = gwsetup
    monkeypatch.setattr(services, "reconcile_routing", lambda: None)
    services.set_routing_device(pi.id, True)
    services.set_routing_device(phone.id, True)
    assert services.db.get_device(pi.id).routing_on == 1
    assert {d.id for d in services.routing_devices(admin.id)} == {phone.id, pi.id}

    res = services.gateway_setup(pi.id, rekey=True)
    assert res["routing_reset"] is True
    assert services.db.get_device(pi.id).routing_on == 0, "назначение сняло флаг"
    assert [d.id for d in services.routing_devices(admin.id)] == [phone.id], "шлюза в списке нет"
    assert services.routing_device_counts(admin.id) == (1, 1)
    # даже с флагом, поставленным в обход (прежняя версия) — не в наборе и не в счёте
    services.db.update_device_fields(pi.id, routing_on=1)
    assert services.routing_device_counts(admin.id) == (1, 1)
    assert pi.address not in sum(services.db.routing_active_addresses(ADMIN).values(), [])
    # «включить на всех» профиля, выдача разрешения владельцу и тумблер по id
    # из старой клавиатуры — шлюз не трогают
    services.db.update_device_fields(pi.id, routing_on=0)
    services.db.set_devices_routing(admin.id, True)
    assert services.db.get_device(pi.id).routing_on == 0
    services.db.set_owner_devices_routing(admin.id, True)
    assert services.db.get_device(pi.id).routing_on == 0
    services.set_routing_device(pi.id, True)
    assert services.db.get_device(pi.id).routing_on == 0
    # повторное назначение без флага — строки о снятии в отчёте нет
    res = services.gateway_setup(pi.id, rekey=True, slot_id=res["gateway"].id)
    assert res["routing_reset"] is False
    from awgbot.bot import texts
    assert "РФ-доступ у устройства снят" in texts.gateway_install_instructions(pi, routing_reset=True)
    assert "РФ-доступ у устройства снят" not in texts.gateway_install_instructions(pi)
