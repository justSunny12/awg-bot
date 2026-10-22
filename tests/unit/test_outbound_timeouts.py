"""Unit: сторож правила «сетевой запрос наружу — не дольше 10 секунд».

Проходит по awgbot/**/*.py и по awg-bot.sh, install/*.sh, install/lib/*.sh:
у каждого вызова наружу (urlopen, create_connection, smtplib.SMTP,
IMAP4_SSL, aiohttp.ClientTimeout, curl) числовой предел должен быть ≤ 10 с,
а у curl-скачивания без --max-time — --connect-timeout ≤ 5 и
--speed-time ≤ 10. Локальные subprocess (systemctl, awg, ip, nft, свои
скрипты) под правило не попадают — сторож их не трогает.

Цена ошибки: один вызов с длинным пределом — и при вялом аплинке кнопка бота,
такт монитора или установка висят минуту, человек жмёт повторно, уведомления
копятся. Исключения — по месту, с объяснением; исключение, которому больше
нечего прикрывать, — красное.

Вторая половина — точечные проверки значений этапа: константы модулей,
умолчания сигнатур и то, что одноразовые Bot(...) для getMe идут через
RetryingSession с тем же пределом.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
import shlex
from typing import Optional

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
PY_FILES = sorted((REPO / "awgbot").rglob("*.py"))
SH_FILES = ([REPO / "awg-bot.sh"] + sorted((REPO / "install").glob("*.sh"))
            + sorted((REPO / "install" / "lib").glob("*.sh")))

LIMIT = 10                       # с — предел запроса наружу
CURL_CONNECT_LIMIT = 5           # с — --connect-timeout у скачиваний без --max-time
CURL_SPEED_TIME_LIMIT = 10       # с — --speed-time: столько секунд без данных — обрыв

# Вызовы наружу: имя функции/класса → ключевой аргумент с пределом и позиция
# того же аргумента (create_connection принимает timeout вторым позиционным).
NET_CALLS: dict[str, tuple[str, Optional[int]]] = {
    "urlopen": ("timeout", None),
    "create_connection": ("timeout", 1),
    "SMTP": ("timeout", None),
    "SMTP_SSL": ("timeout", None),
    "IMAP4_SSL": ("timeout", None),
    "SMTP4": ("timeout", None),          # обёртки net4 «только IPv4» — тот же выход наружу
    "IMAP4_SSL4": ("timeout", None),
    "ClientTimeout": ("total", None),
}
# Свои обёртки над сетью: предел по умолчанию ловится у вызова наружу внутри
# них, а здесь — явный timeout= у вызывающего (feeds.py когда-то передавал 60).
WRAPPERS = {"fetch", "check_imap", "check_smtp", "send_mail", "http"}

# (файл, вызов, кусок строки) → почему предел не выводится из исходника
ALLOWED_PY: dict[tuple[str, str, str], str] = {
    ("awgbot/runtime/tgsession.py", "ClientTimeout", "total=float(total)"):
        "предел — TOTAL_S (test_the_default_total_limit_is_ten_seconds) либо явный "
        "timeout aiogram для long polling getUpdates: единственное законное исключение",
}
# (файл, кусок строки) → почему это не вызов curl
ALLOWED_SH: dict[tuple[str, str], str] = {
    ("install/awg-bot-install.sh", "curl -fsSL https://raw.githubusercontent.com/$REPO/main/install/awg-bot-install.sh | sudo bash\""):
        "текст подсказки в die — команда для человека, не вызов",
}


# ─────────────────────────────────────────────────────────────────────────────
# Python: разбор ast
# ─────────────────────────────────────────────────────────────────────────────

def _callee(node: ast.Call) -> Optional[str]:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _num(node: ast.AST) -> Optional[float]:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    return None


def _module_consts(tree: ast.Module) -> dict[str, float]:
    """Числовые константы уровня модуля: NAME = 10."""
    out: dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = _num(node.value)
            if v is not None:
                out[node.targets[0].id] = v
    return out


def _param_default(fn: ast.AST, name: str, consts: dict[str, float]) -> Optional[float]:
    """Умолчание параметра name у функции fn (позиционного или keyword-only):
    число либо константа модуля (`timeout: float = TIMEOUT`)."""
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    a = fn.args
    pos = a.posonlyargs + a.args
    pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + \
        [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
    for arg, default in pairs:
        if arg.arg == name:
            v = _num(default)
            if v is None and isinstance(default, ast.Name):
                v = consts.get(default.id)
            return v
    return None


def _resolve(node: ast.AST, consts: dict[str, float], fn: Optional[ast.AST]) -> Optional[float]:
    """Число из выражения: литерал, константа модуля, умолчание параметра
    функции, float(...)/int(...) от них. None — не вывести."""
    v = _num(node)
    if v is not None:
        return v
    if isinstance(node, ast.Name):
        d = _param_default(fn, node.id, consts) if fn is not None else None
        return d if d is not None else consts.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("float", "int") \
            and len(node.args) == 1:
        return _resolve(node.args[0], consts, fn)
    return None


def _calls(tree: ast.Module):
    """(вызов, объемлющая функция) — обход с запоминанием функции."""
    def walk(node: ast.AST, fn: Optional[ast.AST]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child)
                continue
            if isinstance(child, ast.Call):
                yield child, fn
            yield from walk(child, fn)
    yield from walk(tree, None)


def _py_hits(path: pathlib.Path, rel: str):
    """Находки одного файла: (rel, вызов, строка исходника, номер, текст
    беды, число|None). Пустой список — файл чист."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = _module_consts(tree)
    lines = src.splitlines()
    out = []
    for call, fn in _calls(tree):
        name = _callee(call)
        if name is None:
            continue
        line = lines[call.lineno - 1].strip()
        if name in NET_CALLS:
            kw, pos = NET_CALLS[name]
            arg = next((k.value for k in call.keywords if k.arg == kw), None)
            if arg is None and pos is not None and len(call.args) > pos:
                arg = call.args[pos]
            if arg is None:
                out.append((rel, name, line, call.lineno, "без предела — ждать можно вечно", None))
                continue
            v = _resolve(arg, consts, fn)
            if v is None:
                out.append((rel, name, line, call.lineno, "предел не вывести из исходника", None))
            elif v > LIMIT:
                out.append((rel, name, line, call.lineno, f"таймаут наружу {v:g} с > {LIMIT}", v))
        elif name in WRAPPERS:
            arg = next((k.value for k in call.keywords if k.arg == "timeout"), None)
            if arg is None:
                continue
            v = _resolve(arg, consts, fn)
            if v is not None and v > LIMIT:
                out.append((rel, name, line, call.lineno, f"таймаут наружу {v:g} с > {LIMIT}", v))
    return out


