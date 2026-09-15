"""E2E: инвариант «одно живое меню» на переходах, где он ломался.

После текстового ввода и после итогов операций живым обязано остаться ровно
одно сообщение с кнопками — последнее; служебное (вопросы, ввод человека)
убирается при возврате в меню. Каждый тест — регресс-сторож конкретного
сценария из ревью v2.19.1.
"""
import pytest

from awgbot.core import config
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import client as ch
from awgbot.bot.handlers import settings as sh
from awgbot.bot.callbacks import BlockCB, ClientCB, ConfirmCB, DeviceCB, PauseCB, SetCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _msg(bot, uid, text):
    return FakeMessage(text=text, chat_id=uid, user_id=uid, bot=bot)


def _deleted(bot):
    return {r[2] for r in bot.records if r[0] == "delete_message"}


# ── настройки: ввод значения ─────────────────────────────────────────────────

async def test_settings_input_moves_nav_and_cleans_prompt(services, fake_bot, monkeypatch):
    """Раздел после ввода — живое меню; приглашение и ввод человека убраны.
    Раньше раздел уходил голым answer: нав-указатель стоял на приглашении с
    живой «Отмена», а «5» и вопрос оставались в чате навсегда."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    st = FakeState()
    cb, prompt = _cb(fake_bot, ADMIN)
    services.db.nav_touch(ADMIN, prompt.message_id)
    await sh.edit_value(cb, SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"),
                        st, services)
    typed = _msg(fake_bot, ADMIN, "5")
    await sh.receive_value(typed, st, services)
    assert services.db.get_nav_message_id(ADMIN) != prompt.message_id
    assert typed.message_id in _deleted(fake_bot), "ввод человека остался в чате"
    assert prompt.message_id in _deleted(fake_bot), "вопрос остался в чате"
    answers = [s for s in typed.sent if s[0] == "answer"]
    assert answers[0][1].startswith("✅ Частота опроса успешно изменена: ") and "→ <b>5</b> мин" in answers[0][1]
    assert answers[0][2] is None and answers[-1][2] is not None, "финишер без кнопок, раздел с кнопками"


async def test_settings_text_value_finisher_shows_old_and_new(services, fake_bot, monkeypatch):
    from awgbot.core import settings
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: {"app.client_config.dns1": "1.1.1.1",
                                                            "app.client_config.dns2": "1.0.0.1"}.get(k, real(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    st = FakeState(); await st.update_data(key="app.client_config.dns1", sec="srv")
    typed = _msg(fake_bot, ADMIN, "10.9.1.1")
    await sh.receive_value(typed, st, services)
    fin = [s for s in typed.sent if s[0] == "answer"][0][1]
    assert fin == "✅ DNS клиентов успешно изменён: 1.1.1.1, 1.0.0.1 → <b>10.9.1.1</b>."


async def test_settings_bad_input_is_tracked_reask(services, fake_bot):
    st = FakeState()
    await st.update_data(key="app.scheduler.monitor_minutes", sec="mon")
    typed = _msg(fake_bot, ADMIN, "abc")
    await sh.receive_value(typed, st, services)
    ids = set(services.db.pop_content_msg_ids(ADMIN))
    assert typed.message_id in ids and len(ids) >= 2, "переспрос и ввод не трекаются"


# ── понижение лимита: итог на месте вопроса, панель следом ───────────────────

async def test_lower_limit_confirm_result_then_panel(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6401, device_limit=5, name="Вася")
    services.add_device(client.id, "a"); services.add_device(client.id, "b")
    st = FakeState(); await st.update_data(client_id=client.id, pending_limit=1)
    cb, question = _cb(fake_bot, ADMIN)
    services.db.nav_touch(ADMIN, question.message_id)
    fake_bot.records.clear()
    await ah.edit_limit_confirm(cb, ConfirmCB(action="lower_limit", ref=client.id, yes=True),
                                services, st)
    edits = [s for s in question.sent if s[0] == "edit_text"]
    assert edits and "Лимит устройств профиля «Вася» изменён: 5 → 1" in edits[-1][1]
    assert edits[-1][2] is None, "итог — без кнопок"
    answers = [s for s in question.sent if s[0] == "answer"]
    assert "Панель администратора" in answers[-1][1] and answers[-1][2] is not None
    assert services.db.get_nav_message_id(ADMIN) != question.message_id
    assert ("edit_markup", ADMIN, question.message_id) not in fake_bot.records, \
        "панель гасили после отправки — живое меню оказывалось выше"


# ── блокировка с приостановкой: прежний экран гаснет, вопросы трекаются ──────

async def test_block_pause_dialog_keeps_one_live_menu(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6402, device_limit=5)
    st = FakeState()
    cb, screen = _cb(fake_bot, ADMIN)
    services.db.nav_touch(ADMIN, screen.message_id)
    await ah.admin_block_pause_yes(cb, BlockCB(target="cli", action="pause_yes", ref=client.id),
                                   st, services)
    assert ("edit_reply_markup", ADMIN) in fake_bot.records, "у «Да/Нет» живые кнопки"
    typed = _msg(fake_bot, ADMIN, "7")
    await ah.admin_block_pause_days(typed, services, st)
    assert services.db.get_nav_message_id(ADMIN) != screen.message_id
    assert typed.message_id in _deleted(fake_bot) or typed.message_id in set(services.db.pop_content_msg_ids(ADMIN))


async def test_client_pause_other_keeps_one_live_menu(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=6403, period_kind="year")
    st = FakeState()
    cb, screen = _cb(fake_bot, cl.tg_id)
    services.db.nav_touch(cl.tg_id, screen.message_id)
    await ch.pause_other(cb, PauseCB(action="other", ref=cl.id), cl, services, st)
    assert ("edit_reply_markup", cl.tg_id) in fake_bot.records
    typed = _msg(fake_bot, cl.tg_id, "3")
    await ch.pause_other_apply(typed, cl, services, st)
    assert services.db.get_nav_message_id(cl.tg_id) != screen.message_id


async def test_add_device_for_friend_shows_slots_and_parks_screen(services, fake_bot,
                                                                   make_active_client):
    cl = make_active_client(tg_id=6404, device_limit=3)
    services.add_device(cl.id, "Своё")
    st = FakeState()
    cb, screen = _cb(fake_bot, cl.tg_id)
    await ch.device_add_friend(cb, cl, services, st)
    assert ("edit_reply_markup", cl.tg_id) in fake_bot.records
    prompt = [s for s in screen.sent if s[0] == "answer"][-1][1]
    assert "1 из 3" in prompt and "для друга" in prompt


# ── переименование своего устройства клиентом: итог и меню следом ────────────

async def test_client_rename_returns_to_menu(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=6405)
    dc = services.add_device(cl.id, "Старое")
    st = FakeState(); await st.update_data(device_id=dc.device_id)
    typed = _msg(fake_bot, cl.tg_id, "Новое")
    await ch.client_device_edit_name_apply(typed, cl, services, st)
    answers = [s for s in typed.sent if s[0] == "answer"]
    assert "«Старое» → «Новое»" in answers[0][1]
    assert answers[-1][2] is not None, "после итога нет меню"
    assert services.db.get_device(dc.device_id).name == "Новое"


# ── выход из паузы: итог на месте, экран следом ──────────────────────────────

async def test_admin_resume_pause_result_then_card(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=6406, period_kind="year")
    ok, *_ = services.enter_pause(cl.id, 5)
    assert ok
    cb, card = _cb(fake_bot, ADMIN)
    services.db.nav_touch(ADMIN, card.message_id)
    await ah.admin_resume_pause(cb, ClientCB(action="resume_pause", client_id=cl.id), services)
    edits = [s for s in card.sent if s[0] == "edit_text"]
    assert edits and "выведен из приостановки" in edits[-1][1] and edits[-1][2] is None
    answers = [s for s in card.sent if s[0] == "answer"]
    assert answers and answers[-1][2] is not None, "карточка не пришла следом"
    assert services.db.get_nav_message_id(ADMIN) != card.message_id


async def test_client_resume_pause_result_then_info(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=6407, period_kind="year")
    ok, *_ = services.enter_pause(cl.id, 5)
    assert ok
    cl = services.db.get_client(cl.id)
    cb, screen = _cb(fake_bot, cl.tg_id)
    await ch.pause_resume(cb, PauseCB(action="resume", ref=cl.id), cl, services)
    edits = [s for s in screen.sent if s[0] == "edit_text"]
    assert edits and edits[-1][2] is None
    answers = [s for s in screen.sent if s[0] == "answer"]
    assert answers and answers[-1][2] is not None


# ── токен агента: сообщение удаляется и при отказе ───────────────────────────

async def test_gateway_token_message_deleted_even_when_rejected(services, fake_bot):
    st = FakeState()
    typed = _msg(fake_bot, ADMIN, "not-a-token")
    deleted = {"n": 0}

    async def delete():
        deleted["n"] += 1
    typed.delete = delete
    await sh.gateway_token_received(typed, st, services)
    assert deleted["n"] == 1
    assert any("не похоже на токен" in s[1] for s in typed.sent if s[0] == "answer")


# ── старый алиас «добавить устройство» у админа снят ─────────────────────────

def test_admin_router_has_no_device_add_alias():
    assert not hasattr(ah, "admin_dev_add_alias")
    assert DeviceCB(action="add").pack() == "d:add:0"     # клиентская кнопка жива


# ── удаление устройства клиентом: финишер, потом меню ────────────────────────

async def test_client_delete_last_device_goes_to_main(services, fake_bot, make_active_client):
    from awgbot.bot.callbacks import DelDeviceCB
    cl = make_active_client(tg_id=6410, device_limit=2)
    dc = services.add_device(cl.id, "Телефон")
    cb, screen = _cb(fake_bot, cl.tg_id)
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=dc.device_id, stage="confirm"),
                                   cl, services)
    edits = [s for s in screen.sent if s[0] == "edit_text"]
    assert edits[-1][1] == "🗑 Устройство «Телефон» удалено. Теперь можно добавить до 2 устройств."
    assert edits[-1][2] is None
    answers = [s for s in screen.sent if s[0] == "answer"]
    assert answers and answers[-1][2] is not None and "Устройства" not in answers[-1][1][:40]


async def test_client_delete_one_of_two_returns_to_device_list(services, fake_bot,
                                                                make_active_client):
    from awgbot.bot.callbacks import DelDeviceCB
    cl = make_active_client(tg_id=6411, device_limit=3)
    a = services.add_device(cl.id, "A"); services.add_device(cl.id, "B")
    cb, screen = _cb(fake_bot, cl.tg_id)
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=a.device_id, stage="confirm"),
                                   cl, services)
    edits = [s for s in screen.sent if s[0] == "edit_text"]
    assert "«A» удалено. Теперь можно добавить до 2 устройств." in edits[-1][1]
    answers = [s for s in screen.sent if s[0] == "answer"]
    assert "Твои устройства" in answers[-1][1]
    labels = [b.text for row in answers[-1][2].inline_keyboard for b in row]
    assert any("B" in l for l in labels)


async def test_connect_method_after_creation_says_menu(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=6412, device_limit=2)
    st = FakeState(); await st.update_data(dev_name="Тел", for_friend=False)
    typed = _msg(fake_bot, cl.tg_id, "0")
    await ch.device_add_traffic(typed, cl, services, st)
    answers = [s for s in typed.sent if s[0] == "answer" and s[2] is not None]
    labels = [b.text for row in answers[-1][2].inline_keyboard for b in row]
    assert "⬅️ В меню" in labels and "⬅️ Назад" not in labels


def test_limit_changed_notice_uses_arrow():
    from awgbot.bot import texts
    assert texts.limit_changed_notice(1, 2) == "Максимальное количество устройств для тебя изменено: 1 → 2."
    assert texts.limit_changed_notice(2, 0).endswith("2 → без ограничения.")
