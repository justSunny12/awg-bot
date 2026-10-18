"""
`awg-bot restore --release <архив>`: восстановление с переходом на выпуск
бэкапа (§3.5). Выпуск проверяется (tools.restore_release) и распаковывается
до остановки; после остановки — первая половина обновления без старта (код
выпуска на место, прежний — в .prev-code), затем exec в скрипт выпуска:
окружение, юнит, conf, слияние и замена его кодом. Отказ — прежние код и
окружение назад, сервис поднят, маркер отказа с версиями для бота
(«ничего не изменилось, работает vC»); успех — digest установки = digest
метки, маркер с версией.

Прогоняются настоящие cmd_restore, restore_release_tree, restore_code_swap,
cmd_restore_after_update, restore_apply и restore_code_back; скрипт выпуска в
архиве — те же функции с теми же подменами (exec уходит в него по-настоящему).
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.unit.test_restore_safety import NEW, OLD, Host, _tgz, prelude
from tests.unit.test_cli_script import SCRIPT, _extract_func

pytestmark = pytest.mark.unit

FUNCS = ("restore_release_tree", "restore_code_swap", "cmd_restore_after_update", "restore_prev_code",
         "update_done")
OLD_VER, NEW_VER = "3.4.2", "3.4.1"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _stubs(log: Path, *, env_fails: bool = False) -> list[str]:
    """Шаги второй половины update, которые трогают систему, — в журнал."""
    names = ("update_guard_disarm", "ensure_python", "install_unit", "ensure_host_autostart",
             "ensure_awg_module_loaded", "ensure_awg_generation", "seed_conf", "venv_rollback", "venv_prune",
             "prune_old_kernel_builds", "gw_plumbing_sync")
    out = [f'{n}() {{ echo "{n}" >> "{log}"; return 0; }}' for n in names]
    out.append(f'build_venv() {{ echo "build_venv" >> "{log}"; '
               + ('die "pip: нет сети"; ' if env_fails else "") + "}")
    return out


def _tool(asset: bytes, *, refuse: str = "") -> list[str]:
    """tools.restore_release: версия бэкапа первой строкой, архив — в --out."""
    if refuse:
        return [f'    tools.restore_release) echo "{refuse}" >&2; return 1 ;;']
    return ['    tools.restore_release)',
            '      local out="" arch=""; while [[ $# -gt 0 ]]; do case "$1" in --out) out="$2" ;; '
            '--archive) arch="$2" ;; esac; shift; done',
            '      cp "$arch" "$out"; echo "' + NEW_VER + '"; echo "♻️ Версия: сейчас v' + OLD_VER + '" ;;']


class Release:
    """Установка vC в opt/ и архив выпуска vV, в котором скрипт — те же
    функции awg-bot.sh с подменами; mv -t — через прокладку (у BSD mv его нет)."""

    def __init__(self, script: str, h: Host, *, merge_fails: bool = False, env_fails: bool = False):
        self.h = h
        self.opt = h.tmp / "opt"
        (self.opt / "awgbot").mkdir(parents=True)
        (self.opt / "awgbot" / "__version__.py").write_text(f'__version__ = "{OLD_VER}"\n', encoding="utf-8")
        (self.opt / "awgbot" / "__main__.py").write_text("# old\n", encoding="utf-8")
        (self.opt / "awg-bot.sh").write_text("# old script\n", encoding="utf-8")
        (self.opt / "venv").mkdir()
        self.bin = h.tmp / "bin"; self.bin.mkdir()
        shim = self.bin / "mv"
        shim.write_text('#!/bin/sh\nif [ "$1" = "-t" ]; then d="$2"; shift 2; exec /bin/mv "$@" "$d"; fi\n'
                        'exec /bin/mv "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        new_script = "\n".join(["#!/usr/bin/env bash", f'echo "new-script $*" >> "{h.log}"',
                                *prelude(script, h, merge_fails=merge_fails, funcs=FUNCS),
                                *_stubs(h.log, env_fails=env_fails),
                                'case "${1:-}" in __restore_after_update) cmd_restore_after_update ;; esac'])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, raw, mode in (("awg-bot/awgbot/__version__.py", f'__version__ = "{NEW_VER}"\n'.encode(), 0o644),
                                    ("awg-bot/awgbot/__main__.py", b"# new\n", 0o644),
                                    ("awg-bot/awg-bot.sh", new_script.encode(), 0o755)):
                ti = tarfile.TarInfo(name); ti.size = len(raw); ti.mode = mode
                tar.addfile(ti, io.BytesIO(raw))
        self.blob = buf.getvalue()
        self.hex = hashlib.sha256(self.blob).hexdigest()
        self.asset = h.backups / "restore-pending.release.tgz"
        self.asset.write_bytes(self.blob)
        # бэкап снят на vV: метка с версией и digest выпуска
        plain = h.tmp / "plain-release.tgz"
        with tarfile.open(h.plain) as tar:
            files = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
        meta = json.loads(files["state/backup-meta.json"])
        meta.update(version=NEW_VER, digest=f"sha256:{self.hex}")
        files["state/backup-meta.json"] = json.dumps(meta).encode()
        plain.write_bytes(_tgz(files))
        self.plain = plain

    def run(self, script: str, *, refuse: str = "") -> subprocess.CompletedProcess:
        h = self.h
        prog = "\n".join([*prelude(script, h, plain=self.plain, extra=_tool(self.blob, refuse=refuse), funcs=FUNCS),
                          *_stubs(h.log), 'cmd_restore "$@"'])
        pending = h.backups / "restore-pending.tgz.enc"
        pending.write_bytes(h.src.read_bytes())
        return subprocess.run(["bash", "-c", prog, "x", "--yes", "--release", str(self.asset), str(pending)],
                              capture_output=True, text=True, timeout=60,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin", "TMPDIR": str(h.tmp)})

    def code(self) -> str:
        return (self.opt / "awgbot" / "__version__.py").read_text(encoding="utf-8")


def test_the_transition_swaps_code_after_the_stop_and_merges_with_the_new_script(script, tmp_path):
    h = Host(tmp_path)
    rel = Release(script, h)
    r = rel.run(script)
    assert r.returncode == 0, r.stderr
    assert h.live() == NEW, "живое не стало бэкапом"
    assert NEW_VER in rel.code() and not (rel.opt / ".prev-code").exists(), "код выпуска бэкапа не лёг"
    c = h.calls()
    stop = c.index("systemctl stop awg-bot")
    assert c.index(next(x for x in c if x.startswith("py tools.restore_release"))) < stop
    new = c.index("new-script __restore_after_update")
    assert stop < new < c.index("build_venv") < c.index(next(x for x in c if x.startswith("py tools.restore_merge --copy")))
    assert c.index("update_guard_disarm") < stop, "сторож прежнего обновления пережил смену кода"
    assert (h.data / "installed-digest").read_text(encoding="utf-8") == f"{NEW_VER} {rel.hex}\n"
    marker = json.loads((h.data / "restore-done.json").read_text(encoding="utf-8"))
    assert marker["version"] == NEW_VER
    assert not rel.asset.exists(), "архив выпуска остался в каталоге бэкапов"
    assert c[-1] == "systemctl start awg-bot"


@pytest.mark.parametrize("how, reason", [
    ("merge", "слияние не прошло: база бэкапа не читается"),
    ("env", "окружение выпуска v3.4.1 не собралось"),
])
def test_a_refusal_after_the_swap_brings_old_code_and_env_back(script, tmp_path, how, reason):
    h = Host(tmp_path)
    rel = Release(script, h, merge_fails=how == "merge", env_fails=how == "env")
    r = rel.run(script)
    assert r.returncode != 0
    assert h.live() == OLD, "живое тронуто"
    assert OLD_VER in rel.code() and (rel.opt / "awg-bot.sh").read_text(encoding="utf-8") == "# old script\n"
    assert not (rel.opt / ".prev-code").exists()
    c = h.calls()
    assert "venv_rollback" in c and c[-1] == "systemctl start awg-bot", c
    assert f"ничего не изменилось, работает v{OLD_VER}" in r.stderr
    failed = json.loads((h.data / "restore-failed.json").read_text(encoding="utf-8"))
    assert failed["release"] == NEW_VER and failed["from"] == OLD_VER and failed["failed"].startswith(reason)
    assert not (h.data / "installed-digest").exists() and not (h.data / "restore-done.json").exists()
    assert not rel.asset.exists()


def test_a_release_refusal_goes_before_the_stop(script, tmp_path):
    h = Host(tmp_path)
    rel = Release(script, h)
    r = rel.run(script, refuse="архив выпуска v3.4.1 не совпал с бэкапом (sha256 другой) — переходить на него не буду")
    assert r.returncode != 0
    assert "systemctl stop awg-bot" not in h.calls() and OLD_VER in rel.code()
    failed = json.loads((h.data / "restore-failed.json").read_text(encoding="utf-8"))
    assert "sha256 другой" in failed["failed"]
    assert "systemctl restart awg-bot" in h.calls(), "бот не узнает об отказе"
    assert not rel.asset.exists()


def test_the_state_reaches_the_second_half_under_its_own_names(script):
    """Скрипт обнуляет RESTORE_YES/TMP/PENDING/PLAN_FILE при загрузке — exec
    передаёт их под AWG_RESTORE_*; договор между версиями."""
    body = _extract_func(script, "cmd_restore")
    after = _extract_func(script, "cmd_restore_after_update")
    for name in ("YES", "TMP", "PENDING", "PLAN_FILE"):
        assert f"AWG_RESTORE_{name}=" in body and f'"${{AWG_RESTORE_{name}:-' in after, name
