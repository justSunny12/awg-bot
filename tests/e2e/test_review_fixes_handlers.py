"""Отказы домена в обработчиках бота доходят до админа словами, а не
всплывашкой «Создаю…» и тишиной:

- «start!» переезда профилей: отказ механики переезда (ServiceErrorMigration —
  не подкласс ServiceError: резолвер нового интерфейса не поднят) — запись в
  чате «⚠️ Переезд не начат» с причиной, раздел следом, функция выключена;
- слоты шлюзов: «⭐ При старте», «📡 Пинг», «▶️ Сделать активным» на слоте,
  которого уже нет (удалили между нажатием и записью), — alert «Такого шлюза
  нет», а не исключение посреди обработчика;
- отчёт о восстановлении на старте: отказ ожидания шлюзов (сервер в отъезде)
  не отменяет меню под отчётом.

Цена ошибки: исключение уходило из обработчика в журнал aiogram — у админа
вечный спиннер или одна всплывашка, на экране прежнее, и непонятно, начался ли
переезд, переключился ли шлюз; после восстановления — отчёт без живого меню.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import GwSlotCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.domain.services import ServiceError
from tests.conftest import FakeCallback, FakeMessage
from tests.e2e.test_gateway_slots_ui import _acb, _settled, _slot1, _slot2, slots

pytestmark = pytest.mark.e2e
__all__ = ["slots"]


# ── «start!» переезда: отказ механики — словами ──────────────────────────────

async def test_migration_start_refusal_is_shown_in_the_chat(services, mig, fake_bot, make_active_client,
                                                            monkeypatch):
    c = make_active_client(name="c", tg_id=7090)
    services.add_device(c.id, "Тел")

    def dns_down(prefix):
        raise RuntimeError("dnsmasq не стартовал")
    monkeypatch.setattr(services, "private_dns_on_migration_start", dns_down)
    msg = FakeMessage(chat_id=1, user_id=1, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=1, bot=fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="start!"), services)
    assert services.migration_running() is False
    shown = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert shown, "отказ старта не оставил записи в чате"
    assert shown[-1].startswith("⚠️ <b>Переезд не начат</b>\n\n")
    assert "Резолвер пользователей на новом интерфейсе не поднят: dnsmasq не стартовал." in shown[-1]
    assert shown[-1].endswith("Функция осталась выключенной, выдача идёт по-старому — устрани причину и включи снова")


# ── слоты шлюзов: слота уже нет ──────────────────────────────────────────────

def _gone(*a, **k):
    raise ServiceError("Такого шлюза нет")


async def test_preferred_toggle_on_a_vanished_slot_is_an_alert(services, slots, fake_bot, monkeypatch):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.setattr(services, "gateway_set_preferred", _gone)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert cb.answers == [("Такого шлюза нет", True)], cb.answers


async def test_ping_on_a_vanished_slot_is_an_alert(services, slots, fake_bot, monkeypatch):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.setattr(services, "gateway_screen_state", _gone)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert cb.answers == [("Такого шлюза нет", True)], cb.answers


async def test_switch_from_the_card_when_the_slot_vanished_answers_once(services, slots, fake_bot, monkeypatch):
    """Переключение прошло, а карточку уже не нарисовать: один alert, второго
    ответа (Telegram его молча роняет) и исключения нет."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    monkeypatch.setattr(services, "gateway_screen_state", _gone)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=2, val=""), services)
    assert cb.answers == [("Такого шлюза нет", True)], cb.answers


# ── отчёт о восстановлении: меню под ним — при любом исходе ожидания ────────

async def test_restore_report_menu_survives_a_refused_wait(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers.restore import report_restore_result
    services.pop_restore_failed_info = lambda: {}
    services.pop_restore_done = lambda: {"created_at": "2026-10-05T14:00:00+03:00", "version": "",
                                         "merge": {"line": "same", "slots": [], "lost": []}}

    def away(report):
        raise ServiceError("Сервер сейчас в отъезде")
    monkeypatch.setattr(services, "restore_wait_start", away, raising=False)
    await report_restore_result(fake_bot, services)
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) >= 2, f"под отчётом нет меню: {sent}"