def _all_py_hits():
    out = []
    for path in PY_FILES:
        out += _py_hits(path, path.relative_to(REPO).as_posix())
    return out


def _py_allowed(rel: str, name: str, line: str) -> bool:
    return any(rel == f and name == n and part in line for (f, n, part) in ALLOWED_PY)


def test_every_outbound_call_in_python_is_limited_to_ten_seconds():
    """Каждый urlopen/create_connection/SMTP/IMAP4_SSL/ClientTimeout и каждый
    явный timeout= у своих обёрток — ≤ 10 с. Сообщение — «файл:строка:
    таймаут наружу N с > 10»."""
    bad = [f"{rel}:{line}: {why} — {name}: {src[:100]}"
           for rel, name, src, line, why, _ in _all_py_hits() if not _py_allowed(rel, name, src)]
    assert not bad, "запросы наружу дольше правила:\n" + "\n".join(bad)


def test_every_python_exception_still_covers_a_real_call():
    """Исключение, которому больше нечего прикрывать, — мёртвый пропуск: под
    него молча подпадёт новый вызов. Переписали место — убери строку."""
    hits = _all_py_hits()
    stale = [key for key in ALLOWED_PY
             if not any(rel == key[0] and name == key[1] and key[2] in src for rel, name, src, *_ in hits)]
    assert not stale, f"исключения без вызовов: {stale}"


