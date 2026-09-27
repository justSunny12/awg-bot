"""E2E: визард-гайды (handlers/guide.py) — запуск, навигация по шагам,
интерактивный шаг подключения, добавление устройства внутри гайда.
"""
import pytest

from awgbot.bot.handlers import guide as gh
from awgbot.bot.callbacks import DeviceCB, GuideCB, HelpCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_help_launch_and_step_nav(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6200)
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6200)
    await gh.help_launch(cb, HelpCB(platform="apple"), services, cl)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 6200)
    await gh.guide_step(cb2, GuideCB(guide="apple", step=1), services, cl)
    # шаг 1 apple теперь со скриншотом → фото (тип сменился с текста, пересоздание)
    assert any(s[0] == "photo" for s in nav2.sent)


async def test_guide_connect_step0_lists_devices(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6201)
    services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6201)
    await gh.guide_step(cb, GuideCB(guide="connect", step=0), services, cl)
    assert any(s[0] == "edit_text" for s in nav.sent)       # интерактивный шаг 0 с устройствами


async def test_guide_pick_device_delivers_config(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6202)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6202)
    await gh.guide_pick_device(cb, DeviceCB(action="gen_guide", device_id=dc.device_id), services, cl)
    # теперь не авто-выдача link+file, а шаг настройки с кнопками выбора способа
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert answers and answers[-1][2] is not None            # есть клавиатура выбора способа
    assert not any(s[0] == "document" for s in nav.sent)     # файл сам не уходит


async def test_guide_method_delivers_and_advances(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6212)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6212)
    # выбор способа «файл» на шаге 1 → выдаётся файл и показывается шаг 2
    await gh.guide_connect_deliver(
        cb, GuideCB(guide="connect", step=1, dev=dc.device_id, kind="file"), services, cl)
    assert any(s[0] == "document" for s in nav.sent)         # артефакт выдан
    assert any(s[0] == "answer" and "Подключаемся" in (s[1] or "") for s in nav.sent)


async def test_guide_method_back_reshows_choice(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6213)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6213)
    # «Назад» на шаге 2 → снова выбор способа для того же устройства
    await gh.guide_connect_methods(
        cb, GuideCB(guide="connect", step=1, dev=dc.device_id), services, cl)
    assert any(s[0] == "edit_text" and s[2] is not None for s in nav.sent)


async def test_guide_add_device_flow(services, fake_bot, make_active_client):
    """Новое устройство внутри гайда: только имя (лимит ставится в карточке) —
    приглашение на месте шага; после имени — «✅ Дев: создано» и сразу шаг
    выбора способа для него. Приглашение и ввод убраны."""
    client = make_active_client(tg_id=6203, device_limit=3)
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6203)
    await gh.guide_add_device(cb, GuideCB(guide="connect", step=-1), services, cl, st)
    prompt = [s for s in nav.sent if s[0] == "edit_text"][-1]
    assert prompt[1] == "➕ Новое устройство · 0 из 3\nКак назвать? Например: «iPhone»"
    assert [b.text for row in prompt[2].inline_keyboard for b in row] == ["✖️ Отмена"]
    m_name = FakeMessage(text="Дев", chat_id=6203, user_id=6203, bot=fake_bot)
    await gh.guide_add_device_name(m_name, services, cl, st)
    assert any(d.name == "Дев" for d in services.db.list_devices(client.id))
    answers = [s for s in m_name.sent if s[0] == "answer"]
    assert answers[0][1] == "✅ Дев: создано", "вопрос о лимите вместо создания"
    labels = [b.text for row in answers[-1][2].inline_keyboard for b in row]
    assert labels[:3] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"], labels
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, m_name.message_id} <= deleted, "приглашение или ввод остались в чате"
    assert await st.get_state() is None


async def test_guide_add_device_cancel_returns_to_the_guide_step(services, fake_bot,
                                                                 make_active_client):
    """«✖️ Отмена» на вводе имени внутри гайда — обратно на шаг 0 того же
    гайда (Apple — своего варианта), а не на главную: человек посреди
    настройки."""
    from awgbot.bot.handlers import reply_commands as rc
    from awgbot.bot.callbacks import CancelCB
    from awgbot.bot import guides
    client = make_active_client(tg_id=6206, device_limit=3)
    services.add_device(client.id, "Старое")
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6206)
    await gh.guide_add_device(cb, GuideCB(guide="connect_apple", step=-1), services, cl, st)
    cancel = CancelCB.unpack(nav.sent[-1][2].inline_keyboard[0][0].callback_data)
    cb2 = FakeCallback(message=nav, user_id=6206, bot=fake_bot)
    await rc.on_cancel_inline(cb2, cancel, st, services, role="client", client=cl)
    text, markup = nav.sent[-1][1], nav.sent[-1][2]
    assert text == guides.step_text("connect_apple", 0), text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🔗 Старое" in labels and "➕ Устройство" in labels, labels
    assert await st.get_state() is None and await st.get_data() == {}
    assert services.db.list_devices(client.id)[0].name == "Старое"


async def test_guide_add_device_full_limit(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6204, device_limit=1)
    services.add_device(client.id, "occupied")
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6204)
    await gh.guide_add_device(cb, GuideCB(guide="connect", step=-1), services, cl, st)
    assert cb.answers[-1][1] is True                        # лимит исчерпан → alert


async def test_guide_add_name_empty_rejected(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6205)
    cl = services.db.get_client(client.id)
    st = FakeState()
    await st.update_data(return_guide="connect")
    m = FakeMessage(text="  ", chat_id=6205, user_id=6205, bot=fake_bot)
    await gh.guide_add_device_name(m, services, cl, st)
    assert any(s[0] == "answer" for s in m.sent)
