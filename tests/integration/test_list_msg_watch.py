"""Посредник запросов сессии бота (lists/msgs/watch) на настоящем Bot и
настоящей базе: что после отправки, правки и удаления оказалось в записи
сообщения и в снимке; снятие записей с кнопками действия при передаче
Telegram резерву.

Цена ошибки: запись без текста после метки «-n» — правка ссылки вернула бы
сообщению текст без метки, и ссылка из уведомления перерисовала бы живое меню
выше в чате; `sent` по часам хоста, а не из ответа Telegram, — «Скрыть»
снимется не через сутки, а раньше или после 48 часов, когда удалить уже
нельзя; чужой итог, принятый за свой, — следующее меню погасит экран в один
итог, которого человек на этом экране не видел; запись, пережившая правку без
ссылки, — такт поправит уже другой экран; снимок, не отпущенный после
правки, — нельзя открыть заново; `deleteMessages`, снявший запись, — у
неудалённого пакетом сообщения ссылка умрёт без правки; отказ отправки,
привязавший снимок, — повтор привяжет его к пустому месту; сбой базы в
посреднике, уронивший отправку, — человек не получает уведомление вовсе;
записи живых меню у основы после переезда — она правит экран, за который
отвечает резерв, и возвращает снятые там кнопки; подпись к фото или файлу
мимо посредника — ссылка в ней умирает с отметкой, «Скрыть» на ней живёт
дольше суток, а запись с подписью, правленная как текст, — отказ Telegram
«нет текста», и правка не идёт.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from awgbot.bot import keyboards as kb
from awgbot.bot import links, screens
from awgbot.bot.lists import msgs
from tests.integration._listmsg import CHAT, OTHER, T0, EditMessageText, Rig

pytestmark = pytest.mark.integration

MENU = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 Пользователи", callback_data="m:users")],
                                             [InlineKeyboardButton(text="⬅️ В меню", callback_data="m:main")]])
EXTEND = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]]))


@pytest.fixture()
def rig(services, monkeypatch):
    return Rig(services, monkeypatch)


def _kb(markup) -> str:
    return markup.model_dump_json(exclude_none=True)


# ── отправка ─────────────────────────────────────────────────────────────────

async def test_a_sent_link_is_recorded_with_the_text_after_the_new_mark_and_its_keyboard(rig):
    key = rig.snap()
    text = links.mark_new("⚠️ Не доставлено: Боря " + rig.link(key))
    m = await rig.bot.send_message(CHAT, text, reply_markup=kb.hide_only())
    rec = rig.rec(m.message_id)
    assert rec is not None, "сообщение со ссылкой на снимок не записано"
    assert rec["html"] == text and f"list-snap-{key}-n" in rec["html"], "в записи не тот текст, что ушёл (без «-n»)"
    assert rec["keys"] == [key] and rec["kb"] == _kb(kb.hide_only()), rec
    assert rig.raw(key)["msg"] == m.message_id, "снимок не привязан к показавшему его сообщению"


async def test_a_notification_with_hide_records_sent_from_the_telegram_answer(rig, monkeypatch):
    """`sent` — `date` ответа Telegram, а не часы хоста: по нему считается возраст «Скрыть»."""
    from awgbot.bot import notifier
    from awgbot.domain.services.types import Notification
    from awgbot.util import timeutil
    rig.session.date = int((T0 - _dt_hours(3)).timestamp())          # Telegram отправил три часа назад
    await notifier.send_notifications(rig.bot, [Notification(CHAT, "✅ Готово")])
    recs = rig.records()
    assert len(recs) == 1, f"уведомление со «Скрыть» не записано: {rig.heads()}"
    sent = timeutil.parse_iso(recs[0]["sent"])
    assert sent == T0 - _dt_hours(3), f"sent не из ответа Telegram: {recs[0]['sent']}"
    assert recs[0]["keys"] == [] and recs[0]["html"] == "", "у записи без ссылок хранится текст"


def _dt_hours(h: float):
    import datetime as _dt
    return _dt.timedelta(hours=h)


async def test_a_message_with_no_link_no_hide_and_no_note_leaves_no_record(rig):
    await rig.bot.send_message(CHAT, "📋 <b>Свои сайты</b>", reply_markup=MENU)
    await rig.bot.send_message(CHAT, "просто текст")
    assert rig.records() == [], "запись без причины — база копит строку на каждое сообщение бота"


async def test_a_note_glued_by_with_note_is_recorded_as_the_note(rig):
    note = "✅ Сайт добавлен"
    text = screens.with_note("📋 <b>Свои сайты</b>\nОткрываются с российского адреса", note)
    m = await rig.bot.send_message(CHAT, text, reply_markup=MENU)
    rec = rig.rec(m.message_id)
    assert rec is not None and rec["note"] == note, f"итог живого меню не записан: {rec}"
    assert msgs.NOTE.get() == "", "итог не снят после записи — достанется следующему сообщению"


async def test_a_foreign_note_is_not_taken(rig):
    """Итог задачи есть, но текст отправки начинается не с него — чужой итог не берётся."""
    screens.with_note("экран", "✅ Сайт добавлен")
    m = await rig.bot.send_message(CHAT, "🛡 <b>SSH-доступ</b>\n\n✅ Сайт добавлен", reply_markup=MENU)
    assert rig.rec(m.message_id) is None, "чужой итог записан — меню погаснет в итог, которого на нём не было"


# ── правка и удаление ────────────────────────────────────────────────────────

async def test_an_in_place_edit_without_link_and_note_drops_the_record_and_frees_the_snapshot(rig):
    key = rig.snap()
    m = await rig.bot.send_message(CHAT, screens.with_note("📋 Свои сайты", "⚠️ Не добавлено: " + rig.link(key)),
                                   reply_markup=MENU)
    assert rig.rec(m.message_id) is not None
    await rig.bot.edit_message_text("📋 <b>Свои сайты</b> · 3", chat_id=CHAT, message_id=m.message_id,
                                    reply_markup=MENU)
    assert rig.rec(m.message_id) is None, "запись пережила переход: такт поправит уже другой экран"
    assert rig.raw(key)["msg"] == 0, "снимок не отпущен — заново его не показать"
    got = rig.db.list_snap_get(key, CHAT)
    assert got is not None and not got.get("stale"), "снимок после перехода не открывается"


async def test_an_in_place_edit_keeps_the_note_only_when_the_new_text_carries_it(rig):
    m = await rig.bot.send_message(CHAT, screens.with_note("экран 1", "✅ Первый итог"), reply_markup=MENU)
    await rig.bot.edit_message_text(screens.with_note("экран 2", "✅ Второй итог"), chat_id=CHAT,
                                    message_id=m.message_id, reply_markup=MENU)
    assert rig.rec(m.message_id)["note"] == "✅ Второй итог", "итог правки на месте не заменил прежний"


async def test_a_keyboard_edit_updates_kb_and_hide_gone_without_other_reasons_drops_the_record(rig):
    key = rig.snap()
    a = await rig.bot.send_message(CHAT, "Итог " + rig.link(key, new=True), reply_markup=EXTEND)
    b = await rig.bot.send_message(CHAT, "⏱ Подписка истекает", reply_markup=EXTEND)
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=a.message_id, reply_markup=kb.hide_only())
    assert rig.rec(a.message_id)["kb"] == _kb(kb.hide_only()), "правка клавиатуры не дошла до записи"
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=a.message_id, reply_markup=None)
    rec = rig.rec(a.message_id)
    assert rec is not None and rec["kb"] == "" and rec["keys"] == [key], f"ссылка держит запись, кнопок нет: {rec}"
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=b.message_id, reply_markup=None)
    assert rig.rec(b.message_id) is None, "«Скрыть» снята, других причин нет — запись осталась"


async def test_hide_put_on_by_a_keyboard_edit_starts_a_record_with_sent_from_the_answer(rig):
    """Итог обновления после «⬅️ В меню»: кнопки заменены одной «Скрыть» — записи не было,
    теперь есть, и возраст её считается от отправки итога, а не от нажатия."""
    from awgbot.util import timeutil
    rig.session.date = int((T0 - _dt_hours(5)).timestamp())
    m = await rig.bot.send_message(CHAT, "✅ Обновлено до v1.2.3", reply_markup=MENU)
    assert rig.rec(m.message_id) is None
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=m.message_id, reply_markup=kb.hide_only())
    rec = rig.rec(m.message_id)
    assert rec is not None and rec["kb"] == _kb(kb.hide_only()), "«Скрыть» на итоге не записана — такт её не снимет"
    assert timeutil.parse_iso(rec["sent"]) == T0 - _dt_hours(5), f"sent не время отправки итога: {rec['sent']}"


async def test_delete_message_drops_the_record_and_its_snapshots_without_a_mark(rig):
    k1, k2 = rig.snap(), rig.snap()
    m = await rig.bot.send_message(CHAT, f"Итог {rig.link(k1)} и {rig.link(k2)}", reply_markup=kb.hide_only())
    await rig.bot.delete_message(CHAT, m.message_id)
    assert rig.rec(m.message_id) is None, "запись удалённого сообщения осталась"
    assert rig.raw(k1) is None and rig.raw(k2) is None, "снимки удалённого сообщения остались (или отметкой)"
    assert rig.db.list_snap_get(k1, CHAT) is None


async def test_delete_messages_in_a_batch_keeps_the_record(rig):
    """Пакетом Telegram пропускает неудаляемые номера молча — успех пакета не значит, что удалено это."""
    key = rig.snap()
    m = await rig.bot.send_message(CHAT, "Итог " + rig.link(key), reply_markup=kb.hide_only())
    await rig.bot.delete_messages(CHAT, [m.message_id])
    assert rig.rec(m.message_id) is not None, "запись снята пакетом — ссылка неудалённого умрёт без правки"
    assert rig.raw(key)["msg"] == m.message_id


async def test_a_link_to_a_snapshot_of_another_chat_binds_nothing(rig):
    key = rig.snap(OTHER)
    m = await rig.bot.send_message(CHAT, "Объявление " + rig.link(key), reply_markup=kb.hide_only())
    assert rig.raw(key)["msg"] == 0, "чужой снимок привязан — такт поправит чужое сообщение"
    assert rig.rec(m.message_id)["keys"] == [], rig.rec(m.message_id)


# ── отказы отправки и сбой базы ──────────────────────────────────────────────

async def test_a_refused_send_binds_nothing(rig):
    from aiogram.methods import SendMessage
    key = rig.snap()
    rig.session.refuse("forbidden", method=SendMessage)
    with pytest.raises(TelegramForbiddenError):
        await rig.bot.send_message(CHAT, "Итог " + rig.link(key), reply_markup=kb.hide_only())
    assert rig.raw(key)["msg"] == 0, "снимок привязан к неотправленному сообщению"
    assert rig.records() == [], "запись неотправленного сообщения"


async def test_the_notifier_retry_after_429_binds_the_snapshot_to_the_delivered_message(rig, monkeypatch):
    from aiogram.methods import SendMessage
    from awgbot.bot import notifier
    from tests.integration._listmsg import AsyncioProxy
    monkeypatch.setattr(notifier, "asyncio", AsyncioProxy(rig.vt))
    key = rig.snap()
    rig.session.refuse("flood", method=SendMessage, retry_after=3)
    sent = await notifier._send(rig.bot, CHAT, "⚠️ Не перенесены " + rig.link(key), kb.hide_only(), True)
    assert sent is not None, "повтор после 429 не отправил"
    assert rig.raw(key)["msg"] == sent.message_id, "снимок не привязан к удачной отправке"
    assert [r["mid"] for r in rig.records()] == [sent.message_id]


async def test_a_database_failure_in_the_watch_does_not_break_the_send(rig, monkeypatch, caplog):
    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(rig.db, "list_msg_put", locked)
    key = rig.snap()
    with caplog.at_level(logging.WARNING):
        m = await rig.bot.send_message(CHAT, "Итог " + rig.link(key), reply_markup=kb.hide_only())
    assert m.message_id and rig.heads()[-1] == f"+ send #{m.message_id}", "отправка сорвалась из-за учёта"
    assert "lists: запись сообщения не обновлена" in caplog.text, caplog.text


# ── передача Telegram резерву ────────────────────────────────────────────────

async def _four(rig):
    """Живое меню с итогом, уведомление с «⏱ Продлить» и «Скрыть», след с одной «Скрыть»,
    уведомление с одной «Скрыть» и ссылкой."""
    key = rig.snap()
    menu = await rig.bot.send_message(CHAT, screens.with_note("экран", "✅ Итог"), reply_markup=MENU)
    ext = await rig.bot.send_message(CHAT, "⏱ Подписка истекает", reply_markup=EXTEND)
    trace = await rig.bot.send_message(CHAT, "🚚 Переезд начат", reply_markup=kb.hide_only())
    note = await rig.bot.send_message(OTHER, "⚠️ Не перенесены", reply_markup=kb.hide_only())
    linked = await rig.bot.send_message(CHAT, "Итог " + rig.link(key, new=True), reply_markup=kb.hide_only())
    return menu, ext, trace, note, linked, key


async def test_away_drops_records_with_action_buttons_and_keeps_lone_hide_ones(rig, services):
    menu, ext, trace, note, linked, key = await _four(rig)
    services.reserve_set_poll("away")
    assert rig.rec(menu.message_id) is None, "запись живого меню осталась — основа правит экран резерва"
    assert rig.rec(ext.message_id) is None, "запись уведомления с «⏱ Продлить» осталась"
    assert rig.rec(trace.message_id) is not None, "след с одной «Скрыть» снят — кнопка переживёт 48 часов"
    assert rig.rec(note.message_id, OTHER) is not None
    assert rig.rec(linked.message_id) is not None and rig.raw(key)["msg"] == linked.message_id


async def test_mine_drops_nothing_and_a_repeated_away_does_not_drop_again(rig, services):
    menu, ext, trace, note, linked, key = await _four(rig)
    services.reserve_set_poll("mine")
    assert len(rig.records()) == 5, "«mine» сняло записи"
    services.reserve_set_poll("away")
    later = await rig.bot.send_message(CHAT, screens.with_note("экран", "✅ Ещё итог"), reply_markup=MENU)
    services.reserve_set_poll("away")
    assert rig.rec(later.message_id) is not None, "повторное «away» без перехода снова сняло записи"


async def test_away_survives_a_failing_record_store(rig, services, monkeypatch, caplog):
    """Передача права важнее учёта: сбой базы записей — строка журнала, право ушло."""
    def broken():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(services.db, "list_msg_drop_live", broken)
    with caplog.at_level(logging.WARNING):
        services.reserve_set_poll("away")
    assert services.rsv_get("rsv_poll").get("state") == "away"
    assert "записи сообщений с кнопками действия не сняты" in caplog.text, caplog.text




# ── «Скрыть», поставленная правкой на старое сообщение ──────────────────────

DAYS3 = 72 * 3600


@pytest.mark.parametrize("how", ["text", "markup"])
async def test_lone_hide_put_on_a_three_day_old_message_by_an_edit_is_taken_off_at_once(rig, how):
    """След переезда на меню трёхдневной давности, «⬅️ В меню» на итоге спустя выходные:
    сообщение старше 48 часов, «Скрыть» на нём не удалит. Кнопка снимается той же
    задачей, до ответа обработчику, — иначе человек нажимает и видит «не удалить»,
    а такт снимет её только ночью."""
    rig.session.date = int((T0 - _dt_hours(72)).timestamp())        # Telegram отправил три дня назад
    m = await rig.bot.send_message(CHAT, "📋 <b>Свои сайты</b>", reply_markup=MENU)
    mid = m.message_id
    if how == "text":
        await rig.bot.edit_message_text("🚚 Переезд начат", chat_id=CHAT, message_id=mid,
                                        reply_markup=kb.hide_only())
    else:
        await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=mid, reply_markup=kb.hide_only())
    heads = [c.head for c in rig.edit_calls(mid)]
    assert heads[-1] == f"- markup off #{mid}", \
        f"«Скрыть» на сообщении трёхдневной давности осталась (правка {how}): {heads}"
    assert len(heads) == 2, f"не одна правка следом, а {heads}"
    assert rig.rec(mid) is None, f"запись без причин осталась после снятия «Скрыть»: {rig.rec(mid)}"


@pytest.mark.parametrize("hours, stays", [(30, True), (37, True), (38, False)])
async def test_hide_put_on_an_old_message_by_an_edit_stays_while_the_tick_can_take_it_off_in_time(rig, hours, stays):
    """T0 — 14:05, такт — завтра 00:06 (через 10 ч 01 мин). Сообщению 30 или 37 часов:
    к такту 40 или 47 ч 01 мин — «Скрыть» стоит до такта (кнопка живёт как можно
    дольше, сообщение не придётся удалять руками); 38 часов — к такту 48 ч 01 мин,
    после 48 часов она не удалит — снять сразу."""
    rig.session.date = int((T0 - _dt_hours(hours)).timestamp())
    m = await rig.bot.send_message(CHAT, "🚚 Переезд", reply_markup=MENU)
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=m.message_id, reply_markup=kb.hide_only())
    heads = [c.head for c in rig.edit_calls(m.message_id)]
    if stays:
        assert heads == [f"= markup #{m.message_id}"], f"«Скрыть» снята раньше такта ({hours} ч): {heads}"
        assert rig.rec(m.message_id) is not None, "запись «Скрыть» не легла"
    else:
        assert heads[-1] == f"- markup off #{m.message_id}" and rig.rec(m.message_id) is None, heads


async def test_extend_and_hide_put_on_an_old_message_keeps_only_extend(rig):
    """Снимается только ряд «Скрыть»: «⏱ Продлить» — действие, его не трогать."""
    rig.session.date = int((T0 - _dt_hours(72)).timestamp())
    m = await rig.bot.send_message(CHAT, "⏱ Подписка истекает", reply_markup=MENU)
    await rig.bot.edit_message_reply_markup(chat_id=CHAT, message_id=m.message_id, reply_markup=EXTEND)
    got = rig.edit_calls(m.message_id)
    assert len(got) == 2, f"не одна правка следом: {[c.head for c in got]}"
    last = got[-1]
    assert last.head == f"= markup #{m.message_id}" and _rows(last.markup) == [[("⏱ Продлить", "ext:take:5")]], \
        f"снято не то: {last.head} {_rows(last.markup)}"
    assert rig.rec(m.message_id) is None, "«Скрыть» снята, ссылок и итога нет — запись осталась"


async def test_hide_put_on_a_fresh_message_by_an_edit_stays(rig):
    """Сообщение минутной давности: «Скрыть» живёт сутки, правка следом не идёт."""
    rig.session.date = int((T0 - _dt_hours(1 / 60)).timestamp())
    m = await rig.bot.send_message(CHAT, "📋 <b>Свои сайты</b>", reply_markup=MENU)
    await rig.bot.edit_message_text("🚚 Переезд начат", chat_id=CHAT, message_id=m.message_id,
                                    reply_markup=kb.hide_only())
    assert [c.head for c in rig.edit_calls(m.message_id)] == [f"= edit #{m.message_id}"], \
        "«Скрыть» снята со свежего сообщения — его нельзя убрать из чата"
    rec = rig.rec(m.message_id)
    assert rec is not None and rec["kb"] == _kb(kb.hide_only()), f"запись «Скрыть» не легла: {rec}"


async def test_a_send_with_an_old_telegram_date_is_not_checked_for_hide_age(rig):
    """Отправка всегда свежая: дата ответа в прошлом бывает только у заглушки, и проверка
    возраста на отправке сняла бы «Скрыть» у сценариев такта, которые так и строятся."""
    rig.session.date = int((T0 - _dt_hours(72)).timestamp())
    m = await rig.bot.send_message(CHAT, "✅ Готово", reply_markup=kb.hide_only())
    assert rig.edit_calls(m.message_id) == [], f"отправку проверили на возраст: {rig.heads()}"
    assert rig.rec(m.message_id) is not None, "запись уведомления со «Скрыть» не легла"


def _rows(markup):
    if markup is None:
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


# ── итог записи — когда меню ушло мимо гашения ──────────────────────────────

async def _menu(rig, note: str, screen: str = "📋 <b>Свои сайты</b>") -> int:
    """Живое меню с итогом первой строкой — номер; указатель меню — на нём."""
    m = await rig.bot.send_message(CHAT, screens.with_note(screen, note), reply_markup=MENU)
    rig.db.nav_touch(CHAT, m.message_id)
    return m.message_id


async def test_start_purge_drops_notes_records_with_only_a_note_vanish_others_stay_without_it(rig, services):
    """/start удаляет прошлые меню пачкой — посредник пачку не учитывает. Итог записи
    больше не нужен: иначе запись живёт вечно, а сообщение, которое Telegram не дал
    удалить, при гашении снова стало бы следом из итога."""
    from awgbot.bot.handlers import common
    key = rig.snap()
    plain = await _menu(rig, "✅ Сайт добавлен")
    linked = await _menu(rig, "⚠️ Не добавлено: " + rig.link(key), screen="🗺 <b>Подсети</b>")
    assert rig.rec(plain)["note"] and rig.rec(linked)["note"], "сценарий не о том: итогов в записях нет"
    await common.purge_menus(rig.bot, services, CHAT)
    assert rig.rec(plain) is None, f"запись с одним итогом пережила /start: {rig.rec(plain)}"
    rec = rig.rec(linked)
    assert rec is not None and rec["keys"] == [key], f"запись со ссылкой снята — ссылка не поправится: {rec}"
    assert rec["note"] == "", f"итог остался у записи ушедшего меню: {rec['note']!r}"


async def test_forget_nav_if_drops_the_note_of_the_deleted_live_menu_only(rig, services):
    """Живое меню удалено вместе с выдачей: указатель и итог долой; удаление другого
    сообщения итог живого меню не трогает."""
    from awgbot.bot.handlers import common
    mid = await _menu(rig, "✅ Устройство «Ноутбук» удалено")
    await common.forget_nav_if(services, CHAT, [mid + 100])
    assert rig.rec(mid) is not None and rig.rec(mid)["note"], "итог снят, хотя удалено не меню"
    await common.forget_nav_if(services, CHAT, [mid])
    assert rig.rec(mid) is None, f"итог удалённого живого меню остался в записи: {rig.rec(mid)}"
    assert services.db.get_nav_message_id(CHAT) is None


@pytest.mark.parametrize("kind", ["cant_edit", "not_found"])
async def test_a_failed_edit_to_the_note_drops_the_note_and_dims_the_menu(rig, services, kind):
    """Правка до итога не удалась: меню гаснет как прежде, а итог записи снят — меню
    следом уже не станет, запись не должна держаться вечно."""
    from awgbot.bot.handlers import common
    old = await _menu(rig, "✅ Сайт добавлен")
    rig.session.refuse(kind, method=EditMessageText, mid=old)
    await common.send_menu_to(rig.bot, services, CHAT, "🏠 Главное меню", MENU)
    heads = rig.heads()
    if kind == "cant_edit":
        assert f"- markup off #{old}" in heads, f"кнопки не сняты — два живых меню: {heads}"
    assert rig.rec(old) is None, f"{kind}: итог остался в записи меню, которое не станет следом: {rig.rec(old)}"


async def test_retire_does_not_take_the_note_of_the_new_menu(rig, services):
    """Прежний итог «A\\n\\nB», новый — «A»: текст следа начинается с итога нового меню.
    Если след заберёт его себе, новое меню останется без итога и при следующем
    переходе погаснет целиком — итог пропадёт из чата."""
    from awgbot.bot.handlers import common
    old = await _menu(rig, "✅ A\n\n⚠️ B")
    new_text = screens.with_note("🗺 <b>Подсети</b>", "✅ A")
    new = await common.send_menu_to(rig.bot, services, CHAT, new_text, MENU)
    assert rig.text_of(old) == "✅ A\n\n⚠️ B", f"прежнее меню не стало следом: {rig.text_of(old)!r}"
    trace = rig.rec(old)
    assert trace is not None and trace["note"] == "", f"след забрал итог нового меню: {trace}"
    rec = rig.rec(new)
    assert rec is not None and rec["note"] == "✅ A", f"у нового меню итога нет: {rec}"


# ── отказ правки экрана обработчиком ─────────────────────────────────────────

async def test_a_refused_screen_edit_releases_the_record_and_the_resent_screen_binds_the_snapshot(rig):
    """Сообщения уже нет («message to edit not found»): обработчик шлёт экран заново
    новым сообщением — снимок должен привязаться к нему, а не остаться на пустом
    месте (ссылка в новом сообщении умирала бы с отметкой)."""
    from aiogram.exceptions import TelegramBadRequest
    key = rig.snap()
    text = "⚠️ Не добавлено " + rig.link(key)
    m = await rig.bot.send_message(CHAT, text, reply_markup=MENU)
    rig.session.refuse("not_found", method=EditMessageText, mid=m.message_id)
    with pytest.raises(TelegramBadRequest):
        await rig.bot.edit_message_text(text + " ещё", chat_id=CHAT, message_id=m.message_id, reply_markup=MENU)
    assert rig.rec(m.message_id) is None, f"запись пропавшего сообщения осталась: {rig.rec(m.message_id)}"
    assert rig.raw(key)["msg"] == 0 and not rig.raw(key).get("gone"), f"снимок не отпущен или снят: {rig.raw(key)}"
    n = await rig.bot.send_message(CHAT, text, reply_markup=MENU)
    assert rig.rec(n.message_id)["keys"] == [key] and rig.raw(key)["msg"] == n.message_id, \
        "экран, присланный заново, не привязал снимок"


async def test_a_refused_edit_of_the_live_menu_makes_the_resent_screen_the_live_menu(rig):
    """Указатель живого меню стоял на пропавшем сообщении: следующее меню не гасило
    бы присланное заново, а ссылка на экран правила бы пустоту."""
    from aiogram.exceptions import TelegramBadRequest
    m = await rig.bot.send_message(CHAT, "экран", reply_markup=MENU)
    rig.db.nav_touch(CHAT, m.message_id)
    rig.session.refuse("not_found", method=EditMessageText, mid=m.message_id)
    with pytest.raises(TelegramBadRequest):
        await rig.bot.edit_message_text("экран ещё", chat_id=CHAT, message_id=m.message_id, reply_markup=MENU)
    assert rig.db.get_nav_message_id(CHAT) == m.message_id, "указатель сдвинут до отправки"
    n = await rig.bot.send_message(CHAT, "экран ещё", reply_markup=MENU)
    assert rig.db.get_nav_message_id(CHAT) == n.message_id, "присланный заново экран не стал живым меню"
    await rig.bot.send_message(CHAT, "уведомление", reply_markup=kb.hide_only())
    assert rig.db.get_nav_message_id(CHAT) == n.message_id, "отказ не снят: следующая отправка сдвинула указатель"


async def test_a_refused_edit_of_another_message_leaves_the_live_menu_alone(rig):
    """Отказ правки не меню (уведомление) — присланное следом не живое меню."""
    from aiogram.exceptions import TelegramBadRequest
    menu = await rig.bot.send_message(CHAT, "экран", reply_markup=MENU)
    rig.db.nav_touch(CHAT, menu.message_id)
    key = rig.snap()
    m = await rig.bot.send_message(CHAT, "⚠️ Не добавлено " + rig.link(key), reply_markup=kb.hide_only())
    rig.session.refuse("not_found", method=EditMessageText, mid=m.message_id)
    with pytest.raises(TelegramBadRequest):
        await rig.bot.edit_message_text("⚠️ Не добавлено ещё", chat_id=CHAT, message_id=m.message_id)
    await rig.bot.send_message(CHAT, "⚠️ Не добавлено ещё", reply_markup=kb.hide_only())
    assert rig.db.get_nav_message_id(CHAT) == menu.message_id, "указатель живого меню сдвинут отказом правки уведомления"


async def test_a_not_modified_refusal_keeps_the_record(rig):
    from aiogram.exceptions import TelegramBadRequest
    key = rig.snap()
    text = "⚠️ Не добавлено " + rig.link(key)
    m = await rig.bot.send_message(CHAT, text, reply_markup=MENU)
    rig.session.refuse("not_modified", method=EditMessageText, mid=m.message_id)
    with pytest.raises(TelegramBadRequest):
        await rig.bot.edit_message_text(text, chat_id=CHAT, message_id=m.message_id, reply_markup=MENU)
    assert rig.rec(m.message_id) is not None and rig.raw(key)["msg"] == m.message_id


# ── подписи к фото и файлам ──────────────────────────────────────────────────

PHOTO = "DUMMYPHOTO"


async def test_a_photo_caption_with_a_link_and_hide_is_recorded_as_a_caption(rig):
    """Объявление картинкой со ссылкой на список: запись как у текста, с признаком
    подписи — иначе правка пошла бы editMessageText и получила бы «нет текста»."""
    key = rig.snap()
    caption = links.mark_new("📣 Не доставлено: Боря " + rig.link(key))
    m = await rig.bot.send_photo(CHAT, PHOTO, caption=caption, reply_markup=kb.hide_only())
    rec = rig.rec(m.message_id)
    assert rec is not None, "фото со ссылкой на снимок не записано"
    assert rec.get("cap") is True and rec["html"] == caption and rec["keys"] == [key], rec
    assert rig.raw(key)["msg"] == m.message_id, "снимок не привязан к фото"


async def test_a_document_caption_with_hide_only_is_recorded_and_the_caption_edit_updates_it(rig):
    key = rig.snap()
    m = await rig.bot.send_document(CHAT, "DUMMYDOC", caption="💾 Бэкап", reply_markup=kb.hide_only())
    rec = rig.rec(m.message_id)
    assert rec is not None and rec.get("cap") is True and rec["keys"] == [], rec
    await rig.bot.edit_message_caption(chat_id=CHAT, message_id=m.message_id,
                                       caption="💾 Бэкап " + rig.link(key), reply_markup=kb.hide_only())
    rec = rig.rec(m.message_id)
    assert rec["keys"] == [key] and rec.get("cap") is True, f"правка подписи не легла в запись: {rec}"
    await rig.bot.edit_message_caption(chat_id=CHAT, message_id=m.message_id, caption="💾 Бэкап")
    assert rig.rec(m.message_id) is None, "ссылка и «Скрыть» ушли из подписи — запись осталась"
    assert rig.raw(key)["msg"] == 0, "снимок не отпущен после правки подписи"


async def test_a_media_group_leaves_no_record(rig):
    """У альбома нет клавиатуры, а подпись стоит у первого из нескольких ответных
    сообщений: посредник его не учитывает — снимок остаётся не привязанным."""
    key = rig.snap()
    media = [InputMediaPhoto(media=PHOTO, caption="📣 " + rig.link(key)), InputMediaPhoto(media=PHOTO)]
    await rig.bot.send_media_group(CHAT, media=media)
    assert rig.records() == [], f"альбом записан: {rig.records()}"
    assert rig.raw(key)["msg"] == 0


async def test_hide_put_on_an_old_photo_by_a_caption_edit_is_taken_off_at_once(rig):
    rig.session.date = int((T0 - _dt_hours(72)).timestamp())
    m = await rig.bot.send_photo(CHAT, PHOTO, caption="📣 Объявление", reply_markup=MENU)
    await rig.bot.edit_message_caption(chat_id=CHAT, message_id=m.message_id, caption="📣 Объявление",
                                       reply_markup=kb.hide_only())
    heads = [c.head for c in rig.edit_calls(m.message_id)]
    assert heads == [f"= caption #{m.message_id}", f"- markup off #{m.message_id}"], \
        f"«Скрыть» на фото трёхдневной давности осталась: {heads}"
    assert rig.rec(m.message_id) is None


async def test_a_photo_menu_with_a_note_retires_by_a_caption_edit(rig, services):
    """Меню картинкой (гайд) с итогом первой строкой: след — правкой подписи, не текста."""
    from awgbot.bot.handlers import common
    m = await rig.bot.send_photo(CHAT, PHOTO, caption=screens.with_note("🧭 <b>Гайд</b>", "✅ Шаг пройден"),
                                 reply_markup=MENU)
    old = m.message_id
    rig.db.nav_touch(CHAT, old)
    await common.send_menu_to(rig.bot, services, CHAT, "🏠 Главное меню", MENU)
    heads = rig.heads()
    assert f"= caption #{old}" in heads and f"= edit #{old}" not in heads, f"след не правкой подписи: {heads}"
    assert rig.caption_of(old) == "✅ Шаг пройден", rig.caption_of(old)
    trace = rig.rec(old)
    assert trace is not None and trace["note"] == "" and trace.get("cap") is True \
        and trace["kb"] == _kb(kb.hide_only()), trace
