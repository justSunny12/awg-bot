"""Устройство-шлюз в чате: назначение из настроек (своё устройство / новая
машина), смена и снятие без токенов, карточка шлюза, запасной путь через
пересланный claim, отчёт агента после применения."""
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
    """Без шлюза раздел предлагает «Назначить шлюз» → выбор вида → из моих
    устройств → подтверждение → токен агента → пометка с новыми ключами линка,
    файл первого применения и инструкция: устройство линка не знает, поэтому
    путь всегда полный, как у нового устройства."""
    _, phone, pi = gwsetup
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "🛰 Назначить шлюз" in labels and "⚙️ Конфигурация шлюза" not in labels
    assert "не назначен" in text
    _, markup = await sh._screen("rt_gw", services)
    assert _labels(markup)[:2] == ["📱 Из моих устройств", "➕ Новое устройство"]
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick_list(cb, GwMarkCB(action="pick_list"), services)
    labels = _labels(next(s[2] for s in nav.sent if s[0] == "edit_text"))
    assert any("NASPi" in l for l in labels) and any("phone" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi.id), services)
    assert any("станет шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi.id), services, st)
    assert any("Токен бота шлюза" in s[1] for s in nav.sent if s[0] == "edit_text"), "сначала токен"
    assert _gw_dev_id(services) is None
    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    assert _gw_dev_id(services) == pi.id
    assert services.modes == ["--rekey"], "ключи линка новые: устройство линка не знает"
    docs = _docs(msg)
    assert len(docs) == 1 and "первого применения" in docs[0][1], "открытый файл, руками"
    assert any("--install" in s[1] for s in msg.sent if s[0] == "answer"), "инструкция"
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "🛰 Шлюз: NASPi" in labels and "➕ Резервный шлюз" in labels
    assert "🛰 Назначить шлюз" not in labels and "NASPi" in text and "жду" in text


async def test_settings_change_gateway_rekeys_and_gives_plain_first_run_file(services, fake_bot, gwsetup):
    """Шлюз был, выбрали другое устройство: ключи линка новые, файл первого
    применения открытый; никаких токенов старому шлюзу."""
    _, phone, pi = gwsetup
    _slot1(services, pi.id)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=phone.id, slot=1), services)
    assert any("Сейчас шлюз — «NASPi»" in s[1] for s in nav.sent if s[0] == "edit_text")
    # Со сменой ключей файл едет открытым и ставится с нуля — значит нужен
    # токен агента, как и для новой машины.
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=phone.id, slot=1), services, st)
    assert any("Токен бота шлюза" in s[1] for s in nav.sent if s[0] == "edit_text")
    assert _gw_dev_id(services) == pi.id, "до токена шлюз прежний"
    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    nav = msg
    assert _gw_dev_id(services) == phone.id
    assert services.db.get_device(pi.id).is_gateway == 0
    assert services.modes == ["--rekey"]
    docs = _docs(nav)
    assert len(docs) == 1 and "первого применения" in docs[0][1]
    assert not any("GW1:" in (s[1] or "") for s in nav.sent), "токенов в новой схеме нет"
    # Машина ставится с нуля — значит и здесь показывается та же инструкция,
    # что для новой машины: одна команда со своей машины.
    assert any("--install" in s[1] and "scp" in s[1]
               for s in nav.sent if s[0] == "answer")


async def test_settings_new_machine_asks_for_the_agent_token_once(services, fake_bot, gwsetup, monkeypatch):
    """Токен бота-агента спрашивается ЗДЕСЬ и один раз: он уедет внутрь файла
    первого применения, и установка на шлюзе не задаст ни одного вопроса."""
    _, phone, pi = gwsetup
    stored: dict = {}
    monkeypatch.setattr(services, "gw_bot_token", lambda slot=None: stored.get("t", ""))
    monkeypatch.setattr(services, "set_gw_bot_token",
                        lambda t, slot=None: stored.__setitem__("t", t))
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask"), services)
    assert any("Новое устройство" in s[1] for s in nav.sent if s[0] == "edit_text")

    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes"), services, st)
    assert any("Токен бота шлюза" in s[1] for s in nav.sent if s[0] == "edit_text")
    assert _gw_dev_id(services) is None, "без токена ничего не создаём"

    msg = _amsg(fake_bot, "123456789:AA-token-value-long-enough-here")
    await sh.gateway_token_received(msg, st, services)
    gw = services.db.get_device(_gw_dev_id(services) or 0)
    assert gw is not None and gw.name == "Шлюз" and gw.id not in (phone.id, pi.id)
    assert services.modes == ["--rekey"]
    assert stored["t"].startswith("123456789:")
    docs = _docs(msg)
    assert len(docs) == 1 and "первого применения" in docs[0][1]
    instr = [s[1] for s in msg.sent if s[0] == "answer" and "--install" in s[1]]
    assert instr, "инструкция не показана"
    # Копирование и установка склеены: установка на шлюзе вопросов не задаёт,
    # значит отделять её от scp и заходить на шлюз вторым сеансом незачем.
    one = instr[0]
    assert "scp awg-gw-bundle.sh" in one and "ssh -t" in one
    assert "&amp;&amp;" in one, "команды не склеены в одну"
    assert "sh /root/awg-gw-bundle.sh --install" in one, "путь к только что скопированному файлу"
    assert one.index("scp") < one.index("ssh -t") < one.index("--install")
    # поставка внутри файла: с шлюза в России GitHub без туннеля не достать
    assert "githubusercontent" not in one

    # Токен уже есть — второй раз не спрашиваем
    services.db.gateway_delete(1)
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes"), services, FakeState())
    assert not any("Токен бота шлюза" in s[1] for s in nav.sent if s[0] == "edit_text")
    assert _gw_dev_id(services) is not None, nav.sent


