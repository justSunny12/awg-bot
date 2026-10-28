"""Инвариант линии совместимости: в пределах COMPAT_FLOOR схема базы и conf
только растут.

Откат обновления в той же установке, восстановление бэкапа на выпуск ниже
текущего и шлюзы, идущие за сервером вниз, ставят прежний код на базу и conf,
которые уже тронул новый. Это работает, только если ни одна таблица и колонка
не пропала, не переименована и не сменила тип, а ключ conf не пропал и не
сменил тип: добавлять можно, убирать и менять — только вместе с подъёмом
COMPAT_FLOOR (docs/RELEASING.md §2: «колонка, добавленная в пределах линии,
сдвига не требует»).

Сторож — снимок на COMPAT_FLOOR в tests/fixtures: schema_floor.sql (DDL
свежей базы) и conf_floor.txt (ключи шаблонов conf/*.yaml с типом значения).
Текущее обязано быть их надмножеством. Снимок переснимается только вместе со
сдвигом COMPAT_FLOOR: `python -m tests.unit.test_schema_line`.

Второй снимок — по последнему выпущенному тегу линии (откат набора и
восстановление в линии идут и на него, не только на COMPAT_FLOOR): колонка
или ключ, добавленные после COMPAT_FLOOR и снятые позже, снимок минимума не
увидел бы. Снимок тега снимается из дерева тега (`git archive`), не из
текущего дерева: снятый из текущего дерева он совпал бы с ним по построению и
ничего не сторожил бы. Нет git или тега (архив поставки, чужая копия) — тест
пропускается, не падает.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCHEMA_FLOOR = FIXTURES / "schema_floor.sql"
CONF_FLOOR = FIXTURES / "conf_floor.txt"
_HEAD = re.compile(r"^-- COMPAT_FLOOR (\S+)$", re.M)


def _floor() -> str:
    from awgbot.core.compat import COMPAT_FLOOR
    return COMPAT_FLOOR


# ── схема базы ──────────────────────────────────────────────────────────────

def _current_ddl(tmp: Path) -> list[str]:
    """DDL таблиц свежей базы — той, что создаёт код (обе роли — одна схема)."""
    from awgbot.infra.db import Database
    db = Database(str(tmp / "floor.db"))
    try:
        db.init_schema()
        rows = db._connection().execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()
    finally:
        db.close()
    return [r[0].strip() for r in rows]


def _columns(ddl: str) -> dict[str, dict[str, str]]:
    """{таблица: {колонка: тип}} по тексту DDL — его разбирает сам SQLite."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(ddl)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: {c[1]: str(c[2]).upper() for c in con.execute(f'PRAGMA table_info("{t}")')}
                for t in tables}
    finally:
        con.close()


def test_the_schema_only_grows_within_the_line(tmp_path):
    snap = SCHEMA_FLOOR.read_text(encoding="utf-8")
    m = _HEAD.search(snap)
    assert m, "в снимке нет строки «-- COMPAT_FLOOR X.Y.Z»"
    assert m.group(1) == _floor(), (
        f"COMPAT_FLOOR сдвинут ({m.group(1)} → {_floor()}): переснять снимок — "
        "python -m tests.unit.test_schema_line")
    floor, cur = _columns(snap), _columns(";\n".join(_current_ddl(tmp_path)) + ";")
    assert floor, "снимок пуст"
    gone = sorted(set(floor) - set(cur))
    assert not gone, f"таблицы пропали в линии {_floor()}: {gone} — это сдвиг COMPAT_FLOOR"
    for table, cols in floor.items():
        for col, typ in cols.items():
            assert col in cur[table], f"{table}.{col} пропала в линии {_floor()} — это сдвиг COMPAT_FLOOR"
            assert cur[table][col] == typ, (
                f"{table}.{col}: тип {typ} → {cur[table][col]} в линии {_floor()} — это сдвиг COMPAT_FLOOR")


# ── conf ────────────────────────────────────────────────────────────────────

