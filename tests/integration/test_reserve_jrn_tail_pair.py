"""Хвост старой эпохи на паре (journal/tail, follow, guestj, handover, route): основа,
резерв и бот основы на резерве (гость) на трёх базах — мир
tests/integration/_reserve_jrn_duo.py.

- авария: основа успела записать правки, которых резерв не подтвердил; бот на
  резерве открылся по журналу и пишет своё; основа уходит в следование — точка
  расхождения F из st резерва, свой хвост — откатить в базе и пирах, `jtail`
  через резерв гостю; гость перебазирует и отвечает `jtail_ok` со списком
  спорного под K; основа хранит список и шлёт Т-Ж4 после возврата отдельным
  сообщением сразу за Т-РУ5б; гость уведомлений не шлёт;
- повтор `jtail` — тот же ответ без второго перебазирования (и после перезапуска
  гостя); подделки — отказ, хвост цел;
- «вернуть без резерва» (С9): правки бота на резерве после точки взятия эпохи
  основой перебазируются у основы при получении (живой гость или журнал гостя с
  диска резерва после его перезагрузки), Т-Ж4/Т-Ж5 — в _return_done;
- свой хвост, не принятый за 10 минут, возврат не держит: основа переносит его
  сама после возврата; обновление отказывает, пока ждём хвост или правки бота
  на резерве.

Ключи — заглушки (`DUMMY`), K — настоящие 32 байта.

Цена ошибки: хвост без отката — события держателя ложатся поверх правок,
которых у него нет (уникальность, расхождение баз навсегда); хвост, снятый без
ответа на него, — правки последних секунд перед аварией теряются молча;
повтор, перебазированный второй раз, — счётчики и вставки дважды; принятая
подделка — чужое слово переписывает базу; Т-Ж4, собранное на госте, — список
пропадает с `--wipe` резерва; Т-Ж4 не за Т-РУ5б — админ читает отчёт о
спорном раньше, чем узнаёт, что пользователи вернулись; С9 без перебазирования —
правки людей на резерве за время отрезанности пропадают; возврат, который
ждёт хвост вечно, — пользователи не возвращаются.
"""
from __future__ import annotations

import os
import shutil
import time

import pytest

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.domain.services.types import ServiceError
from awgbot.infra import awg, rsvzone
from awgbot.util import timeutil
from awgbot.util.rsvjrn import event

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo, checked, sends, to_guest

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _names(db) -> set[str]:
    return {c.name for c in db.list_clients()}


# ── мир аварии ──────────────────────────────────────────────────────────────

def _base(duo, extra: int = 0) -> dict:
    """Журнал включён, Иван (и ещё extra профилей) у основы — подтверждены резервом."""
    duo.journal_on()
    ids = {"ivan": duo.edit("Иван")}
    ids["many"] = [duo.a.db.create_client(f"П{n:02d}", 1, _START, _FUTURE, f"INV-DUMMY-P{n}") for n in range(extra)]
    duo.tick()
    assert duo.a.db.jrn_meta()["acked"] == duo.a.db.jrn_last_i(), "условие: всё до аварии подтверждено"
    return ids


def _follow_after_crash(duo) -> dict:
    """Гость открылся по журналу (авария), основа узнаёт его эпоху и точку из st резерва
    и уходит в следование."""
    gm = duo.gdb.jrn_meta()
    keep = duo.b.reserve_keep_view()
    keep.update(guest_e=gm["epoch"], guest_fork=gm["fork_i"])
    duo.b.__dict__["_rsv_guest_jrn"] = {"e": gm["epoch"], "fork": gm["fork_i"]}
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "serving"})
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following(), "основа не ушла в следование"
    return gm


def _crash(duo, *, tail, guest, extra: int = 0) -> dict:
    """Правки основы перед аварией (`tail(db, ids)`, не дошли), открытие гостя,
    правки гостя (`guest(db, ids)`), основа следует."""
    ids = _base(duo, extra)
    tail(duo.a.db, ids)
    ids["tail_top"] = duo.a.db.jrn_last_i()
    duo.open_guest(button=False)
    with duo.as_guest():
        guest(duo.g.db, ids)
    ids["gm"] = _follow_after_crash(duo)
    return ids


def _rename_ivan(db, ids):
    db.update_client_fields(ids["ivan"], name="Иван здесь")


def _guest_rename_ivan(db, ids):
    db.update_client_fields(ids["ivan"], name="Иван на резерве")


def _tail_vera(db, ids):
    v = db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
    ids["vera"] = v
    ids["vera_dev"] = db.create_device(v, "Планшет", "DUMMY-pub-vera", "DUMMY-psk", "10.8.1.77",
                                      private_key="DUMMY-priv-vera")