def test_the_python_scan_actually_sees_the_calls(tmp_path):
    """Сторож самого сторожа: литерал 30, константа модуля, умолчание
    параметра, float(константа), отсутствие timeout и чужое имя — на
    пробном файле; иначе зелёный прогон ничего не значит."""
    src = (
        "import urllib.request, socket, smtplib, aiohttp\n"
        "SLOW = 20\n"
        "def a(req):\n    return urllib.request.urlopen(req, timeout=30)\n"
        "def b(req):\n    return urllib.request.urlopen(req, timeout=SLOW)\n"
        "def c(req, timeout=15):\n    return urllib.request.urlopen(req, timeout=timeout)\n"
        "def d():\n    return socket.create_connection(('h', 1), 3)\n"
        "def e():\n    return smtplib.SMTP('h', 25)\n"
        "def f():\n    return aiohttp.ClientTimeout(total=float(SLOW))\n"
        "def g(x):\n    return subprocess.run(x, timeout=600)\n"
        "def h():\n    return routing.fetch('u', timeout=60)\n"
        "def i():\n    return routing.fetch('u')\n"
        "def j(req, *, timeout=SLOW):\n    return urllib.request.urlopen(req, timeout=timeout)\n"
    )
    tmp = tmp_path / "probe.py"
    tmp.write_text(src, encoding="utf-8")
    hits = {(name, line, v) for _, name, _, line, _, v in _py_hits(tmp, "probe.py")}
    assert ("urlopen", 4, 30.0) in hits, "литерал 30 не замечен"
    assert ("urlopen", 6, 20.0) in hits, "константа модуля не разрешилась"
    assert ("urlopen", 8, 15.0) in hits, "умолчание параметра функции не разрешилось"
    assert ("SMTP", 12, None) in hits, "SMTP без timeout прошёл как ограниченный"
    assert ("ClientTimeout", 14, 20.0) in hits, "float(константа) не разрешилось"
    assert ("fetch", 18, 60.0) in hits, "явный timeout=60 у обёртки не замечен"
    assert ("urlopen", 22, 20.0) in hits, "умолчание параметра константой модуля не разрешилось"
    assert not any(n == "create_connection" for n, *_ in hits), "create_connection с 3 с позиционно — ложная тревога"
    assert not any(n == "run" for n, *_ in hits), "локальный subprocess попал под правило"
    assert not any(line == 20 for _, line, _ in hits), "обёртка без явного timeout — ложная тревога"


def test_the_python_scan_sees_net4_urlopen_as_an_outbound_call(tmp_path):
    """`net4.urlopen` — тот же выход наружу, что urllib: вызов с пределом
    больше правила и вызов без предела должны быть замечены, иначе перевод
    проекта на IPv4-опенер вывел бы все HTTP-запросы из-под сторожа."""
    src = (
        "from awgbot.util import net4\n"
        "def a(req):\n    return net4.urlopen(req, timeout=30)\n"
        "def b(req):\n    return net4.urlopen(req)\n"
        "def c(req):\n    return net4.urlopen(req, timeout=5)\n"
    )
    tmp = tmp_path / "probe.py"
    tmp.write_text(src, encoding="utf-8")
    hits = {(name, line, v) for _, name, _, line, _, v in _py_hits(tmp, "probe.py")}
    assert ("urlopen", 3, 30.0) in hits, "net4.urlopen с пределом 30 не замечен"
    assert ("urlopen", 5, None) in hits, "net4.urlopen без timeout прошёл как ограниченный"
    assert not any(line == 7 for _, line, _ in hits), "net4.urlopen с 5 с — ложная тревога"


def test_the_python_scan_sees_net4_mail_classes_as_outbound_calls(tmp_path):
    """`net4.SMTP4` и `net4.IMAP4_SSL4` — те же smtplib/imaplib наружу: без
    timeout или с пределом больше правила должны быть замечены, иначе почта
    выпала бы из-под сторожа при переводе на IPv4-соединение."""
    src = (
        "from awgbot.util import net4\n"
        "def a(h):\n    return net4.SMTP4(h, 587)\n"
        "def b(h):\n    return net4.IMAP4_SSL4(h, 993, timeout=30)\n"
        "def c(h):\n    return net4.IMAP4_SSL4(h, 993, timeout=10)\n"
    )
    tmp = tmp_path / "probe.py"
    tmp.write_text(src, encoding="utf-8")
    hits = {(name, line, v) for _, name, _, line, _, v in _py_hits(tmp, "probe.py")}
    assert ("SMTP4", 3, None) in hits, "net4.SMTP4 без timeout прошёл как ограниченный"
    assert ("IMAP4_SSL4", 5, 30.0) in hits, "net4.IMAP4_SSL4 с пределом 30 не замечен"
    assert not any(line == 7 for _, line, _ in hits), "net4.IMAP4_SSL4 с 10 с — ложная тревога"


