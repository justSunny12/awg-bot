"""
`awg-bot update`, версия совместимости и минимум обновления: поставка ниже
COMPAT_FLOOR установленного кода — отказ до остановки сервиса («откат ниже
3.4.0 невозможен — только чистая установка»); установка ниже UPDATE_FROM новой
поставки — отказ там же («обновиться с v3.4.1 сразу до v4.1.0 нельзя — сначала обнови до
v4.0.0»): шагов перехода с версий ниже минимума в поставке нет, и прыжок
оставил бы хост на conf и обвязке прежней раскладки без единой ошибки; выше
— как раньше.

Прогоняется настоящая cmd_update из awg-bot.sh до остановки сервиса:
подставной systemctl на `stop` завершает прогон — дальше подмена кода,
которую проверяют другие тесты.
"""
from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

from tests.botscript import flat

from tests.unit.test_cli_script import _extract_func

pytestmark = pytest.mark.unit

FUNCS = ("locate_tgz", "compat_floor", "update_from", "tree_version", "version_lt", "cmd_update")


def _release(path, version: str, update_from: str = "") -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = [("awg-bot/awgbot/__main__.py", b""),
                 ("awg-bot/awgbot/__version__.py", f'__version__ = "{version}"\n'.encode())]
        if update_from:
            files.append(("awg-bot/awgbot/core/compat.py",
                          f'COMPAT_FLOOR = "{update_from}"\nUPDATE_FROM = "{update_from}"\n'.encode()))
        for name, raw in files:
            ti = tarfile.TarInfo(name); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    path.write_bytes(buf.getvalue())


def _run(tmp_path, version: str, *, installed: str = "", update_from: str = "") \
        -> tuple[subprocess.CompletedProcess, list[str]]:
    tmp_path.mkdir(exist_ok=True)
    inst = tmp_path / "opt"; (inst / "awgbot" / "core").mkdir(parents=True)
    (inst / "awgbot" / "core" / "compat.py").write_text('COMPAT_FLOOR = "3.4.0"\n', encoding="utf-8")
    if installed:
        (inst / "awgbot" / "__version__.py").write_text(f'__version__ = "{installed}"\n', encoding="utf-8")
    tgz = tmp_path / "awg-bot.tgz"
    _release(tgz, version, update_from)
    log = tmp_path / "calls"
    script = flat()
    prog = "\n".join([
        f'INSTALL_DIR="{inst}"; DATA_DIR="{tmp_path}/data"; ENV_FILE=/x; SERVICE=awg-bot; AWG_UPDATE_KEEP=1',
        f'SELF_DIR="{tmp_path}"',
        'log() { echo "LOG $*"; }; die() { echo "DIE $*" >&2; exit 1; }; warn() { echo "WARN $*" >&2; }',
        "require_root() { :; }; require_installed() { :; }; ensure_python() { :; }; chown() { :; }",
        "update_lock() { :; }",
        f'systemctl() {{ echo "systemctl $*" >> "{log}"; [[ "$1" == stop ]] && exit 0; }}',
        *(_extract_func(script, f) for f in FUNCS),
        f'cmd_update "{tgz}"',
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)})
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return r, calls


def test_an_archive_below_the_floor_is_refused_before_the_service_stops(tmp_path):
    r, calls = _run(tmp_path, "3.3.1")
    assert r.returncode == 1 and "откат ниже 3.4.0 невозможен — только чистая установка" in r.stderr, r.stderr
    assert calls == [], "сервис тронут до отказа"


def test_an_archive_above_the_floor_goes_on_as_before(tmp_path):
    r, calls = _run(tmp_path, "3.4.1")
    assert r.returncode == 0, r.stderr
    assert calls == ["systemctl stop awg-bot"]


def test_a_hotfix_of_the_floor_is_not_below_it(tmp_path):
    r, calls = _run(tmp_path, "3.4.0.2")
    assert r.returncode == 0 and calls == ["systemctl stop awg-bot"], r.stderr


# ── минимум обновления новой поставки ────────────────────────────────────────

def test_an_install_below_the_update_from_of_the_delivery_is_refused_before_the_service_stops(tmp_path):
    """Хост 3.4.1 → поставка 4.1.0 с UPDATE_FROM 4.0.0: отказ с подсказкой,
    какой выпуск нужен сначала, и сервис не тронут — прежний код работает."""
    r, calls = _run(tmp_path, "4.1.0", installed="3.4.1", update_from="4.0.0")
    assert r.returncode == 1, r.stderr
    assert "обновиться с v3.4.1 сразу до v4.1.0 нельзя — сначала обнови до v4.0.0:" in r.stderr, r.stderr
    assert "/releases/download/v4.0.0/awg-bot.tgz && sudo awg-bot update /tmp/awg-bot.tgz" in r.stderr, r.stderr
    assert calls == [], "сервис тронут до отказа"


@pytest.mark.parametrize("installed", ["4.0.0", "4.0.2", "4.0.0.3"], ids=["minimum", "above", "hotfix-of-minimum"])
def test_an_install_at_or_above_the_update_from_goes_on(tmp_path, installed):
    r, calls = _run(tmp_path, "4.1.0", installed=installed, update_from="4.0.0")
    assert r.returncode == 0 and calls == ["systemctl stop awg-bot"], r.stderr


def test_a_delivery_without_update_from_or_an_unreadable_installed_version_is_not_refused(tmp_path):
    """Поставки до минимума (без константы) и установка с нечитаемой версией —
    сторожу не с чем сравнивать: как раньше, без отказа."""
    r, calls = _run(tmp_path / "a", "4.1.0", installed="3.4.1")
    assert r.returncode == 0 and calls == ["systemctl stop awg-bot"], r.stderr
    r, calls = _run(tmp_path / "b", "4.1.0", update_from="4.0.0")
    assert r.returncode == 0 and calls == ["systemctl stop awg-bot"], r.stderr


def test_update_from_is_read_from_the_real_compat_module_the_same_way_as_the_floor():
    """Регулярное выражение скрипта обязано читать настоящий compat.py: иначе
    константа есть, а сторож молчит."""
    from pathlib import Path
    from awgbot.core import compat
    root = Path(__file__).resolve().parents[2]
    prog = "\n".join([_extract_func(flat(), "update_from"), _extract_func(flat(), "compat_floor"),
                      f'update_from "{root}"; compat_floor "{root}"'])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.stdout.split() == [compat.UPDATE_FROM, compat.COMPAT_FLOOR], r.stdout + r.stderr
