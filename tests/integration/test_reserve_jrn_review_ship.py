"""Доставка и хранение журнала — правки по ревью 4.1.0 на паре основа ↔ резерв (и бот
основы на резерве) — мир tests/integration/_reserve_jrn_duo.py.

- эпоха линии ниже известной резерву (база восстановлена из копии: jrn_meta в копию
  не идёт) поднимается по его слову — jstale или сверка сессии — и события уходят в
  той же сессии; равная эпоха при держателе здесь и госте на резерве — следование;
  снимок со сбросом не роняет известную резерву эпоху;
- отставание следования — по присланному держателем, а не по застывшему guest_last;
- сверка новой сессии в следовании считает свои номера по подтверждённому и
  применённому (хвост поднял sqlite_sequence) — hold не ставится; хвост и
  отставание идут и при hold;
- нет места на диске у резерва — без jack, сессия не рвётся, админу один раз;
- снимок по таймеру решает «не нужен» до копии базы; переезд кнопкой при журнале
  без изменений вне базы — go_req с прежним поколением, без копии базы;
- такт журнала в простое не читает события и счётчики; страховка — полный проход
  раз в CHECK_S; правка уходит сразу, повтор — по RESEND_S;
- тело st резерва не перечитывает файлы хранилища без их смены; отметка снимка по
  размеру у резерва — не чаще раза в pkg_minutes; проба JSON — раз за процесс.

Ключи — заглушки (`DUMMY`), K — настоящие 32 байта.

Цена ошибки: эпоха, не поднятая после восстановления, — каждое событие получает
jstale каждые 20 с навсегда, данные для переезда у резерва застывают на снимке;
отставание по застывшему st — Т-Ж6 не приходит, когда основа перестала применять;
hold на весь отъезд — свой хвост переносится только после возврата; ENOSPC,
рвущий сессию, — канал пары мигает на каждом jev; копия базы каждые полчаса ради
«не нужен» — диск и ввод-вывод впустую; полный снимок на каждом переезде кнопкой —
лишние минуты ожидания и мегабайты по линку; десяток SELECT раз в секунду на
обоих серверах — бесполезная нагрузка; чтение пачек целиком раз в 5 с — то же.
"""
from __future__ import annotations

import errno
import time

import pytest

from awgbot.domain.services.reserve.journal import ship
from awgbot.infra import awg
from awgbot.util import timeutil
from awgbot.util.rsvpkg import bot as pkgbot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo, sends
from tests.integration.test_reserve_jrn_tail_pair import _crash, _tail_vera, _to_main

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


# ── A6: эпоха линии ниже известной резерву ──────────────────────────────────

def _restored_from_a_copy(duo) -> int:
    """Пара с историей передач: эпоха линии 3, резерв её знает; база основы восстановлена
    из копии — jrn_meta в копию не идёт, эпоха здесь снова 0."""
    duo.journal_on()
    duo.a.db.jrn_pass(holder="self", role="holder", why="тест: история передач", epoch=3)
    duo.edit("До копии")
    duo.tick()
    assert int(duo.b.reserve_keep_view()["e"]) == 3, "условие: резерв знает эпоху 3"
    duo.a.db._connection().execute("UPDATE jrn_meta SET epoch = 0 WHERE id = 1")
    duo.a.db._connection().commit()
    assert int(duo.a.db.jrn_meta()["epoch"]) == 0
    return 3


