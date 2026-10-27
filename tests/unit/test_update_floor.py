"""
`awg-bot update` и версия совместимости: поставка ниже COMPAT_FLOOR
установленного кода — отказ до остановки сервиса («откат ниже 3.4.0
невозможен — только чистая установка»); выше — как раньше.

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

FUNCS = ("locate_tgz", "compat_floor", "tree_version", "version_lt", "cmd_update")


def _release(path, version: str) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, raw in (("awg-bot/awgbot/__main__.py", b""),
                          ("awg-bot/awgbot/__version__.py", f'__version__ = "{version}"\n'.encode())):
            ti = tarfile.TarInfo(name); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    path.write_bytes(buf.getvalue())


def _run(tmp_path, version: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    inst = tmp_path / "opt"; (inst / "awgbot" / "core").mkdir(parents=True)
    (inst / "awgbot" / "core" / "compat.py").write_text('COMPAT_FLOOR = "3.4.0"\n', encoding="utf-8")
    tgz = tmp_path / "awg-bot.tgz"
    _release(tgz, version)
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
