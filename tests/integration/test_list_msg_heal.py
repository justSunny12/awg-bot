"""Правка ссылок умерших снимков (lists/msgs/heal) в действии: 51-й снимок чата
вытесняет самый ранний — сообщение со ссылкой на него правится тем же
действием, до возврата из отправки; отказы Telegram при правке; 429 — без
ожидания в обработчике, фоновым повтором; две правки чата разом — одна.

Цена ошибки: правка после ответа обработчику — человек нажимает только что
показанную ссылку и получает «больше не хранится»; снятые обе ссылки отчёта
рассылки вместо одной — список недоставленных больше не открыть; правка без
клавиатуры — у уведомления пропадают «⏱ Продлить» и «Скрыть»; «not found»,
оставивший запись, — такт каждую ночь правит удалённое сообщение; 403,
снявший отметку, — ссылка в сообщении, которое больше не правится, отвечает
«списка нет», будто подделана; сеть, снявшая запись, — ссылка так и умрёт
ссылкой; 429, которого ждёт обработчик, — человек минутами смотрит на часики;
две правки одного сообщения — лишний запрос под флуд-контроль.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.methods import EditMessageText

from awgbot.bot import keyboards as kb
from awgbot.bot.lists.msgs import heal
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from tests.integration._listmsg import CHAT, Rig, rows

pytestmark = pytest.mark.integration

EXTEND = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]]))


@pytest.fixture()
def rig(services, monkeypatch):
    return Rig(services, monkeypatch)


async def _shown(rig, *labels_keys, markup=EXTEND) -> int:
    """Сообщение со ссылками на снимки (подпись, ключ) — номер."""
    body = "⚠️ Не доставлено: Боря" + "".join(f" {rig.link(k, label, new=True)}" for label, k in labels_keys)
    return (await rig.bot.send_message(CHAT, body, reply_markup=markup)).message_id


async def _evict_first(rig) -> int:
    """Дополнить чат до 50 живых снимков и показать 51-й новым сообщением: самый ранний
    вытеснен. Возвращает номер нового сообщения."""
    alive = sum(1 for _k, d in rig.db._snaps() if not d.get("gone") and int(d.get("chat") or 0) == CHAT)
    rig.fill(50 - alive)
    k51 = rig.snap()
    return (await rig.bot.send_message(CHAT, "Новый итог " + rig.link(k51), reply_markup=kb.hide_only())).message_id


# ── вытеснение: правка тем же действием ──────────────────────────────────────

async def test_the_51st_snapshot_edits_the_old_message_before_the_send_returns(rig):
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    b = await _evict_first(rig)
    heads = rig.heads()
    assert f"= edit #{a}" in heads, f"старое сообщение не поправлено тем же действием: {heads[-4:]}"
    assert heads.index(f"+ send #{b}") < heads.index(f"= edit #{a}"), "правка раньше отправки нового"
    text = rig.text_of(a)
    assert "list-snap-" not in text and text.endswith("Боря и ещё 27"), f"ссылка не стала текстом: {text}"
    edit = rig.edit_calls(a)[-1]
    assert rows(edit.markup) == rows(EXTEND), "правка ушла не с той клавиатурой — кнопки уведомления пропали"
    assert rig.raw(k1).get("gone") == 1, "отметка вытесненного снята — открытый экран снимка соврёт"


async def test_of_two_links_only_the_dead_one_is_replaced(rig):
    k1, k2 = rig.snap(), rig.snap()
    a = await _shown(rig, ("и ещё 20", k1), ("и ещё 27", k2))
    await _evict_first(rig)
    text = rig.text_of(a)
    assert f"list-snap-{k1}" not in text and f"list-snap-{k2}-n" in text, f"снята не та ссылка: {text}"
    rec = rig.rec(a)
    assert rec["keys"] == [k2] and rec["html"] == text, f"запись не по новому тексту: {rec}"
    assert rig.raw(k2)["msg"] == a, "живой снимок отвязан от сообщения"


async def test_nothing_dead_means_no_edit(rig):
    k1 = rig.snap()
    await _shown(rig, ("и ещё 27", k1))
    rig.fill(10)
    k = rig.snap()
    await rig.bot.send_message(CHAT, "Новый итог " + rig.link(k), reply_markup=kb.hide_only())
    assert rig.edit_calls() == [], f"правка без мёртвых ссылок: {rig.heads()}"


# ── отказы Telegram ──────────────────────────────────────────────────────────

async def test_not_found_drops_the_record_and_its_snapshots_without_a_mark(rig):
    k1, k2 = rig.snap(), rig.snap()
    a = await _shown(rig, ("и ещё 20", k1), ("и ещё 27", k2))
    rig.session.refuse("not_found", mid=a)
    await _evict_first(rig)
    assert rig.rec(a) is None, "запись удалённого человеком сообщения осталась — такт будет править его каждую ночь"
    assert rig.raw(k1) is None and rig.raw(k2) is None, "снимки удалённого сообщения остались (или отметкой)"


@pytest.mark.parametrize("kind", ["forbidden", "cant_edit", "chat_not_found"])
async def test_a_final_refusal_drops_the_record_and_keeps_the_mark(rig, kind):
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    rig.session.refuse(kind, mid=a)
    await _evict_first(rig)
    assert rig.rec(a) is None, f"{kind}: запись осталась — правка будет повторяться без толку"
    assert rig.raw(k1) is not None and rig.raw(k1).get("gone") == 1, \
        f"{kind}: отметка снята — ссылка, которую не поправить, ответит «списка нет»"


@pytest.mark.parametrize("kind", ["network", "server", "unauthorized"])
async def test_an_unreachable_telegram_keeps_the_record_and_the_next_tick_edits(rig, kind):
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    rig.session.refuse(kind, mid=a)
    await _evict_first(rig)
    rec = rig.rec(a)
    assert rec is not None and rec["keys"] == [k1], f"{kind}: запись снята — ссылка так и умрёт ссылкой"
    assert "list-snap-" in rig.text_of(a)
    await heal.nightly(rig.bot, rig.services)
    assert "list-snap-" not in rig.text_of(a), f"{kind}: следующий такт не поправил"
    assert rig.rec(a)["keys"] == []


async def test_not_modified_counts_as_done_and_updates_the_record(rig):
    """Правку уже сделала прошлая попытка: ответ «not modified» посредник не видит — запись обновляет правка."""
    k1, k2 = rig.snap(), rig.snap()
    a = await _shown(rig, ("и ещё 20", k1), ("и ещё 27", k2))
    rig.session.refuse("not_modified", mid=a)
    await _evict_first(rig)
    rec = rig.rec(a)
    assert rec is not None and rec["keys"] == [k2], f"запись не обновлена после «not modified»: {rec}"
    assert f"list-snap-{k1}" not in rec["html"] and f"list-snap-{k2}-n" in rec["html"]
    assert rig.raw(k1).get("gone") == 1


# ── 429 в действии ───────────────────────────────────────────────────────────

def _edits_of(rig, mid: int) -> list:
    return [e for e in rig.session.edits if e[3] == mid]


async def test_429_in_an_action_does_not_wait_and_a_background_task_retries(rig):
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    rig.session.refuse("flood", mid=a, retry_after=7)
    await _evict_first(rig)
    assert len(_edits_of(rig, a)) == 1, "обработчик ждал 429 и повторил сам — человек смотрит на часики"
    assert "list-snap-" in rig.text_of(a)
    done = await rig.settle(lambda: "list-snap-" not in (rig.text_of(a) or ""))
    assert done, f"фоновый повтор не поправил сообщение: {rig.heads()[-3:]}"
    assert 7 in rig.vt.slept, f"повтор не через retry_after: {rig.vt.slept}"
    await rig.settle(lambda: rig.rec(a) is not None and rig.rec(a)["keys"] == [])
    assert rig.rec(a)["keys"] == []


async def test_the_background_retry_waits_at_most_ten_minutes(rig):
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    rig.session.refuse("flood", mid=a, retry_after=3600)
    await _evict_first(rig)
    assert await rig.settle(lambda: "list-snap-" not in (rig.text_of(a) or "")), "повтора не было"
    assert max(rig.vt.long()) == heal.RETRY_MAX_S == 600, f"фоновый повтор ждал не 10 минут: {rig.vt.slept}"


# ── две правки чата разом ────────────────────────────────────────────────────

async def test_two_heals_of_a_chat_at_once_edit_the_message_once(rig, monkeypatch):
    """Два действия в одном чате одновременно (замок чата): одно сообщение — одна правка.
    Оба прочли записи к правке до того, как любое из них поправило: так и бывает, когда
    два обработчика одного чата отправляют по сообщению в одну и ту же секунду."""
    import threading
    k1 = rig.snap()
    a = await _shown(rig, ("и ещё 27", k1))
    rig.fill(49)
    rig.snap()                                        # 51-й: первый вытеснен, но отправки ещё не было
    assert rig.raw(k1).get("gone") == 1
    both = threading.Barrier(2, timeout=5)
    real_due = rig.db.list_msg_due
    reads = []

    def due(*a, **k):
        out = real_due(*a, **k)
        reads.append(1)
        if len(reads) <= 2:
            both.wait()                               # оба прочли до правки — дальше наперегонки
        return out                                    # перечитывание под замком чата — без ожидания
    monkeypatch.setattr(rig.db, "list_msg_due", due)
    await asyncio.gather(heal.heal_chat(rig.bot, rig.services, CHAT), heal.heal_chat(rig.bot, rig.services, CHAT))
    got = [e for e in _edits_of(rig, a) if e[1] == EditMessageText.__name__]
    assert len(got) == 1, f"сообщение поправлено {len(got)} раза — лишний запрос под флуд-контроль"


# ── итог записи без мёртвых ссылок ───────────────────────────────────────────

MENU = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 Пользователи", callback_data="m:users")]])


async def test_a_link_edit_strips_the_note_too_and_the_trace_brings_no_dead_link_back(rig):
    """Живое меню с итогом «⚠️ Не добавлено … и ещё 7» ссылкой; снимок протух, такт
    сделал ссылку текстом. Итог в записи — тоже без ссылки: иначе следующее меню
    погасит это в след из прежнего итога, и мёртвая ссылка вернётся в чат."""
    import datetime as _dt
    from awgbot.bot import screens
    from awgbot.bot.lists.msgs import retire
    from awgbot.core import config
    key = rig.snap()
    note = "⚠️ Не добавлено: r0.example " + rig.link(key, "и ещё 7")
    mid = (await rig.bot.send_message(CHAT, screens.with_note("📋 <b>Свои сайты</b>", note),
                                      reply_markup=MENU)).message_id
    assert rig.rec(mid)["note"] == note, "сценарий не о том: итог со ссылкой не записан"
    rig.clock.t = _dt.datetime(2026, 11, 27, 0, 2, tzinfo=config.TZ)       # срок снимка прошёл
    await heal.nightly(rig.bot, rig.services)
    assert "list-snap-" not in rig.text_of(mid), "такт не поправил ссылку меню"
    rec = rig.rec(mid)
    assert rec is not None and rec["note"] == "⚠️ Не добавлено: r0.example и ещё 7", \
        f"итог записи не очищен от мёртвой ссылки: {rec and rec['note']!r}"
    await retire.retire(rig.bot, rig.services, CHAT, mid)
    trace = rig.text_of(mid)
    assert trace == "⚠️ Не добавлено: r0.example и ещё 7", f"след вернул мёртвую ссылку: {trace!r}"


# ── обработчик поправил сообщение во время правки ────────────────────────────

async def test_a_handler_edit_during_the_heal_edit_is_not_overwritten(rig, monkeypatch, caplog):
    """Пока такт снимал «Скрыть», обработчик перерисовал то же сообщение в меню с итогом
    (запись новее). Правка такта не должна затереть её своим видом: иначе запись
    теряет клавиатуру меню, и следующее гашение промахнётся."""
    import datetime as _dt
    import logging
    from aiogram.methods import EditMessageReplyMarkup
    rig.session.date = int((rig.clock.t - _dt.timedelta(hours=25)).timestamp())
    mid = (await rig.bot.send_message(CHAT, "✅ Готово", reply_markup=kb.hide_only())).message_id
    menu_kb = MENU.model_dump_json(exclude_none=True)
    real = rig.session.make_request
    fired = []

    async def handler_meanwhile(bot, method, timeout=None):
        out = await real(bot, method, timeout)
        if isinstance(method, EditMessageReplyMarkup) and int(method.message_id) == mid and not fired:
            fired.append(1)
            rig.clock.advance(1)
            rig.db.list_msg_put(CHAT, mid, "", "📋 Свои сайты", menu_kb, [], "✅ Сайт добавлен")
        return out
    monkeypatch.setattr(rig.session, "make_request", handler_meanwhile)
    with caplog.at_level(logging.INFO):
        await heal.nightly(rig.bot, rig.services)
    assert fired, "сценарий не о том: правки такта не было"
    rec = rig.rec(mid)
    assert rec is not None and rec["kb"] == menu_kb and rec["note"] == "✅ Сайт добавлен", \
        f"правка такта затёрла более свежую запись обработчика: {rec}"
    assert "запись изменилась во время правки" in caplog.text, caplog.text


# ── незнакомый отказ ─────────────────────────────────────────────────────────

def _odd_refusal(rig, monkeypatch, kind: str, chat: int = CHAT, mid: int | None = None):
    """Правки в чате chat (сообщения mid, если назван) получают ответ, которого правка
    не знает: 418 или не-JSON (прокси отдал страницу вместо ответа)."""
    import json as _json
    from aiogram.methods import EditMessageReplyMarkup
    real = rig.session.make_request

    async def odd(bot, method, timeout=None):
        if (isinstance(method, (EditMessageText, EditMessageReplyMarkup)) and int(method.chat_id) == chat
                and (mid is None or int(method.message_id) == mid)):
            rig.session.edits.append((rig.vt.t, type(method).__name__, int(method.chat_id),
                                      int(method.message_id)))
            if kind == "teapot":
                body = _json.dumps({"ok": False, "error_code": 418, "description": "I'm a teapot"})
                return rig.session.check_response(bot=bot, method=method, status_code=418, content=body).result
            return rig.session.check_response(bot=bot, method=method, status_code=502,
                                              content="<html>Bad Gateway</html>").result
        return await real(bot, method, timeout)
    monkeypatch.setattr(rig.session, "make_request", odd)


@pytest.mark.parametrize("kind", ["teapot", "decode"])
async def test_an_unknown_refusal_in_an_action_keeps_the_record_and_the_next_message_is_still_edited(
        rig, monkeypatch, kind):
    """Незнакомый ответ Telegram на правку вытесненного: отправка 51-го не падает (человек
    получает свой итог), запись остаётся — ссылку поправит такт, а соседнее сообщение
    с вытесненной ссылкой поправлено тем же действием, а не брошено."""
    k1, k2 = rig.snap(), rig.snap()
    a = await _shown(rig, ("и ещё 20", k1))
    b = await _shown(rig, ("и ещё 27", k2))
    first, second = [int(r["mid"]) for r in rig.db.list_msg_all(CHAT)]      # в этом порядке их правит действие
    assert {first, second} == {a, b}
    keys = {a: k1, b: k2}
    _odd_refusal(rig, monkeypatch, kind, mid=first)
    rig.fill(50 - 2)
    rig.snap()
    k = rig.snap()                                    # вытеснены k1 и k2
    m = await rig.bot.send_message(CHAT, "Новый итог " + rig.link(k), reply_markup=kb.hide_only())
    assert m.message_id and rig.heads()[-1].startswith(("+ send", "= edit")), f"{kind}: отправка сорвалась"
    assert any(e[3] == first for e in rig.session.edits), f"{kind}: сценарий не о том — правки не было"
    rec = rig.rec(first)
    assert rec is not None and rec["keys"] == [keys[first]], \
        f"{kind}: запись снята — ссылка так и умрёт ссылкой: {rec}"
    assert "list-snap-" not in rig.text_of(second), f"{kind}: после незнакомого отказа соседнее сообщение брошено"


# ── в действии — не больше трёх правок, без ждущих повтора после 429 ─────────

async def test_an_action_edits_at_most_three_messages_and_leaves_the_rest_to_the_tick(rig):
    """Пять сообщений с вытесненными разом снимками: обработчик правит три (человек не
    ждёт пять секунд темпа чата), два — такт."""
    shown = []
    for i in range(5):
        k = rig.snap()
        shown.append((await _shown(rig, (f"и ещё {i + 20}", k))))
    rig.fill(50 - 5)
    for _ in range(4):
        rig.snap()                                    # вытеснены первые четыре
    k = rig.snap()                                    # пятый вытеснен этим, его ссылка уходит в чат
    await rig.bot.send_message(CHAT, "Новый итог " + rig.link(k), reply_markup=kb.hide_only())
    edited = [m for m in shown if "list-snap-" not in (rig.text_of(m) or "")]
    assert len(edited) == heal.ACTION_MAX == 3, f"в действии поправлено {len(edited)} из 5"
    left = [m for m in shown if m not in edited]
    assert all(rig.rec(m) and rig.rec(m)["keys"] for m in left), "оставшиеся такту записи потеряны"
    await heal.nightly(rig.bot, rig.services)
    assert all("list-snap-" not in rig.text_of(m) for m in shown), "такт не поправил оставшиеся"


async def test_an_action_skips_a_message_waiting_for_its_429_retry(rig, monkeypatch):
    """Сообщение уже ждёт фонового повтора после 429: вторая попытка из действия —
    лишний запрос под тот же флуд-контроль."""
    k1, k2 = rig.snap(), rig.snap()
    a = await _shown(rig, ("и ещё 20", k1))
    b = await _shown(rig, ("и ещё 27", k2))
    monkeypatch.setitem(heal._retry, (CHAT, a), 5)
    rig.fill(50 - 2)
    rig.snap()
    k = rig.snap()                                    # вытеснены k1 и k2
    await rig.bot.send_message(CHAT, "Новый итог " + rig.link(k), reply_markup=kb.hide_only())
    assert _edits_of(rig, a) == [], f"ждущее повтора после 429 поправлено из действия: {_edits_of(rig, a)}"
    assert "list-snap-" not in rig.text_of(b), "соседнее сообщение не поправлено"
