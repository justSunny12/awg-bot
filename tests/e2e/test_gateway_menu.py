"""Интерфейс агента v2.4.3: раскладка кнопок, снимок vs живьём, перезапуск бота."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage, FakeState


class _Svc(GatewayServices):
    def __init__(self, db):
        super().__init__(db)
        self.probes = 0
        self.bot_restarts = 0

    def status(self):
        self.probes += 1
        return GwStatus(link_up=True, handshake_age=5.0, hostname="pi")

    def restart_bot(self):
        self.bot_restarts += 1


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return _Svc(d)


def _labels(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def test_main_menu_layout():
    assert _labels(kb.gateway_panel_kb()) == [["🔄 Обновить", "🌡 Монитор здоровья"],
                                              ["🔧 Мастер восстановления"], ["⚙️ Настройки"]]
    assert _labels(kb.gateway_settings_kb()) == [["🔄 Обслуживание"], ["⬆️ Обновления бота"],
                                                 ["⬅️ В меню"]]
    assert _labels(kb.gateway_maint_kb()) == [["🔁 Перезапустить AWG"], ["🔁 Перезапустить бота"],
                                              ["⬅️ Назад"]]


def test_updates_back_leads_to_settings(monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get", lambda key, default=None: "day")
    rows = kb.gateway_updates_kb(False).inline_keyboard
    assert rows[-1][0].callback_data == GwCB(action="settings").pack()


async def test_start_uses_the_tick_snapshot_and_refresh_probes_live(svc, fake_bot):
    """/start и «В меню» рисуются из снимка тика (ноль проб), «Обновить» —
    живьём и обновляет снимок."""
    svc.snapshot()                                   # тик уже был
    assert svc.probes == 1
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.probes == 1, "/start сходил по пробам вместо снимка"
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_refresh(cb, svc, FakeState())
    assert svc.probes == 2
    assert any("РФ-шлюз (pi)" in t for kind, t, _ in msg.sent if kind == "edit_text")


async def test_stale_snapshot_falls_back_to_live(svc, fake_bot):
    svc.db.set_state(GatewayServices._SNAPSHOT_KEY,
                     GwStatus(ts="2000-01-01T00:00:00+03:00").to_json())
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.probes == 1


async def test_bot_restart_is_confirmed_then_promised_and_kept(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_confirm(cb, GwCB(action="botrestart"), svc)
    assert svc.bot_restarts == 0
    assert any("Перезапустить бота?" in t for kind, t, _ in msg.sent if kind == "edit_text")
    await gh.gw_bot_restart(cb, svc)
    assert svc.bot_restarts == 1
    assert svc.db.get_state("restart_wait") == f"{cfg.ADMIN_ID}:{msg.message_id}"
    # новый процесс: обещание → отчёт, панель следом
    await gh.restore_panel_after_restart(fake_bot, svc)
    assert svc.db.get_state("restart_wait") == ""
    edited = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edited and texts.BOT_RESTARTED in str(edited[-1])
    assert any(r[0] == "send_message" and "РФ-шлюз" in str(r) for r in fake_bot.records)


async def test_health_screen_is_live(svc, fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1
    assert any("Монитор здоровья" in t for kind, t, _ in msg.sent if kind == "edit_text")
