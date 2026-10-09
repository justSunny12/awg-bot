"""E2E: коды у тех, кто уже в боте — второе устройство от
того же владельца, отказ на чужого, гость становится владельцем.

Экраны этих веток (одно устройство, единственное число) — в эталонах
tests/screens/{client,guest}.txt; здесь — БД, уведомления владельцу и админу
и формы для нескольких устройств, которых в эталонах нет."""
import pytest

from awgbot.core import config
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
    """Второй код того же владельца: устройство переходит к гостю, владелец
    узнаёт об этом."""
    owner = make_active_client(tg_id=6200, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 96200, "Телефон")
    d2 = services.add_device(owner.id, "Ноутбук")
    code = services.make_device_friendly(d2.device_id)
    msg = _msg(fake_bot, 96200, username="artem")
    fake_bot.records.clear()
    await ch.take_code_as_member(msg, services, guest, code)
    assert services.db.get_device(d2.device_id).holder_client_id == guest.id
    assert any("активировал устройство «Ноутбук»" in t for t in _sent_to(fake_bot, 6200))


async def test_guest_refused_code_from_another_owner_code_survives(services, fake_bot,
                                                                    make_active_client):
    """Код другого владельца у гостя с двумя устройствами: отказ во
    множественном числе, код не сгорает — гость может вернуться к нему,
    удалив прежние устройства."""
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


async def test_guest_becomes_owner_with_all_devices(services, fake_bot, make_active_client):
    """Гость с тремя устройствами активирует клиентский код с лимитом 2:
    все три переезжают и работают, итог объясняет перебор лимита, владелец
    и админ получают уведомления во множественном числе."""
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
        '🎉 Доступ открыт\n\n'
        'Переданные тебе устройства от профиля <a href="tg://user?id=6206">Вася</a> перенесены в твой '
        'профиль — перенастраивать ничего не нужно, они работают как раньше:\n'
        '• Телефон\n• Ноутбук\n• Планшет\n\n'
        'В твою подписку входит 2 устройства, а перенесено 3 — все они продолжают работать. '
        'Добавить новое получится, когда освободится место в рамках лимита.')
    assert _sent_to(fake_bot, 6206) == [
        '📤 Устройства «Телефон», «Ноутбук» и «Планшет» перешли к <a href="tg://user?id=96206">Артём</a> — '
        'он активировал собственную подписку, и устройства переехали в его профиль. '
        'У тебя теперь 0 из 5 устройств.']
    admin = _sent_to(fake_bot, config.ADMIN_ID)
    assert admin and admin[0].endswith("Перенесено переданных устройств: 3 (от Вася), лимит подписки 2.")
    new = services.db.get_client_by_tg(96206)
    assert not new.is_guest and services.db.count_devices(new.id) == 3
    assert services.db.count_devices(owner.id) == 0


async def test_guest_upgrade_within_limit_notifies_owner_once(services, fake_bot, make_active_client):
    """Гость с одним устройством стал владельцем: прежний владелец получает
    уведомление в единственном числе — и ровно одно."""
    owner = make_active_client(tg_id=6207, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 96207, "Телефон")
    created = services.create_client("Артём", 3, "year", 0)
    msg = _msg(fake_bot, 96207)
    fake_bot.records.clear()
    await ch.take_code_as_member(msg, services, guest, created.invite_code)
    sent = _sent_to(fake_bot, 6207)
    assert len(sent) == 1 and sent[0].startswith('📤 Устройство «Телефон» перешло к'), sent