def _jtail_parts(duo) -> list[dict]:
    duo.a.__dict__.pop("_tail_sent_at", None)
    acts = duo.a.reserve_jrn_tick()
    return [checked(k, b) for k, b in sends(acts, "jtail")]


def _to_guest(duo, kind: str, bodies: list[dict]) -> list[tuple]:
    """Сообщения основы через бот резерва — гостю; ответы гостя (по локальному каналу)."""
    at_b = []
    for b in bodies:
        at_b += duo.b.reserve_jrn_in(kind, b)
    assert to_guest(at_b, kind), f"резерв не передал «{kind}» гостю: {at_b}"
    return duo.b_to_guest(at_b)


def _to_main(duo, msgs: list[tuple]) -> list[tuple]:
    return duo.b_to_a(duo.guest_to_b(msgs))


def _guest_events_to_main(duo) -> list[tuple]:
    """Такт гостя: его события (в т. ч. перенесённый хвост) — основе, её ответы — гостю."""
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    duo.b_to_guest(duo.a_to_b(at_a))
    return at_a


def _return_to_done(duo) -> list:
    """Возврат с журналом до конца: гость сдаёт эпоху, основа берёт её, резерв снял
    хранилище (`wiped`) — _return_done; уведомления по порядку."""
    duo.a.reserve_set_out("returning", kind="dead", gen=7)
    duo.a._out_step("back_req", 300)
    duo.a.reserve_epoch_sync()
    duo.b.rsv_update("rsv_in", state="releasing")
    _guest_events_to_main(duo)
    with duo.as_guest():
        assert duo.g.guest_jrn_drained(), "гость не сдан: основа не подтвердила его события"
        rel = duo.g.guest_jrn_release()
    (kind, body), = [(a[0], a[1]) for a in duo.b.reserve_guest_released_jrn(rel)]
    assert kind == "send_released", kind
    acts = duo.a.reserve_released_jrn_in(body["epoch"], body["last"])
    assert ("take_tg",) in acts, acts
    return rm.notices(duo.a.reserve_wiped_in())


# ── авария: откат, jtail, перебазирование у гостя, jtail_ok ─────────────────

def test_after_a_crash_the_primary_undoes_its_tail_with_peers_at_the_fork_from_the_reserve(duo):
    def tail(db, i):
        _rename_ivan(db, i)
        _tail_vera(db, i)
        duo.fake.peers["DUMMY-pub-vera"] = ("DUMMY-psk", "10.8.1.77")
    ids = _crash(duo, tail=tail, guest=_guest_rename_ivan)
    m = duo.a.db.jrn_meta()
    assert (m["applied"], m["fork_i"]) == (ids["gm"]["fork_i"], ids["gm"]["fork_i"]), m
    assert duo.a.db.get_client(ids["ivan"]).name == "Иван", "правка хвоста осталась в базе следующего"
    assert "Вера" not in _names(duo.a.db), "вставка хвоста осталась — событие держателя столкнётся с ней"
    assert "DUMMY-pub-vera" not in duo.fake.peers, "пир откаченного устройства остался на интерфейсе"
    t = duo.a.rsv_get("rsv_tail")
    assert (t.get("e"), t.get("F")) == (ids["gm"]["epoch"] - 1, ids["gm"]["fork_i"]), t
    assert [e.i for e in duo.a.db.jrn_tail_rows()] == list(range(ids["gm"]["fork_i"] + 1, ids["tail_top"] + 1))


def test_the_tail_goes_to_the_guest_who_rebases_it_and_answers_with_the_disputes_under_k(duo):
    ids = _crash(duo, tail=lambda db, i: (_rename_ivan(db, i), _tail_vera(db, i)), guest=_guest_rename_ivan)
    parts = _jtail_parts(duo)
    assert parts and all(p["F"] == ids["gm"]["fork_i"] for p in parts), parts
    at_b = []
    for p in parts:
        at_b += duo.b.reserve_jrn_in("jtail", p)
    with duo.as_guest():
        acts = []
        for kind, body in to_guest(at_b, "jtail"):
            acts += duo.g.reserve_tail_in(body)
    assert acts and all(a[0] == "guest_send" and a[1] == "jtail_ok" for a in acts), \
        f"гость шлёт что-то кроме ответа (уведомление админу из гостевой базы?): {acts}"
    assert _names(duo.gdb) >= {"Вера", "Иван на резерве"}, _names(duo.gdb)
    assert "Иван здесь" not in _names(duo.gdb), "правка хвоста победила новую эпоху"
    ok = acts[0][2]
    assert (ok["e"], ok["F"], ok["disputed_n"], ok["more"]) == (ids["gm"]["epoch"] - 1, ids["gm"]["fork_i"], 1, 0), ok
    assert ok["moved"] >= 1 and ok["blob"], ok
    assert "Иван здесь" not in bytes.fromhex(ok["blob"]).decode("latin-1"), "список спорного не под K"
    duo.b_to_a(duo.guest_to_b([(a[1], a[2]) for a in acts]))
    assert duo.a.db.jrn_tail_rows(("wait", "sent", "hold")) == [] and duo.a.rsv_get("rsv_tail") == {}, \
        "основа не сняла хвост по ответу"
    rep = duo.a.rsv_get("rsv_out").get("tail_report") or {}
    assert rep.get("variant") == "crash" and rep.get("total") == 1, rep
    (d,) = rep["disputed"]
    assert (d["col"], d["tail"], d["kept"], d["client_id"]) == ("name", "Иван здесь", "Иван на резерве", ids["ivan"]), d