def _type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    return {int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(type(v), type(v).__name__)


def _flat(node, prefix: str = ""):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from _flat(v, f"{prefix}.{k}" if prefix else str(k))
    elif prefix:
        yield prefix, _type(node)


def _current_conf() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for f in sorted((ROOT / "conf").glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key, typ in _flat(data):
            out[(f.name, key)] = typ
    return out


def _read_conf_floor() -> tuple[str, dict[tuple[str, str], str]]:
    text = CONF_FLOOR.read_text(encoding="utf-8")
    m = _HEAD.search(text)
    assert m, "в снимке conf нет строки «-- COMPAT_FLOOR X.Y.Z»"
    keys = {}
    for ln in text.splitlines():
        if ln and not ln.startswith("--"):
            name, key, typ = ln.split("\t")
            keys[(name, key)] = typ
    return m.group(1), keys


def test_conf_keys_only_grow_within_the_line():
    """Ключ пропал или сменил тип — прежний код прочтёт conf нового не так.
    null (пустое значение в шаблоне) — «не задано», совместим с любым типом."""
    ver, floor = _read_conf_floor()
    assert ver == _floor(), (
        f"COMPAT_FLOOR сдвинут ({ver} → {_floor()}): переснять снимок — python -m tests.unit.test_schema_line")
    assert floor, "снимок conf пуст"
    cur = _current_conf()
    gone = sorted(f"{n}: {k}" for n, k in floor if (n, k) not in cur)
    assert not gone, f"ключи conf пропали в линии {_floor()}: {gone} — это сдвиг COMPAT_FLOOR"
    changed = sorted(f"{n}: {k} {t} → {cur[(n, k)]}" for (n, k), t in floor.items()
                     if "null" not in (t, cur[(n, k)]) and cur[(n, k)] != t)
    assert not changed, f"ключи conf сменили тип в линии {_floor()}: {changed} — это сдвиг COMPAT_FLOOR"


def test_the_guard_sees_a_dropped_column_and_a_changed_type():
    """Сторож сам по себе: пропажу и смену типа он видит."""
    floor = _columns("CREATE TABLE t (a INTEGER, b TEXT);")
    assert set(floor["t"]) - set(_columns("CREATE TABLE t (a INTEGER);")["t"]) == {"b"}
    assert _columns("CREATE TABLE t (a TEXT, b TEXT);")["t"]["a"] != floor["t"]["a"]
    assert dict(_flat({"a": {"b": 1, "c": None}, "d": [1]})) == {"a.b": "int", "a.c": "null", "d": "list"}


# ── последний выпущенный тег линии ──────────────────────────────────────────

_TAG_SNAPSHOT = """
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import yaml
from awgbot.infra.db import Database
with tempfile.TemporaryDirectory() as d:
    db = Database(str(Path(d) / "tag.db"))
    try:
        db.init_schema()
        rows = db._connection().execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    finally:
        db.close()
conf = {}
for f in sorted((Path(sys.argv[1]) / "conf").glob("*.yaml")):
    conf[f.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
print(json.dumps({"ddl": [r[0].strip() for r in rows], "conf": conf}))
"""


def _latest_release_tag() -> str:
    """Последний выпуск по docs/releases (файл есть у каждого выпуска с 3.3.0)."""
    import json
    versions = []
    for f in (ROOT / "docs" / "releases").glob("v*.json"):
        from awgbot.core.compat import version_key
        v = str(json.loads(f.read_text(encoding="utf-8"))["version"])
        versions.append((version_key(v), v))
    return "v" + max(versions)[1]


def _tag_snapshot(tag: str, tmp: Path) -> dict | None:
    """DDL и conf из дерева тега: git archive → распаковка → схема кодом тега в
    подпроцессе (его импорт — отдельный интерпретатор, не этот). None — нет git
    или тега."""
    import json
    import shutil
    import subprocess
    import tarfile
    if not shutil.which("git"):
        return None
    probe = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        return None
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", tag], capture_output=True, timeout=60)
    assert archive.returncode == 0, archive.stderr.decode(errors="replace")
    tree = tmp / "tag"
    tree.mkdir()
    with tarfile.open(fileobj=__import__("io").BytesIO(archive.stdout)) as tar:
        tar.extractall(tree, filter="data")
    r = subprocess.run([sys.executable, "-c", _TAG_SNAPSHOT, str(tree)], capture_output=True, text=True, timeout=120,
                       cwd=str(tree), env={"PATH": "/usr/bin:/bin", "HOME": str(tmp)})
    assert r.returncode == 0, f"схема тега {tag} не снялась:\n{r.stderr[-2000:]}"
    return json.loads(r.stdout)


def test_the_schema_and_conf_are_a_superset_of_the_last_released_tag(tmp_path):
    tag = _latest_release_tag()
    snap = _tag_snapshot(tag, tmp_path)
    if snap is None:
        pytest.skip(f"нет git или тега {tag} — снимок тега не снять")
    tagged, cur = _columns(";\n".join(snap["ddl"]) + ";"), _columns(";\n".join(_current_ddl(tmp_path)) + ";")
    assert tagged, f"снимок тега {tag} пуст"
    gone = sorted(set(tagged) - set(cur))
    assert not gone, f"таблицы тега {tag} пропали: {gone} — это сдвиг COMPAT_FLOOR"
    for table, cols in tagged.items():
        for col, typ in cols.items():
            assert col in cur[table], f"{table}.{col} из {tag} пропала — это сдвиг COMPAT_FLOOR"
            assert cur[table][col] == typ, f"{table}.{col}: тип {typ} ({tag}) → {cur[table][col]} — это сдвиг COMPAT_FLOOR"
    tagged_conf = {(name, key): typ for name, data in snap["conf"].items() for key, typ in _flat(data)}
    cur_conf = _current_conf()
    gone = sorted(f"{n}: {k}" for n, k in tagged_conf if (n, k) not in cur_conf)
    assert not gone, f"ключи conf из {tag} пропали: {gone} — это сдвиг COMPAT_FLOOR"
    changed = sorted(f"{n}: {k} {t} → {cur_conf[(n, k)]}" for (n, k), t in tagged_conf.items()
                     if "null" not in (t, cur_conf[(n, k)]) and cur_conf[(n, k)] != t)
    assert not changed, f"ключи conf из {tag} сменили тип: {changed} — это сдвиг COMPAT_FLOOR"


def test_the_tag_snapshot_is_taken_from_the_tag_tree_not_from_this_one(tmp_path):
    """Сторож самого сторожа: снимок тега обязан отличаться от текущего дерева
    хотя бы версией — иначе он снят не из дерева тега."""
    tag = _latest_release_tag()
    import subprocess, shutil
    if not shutil.which("git") or subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify",
                                                   f"refs/tags/{tag}^{{commit}}"], capture_output=True).returncode:
        pytest.skip(f"нет git или тега {tag}")
    ver = subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:awgbot/__version__.py"],
                         capture_output=True, text=True, timeout=30).stdout
    assert f'__version__ = "{tag[1:]}"' in ver, f"дерево тега {tag} несёт не свою версию: {ver!r}"


