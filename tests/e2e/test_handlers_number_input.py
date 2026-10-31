"""E2E: числовой ввод в диалогах обеих ролей — «²», арабско-индийская цифра и
число длиннее девяти знаков переспрашиваются, диалог остаётся открытым, база
не тронута; лимит трафика выше потолка — тоже переспрос; диалог лимита
закрывается после записи, а не до неё.

Цена ошибки: int("²") бросал ValueError внутри обработчика — ни переспроса, ни
меню, состояние FSM висит; «12345678901 ГБ» уходило в базу и падало
OverflowError после state.clear() — диалог закрыт, лимит не записан, человек
не понял, что случилось.
"""
import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import PauseCB, PresetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import client as ch
from awgbot.bot.handlers.admin import broadcast as bc
from awgbot.bot.inputs import TRAFFIC_GB_MAX
from awgbot.bot.states import AddDevice, Broadcast, EditLimit, EditTrafficLimit, PauseDays
from awgbot.core import config
from awgbot.domain.services import ServiceError
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
G = 1024 ** 3
BAD = ("²", "⑤", "٣", "12345678901")


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _msg(bot, uid, text):
    return FakeMessage(text=text, chat_id=uid, user_id=uid, bot=bot)


def _asked(bot, uid, text) -> int:
    return sum(1 for r in bot.records if r[0] == "answer" and r[1] == uid and r[2] == text)


async def test_pause_days_odd_digits_are_asked_again_and_the_dialog_stays(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7101, period_kind="year")
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, _ = _cb(fake_bot, 7101)
    await ch.pause_other(cb, PauseCB(action="other", ref=client.id), cl, services, st)
    avail = services.pause_available_days(client.id)
    for bad in BAD:
        await ch.pause_other_apply(_msg(fake_bot, 7101, bad), cl, services, st)
    assert not services.db.get_client(client.id).is_paused
    assert await st.get_state() == PauseDays.value.state, "диалог закрылся без ответа"
    assert _asked(fake_bot, 7101, texts.pause_days_bad(avail)) == len(BAD)


async def test_admin_device_limit_odd_digits_are_asked_again(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7102, device_limit=2)
    st = FakeState()
    cb, _ = _cb(fake_bot, ADMIN)
    await ah.edit_limit_preset(cb, PresetCB(kind="cli_devs", ref=client.id, val=-1), services, st)
    for bad in BAD:
        await ah.edit_limit_apply(_msg(fake_bot, ADMIN, bad), services, st)
    assert services.db.get_client(client.id).device_limit == 2
    assert await st.get_state() == EditLimit.value.state
    assert _asked(fake_bot, ADMIN, texts.NUMBER_BAD_LIMIT) == len(BAD)


async def test_admin_traffic_limit_above_the_ceiling_is_asked_again(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7103, traffic_limit=5 * G)
    st = FakeState()
    cb, _ = _cb(fake_bot, ADMIN)
    await ah.edit_traffic_preset(cb, PresetCB(kind="cli_traffic", ref=client.id, val=-1), services, st)
    for bad in BAD + (str(TRAFFIC_GB_MAX + 1),):
        await ah.edit_traffic_apply(_msg(fake_bot, ADMIN, bad), services, st)
    assert services.db.get_client(client.id).traffic_limit == 5 * G
    assert await st.get_state() == EditTrafficLimit.value.state
    assert _asked(fake_bot, ADMIN, texts.NUMBER_BAD_TRAFFIC) == len(BAD) + 1
    await ah.edit_traffic_apply(_msg(fake_bot, ADMIN, str(TRAFFIC_GB_MAX)), services, st)
    assert services.db.get_client(client.id).traffic_limit == TRAFFIC_GB_MAX * G
    assert await st.get_state() is None


async def test_admin_traffic_limit_dialog_survives_a_refused_write(services, fake_bot, make_active_client, monkeypatch):
    """Запись отказана сервисом — диалог открыт, повтор ввода возможен; диалог
    закрывается только записью."""
    client = make_active_client(tg_id=7104, traffic_limit=5 * G)
    st = FakeState()
    cb, _ = _cb(fake_bot, ADMIN)
    await ah.edit_traffic_preset(cb, PresetCB(kind="cli_traffic", ref=client.id, val=-1), services, st)

    def refuse(client_id, new_b):
        raise ServiceError("база занята")
    monkeypatch.setattr(services, "set_client_traffic_limit", refuse)
    with pytest.raises(ServiceError):
        await ah.edit_traffic_apply(_msg(fake_bot, ADMIN, "7"), services, st)
    assert await st.get_state() == EditTrafficLimit.value.state, "диалог закрыт до записи"
    monkeypatch.undo()
    await ah.edit_traffic_apply(_msg(fake_bot, ADMIN, "7"), services, st)
    assert services.db.get_client(client.id).traffic_limit == 7 * G
    assert await st.get_state() is None


async def test_client_device_limit_for_a_friend_rejects_odd_digits_and_the_ceiling(
        services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7105, device_limit=3)
    cl = services.db.get_client(client.id)
    st = FakeState()
    await st.set_state(AddDevice.traffic)
    await st.update_data(for_friend=True, dev_name="Ноут")
    for bad in BAD + (str(TRAFFIC_GB_MAX + 1),):
        await ch.device_add_traffic(_msg(fake_bot, 7105, bad), cl, services, st)
    assert not [d for d in services.db.list_devices(client.id) if d.name == "Ноут"]
    assert await st.get_state() == AddDevice.traffic.state
    assert _asked(fake_bot, 7105, texts.NUMBER_BAD) == len(BAD) + 1


async def test_broadcast_days_odd_digits_are_asked_again(services, fake_bot):
    st = FakeState()
    await st.set_state(Broadcast.days)
    for bad in BAD:
        await bc.broadcast_days(_msg(fake_bot, ADMIN, bad), st, services)
    assert await st.get_state() == Broadcast.days.state
    assert _asked(fake_bot, ADMIN, texts.BROADCAST_DAYS_BAD) == len(BAD)


async def test_mail_wizard_port_rejects_odd_digits():
    from awgbot.bot.handlers.mailwizard import _host_port
    assert _host_port("imap.example.com:993") == ("imap.example.com", 993)
    assert _host_port("imap.example.com:⑨⑨③") is None
    assert _host_port("imap.example.com:99999") is None
