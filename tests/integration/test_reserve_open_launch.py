"""Запуск процесса открытия на резерве (domain/services/reserve/inbound.py
`_rsvopen_launch`, `_rsvopen_exit`, `_tick_taking`): хранилище гостя бот
монтирует сам — в пространстве хоста и до запуска юнита; каталоги из
ReadWritePaths создаются заранее и помечены «-»; песочница — ProtectSystem=strict,
каталог данных резерва закрыт временной ФС, виден только пакет второй
стороны; stderr процесса — в STATE_DIR/rsvopen.err (0600), и его хвост идёт в
причину, если процесс завершился, не дойдя до итога.

Зона, systemd-run и Popen подставлены (фикстура `host` из test_reserve_hosting):
тест решает, чем кончилось монтирование и с каким кодом вышел процесс.

Цена ошибки: tmpfs, смонтированный внутри юнита, хосту не виден — гость
стартует на пустом каталоге, переезд «прошёл», а бота основы нет; путь в
ReadWritePaths без «-», которого ещё нет, — юнит падает кодом 226 до первой
строки процесса, и резерв ждёт итога 35 минут, пока основа считает переезд
идущим; база резерва, видимая процессу, — данные второй стороны рядом с
чужим токеном; процесс, убитый OOM, без проверки кода — те же 35 минут
тишины; проверка кода после `done` — уже обслуживающая зона снимается
из-за завершившегося, как положено, процесса.
"""
from __future__ import annotations

import logging
import os
import stat

import pytest

from awgbot.core import config
from awgbot.infra import rsvzone

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent
from tests.integration.test_reserve_hosting import K_HEX, _taking, host, stdin_lines

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]


def _props(cmd: list[str]) -> dict[str, list[str]]:
    """Свойства `-p KEY=VALUE` команды systemd-run: ключ → все значения."""
    out: dict[str, list[str]] = {}
    for i, c in enumerate(cmd[:-1]):
        if c == "-p":
            k, _, v = cmd[i + 1].partition("=")
            out.setdefault(k, []).append(v)
    return out


# ── запуск: хранилище, каталоги, песочница ──────────────────────────────────

def test_the_store_is_mounted_by_the_bot_before_the_open_process_starts(host):
    _taking(host)
    assert host.zone.events == ["store", "popen"], \
        f"хранилище смонтировано не до юнита — внутри него tmpfs хосту не виден: {host.zone.events}"


def test_the_writable_dirs_exist_before_the_unit_starts(host):
    """ReadWritePaths с несуществующим путём роняет юнит кодом 226 — каталоги
    создаются до запуска, с правами только для владельца."""
    seen = {}
    real = host.spawn.Popen

    def popen(cmd, **kw):
        seen.update({str(d): d.is_dir() for d in (rsvzone.VAR_DIR, rsvzone.STATE_DIR)})
        return real(cmd, **kw)
    host.spawn.Popen = popen
    assert not rsvzone.VAR_DIR.exists(), "предусловие: каталога ещё нет"
    _taking(host)
    assert seen and all(seen.values()), f"на момент запуска каталогов нет: {seen}"
    assert stat.S_IMODE(rsvzone.VAR_DIR.stat().st_mode) == 0o700, oct(rsvzone.VAR_DIR.stat().st_mode)


def test_a_store_that_does_not_mount_refuses_the_go_and_launches_nothing(host):
    host.zone.fail_on["store"] = "нужен root"
    why = host.svc.reserve_go_req_in(7, "button")
    assert "хранилище гостя не смонтировано" in why and "нужен root" in why, why
    assert not host.spawn.popen, "процесс открытия запущен без хранилища — гость стартует на пустом каталоге"
    assert host.svc.reserve_in() == "idle", "зона осталась занятой отказом монтирования"


