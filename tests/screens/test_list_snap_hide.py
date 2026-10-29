"""«Скрыть» у сообщений со ссылками на снимки (следы
переезда, отчёт рассылки, уведомления — восстановление и Т-Ж4; итоги живого
меню — следом из погашенного меню, test_list_msg_retire) и обработчик `hd`
через настоящий диспетчер: нажатие удаляет сообщение и тем же действием
снимает его снимки и запись; отказ удаления — ничего не снято; правка
сообщения, которое человек удалил сам, — снимки прочь; сторож записей
снимков экранов (harness.listmsg_problems) умеет ловить.

Цена ошибки: след или отчёт без «Скрыть» — сообщение со ссылкой нельзя
убрать, и через месяц ссылка ведёт в «больше не хранится»; «Скрыть», не
снявшая снимки, — база держит список удалённого сообщения 30 дней, и такт
правит то, чего нет; снятие при отказе удаления — сообщение осталось в чате
с мёртвой ссылкой, которую больше никто не поправит; сообщение, удалённое
человеком, чья запись осталась, — такт каждую ночь получает «not found».
"""
from __future__ import annotations

import json

import pytest

from awgbot.bot.callbacks import HideCB
from awgbot.core import config
from tests.screens import catalog, harness
from tests.screens import shots_snaps as ss
from tests.screens.base import Shot
from tests.screens.harness import take

pytestmark = pytest.mark.screens

_BY_ID = {s.id: s for s in catalog.SHOTS}
HIDE_ROW = [("Скрыть", "hd")]


def _rows(markup):
    return None if markup is None else [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _num(head: str) -> int:
    return int(head.split("#", 1)[1].split()[0])


# ── «Скрыть» у следов и уведомлений со ссылкой (или без — тот же род) ───────

@pytest.mark.parametrize("shot_id, needle", [
    ("adm.set.mig.start.yes", "🚚 Переезд начат"),                 # след начала переезда
    ("adm.snap.mig.finish", "list-snap-"),                          # след завершения, «и ещё 30»
    ("adm.set.mig.cancel.yes", "↩️ Переезд отменён"),               # след отмены
    ("adm.snap.bc.report", "list-snap-"),                           # отчёт рассылки без картинок, две ссылки
    ("adm.snap.restore", "list-snap-"),                             # отчёт восстановления
    ("adm.snap.tj4.crash", "list-snap-"),                           # Т-Ж4 после аварии
    ("adm.snap.tj4.c9", "list-snap-"),                              # Т-Ж4 С9
])
async def test_every_trace_and_notice_carries_hide_as_its_last_row(tmp_path, frozen, fakes, shot_id, needle):
    rec = await take(_BY_ID[shot_id], tmp_path, fakes)
    hits = [c for c in rec.calls if c.body and needle in c.body]
    assert hits, f"{shot_id}: нет сообщения с «{needle}»: {[c.head for c in rec.calls]}"
    rows = _rows(hits[-1].markup)
    assert rows and rows[-1] == HIDE_ROW, f"{shot_id}: последним рядом не «Скрыть»: {rows}"
    if shot_id.startswith(("adm.set.mig", "adm.snap.mig", "adm.snap.bc")):
        assert rows == [HIDE_ROW], f"{shot_id}: на следе навигация — второе живое меню: {rows}"


# ── обработчик «Скрыть» ──────────────────────────────────────────────────────

def _aim_at_link():
    """Нажать кнопку на сообщении со ссылкой: «Скрыть»-сообщение не живое меню заглушки."""
    async def aim(services, bot):
        s = bot.session
        mids = [_num(c.head) for c in s.calls if c.body and "list-snap-" in c.body and "→ чат" not in c.head]
        assert mids, "ссылки на снимок в чате нет"
        s.nav = mids[-1]
    return aim


def _look(into: dict, *keys: str):
    """Состояние базы после шага (база снимка закрывается вместе с ним)."""
    async def look(services, bot):
        db = services.db
        into["mids"] = [int(r["mid"]) for r in db.list_msg_all() if int(r["chat"]) == config.ADMIN_ID]
        for k in keys:
            row = db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                           ("list_snap:" + k,)).fetchone()
            into[k] = None if row is None else json.loads(row[0])
            into["get:" + k] = db.list_snap_get(k, config.ADMIN_ID)
        await bot.send_message(config.ADMIN_ID, "проверено")
    return look


