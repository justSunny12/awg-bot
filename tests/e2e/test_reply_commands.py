"""E2E: reply-команды (handlers/reply_commands.py) — «Скрыть» и «Отмена»."""
import pytest

from awgbot.bot.handlers import hide as hide_h
from awgbot.bot.handlers import reply_commands as rc
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


async def test_on_hide_deletes_message(services, fake_bot):
    nav = FakeMessage(chat_id=700, user_id=700, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=700, bot=fake_bot)
    await hide_h.on_hide(cb)
    assert any(r[0] == "delete" for r in fake_bot.records)
    assert cb.answers


async def test_on_cancel_clears_state_and_shows_menu(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=701)
    cl = services.db.get_client(client.id)
    st = FakeState()
    await st.update_data(dev_name="in-progress")           # незавершённый диалог
    m = FakeMessage(text="✖️ Отмена", chat_id=701, user_id=701, bot=fake_bot)
    await rc.on_cancel(m, st, services, role="client", client=cl)
    assert await st.get_data() == {}                        # FSM сброшен (clear)
    assert any(s[0] == "answer" and s[1] == "Отменено." for s in m.sent)


async def test_on_cancel_names_the_dialog(services, fake_bot, make_active_client):
    """«Отменено.» без уточнения не говорило, какой из диалогов прерван — а
    их у админа десяток. Финишер называет диалог по группе состояний."""
    from awgbot.bot.states import AddDevice, Broadcast, PauseDays
    from awgbot.bot import texts
    client = make_active_client(tg_id=702)
    cl = services.db.get_client(client.id)
    for st_obj, expected in ((AddDevice.name, "Добавление устройства отменено."),
                             (PauseDays.value, "Пауза подписки отменена."),
                             (Broadcast.days, "Объявление отменено.")):
        st = FakeState()
        await st.set_state(st_obj)
        m = FakeMessage(text="✖️ Отмена", chat_id=702, user_id=702, bot=fake_bot)
        await rc.on_cancel(m, st, services, role="client", client=cl)
        assert any(s[0] == "answer" and s[1] == expected for s in m.sent), (expected, m.sent)
        assert await st.get_state() is None
    assert texts.cancelled(None) == "Отменено."


# ── «✖️ Отмена» под приглашением к вводу (инлайн, CancelCB) ─────────────────

async def _prompt_rename(services, fake_bot, cl, dev_id):
    """Карточка устройства → «✏️ Имя»: приглашение на месте карточки."""
    from awgbot.bot.callbacks import DeviceCB
    from awgbot.bot.handlers import client as ch
    st = FakeState()
    nav = FakeMessage(chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    services.db.nav_touch(cl.tg_id, nav.message_id)
    cb = FakeCallback(message=nav, user_id=cl.tg_id, bot=fake_bot)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dev_id), cl, services, st)
    card = nav.sent[-1]
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dev_id),
                                            cl, services, st)
    return st, nav, card


async def test_inline_cancel_returns_the_same_screen_in_place(services, fake_bot, make_active_client):
    """«✖️ Отмена» — тот же экран на месте приглашения, без строки итога и
    без сообщения-следа; диалог сброшен, имя не тронуто."""
    from awgbot.bot.callbacks import CancelCB
    cl = make_active_client(tg_id=703)
    dev = services.add_device(cl.id, "iPhone")
    st, nav, card = await _prompt_rename(services, fake_bot, cl, dev.device_id)
    prompt = nav.sent[-1]
    assert prompt[1] == "✏️ Новое имя для устройства «iPhone»"
    btn = prompt[2].inline_keyboard[0][0]
    assert btn.text == "✖️ Отмена" and CancelCB.unpack(btn.callback_data) == CancelCB(kind="dev", ref=dev.device_id)
    fake_bot.records.clear()
    cb = FakeCallback(message=nav, user_id=703, bot=fake_bot)
    await rc.on_cancel_inline(cb, CancelCB.unpack(btn.callback_data), st, services,
                              role="client", client=cl)
    assert nav.sent[-1][0] == "edit_text" and nav.sent[-1][1] == card[1], "экран не тот же"
    assert [[b.text for b in r] for r in nav.sent[-1][2].inline_keyboard] == \
        [[b.text for b in r] for r in card[2].inline_keyboard]
    assert not any(r[0] in ("answer", "send_message") for r in fake_bot.records), "сообщение-след"
    assert await st.get_state() is None and await st.get_data() == {}
    assert services.db.get_device(dev.device_id).name == "iPhone"
    assert cb.answers == [(None, False)]


async def test_inline_cancel_unknown_screen_falls_back_to_main(services, fake_bot, make_active_client):
    """Экрана уже нет (устройство удалили, пока висело приглашение) — главная
    роли, а не молчащая кнопка."""
    from awgbot.bot.callbacks import CancelCB
    cl = make_active_client(tg_id=704)
    nav = FakeMessage(chat_id=704, user_id=704, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=704, bot=fake_bot)
    await rc.on_cancel_inline(cb, CancelCB(kind="dev", ref=999999), FakeState(), services,
                              role="client", client=cl)
    assert nav.sent[-1][0] == "edit_text" and nav.sent[-1][1].startswith("👋 "), nav.sent


async def test_screen_restored_by_cancel_is_not_swept_as_a_service_message(
        services, fake_bot, make_active_client):
    """Приглашение пишется в служебные (его убирают после ввода). Отмена
    возвращает на его место живой экран — и он не должен остаться в
    служебных: иначе следующий возврат на главную удалит живое меню вместе с
    мусором, и в Telegram оно придёт новым сообщением мимо учёта «одного
    живого меню»."""
    from awgbot.bot.callbacks import CancelCB, Menu
    from awgbot.bot.handlers import client as ch
    cl = make_active_client(tg_id=705)
    dev = services.add_device(cl.id, "iPhone")
    st, nav, _ = await _prompt_rename(services, fake_bot, cl, dev.device_id)
    cb = FakeCallback(message=nav, user_id=705, bot=fake_bot)
    await rc.on_cancel_inline(cb, CancelCB(kind="dev", ref=dev.device_id), st, services,
                              role="client", client=cl)
    fake_bot.records.clear()
    cb = FakeCallback(data=Menu(action="main").pack(), message=nav, user_id=705, bot=fake_bot)
    await ch.menu_main(cb, cl, services, FakeState())
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert nav.message_id not in deleted, "живой экран удалён как служебное сообщение"
