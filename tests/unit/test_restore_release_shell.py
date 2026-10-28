"""
`awg-bot restore --release <архив>`: восстановление с переходом на выпуск
бэкапа. Выпуск проверяется (tools.restore_release) и распаковывается
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
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.botscript import flat

from tests.unit.test_restore_safety import NEW, OLD, Host, _tgz, prelude
from tests.unit.test_cli_script import _extract_func

pytestmark = pytest.mark.unit

FUNCS = ("restore_release_tree", "restore_code_swap", "cmd_restore_after_update", "restore_prev_code",
         "update_done", "update_env_steps")
OLD_VER, NEW_VER = "3.4.2", "3.4.1"


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _stubs(log: Path, *, env_fails: bool = False, real=()) -> list[str]:
    """Шаги второй половины update, которые трогают систему, — в журнал
    (real — эти остаются настоящими)."""
    names = ("update_guard_disarm", "ensure_python", "ensure_ping", "install_unit", "ensure_host_autostart",
             "ensure_awg_module_loaded", "ensure_awg_generation", "adopt_resolver", "seed_conf", "validate_config",
             "venv_rollback", "venv_prune", "prune_old_kernel_builds", "gw_plumbing_sync")
    out = [f'{n}() {{ echo "{n}" >> "{log}"; return 0; }}' for n in names if n not in real]
    out.append(f'build_venv() {{ echo "build_venv" >> "{log}"; '
               + ('die "pip: нет сети"; ' if env_fails else "") + "}")
    return out


def _version_line() -> str:
    """Строка версии, которую tools.restore_release печатает после версии,
    когда сам скачал выпуск (без --archive), — настоящим текстом."""
    from awgbot.domain.fleet import restore as fr
    from awgbot.domain.notices import backup as nb
    from awgbot.domain.notices import fleet as nf
    return nb.plain(nf.restore_version_line(fr.DOWN, OLD_VER, NEW_VER))


def _tool(asset: bytes, *, refuse: str = "", github: Path | None = None) -> list[str]:
    """tools.restore_release как настоящий: с --archive (выпуск уже скачан
    ботом) — архив в --out и только версия; без него — «скачивание» с GitHub
    (файл github) в --out, версия и строка версии для плана."""
    if refuse:
        return [f'    tools.restore_release) echo "{refuse}" >&2; return 1 ;;']
    return ['    tools.restore_release)',
            '      local out="" arch=""; while [[ $# -gt 0 ]]; do case "$1" in --out) out="$2" ;; '
            '--archive) arch="$2" ;; esac; shift; done',
            '      if [[ -n "$arch" ]]; then cp "$arch" "$out"; echo "' + NEW_VER + '"; else',
            f'        cp "{github}" "$out"; echo "{NEW_VER}"; echo "{_version_line()}"; fi ;;']


class Release:
    """Установка vC в opt/ и архив выпуска vV, в котором скрипт — те же
    функции awg-bot.sh с подменами; mv -t — через прокладку (у BSD mv его нет)."""

    def __init__(self, script: str, h: Host, *, merge_fails: bool = False, env_fails: bool = False,
                 compat: str = "", kernel: bool = False, real=(), **pre):
        self.h = h
        self.real, self.pre = tuple(real), pre
        funcs = FUNCS + self.real
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
                                *prelude(script, h, merge_fails=merge_fails, funcs=funcs, **pre),
                                *_stubs(h.log, env_fails=env_fails, real=self.real),
                                'case "${1:-}" in __restore_after_update) cmd_restore_after_update ;; esac'])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            files = [("awg-bot/awgbot/__version__.py", f'__version__ = "{NEW_VER}"\n'.encode(), 0o644),
                     ("awg-bot/awgbot/__main__.py", b"# new\n", 0o644),
                     ("awg-bot/awg-bot.sh", new_script.encode(), 0o755)]
            if compat:
                files.append(("awg-bot/awgbot/core/compat.py", f'COMPAT_FLOOR = "{compat}"\n'.encode(), 0o644))
            if kernel:
                files += [("awg-bot/install/awg.lock", b"AWG_GENERATION=1\n# new\n", 0o644),
                          ("awg-bot/install/awg-kernel-install.sh",
                           f'echo "kernel $1 $(tail -n1 "$AWG_LOCK")" >> "{h.log}"\n'.encode(), 0o755)]
            for name, raw, mode in files:
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

    def run(self, script: str, *, refuse: str = "", release: bool = True) -> subprocess.CompletedProcess:
        """release — выпуск уже скачан ботом (--release <архив>, из чата);
        False — терминал качает его сам (tools.restore_release без --archive)."""
        h = self.h
        github = h.tmp / "github-asset.tgz"
        github.write_bytes(self.blob)
        prog = "\n".join([*prelude(script, h, plain=self.plain,
                                   extra=_tool(self.blob, refuse=refuse, github=github),
                                   funcs=FUNCS + self.real, **self.pre),
                          *_stubs(h.log, real=self.real), 'cmd_restore "$@"'])
        pending = h.backups / "restore-pending.tgz.enc"
        pending.write_bytes(h.src.read_bytes())
        args = ["--yes", *(["--release", str(self.asset)] if release else []), str(pending)]
        return subprocess.run(["bash", "-c", prog, "x", *args],
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
    # прежнее (код, окружение, сборки ядра) — только после того, как сервис на выпуске бэкапа поднялся
    assert c.index("systemctl start awg-bot") < c.index("prune_old_kernel_builds") == len(c) - 1, c


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


def test_the_state_reaches_the_second_half_under_its_own_names(script, tmp_path):
    """Скрипт обнуляет RESTORE_YES/TMP/PENDING/PLAN_FILE при загрузке — exec
    передаёт их под AWG_RESTORE_*: вторая половина (скрипт выпуска) убирает
    файл из чата и план вопроса, временный каталог операции, а при отказе
    пишет маркер для бота (он есть только при --yes)."""
    h = Host(tmp_path)
    plan = h.backups / "restore-pending.plan.json"
    plan.write_text("{}", encoding="utf-8")
    rel = Release(script, h)
    assert rel.run(script).returncode == 0
    assert not (h.backups / "restore-pending.tgz.enc").exists(), "файл из чата остался в каталоге бэкапов"
    assert not plan.exists(), "план вопроса остался"
    blocks = next(c for c in h.calls() if c.startswith("py tools.restore_merge --apply-blocks "))
    op_tmp = Path(blocks.split()[-1]).parent.parent                 # RESTORE_TMP/copy/restore-merge.json
    assert not op_tmp.exists(), f"временный каталог операции (расшифрованный бэкап) остался: {op_tmp}"
    h2 = Host(tmp_path / "refused")
    rel2 = Release(script, h2, merge_fails=True)
    assert rel2.run(script).returncode != 0
    assert (h2.data / "restore-failed.json").exists(), "отказ во второй половине — без маркера для бота"


@pytest.mark.skipif(shutil.which("flock") is None, reason="нет flock")
def test_restore_takes_the_install_lock_and_refuses_while_it_is_held(script, tmp_path):
    """Замок установки занят (обновление, сторож, откат) — отказ до остановки,
    «ничего не изменилось»; вторая половина замок не берёт заново — наследует."""
    import os
    import signal
    import time
    h = Host(tmp_path)
    rel = Release(script, h)
    ready = tmp_path / "held"
    # flock держит замок в дочернем процессе команды: убить один flock мало —
    # его sleep остался бы с замком, и повтор ниже упёрся бы в него же.
    # Держатель — своей группой, снимаем её целиком
    holder = subprocess.Popen(["flock", str(h.data / "update-guard.lock"), "sh", "-c",
                               f'touch "{ready}"; exec sleep 30'], start_new_session=True)
    try:
        for _ in range(100):                         # ждём факта: замок взят
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "держатель замка не взял его"
        r = rel.run(script)
    finally:
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()
    assert r.returncode != 0 and "ничего не изменилось" in r.stderr
    assert "systemctl stop awg-bot" not in h.calls() and OLD_VER in rel.code() and h.live() == OLD
    after = _extract_func(script, "cmd_restore_after_update")
    assert "update_lock" not in after, "вторая половина берёт замок повторно — ждала бы сама себя"
    # отказ убирает и скачанный ботом выпуск (как бэкап из чата) — повтор
    # кнопкой приходит с новой копией
    assert not rel.asset.exists(), "выпуск из чата пережил отказ"
    rel.asset.write_bytes(rel.blob)
    r = rel.run(script)
    assert r.returncode == 0, r.stderr


# ── правки по ревью (FIXES-uupd-2) ───────────────────────────────────────────

def _failed(h) -> dict:
    return json.loads((h.data / "restore-failed.json").read_text(encoding="utf-8"))


def test_a_jump_through_the_release_compat_floor_is_refused_before_the_stop(script, tmp_path):
    """restore-2: версия совместимости выпуска бэкапа выше установленной — его
    код данных этой версии не примет: отказ до остановки, причина в маркере."""
    h = Host(tmp_path)
    rel = Release(script, h, compat="3.4.5")
    r = rel.run(script)
    assert r.returncode != 0
    assert "systemctl stop awg-bot" not in h.calls() and OLD_VER in rel.code() and h.live() == OLD
    assert _failed(h)["failed"] == (f"выпуск v{NEW_VER} принимает переход только с v3.4.5 и новее, а здесь "
                                    f"v{OLD_VER} — сначала обнови сервер AWG до v3.4.5 кнопкой «⬆️ Обновить», "
                                    "затем восстанови бэкап")
    rel2 = Release(script, Host(tmp_path / "ok"), compat="3.4.0")
    assert rel2.run(script).returncode == 0, "версия совместимости ниже установленной — не помеха"


def test_a_refusal_returns_the_venv_link_of_the_old_code_not_a_stale_one(script, tmp_path):
    """restore-3: .venvs/.prev остался от прежнего обновления (окружение vB);
    отказ после смены кода возвращает ссылку venv на окружение vC — то, с
    которым работал прежний код."""
    h = Host(tmp_path)
    rel = Release(script, h, env_fails=True, real=("venv_rollback",))
    import shutil as _sh
    _sh.rmtree(rel.opt / "venv")
    venvs = rel.opt / ".venvs"
    (venvs / "B").mkdir(parents=True); (venvs / "C").mkdir()
    (rel.opt / "venv").symlink_to(venvs / "C")
    (venvs / ".prev").write_text(f"{venvs / 'B'}\n", encoding="utf-8")       # хвост прежнего обновления
    r = rel.run(script)
    assert r.returncode != 0 and OLD_VER in rel.code()
    import os
    assert os.path.realpath(rel.opt / "venv") == os.path.realpath(venvs / "C"), "код vC на окружении vB"


def test_a_failed_exec_of_the_release_script_goes_through_the_trap(script):
    """restore-4: неудачный exec не обрывает процесс мимо ловушки: execfail,
    затем restore_die — прежние код, окружение и ядро назад."""
    body = _extract_func(script, "cmd_restore")
    tail = body.split("shopt -s execfail", 1)[1]
    assert tail.index('exec "$INSTALL_DIR/awg-bot.sh" __restore_after_update') < tail.index('restore_die "$RESTORE_FAIL"')
    prog = ('restore_die() { echo "DIED $*"; exit 3; }\nINSTALL_DIR=/nonexistent; RESTORE_FAIL="скрипт не запустился"\n'
            "shopt -s execfail\n" + tail.split("\n    fi", 1)[0])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10)
    assert r.returncode == 3 and "DIED скрипт не запустился" in r.stdout, r.stdout + r.stderr


def _kernel_files(rel) -> None:
    inst = rel.opt / "install"; inst.mkdir(exist_ok=True)
    (inst / "awg.lock").write_text("AWG_GENERATION=1\n# old\n", encoding="utf-8")
    (inst / "awg-kernel-install.sh").write_text(f'echo "kernel $1 $(tail -n1 "$AWG_LOCK")" >> "{rel.h.log}"\n',
                                                encoding="utf-8")


def test_a_refusal_after_the_kernel_build_gives_the_old_kernel_back(script, tmp_path):
    """restore-5: ядро выпуска бэкапа собирается до остановки; отказ дальше —
    ядро прежней версии ставится обратно (тулзы и модуль — под код vC)."""
    h = Host(tmp_path)
    rel = Release(script, h, env_fails=True, kernel=True)
    _kernel_files(rel)
    r = rel.run(script)
    assert r.returncode != 0
    kernels = [c for c in h.calls() if c.startswith("kernel ")]
    assert kernels == ["kernel install # new", "kernel install # old"], h.calls()
    assert "ядро прежней версии" in r.stdout


def test_a_successful_transition_keeps_the_kernel_of_the_backup(script, tmp_path):
    h = Host(tmp_path)
    rel = Release(script, h, kernel=True)
    _kernel_files(rel)
    assert rel.run(script).returncode == 0
    assert [c for c in h.calls() if c.startswith("kernel ")] == ["kernel install # new"]


def test_the_old_code_stays_when_the_service_does_not_come_up(script, tmp_path):
    """restore-10: прежние код и сборки ядра снимаются только после того, как
    сервис на выпуске бэкапа поднялся; не поднялся — прежний код оставлен."""
    h = Host(tmp_path)
    rel = Release(script, h, start_fails=True)
    r = rel.run(script)
    assert (rel.opt / ".prev-code").is_dir(), "прежний код снят до того, как сервис поднялся"
    assert "prune_old_kernel_builds" not in h.calls()
    assert f"прежний код v{OLD_VER} оставлен" in r.stderr
    assert r.returncode == 0


def test_the_second_half_takes_the_same_steps_as_an_update(script, tmp_path):
    """restore-11: вторая половина — та же функция шагов, что у update (ping,
    резолвер, проверка conf)."""
    after = _extract_func(script, "cmd_restore_after_update")
    assert re.search(r"^\s*update_env_steps\s*(#.*)?$", after, re.M), \
        "восстановление зовёт шаги с аргументом, которого функция не читает"
    h = Host(tmp_path)
    rel = Release(script, h)
    assert rel.run(script).returncode == 0
    c = h.calls()
    for step in ("ensure_python", "ensure_ping", "build_venv", "adopt_resolver", "seed_conf", "validate_config"):
        assert step in c, step


def test_the_version_line_comes_after_the_plan_header(script, tmp_path):
    """Вне оси: `awg-bot restore <файл>` из терминала (без --release) — выпуск
    качает tools.restore_release сам и печатает строку версии; она — после
    заголовка плана, как в вопросе бота. Переход доводится тем же путём."""
    h = Host(tmp_path)
    rel = Release(script, h)
    r = rel.run(script, release=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.index("ПЛАН") < r.stdout.index(_version_line()), r.stdout
    assert h.live() == NEW and NEW_VER in rel.code(), "без --release переход не доведён"
    assert not any("--archive" in c for c in h.calls() if c.startswith("py tools.restore_release")), h.calls()


def test_a_release_from_the_chat_prints_no_version_line_of_its_own(script, tmp_path):
    """С --release (выпуск скачал бот) инструмент печатает только версию —
    строки версии в выводе нет, план — как есть."""
    h = Host(tmp_path)
    rel = Release(script, h)
    r = rel.run(script)
    assert r.returncode == 0, r.stderr
    assert "♻️ Версия" not in r.stdout and "ПЛАН" in r.stdout
    assert any("--archive" in c for c in h.calls() if c.startswith("py tools.restore_release"))


def test_a_bare_failure_still_writes_the_marker(script, tmp_path):
    """Вне оси: голый die или errexit после «RESTORE_FAIL=» — маркер отказа
    для бота всё равно пишется."""
    h = Host(tmp_path)
    funcs = "\n".join(_extract_func(script, f) for f in ("json_str", "restore_fail_marker", "restore_exit"))
    stubs = "\n".join(f"{n}() {{ :; }}" for n in ("restore_unstage", "restore_code_back", "restore_kernel_back",
                                                  "restore_prune_mark", "systemctl"))
    prog = (f'DATA_DIR="{h.data}"; RESTORE_YES=1; RESTORE_FAIL=""; RESTORE_PHASE=stopped\n'
            'RESTORE_TMP=""; RESTORE_PENDING=""; RESTORE_PLAN_FILE=""\n'
            f"{funcs}\n{stubs}\ntrap restore_exit EXIT\nfalse\nexit 1\n")
    subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10)
    assert _failed(h)["failed"] == "операция прервана (код 1)"
