"""Сторож `reserve_busy_guard` (install/lib/update.sh) и stderr tools.rsvbusy.

Сторож зовёт `bot_tool tools.rsvbusy` до остановки сервиса. Его stderr — это
строки для человека у терминала: «резерв: база не прочитана — проверку
резерва пропускаю» и «резерв: ожидание снято по AWG_RESERVE_IGNORE_WAIT=1 — …».
Прежде stderr уходил в /dev/null. Действие («обновление» / «восстановление»)
сторож передаёт инструменту аргументом и им же начинает отказ.

Цена ошибки: админ ставит AWG_RESERVE_IGNORE_WAIT=1, обновление идёт, а
предупреждения, что правки с резерва потеряны, он не видел — и не проверяет
профили глазами; при упавшем инструменте (прежний код без tools/rsvbusy.py)
трассировка Python в терминале выглядит как сбой обновления, хотя сторож
по правилу «не помеха»; временный файл на каждый вызов копится в /tmp хоста.

Фрагмент гоняется настоящим bash, bot_tool — функцией-подделкой, TMPDIR —
во временный каталог теста.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from tests.unit.test_reserve_fixes_shell import UPDATE, _func

pytestmark = pytest.mark.unit


_MKTEMP = 'mktemp() { command mktemp "$TMPDIR/tmp.XXXXXX"; }'
_NO_MKTEMP = 'mktemp() { echo "mktemp: failed to create file via template: No space left on device" >&2; return 1; }'


def _guard(tmp_path, tool: str, mktemp: str = _MKTEMP, act: str = "обновление") -> subprocess.CompletedProcess:
    """Вызов сторожа так, как его зовут cmd_update/cmd_restore, под set -euo pipefail;
    mktemp — в свой каталог теста (macOS без шаблона TMPDIR не слушает): по нему
    видно, остался ли файл."""
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    lines = ["set -euo pipefail", "die() { echo \"DIE: $*\"; exit 1; }", f"bot_tool() {{ {tool}; }}",
             mktemp, _func(UPDATE, "reserve_busy_guard"),
             f'if declare -F reserve_busy_guard >/dev/null 2>&1; then reserve_busy_guard "{act}"; fi',
             "echo continued"]
    env = dict(os.environ, TMPDIR=str(tmp))
    return subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, env=env, timeout=10)


_IGNORED = "резерв: ожидание снято по AWG_RESERVE_IGNORE_WAIT=1 — изменения с резерва могут быть потеряны"


def test_the_bypass_warning_reaches_the_terminal_and_the_update_goes_on(tmp_path):
    """Код 0 со строкой в stderr (обход ожидания) — строка у человека, обновление идёт."""
    r = _guard(tmp_path, f'echo "{_IGNORED}" >&2; return 0')
    assert r.returncode == 0 and "continued" in r.stdout, r.stdout + r.stderr
    assert _IGNORED in r.stderr, f"предупреждение обхода заглохло: stderr={r.stderr!r}"


def test_the_refusal_keeps_the_tools_stderr_next_to_the_reason(tmp_path):
    """Код 3: отказ с причиной из stdout, а stderr сторожа — тоже человеку."""
    r = _guard(tmp_path, 'echo "резерв: база не прочитана (замок) — проверку резерва пропускаю" >&2; '
                         'echo "пользователи этого сервера сейчас у резерва «Beta» (away)"; return 3')
    assert r.returncode != 0 and "continued" not in r.stdout, "обновление пошло при коде 3"
    assert "DIE: обновление сейчас невозможно: пользователи этого сервера сейчас у резерва «Beta» (away)" in r.stdout, \
        r.stdout
    assert "резерв: база не прочитана (замок) — проверку резерва пропускаю" in r.stderr, \
        f"stderr сторожа пропал: {r.stderr!r}"


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_a_failing_tool_stays_silent_and_does_not_stop_the_update(tmp_path, rc):
    """Прочий код (нет инструмента, упал) — не помеха и без трассировки в терминале."""
    r = _guard(tmp_path, f'echo "Traceback (most recent call last): ModuleNotFoundError" >&2; return {rc}')
    assert r.returncode == 0 and "continued" in r.stdout, r.stdout + r.stderr
    assert "Traceback" not in r.stderr, f"трассировка упавшего инструмента у человека: {r.stderr!r}"


@pytest.mark.parametrize("tool", ['echo "x" >&2; return 0', "return 0", 'echo "x" >&2; return 1'],
                         ids=["stderr-free", "silent-free", "failed"])
def test_no_temporary_file_is_left_after_a_free_or_failed_check(tmp_path, tool):
    r = _guard(tmp_path, tool)
    assert r.returncode == 0, r.stdout + r.stderr
    left = sorted(p.name for p in (tmp_path / "tmp").iterdir())
    assert left == [], f"временный файл сторожа остался: {left}"


def test_no_temporary_file_is_left_after_a_refusal(tmp_path):
    """Отказ (die) — тоже без следа: rm до die."""
    r = _guard(tmp_path, 'echo "x" >&2; echo "занят"; return 3')
    assert r.returncode != 0, r.stdout
    left = sorted(p.name for p in (tmp_path / "tmp").iterdir())
    assert left == [], f"временный файл сторожа остался после отказа: {left}"


def test_an_empty_stderr_adds_nothing(tmp_path):
    """Свободно и молча — в stderr ничего (не пустая строка, не «cat: …»)."""
    r = _guard(tmp_path, "return 0")
    assert r.returncode == 0 and r.stderr == "", f"лишнее в stderr: {r.stderr!r}"


# ── нет временного файла (каталог временных файлов полон или только для чтения) ──

def test_without_a_temporary_file_a_free_server_still_updates(tmp_path):
    """mktemp не смог — это не повод ронять обновление свободного сервера: прежде
    сторож падал только кодом 3. Под set -e упавшая подстановка в функции рвёт
    всю команду без строки «DIE: …» — человек видит одно «mktemp: …»."""
    r = _guard(tmp_path, "return 0", mktemp=_NO_MKTEMP)
    assert r.returncode == 0 and "continued" in r.stdout, \
        f"обновление свободного сервера оборвано из-за mktemp: rc={r.returncode} {r.stdout!r} {r.stderr!r}"


def test_without_a_temporary_file_a_busy_server_is_refused_with_its_reason(tmp_path):
    """Занят — отказ с причиной, как и с файлом: без неё админ не знает, что ждать возврата."""
    r = _guard(tmp_path, 'echo "пользователи этого сервера сейчас у резерва «Beta» (away)"; return 3',
               mktemp=_NO_MKTEMP)
    assert r.returncode != 0 and "continued" not in r.stdout, "обновление пошло при коде 3"
    assert "DIE: обновление сейчас невозможно: пользователи этого сервера сейчас у резерва «Beta» (away)" in r.stdout, \
        f"отказ без причины: {r.stdout!r} {r.stderr!r}"


# ── действие словом: от вызывающего — до инструмента и в отказ ──────────────

@pytest.mark.parametrize("act", ["обновление", "восстановление"])
def test_the_action_reaches_the_tool_as_its_argument(tmp_path, act):
    """bot_tool получает `tools.rsvbusy <действие>`: без аргумента инструмент говорит
    «обновление станет возможным … обновить немедленно» и человеку, запустившему
    восстановление, — подсказка не про его команду."""
    got = tmp_path / "argv"
    r = _guard(tmp_path, f'printf "%s\\n" "$@" > "{got}"; return 0', act=act)
    assert r.returncode == 0 and "continued" in r.stdout, r.stdout + r.stderr
    assert got.read_text(encoding="utf-8").splitlines() == ["tools.rsvbusy", act], \
        f"инструменту дошли не те аргументы: {got.read_text(encoding='utf-8')!r}"


@pytest.mark.parametrize("act", ["обновление", "восстановление"])
def test_the_refusal_starts_with_the_action_and_says_impossible_now(tmp_path, act):
    """Отказ — «<действие> сейчас невозможно: <причина>», причина из stdout — как есть."""
    why = f"Жду изменения от бота на «Beta» — {act} станет возможным после их получения"
    r = _guard(tmp_path, f'echo "{why}"; return 3', act=act)
    assert r.returncode != 0 and "continued" not in r.stdout, f"{act} пошло при коде 3"
    assert r.stdout.strip() == f"DIE: {act} сейчас невозможно: {why}", r.stdout