async def test_hide_deletes_the_message_and_its_two_snapshots_and_the_record(tmp_path, frozen, fakes):
    seen, before = {}, {}
    steps = [*ss._BC, ("call", _aim_at_link(), "прицел"), ("call", _look(before, "snapkey001", "snapkey002"), "до"),
             ("press", HideCB()), ("call", _look(seen, "snapkey001", "snapkey002"), "после")]
    rec = await take(Shot("adm.hide.bc", role="admin", data=ss._bc_thirty, steps=steps), tmp_path, fakes)
    report = [_num(c.head) for c in rec.everything if c.body and "list-snap-" in c.body][-1]
    assert report in before["mids"] and before["snapkey001"] is not None, "до нажатия записи и снимков нет — тест пуст"
    assert f"- delete #{report}" in [c.head for c in rec.everything], "«Скрыть» не удалила отчёт"
    assert report not in seen["mids"], "запись удалённого сообщения осталась"
    for k in ("snapkey001", "snapkey002"):
        assert seen[k] is None, f"{k}: снимок удалённого сообщения остался ({seen[k]})"
        assert seen["get:" + k] is None, f"{k}: ссылка отвечает «больше не хранится», а не «нет»"


async def test_the_hide_handler_drops_snapshots_itself_not_relying_on_the_watch(tmp_path, frozen, fakes, monkeypatch):
    """Посредника нет (он лишь повторил бы): снимки и запись снимает сам обработчик `hd`."""
    from awgbot.bot.lists import msgs
    monkeypatch.setattr(msgs, "attach", lambda bot, services: None)
    seen = {}

    async def put(services, bot):
        s = bot.session
        mid = [_num(c.head) for c in s.calls if c.body and "list-snap-" in c.body][-1]
        body = s.messages[(s.chat_id, mid)]["text"]
        from awgbot.bot.lists.msgs import scan
        services.db.list_msg_put(s.chat_id, mid, "2026-09-15T12:00:00+05:00", body,
                                 '{"inline_keyboard":[[{"text":"Скрыть","callback_data":"hd"}]]}',
                                 scan.keys(body), "")
        s.nav = mid
    steps = [*ss._BC, ("call", put, "запись"), ("press", HideCB()), ("call", _look(seen, "snapkey001", "snapkey002"))]
    await take(Shot("adm.hide.own", role="admin", data=ss._bc_thirty, steps=steps), tmp_path, fakes)
    assert seen["mids"] == [] and seen["snapkey001"] is None and seen["snapkey002"] is None, \
        f"обработчик «Скрыть» не снял снимки и запись сам: {seen}"


class _NoDelete(harness.StubSession):
    """Telegram не удаляет («message can't be deleted for everyone» — старше 48 часов)."""

    async def make_request(self, bot, method, timeout=None):
        from aiogram.methods import DeleteMessage
        if isinstance(method, DeleteMessage):
            self.calls.append(harness.Call(f"! отказ удаления #{method.message_id}"))
            body = {"ok": False, "error_code": 400, "description": "Bad Request: message can't be deleted for everyone"}
            return self.check_response(bot=bot, method=method, status_code=400, content=json.dumps(body)).result
        return await super().make_request(bot, method, timeout)


