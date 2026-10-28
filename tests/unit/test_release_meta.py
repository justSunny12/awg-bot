"""Метаданные релиза (awg-bot-release.json, схема 1): что боты принимают и что
отвергают, и инструмент выпуска tools/release_meta.py — строит то же из
аргументов, awgbot/__version__.py и install/awg.lock, печатает строки тела
только для релизов моложе 3.3.2."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from awgbot.infra.updates import meta as m
from tools import release_meta as tool

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _meta(version="3.3.1", roles=None, **over):
    data = {"schema": 1, "version": version, "awg_gen": 1,
            "roles": roles if roles is not None else {
                "main": {"requires": "3.3.0", "needs_gw": "3.3.0"},
                "gw": {"requires": "3.3.0", "needs_main": "3.3.0"}}}
    data.update(over)
    return data


def test_a_valid_file_is_accepted_and_read():
    meta = m.parse(json.dumps(_meta()).encode(), "v3.3.1")
    assert m.requires(meta) == {"main": (3, 3, 0), "gw": (3, 3, 0)}
    assert m.needs(meta, "main") == (3, 3, 0) and m.needs(meta, "gw") == (3, 3, 0)


def test_unknown_keys_and_roles_are_skipped_new_fields_come_with_the_same_schema():
    meta = _meta(extra={"x": 1})
    meta["roles"]["main"]["later"] = "3.4.0"
    meta["roles"]["guest"] = {"anything": True}                 # роль будущей версии
    assert m.check(meta, "v3.3.1") is meta
    assert m.requires(meta) == {"main": (3, 3, 0), "gw": (3, 3, 0)}, "неизвестная роль — не адресат"


@pytest.mark.parametrize("why, meta, tag", [
    ("нет requires у роли", _meta(roles={"main": {"needs_gw": "3.3.0"}}), "v3.3.1"),
    ("needs_gw у gw", _meta(roles={"gw": {"requires": "3.3.0", "needs_gw": "3.3.0"}}), "v3.3.1"),
    ("needs_main у main", _meta(roles={"main": {"requires": "3.3.0", "needs_main": "3.3.0"}}), "v3.3.1"),
    ("needs у хотфикса", _meta("3.3.1.1", roles={"gw": {"requires": "3.3.0", "needs_main": "3.3.0"}}),
     "v3.3.1.1"),
    ("needs в четыре цифры", _meta(roles={"gw": {"requires": "3.3.0", "needs_main": "3.3.0.1"}}), "v3.3.1"),
    ("версия не тег", _meta("3.3.2"), "v3.3.1"),
    ("неизвестная схема", _meta(schema=2), "v3.3.1"),
    ("нет поколения", _meta(awg_gen=None), "v3.3.1"),
    ("пустые адресаты", _meta(roles={}), "v3.3.1"),
    ("ни одной известной роли", _meta(roles={"all": {"requires": "3.3.0"}}), "v3.3.1"),
    ("не объект", [1, 2], "v3.3.1"),
])
def test_broken_metadata_is_refused(why, meta, tag):
    with pytest.raises(m.MetaError):
        m.check(meta, tag)


def test_garbage_and_oversize_are_refused():
    with pytest.raises(m.MetaError):
        m.parse(b"\xff not json", "v3.3.1")
    big = json.dumps(_meta(pad="x" * (m.META_MAX + 1))).encode()
    with pytest.raises(m.MetaError):
        m.parse(big, "v3.3.1")


def test_body_lines_only_before_the_metadata_only_step():
    assert m.body_lines(_meta("3.3.0", {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}})) == \
        ["#requires_main_3.2.0", "#requires_gw_3.2.0", "#awg_gen1"]
    assert m.body_lines(_meta("3.3.1")) == ["#requires_main_3.3.0", "#requires_gw_3.3.0", "#awg_gen1"], \
        "перекрёстных условий в теле нет никогда"
    assert m.body_lines(_meta("3.3.2")) == []


def test_the_tool_builds_the_same_from_arguments_version_and_lock():
    ver, gen = tool.installed_version(), tool.awg_generation()
    meta = tool.build(ver, gen, main="3.2.0", gw="3.2.0")
    assert meta == {"schema": 1, "version": ver, "awg_gen": gen,
                    "roles": {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}}}
    meta = tool.build("3.3.1", 1, main="3.3.0", needs_gw="3.3.0", gw="3.3.0", needs_main="3.3.0")
    assert meta == _meta()
    with pytest.raises(ValueError, match="needs_gw"):         # схема грузится файлом: класс — свой
        tool.build("3.3.1", 1, gw="3.3.0", needs_gw="3.3.0")      # условие сервера без адресата main


def _run(*args):
    return subprocess.run([sys.executable, "-m", "tools.release_meta", *args], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_the_tool_prints_body_lines_and_checks_files(tmp_path):
    good = tmp_path / "v3.3.1.json"
    good.write_text(json.dumps(_meta()), encoding="utf-8")
    proc = _run("--hashtags", str(good))
    assert proc.returncode == 0 and proc.stdout.split() == ["#requires_main_3.3.0", "#requires_gw_3.3.0",
                                                            "#awg_gen1"]
    later = tmp_path / "v3.3.2.json"
    later.write_text(json.dumps(_meta("3.3.2")), encoding="utf-8")
    assert _run("--hashtags", str(later)).stdout == "", "с 3.3.2 — только метаданные"
    assert _run("--check", str(good), "--expect-version", "3.3.1").returncode == 0
    wrong = _run("--check", str(good), "--expect-version", "3.3.2")
    assert wrong.returncode == 1 and "3.3.2" in wrong.stderr
    named = tmp_path / "v3.3.0.json"
    named.write_text(json.dumps(_meta()), encoding="utf-8")
    assert _run("--check", str(named)).returncode == 1, "версия в файле не равна имени файла"
    assert _run("--main").returncode == 2
    assert _run("--check", str(good), "--hashtags", str(good)).returncode == 2, "режимы взаимоисключающие"
    huge = tmp_path / "v3.3.3.json"
    huge.write_text(json.dumps(_meta("3.3.3", pad="x" * m.META_MAX)), encoding="utf-8")
    assert _run("--check", str(huge)).returncode == 1, "предел — как у бота"


def test_the_check_compares_the_generation_with_the_lock(tmp_path):
    ver, gen = tool.installed_version(), tool.awg_generation()
    path = tmp_path / f"v{ver}.json"
    path.write_text(json.dumps(_meta(ver, {"main": {"requires": "3.2.0"}}, awg_gen=gen)), encoding="utf-8")
    assert _run("--check", str(path), "--expect-version", ver).returncode == 0
    path.write_text(json.dumps(_meta(ver, {"main": {"requires": "3.2.0"}}, awg_gen=gen + 1)), encoding="utf-8")
    bad = _run("--check", str(path), "--expect-version", ver)
    assert bad.returncode == 1 and "awg.lock" in bad.stderr


def test_the_tool_is_not_shipped():
    """Инструмент выпуска в поставку не едет — ни сборкой, ни поставкой из
    установки (util/dist.py, тот же состав)."""
    build = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    assert 'rm -f "$s/tools/release_meta.py"' in build
    assert 'rm -f "$OUT/awg-bot-release.json"' in build, "пропуск артефакта убирает метаданные прошлой сборки"
    from awgbot.util import dist
    import tarfile
    info = tarfile.TarInfo("./tools/release_meta.py")
    assert dist._tar_filter(info) is None


# ── requires адресатов = UPDATE_FROM ────────────────────────────────────────

def test_the_tool_refuses_a_minimum_other_than_update_from():
    """`--main X --gw X` с X ≠ UPDATE_FROM — код 1 и причина: иначе в выпуск ушёл бы
    минимум, с которым разошёлся бы сторож терминала (`awg-bot update` читает
    UPDATE_FROM), — бот скажет «сначала vX», а терминал пустит или откажет иначе."""
    floor = tool.update_from()
    other = "3.3.0" if floor != "3.3.0" else "3.2.0"
    for args in (("--main", other, "--gw", other), ("--main", floor, "--gw", other), ("--gw", other)):
        proc = _run(*args)
        assert proc.returncode == 1 and f"≠ UPDATE_FROM {floor}" in proc.stderr, (args, proc.returncode, proc.stderr)
        assert proc.stdout == "", f"при отказе метаданные всё равно напечатаны: {proc.stdout!r}"


def test_the_tool_builds_with_the_minimum_equal_to_update_from():
    floor = tool.update_from()
    for args, roles in ((("--main", floor, "--gw", floor), {"main", "gw"}), (("--main", floor), {"main"})):
        proc = _run(*args)
        assert proc.returncode == 0, (args, proc.stderr)
        meta = json.loads(proc.stdout)
        assert {r: s["requires"] for r, s in meta["roles"].items()} == {r: floor for r in roles}, meta


def test_update_from_is_read_exactly_as_the_package_sees_it():
    from awgbot.core import compat
    assert tool.update_from() == compat.UPDATE_FROM


def test_the_check_of_a_ready_file_does_not_compare_with_update_from(tmp_path):
    """--check проверяет выпущенный файл (в том числе старых выпусков с другим
    минимумом) — сверка с нынешним UPDATE_FROM ему не нужна."""
    floor = tool.update_from()
    old = "3.3.0" if floor != "3.3.0" else "3.2.0"
    path = tmp_path / "v3.3.1.json"
    path.write_text(json.dumps(_meta(roles={"main": {"requires": old}, "gw": {"requires": old}})), encoding="utf-8")
    proc = _run("--check", str(path))
    assert proc.returncode == 0 and "UPDATE_FROM" not in proc.stderr, proc.stderr