def test_after_a_restore_the_epoch_is_raised_by_the_reserves_jstale_and_events_go_in_the_same_session(duo):
    known = _restored_from_a_copy(duo)
    duo.edit("После копии")
    first = duo.a.reserve_jrn_tick()
    stale = duo.a_to_b(first)
    assert [k for k, _ in sends(stale)] == ["jstale"], f"условие: резерв отвечает jstale: {stale}"
    duo.b_to_a(stale)
    m = duo.a.db.jrn_meta()
    assert (int(m["epoch"]), m["holder"], m["role"]) == (known, "self", "holder"), \
        f"эпоха не поднята до известной резерву (или ушла в следование): {m}"
    duo.clock.advance(ship.RESEND_S + 1)
    acks = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert [k for k, _ in sends(acks)] == ["jack"], f"события после подъёма эпохи не приняты: {acks}"
    duo.b_to_a(acks)
    assert int(duo.a.db.jrn_meta()["acked"]) == duo.a.db.jrn_last_i(), "события не подтверждены в той же сессии"
    for _ in range(3):                                    # дальше — без jstale по кругу
        duo.clock.advance(ship.RESEND_S + 1)
        assert not sends(duo.a_to_b(duo.a.reserve_jrn_tick()), "jstale")


def test_after_a_restore_the_session_check_raises_the_epoch_before_the_first_event(duo):
    known = _restored_from_a_copy(duo)
    duo.session()                                         # новая сессия: st резерва с keep.e = 3
    assert int(duo.a.db.jrn_meta()["epoch"]) == known, "сверка сессии не подняла эпоху"
    duo.edit("После копии")
    acks = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert [k for k, _ in sends(acks)] == ["jack"], acks


def test_the_epoch_is_not_raised_while_the_reserve_carries_my_users(duo):
    """Резерв несёт моих (serving) — его эпоха значит «держатель у него»: не подъём, а
    следование."""
    _restored_from_a_copy(duo)
    keep = duo.b.reserve_keep_view()
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "serving"})
    m = duo.a.db.jrn_meta()
    assert not (m["role"] == "holder" and int(m["epoch"]) == int(keep["e"])), f"эпоха поднята при госте: {m}"


def test_an_equal_epoch_while_holding_here_is_adopted_as_following(mv):
    """Своя эпоха поднята по слову резерва, а его гость держит тот же номер — два писателя
    под одним номером недопустимы: равная эпоха при держателе здесь — следование;
    при держателе там — уже следую; меньшая — нет."""
    db = mv.svc.db
    db.jrn_pass(holder="self", role="holder", why="тест", epoch=5)
    assert db.jrn_adopt(4, why="меньше") is None
    meta = db.jrn_adopt(5, why="равная при госте")
    assert meta is not None and (meta["holder"], meta["role"], int(meta["epoch"])) == ("peer", "follower", 5), meta
    assert db.jrn_adopt(5, why="повтор") is None, "равная при держателе там — снова переход"