def test_the_rebased_tail_comes_back_to_the_primary_as_holder_events_with_the_private_key(duo):
    """Перенесённое — события эпохи гостя: основа применяет их обычным порядком; закрытый
    ключ откаченного устройства (у гостя его нет) — снова на месте."""
    _crash(duo, tail=lambda db, i: _tail_vera(db, i), guest=lambda db, i: None)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    _guest_events_to_main(duo)
    vera = [c for c in duo.a.db.list_clients() if c.name == "Вера"]
    assert vera, "перенесённая вставка не вернулась к основе"
    dev = [d for d in duo.a.db.list_all_devices() if d.client_id == vera[0].id][0]
    assert duo.a.db._connection().execute("SELECT private_key FROM devices WHERE id = ?", (dev.id,)).fetchone()[0] \
        == "DUMMY-priv-vera", "закрытый ключ устройства из хвоста не вернулся — конфигурацию не выдать"


def test_the_primary_sends_tj4_right_after_the_return_report(duo):
    """Т-Ж4 шлёт основа — после возврата, отдельным сообщением сразу за Т-РУ5б."""
    _crash(duo, tail=_rename_ivan, guest=_guest_rename_ivan)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    notes = _return_to_done(duo)
    texts = [n.text for n in notes]
    assert len(texts) >= 2 and texts[0].startswith("↩️ Пользователи и бот снова здесь"), texts
    tj4 = texts[1]
    assert tj4.startswith("⚠️ Не перенесена 1 правка, сделанная здесь перед переездом на «Beta»"), tj4
    assert "имя «Иван здесь» — на «Beta» осталось «Иван на резерве»" in tj4, tj4
    assert notes[1].silent, "Т-Ж4 со звуком"
    assert duo.a.rsv_get("rsv_out").get("tail_report") is None, "отчёт хвоста не снят — придёт снова"


# ── повтор и подделки ───────────────────────────────────────────────────────

def _rebased_once(duo) -> tuple[list, list]:
    _crash(duo, tail=lambda db, i: (_rename_ivan(db, i), _tail_vera(db, i)), guest=_guest_rename_ivan)
    parts = _jtail_parts(duo)
    first = _to_guest(duo, "jtail", parts)
    return parts, first


def test_a_repeated_jtail_gets_the_same_answer_without_a_second_rebase(duo):
    parts, first = _rebased_once(duo)
    top = duo.gdb.jrn_last_i()
    again = _to_guest(duo, "jtail", parts)
    assert [(k, {x: v for x, v in b.items() if x != "blob"}) for k, b in again] == \
        [(k, {x: v for x, v in b.items() if x != "blob"}) for k, b in first], "на повтор — другой ответ"
    assert duo.gdb.jrn_last_i() == top, "повтор хвоста перебазирован второй раз"
    assert len([c for c in duo.gdb.list_clients() if c.name == "Вера"]) == 1


def test_a_repeated_jtail_after_a_guest_restart_gets_the_same_answer(duo):
    """Отметка «перенесён» — в базе гостя, а не в памяти: перезапущенный гость отвечает
    тем же и не переносит второй раз."""
    parts, first = _rebased_once(duo)
    top = duo.gdb.jrn_last_i()
    old = config.GUEST
    config.GUEST = True
    try:
        duo.g = Services(duo.gdb)
    finally:
        config.GUEST = old
    again = _to_guest(duo, "jtail", parts)
    assert [b["moved"] for _, b in again] == [b["moved"] for _, b in first] and again, again
    assert duo.gdb.jrn_last_i() == top, "перезапущенный гость перенёс хвост второй раз"


def _guest_says(duo, body) -> list:
    with duo.as_guest():
        return duo.g.reserve_tail_in(body)


def test_a_jtail_with_a_foreign_fork_is_refused(duo):
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    p = _jtail_parts(duo)[0]
    top = duo.gdb.jrn_last_i()
    for f in (p["F"] - 1, p["F"] + 1):
        assert _guest_says(duo, dict(p, F=f)) == [], f"хвост с чужой точкой {f} принят"
    assert duo.gdb.jrn_last_i() == top and "Вера" not in _names(duo.gdb)
    assert _guest_says(duo, p), "настоящий хвост после отказов не принят"


