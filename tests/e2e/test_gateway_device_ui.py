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
    admin = make_active_client(name="Админ", tg_id=ADMIN)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    modes = []
    monkeypatch.setattr(services, "_link_privkey", lambda: PRIV)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: modes.append(mode))
    def _issued(blob, name):
        from awgbot.util import timeutil
        services.db.set_state(services._GW_BUNDLE_ISSUED_KEY, timeutil.to_iso(timeutil.now()))
        return blob, name
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda: _issued(b"ENC", "awg-gw-bundle.enc"))
    monkeypatch.setattr(services, "gw_bundle_plain", lambda: _issued(b"PLAIN", "awg-gw-bundle.sh"))
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True if k == "app.routing.enabled" else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "routing_link_ok", lambda: False)
    services.modes = modes
    return admin, services.db.get_device(phone.device_id), services.db.get_device(pi.device_id)


def _docs(nav):
    return [s for s in nav.sent if s[0] == "document"]


async def test_settings_assign_existing_device_no_rekey(services, fake_bot, gwsetup):
    """Без шлюза раздел предлагает «Назначить шлюз» → выбор вида → из моих
    устройств → подтверждение → пометка; ключи не менялись → шифрованный файл."""
    _, phone, pi = gwsetup
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "🛰 Назначить шлюз" in labels and "⚙️ Конфигурация шлюза" not in labels
    assert "не назначен" in text
    _, markup = await sh._screen("rt_gw", services)
    assert _labels(markup)[:2] == ["📱 Из моих устройств", "➕ Новая машина"]
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick_list(cb, services)
    labels = _labels(next(s[2] for s in nav.sent if s[0] == "edit_text"))
    assert any("NASPi" in l for l in labels) and any("phone" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi.id), services)
    assert any("станет шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi.id), services)
    assert services.db.gateway_device().id == pi.id
    assert services.modes == [], "тот же ключ линка: машина уже его знает"
    docs = _docs(nav)
    assert len(docs) == 1 and "боту шлюза" in docs[0][1], "шифрованный файл для чата агента"
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "⚙️ Конфигурация шлюза" in labels and "🔁 Сменить шлюз" in labels and "🛑 Убрать шлюз" in labels
    assert "🛰 Назначить шлюз" not in labels and "NASPi" in text and "жду" in text


async def test_settings_change_gateway_rekeys_and_gives_plain_first_run_file(services, fake_bot, gwsetup):
    """Шлюз был, выбрали другое устройство: ключи линка новые, файл первого
    применения открытый; никаких токенов старому шлюзу."""
    _, phone, pi = gwsetup
    services.db.set_gateway(pi.id)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=phone.id), services)
    assert any("Сейчас шлюз — «NASPi»" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=phone.id), services)
    assert services.db.gateway_device().id == phone.id
    assert services.db.get_device(pi.id).is_gateway == 0
    assert services.modes == ["--rekey"]
    docs = _docs(nav)
    assert len(docs) == 1 and "первого применения" in docs[0][1]
    assert not any("GW1:" in (s[1] or "") for s in nav.sent), "токенов в новой схеме нет"
    assert any("руками" in s[1] for s in nav.sent if s[0] == "edit_text")


async def test_settings_new_machine_creates_device_and_rekeys(services, fake_bot, gwsetup):
    _, phone, pi = gwsetup
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, services)
    assert any("Новая машина" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, services)
    gw = services.db.gateway_device()
    assert gw is not None and gw.name == "Шлюз" and gw.id not in (phone.id, pi.id)
    assert services.modes == ["--rekey"]
    docs = _docs(nav)
    assert len(docs) == 1 and "первого применения" in docs[0][1]


async def test_remove_gateway_from_settings_and_card(services, fake_bot, gwsetup):
    """«Убрать шлюз» / «Не шлюз?»: флаг снят, ключи сменены, маршрутизация
    выключена; карточка снова обычная."""
    _, phone, pi = gwsetup
    services.db.set_gateway(pi.id)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    text, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert "🛰" in text and "шлюз условной маршрутизации" in text
    labels = _labels(markup)
    assert "🛑 Не шлюз?" in labels and "⚙️ Конфигурация шлюза" in labels and "✏️ Имя" in labels
    assert not any("Удалить" in l or "Заблокировать" in l or "подключения" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_ask(cb, services)
    assert any("перестанет быть шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, services)
    assert services.db.gateway_device() is None
    assert services.modes == ["--rekey"]
    assert not any("GW1:" in (s[1] or "") for s in nav.sent)
    assert any("больше не шлюз" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    _, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert any("Удалить" in b.text for row in markup.inline_keyboard for b in row)
    # повторное снятие — сообщение, а не падение
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_yes(cb, services)
    assert any("и так не назначен" in s[1] for s in nav.sent if s[0] == "edit_text")


async def test_forwarded_claim_is_fallback_only(services, fake_bot, gwsetup):
    """Пересланный claim: помечает, когда шлюза нет, и отдаёт конфигурацию;
    при назначенном другом шлюзе — отказ с отсылкой в настройки."""
    _, phone, pi = gwsetup
    msg = _amsg(fake_bot, "Перешли основному боту:\n" + gwsign.sign(PRIV, "claim", pi.public_key))
    await ah.gateway_claim_message(msg, services)
    assert services.db.gateway_device().id == pi.id
    assert any("назначено шлюзом" in s[1] for s in msg.sent if s[0] == "answer")
    assert any(s[0] == "document" for s in msg.sent), "конфигурация сразу"
    msg = _amsg(fake_bot, gwsign.sign(PRIV, "claim", phone.public_key))
    await ah.gateway_claim_message(msg, services)
    assert services.db.gateway_device().id == pi.id
    assert any("не принято" in s[1] and "Сменить шлюз" in s[1] for s in msg.sent if s[0] == "answer")
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
        services.gateway_mark(foreign.device_id)
    res = services.gateway_mark(pi.id)
    assert res["previous"] is None and services.db.gateway_device().id == pi.id
    assert services.gateway_mark(pi.id)["previous"] is None, "повтор — без изменений"
    res = services.gateway_mark(phone.id)
    assert res["previous"].id == pi.id and services.db.gateway_device().id == phone.id
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
    services.db.set_gateway(pi.id)
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
