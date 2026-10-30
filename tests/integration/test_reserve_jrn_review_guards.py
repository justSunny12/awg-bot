"""Журнал резерва — правки по ревью 4.1.0: сторожа и правила, которые решают путь
без пары баз (journal/tail, ship, guestj, epoch, package, session, vote,
outbound, outbound_stuck, tools/rsvbusy, tools/rsvopen, infra/db/jrn).

- ошибка базы (замок дольше busy_timeout, сбой диска) в проверках журнала —
  наружу, а не «хвоста нет» / «журнала нет»; только база без таблиц журнала —
  прежний путь; роль при нечитаемой базе — следование; сторож терминала при
  ошибке базы отказывает (код 3), обход — AWG_RESERVE_IGNORE_WAIT=1;
- «резерв несёт моих после перехода домой» — одним правилом для роли и для
  такта автомата: `st` serving от мёртвого линка (живость down, старый st) не
  уводит автомат в away; зависший вебхук при мёртвом линке снимается по
  down_since (30 мин), а не по старому idle (10 мин);
- аварийное открытие при записи DNS «вручную» (пакет без конверта DNS) —
  отказ в предусловиях резерва и в процессе открытия; кнопка — как прежде;
- время события журнала — с поясом (UTC), а не наивное местное хоста;
- ограждение кворума (держатель здесь без права писать) такт эпохи не снимает;
- смена номера эпохи в следовании — с synchronous = FULL.

Мир — пара из tests/integration/_reserve_move.py, основа и резерв на двух базах
(tests/integration/_reserve_jrn_duo.py), голосование шлюзов
(tests/integration/_reserve_vote_common.py), процесс открытия
(tests/integration/test_rsvopen.py). Ключи — заглушки, адреса — документационные.

Цена ошибки: замок базы, принятый за «хвоста нет», — гость считается осушенным
при живом хвосте и стирается с ним (`--wipe`), а терминал пускает обновление
посреди переноса; принятый за «журнала нет» — основа шлёт pkg2, резерв сносит
хранилище журнала линии; автомат, ушедший в away по флагу TCP-сеанса мёртвого
линка, — эпоха уходит к резерву, который пользователей не несёт, и каждое
действие с профилями получает отказ; аварийное открытие без провайдера DNS —
десять минут без VPN и Telegram и срыв с чужой причиной; наивное время
события на хосте не в UTC+3 — Т-Ж1/Т-Ж2/Т-Ж6 на часы раньше или позже;
такт, снимающий ограждение кворума, — два писателя.
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from awgbot.domain.services.reserve.journal import tail as tailmod
from awgbot.infra.db import Database
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo
from tests.integration._reserve_vote_common import RSV_SHARE, box, gw_sent
from tests.integration.test_rsvopen import DNS
from tests.integration.test_rsvopen import box as open_box

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["box", "open_box"]                            # фикстуры голосования и процесса открытия

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"
LOCKED = "database is locked"


def _locked(*_a, **_k):
    raise sqlite3.OperationalError(LOCKED)


def _old(*_a, **_k):
    raise sqlite3.OperationalError("no such table: jrn_tail")


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


# ── A1: ошибка базы — наружу, «нет таблиц» — прежний путь ───────────────────

def test_a_locked_base_is_not_taken_for_no_own_tail(mv, monkeypatch):
    """Замок базы — не «хвоста нет»: иначе гость считается осушенным при живом хвосте,
    а бот и терминал пускают обновление посреди переноса."""
    svc = mv.svc
    monkeypatch.setattr(svc.db, "jrn_tail_rows", _locked)
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        svc.reserve_tail_pending()
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        tailmod.own_tail_blocks_raw(svc.db)


def test_a_base_without_journal_tables_still_means_no_own_tail(mv, monkeypatch):
    svc = mv.svc
    monkeypatch.setattr(svc.db, "jrn_tail_rows", _old)
    assert svc.reserve_tail_pending() is False, "база старше журнала — хвоста нет, а не ошибка"
    assert tailmod.own_tail_blocks_raw(svc.db) is False


def test_a_guest_with_a_locked_base_is_not_drained(duo, monkeypatch):
    """Гость при замке базы не объявляет «основа подтвердила всё» — иначе released и
    `--wipe` унесли бы неперенесённый хвост."""
    duo.journal_on()
    duo.open_guest(button=False)
    monkeypatch.setattr(duo.gdb, "jrn_tail_rows", _locked)
    with duo.as_guest():
        with pytest.raises(sqlite3.OperationalError, match=LOCKED):
            duo.g.guest_jrn_drained()


def test_a_locked_base_does_not_turn_the_snapshot_into_a_package_without_the_journal(duo, monkeypatch):
    """Пакет при журнале — только pkg3: pkg2 по замку базы резерв принял бы как «журнала
    нет» и стёр хранилище событий линии."""
    duo.journal_on()
    real = duo.a.db.jrn_meta
    monkeypatch.setattr(duo.a.db, "jrn_meta", _locked)
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        duo.a.reserve_pkg_build(force=True)
    monkeypatch.setattr(duo.a.db, "jrn_meta", real)
    built = duo.a.reserve_pkg_build(force=True)
    assert built is not None and built.get("jrn"), "контроль: без замка снимок — pkg3 с частью jrn"


def test_a_base_without_journal_tables_builds_the_old_package(duo, monkeypatch):
    duo.journal_on()
    monkeypatch.setattr(duo.a.db, "jrn_meta", _old)
    built = duo.a.reserve_pkg_build(force=True)
    assert built is not None and not built.get("jrn"), "база старше журнала — прежний пакет без части jrn"


def test_the_capture_switch_lets_a_locked_base_out_and_skips_a_base_without_tables(mv, monkeypatch):
    svc = mv.svc
    svc.__dict__.pop("_jrn_on_last", None)                # направление «сменилось» — база читается
    monkeypatch.setattr(svc.db, "jrn_capture_set", _locked)
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        svc.reserve_jrn_sync()
    monkeypatch.setattr(svc.db, "jrn_capture_set", _old)
    svc.__dict__.pop("_jrn_on_last", None)
    assert svc.reserve_jrn_sync() is None, "база без таблиц журнала роняет такт"


def test_the_guest_journal_switch_lets_a_locked_base_out(mv, monkeypatch):
    from awgbot.core import config
    svc = mv.svc
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(svc.db, "jrn_meta", _old)
    assert svc.guest_jrn_on() is False
    monkeypatch.setattr(svc.db, "jrn_meta", _locked)
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        svc.guest_jrn_on()


def test_the_role_on_an_unreadable_base_is_following_for_this_tick(mv, monkeypatch):
    """Роль не прочитана — на этот такт следование: не писать решения безопаснее, чем
    писать не держателем; база без таблиц журнала — держатель, как прежде."""
    svc = mv.svc
    monkeypatch.setattr(svc.db, "jrn_following", _locked)
    assert svc.reserve_following() is True, "роль при замке базы — держатель: решения пишутся вслепую"
    monkeypatch.setattr(svc.db, "jrn_following", _old)
    assert svc.reserve_following() is False


def test_the_journal_part_of_st_lets_a_locked_base_out(duo, monkeypatch):
    duo.journal_on()
    monkeypatch.setattr(duo.a.db, "jrn_meta", _old)
    assert duo.a._jrn_st_part() == {}
    monkeypatch.setattr(duo.a.db, "jrn_meta", _locked)
    with pytest.raises(sqlite3.OperationalError, match=LOCKED):
        duo.a._jrn_st_part()


# ── сторож терминала ────────────────────────────────────────────────────────

def _busy(capsys) -> tuple[int, str, str]:
    import sys
    from tools import rsvbusy
    saved, sys.argv = sys.argv, ["rsvbusy", "обновление"]   # как зовёт update.sh; argv pytest — не аргумент сторожа
    try:
        code = rsvbusy.main()
    finally:
        sys.argv = saved
    cap = capsys.readouterr()
    return code, cap.out.strip(), cap.err.strip()


def test_the_terminal_guard_refuses_on_a_locked_base(mv, monkeypatch, capsys):
    """Сторож терминала по замку базы не пускает обновление: «хвоста нет» по замку пустило
    бы его посреди переноса; причина — одной строкой с самой ошибкой."""
    monkeypatch.setattr(Database, "jrn_tail_rows", _locked)
    code, out, _err = _busy(capsys)
    assert code == 3, f"обновление пущено при нечитаемой базе: {code} ({out!r})"
    assert "состояние резерва не прочитано" in out and LOCKED in out and "\n" not in out, out


def test_the_terminal_guard_bypass_lets_the_update_go_and_says_so(mv, monkeypatch, capsys):
    """AWG_RESERVE_IGNORE_WAIT=1 — обход ожидания правок бота на резерве: обновление
    идёт, в stderr — что именно может потеряться; без переменной — отказ с подсказкой."""
    mv.svc.rsv_update("rsv_out", await_merge=True)
    monkeypatch.delenv("AWG_RESERVE_IGNORE_WAIT", raising=False)
    code, out, _err = _busy(capsys)
    assert code == 3 and "если не вернётся — удали пару" in out, f"нет подсказки выхода: {out!r}"
    assert "обновить немедленно — AWG_RESERVE_IGNORE_WAIT=1" in out, f"нет подсказки обхода: {out!r}"
    monkeypatch.setenv("AWG_RESERVE_IGNORE_WAIT", "1")
    code, out, err = _busy(capsys)
    assert (code, out) == (0, ""), f"обход не пустил обновление: {code} ({out!r})"
    assert "ожидание снято по AWG_RESERVE_IGNORE_WAIT=1" in err and "изменения с резерва могут быть потеряны" in err, err


def test_the_terminal_guard_bypass_does_not_override_the_users_at_the_reserve(mv, monkeypatch, capsys):
    """Обход — только ожидания правок: пользователи у резерва держат обновление и с ним."""
    monkeypatch.setenv("AWG_RESERVE_IGNORE_WAIT", "1")
    mv.svc.reserve_set_out("away")
    assert _busy(capsys)[0] == 3, "обход ожидания пустил обновление при пользователях у резерва"


@pytest.mark.parametrize("value", ["", "0", "yes"])
def test_the_terminal_guard_bypass_is_only_the_exact_one(mv, monkeypatch, capsys, value):
    mv.svc.rsv_update("rsv_out", await_merge=True)
    monkeypatch.setenv("AWG_RESERVE_IGNORE_WAIT", value)
    assert _busy(capsys)[0] == 3, f"обход сработал на «{value}»"


# ── A2: «резерв несёт моих» — одно правило для роли и для такта ─────────────

def _following(svc) -> bool:
    return svc.db.jrn_following()


@pytest.mark.parametrize("why", ["liveness_down", "st_stale"])
def test_a_serving_st_over_a_dead_link_does_not_move_the_automaton_away(mv, why):
    """st serving, а линк мёртв (живость down) или st получен до потери связи: роль —
    держатель, и такт автомата тоже не уводит в away (прежде такт смотрел только флаг
    TCP-сеанса и забирал эпоху следующим тактом)."""
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving"})
    if why == "liveness_down":
        svc._lv.state = "down"
    else:
        svc.rsv_update("rsv_peer_state", st_stale=True)
    assert svc.reserve_online(), "сценарий не о том: TCP-сеанс жив"
    svc.reserve_epoch_sync()
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home", f"такт увёл автомат в away по мёртвому линку: {acts}"
    assert not _following(svc), "эпоха ушла к резерву по флагу сеанса"
    assert svc.reserve_poll() != "away"


def test_a_serving_st_over_a_live_link_moves_the_automaton_away(mv):
    """Контроль правила: живой резерв несёт моих — такт переводит в away, роль — следование."""
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving"})
    svc._lv.state = "ok"
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "away" and ("live",) in acts, acts
    assert _following(svc)


def test_a_serving_st_after_force_home_does_not_move_the_automaton_away(mv):
    """После «вернуть без резерва» (return_why = force) serving не уводит: решения здесь."""
    svc = mv.svc
    svc.reserve_set_out("home", step="", return_why="force")
    rm.st_in(svc, **{"in": "serving"})
    svc._lv.state = "ok"
    svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and not _following(svc)


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def test_a_stuck_webhook_over_a_dead_link_waits_for_the_link_timeout_not_for_the_old_idle(mv):
    """Дома, вебхук у резерва, запись на мне, последний st — idle, но линк мёртв при живом
    TCP-сеансе: решает down_since (30 мин), а не старое idle (10 мин)."""
    svc = mv.svc
    svc.reserve_set_out("home", step="")
    svc.reserve_set_poll("away")
    mv.dns.auth = [rm.OWN_IP]
    rm.st_in(svc, **{"in": "idle"})
    svc._lv.state = "down"
    svc.rsv_update("rsv_peer_state", down_since=_ago(12))
    for _ in range(3):
        svc.rsv_update("rsv_out", stuck_since=_ago(11))
        mv.clock.advance(169)
        assert ("take_home",) not in svc.reserve_out_tick(), "вебхук снят по idle мёртвого линка через 10 минут"
    svc.rsv_update("rsv_peer_state", down_since=_ago(31))
    mv.clock.advance(169)
    svc.reserve_out_tick()                                 # stuck_since — с этого такта
    svc.rsv_update("rsv_out", stuck_since=_ago(11))
    mv.clock.advance(169)
    assert ("take_home",) in svc.reserve_out_tick(), "резерв не на связи 31 мин — вебхук не снят"


# ── A14: аварийное открытие при записи «вручную» ────────────────────────────

def _code_box_of_the_package(s) -> None:
    """У резерва ящик кода того же поколения `M`, что и пакет (qkeys основы принят):
    без него открытие без K основы упирается в «данных для переезда нет» раньше записи."""
    m_gen = int(s.rsv_get("rsv_pkg_in").get("m_gen") or 1)
    s.rsv_update("rsv_qkeys", peer={"l": "b", "m_gen": m_gen, "x": 1, "c_gen": 1, "cy": "ab" * 16,
                                    "cbox": "00" * 56, "q_gen": 0, "t": 0, "y": "", "qbox": ""})


def test_an_emergency_opening_is_refused_when_the_primary_record_is_manual(box):
    """Пакет без конверта DNS (запись у основы «вручную»): доли шлюзов не открывают —
    записью некому управлять; причина — строкой карточки."""
    pkg = box.s.rsv_get("rsv_pkg_in")
    box.s.rsv_put("rsv_pkg_in", {**pkg, "has_dns": False})
    _code_box_of_the_package(box.s)
    why = box.s.reserve_open_block()
    assert why.startswith("DNS-запись у «") and why.endswith("» настраивается вручную"), why
    assert box.s.reserve_auto_block() == why, "карточка не называет причину"
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=True)
    acts = box.share(1)
    assert box.launched == [], f"процесс открытия запущен при записи «вручную»: {acts}"
    assert box.s.reserve_in() == "idle"
    assert not [b for _, b in gw_sent(acts, "state") if b.get("state") == "serving"], acts


def test_a_button_move_is_not_refused_for_a_manual_record(box):
    """Кнопкой запись меняет основа — ей конверт DNS у резерва не нужен."""
    pkg = box.s.rsv_get("rsv_pkg_in")
    box.s.rsv_put("rsv_pkg_in", {**pkg, "has_dns": False})
    assert box.s.reserve_open_block(need_box=False) == ""


def test_an_emergency_opening_goes_with_the_dns_envelope(box):
    """Контроль: конверт DNS есть (или пакет прежнего выпуска без отметки) — доли открывают."""
    pkg = box.s.rsv_get("rsv_pkg_in")
    box.s.rsv_put("rsv_pkg_in", {**pkg, "has_dns": True, "share": RSV_SHARE})
    assert box.s.reserve_open_block() == ""
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=True)
    box.share(1)
    assert len(box.launched) == 1


def test_the_opening_process_by_points_without_a_provider_fails_with_the_readme_text(open_box):
    """Точки кворума с ящиком (ключ M) и пакет без конверта DNS: процесс открытия не идёт к «done record=skipped»
    (гость без записи и без вебхука), а отказывает словами README."""
    open_box.write_package(dns=None)
    assert open_box.open(**open_box.points(1)) != 0, open_box.out
    from awgbot.infra import rsvzone
    st = rsvzone.open_status()
    assert st["step"] == "fail", st
    assert st["reason"] == "DNS-запись сервера настраивается вручную — аварийный переезд невозможен", st
    assert "set_record" not in open_box.kinds() and not rsvzone.TG_TAKE_FILE.exists()


def test_the_opening_process_by_points_with_a_manual_provider_fails_too(open_box):
    open_box.write_package(dns={**DNS, "provider": ""})
    assert open_box.open(**open_box.points(1)) != 0, open_box.out
    from awgbot.infra import rsvzone
    assert rsvzone.open_status().get("reason", "").startswith("DNS-запись сервера настраивается вручную")


def test_the_opening_process_by_k_without_dns_still_opens(open_box):
    """По K (кнопка) запись меняет основа: без провайдера — как прежде, done без записи."""
    open_box.write_package(dns=None)
    assert open_box.open(k=open_box.k.hex()) == 0, open_box.out
    from awgbot.infra import rsvzone
    assert rsvzone.open_status().get("record") == "skipped"


# ── A15: время события — с поясом ───────────────────────────────────────────

@pytest.mark.parametrize("tz", ["Asia/Yekaterinburg", "America/New_York", "UTC"])
def test_the_event_time_has_a_zone_and_reads_right_on_a_host_outside_utc_plus_3(duo, monkeypatch, tz):
    """Время события пишет SQLite: наивное местное время хоста читалось бы как UTC+3 —
    на хосте в UTC+5 «правка 2 минуты назад» стала бы «2 часа и 2 минуты»."""
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        duo.journal_on()
        cid = duo.edit("Часовой")
        duo.a.db.reset_period_traffic(cid)                # событие держателя над счётчиками — свой INSERT
        rows = duo.a.db._connection().execute("SELECT at, t FROM jrn_events ORDER BY i").fetchall()
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    assert rows and "device_traffic" in {t for _, t in rows}, f"событий решений и счётчиков нет: {rows}"
    for at, _t in rows:
        assert str(at).endswith("+00:00"), f"время события без пояса: {at!r}"
        lag = abs(timeutil.elapsed(timeutil.parse_iso(str(at))))
        assert lag < 120, f"время события {at!r} на хосте {tz} читается со сдвигом {lag:.0f} с"


# ── A20: ограждение кворума ─────────────────────────────────────────────────

def test_the_epoch_tick_does_not_lift_the_quorum_fence(mv):
    """Держатель здесь без права писать (jrn_fence): такт эпохи и автомата его не снимают
    — снимает только jrn_unfence."""
    svc = mv.svc
    assert svc.db.jrn_fence(why="кворум: нет большинства") is not None
    epoch = int(svc.db.jrn_meta()["epoch"])
    for _ in range(3):
        svc.reserve_epoch_sync()
        svc.reserve_epoch_check()
        svc.reserve_out_tick()
    m = svc.db.jrn_meta()
    assert (m["holder"], m["role"], int(m["epoch"])) == ("self", "follower", epoch), \
        f"такт снял ограждение кворума: {m}"
    with pytest.raises(sqlite3.IntegrityError, match="^jrn:"):
        svc.db.create_client("За оградой", 1, _START, _FUTURE, "INV-DUMMY-F")
    assert svc.db.jrn_unfence(why="кворум есть") is not None
    svc.reserve_epoch_sync()
    assert svc.db.jrn_meta()["role"] == "holder"


# ── F1-8: смена номера эпохи в следовании — с FULL ──────────────────────────

def test_the_follower_epoch_change_is_written_with_full_sync(mv):
    """Номер эпохи держателя в следовании — переход эпохи, как остальные: с synchronous =
    FULL (пропажа питания не откатывает его), после — снова NORMAL."""
    db = mv.svc.db
    db.jrn_pass(holder="peer", role="follower", why="тест", epoch=4)
    conn = db._connection()
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        db.jrn_adopt_epoch(6)
    finally:
        conn.set_trace_callback(None)
    assert int(db.jrn_meta()["epoch"]) == 6
    upd = [n for n, s in enumerate(seen) if s.startswith("UPDATE jrn_meta SET epoch")]
    full = [n for n, s in enumerate(seen) if s.replace(" ", "").upper() == "PRAGMASYNCHRONOUS=FULL"]
    normal = [n for n, s in enumerate(seen) if s.replace(" ", "").upper() == "PRAGMASYNCHRONOUS=NORMAL"]
    assert upd and full and full[0] < upd[0], f"эпоха записана без FULL: {seen}"
    assert normal and normal[-1] > upd[0], f"после перехода не вернулся NORMAL: {seen}"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1, "соединение осталось на FULL"


# ── строки журнала: один раз на номер; SQLite без JSON ──────────────────────

def _lines(caplog, part: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if part in r.getMessage()]


def test_a_stale_epoch_is_logged_once_per_epoch(duo, caplog):
    """jstale на каждую пачку меньшей эпохи — строкой журнала один раз на номер эпохи,
    а не раз в секунду."""
    import logging
    duo.journal_on()
    duo.open_guest(button=False)
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        (kind, body), = [m for m in duo.g.guest_jrn_tick() if m[0] == "jev"]
    duo.a.db.jrn_pass(holder="self", role="holder", why="тест", epoch=int(body["e"]) + 3)
    caplog.set_level(logging.INFO)
    for _ in range(4):
        assert rm.sent(duo.a.reserve_jrn_in("jev", dict(body))) == ["jstale"]
    assert len(_lines(caplog, "отклонены (jstale")) == 1, _lines(caplog, "jstale")


def test_a_gap_in_the_holders_events_is_logged_once_per_number(duo, caplog):
    import logging
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        duo.g.guest_jrn_tick()                            # потерялось
        duo.g.db.create_client("Пётр", 1, _START, _FUTURE, "INV-DUMMY-P")
        (kind, body), = [m for m in duo.g.guest_jrn_tick() if m[0] == "jev"]
    caplog.set_level(logging.INFO)
    for _ in range(4):
        assert "jneed" in rm.sent(duo.a.reserve_jrn_in("jev", dict(body)))
    assert len(_lines(caplog, "прошу досылку")) == 1, _lines(caplog, "досылк")


def test_sqlite_without_json_is_logged_once_and_named_as_the_reason(tmp_path, monkeypatch, caplog):
    """Нет JSON-функций в SQLite хоста — журнал правок не пишется: строкой журнала один
    раз (а не молча), и смена пути называет эту причину, а не «у резерва нет журнала»."""
    import logging
    from awgbot.util.rsvjrn import triggers
    path = str(tmp_path / "schema.db")
    d = Database(path)
    d.init_schema()
    d.close()
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {})
    monkeypatch.setattr(triggers, "json_ok", lambda conn: False)
    caplog.set_level(logging.WARNING)
    for _ in range(3):
        con = sqlite3.connect(path)
        try:
            triggers.install(con)
        finally:
            con.close()
    assert len(_lines(caplog, "нет JSON-функций")) == 1, _lines(caplog, "JSON")
    assert triggers.capture_failed() == "в SQLite нет JSON-функций"


def test_the_direction_change_names_sqlite_without_json(duo, monkeypatch, caplog):
    import logging
    from awgbot.domain.services.reserve import compat
    duo.journal_on()
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(compat, "journal_capable", lambda: False)
    duo.a.__dict__.pop("_jrn_caps_cache", None)
    duo.a.reserve_jrn_sync()
    assert duo.a.db.jrn_meta()["capture"] == 0, "условие: захват выключился"
    lines = _lines(caplog, "данные для переезда к «Beta»")
    assert lines and "SQLite без JSON-функций" in lines[-1], lines