def test_a_jtail_with_events_of_the_holders_epoch_is_refused(duo):
    """Записи хвоста эпохи держателя (или не после F) — не хвост: отказ."""
    ids = _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    p = _jtail_parts(duo)[0]
    k, line = duo.a.reserve_k(), duo.a.db.jrn_line()
    evs = duo.a.db.jrn_tail_rows()
    from awgbot.util.rsvjrn.apply import Event
    same_epoch = [Event(ev.i, ids["gm"]["epoch"], ev.t, ev.op, ev.k, ev.c, ev.o, ev.at) for ev in evs]
    blob = b"".join(event.seal_event(k, line, ev, tail=True) for ev in same_epoch).hex()
    assert _guest_says(duo, dict(p, blob=blob)) == [], "события эпохи держателя приняты за хвост"
    early = [Event(ids["gm"]["fork_i"], ev.e, ev.t, ev.op, ev.k, ev.c, ev.o, ev.at) for ev in evs[:1]]
    blob = b"".join(event.seal_event(k, line, ev, tail=True) for ev in early).hex()
    assert _guest_says(duo, dict(p, blob=blob)) == [], "событие с номером по F принято за хвост"
    assert "Вера" not in _names(duo.gdb)


def test_ordinary_records_are_not_taken_for_a_tail(duo):
    """Записи вида E (обычные события) под тем же K — хвостом не выдать."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    p = _jtail_parts(duo)[0]
    k, line = duo.a.reserve_k(), duo.a.db.jrn_line()
    blob = b"".join(event.seal_event(k, line, ev) for ev in duo.a.db.jrn_tail_rows()).hex()
    assert _guest_says(duo, dict(p, blob=blob)) == []
    assert "Вера" not in _names(duo.gdb)


def test_a_jtail_under_another_key_is_refused(duo):
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    p = _jtail_parts(duo)[0]
    blob = b"".join(event.seal_event(os.urandom(32), duo.a.db.jrn_line(), ev, tail=True)
                    for ev in duo.a.db.jrn_tail_rows()).hex()
    assert _guest_says(duo, dict(p, blob=blob)) == []
    assert "Вера" not in _names(duo.gdb)


def _ok_body(duo) -> dict:
    parts = _jtail_parts(duo)
    (kind, body), = _to_guest(duo, "jtail", parts)
    return checked(kind, body)


@pytest.mark.parametrize("forge", ["empty_blob", "other_fork", "other_epoch", "inner_mismatch", "garbage"])
def test_a_forged_jtail_ok_does_not_drop_the_primarys_tail(duo, forge):
    """Ответ не на мой хвост или без открывшегося списка — хвост цел, отчёта нет."""
    _crash(duo, tail=_rename_ivan, guest=_guest_rename_ivan)
    ok = _ok_body(duo)
    k = duo.a.reserve_k()
    if forge == "empty_blob":
        bad = dict(ok, blob="")
    elif forge == "other_fork":
        bad = dict(ok, F=ok["F"] + 1)
    elif forge == "other_epoch":
        bad = dict(ok, e=ok["e"] + 1)
    elif forge == "inner_mismatch":
        inner = {"v": 1, "line": duo.a.db.jrn_line(), "e": ok["e"], "F": ok["F"] - 1, "disputed": [], "more": 0}
        bad = dict(ok, blob=event.seal_obj(k, inner).hex())
    else:
        bad = dict(ok, blob=os.urandom(300).hex())
    duo.a.reserve_jrn_in("jtail_ok", bad)
    assert duo.a.db.jrn_tail_rows(), f"хвост снят подделкой ({forge})"
    assert not duo.a.rsv_get("rsv_out").get("tail_report"), "отчёт по подделке"
    duo.a.reserve_jrn_in("jtail_ok", ok)
    assert not duo.a.db.jrn_tail_rows() and duo.a.rsv_get("rsv_out").get("tail_report"), "настоящий ответ не принят"


def test_more_than_2000_disputes_go_as_2000_records_and_more(duo, monkeypatch):
    """Список под K — не больше 2000 записей, сверх — числом `more`; Т-Ж4 называет все."""
    _crash(duo, tail=_rename_ivan, guest=_guest_rename_ivan)
    real = duo.gdb.jrn_rebase

    def padded(evs, before_commit=None):
        def bc(conn, rpt):
            rpt.disputed += [dict(rpt.disputed[0], i=10_000 + n) for n in range(2000)]
            before_commit(conn, rpt)
        return real(evs, before_commit=bc)
    monkeypatch.setattr(duo.gdb, "jrn_rebase", padded)
    msgs = _to_guest(duo, "jtail", _jtail_parts(duo))
    assert msgs and all(b["disputed_n"] == 2001 and b["more"] == 1 for _, b in msgs), \
        [{x: v for x, v in b.items() if x != "blob"} for _, b in msgs]
    _to_main(duo, msgs)
    rep = duo.a.rsv_get("rsv_out").get("tail_report") or {}
    assert len(rep.get("disputed") or []) == 2000 and rep.get("more") == 1 and rep.get("total") == 2001, \
        {k: (len(v) if isinstance(v, list) else v) for k, v in rep.items()}
    tj4 = [n.text for n in duo.a.reserve_tail_notes()][0]
    assert "Не перенесена 2001 правка, сделанная" in tj4 and "… и ещё 1996" in tj4, tj4


# ── список у основы после --wipe резерва ────────────────────────────────────

def test_after_the_reserves_wipe_all_twelve_lines_are_built_by_the_primary(duo):
    """Возврат после аварии с 12 спорными, хранилище гостя и reserve/jrn резерва удалены:
    весь список — в базе основы (rsv_out.tail_report до отправки), Т-Ж4 — пять строк
    и «и ещё 7» после."""
    def tail(db, ids):
        for cid in ids["many"]:
            db.update_client_fields(cid, name=f"Здесь {cid}")

    def guest(db, ids):
        for cid in ids["many"]:
            db.update_client_fields(cid, name=f"Там {cid}")
    ids = _crash(duo, tail=tail, guest=guest, extra=12)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    # --wipe резерва: журнал данных гостя, хранилище журнала основы и база гостя — прочь
    duo.b_keep.wipe()
    shutil.rmtree(rsvzone.VAR_DIR / "jrn", ignore_errors=True)
    rep = duo.a.rsv_get("rsv_out").get("tail_report") or {}
    assert len(rep.get("disputed") or []) == 12 and rep.get("total") == 12, rep
    lines = [duo.a._tail_line(d, "crash") for d in rep["disputed"]]
    assert len(set(lines)) == 12, lines
    for cid in ids["many"]:
        assert any(f"«Здесь {cid}»" in ln and f"«Там {cid}»" in ln for ln in lines), (cid, lines)
    notes = _return_to_done(duo)
    tj4 = notes[1].text
    assert tj4.startswith("⚠️ Не перенесены 12 правок"), tj4
    assert tj4.count("\n• ") == 5 and tj4.rstrip().endswith("… и ещё 7"), tj4


# ── возврат ждёт хвост не дольше 10 минут ───────────────────────────────────

def test_the_return_waits_for_the_own_tail_up_to_ten_minutes(duo):
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)                                   # ушло, ответа нет
    why = duo.a.reserve_return_check()
    assert why == "Правки, сделанные здесь перед переездом на «Beta», ещё переносятся туда — возврат после этого", why
    duo.a.rsv_update("rsv_tail", at=_ago(9))
    assert duo.a.reserve_return_check() == why, "ждал меньше 10 минут"
    duo.a.rsv_update("rsv_tail", at=_ago(11))
    assert "переносятся" not in duo.a.reserve_return_check(), "возврат держит хвост дольше 10 минут"


def test_a_tail_never_taken_by_the_guest_is_moved_by_the_primary_after_the_return(duo):
    """Бот на резерве хвост так и не принял — после возврата основа переносит его сама
    поверх своей базы; Т-Ж5 «после возврата»."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    duo.a.rsv_update("rsv_tail", at=_ago(11))
    notes = _return_to_done(duo)
    assert "Вера" in _names(duo.a.db), "свой хвост после возврата не перенесён"
    assert not duo.a.db.jrn_tail_rows(), "хвост остался"
    texts = [n.text for n in notes]
    head, n = texts[1].rsplit(": ", 1)
    assert head == "ℹ️ Правки, сделанные здесь перед переездом на «Beta», перенесены после возврата" and int(n) >= 1, \
        f"Т-Ж5 своего хвоста не вторым сообщением или не тем вариантом: {texts}"
    assert notes[1].silent


