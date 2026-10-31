"""Сторож: в async-функциях awgbot/runtime тяжёлые методы сервисов (база,
reserve.json, настройки, subprocess, файлы) не зовутся прямо в цикле событий —
только через `asyncio.to_thread`.

Разбор по ast: вызов `services.X(...)` / `self.services.X(...)` внутри
`async def` (вложенные синхронные `def`, которые сами уходят в поток, не
считаются), где X — метод Services, в исходнике которого (или на два вызова
вглубь по `self.…()`) есть обращение к базе, хранилищу резерва, настройкам,
subprocess или файлу.

Прямые вызовы, известные сегодня, перечислены с причиной; запись без вызова —
красное (мёртвый пропуск). Новый прямой вызов — красное с file:line.

Цена ошибки: один `awg show` или запрос к базе в цикле событий на каждое
подключение гостя или такт — паузы в ответах бота всем админам сразу, а при
занятой базе (слияние реплики) — зависший цикл до снятия замка.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re

import pytest

from awgbot.domain.services import Services

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = REPO / "awgbot" / "runtime"
BASES = {"services", "svc", "self.services", "self._services", "_services"}
MARKERS = ("self.db.", "rsv_update", "rsv_put", "self.rsv_get", "subprocess", "settings.", "open(")

# (файл относительно awgbot/runtime, async-функция, метод) → почему вызов прямой
ALLOWED: dict[tuple[str, str, str], str] = {
    ("main/client.py", "main", "ensure_admin_client"): "старт процесса до поднятия поллинга — цикл ещё никому не отвечает",
    ("main/client.py", "main", "normalize_update_schedule"): "старт процесса до поднятия поллинга",
    ("main/client.py", "main", "gwlink_sessions_reset"): "старт процесса до поднятия поллинга",
    ("main/client.py", "main", "reserve_session_reset"): "старт процесса до поднятия поллинга",
    ("main/client.py", "main", "reserve_quorum_leader_start"): "старт процесса до поднятия поллинга",
    ("main/client.py", "_after_start", "reserve_pkg_hook_install"): "стартовая задача, один раз; крючок коммита — память",
    ("main/client.py", "_after_start", "reserve_paired"): "стартовая задача, один раз",
    ("main/guest.py", "run_guest", "guest_adopt_snapshot"): "старт гостя до поднятия вебхука",
    ("main/guest.py", "run_guest", "guest_snapshot_hook_install"): "старт гостя до поднятия вебхука",
    ("main/guest.py", "run_guest", "ensure_admin_client"): "старт гостя до поднятия вебхука",
    ("main/guest.py", "run_guest", "gwlink_sessions_reset"): "старт гостя до поднятия вебхука",
    ("main/guest.py", "run_guest", "reserve_quorum_guest_start"): "старт гостя до поднятия вебхука",
    ("main/guest.py", "run_guest", "guest_jrn_on"): "старт и остановка гостя; чтение одной строки меты журнала в памяти",
    ("gwbotme.py", "refresh", "gw_bot_token"): "чтение токена слота — одна строка настроек; на старте и по смене токена",
    ("gwbotme.py", "refresh", "set_gw_bot_identity"): "запись имени бота слота — после getMe, редко",
    ("gwbotme.py", "ensure_all", "gw_bot_identity_missing"): "один запрос на старте",
    ("linkserver/session.py", "_serve", "gwjoin_for_addr"): "приём сессии шлюза: чтение раздела сопряжения",
    ("scheduler/common.py", "notify_update_available", "fleet_release"): "одна строка настроек при уведомлении об обновлении",
}


def _heavy(method: str, depth: int = 0) -> bool:
    try:
        obj = inspect.getattr_static(Services, method)
    except AttributeError:
        return False
    if isinstance(obj, property):
        return False
    try:
        src = inspect.getsource(obj)
    except (OSError, TypeError):
        return False
    if any(m in src for m in MARKERS):
        return True
    if depth < 2:
        return any(_heavy(sub, depth + 1) for sub in set(re.findall(r"self\.([A-Za-z_]\w*)\(", src)) if sub != method)
    return False


def _async_calls(fn: ast.AsyncFunctionDef):
    """Вызовы внутри async-функции без спуска во вложенные синхронные def и lambda
    (они уходят в поток целиком — так устроен монитор планировщика); вложенные
    async def разбираются как свои функции."""
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.Lambda, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.Call):
                yield child
            yield from walk(child)
    yield from walk(fn)


def _hits(root: pathlib.Path = RUNTIME):
    out = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in _async_calls(fn):
                if not isinstance(call.func, ast.Attribute) or ast.unparse(call.func.value) not in BASES:
                    continue
                method = call.func.attr
                if hasattr(Services, method) and _heavy(method):
                    out.append((rel, fn.name, method, call.lineno))
    return out


def test_heavy_service_calls_in_runtime_coroutines_go_through_a_thread():
    bad = [f"awgbot/runtime/{rel}:{line}: {fn}() зовёт services.{m}() в цикле событий"
           for rel, fn, m, line in _hits() if (rel, fn, m) not in ALLOWED]
    assert not bad, "тяжёлый вызов сервисов в цикле событий — оберни asyncio.to_thread:\n" + "\n".join(bad)


def test_every_exception_still_covers_a_real_call():
    hits = {(rel, fn, m) for rel, fn, m, _ in _hits()}
    stale = [k for k in ALLOWED if k not in hits]
    assert not stale, f"исключения без вызовов — убери: {stale}"


def test_the_scan_sees_a_direct_call_and_ignores_a_thread(tmp_path):
    """Сторож сторожа: прямой вызов в async def — находка; тот же метод через
    to_thread или во вложенном sync def — нет."""
    src = (
        "import asyncio\n"
        "async def a(services):\n    return services.reserve_paired()\n"
        "async def b(services):\n    return await asyncio.to_thread(services.reserve_paired)\n"
        "async def c(services):\n"
        "    def inner():\n        return services.reserve_paired()\n"
        "    return await asyncio.to_thread(inner)\n"
    )
    (tmp_path / "probe.py").write_text(src, encoding="utf-8")
    assert [(fn, m) for _, fn, m, _ in _hits(tmp_path)] == [("a", "reserve_paired")]