async def test_a_refused_delete_drops_nothing(tmp_path, frozen, fakes, monkeypatch):
    monkeypatch.setattr(harness, "StubSession", _NoDelete)
    seen = {}
    steps = [*ss._BC, ("call", _aim_at_link(), "прицел"), ("press", HideCB()),
             ("call", _look(seen, "snapkey001", "snapkey002"), "после")]
    rec = await take(Shot("adm.hide.refused", role="admin", data=ss._bc_thirty, steps=steps), tmp_path, fakes)
    report = [_num(c.head) for c in rec.everything if c.body and "list-snap-" in c.body][-1]
    assert f"! отказ удаления #{report}" in [c.head for c in rec.everything], "удаление не запрашивалось"
    assert report in seen["mids"], "запись снята, хотя сообщение осталось в чате"
    assert seen["snapkey001"] is not None and seen["snapkey002"] is not None, "снимки сняты при отказе удаления"
    assert not seen["get:snapkey001"].get("stale"), "снимок оставшегося сообщения больше не открывается"


# ── человек удалил сообщение сам: правка получает «not found» ───────────────

async def test_a_message_deleted_by_hand_loses_its_snapshot_when_the_edit_is_refused(tmp_path, frozen, fakes):
    """Человек удалил отчёт восстановления у себя (бот об этом не знает); 51-й снимок
    вытесняет его снимок — правка получает «message to edit not found»: снимок и запись прочь."""
    seen = {}

    async def by_hand(services, bot):
        s = bot.session
        mid = [_num(c.head) for c in s.calls if c.body and "list-snap-" in c.body][-1]
        s.deleted.add((s.chat_id, mid))
        seen["mid"] = mid

    async def fifty_one(services, bot):
        for n in range(50):
            services.db.list_snap_keep(config.ADMIN_ID, "admin", f"№{n}", ["<code>x</code>"])
        await bot.send_message(config.ADMIN_ID, "Новый итог "
                               '<a href="https://t.me/test_bot?start=list-snap-snapkey051">и ещё 1</a>')
    steps = [("start", ""), ss._RESTORE, ("call", by_hand, "удалил сам"), ("call", fifty_one, "51-й"),
             ("call", _look(seen, "snapkey001"), "после")]
    rec = await take(Shot("adm.hide.by_hand", role="admin", data=ss._restore8, steps=steps, strict_deleted=True),
                     tmp_path, fakes)
    heads = [c.head for c in rec.everything]
    assert f"! правка удалённого #{seen['mid']} — отказ" in heads, f"правки удалённого не было: {heads[-6:]}"
    assert seen["mid"] not in seen["mids"], "запись удалённого человеком сообщения осталась"
    assert seen["snapkey001"] is None, "снимок удалённого сообщения остался отметкой"


# ── сторож записей по снимкам экранов ────────────────────────────────────────

def test_the_screens_guard_catches_a_bare_link_and_a_dimmed_note_menu():
    """Сторож в сверке эталонов (test_screens) не пустой: ссылка без кнопок и погашенное
    меню, у записи которого есть итог, — нарушения; живое меню и «Скрыть» — нет."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    link = 'Итог <a href="https://t.me/test_bot?start=list-snap-abcdefghij-n">и ещё 3</a>'
    hide = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Скрыть", callback_data="hd")]])
    menu = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="m:main")]])
    ok = harness.Record("> x", calls=[harness.Call("+ send #2", link, markup=hide),
                                      harness.Call("= edit #3", link, markup=menu)])
    assert harness.listmsg_problems("adm.x", ok) == []
    bare = harness.Record("> x", calls=[harness.Call("+ send #2", link)])
    assert len(harness.listmsg_problems("adm.x", bare)) == 1, "ссылка без «Скрыть» и не на меню пропущена"
    off = harness.Call("- markup off #3")
    dimmed = harness.Record("> x", calls=[off], everything=[off], notes={3: "✅ Итог"})
    assert len(harness.listmsg_problems("adm.x", dimmed)) == 1, "меню с итогом, погашенное целиком, пропущено"
    assert harness.listmsg_problems("adm.x", harness.Record("> x", calls=[off], everything=[off])) == []