async def test_remove_gateway_from_settings_and_card(services, fake_bot, gwsetup):
    """«Убрать шлюз» / «Не шлюз?»: флаг снят, ключи сменены, маршрутизация
    выключена; карточка снова обычная."""
    _, phone, pi = gwsetup
    _slot1(services, pi.id)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    text, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert "🛰" in text and "шлюз условной маршрутизации" in text
    labels = _labels(markup)
    assert "🛑 Не шлюз?" in labels and "⚙️ Конфигурация шлюза" in labels and "✏️ Имя" in labels
    assert labels[-2] == "📡 Пинг" and labels[-1] == "⬅️ Назад", "пинг — последним перед «Назад»"
    assert not any("Удалить" in l or "Заблокировать" in l or "подключения" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_ask(cb, GwMarkCB(action="remove_ask"), services)
    assert any("перестанет быть шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, GwMarkCB(action="remove_yes"), services)
    assert _gw_dev_id(services) is None
    assert services.modes == ["--rekey"]
    assert not any("GW1:" in (s[1] or "") for s in nav.sent)
    assert any("больше не шлюз" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    _, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert any("Удалить" in b.text for row in markup.inline_keyboard for b in row)
    # повторное снятие — сообщение, а не падение
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, GwMarkCB(action="remove_yes"), services)
    assert any("и так не назначен" in s[1] for s in nav.sent if s[0] == "edit_text")


async def test_forwarded_claim_is_fallback_only(services, fake_bot, gwsetup):
    """Пересланный claim: помечает, когда шлюза нет, и отдаёт конфигурацию;
    при назначенном другом шлюзе — отказ с отсылкой в настройки."""
    _, phone, pi = gwsetup
    msg = _amsg(fake_bot, "Перешли основному боту:\n" + gwsign.sign(PRIV, "claim", pi.public_key))
    await ah.gateway_claim_message(msg, services)
    assert _gw_dev_id(services) == pi.id
    assert any("назначено шлюзом" in s[1] for s in msg.sent if s[0] == "answer")
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


async def _agent_apply(fake_bot, monkeypatch, status: dict):
    from awgbot.bot.handlers import gateway as gh
    from awgbot.bot.callbacks import GwCB
    from awgbot.domain import gateway as gw
    from awgbot.domain.gateway import GatewayServices, GwStatus
    from awgbot.infra import gwguard
    from awgbot.infra.db import Database
    import tempfile, pathlib as _pl
    monkeypatch.setattr(gwguard, "script_status", lambda: status)
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", "K="))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "[Interface]\nPrivateKey = " + PRIV + "\n")
    db = Database(_pl.Path(tempfile.mkdtemp()) / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    monkeypatch.setattr(svc, "apply_bundle", lambda blob, ow=False: (True, "хвост вывода скрипта"))
    monkeypatch.setattr(svc, "status", lambda: GwStatus())
    cb, nav = _acb(fake_bot)
    st = FakeState(); await st.update_data(bundle=base64.b64encode(b"x").decode())
    await gh.gw_bundle_apply(cb, GwCB(action="apply"), svc, st)
    return nav


async def test_agent_reports_status_in_words_and_claims_only_when_unmarked(fake_bot, monkeypatch):
    nav = await _agent_apply(fake_bot, monkeypatch,
                             {"GW_STATUS": "confirmed", "UPLINK": "installed", "LINK": "up"})
    results = [s[1] for s in nav.sent if s[0] == "edit_text"]
    assert any("Аплинк обновлён и поднят, линк поднят, шлюз подтверждён." in t for t in results), results
    assert not any("хвост вывода" in t for t in results), "при успехе — отчёт, не хвост"
    assert not any("GW1:" in (s[1] or "") for s in nav.sent)
    nav = await _agent_apply(fake_bot, monkeypatch, {"GW_STATUS": "unmarked"})
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
    # «включить на всех» профиля шлюз не трогает
    services.db.update_device_fields(pi.id, routing_on=0)
    services.db.set_devices_routing(admin.id, True)
    assert services.db.get_device(pi.id).routing_on == 0
    # повторное назначение без флага — строки о снятии в отчёте нет
    res = services.gateway_setup(pi.id, rekey=True, slot_id=res["gateway"].id)
    assert res["routing_reset"] is False
    from awgbot.bot import texts
    assert "РФ-доступ у устройства снят" in texts.gateway_install_instructions(pi, routing_reset=True)
    assert "РФ-доступ у устройства снят" not in texts.gateway_install_instructions(pi)