# ─────────────────────────────────────────────────────────────────────────────
# Shell: curl
# ─────────────────────────────────────────────────────────────────────────────

_CURL = re.compile(r"\bcurl\s+-")


def _logical_lines(text: str):
    """(номер первой строки, склеенная строка): продолжения через «\\» —
    одна команда, опции curl могут стоять на следующей строке."""
    buf, start = [], 0
    for i, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = i
        buf.append(raw.rstrip())
        if raw.rstrip().endswith("\\"):
            buf[-1] = buf[-1][:-1]
            continue
        yield start, " ".join(buf)
        buf = []
    if buf:
        yield start, " ".join(buf)


def _curl_opts(line: str) -> dict[str, float]:
    """Числовые пределы curl из строки: max-time, connect-timeout, speed-time."""
    try:
        words = shlex.split(line[_CURL.search(line).start():], posix=True)
    except ValueError:
        words = line[_CURL.search(line).start():].split()
    out: dict[str, float] = {}
    names = {"--max-time": "max-time", "-m": "max-time", "--connect-timeout": "connect-timeout",
             "--speed-time": "speed-time", "-y": "speed-time"}
    for w, nxt in zip(words, words[1:] + [""]):
        if w in names:
            try:
                out[names[w]] = float(nxt)
            except ValueError:
                pass
    return out


def _sh_hits(path: pathlib.Path, rel: str):
    """Находки одного скрипта: (rel, строка, номер, текст беды)."""
    out = []
    for lineno, line in _logical_lines(path.read_text(encoding="utf-8")):
        if line.lstrip().startswith("#") or not _CURL.search(line):
            continue
        opts = _curl_opts(line)
        if "max-time" in opts:
            if opts["max-time"] > LIMIT:
                out.append((rel, line.strip(), lineno, f"таймаут наружу {opts['max-time']:g} с > {LIMIT}"))
            continue
        if "connect-timeout" not in opts or "speed-time" not in opts:
            out.append((rel, line.strip(), lineno,
                        "без предела: нужен --max-time ≤ 10 или --connect-timeout ≤ 5 с --speed-time ≤ 10"))
            continue
        if opts["connect-timeout"] > CURL_CONNECT_LIMIT:
            out.append((rel, line.strip(), lineno,
                        f"таймаут наружу {opts['connect-timeout']:g} с > {CURL_CONNECT_LIMIT} (--connect-timeout)"))
        if opts["speed-time"] > CURL_SPEED_TIME_LIMIT:
            out.append((rel, line.strip(), lineno,
                        f"таймаут наружу {opts['speed-time']:g} с > {CURL_SPEED_TIME_LIMIT} (--speed-time)"))
    return out


def _all_sh_hits():
    out = []
    for path in SH_FILES:
        out += _sh_hits(path, path.relative_to(REPO).as_posix())
    return out


def _sh_allowed(rel: str, line: str) -> bool:
    return any(rel == f and part in line for (f, part) in ALLOWED_SH)


def test_every_curl_in_the_scripts_is_limited():
    """curl с --max-time ≤ 10, либо скачивание с --connect-timeout ≤ 5 и
    --speed-time ≤ 10 (большую поставку за 10 с не скачать, но застрявшее
    соединение держать минуту тоже нельзя)."""
    assert all(p.is_file() for p in SH_FILES), [p for p in SH_FILES if not p.is_file()]
    bad = [f"{rel}:{line}: {why} — {src[:110]}"
           for rel, src, line, why in _all_sh_hits() if not _sh_allowed(rel, src)]
    assert not bad, "curl дольше правила:\n" + "\n".join(bad)


def _curl_is_ipv4(line: str) -> bool:
    """У вызова curl есть -4 (отдельно, в связке коротких флагов или --ipv4)."""
    try:
        words = shlex.split(line[_CURL.search(line).start():], posix=True)
    except ValueError:
        words = line[_CURL.search(line).start():].split()
    for w in words[1:]:
        if w == "--ipv4" or (w.startswith("-") and not w.startswith("--") and "4" in w):
            return True
    return False


