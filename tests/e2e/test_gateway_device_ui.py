"""Устройство-шлюз в чате: пересланный claim у админа, замена с подтверждением,
карточка шлюза, «Не шлюз?», приём release агентом и claim после бандла."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot.callbacks import DeviceCB, GwMarkCB
from awgbot.bot.handlers import admin as ah
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


@pytest.fixture()
def gwsetup(services, fake_awg, make_active_client, monkeypatch):
    admin = make_active_client(name="Админ", tg_id=ADMIN)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    monkeypatch.setattr(services, "_link_privkey", lambda: PRIV)
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    return admin, services.db.get_device(phone.device_id), services.db.get_device(pi.device_id)


async def test_forwarded_claim_marks_device(services, fake_bot, gwsetup):
    _, phone, pi = gwsetup
    msg = _amsg(fake_bot, "Перешли основному боту:\n" + gwsign.sign(PRIV, "claim", pi.public_key))
    await ah.gateway_claim_message(msg, services)
    assert services.db.gateway_device().id == pi.id
    assert any("помечено как шлюз" in s[1] for s in msg.sent if s[0] == "answer")
    bad = _amsg(fake_bot, "GW1:abc.def")
    await ah.gateway_claim_message(bad, services)
    assert any("не принято" in s[1] for s in bad.sent if s[0] == "answer")


async def test_replace_flow_asks_then_gives_release_for_the_old(services, fake_bot, gwsetup):
    _, phone, pi = gwsetup
    services.db.set_gateway(pi.id)
    msg = _amsg(fake_bot, gwsign.sign(PRIV, "claim", phone.public_key))
    await ah.gateway_claim_message(msg, services)
    assert services.db.gateway_device().id == pi.id, "без подтверждения шлюз прежний"
    assert any("Два шлюза" in s[1] for s in msg.sent if s[0] == "answer")
    cb, nav = _acb(fake_bot)
    await ah.gateway_replace_yes(cb, GwMarkCB(action="replace_yes", device_id=phone.id), services)
    assert services.db.gateway_device().id == phone.id
    tokens = [s[1] for s in nav.sent if s[0] == "answer" and "GW1:" in s[1]]
    assert tokens, "release для старого шлюза не выдан"
    data = gwsign.verify(PRIV, tokens[0])
    assert data["act"] == "release" and data["pub"] == pi.public_key


async def test_gateway_card_and_release(services, fake_bot, gwsetup):
    _, phone, pi = gwsetup
    services.db.set_gateway(pi.id)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    text, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert "🛰" in text and "шлюз условной маршрутизации" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🛑 Не шлюз?" in labels and "⚙️ Конфигурация шлюза" in labels and "✏️ Имя" in labels
    assert not any("Удалить" in l or "Заблокировать" in l or "подключения" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await ah.gateway_release_ask(cb, GwMarkCB(action="release_ask", device_id=pi.id), services)
    assert any("перестанет быть шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await ah.gateway_release_yes(cb, GwMarkCB(action="release_yes", device_id=pi.id), services)
    assert services.db.gateway_device() is None
    assert any("GW1:" in s[1] for s in nav.sent if s[0] == "answer"), "release для агента"
    # обычная карточка вернулась
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi.id), services)
    _, markup = next((s[1], s[2]) for s in nav.sent if s[0] == "edit_text")
    assert any("Удалить" in b.text for row in markup.inline_keyboard for b in row)


async def test_agent_accepts_release_and_sends_claim_after_bundle(tmp_path, fake_bot, monkeypatch):
    from awgbot.bot.handlers import gateway as gh
    from awgbot.domain import gateway as gw
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra import gwguard
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    pub = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "[Interface]\nPrivateKey = " + PRIV + "\n")
    monkeypatch.setattr(gwguard, "uplink_pubkey", lambda: ("awg0", pub))
    monkeypatch.setattr(gw, "_run", lambda argv, timeout=10: __import__("subprocess").CompletedProcess(argv, 0, b"", b""))
    msg = _amsg(fake_bot, "от основного бота:\n" + gwsign.sign(PRIV, "release", pub))
    await gh.gw_release_message(msg, svc, FakeState())
    assert any("больше не шлюз" in s[1] for s in msg.sent if s[0] == "answer")
    assert svc.gateway_mark_status() == "released"
    # применение бандла без пометки → claim отдельным сообщением
    monkeypatch.setattr(gwguard, "script_status", lambda: {"GW_STATUS": "unmarked"})
    out = svc.gateway_mark_outcome()
    assert out["claim"] and gwsign.verify(PRIV, out["claim"])["pub"] == pub


async def test_settings_offers_assign_when_no_gateway_and_marks_with_bundle(services, fake_bot, gwsetup, monkeypatch):
    """Без шлюза раздел предлагает «Назначить шлюз»; выбор → подтверждение →
    пометка → конфигурация файлом сразу. С шлюзом — «Конфигурация шлюза»."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    _, phone, pi = gwsetup
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True if k == "app.routing.enabled" else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda: (b"BUNDLE", "awg-gw-bundle.enc"))
    text, markup = await sh._screen("rt", services)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🛰 Назначить шлюз" in labels and "⚙️ Конфигурация шлюза" not in labels
    assert "не назначен" in text
    text, markup = await sh._screen("rt_gw", services)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("NASPi" in l for l in labels) and any("phone" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi.id), services)
    assert any("станет шлюзом" in s[1] for s in nav.sent if s[0] == "edit_text")
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi.id), services)
    assert services.db.gateway_device().id == pi.id
    assert any(s[0] == "document" for s in nav.sent), "конфигурация выдана сразу"
    text, markup = await sh._screen("rt", services)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "⚙️ Конфигурация шлюза" in labels and "🛰 Назначить шлюз" not in labels
    assert "NASPi" in text


async def test_forwarded_claim_sends_bundle_right_away(services, fake_bot, gwsetup, monkeypatch):
    _, phone, pi = gwsetup
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda: (b"BUNDLE", "awg-gw-bundle.enc"))
    msg = _amsg(fake_bot, gwsign.sign(PRIV, "claim", pi.public_key))
    await ah.gateway_claim_message(msg, services)
    assert any(s[0] == "document" for s in msg.sent), "после пометки конфигурация выдаётся сразу"


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