# ── отказ обновления ────────────────────────────────────────────────────────

def test_the_update_is_refused_while_the_own_tail_is_on_its_way(duo):
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    _jtail_parts(duo)
    with pytest.raises(ServiceError) as e:
        duo.a.reserve_refuse_update_if_waiting()
    assert str(e.value) == "Правки, сделанные здесь перед переездом на «Beta», ещё переносятся туда — обновление после этого"
    duo.a.rsv_update("rsv_tail", at=_ago(11))
    duo.a.reserve_refuse_update_if_waiting()           # дольше 10 минут — не держит


def test_the_update_is_refused_while_waiting_for_the_reserves_edits(mv):
    rc.pair_row(mv.svc)
    mv.svc.rsv_update("rsv_out", await_merge=True)
    with pytest.raises(ServiceError) as e:
        mv.svc.reserve_refuse_update_if_waiting()
    assert str(e.value) == "Жду правки бота на «Beta», сделанные после потери связи, — обновление после того, как они придут"
    mv.svc.rsv_update("rsv_out", await_merge=False)
    mv.svc.reserve_refuse_update_if_waiting()


# ── С9: «вернуть без резерва» ───────────────────────────────────────────────

def _away(duo) -> None:
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following()


def _force_home(duo) -> dict:
    """Резерв не на связи 11 минут — рычаг: эпоха здесь, правки бота на резерве — хвостом."""
    rm.automatic(duo.a)
    duo.a.db.set_state("last_server_ok", "0")
    duo.a.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    guest_e = duo.a.db.jrn_meta()["epoch"]
    duo.a.reserve_force_home()
    duo.a.__dict__.pop("_rsv_pkg_due_at", None)
    duo.a.reserve_stuck_home()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    c9 = duo.a.rsv_get("rsv_c9")
    assert (c9.get("e"), c9.get("F")) == (guest_e, m["fork_i"]), c9
    return c9