def test_a_reset_snapshot_does_not_lower_the_epoch_the_reserve_knows(duo):
    """Снимок со сбросом с меньшей эпохой (восстановленная основа): известная резерву эпоха
    линии не падает — ни в его отметке, ни в meta.json процесса открытия."""
    import json
    known = _restored_from_a_copy(duo)
    duo.b.reserve_keep_snapshot_in({"line": duo.line, "e": 0, "i": duo.a.db.jrn_last_i(), "cb": 0, "reset": True})
    assert int(duo.b.reserve_keep_view()["e"]) == known, duo.b.reserve_keep_view()
    meta = json.loads((duo.b_keep.path / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"line": duo.line, "e": known}, meta


# ── A7: отставание следования — по присланному ──────────────────────────────

def _following(duo) -> None:
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following()


def test_the_lag_is_measured_by_what_the_holder_sent_not_by_a_frozen_st(duo):
    """st резерва без guest_last (он уходит только по смене линии, эпохи и снимка): гость
    прислал пачку с разрывом — применить нечего; Т-Ж6 после минуты — по присланному."""
    _following(duo)
    keep = dict(duo.a.reserve_peer_state().get("jrn_keep") or {})
    keep.pop("guest_last", None)
    duo.a.rsv_update("rsv_peer_state", jrn_keep=keep)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        duo.g.guest_jrn_tick()                            # потерялось
        duo.g.db.create_client("Пётр", 1, _START, _FUTURE, "INV-DUMMY-P")
        msgs = duo.g.guest_jrn_tick()
    assert sends(_to_main(duo, msgs), "jneed"), "условие: разрыв — не применено"
    assert duo.a.reserve_follow_lag_tick() == [], "отставание сказано раньше минуты"
    duo.a.__dict__["_follow_lag_since"] = time.monotonic() - 61
    notes = rm.notices(duo.a.reserve_follow_lag_tick())
    assert len(notes) == 1 and notes[0].text.startswith("⚠️ Данные здесь перестали догонять бот на «Beta»"), \
        f"Т-Ж6 не пришло при застывшем st: {[n.text for n in notes]}"


def test_the_lag_measure_is_cleared_with_the_follow_state(duo):
    _following(duo)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        duo.g.guest_jrn_tick()
        duo.g.db.create_client("Пётр", 1, _START, _FUTURE, "INV-DUMMY-P")
        msgs = duo.g.guest_jrn_tick()
    _to_main(duo, msgs)
    duo.a.reserve_follow_clear()
    lag, _applied, last = duo.a._follow_lag()
    assert not lag and last <= int(duo.a.db.jrn_meta()["applied"]), "мерка прошлого отъезда осталась"


# ── A10: новая сессия в следовании с хвостом ────────────────────────────────

def test_a_new_session_while_following_with_a_tail_does_not_hold_and_the_tail_goes(duo):
    """Основа ушла в следование с неподтверждёнными правками (они в хвосте), гость ещё
    ничего не писал; сессия пары пересоздалась — сверка не просит снимок, hold не
    ставится, jtail уходит."""
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    duo.a.reserve_session_opened({})
    keep = duo.b.reserve_keep_view()
    gm = duo.gdb.jrn_meta()
    keep.update(guest_e=gm["epoch"], guest_fork=gm["fork_i"])
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "serving"})
    assert not duo.a.rsv_get("rsv_jrn_out").get("reset"), "сверка в следовании попросила снимок со сбросом"
    duo.a.__dict__.pop("_tail_sent_at", None)
    acts = duo.a.reserve_jrn_tick()
    assert sends(acts, "jtail"), f"хвост не ушёл после новой сессии: {acts}"
    assert duo.a._jrn_sess().get("hold") is False, duo.a._jrn_sess()


def test_the_tail_and_the_lag_go_even_while_the_session_holds(duo):
    _crash(duo, tail=_tail_vera, guest=lambda db, i: None)
    duo.a._jrn_snap_need("тест: держим")
    assert duo.a._jrn_sess().get("hold")
    duo.a.__dict__.pop("_tail_sent_at", None)
    assert sends(duo.a.reserve_jrn_tick(), "jtail"), "хвост ждёт снятия hold"


# ── A13: нет места на диске у резерва ───────────────────────────────────────

def test_no_space_at_the_reserve_gives_no_jack_keeps_the_session_and_tells_once(duo, monkeypatch):
    """ENOSPC при дописке журнала основы: подтверждения нет (основа повторит), исключение
    не уходит в канал (сессия не рвётся), админу резерва — один раз на эпизод; место
    появилось — jack; новый эпизод — снова один раз."""
    duo.journal_on()
    real = duo.b_keep.append_events

    def full(records):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(duo.b_keep, "append_events", full)
    duo.edit("Пётр")
    told = []
    for _ in range(3):
        acts = duo.a_to_b(duo.a.reserve_jrn_tick())
        assert not sends(acts, "jack"), f"подтверждено то, что не записано: {acts}"
        told += [n.text for n in rm.notices(acts)]
        duo.clock.advance(ship.RESEND_S + 1)
    assert len(told) == 1 and "не пишется" in told[0] and "No space left on device" in told[0], told
    assert not told[0].endswith("."), told[0]
    monkeypatch.setattr(duo.b_keep, "append_events", real)
    ok = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert sends(ok, "jack") and not rm.notices(ok), ok
    monkeypatch.setattr(duo.b_keep, "append_events", full)
    duo.edit("Павел")
    duo.b_to_a(ok)
    again = duo.a_to_b(duo.a.reserve_jrn_tick())
    assert len(rm.notices(again)) == 1, "новый эпизод нехватки места не сказан"



