"""E2E: добор веток роутера клиента — выдача qr/file из меню, приостановка
(resume-ask/cancel), передача другу, добавление устройства другу, активация по
команде /code, холодный старт.
"""
import types

import pytest

from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import DeviceCB, Menu, PauseCB
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _fresh(services, client):
    return services.db.get_client(client.id)


async def test_menu_gen_qr_and_file(services, fake_bot, make_active_client):
    """QR и файл с главной при одном устройстве — сразу выдача, без выбора."""
    client = make_active_client(tg_id=5100)
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5100)
    await ch.menu_gen_pick(cb, Menu(action="gen_qr"), cl, services)
    assert [s[0] for s in nav.sent if s[0] != "edit_text"] == ["animation"], nav.sent
    cb2, nav2 = _cb(fake_bot, 5100)
    await ch.menu_gen_pick(cb2, Menu(action="gen_file"), cl, services)
    assert [s[0] for s in nav2.sent if s[0] != "edit_text"] == ["document"], nav2.sent
    assert not any(s[0] == "edit_text" for s in nav.sent + nav2.sent), "экран выбора из одного"


async def test_device_gen_file_sends_conf(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5101)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5101)
    await ch.device_gen(cb, DeviceCB(action="gen_file", device_id=dc.device_id), cl, services)
    assert any(s[0] == "document" for s in nav.sent)


async def test_device_transfer_ask(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5102)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5102)
    await ch.device_transfer_ask(cb, DeviceCB(action="transfer", device_id=dc.device_id), cl, services)
    text, labels = last_screen(nav)
    assert text == ("👤 Передать d другу?\nДруг получит это подключение; одно подключение на двух "
                    "устройствах работать не будет.\nЕсли устройство d твоё — сначала заведи себе новое")
    assert labels == ["⬅️ Отмена", "👤 Передать"], "«Отмена» — первой"
    assert nav.sent[-1][2].inline_keyboard[0][1].style is None, "передача — не разрушительное действие"
    assert services.db.get_device(dc.device_id).friend_status is None, "вопрос ничего не передаёт"


async def test_device_add_friend_presets_then_invite_with_share_and_copy(
        services, fake_bot, make_active_client):
    """Другу: «👤 Это для друга» меняет приглашение на месте; после имени —
    пресеты лимита не выше лимита профиля; пресет — устройство и
    приглашение с «📤 Отправить» и «📋 Скопировать», пояснение с «⬅️ В меню»."""
    from urllib.parse import parse_qs, urlparse
    from awgbot.bot.callbacks import PresetCB
    G = 1024 ** 3
    client = make_active_client(tg_id=5103, device_limit=3, traffic_limit=100 * G)
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5103)
    await ch.device_add_start(cb, DeviceCB(action="add"), cl, services, st)
    await ch.device_add_for_whom(cb, DeviceCB(action="add_friend"), cl, services, st)
    assert (await st.get_data()).get("for_friend") is True
    text, labels = last_screen(nav)
    assert text == "👤 Устройство для друга · 0 из 3 · займёт твой слот.\nКак назвать? Имя увидит друг"
    assert labels == ["📱 Это для меня", "✖️ Отмена"]

    typed = FakeMessage(text="Планшет", chat_id=5103, user_id=5103, bot=fake_bot)
    await ch.device_add_name(typed, cl, services, st)
    assert services.db.list_devices(client.id) == [], "устройство создано до выбора лимита"
    shown = [s for s in typed.sent if s[0] == "answer"][-1]
    assert shown[1] == "📊 Лимит трафика устройства Планшет · не больше 100 ГБ профиля"
    assert [b.text for r in shown[2].inline_keyboard for b in r] == [
        "10 ГБ", "50 ГБ", "100 ГБ", "✏️ Другое", "⬅️ Отмена"], "∞ — только у безлимитного"

    cb2, nav2 = _cb(fake_bot, 5103)
    await ch.device_add_limit_preset(cb2, PresetCB(kind="devlimit", ref=0, val=50), cl, services, st)
    dev = services.db.list_devices(client.id)[0]
    assert dev.name == "Планшет" and dev.traffic_limit == 50 * G and dev.friend_status == "pending"
    answers = [s for s in nav2.sent if s[0] == "answer"]
    invite, finisher = answers[0], answers[-1]
    code = dev.friend_code
    assert invite[1] == (f"Твоё приглашение для устройства «Планшет» 👇\n"
                         f"https://t.me/test_bot?start={code}\n"
                         f'или <a href="https://t.me/test_bot">в боте</a>: <code>/code {code}</code>')
    share, copy = invite[2].inline_keyboard[0]
    assert (share.text, copy.text) == ("📤 Отправить", "📋 Скопировать")
    plain = (f"Твоё приглашение для устройства «Планшет»: https://t.me/test_bot?start={code} "
             f"или в TG-боте (@test_bot): /code {code}")
    assert copy.copy_text.text == plain
    q = parse_qs(urlparse(share.url).query)
    assert share.url.startswith("https://t.me/share/url?") and q["url"] == [f"https://t.me/test_bot?start={code}"]
    assert q["text"] == [plain]
    assert finisher[1] == "☝️ Отправь приглашение другу — он активирует и получит Планшет"
    assert [b.text for r in finisher[2].inline_keyboard for b in r] == ["⬅️ В меню"]