def _sh_curls_without_ipv4():
    out = []
    for path in SH_FILES:
        rel = path.relative_to(REPO).as_posix()
        for lineno, line in _logical_lines(path.read_text(encoding="utf-8")):
            if line.lstrip().startswith("#") or not _CURL.search(line) or _sh_allowed(rel, line):
                continue
            if not _curl_is_ipv4(line):
                out.append(f"{rel}:{lineno}: {line.strip()[:110]}")
    return out


def test_every_curl_in_the_scripts_goes_out_over_ipv4_only():
    """Правило проекта: наружу только IPv4. curl без -4 берёт AAAA первым и на
    шлюзе уходит мимо туннеля, на ВПС — в никуда: установка и обновление
    стоят секунды на каждом запросе к GitHub и спискам."""
    bad = _sh_curls_without_ipv4()
    assert not bad, "curl без -4:\n" + "\n".join(bad)


def test_the_shell_scan_sees_curl_without_ipv4(tmp_path):
    """Сторож сторожа: -4 отдельно, в связке -4fsSL, --ipv4 — норма; без них —
    находка; комментарий и `command -v curl` — не вызовы."""
    assert _curl_is_ipv4('curl -4 -fsS --max-time 5 "$U"')
    assert _curl_is_ipv4('x="$(curl -4fsSL --retry 3 -o f "$U")"')
    assert _curl_is_ipv4('curl --ipv4 -fsS "$U"')
    assert not _curl_is_ipv4('curl -fsSL --retry 3 -o f "$U"'), "curl без -4 прошёл как IPv4"
    assert not _curl_is_ipv4('curl -fsS "$U" -o f4'), "цифра в имени файла сошла за -4"


def test_every_shell_exception_still_covers_a_real_line():
    """Мёртвое исключение — молчаливый пропуск для следующей строки."""
    hits = _all_sh_hits()
    stale = [key for key in ALLOWED_SH if not any(rel == key[0] and key[1] in src for rel, src, *_ in hits)]
    assert not stale, f"исключения без строк: {stale}"


def test_the_shell_scan_actually_sees_curl(tmp_path):
    """Сторож сторожа: --max-time 60, curl без пределов, --speed-time 30,
    опции на строке-продолжении, комментарий и `command -v curl` — на
    пробном скрипте."""
    tmp = tmp_path / "probe.sh"
    tmp.write_text(
        "# curl -fsS --max-time 60 http://x\n"              # 1: комментарий
        "command -v curl >/dev/null || die 'нужен curl'\n"  # 2: не вызов
        "curl -fsS --max-time 60 \"$URL\" -o f\n"            # 3: длинный max-time
        "curl -fsSL --retry 3 -o f \"$URL\"\n"               # 4: без пределов
        "x=\"$(curl -fsS --max-time 5 https://api.ipify.org)\"\n"   # 5: норма
        "curl -sf --connect-timeout 5 --speed-time 30 --speed-limit 1024 \"$U\"\n"  # 6: speed-time 30
        "run curl -fsSL \\\n    --connect-timeout 5 --speed-time 10 -o f \"$U\"\n"   # 7-8: норма, продолжение
        "curl -fsS --connect-timeout 20 --speed-time 10 \"$U\"\n",   # 9: connect 20
        encoding="utf-8")
    hits = {line: why for _, _, line, why in _sh_hits(tmp, "probe.sh")}
    assert set(hits) == {3, 4, 6, 9}, hits
    assert "60 с > 10" in hits[3]
    assert "без предела" in hits[4]
    assert "--speed-time" in hits[6]
    assert "--connect-timeout" in hits[9]


# ─────────────────────────────────────────────────────────────────────────────
# Точечно: значения этапа
# ─────────────────────────────────────────────────────────────────────────────

