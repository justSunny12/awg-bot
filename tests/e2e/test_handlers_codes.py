"""E2E: коды у тех, кто уже в боте — второе устройство от
того же владельца, отказ на чужого, клиент берёт чужое устройство, гость
становится владельцем."""
import pytest

from awgbot.core import config
from awgbot.bot import texts
from awgbot.bot.handlers import client as ch
from tests.conftest import FakeMessage

pytestmark = pytest.mark.e2e


def _msg(bot, uid, text="", username=None):
    return FakeMessage(text=text, chat_id=uid, user_id=uid, bot=bot, username=username)


def _sent_to(bot, uid):
    return [r[2] for r in bot.records if r[0] == "send_message" and r[1] == uid]


def _lend(services, owner, tg, name, tg_name="Артём"):
    dc = services.add_device(owner.id, name)
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=tg, tg_name=tg_name)
    assert res.ok, res.reason
    return dc, res.holder


async def test_guest_takes_second_code_from_the_same_owner(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=6200, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 96200, "Телефон")
    d2 = services.add_device(owner.id, "Ноутбук")
    code = services.make_device_friendly(d2.device_id)
    msg = _msg(fake_bot, 96200, username="artem")
    fake_bot.records.clear()
    await ch.take_code_as_member(msg, services, guest, code)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers[0] == ('✅ Устройство «Ноутбук» от <a href="tg://user?id=6200">Вася</a> успешно добавлено.\n'
                          'Теперь у тебя 2 устройства.')
    assert "У тебя 2 устройства" in answers[-1], "главный экран следом"
    assert services.db.get_device(d2.device_id).holder_client_id == guest.id
    assert any("активировал устройство «Ноутбук»" in t for t in _sent_to(fake_bot, 6200))


async def test_guest_refused_code_from_another_owner_code_survives(services, fake_bot,
                                                                    make_active_client):
    owner = make_active_client(tg_id=6201, name="Вася", device_limit=3)
    other = make_active_client(tg_id=6202, name="Петя")
    _, guest = _lend(services, owner, 96201, "тест3")
    _, guest = _lend(services, owner, 96201, "Ноутбук")
    foreign = services.add_device(other.id, "Чужое")
    code = services.make_device_friendly(foreign.device_id)
    msg = _msg(fake_bot, 96201)
    await ch.take_code_as_member(msg, services, guest, code)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers == [
        'У тебя уже есть устройства «тест3» и «Ноутбук», переданные <a href="tg://user?id=6201">Вася</a>.\n'
        'Владеть устройствами от разных друзей одновременно не получится 😔\n'
        'Ты можешь либо удалить все устройства от <a href="tg://user?id=6201">Вася</a> и отправить мне '
        'этот код повторно, либо оставить всё как есть — решать тебе 🤷‍♂️']
    assert services.db.get_device_by_friend_code(code) is not None, "код сгорел"
    # с одним устройством — единственное число
    one_owner = make_active_client(tg_id=6203, name="Оля")
    _, g2 = _lend(services, one_owner, 96203, "Тел")
    msg = _msg(fake_bot, 96203)
    await ch.take_code_as_member(msg, services, g2, code)
    t = [s[1] for s in msg.sent if s[0] == "answer"][0]
    assert t.startswith('У тебя уже есть устройство «Тел», переданное') and "удалить устройство от" in t


async def test_client_takes_a_foreign_device(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=6204, name="Вася", device_limit=3)
    holder = make_active_client(tg_id=6205, name="Петя", device_limit=2)
    services.add_device(holder.id, "Своё")
    d = services.add_device(owner.id, "Чужое")
    code = services.make_device_friendly(d.device_id)
    msg = _msg(fake_bot, 6205)
    await ch.take_code_as_member(msg, services, holder, code)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers[0] == ('✅ Устройство «Чужое» от <a href="tg://user?id=6204">Вася</a> успешно добавлено.\n'
                          'Теперь у тебя 1 из 2 устройств (+ 1 от <a href="tg://user?id=6204">Вася</a>).')
    assert "Привет, Петя" in answers[-1]
    # клиентский инвайт клиенту — по-прежнему «уже есть доступ»
    created = services.create_client("Ещё", 1, "year", 0)
    msg = _msg(fake_bot, 6205)
    await ch.take_code_as_member(msg, services, holder, created.invite_code)
    assert [s[1] for s in msg.sent if s[0] == "answer"] == [texts.ACTIVATION_ALREADY]


async def test_guest_becomes_owner_with_all_devices(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=6206, name="Вася", device_limit=5)
    _, guest = _lend(services, owner, 96206, "Телефон")
    _, guest = _lend(services, owner, 96206, "Ноутбук")
    _, guest = _lend(services, owner, 96206, "Планшет")
    created = services.create_client("Артём", 2, "year", 0)
    msg = _msg(fake_bot, 96206, username="artem")
    fake_bot.records.clear()
    await ch.take_code_as_member(msg, services, guest, created.invite_code)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers[0] == (
        'Готово! Доступ активирован. 🎉\n\n'
        'Переданные тебе устройства от профиля <a href="tg://user?id=6206">Вася</a> перенесены в твой '
        'профиль — перенастраивать ничего не нужно, они работают как раньше:\n'
        '• Телефон\n• Ноутбук\n• Планшет\n\n'
        'В твою подписку входит 2 устройства, а перенесено 3 — все они продолжают работать. '
        'Добавить новое получится, когда освободится место в рамках лимита.')
    assert "Привет, Артём" in answers[-1] and "Помощь" not in answers[0]
    assert _sent_to(fake_bot, 6206) == [
        '📤 Устройства «Телефон», «Ноутбук» и «Планшет» перешли к <a href="tg://user?id=96206">Артём</a> — '
        'он активировал собственную подписку, и устройства переехали в его профиль. '
        'У тебя теперь 0 из 5 устройств.']
    admin = _sent_to(fake_bot, config.ADMIN_ID)
    assert admin and admin[0].endswith("Перенесено переданных устройств: 3 (от Вася), лимит подписки 2.")
    new = services.db.get_client_by_tg(96206)
    assert not new.is_guest and services.db.count_devices(new.id) == 3
    assert services.db.count_devices(owner.id) == 0


async def test_guest_upgrade_within_limit_has_no_overflow_note(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=6207, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 96207, "Телефон")
    created = services.create_client("Артём", 3, "year", 0)
    msg = _msg(fake_bot, 96207)
    fake_bot.records.clear()
    await ch.take_code_as_member(msg, services, guest, created.invite_code)
    first = [s[1] for s in msg.sent if s[0] == "answer"][0]
    assert first.endswith("• Телефон") and "входит" not in first
    assert _sent_to(fake_bot, 6207)[0].startswith('📤 Устройство «Телефон» перешло к')
