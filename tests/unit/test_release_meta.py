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


def test_unknown_keys_are_skipped_new_fields_come_with_the_same_schema():
    meta = _meta(extra={"x": 1})
    meta["roles"]["main"]["later"] = "3.4.0"
    assert m.check(meta, "v3.3.1") is meta


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
    ("чужая роль", _meta(roles={"all": {"requires": "3.3.0"}}), "v3.3.1"),
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


def test_the_tool_is_not_shipped():
    """Инструмент выпуска в поставку не едет — ни сборкой, ни поставкой из
    установки (util/dist.py, тот же состав)."""
    build = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    assert 'rm -f "$s/tools/release_meta.py"' in build
    from awgbot.util import dist
    import tarfile
    info = tarfile.TarInfo("./tools/release_meta.py")
    assert dist._tar_filter(info) is None
