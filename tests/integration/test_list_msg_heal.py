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