def _c9_world(duo) -> dict:
    """Иван у основы, переезд; гость отрезан и пишет (переименовал Ивана, завёл Ольгу),
    основа вернула пользователей без резерва и тоже переименовала Ивана."""
    ids = _base(duo)
    _away(duo)
    c9 = _force_home(duo)
    with duo.as_guest():
        duo.g.db.update_client_fields(ids["ivan"], name="Иван на резерве")
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    duo.a.db.update_client_fields(ids["ivan"], name="Иван здесь")
    ids["c9"] = c9
    return ids


def _c9_return_done(duo) -> list:
    """Резерв снова на связи с гостем: обычный возврат с back_req, гость сдаёт эпоху, wiped."""
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "serving"})
    acts = duo.a.reserve_out_tick()
    assert duo.a.reserve_out() == "returning", acts
    duo.b.rsv_update("rsv_in", state="releasing")
    _guest_events_to_main(duo)
    with duo.as_guest():
        rel = duo.g.guest_jrn_release()
    (kind, body), = [(a[0], a[1]) for a in duo.b.reserve_guest_released_jrn(rel)]
    duo.a.reserve_released_jrn_in(body["epoch"], body["last"])
    return rm.notices(duo.a.reserve_wiped_in())


def test_c9_the_reserves_edits_are_rebased_at_the_primary_on_arrival_and_acked(duo):
    ids = _c9_world(duo)
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    assert [k for k, _ in sends(at_a)] == ["jack"], f"правки бота на резерве не подтверждены: {at_a}"
    assert "Ольга" in _names(duo.a.db), "правка бота на резерве не перенесена"
    assert duo.a.db.get_client(ids["ivan"]).name == "Иван здесь", "в С9 побеждает основа (держатель)"
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9["total"] == 1 and c9["moved"] >= 1 and c9["upto"] > ids["c9"]["F"], c9
    again = _to_main(duo, msgs)
    assert sends(again, "jack") and len([c for c in duo.a.db.list_clients() if c.name == "Ольга"]) == 1, \
        "повтор пачки бота на резерве перенесён второй раз"


def test_c9_tj4_goes_after_the_return_report_in_the_c9_wording(duo):
    _c9_world(duo)
    notes = _c9_return_done(duo)
    texts = [n.text for n in notes]
    tj4 = [t for t in texts if t.startswith("⚠️ Не перенесен")]
    assert len(tj4) == 1 and texts.index(tj4[0]) == 1, texts
    assert tj4[0].startswith("⚠️ Не перенесена 1 правка бота на «Beta», сделанная после потери связи: здесь те же поля "
                             "успели поменять по-своему"), tj4[0]
    assert "имя «Иван на резерве» — здесь осталось «Иван здесь»" in tj4[0], tj4[0]
    assert duo.a.rsv_get("rsv_c9") == {}, "отчёт С9 не снят — придёт снова"


def test_c9_without_disputes_is_one_quiet_tj5(duo):
    ids = _base(duo)
    _away(duo)
    _force_home(duo)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
    del ids
    texts = [n.text for n in _c9_return_done(duo)]
    tj5 = [t for t in texts if t.startswith("ℹ️ Правки бота на «Beta» после потери связи перенесены сюда: ")]
    assert len(tj5) == 1 and texts.index(tj5[0]) == 1, texts
    assert not [t for t in texts if t.startswith("⚠️ Не перенесен")], texts