def test_without_git_the_tag_guard_is_skipped_not_failed(tmp_path, monkeypatch):
    """Архив поставки и чужая копия без git: сторож тега обязан пропуститься с
    причиной, а не упасть — иначе набор тестов красный у всякого, кто гоняет
    его не из клона репозитория."""
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: None)

    def no_git(*a, **kw):
        raise AssertionError(f"без git его всё равно позвали: {a[0] if a else kw}")
    monkeypatch.setattr(subprocess, "run", no_git)
    assert _tag_snapshot("v4.0.2", tmp_path) is None
    with pytest.raises(pytest.skip.Exception, match="нет git или тега"):
        test_the_schema_and_conf_are_a_superset_of_the_last_released_tag(tmp_path)
    with pytest.raises(pytest.skip.Exception, match="нет git или тега"):
        test_the_tag_snapshot_is_taken_from_the_tag_tree_not_from_this_one(tmp_path)


def test_a_tag_missing_in_the_clone_is_skipped_not_failed(tmp_path):
    """Клон без тегов (`git clone --no-tags`, мелкий клон CI): тега нет — пропуск."""
    import shutil
    if not shutil.which("git"):
        pytest.skip("нет git")
    assert _tag_snapshot("v0.0.0-no-such-tag", tmp_path) is None
    assert not (tmp_path / "tag").exists(), "без тега дерево всё равно распаковывали"


# ── переснять снимок (только со сдвигом COMPAT_FLOOR) ──────────────────────

def _write_snapshots() -> None:
    import tempfile
    FIXTURES.mkdir(parents=True, exist_ok=True)
    head = (f"-- COMPAT_FLOOR {_floor()}\n"
            "-- Снимок на версию совместимости (tests/unit/test_schema_line.py): переснимается\n"
            "-- только вместе со сдвигом COMPAT_FLOOR — python -m tests.unit.test_schema_line\n")
    with tempfile.TemporaryDirectory() as d:
        ddl = _current_ddl(Path(d))
    SCHEMA_FLOOR.write_text(head + "\n" + ";\n\n".join(ddl) + ";\n", encoding="utf-8")
    lines = [f"{n}\t{k}\t{t}" for (n, k), t in sorted(_current_conf().items())]
    CONF_FLOOR.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    _write_snapshots()
    sys.exit(0)
