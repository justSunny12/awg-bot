"""Каркас набора: где живут временные каталоги тестов и что видит сторож временного (tests/conftest.py).

Цена ошибки. Общий для всех рабочих деревьев `pytest-of-<user>` при retention_count = 1 — выход
точечного прогона в соседнем дереве стирает каталоги упавших тестов только что кончившегося
прогона этого: артефактов для разбора уже нет. Сторож временного, который на Linux смотрит
только $TMPDIR процесса, не видит tmp.* скриптов, запущенных тестом без TMPDIR (они идут в /tmp),
и мусор копится на стенде молча.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

from tests import conftest

pytestmark = pytest.mark.unit


def _controller_argv(config) -> list[str]:
    """Аргументы прогона: у воркера xdist — аргументы контроллера (его basetemp — общий)."""
    wi = getattr(config, "workerinput", None)
    return list(wi["mainargv"]) if wi else list(sys.argv)


def test_each_tree_has_its_own_temp_root_and_the_same_tree_keeps_it(tmp_path):
    """Два дерева — два корня, одно дерево — один и тот же корень между прогонами: иначе либо
    деревья снова чистят друг друга, либо дерево не находит каталоги своего упавшего прогона."""
    a, b = tmp_path / "tree-a", tmp_path / "tree-b"
    a.mkdir(); b.mkdir()
    ra, rb = conftest._tree_temproot(str(a)), conftest._tree_temproot(str(b))
    assert ra != rb, "у двух деревьев общий корень временного"
    assert conftest._tree_temproot(str(a)) == ra, "корень дерева меняется от вызова к вызову"
    assert conftest._tree_temproot(str(a) + "/") == ra, "тот же каталог другой записью — другой корень"


def test_the_tree_root_is_outside_the_shared_pytest_dir_and_the_repo():
    """Корень — рядом с общим `pytest-of-<user>`, а не внутри него (там его нумерованные каталоги
    чистит любой прогон) и не в дереве (testpaths, git status)."""
    root = pathlib.Path(conftest._tree_temproot(conftest._ROOT))
    assert root.parent == pathlib.Path(tempfile.gettempdir()), root
    assert not root.name.startswith("pytest-"), f"{root.name}: такие имена в общем каталоге чистит pytest"
    assert not str(root.resolve()).startswith(os.path.realpath(conftest._ROOT) + os.sep), root


def test_tmp_path_of_a_manual_run_lies_under_the_tree_root(request, tmp_path):
    """Ручной прогон без --basetemp кладёт tmp_path под корень своего дерева (через
    PYTEST_DEBUG_TEMPROOT), а не в общий `pytest-of-<user>` всех деревьев."""
    argv = _controller_argv(request.config)
    if any(a == "--basetemp" or a.startswith("--basetemp=") for a in argv):
        pytest.skip("свой --basetemp (очередь полных прогонов) — каталог задан снаружи")
    want = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    assert want, "conftest не задал корень временного дерева"
    if want != conftest._tree_temproot(conftest._ROOT):
        pytest.skip("корень временного задан снаружи — он главнее")
    assert pathlib.Path(want).is_dir(), want
    assert str(tmp_path.resolve()).startswith(os.path.realpath(want) + os.sep), \
        f"tmp_path {tmp_path} не под корнем дерева {want}: деревья снова делят pytest-of-<user>"


def test_the_script_tmp_guard_on_linux_watches_tmpdir_and_tmp(monkeypatch):
    """На Linux сторож смотрит и $TMPDIR процесса (на стенде /var/tmp/awglab-tmp), и /tmp: скрипт,
    которому тест дал окружение без TMPDIR, кладёт tmp.* в /tmp."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("TMPDIR", "/var/tmp/awglab-tmp")
    assert conftest._script_tmp_roots() == [pathlib.Path("/var/tmp/awglab-tmp"), pathlib.Path("/tmp")]


def test_the_script_tmp_guard_on_linux_without_tmpdir_watches_tmp_once(monkeypatch):
    """Без TMPDIR — один /tmp, не дважды: иначе каждый новый tmp.* в итоге прогона считался бы дважды."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("TMPDIR", raising=False)
    assert conftest._script_tmp_roots() == [pathlib.Path("/tmp")]