def test_c9_after_a_reserve_reboot_takes_the_guests_journal_from_its_disk(duo):
    """Резерв перезагрузился, гостя нет, а журнал данных гостя на диске есть: основа не
    уходит в «правки не вольются», возврат забирает журнал с диска, правки — хвостом."""
    ids = _c9_world(duo)
    with duo.as_guest():
        duo.g.guest_jrn_tick()                            # записи — на диск резерва, основе не дошли
    duo.gdb.close()
    duo.gdb, duo.g = None, None                           # гостя нет
    duo.b.rsv_update("rsv_in", state="idle")
    keep = duo.b.reserve_keep_view()
    assert keep.get("gj_e") == ids["c9"]["e"] and keep.get("gj_last", 0) > ids["c9"]["upto"], keep
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "idle"})
    acts = duo.a.reserve_out_tick()
    assert duo.a.reserve_out() == "returning" and rm.sent(acts) == ["back_req"], \
        f"основа ушла в «не вольются», хотя журнал гостя на диске: {acts}"
    back = duo.b.reserve_back_req_in()
    jev = [checked("jev", a[2]) for a in back if a[0] == "send" and a[1] == "jev"]
    rel = [a[1] for a in back if a[0] == "send_released"]
    assert jev and rel, back
    for b in jev:
        assert sends(duo.a.reserve_jrn_in("jev", b), "jack"), "правки с диска не подтверждены"
    assert "Ольга" in _names(duo.a.db), "журнал гостя с диска не перенесён"
    duo.a.reserve_released_jrn_in(rel[0]["epoch"], rel[0]["last"])
    texts = [n.text for n in rm.notices(duo.a.reserve_wiped_in())]
    assert any("правка бота на «Beta»" in t for t in texts), texts


def test_the_reserve_drops_the_guests_disk_journal_after_a_return_without_a_guest(duo):
    _c9_world(duo)
    with duo.as_guest():
        duo.g.guest_jrn_tick()
    store = rsvzone.VAR_DIR / "jrn"
    assert any(store.iterdir()), "условие: журнал гостя на диске"
    duo.b.rsv_update("rsv_in", state="idle")
    duo.b.reserve_back_in()
    assert not duo.b.guest_jrn_store().events(), "журнал гостя под прежним K остался на диске резерва"


# ── Т-Ж6: отставание следования ─────────────────────────────────────────────

def _following(duo) -> None:
    _base(duo)
    _away(duo)


def _lag(duo, last: int) -> None:
    ps = duo.a.reserve_peer_state()
    keep = dict(ps.get("jrn_keep") or {})
    keep["guest_last"] = last
    duo.a.rsv_update("rsv_peer_state", jrn_keep=keep)


def test_tj6_lag_over_a_minute_is_told_once_and_the_catch_up_once(duo):
    _following(duo)
    applied = duo.a.db.jrn_meta()["applied"]
    _lag(duo, applied + 5)
    assert duo.a.reserve_follow_lag_tick() == [], "отставание сказано раньше минуты"
    duo.a.__dict__["_follow_lag_since"] = time.monotonic() - 61
    first = rm.notices(duo.a.reserve_follow_lag_tick())
    assert len(first) == 1 and first[0].text.startswith("⚠️ Данные здесь перестали догонять бот на «Beta»: ") \
        and first[0].text.endswith(" — возврат подождёт") and first[0].silent, [n.text for n in first]
    assert rm.notices(duo.a.reserve_follow_lag_tick()) == [], "Т-Ж6 повторилось"
    _lag(duo, applied)
    ok = rm.notices(duo.a.reserve_follow_lag_tick())
    assert [n.text for n in ok] == ["🟢 Данные здесь снова идут за ботом на «Beta»"], [n.text for n in ok]
    assert rm.notices(duo.a.reserve_follow_lag_tick()) == [], "🟢 повторилось"


def test_tj6_is_silent_without_a_lag(duo):
    _following(duo)
    _lag(duo, duo.a.db.jrn_meta()["applied"])
    duo.a.__dict__["_follow_lag_since"] = time.monotonic() - 3600
    assert duo.a.reserve_follow_lag_tick() == []


# ── Т-Ж4 на 9 спорных: снимок всего списка в базе основы ────────────────────