@pytest.mark.parametrize("err, advice, not_advice", [
    (errno.ENOSPC, "освободи место на диске", "проверь"),
    (errno.EIO, "проверь диск", "место на диске"),
], ids=["no-space", "io-error"])
def test_the_disk_advice_follows_the_errno(duo, monkeypatch, err, advice, not_advice):
    """Совет в уведомлении — по причине: нет места — освободить; прочий сбой ввода-
    вывода — проверить диск (чистка места умирающему диску не поможет, а админ
    потратит на неё время, пока журнал второй стороны стоит)."""
    duo.journal_on()

    def broken(records):
        raise OSError(err, "disk says no")
    monkeypatch.setattr(duo.b_keep, "append_events", broken)
    duo.edit("Пётр")
    told = [n.text for n in rm.notices(duo.a_to_b(duo.a.reserve_jrn_tick()))]
    assert len(told) == 1 and "не пишется" in told[0], told
    assert advice in told[0] and not_advice not in told[0], told[0]

# ── A19, F4-7: копия базы только когда нужна ─────────────────────────────────

@pytest.fixture()
def copies(monkeypatch):
    calls = []
    real = pkgbot.replica_marked

    def counting(path):
        calls.append(path)
        return real(path)
    monkeypatch.setattr(pkgbot, "replica_marked", counting)
    return calls


def test_the_timer_does_not_copy_the_base_when_nothing_changed_outside_it(duo, copies):
    """Таймер при журнале: база едет событиями — без изменений вне базы снимок не нужен,
    и решается это до копии базы."""
    duo.journal_on()
    duo.edit("Пётр")                                      # меняется только база
    n = len(copies)
    assert duo.a.reserve_pkg_build(force=False) is None
    assert len(copies) == n, "по таймеру без изменений вне базы снята копия базы"
    assert duo.a.reserve_pkg_build(force=True) is not None and len(copies) == n + 1, "контроль: снимок по слову — с копией"


def _button_move_at_pkg(duo) -> int:
    duo.journal_on()
    gen = int(duo.a.rsv_get("rsv_pkg_sent")["gen"])
    duo.a.rsv_update("rsv_pkg_sent", at=_ago(5))          # снимок — до начала переезда
    rm.to_step(duo.a, "pkg")
    duo.a._out_step("pkg", 300, pkg_unchanged=False)
    duo.a.reserve_pkg_mark()
    duo.clock.advance(11)
    return gen


def test_a_button_move_with_the_journal_reuses_the_snapshot_when_nothing_changed(duo, copies):
    """Переезд кнопкой: у резерва снимок и все события подтверждены, вне базы ничего не
    изменилось — полный снимок не собирается, go_req уходит с прежним поколением."""
    gen = _button_move_at_pkg(duo)
    n = len(copies)
    assert duo.a.reserve_pkg_tick() is None
    assert len(copies) == n, "на шаге pkg снята копия базы"
    assert duo.a.rsv_get("rsv_out").get("pkg_unchanged") is True
    acts = duo.a.reserve_out_tick()
    go = [b for k, b in sends(acts) if k == "go_req"]
    assert go and go[0]["gen"] == gen, f"go_req не ушёл с прежним поколением: {acts}"


def test_a_button_move_waits_while_events_are_unacked(duo, copies):
    """Неподтверждённые события — снимок тот же, но go_req ждёт подтверждения."""
    _button_move_at_pkg(duo)
    duo.edit("Пётр")
    duo.a.reserve_pkg_tick()
    assert not sends(duo.a.reserve_out_tick(), "go_req"), "go_req при неподтверждённых правках"
    duo.tick()
    assert sends(duo.a.reserve_out_tick(), "go_req"), "после подтверждения go_req не ушёл"


# ── F4-2: такт журнала в простое ────────────────────────────────────────────

def _sql(db) -> list[str]:
    seen: list[str] = []
    db._connection().set_trace_callback(seen.append)
    return seen


