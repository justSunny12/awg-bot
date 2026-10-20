"""Сторож хранения бота основы на резерве: ключи пакета и реплика живут только
в хранилище гостя в памяти и в шифрованном `rep.bin`.

Проверяется по исходникам, а не по поведению: нарушение (одна лишняя строка
`open(..., "w")`) в рабочем коде ничем не проявится, пока резерв не окажется
на разборе — а к тому времени ключи основы уже лежат у чужого сервера на диске.

Цена ошибки: снятие `M`/`K` и разбор частей пакета в любом модуле бота резерва
— процесс с токеном и ключами резерва видит открытые данные основы; запись
ключа или реплики мимо `rsvzone.write_private` — файл с правами по умолчанию
(читаем шире 0600) или на диске, а не в памяти.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# снятие K/M и разбор частей пакета — единственный потребитель tools/rsvopen.py
OPENERS = {"open_k", "open_dns", "open_parts", "recover_m"}
OPENER_HOMES = {"awgbot/util/rsvpkg/seal.py", "awgbot/util/rsvkeys.py", "tools/rsvopen.py"}

# модули бота основы на резерве и обёртки зоны: всё, что работает в гостевом режиме
GUEST_SIDE = (
    "awgbot/runtime/main/guest.py", "awgbot/runtime/webhook.py", "awgbot/runtime/pairlink/guestctl.py",
    "awgbot/runtime/scheduler/guest.py", "awgbot/domain/services/reserve/guest.py", "awgbot/infra/rsvzone.py",
    "awgbot/bot/handlers/guest.py", "awgbot/bot/handlers/settings/reserve/guest.py", "awgbot/bot/texts/reserve/guest.py",
)
# единственные места, где гостевая сторона создаёт файлы: приватная запись хранилища, статус открытия (без секретов)
# и временная копия базы для снимка (в хранилище в памяти, удаляется сразу)
WRITE_ALLOWED = {
    ("awgbot/infra/rsvzone.py", "write_private"),
    ("awgbot/infra/rsvzone.py", "open_status_write"),
    ("awgbot/domain/services/reserve/guest.py", "ReserveGuestMixin._guest_db_bytes"),
}
# методы, создающие файл, где бы ни стоял вызов; и os.* / shutil.*, меняющие файловую систему
WRITE_ATTRS = {"write_text", "write_bytes", "mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile"}
WRITE_BY_MODULE = {"os": {"open", "fdopen", "replace", "rename", "symlink", "link", "mkfifo"},
                   "shutil": {"copy", "copy2", "copyfile", "copyfileobj", "copytree", "move"}}


def _py_files(*roots: str):
    for r in roots:
        for p in sorted((ROOT / r).rglob("*.py")):
            if "__pycache__" not in p.parts:
                yield p


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _opener_uses(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in OPENERS:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in OPENERS:
            found.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            found |= {a.name for a in node.names if a.name in OPENERS}
    return found


def test_only_the_open_process_removes_k_and_m_or_reads_the_parts_of_a_package():
    offenders = {}
    for p in _py_files("awgbot", "tools"):
        rel = _rel(p)
        if rel in OPENER_HOMES:
            continue
        used = _opener_uses(ast.parse(p.read_text(encoding="utf-8")))
        if used:
            offenders[rel] = sorted(used)
    assert not offenders, f"снятие K/M или разбор частей пакета вне tools/rsvopen.py: {offenders}"


def test_the_guard_sees_the_open_process_so_it_can_fail():
    used = _opener_uses(ast.parse((ROOT / "tools/rsvopen.py").read_text(encoding="utf-8")))
    assert used == OPENERS, f"сторож не узнаёт вызовы процесса открытия: {used}"


@pytest.mark.parametrize("src, want", [
    ("from awgbot.util.rsvpkg import seal\nseal.open_parts(b, k)", {"open_parts"}),
    ("from awgbot.util.rsvpkg.seal import open_k\nopen_k(b, m)", {"open_k"}),
    ("from awgbot.util.rsvpkg.seal import open_dns as od\nod(b, m)", {"open_dns"}),
    ("rsvkeys.recover_m(p1, p2)", {"recover_m"}),
    ("seal.header(b); seal.digest(b)", set()),
])
def test_the_opener_detector_catches_every_way_to_call_the_openers(src, want):
    assert _opener_uses(ast.parse(src)) == want


@pytest.mark.parametrize("src, kind", [
    ('open(p, "w")', "open"), ('open(p, "rb+")', "open"), ('open(p, mode="a")', "open"), ('open(p, "x")', "open"),
    ("open(p, mode)", "open(режим не константа)"), ('open(p)', ""), ('open(p, "r")', ""), ('open(p, "rb")', ""),
    ("os.open(p, os.O_WRONLY)", "os.open"), ("os.replace(a, b)", "os.replace"), ("shutil.copyfile(a, b)", "shutil.copyfile"),
    ("path.write_text(x)", "write_text"), ("path.write_bytes(x)", "write_bytes"), ("tempfile.mkstemp()", "mkstemp"),
    ("text.replace(a, b)", ""), ("json.dumps(x)", ""),
])
def test_the_write_detector_sees_every_way_to_create_a_file_and_ignores_reads(src, kind):
    call = ast.parse(src).body[0].value
    assert _is_write_call(call) == kind


def test_no_bot_module_imports_the_open_process():
    offenders = []
    for p in _py_files("awgbot"):
        text = p.read_text(encoding="utf-8")
        if "rsvopen" in text and "tools" in text:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""] + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                if any("rsvopen" in m for m in mods):
                    offenders.append(_rel(p))
    assert not offenders, f"модуль бота подключает процесс открытия: {offenders}"


def _calls_with_scope(tree: ast.AST):
    """(квалифицированное имя охватывающей функции, Call)."""
    out = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child.name])
                continue
            if isinstance(child, ast.Call):
                out.append((".".join(stack), child))
            visit(child, stack)
    visit(tree, [])
    return out


def _is_write_call(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name) and f.id == "open":
        mode = call.args[1] if len(call.args) >= 2 else None
        for kw in call.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            return ""
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return "open" if set(mode.value) & set("wax+") else ""
        return "open(режим не константа)"
    if isinstance(f, ast.Attribute):
        if isinstance(f.value, ast.Name) and f.attr in WRITE_BY_MODULE.get(f.value.id, ()):
            return f"{f.value.id}.{f.attr}"
        if f.attr in WRITE_ATTRS:
            return f.attr
    return ""


def test_guest_side_modules_write_files_only_through_the_private_helper_or_the_allowed_places():
    offenders = []
    seen_allowed = set()
    for rel in GUEST_SIDE:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for scope, call in _calls_with_scope(tree):
            kind = _is_write_call(call)
            if not kind:
                continue
            if (rel, scope) in WRITE_ALLOWED:
                seen_allowed.add((rel, scope))
                continue
            offenders.append(f"{rel}:{call.lineno} {scope or '<модуль>'} → {kind}")
    assert not offenders, "запись файла вне rsvzone.write_private:\n" + "\n".join(offenders)
    assert seen_allowed == WRITE_ALLOWED, f"сторож не видит ожидаемые записи — он слеп: {WRITE_ALLOWED - seen_allowed}"


def test_the_replica_and_the_key_reach_the_disk_through_the_private_helper():
    """rep.bin и ключ пакета в хранилище пишутся только write_private (0600)."""
    guest = (ROOT / "awgbot/domain/services/reserve/guest.py").read_text(encoding="utf-8")
    assert "rsvzone.write_private(rsvzone.REP_PATH" in guest
    opener = (ROOT / "tools/rsvopen.py").read_text(encoding="utf-8")
    assert "rsvzone.write_private(rsvzone.K_FILE" in opener
    assert "rsvzone.write_private(run / \"data\" / \"bot.db\"" in opener
    for line in opener.splitlines():
        assert not ('open(' in line and '"w' in line), f"прямая запись в rsvopen: {line.strip()}"


def test_the_zone_script_writes_nothing_secret_to_arguments_or_logs():
    """Конфиг клона приходит только через stdin; ключ в аргументах виден в ps."""
    script = (ROOT / "install/reserve-zone-setup.sh").read_text(encoding="utf-8")
    assert 'cat > "$RUN/awg/$iface.conf"' in script, "конфиг клона — из stdin в хранилище"
    for line in script.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        assert "PrivateKey" not in s or s.startswith("grep"), f"ключ в строке скрипта: {s}"
        assert "private-key" not in s or "private-key /dev/stdin" in s, f"ключ в аргументах awg: {s}"