def _snap_keys(db) -> list[str]:
    return [r[0][len("list_snap:"):] for r in db._connection().execute(
        "SELECT key FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchall()]


def _tj4_link(text: str) -> str:
    """Ключ снимка из последней строки Т-Ж4 «… и ещё 4» (ссылкой)."""
    import re
    last = text.rstrip().split("\n")[-1]
    m = re.fullmatch(r'… <a href="https://t\.me/test_bot\?start=list-snap-([a-z0-9]{10})">и ещё 4</a>', last)
    assert m, f"хвост Т-Ж4 — не ссылка «и ещё 4» на снимок: {last!r}"
    return m.group(1)


async def _open_snap(svc, key: str):
    from awgbot.bot import lists
    return await lists.render(svc, "snap", role="admin", key=key, chat_id=int(config.ADMIN_ID), new=True)


def _nine(duo, *, here: str, there: str):
    def tail(db, ids):
        for cid in ids["many"]:
            db.update_client_fields(cid, name=f"{here} {cid}")

    def guest(db, ids):
        for cid in ids["many"]:
            db.update_client_fields(cid, name=f"{there} {cid}")
    return tail, guest


async def test_tj4_on_nine_disputes_after_a_crash_links_a_snapshot_kept_at_the_primary(duo):
    """Авария, 9 спорных: Т-Ж4 — пять строк и «… и ещё 4» ссылкой; снимок всех девяти
    лежит в базе бота основы (не гостя), открывается после --wipe резерва; метку «-n»
    ссылке ставит отправка уведомления."""
    from awgbot.bot import links
    duo.a.bot_username = "test_bot"
    tail, guest = _nine(duo, here="Здесь", there="Там")
    _crash(duo, tail=tail, guest=guest, extra=9)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    notes = _return_to_done(duo)
    tj4 = notes[1].text
    assert tj4.startswith("⚠️ Не перенесены 9 правок, сделанные здесь перед переездом на «Beta»"), tj4
    assert tj4.count("\n• ") == 5, tj4
    key = _tj4_link(tj4)
    assert f"list-snap-{key}-n" in links.mark_new(tj4), "из уведомления ссылка не получает метку «-n»"
    assert _snap_keys(duo.a.db) == [key], f"снимки основы: {_snap_keys(duo.a.db)}"
    assert not _snap_keys(duo.gdb), "снимок Т-Ж4 лёг в базу гостя — уйдёт с --wipe резерва"
    duo.b_keep.wipe()
    shutil.rmtree(rsvzone.VAR_DIR / "jrn", ignore_errors=True)
    text, markup = await _open_snap(duo.a, key)
    head, body = text.split("\n\n", 1)
    assert head.startswith("⚠️ <b>Не перенесены правки, сделанные здесь перед переездом на «Beta»</b> · 9 · на "), head
    rows = body.split("\n")
    shown = [ln[2:] for ln in tj4.split("\n") if ln.startswith("• ")]
    assert len(rows) == 9 and rows[:5] == shown, f"строки снимка — не строки Т-Ж4:\n{rows[:2]}\n{shown[:2]}"
    assert all("«Здесь " in r and "«Там " in r for r in rows), rows
    assert [b.text for row in markup.inline_keyboard for b in row] == ["⬅️ В меню"]


async def test_tj4_on_nine_disputes_in_c9_links_a_snapshot_with_the_c9_title(duo):
    duo.a.bot_username = "test_bot"
    ids = _base(duo, extra=9)
    _away(duo)
    _force_home(duo)
    with duo.as_guest():
        for cid in ids["many"]:
            duo.g.db.update_client_fields(cid, name=f"Там {cid}")
    for cid in ids["many"]:
        duo.a.db.update_client_fields(cid, name=f"Здесь {cid}")
    notes = _c9_return_done(duo)
    tj4 = [n.text for n in notes if n.text.startswith("⚠️ Не перенесен")]
    assert len(tj4) == 1, [n.text[:60] for n in notes]
    assert tj4[0].startswith("⚠️ Не перенесены 9 правок бота на «Beta», сделанные после потери связи"), tj4[0]
    key = _tj4_link(tj4[0])
    assert _snap_keys(duo.a.db) == [key] and not _snap_keys(duo.gdb), (_snap_keys(duo.a.db), _snap_keys(duo.gdb))
    duo.b_keep.wipe()
    text, _ = await _open_snap(duo.a, key)
    assert text.startswith("⚠️ <b>Не перенесены правки бота на «Beta»</b> · 9 · на "), text[:120]
    assert len(text.split("\n\n", 1)[1].split("\n")) == 9


def test_tj4_with_one_dispute_has_no_tail_and_no_snapshot(duo):
    duo.a.bot_username = "test_bot"
    _crash(duo, tail=_rename_ivan, guest=_guest_rename_ivan)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    tj4 = _return_to_done(duo)[1].text
    assert "и ещё" not in tj4 and "list-snap-" not in tj4, tj4
    assert not _snap_keys(duo.a.db), "снимок при одной спорной правке — хвоста нет"


def test_tj4_keeps_the_text_tail_when_the_snapshot_is_not_written(duo, monkeypatch):
    from awgbot.infra.db import listsnap

    def locked(self, *a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(listsnap.ListSnapMixin, "_snap_keep", locked)
    duo.a.bot_username = "test_bot"
    tail, guest = _nine(duo, here="Здесь", there="Там")
    _crash(duo, tail=tail, guest=guest, extra=9)
    duo.b_to_a(duo.guest_to_b(_to_guest(duo, "jtail", _jtail_parts(duo))))
    tj4 = _return_to_done(duo)[1].text
    assert tj4.rstrip().endswith("\n… и ещё 4") and "list-snap-" not in tj4, tj4