def test_the_module_constants_for_outbound_requests_are_ten_seconds():
    """GitHub, пул адресов Telegram, getMe нового токена агента, запасной
    канал tgsend — по 10 с: иначе проверка обновлений или смена токена
    держит такт дольше правила."""
    from awgbot.infra.updates import github
    from awgbot.domain.services.reserve import tgpool
    from awgbot.domain.gateway.cfg import token
    from awgbot.infra import tgsend
    assert github._TIMEOUT == 10, "запрос к GitHub дольше 10 с"                       # noqa: SLF001
    assert tgpool.FETCH_TIMEOUT == 10, "скачивание пула адресов дольше 10 с"
    assert token.GETME_TIMEOUT == 10, "getMe новым токеном дольше 10 с"
    assert tgsend._TIMEOUT <= 10, "запасной канал в Telegram дольше 10 с"              # noqa: SLF001


def test_the_default_timeouts_of_the_wrappers_are_ten_seconds():
    """Умолчания fetch списков и почтовых проверок — 10 с: вызывающий без
    явного timeout получает предел правила, а не прежние 15–20."""
    from awgbot.infra.routing import feeds
    from awgbot.infra import mail
    assert inspect.signature(feeds.fetch).parameters["timeout"].default == 10
    for fn in (mail.check_imap, mail.check_smtp, mail.send_mail):
        assert inspect.signature(fn).parameters["timeout"].default == 10.0, \
            f"{fn.__name__}: умолчание не 10 с"


def test_the_mailbox_poll_connects_with_a_ten_second_limit(monkeypatch):
    """Опрос ящика кодов возобновления открывает IMAP с пределом 10 с — фоновая
    задача не должна зависать на почтовом сервере."""
    from awgbot.infra import email_resume as er
    from awgbot.infra.mail import MailAccount
    seen: list[dict] = []

    class FakeIMAP:
        def __init__(self, *a, **kw):
            seen.append(kw)
        def login(self, u, p): pass
        def select(self, box): return "NO", [b""]
        def logout(self): pass

    monkeypatch.setattr(er.net4, "IMAP4_SSL4", FakeIMAP)
    acc = MailAccount("box@example.org", "pass", "imap.example.org", 993, "smtp.example.org", 587)
    assert er.poll_once(acc, lambda code: False) == 0
    assert seen and seen[0].get("timeout") == 10, f"IMAP открыт с пределом {seen and seen[0].get('timeout')}"


class _FakeBot:
    """Подмена aiogram.Bot: запоминает сессию, отвечает на getMe без сети."""
    made: list = []

    def __init__(self, token, session=None, **kw):
        self.token, self.session = token, session
        _FakeBot.made.append(self)

    async def get_me(self):
        from types import SimpleNamespace
        return SimpleNamespace(id=42, username="gw_bot", first_name="Шлюз")


def _assert_retrying(session) -> None:
    from awgbot.runtime import tgsession
    assert isinstance(session, tgsession.RetryingSession), \
        f"Bot создан с сессией {type(session).__name__}: без короткого connect и предела 10 с"
    assert session.timeout == tgsession.TOTAL_S


async def test_the_gateway_bot_identity_refresh_uses_the_retrying_session(monkeypatch):
    """getMe бота слота идёт через RetryingSession: иначе при вялом аплинке
    такт ensure_all висит минуту на одном слоте."""
    import aiogram
    from awgbot.runtime import gwbotme
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)
    _FakeBot.made.clear()
    stored: list = []

    class Services:
        def gw_bot_token(self, slot_id): return "123456789:DUMMYDUMMYDUMMYDUMMYDUMMY"
        def set_gw_bot_identity(self, slot_id, username, title): stored.append((slot_id, username, title))

    assert await gwbotme.refresh(Services(), 1) is True
    assert stored == [(1, "gw_bot", "Шлюз")], "ответ getMe не записан"
    assert len(_FakeBot.made) == 1
    _assert_retrying(_FakeBot.made[0].session)


async def test_the_token_check_in_the_service_section_uses_the_retrying_session(monkeypatch):
    """Проверка нового токена шлюза в «🔧 Сервис» — через RetryingSession:
    админ ждёт ответа не дольше 10 с."""
    import aiogram
    from awgbot.bot.handlers.settings import gwtoken
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)
    _FakeBot.made.clear()
    assert await gwtoken._getme("123456789:DUMMYDUMMYDUMMYDUMMYDUMMY") == (42, "gw_bot")   # noqa: SLF001
    assert len(_FakeBot.made) == 1
    _assert_retrying(_FakeBot.made[0].session)