async def test_device_add_friend_other_limit_refuses_above_profile(services, fake_bot, make_active_client):
    """«✏️ Другое» больше лимита профиля не примет: переспрос, устройства нет."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=5106, device_limit=3, traffic_limit=100 * 1024 ** 3)
    cl = _fresh(services, client)
    st = FakeState()
    await st.set_state(__import__("awgbot.bot.states", fromlist=["AddDevice"]).AddDevice.traffic)
    await st.update_data(for_friend=True, dev_name="Планшет", ctx_kind="main")
    cb, nav = _cb(fake_bot, 5106)
    await ch.device_add_limit_preset(cb, PresetCB(kind="devlimit", ref=0, val=-1), cl, services, st)
    text, labels = last_screen(nav)
    assert labels == ["✖️ Отмена"] and "100" in text, text
    typed = FakeMessage(text="101", chat_id=5106, user_id=5106, bot=fake_bot)
    await ch.device_add_traffic(typed, cl, services, st)
    assert [s[1] for s in typed.sent if s[0] == "answer"] == ["⚠️ Не больше 100 ГБ — лимита профиля"]
    assert services.db.list_devices(client.id) == []


async def test_pause_cancel_returns_to_subscription(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5104, period_kind="year")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5104)
    await ch.pause_cancel(cb, PauseCB(action="cancel", ref=client.id), cl, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("💳 Подписка: годовая") and labels == ["⏸️ Пауза", "⬅️ Назад"]
    assert not _fresh(services, client).is_paused
    assert cb.answers


async def test_code_activation_and_cold_start(services, fake_bot, make_active_client):
    # /code с кодом друга
    owner = make_active_client(tg_id=5105)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    st = FakeState()
    m = FakeMessage(text=f"/code {code}", chat_id=95105, user_id=95105, bot=fake_bot)
    cmd = types.SimpleNamespace(args=code)
    await ch.code_activation(m, cmd, services, st)
    assert services.db.get_device(dc.device_id).friend_tg_id == 95105
    # холодный старт (без кода) — приветствие-заглушка
    m2 = FakeMessage(text="/start", chat_id=95106, user_id=95106, bot=fake_bot)
    st2 = FakeState()
    await ch.start_cold(m2, st2)
    assert any(s[0] == "answer" for s in m2.sent)


async def test_code_activation_empty_arg(services, fake_bot):
    st = FakeState()
    m = FakeMessage(text="/code", chat_id=95107, user_id=95107, bot=fake_bot)
    cmd = types.SimpleNamespace(args=None)
    await ch.code_activation(m, cmd, services, st)
    assert any(s[0] == "answer" for s in m.sent)            # просьба прислать код