def test_an_idle_journal_tick_reads_neither_events_nor_counts_and_checks_fully_every_check_s(duo):
    """Простой: такт раз в секунду — без чтения событий и счётчиков; полный проход —
    раз в CHECK_S; правка уходит на ближайшем такте, без ожидания страховки."""
    duo.journal_on()
    duo.edit("Пётр")
    duo.tick()
    duo.clock.advance(ship.CHECK_S + 1)
    duo.tick()
    seen = _sql(duo.a.db)
    try:
        for _ in range(5):
            duo.clock.advance(1)
            assert duo.a.reserve_jrn_tick() == []
        idle = list(seen)
        seen.clear()
        duo.clock.advance(ship.CHECK_S)
        duo.a.reserve_jrn_tick()
        full = list(seen)
        seen.clear()
        duo.edit("Павел")
        seen.clear()
        duo.clock.advance(1)
        acts = duo.a.reserve_jrn_tick()
    finally:
        duo.a.db._connection().set_trace_callback(None)
    assert not [s for s in idle if "FROM jrn_events" in s or "FROM jrn_counts" in s], \
        f"такт в простое читает события или счётчики: {[s for s in idle if 'jrn_' in s]}"
    assert [s for s in full if "FROM jrn_events" in s], "страховочный полный проход не случился за CHECK_S"
    assert sends(acts, "jev"), f"правка не ушла на ближайшем такте: {acts}"


# ── F4-4: тело st резерва без чтения диска ──────────────────────────────────

def test_the_keep_view_reads_the_store_files_only_when_they_change(duo, monkeypatch):
    duo.journal_on()
    calls = {"size": 0, "cnt": 0}
    real_size, real_cnt = duo.b_keep.events_size, duo.b_keep.counts_last

    def size():
        calls["size"] += 1
        return real_size()

    def cnt(side):
        calls["cnt"] += 1
        return real_cnt(side)
    monkeypatch.setattr(duo.b_keep, "events_size", size)
    monkeypatch.setattr(duo.b_keep, "counts_last", cnt)
    first = duo.b.reserve_keep_view()
    base = dict(calls)
    for _ in range(5):
        assert duo.b.reserve_keep_view() == first
    assert calls == base, f"файлы хранилища перечитаны без их смены: {base} → {calls}"
    duo.edit("Пётр")
    duo.tick()
    view = duo.b.reserve_keep_view()
    assert calls["size"] > base["size"] and view["size"] > first["size"], "смена хранилища не увидена"


def test_a_big_store_marks_a_snapshot_at_most_once_per_pkg_minutes(duo, monkeypatch):
    """`keep.size` больше мегабайта в каждом st — не рычаг: отметка снимка не чаще раза в
    pkg_minutes."""
    from awgbot.core import settings
    duo.journal_on()
    duo.clock.advance(86400)                              # сервер работает не первые минуты
    marks = []
    monkeypatch.setattr(duo.a, "reserve_pkg_mark", lambda: marks.append(duo.clock.t))
    big = dict(duo.b.reserve_keep_view(), size=ship.SNAP_BYTES + 1)
    minutes = max(1, settings.get_int("app.reserve.pkg_minutes", 30))
    for _ in range(20):
        duo.a.reserve_jrn_keep_in(big)
        duo.clock.advance(5)
    assert len(marks) == 1, f"отметка снимка по размеру — {len(marks)} раз за 100 с"
    duo.clock.advance(minutes * 60)
    duo.a.reserve_jrn_keep_in(big)
    assert len(marks) == 2, "после pkg_minutes отметки нет"


def test_the_json_probe_runs_once_per_process(monkeypatch):
    from awgbot.domain.services.reserve import compat
    from awgbot.util import rsvjrn
    calls = []

    def probe(con):
        calls.append(con)
        return True
    monkeypatch.setattr(rsvjrn, "json_ok", probe)
    for _ in range(5):
        assert compat.journal_capable() is True
    assert len(calls) == 1, f"проба JSON на каждом st: {len(calls)}"

