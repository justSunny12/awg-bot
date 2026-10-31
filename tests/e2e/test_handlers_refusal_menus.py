"""E2E: отказ посреди действия не оставляет чат без живого меню и без ответа.

  • выдача настроек устройства с панели админа: сбой сервера AWG (AwgError), а
    не только отказ по условию, — строка причины и панель админа заново;
  • выдача в гайде подключения: сообщение с выбором способа уже снято — после
    причины показывается главная;
  • ввод кода сопряжения шлюза: отказ сервиса на проверке кода — строка «⚠️ …»
    с «✖️ Отмена», диалог ввода остаётся (отказ не окончательный, код можно
    прислать снова).

Цена ошибки: AwgError пролетал мимо except ServiceError — сообщение с кнопками
снято, нового меню нет, у человека чат без единой кнопки; ввод кода уже
удалён из чата, ответа нет — диалог выглядит повисшим, а следующий текст
админа уходит в тот же обработчик как код.
"""
import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import DeviceCB, GuideCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import guide as gh
from awgbot.bot.handlers.admin import devices as admin_devices
from awgbot.bot.handlers.settings import gwjoin
from awgbot.bot.states import GatewayJoinCode
from awgbot.core import config
from awgbot.domain.services import ServiceError
from awgbot.infra.awg import AwgError
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _menus(nav) -> list:
    """Сообщения с инлайн-клавиатурой (живое меню), отправленные в ответ."""
    return [s for s in nav.sent if s[0] == "answer" and s[2] is not None
            and getattr(s[2], "inline_keyboard", None)]


async def _raise_awg(*a, **kw):
    raise AwgError("awg не отвечает")


async def test_admin_device_issue_on_awg_failure_returns_the_panel(services, fake_bot, make_active_client,
                                                                  monkeypatch):
    client = make_active_client(tg_id=7201)
    dev = services.db.get_device(services.add_device(client.id, "ноут").device_id)
    monkeypatch.setattr(admin_devices, "send_device_config", _raise_awg)
    cb, nav = _cb(fake_bot, ADMIN)
    await ah.admin_dev_gen(cb, DeviceCB(action="gen_link", device_id=dev.id), services)
    texts_sent = [s[1] for s in nav.sent if s[0] == "answer"]
    assert texts.issue_failed(AwgError("x")) in texts_sent, texts_sent
    assert _menus(nav), "после сбоя выдачи панель админа не показана — чат без меню"
    assert texts_sent.index(texts.issue_failed(AwgError("x"))) < len(texts_sent) - 1, "причина — до панели"


async def test_guide_issue_on_awg_failure_shows_the_main_menu(services, fake_bot, make_active_client, monkeypatch):
    client = make_active_client(tg_id=7202)
    cl = services.db.get_client(client.id)
    dev = services.db.get_device(services.add_device(client.id, "телефон").device_id)
    monkeypatch.setattr(gh, "_send_captioned", _raise_awg)
    cb, nav = _cb(fake_bot, 7202)
    await gh.guide_connect_deliver(cb, GuideCB(guide="connect", step=1, kind="link", dev=dev.id), services, cl)
    texts_sent = [s[1] for s in nav.sent if s[0] == "answer"]
    assert texts.issue_failed(AwgError("x")) in texts_sent
    assert _menus(nav), "после сбоя выдачи в гайде главная не показана — чат без меню"
    assert services.db.get_nav_message_id(7202) is not None


async def test_gwjoin_code_refusal_is_answered_and_the_dialog_stays(services, fake_bot, monkeypatch):
    st = FakeState()
    await st.set_state(GatewayJoinCode.value)
    await st.update_data(gwjoin_slot=1, gwjoin_id="a" * 16)

    def refuse(slot, jid, text):
        raise ServiceError("база занята — повтори через минуту")
    monkeypatch.setattr(services, "gwjoin_code", refuse)
    msg = FakeMessage(text="K7F2QX", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gwjoin.gwjoin_code_received(msg, st, services)
    assert msg.deleted, "ввод кода остался в чате"
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert answers and answers[-1][1] == "⚠️ база занята — повтори через минуту", answers
    assert answers[-1][2] is not None, "под ответом нет «✖️ Отмена»"
    assert await st.get_state() == GatewayJoinCode.value.state, "диалог ввода кода закрыт отказом"
