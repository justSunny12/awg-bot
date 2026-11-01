"""
tests.conftest.tmp_env — временное скрипта под тестом остаётся в каталоге теста.

Стережём: mktemp без шаблона на macOS кладёт файл в личный временный каталог
пользователя мимо TMPDIR (GNU без TMPDIR — в /tmp), и всё, что скрипт под
тестом не убрал (exec дальше, обрыв), копилось между прогонами — около тысячи
каталогов tmp.*. Подмена mktemp из tmp_env берёт шаблон из TMPDIR; с шаблоном
отдаёт настоящему mktemp как есть. Цена ошибки — снова мусор вне каталогов набора.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import tmp_env


def _mktemp(env: dict, *args: str) -> Path:
    r = subprocess.run(["sh", "-c", 'mktemp "$@"', "sh", *args], capture_output=True, text=True,
                       env=env, timeout=10)
    assert r.returncode == 0, r.stderr
    return Path(r.stdout.strip())


def test_mktemp_without_a_template_lands_in_the_test_tmpdir(tmp_path):
    env = tmp_env(tmp_path)
    f = _mktemp(env)
    d = _mktemp(env, "-d")
    assert f.parent == d.parent == Path(env["TMPDIR"])
    assert f.is_file() and d.is_dir() and f.name.startswith("tmp.") and d.name.startswith("tmp.")


def test_mktemp_with_a_template_keeps_it(tmp_path):
    env = tmp_env(tmp_path)
    d = _mktemp(env, "-d", str(tmp_path / "own.XXXXXX"))
    assert d.parent == tmp_path and d.name.startswith("own.") and d.is_dir()


def test_tmp_env_keeps_the_given_path_after_the_shim(tmp_path):
    bin_dir = tmp_path / "bin"
    env = tmp_env(tmp_path, bin_dir, path="/opt/x:/usr/bin:/bin")
    assert env["PATH"] == f"{bin_dir}:/opt/x:/usr/bin:/bin"
    assert (bin_dir / "mktemp").is_symlink()
