"""Интерфейс агента: снимок vs живьём, перезапуск бота, побочные эффекты
разделов настроек. Тексты и раскладки экранов — в эталоне tests/screens/gateway.txt."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
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
    assert svc.probes == 2, "«Обновить» не сходил по пробам живьём"


async def test_stale_snapshot_falls_back_to_live(svc, fake_bot):
    """Протухший снимок тика не выдаётся за свежие показания — панель
    снимается живьём."""
    svc.db.set_state(GatewayServices._SNAPSHOT_KEY,
                     GwStatus(ts="2000-01-01T00:00:00+03:00").to_json())
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert svc.probes == 1, "протухший снимок показан без живого замера"


async def test_bot_restart_is_confirmed_then_promised_and_kept(svc, fake_bot):
    """Перезапуск — только после подтверждения; обещание запоминается, чтобы
    новый процесс нашёл его, исполнил и забыл (иначе «вернётся через
    несколько секунд» висит в чате навсегда)."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_confirm(cb, GwCB(action="botrestart"), svc)
    assert svc.bot_restarts == 0, "бот перезапущен без подтверждения"
    await gh.gw_bot_restart(cb, svc)
    assert svc.bot_restarts == 1
    assert svc.db.get_state("restart_wait") == f"{cfg.ADMIN_ID}:{msg.message_id}"
    # новый процесс: обещание исполнено и забыто
    await gh.restore_panel_after_restart(fake_bot, svc)
    assert svc.db.get_state("restart_wait") == "", "обещание не забыто — повторится на следующем старте"


async def test_health_screen_is_live(svc, fake_bot):
    """«Здоровье» — всегда живой замер, а не снимок тика."""
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1, "здоровье нарисовано без живого замера"


async def test_edit_flow_writes_value_and_returns_to_section(svc, fake_bot, monkeypatch):
    from awgbot.core import settings
    written = {}
    monkeypatch.setattr(settings, "set_value", lambda key, val: written.__setitem__(key, val))
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: written.get(key, default))
    monkeypatch.setattr(settings, "get_bool", lambda key, default=True: True)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.monitor_minutes"), svc, state)
    bad = FakeMessage(text="0", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_receive_value(bad, state, svc)
    assert written == {}, "значение вне границ записано"
    assert await state.get_state() is not None, "переспрос закрыл ввод"
    good = FakeMessage(text="5", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_receive_value(good, state, svc)
    assert written == {"app.gateway.monitor_minutes": 5}
    assert await state.get_state() is None, "ввод остался открытым после записи"


async def test_gateway_email_section_and_channel_offer(svc, fake_bot, monkeypatch):
    """Без ящика «Аварии на e-mail» не включаются (иначе аварии уходят в
    никуда); с ящиком — включаются. Канала бэкапов у шлюза нет."""
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_section(cb, GwCB(action="email"), svc, FakeState())
    assert "бэкап" not in msg.sent[-1][1].lower(), msg.sent[-1][1]
    await gh.gw_toggle(cb, GwCB(action="tgl", val="notifications.email_fallback"), svc)
    assert "notifications.email_fallback" not in store, "аварии на e-mail включены без ящика"
    svc.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    assert not await gh._shared(cb, GwCB(action="cyc", val="app.scheduler.backup_channel"), svc)
    assert "app.scheduler.backup_channel" not in store
    await gh.gw_toggle(cb, GwCB(action="tgl", val="notifications.email_fallback"), svc)
    assert store["notifications.email_fallback"] is True


async def test_two_refresh_taps_in_one_second_do_not_leave_a_second_panel(svc, fake_bot, monkeypatch):
    """Два нажатия «обновлено в … 🔄» за секунду: текст совпал, Telegram отвечает
    «message is not modified» — панель уже такая, новое меню под ней не шлётся
    (раньше любая ошибка правки превращалась во второе меню)."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    from awgbot.bot import texts
    from awgbot.bot.handlers.gateway import panel as gpanel

    class _Cmd:
        args = texts.REFRESH_PAYLOAD

    svc.db.set_nav_message_id(cfg.ADMIN_ID, 777)

    async def _not_modified(*a, **k):
        raise TelegramBadRequest(EditMessageText(text="x", chat_id=cfg.ADMIN_ID, message_id=777),
                                 "Bad Request: message is not modified")
    monkeypatch.setattr(fake_bot, "edit_message_text", _not_modified)
    sent = []

    async def _send_menu(*a, **k):
        sent.append(a)
    monkeypatch.setattr(gpanel, "send_menu", _send_menu)
    msg = FakeMessage(text="/start refresh", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState(), command=_Cmd())
    assert sent == [], "«без изменений» — а под панелью появилось второе меню"
