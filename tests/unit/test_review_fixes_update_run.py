"""`awg-bot update <архив>` целиком: настоящий скрипт от распаковки до exec в
скрипт поставки, вторая половина, сторож, удержание набора и его откат.

Скрипт — копия awg-bot.sh с путями во временном каталоге; в архиве поставки
— та же копия (exec уходит в неё по-настоящему). Подменены только внешние
команды хоста (systemctl, systemd-run, timedatectl, chown, `mv -t` — у BSD mv
его нет) и тяжёлые шаги второй половины (сборка ядра — скриптом поставки,
pip — созданием каталога окружения; переключение окружения настоящее).

Цена ошибки: хвост первой половины (перенос в .prev-code, AWG_UPDATE_PREV,
exec __post_update) — единственный мост между версиями: потеряй его, и
обновление идёт без сторожа, а откат набора — без прежнего кода; отказ «откат
невозможен» после вопроса «Удалить данные?» — человек уже ответил «y» зря;
прежний код, получивший юнит с неизвестным ему шагом, — мусор в журнале на
каждом старте.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.botscript import flat

from tests.unit.test_update_guard import _HEAVY, _Host

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "awg-bot.sh"
UNIT_LITERAL = "/etc/systemd/system/$SERVICE.service"


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _patched(tmp: Path, inst: Path, etc: Path, data: Path, hook: Path) -> str:
    """awg-bot.sh с путями во временном каталоге и подменами из hook."""
    src = flat()
    for var, val in (("INSTALL_DIR", inst), ("ETC_DIR", etc), ("DATA_DIR", data),
                     ("UNIT_PATH", tmp / "awg-bot.service"), ("SELF_LINK", tmp / "awg-bot")):
        src, n = re.subn(rf'^{var}="[^"]*"', f'{var}="{val}"', src, count=1, flags=re.M)
        assert n == 1, var
    src, n = re.subn(r"^require_root\(\) \{.*$", "require_root() { :; }", src, count=1, flags=re.M)
    assert n == 1
    src = src.replace(UNIT_LITERAL, str(tmp / "awg-bot.service"))
    # pid каждого глагола — в журнал: замок установки и update-running
    # переходят во вторую половину только через exec (тот же процесс)
    return src.replace('VERB="${1:-}"; shift || true',
                       f'source "{hook}"\nVERB="${{1:-}}"; shift || true\necho "pid $VERB $$" >> "$CALLS"', 1)


class _Box:
    """Установленная v3.4.0 (код «old», окружение A) и поставки в архивах."""

    def __init__(self, tmp: Path, *, floor: str = "3.4.0"):
        self.tmp, self.floor = tmp, floor
        self.inst, self.etc, self.data = tmp / "opt", tmp / "etc", tmp / "data"
        self.conf = self.etc / "conf"
        for d in (self.inst, self.conf, self.data, tmp / "t"):
            d.mkdir(parents=True, exist_ok=True)
        self.calls = tmp / "calls.log"; self.calls.write_text("", encoding="utf-8")
        self.bin = tmp / "bin"; self.bin.mkdir()
        _exe(self.bin / "systemctl", '#!/bin/sh\necho "systemctl $*" >> "$CALLS"\n'
             'case "$1 $3" in "is-active "*.timer) exit 3 ;; esac\nexit 0\n')
        _exe(self.bin / "systemd-run", '#!/bin/sh\necho "systemd-run $*" >> "$CALLS"\nexit 0\n')
        _exe(self.bin / "timedatectl", "#!/bin/sh\necho Asia/Yekaterinburg\n")
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        _exe(self.bin / "chown", "#!/bin/sh\nexit 0\n")
        _exe(self.bin / "mv", '#!/bin/sh\nif [ "$1" = "-t" ]; then d="$2"; shift 2; exec /bin/mv "$@" "$d"; fi\n'
                              'exec /bin/mv "$@"\n')
        hook = tmp / "hook.sh"
        heavy = [n for n in _HEAVY if n != "build_venv"]
        hook.write_text(
            "".join(f"{n}() {{ :; }}\n" for n in heavy)
            + "note_installed_digest() { :; }\n"
            + 'confirm() { echo "ASKED $1" >> "$CALLS"; return 1; }\n'
            # pip — каталогом окружения; переключение ссылки venv — настоящее
            + 'venv_build_next() { VENV_NEXT="$INSTALL_DIR/.venvs/v$RANDOM$$"; mkdir -p "$VENV_NEXT/bin"; '
              ': > "$VENV_NEXT/bin/python"; chmod +x "$VENV_NEXT/bin/python"; }\n', encoding="utf-8")
        self.script = _patched(tmp, self.inst, self.etc, self.data, hook)
        self._tree(self.inst, "3.4.0", "old")
        self.env_a = self.inst / ".venvs" / "A"; (self.env_a / "bin").mkdir(parents=True)
        _exe(self.env_a / "bin" / "python", "#!/bin/sh\nexit 0\n")
        (self.inst / "venv").symlink_to(self.env_a)
        self.app = self.conf / "app.yaml"
        self.app.write_text('role: "client"\n', encoding="utf-8")

    def _files(self, version: str, tag: str) -> dict[str, tuple[bytes, int]]:
        return {
            "awgbot/__version__.py": (f'__version__ = "{version}"\n'.encode(), 0o644),
            "awgbot/core/compat.py": (f'COMPAT_FLOOR = "{self.floor}"\n'.encode(), 0o644),
            "awgbot/__main__.py": (b"# main\n", 0o644),
            "whose": (tag.encode(), 0o644),
            "awg-bot.sh": (self.script.encode(), 0o755),
            "install/awg.lock": (b"AWG_GENERATION=2\n", 0o644),
            "install/awg-kernel-install.sh": (f'#!/bin/bash\necho "kernel $1 {tag}" >> "$CALLS"\n'.encode(), 0o755),
        }

    def _tree(self, root: Path, version: str, tag: str) -> None:
        for name, (raw, mode) in self._files(version, tag).items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)
            p.chmod(mode)

    def tgz(self, version: str, tag: str) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, (raw, mode) in self._files(version, tag).items():
                ti = tarfile.TarInfo(f"awg-bot/{name}"); ti.size = len(raw); ti.mode = mode
                tar.addfile(ti, io.BytesIO(raw))
        path = self.tmp / f"awg-bot-{version}.tgz"
        path.write_bytes(buf.getvalue())
        return path

    def run(self, *args: str, hold: str = "", keep: bool = True) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "CALLS": str(self.calls),
               "HOME": str(self.tmp), "TMPDIR": str(self.tmp / "t")}
        if hold:
            env["AWG_UPDATE_HOLD"] = hold
        if keep:
            env["AWG_UPDATE_KEEP"] = "1"
        return subprocess.run(["bash", str(self.inst / "awg-bot.sh"), *args], capture_output=True, text=True,
                              timeout=60, env=env)

    def update(self, version: str, tag: str, **kw) -> subprocess.CompletedProcess:
        r = self.run("update", str(self.tgz(version, tag)), **kw)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def alive(self, version: str) -> None:
        """Новый процесс вышел на связь: отметка жизни и фиксация (--alive)."""
        (self.data / "update-ok").write_text(f"{version} 2026-10-19T12:00:00+05:00\n", encoding="utf-8")
        r = self.run("__update_guard", "--alive")
        assert r.returncode == 0, r.stdout + r.stderr

    def log(self) -> list[str]:
        return [ln for ln in self.calls.read_text(encoding="utf-8").splitlines() if ln]

    def whose(self, root: Path | None = None) -> str:
        return ((root or self.inst) / "whose").read_text(encoding="utf-8")

    def venv(self) -> Path:
        return Path(os.path.realpath(self.inst / "venv"))

    def guard(self) -> dict:
        text = (self.data / "update-guard").read_text(encoding="utf-8")
        return dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)


@pytest.fixture()
def box(tmp_path):
    return _Box(tmp_path)


# ── honest-1: ручное обновление целиком ─────────────────────────────────────

def test_a_manual_update_execs_the_new_script_and_arms_the_guard(box):
    """Первая половина: ядро поставки — до остановки, прежний сторож снят
    перед остановкой, прежний код — в .prev-code; exec в скрипт поставки
    (AWG_UPDATE_PREV доехал): сторож взведён на прежнюю v3.4.0, окружение
    новое, прежнее записано для отката. Отметка жизни — прежнее прочь."""
    box.update("3.4.1", "new")
    log = box.log()
    assert box.whose() == "new" and box.whose(box.inst / ".prev-code") == "old"
    kernel = log.index("kernel install new")
    disarm = log.index("systemctl stop awg-bot-update-guard.timer")
    assert kernel < disarm < log.index("systemctl stop awg-bot"), log
    arm = [c for c in log if c.startswith("systemd-run")]
    assert len(arm) == 1 and arm[0].endswith(f"{box.inst}/awg-bot.sh __update_guard"), \
        "вторая половина без сторожа: прежний код не вернётся, если новый не выйдет на связь"
    assert box.guard()["VERSION"] == "3.4.1" and box.guard()["PREV"] == "3.4.0"
    pids = {c.split()[1]: c.split()[2] for c in log if c.startswith("pid ")}
    assert pids["update"] == pids["__post_update"], "вторая половина — не exec: замок установки не перешёл"
    assert box.venv() != box.env_a.resolve() and box.env_a.is_dir(), "прежнее окружение снято до отметки"
    assert log.index("systemctl stop awg-bot") < log.index("systemctl start awg-bot")
    box.alive("3.4.1")
    assert not (box.inst / ".prev-code").exists() and not box.env_a.exists()
    assert not (box.data / "update-guard").exists() and not (box.data / "update-running").exists()
    assert box.whose() == "new"
    assert os.listdir(box.tmp / "t") == [], "распакованная поставка осталась во временном каталоге"


def test_a_fleet_chain_holds_the_very_first_code_and_the_revert_brings_it_back(box):
    """Шаг набора (AWG_UPDATE_HOLD) вышел на связь — прежнее в удержании;
    вторая ступень той же команды — промежуточное прочь, первое держится;
    откат набора — код и окружение v3.4.0 на месте, без сети."""
    box.update("3.4.1", "mid", hold="a1b2c3d4")
    assert box.guard()["HOLD"] == "a1b2c3d4"
    box.alive("3.4.1")
    assert box.whose(box.inst / ".hold-code") == "old" and box.env_a.is_dir()
    env_mid = box.venv()
    box.update("3.4.2", "third", hold="a1b2c3d4")
    assert box.whose(box.inst / ".hold-code") == "old", "удержание набора задето второй ступенью"
    box.alive("3.4.2")
    assert not (box.inst / ".prev-code").exists() and not env_mid.exists(), "промежуточная ступень осталась"
    assert box.whose() == "third" and box.whose(box.inst / ".hold-code") == "old"
    box.calls.write_text("", encoding="utf-8")
    r = box.run("__update_revert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert box.whose() == "old" and box.venv() == box.env_a.resolve()
    assert not (box.inst / ".hold-code").exists() and not (box.data / "update-hold").exists()
    log = box.log()
    assert log.index("systemctl stop awg-bot") < log.index("systemctl start awg-bot")


def test_a_manual_update_drops_a_fleet_hold(box):
    """Ручной update поверх удержания набора — удержание прочь: откатываться
    туда больше незачем, а держать два прежних кода — место на диске."""
    box.update("3.4.1", "mid", hold="a1b2c3d4")
    box.alive("3.4.1")
    box.update("3.4.2", "third")
    assert not (box.inst / ".hold-code").exists() and not (box.data / "update-hold").exists()
    assert box.whose(box.inst / ".prev-code") == "mid"


# ── upd-5: отказ «откат невозможен» — до вопросов ───────────────────────────

def test_an_archive_below_the_compat_floor_is_refused_before_any_question(box):
    r = box.run("update", str(box.tgz("3.3.1", "older")), keep=False)
    assert r.returncode != 0
    assert "откат ниже 3.4.0 невозможен — только чистая установка" in r.stdout + r.stderr
    log = box.log()
    assert not [c for c in log if c.startswith("ASKED")], f"вопрос задан до отказа: {log}"
    assert "systemctl stop awg-bot" not in log and box.whose() == "old"
    assert os.listdir(box.tmp / "t") == [], "распакованная поставка осталась после отказа"


def test_a_plain_update_still_asks_about_the_data(box):
    """Контроль: поставка в линии совместимости, руками без KEEP — вопрос
    об удалении данных задан (после распаковки и проверки версии)."""
    box.update("3.4.1", "new", keep=False)
    assert [c for c in box.log() if c.startswith("ASKED")][:1] == [
        f"ASKED Удалить пользовательские данные (БД в {box.data} + секреты {box.etc}/env + конфиг)?"]


# ── honest-8: самообновление кнопкой идёт тем же путём ──────────────────────

def test_the_self_update_spawns_the_same_update_verb_outside_the_cgroup(monkeypatch):
    """Кнопка «⬆️ Обновить» и шаг набора зовут тот же `awg-bot update <архив>`
    (а он — сторожа) транзиентным юнитом; архив — в файле, шаг — в окружении."""
    from awgbot.infra import detach
    from awgbot.infra.updates import github
    got = []
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, **kw: got.append((argv, kw)))
    github.apply(b"DUMMY archive", hold="a1b2c3d4", sha256="c" * 64)
    (argv, kw), = got
    try:
        assert argv[0] == github._AWG_BOT_BIN and argv[1] == "update"
        assert Path(argv[2]).read_bytes() == b"DUMMY archive"
        assert kw["unit_prefix"] == "awg-bot-selfupdate"
        assert kw["env"] == {"AWG_UPDATE_CLEANUP": argv[2], "AWG_UPDATE_HOLD": "a1b2c3d4",
                             "AWG_BOT_VERIFIED_SHA256": "c" * 64}
    finally:
        os.remove(argv[2])


def test_a_failure_after_the_guard_is_armed_disarms_it_and_gives_the_old_one_back(tmp_path):
    """Сторож уже взведён, а сервис на новом коде не стартовал (systemctl
    start отказал): ловушка возвращает прежнее, ключ пояса и снимает сторожа
    этого прогона — иначе через 10 минут он «откатил» бы уже прежний код."""
    host = _Host(tmp_path, prev="3.3.9", floor="3.3.0")
    flag = tmp_path / "start.failed"
    (host.bin / "systemctl").write_text(
        '#!/bin/sh\necho "systemctl $*" >> "$CALLS"\n'
        f'if [ "$1 $2" = "start awg-bot" ] && [ ! -f "{flag}" ]; then : > "{flag}"; exit 1; fi\n'
        'case "$1 $3" in "is-active "*.timer) exit 3 ;; esac\nexit 0\n', encoding="utf-8")
    r = host.run("__post_update", "0")
    assert r.returncode != 0, "сценарий собран не так: старт не отказал"
    log = host.log()
    arm = next(i for i, c in enumerate(log) if c.startswith("systemd-run"))
    assert any(c == "systemctl stop awg-bot-update-guard.timer" for c in log[arm:]), "сторож прогона не снят"
    assert not (host.data / "update-guard").exists()
    assert host.whose() == "old" and host.app.read_text(encoding="utf-8") == host.app_text