def test_a_state_dir_that_cannot_be_made_refuses_the_go(host, monkeypatch, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("DUMMY")
    monkeypatch.setattr(rsvzone, "STATE_DIR", blocker / "rsv")
    why = host.svc.reserve_go_req_in(7, "button")
    assert "не создан" in why and str(blocker / "rsv") in why, why
    assert not host.spawn.popen and host.svc.reserve_in() == "idle"


def test_the_unit_is_sandboxed_strictly_with_optional_writable_paths(host):
    _taking(host)
    p = _props(host.spawn.popen[0]["cmd"])
    assert p["ProtectSystem"] == ["strict"], p.get("ProtectSystem")
    rw = p["ReadWritePaths"][0].split()
    want = [f"-{rsvzone.RUN_DIR}", f"-{rsvzone.VAR_DIR}", f"-{rsvzone.STATE_DIR}", "-/etc/systemd", "-/run/netns"]
    assert rw == want, f"пути записи юнита: {rw}"
    assert all(x.startswith("-") for x in rw), "путь без «-», которого нет, — юнит падает кодом 226"
    data = str(config.DATA_DIR)
    assert p["TemporaryFileSystem"] == [f"{data}:ro"], "каталог данных резерва (база, WAL, стороны линков) открыт процессу"
    assert p["BindReadOnlyPaths"] == [f"-{data}/reserve/guest.pkg -{data}/reserve/jrn"], \
        f"процессу видны только пакет и журнал второй стороны на чтение: {p.get('BindReadOnlyPaths')}"
    assert not any(v.endswith("bot.db") for v in p.get("InaccessiblePaths", [])), \
        "прежнее закрытие одной базы оставляет открытыми её WAL и копии"


def test_the_stderr_of_the_open_process_goes_to_a_private_file(host):
    _taking(host)
    fd = host.spawn.popen[0]["kw"]["stderr"]
    assert isinstance(fd, int) and fd != host.spawn.DEVNULL, f"stderr процесса уходит в никуда: {fd!r}"
    err = rsvzone.STATE_DIR / "rsvopen.err"
    assert err.is_file(), "файла stderr процесса открытия нет"
    assert stat.S_IMODE(err.stat().st_mode) == 0o600, oct(err.stat().st_mode)
    with pytest.raises(OSError):
        os.fstat(fd)                                       # дескриптор закрыт у бота после запуска


def test_the_stderr_file_from_the_previous_opening_is_truncated(host):
    rsvzone.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (rsvzone.STATE_DIR / "rsvopen.err").write_text("старая ошибка прошлого открытия\n")
    _taking(host)
    assert (rsvzone.STATE_DIR / "rsvopen.err").read_text() == "", \
        "хвост прошлого открытия попадёт в причину этого"


# ── stdin процесса: вход первой строкой, конверт второй ─────────────────────

def test_the_launch_with_await_k_keeps_the_pipe_open_and_the_plain_launch_closes_it(host):
    host.svc._rsvopen_launch({"await_k": True}, keep_stdin=True)
    run = host.spawn.popen[-1]
    assert stdin_lines(run) == ['{"await_k": true}'] and run.get("flushed") and not run.get("closed"), \
        "труба закрыта до конверта — процесс получит EOF вместо ключа"
    host.svc._rsvopen_launch({"k": K_HEX})
    run = host.spawn.popen[-1]
    assert run.get("closed"), "без ожидания конверта труба обязана закрыться сразу: процесс читает вход до EOF"


def test_the_second_line_is_written_once_and_closes_the_pipe(host):
    svc = host.svc
    svc._rsvopen_launch({"await_k": True}, keep_stdin=True)
    run = host.spawn.popen[-1]
    assert svc._rsvopen_send({"sealed": "ab" * 10}) == ""
    assert stdin_lines(run) == ['{"await_k": true}', '{"sealed": "abababababababababab"}'] and run.get("closed")
    assert "не ждёт" in svc._rsvopen_send({"sealed": "cd" * 10}), "вторая запись в закрытую трубу не отклонена"
    assert len(stdin_lines(run)) == 2


def test_the_second_line_is_refused_when_no_process_waits_for_it(host):
    svc = host.svc
    assert "не ждёт" in svc._rsvopen_send({"sealed": "ab" * 10}), "конверт без процесса"
    svc._rsvopen_launch({"k": K_HEX})
    assert "не ждёт" in svc._rsvopen_send({"sealed": "ab" * 10}), "процесс с ключом во входе конверта не ждёт"
    assert len(stdin_lines(host.spawn.popen[-1])) == 1


def test_a_broken_pipe_on_the_second_line_is_a_reason_not_an_exception(host, caplog):
    """Процесс открытия умер до второй строки: причина уходит основе и админу
    по-русски, без английского текста ОС; сам текст ОС — в журнал, иначе
    разбирать сбой нечем."""
    caplog.set_level(logging.DEBUG)
    svc = host.svc
    svc._rsvopen_launch({"await_k": True}, keep_stdin=True)
    host.spawn.stdin_error = BrokenPipeError(32, "Broken pipe")
    why = svc._rsvopen_send({"sealed": "ab" * 10})
    assert why == "ключ процессу открытия не передан — процесс уже завершился", why
    assert "Broken pipe" not in why and "Errno" not in why, f"текст ОС в причине: {why}"
    assert "Broken pipe" in caplog.text and "не передан" in caplog.text, f"текста ОС нет в журнале: {caplog.text}"


# ── код завершения: такт ────────────────────────────────────────────────────

def _err(*lines: str) -> None:
    (rsvzone.STATE_DIR / "rsvopen.err").write_text("".join(f"{ln}\n" for ln in lines), encoding="utf-8")


@pytest.mark.parametrize("step", ["launched", "clone_up", "record_set"])
def test_a_process_that_exited_without_a_result_ends_the_hosting_at_once_with_the_code_and_the_tail(host, step):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": step}
    _err("Traceback (most recent call last):", "  File x", "MemoryError")
    host.spawn.rc = 226
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle", "процесс мёртв, а резерв ждёт итога 35 минут"
    assert host.zone.names()[-2:] == ["teardown", "wipe"], "данные основы остались в хранилище"
    down = [a[2]["reason"] for a in acts if a[:2] == ("send", "down")]
    assert down and "код 226" in down[0], down
    text = notices(acts)[0].text
    assert "процесс открытия завершился без итога (код 226)" in text, text
    assert "Traceback (most recent call last): · File x · MemoryError" in text, f"хвоста stderr нет в причине: {text}"


def test_the_tail_is_the_last_three_lines_at_most_200_chars(host):
    svc = host.svc
    _taking(host)
    _err("первая", "вторая", "", "третья", "x" * 400)
    host.spawn.rc = 1
    acts = svc.reserve_in_tick()
    reason = svc.rsv_get("rsv_in").get("reason") or ""
    assert "первая" not in reason and "вторая" not in reason, f"в причине больше трёх строк: {reason}"
    tail = reason.split(": ", 1)[1]
    assert tail.startswith("третья · x") and len(tail) == 200, f"хвост {len(tail)} символов: {tail[:40]}…"
    assert sent(acts).count("down") == 1


def test_an_empty_stderr_gives_the_code_without_a_tail(host):
    _taking(host)
    host.spawn.rc = 137
    host.svc.reserve_in_tick()
    assert host.svc.rsv_get("rsv_in").get("reason") == "процесс открытия завершился без итога (код 137)"


def test_a_process_that_exited_zero_without_done_is_still_without_a_result(host):
    """Код 0, а шага done нет — итога всё равно нет: ждать его некому."""
    _taking(host)
    host.zone.status = {"step": "clone_up"}
    host.spawn.rc = 0
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "idle" and "(код 0)" in host.svc.rsv_get("rsv_in").get("reason", "")


def test_a_running_process_changes_nothing(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "clone_up"}
    host.spawn.rc = None
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "taking" and "down" not in sent(acts)
    assert "teardown" not in host.zone.names()


def test_after_done_the_exit_code_is_not_looked_at(host):
    """Процесс дошёл до done и вышел (любым кодом) — зона уже обслуживает,
    снимать её из-за завершения процесса нельзя."""
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "done"}
    host.spawn.rc = 1
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "taking" and "down" not in sent(acts), "обслуживающая зона снята по коду процесса"
    assert host.zone.names() == ["serve"], host.zone.names()


def test_the_fail_step_wins_over_the_exit_code(host):
    """Процесс сам сказал fail и вышел с кодом 1: причина — его, не «без итога»."""
    _taking(host)
    host.zone.status = {"step": "fail", "reason": "доли не сошлись"}
    host.spawn.rc = 1
    acts = host.svc.reserve_in_tick()
    assert [a[2]["reason"] for a in acts if a[:2] == ("send", "down")] == ["доли не сошлись"]


def test_the_exit_is_reported_once(host):
    _taking(host)
    host.spawn.rc = 1
    assert "down" in sent(host.svc.reserve_in_tick())
    assert host.svc.reserve_in_tick() == [], "отказ по коду процесса повторяется на каждом такте"


def test_after_a_bot_restart_the_exit_code_is_unknown_and_the_timeout_rules(host):
    """Бот перезапустился — процесса в памяти нет: код неизвестен, такт ждёт
    итога до срока, а не отказывает сразу."""
    _taking(host)
    host.svc.__dict__["_rsvopen_proc"] = None
    host.zone.status = {"step": "clone_up"}
    assert host.svc._rsvopen_exit() == (None, "")
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "taking"
