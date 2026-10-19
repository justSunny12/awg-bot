"""awg-bot.sh: сторож обновления — откат в той же установке.

После старта нового сервиса прежние код (.prev-code), окружение (.venvs/.prev)
и сборка ядра живут, пока новый процесс не напишет
отметку жизни (update-ok своей версией); сторож — таймер systemd через 10
минут (__update_guard): отметка есть — прежнее прочь, нет — прежнее на место,
перезапуск и метка отката для прежнего процесса. Переход через COMPAT_FLOOR
сторожем не прикрыт — ниже версии совместимости назад нельзя.

Цена ошибки: обновление, не вышедшее на связь, оставляло хост без бота до
визита человека («назад дороги нет» после старта); сторож, снявший прежнее
раньше отметки, — то же самое; сторож, откативший живую версию, — вечный
откат. Скрипт настоящий целиком (копия с путями во временном каталоге и
вызовом глаголом), подменены systemctl, systemd-run, timedatectl, сборка
окружения и прочие тяжёлые шаги второй половины обновления.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "awg-bot.sh"

_HEAVY = ("ensure_python", "ensure_ping", "build_venv", "install_unit", "ensure_host_autostart",
          "ensure_awg_module_loaded", "ensure_awg_generation", "adopt_resolver", "seed_conf",
          "validate_config")


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _tree(root: Path, version: str, floor: str, tag: str) -> None:
    """Поставка в каталоге: версия, версия совместимости, метка «чей код»,
    подставная сборка ядра (уборку только отмечает)."""
    (root / "awgbot" / "core").mkdir(parents=True, exist_ok=True)
    (root / "awgbot" / "__version__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "awgbot" / "core" / "compat.py").write_text(f'COMPAT_FLOOR = "{floor}"\n', encoding="utf-8")
    (root / "whose").write_text(tag, encoding="utf-8")
    (root / "awgbot" / "__main__.py").write_text("# main\n", encoding="utf-8")   # полная поставка: откат её примет
    (root / "awg-bot.sh").write_text("# script\n", encoding="utf-8")
    (root / "install").mkdir(exist_ok=True)
    (root / "install" / "awg.lock").write_text("AWG_GENERATION=2\n", encoding="utf-8")
    _exe(root / "install" / "awg-kernel-install.sh",
         '#!/bin/bash\necho "kernel $1" >> "$CALLS"\n')


class _Host:
    """Установка сразу после первой половины обновления: в $INSTALL_DIR новая
    поставка, в .prev-code — прежняя, venv смотрит на новое окружение, прежнее
    записано в .venvs/.prev; app.yaml с ключом timezone, вписанным руками."""

    def __init__(self, tmp: Path, *, new: str = "3.4.1", prev: str = "3.4.0", floor: str = "3.4.0"):
        self.tmp = tmp
        self.inst, self.etc, self.data = tmp / "opt", tmp / "etc", tmp / "data"
        self.conf = self.etc / "conf"
        for d in (self.inst, self.conf, self.data):
            d.mkdir(parents=True, exist_ok=True)
        self.calls = tmp / "calls.log"; self.calls.write_text("", encoding="utf-8")
        self.new, self.prev = new, prev
        _tree(self.inst, new, floor, "new")
        _tree(self.inst / ".prev-code", prev, floor, "old")
        self.env_old = self.inst / ".venvs" / "A"; self.env_old.mkdir(parents=True)
        self.env_new = self.inst / ".venvs" / "B"; self.env_new.mkdir()
        (self.inst / "venv").symlink_to(self.env_new)
        (self.inst / ".venvs" / ".prev").write_text(f"{self.env_old}\n", encoding="utf-8")
        self.app = self.conf / "app.yaml"
        self.app_text = 'role: "client"\ntimezone: "Europe/Moscow"\n'
        self.app.write_text(self.app_text, encoding="utf-8")
        self.bin = tmp / "bin"; self.bin.mkdir()
        # is-active таймера сторожа — по TIMER_RC (по умолчанию таймера нет)
        _exe(self.bin / "systemctl", '#!/bin/sh\necho "systemctl $*" >> "$CALLS"\n'
             'case "$1 $3" in "is-active "*.timer) exit "${TIMER_RC:-3}" ;; esac\nexit 0\n')
        _exe(self.bin / "systemd-run", '#!/bin/sh\necho "systemd-run $*" >> "$CALLS"\nexit "${RUN_RC:-0}"\n')
        _exe(self.bin / "timedatectl", "#!/bin/sh\necho Asia/Yekaterinburg\n")
        _exe(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        # тяжёлые шаги второй половины — заглушками; остальное — как в скрипте
        hook = tmp / "hook.sh"
        hook.write_text("".join(f"{n}() {{ :; }}\n" for n in _HEAVY), encoding="utf-8")
        src = SCRIPT.read_text(encoding="utf-8")
        for var, val in (("INSTALL_DIR", self.inst), ("ETC_DIR", self.etc), ("DATA_DIR", self.data),
                         ("UNIT_PATH", tmp / "awg-bot.service"), ("SELF_LINK", tmp / "awg-bot")):
            src, n = re.subn(rf'^{var}="[^"]*"', f'{var}="{val}"', src, count=1, flags=re.M)
            assert n == 1, var
        src, n = re.subn(r"^require_root\(\) \{.*$", "require_root() { :; }", src, count=1, flags=re.M)
        assert n == 1
        src = src.replace('VERB="${1:-}"; shift || true',
                          f'source "{hook}"\nVERB="${{1:-}}"; shift || true', 1)
        self.script = tmp / "awg-bot.sh"
        _exe(self.script, src)

    def run(self, *args: str, run_rc: int = 0, timer_rc: int = 3, hold: bool = False) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "CALLS": str(self.calls),
               "RUN_RC": str(run_rc), "TIMER_RC": str(timer_rc),
               "AWG_UPDATE_PREV": str(self.inst / ".prev-code"), "HOME": str(self.tmp)}
        if hold:
            env["AWG_UPDATE_HOLD"] = "1"
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True,
                              timeout=30, env=env)

    def post_update(self, **kw) -> subprocess.CompletedProcess:
        r = self.run("__post_update", "0", **kw)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def guard(self, *mode: str, **kw) -> subprocess.CompletedProcess:
        self.calls.write_text("", encoding="utf-8")
        r = self.run("__update_guard", *mode, **kw)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def mark(self, version: str) -> None:
        (self.data / "update-ok").write_text(f"{version} 2026-10-18T12:00:00+03:00\n", encoding="utf-8")

    def log(self) -> list[str]:
        return [ln for ln in self.calls.read_text(encoding="utf-8").splitlines() if ln]

    def whose(self) -> str:
        return (self.inst / "whose").read_text(encoding="utf-8")

    def venv(self) -> Path:
        return Path(os.readlink(self.inst / "venv"))


def _guard_state(host: _Host) -> dict:
    text = (host.data / "update-guard").read_text(encoding="utf-8")
    return dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)


# ── после обновления: прежнее на месте до отметки ───────────────────────────

def test_after_update_the_old_code_and_env_wait_for_the_mark(tmp_path):
    host = _Host(tmp_path)
    host.mark(host.new)                                  # отметка прошлого запуска той же версии
    r = host.post_update()
    assert (host.inst / ".prev-code" / "whose").read_text(encoding="utf-8") == "old", "прежний код снят до отметки"
    assert host.env_old.is_dir() and (host.inst / ".venvs" / ".prev").exists(), "прежнее окружение снято до отметки"
    assert host.venv() == host.env_new and host.whose() == "new"
    st = _guard_state(host)
    assert (st["VERSION"], st["PREV"]) == (host.new, host.prev)
    assert (host.data / "update-guard").stat().st_mode & 0o777 == 0o600
    assert not (host.data / "update-ok").exists(), "отметка прошлого запуска сошла бы за отметку нового"
    run = [c for c in host.log() if c.startswith("systemd-run")]
    assert len(run) == 1 and "--on-active=10min" in run[0] and run[0].endswith("__update_guard"), host.log()
    assert "--unit=awg-bot-update-guard" in run[0]
    assert "kernel prune" not in host.log(), "прежняя сборка ядра снята до отметки"
    assert host.log().index(run[0]) < host.log().index("systemctl start awg-bot"), \
        "сторож взводится раньше старта: новый процесс может отметиться сразу"
    assert "сторож посмотрит через 10 минут" in r.stdout


def test_the_mark_of_this_version_commits_the_update(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    host.mark(host.new)
    host.guard()
    assert not (host.inst / ".prev-code").exists(), "прежний код остался после отметки"
    assert not host.env_old.exists() and not (host.inst / ".venvs" / ".prev").exists()
    assert host.venv() == host.env_new and host.whose() == "new"
    assert "kernel prune" in host.log(), "прежняя сборка ядра не убрана после отметки"
    assert "systemctl stop awg-bot" not in host.log(), "живой сервис остановлен"
    for name in ("update-guard", "update-ok", "update-rolled-back"):
        assert not (host.data / name).exists(), name


@pytest.mark.parametrize("mark", [None, "3.4.0", "3.4.1.1"])
def test_no_mark_of_this_version_brings_the_old_one_back(tmp_path, mark):
    """Отметки нет или она другой версии (прежний процесс, чужая сборка) —
    прежние код и окружение на место, перезапуск, метка отката."""
    host = _Host(tmp_path)
    host.post_update()
    if mark:
        host.mark(mark)
    r = host.guard()
    assert host.whose() == "old", "прежний код не вернулся"
    assert (host.inst / "awgbot" / "__version__.py").read_text(encoding="utf-8").count(host.prev)
    assert not (host.inst / ".prev-code").exists()
    assert host.venv() == host.env_old, "окружение не вернулось"
    assert not host.env_new.exists(), "окружение невышедшей версии осталось на диске"
    log = host.log()
    assert "systemctl stop awg-bot" in log and "systemctl start awg-bot" in log
    assert log.index("systemctl stop awg-bot") < log.index("systemctl start awg-bot")
    rolled = json.loads((host.data / "update-rolled-back").read_text(encoding="utf-8"))
    assert (rolled["version"], rolled["prev"]) == (host.new, host.prev) and rolled["at"]
    assert (host.data / "update-rolled-back").stat().st_mode & 0o777 == 0o600
    for name in ("update-guard", "update-ok"):
        assert not (host.data / name).exists(), name
    assert "kernel prune" not in log
    assert f"v{host.new} не вышла на связь за 10 минут — возвращаю v{host.prev}" in r.stdout + r.stderr


def test_the_guard_is_quiet_without_its_state_or_for_another_install(tmp_path):
    """Сторож не взведён (ручной вызов, повторный таймер) — ничего; установку
    сменили мимо него (новое обновление, чистая установка) — ничего не
    возвращает, только снимает своё состояние."""
    host = _Host(tmp_path)
    host.guard()
    assert host.log() == [] and host.whose() == "new" and (host.inst / ".prev-code").exists()
    host.post_update()
    (host.inst / "awgbot" / "__version__.py").write_text('__version__ = "3.4.2"\n', encoding="utf-8")
    host.guard()
    assert host.log() == [], "чужую установку сторож трогать не должен"
    assert host.whose() == "new" and not (host.data / "update-guard").exists()


# ── фиксация ботом сразу и проверка на старте сервиса (перезагрузка в окне) ──

def _age(host: _Host, minutes: int) -> None:
    """Сторож взведён `minutes` минут назад."""
    import time
    st = _guard_state(host)
    st["AT"] = str(int(time.time()) - minutes * 60)
    (host.data / "update-guard").write_text("".join(f"{k}={v}\n" for k, v in st.items()), encoding="utf-8")


def test_the_bot_commits_at_once_after_its_mark(tmp_path):
    """Бот поставил отметку и позвал --alive: прежнее снимается сразу, таймер
    снят — живой код не ждёт окна."""
    host = _Host(tmp_path)
    host.post_update()
    host.mark(host.new)
    host.guard("--alive")
    assert not (host.inst / ".prev-code").exists() and not host.env_old.exists()
    assert "kernel prune" in host.log() and "systemctl stop awg-bot-update-guard.timer" in host.log()
    assert "systemctl stop awg-bot" not in host.log()


def test_alive_without_the_mark_leaves_the_decision_to_the_timer(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    host.guard("--alive")
    assert host.log() == [] and host.whose() == "new" and (host.inst / ".prev-code").exists()
    assert (host.data / "update-guard").exists()


def test_service_start_with_a_live_timer_changes_nothing(tmp_path):
    """Старт сервиса самим обновлением или перезапуск в окне: таймер стоит — решает он."""
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 30)
    host.guard("--start", timer_rc=0)
    assert host.whose() == "new" and (host.data / "update-guard").exists()
    assert not any(c.startswith(("systemd-run", "systemctl stop", "systemctl start")) for c in host.log())


def test_reboot_inside_the_window_rearms_the_guard_for_what_is_left(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 3)
    host.guard("--start")
    run = [c for c in host.log() if c.startswith("systemd-run")]
    assert len(run) == 1 and "--on-active=7min" in run[0] and run[0].endswith("__update_guard"), host.log()
    assert host.whose() == "new" and (host.data / "update-guard").exists()


def test_reboot_after_the_window_gives_the_new_version_five_minutes(tmp_path):
    """Окно по стенным часам включает простой хоста: после загрузки новой
    версии — не меньше 5 минут выйти на связь, а не откат с порога."""
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 11)
    host.guard("--start")
    assert host.whose() == "new" and (host.data / "update-guard").exists()
    assert any("--on-active=5min" in c for c in host.log()), host.log()


def test_reboot_after_the_window_without_the_mark_rolls_back_before_start(tmp_path):
    """Таймер унесла перезагрузка, окно истекло, отметки нет, а взвести сторожа
    заново нечем — откат прямо в ExecStartPre: без systemctl для самого
    сервиса (его запуск ждал бы сам себя)."""
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 11)
    host.guard("--start", run_rc=1)
    assert host.whose() == "old" and host.venv() == host.env_old
    assert host.app.read_text(encoding="utf-8") == host.app_text
    assert (host.data / "update-rolled-back").exists() and not (host.data / "update-guard").exists()
    assert not any(c in ("systemctl stop awg-bot", "systemctl start awg-bot") for c in host.log()), host.log()


def test_reboot_after_the_window_with_the_mark_commits(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 11)
    host.mark(host.new)
    host.guard("--start")
    assert host.whose() == "new" and not (host.inst / ".prev-code").exists()
    assert "kernel prune" in host.log()


def test_the_unit_checks_the_guard_before_every_start(script):
    unit = _func(script, "install_unit")
    pre = unit.index("ExecStartPre=-$INSTALL_DIR/awg-bot.sh __update_guard --start")
    assert pre < unit.index("ExecStart=$INSTALL_DIR/venv/bin/python -m awgbot")


# ── без сторожа ─────────────────────────────────────────────────────────────

def test_crossing_the_compat_floor_has_no_guard(tmp_path):
    """3.3.1 → 3.4.0: откат вернул бы версию ниже COMPAT_FLOOR — сторожа нет,
    прежнее снимается сразу после старта, как раньше."""
    host = _Host(tmp_path, new="3.4.0", prev="3.3.1", floor="3.4.0")
    host.post_update()
    assert not any(c.startswith("systemd-run") for c in host.log()), "сторож поставлен на переходе через версию совместимости"
    assert not (host.data / "update-guard").exists()
    assert not (host.inst / ".prev-code").exists() and not host.env_old.exists()
    assert "kernel prune" in host.log()


def test_systemd_run_refusing_falls_back_to_committing_at_once(tmp_path):
    host = _Host(tmp_path)
    r = host.post_update(run_rc=1)
    assert "сторож обновления не взведён" in r.stdout + r.stderr
    assert not (host.data / "update-guard").exists()
    assert not (host.inst / ".prev-code").exists() and "kernel prune" in host.log()


def test_a_guard_still_pending_is_disarmed_first(tmp_path):
    """Обновление поверх неподтверждённого: прежний сторож снят до нового —
    таймер с тем же именем systemd-run второй раз не создал бы."""
    host = _Host(tmp_path)
    host.post_update()
    host.calls.write_text("", encoding="utf-8")
    host.post_update()
    log = host.log()
    stop = log.index("systemctl stop awg-bot-update-guard.timer")
    assert stop < next(i for i, c in enumerate(log) if c.startswith("systemd-run"))


# ── проводка: тот же сторож у ручного пути, снятие при отказе и uninstall ───

@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


def test_uninstall_disarms_the_guard(script):
    """Снятие бота снимает и сторожа: таймер через 10 минут «откатил» бы
    удалённую установку. Здесь — по исходнику: uninstall трогает хост за
    пределами временного каталога (nft, iptables, юниты), гонять его на машине
    теста нельзя. Ручной update, самообновление кнопкой, снятие прежнего
    сторожа перед остановкой и ловушка второй половины — прогоном настоящего
    скрипта (test_review_fixes_update_run)."""
    assert "update_guard_disarm" in _func(script, "cmd_uninstall")


# ── шаг набора: прежнее держится до фиксации или отката всего набора ────────

def _held(tmp_path, **kw) -> _Host:
    """Шаг набора (hold) вышел на связь: бот поставил отметку и позвал --alive."""
    host = _Host(tmp_path, **kw)
    host.post_update(hold=True)
    assert _guard_state(host)["HOLD"] == "1"
    host.mark(host.new)
    host.guard("--alive")
    return host


def test_with_hold_the_mark_lifts_the_guard_but_keeps_the_old_one(tmp_path):
    host = _held(tmp_path)
    assert not (host.data / "update-guard").exists(), "сторож остался после отметки"
    assert (host.inst / ".hold-code" / "whose").read_text(encoding="utf-8") == "old"
    assert not (host.inst / ".prev-code").exists()
    assert host.env_old.is_dir(), "прежнее окружение снято до фиксации набора"
    assert "kernel prune" not in host.log(), "сборка ядра снята до фиксации набора"
    hold = dict(ln.split("=", 1) for ln in (host.data / "update-hold").read_text(encoding="utf-8").splitlines())
    assert hold["PREV"] == host.prev and hold["ENV"] == str(host.env_old)


def test_commit_drops_the_held_old_one_and_kernel_builds(tmp_path):
    host = _held(tmp_path)
    host.calls.write_text("", encoding="utf-8")
    r = host.run("__update_commit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (host.inst / ".hold-code").exists() and not host.env_old.exists()
    assert not (host.data / "update-hold").exists()
    assert "kernel prune" in host.log() and host.whose() == "new" and host.venv() == host.env_new


def test_revert_brings_the_held_one_back_without_network(tmp_path):
    host = _held(tmp_path)
    host.calls.write_text("", encoding="utf-8")
    assert host.run("__update_revert", "--check").returncode == 0
    r = host.run("__update_revert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert host.whose() == "old" and host.venv() == host.env_old and not host.env_new.exists()
    assert host.app.read_text(encoding="utf-8") == host.app_text, "ключ пояса не вернулся"
    assert not (host.inst / ".hold-code").exists() and not (host.data / "update-hold").exists()
    log = host.log()
    assert log.index("systemctl stop awg-bot") < log.index("systemctl start awg-bot")


def test_revert_without_anything_to_go_back_to_is_code_2(tmp_path):
    host = _Host(tmp_path)
    import shutil
    shutil.rmtree(host.inst / ".prev-code")
    assert host.run("__update_revert", "--check").returncode == 2
    assert host.run("__update_revert").returncode == 2
    assert host.whose() == "new" and "systemctl stop awg-bot" not in host.log()


def test_revert_of_a_step_not_yet_alive_uses_the_guarded_old_one(tmp_path):
    host = _Host(tmp_path)
    host.post_update(hold=True)
    r = host.run("__update_revert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert host.whose() == "old" and host.venv() == host.env_old
    assert not (host.data / "update-guard").exists()


def test_a_chain_under_one_command_holds_the_very_first_old_one(tmp_path):
    """Две ступени одной командой: промежуточная снимается, когда следующая
    вышла на связь; откат набора — сразу на самое первое прежнее."""
    import shutil
    host = _held(tmp_path)
    # вторая ступень: промежуточный код (new) — в .prev-code, на месте — третий
    shutil.move(str(host.inst / "whose"), str(tmp_path / "whose-new"))
    (host.inst / ".prev-code").mkdir()
    _tree(host.inst / ".prev-code", host.new, "3.4.0", "new")
    _tree(host.inst, "3.4.2", "3.4.0", "third")
    env_third = host.inst / ".venvs" / "C"; env_third.mkdir()
    (host.inst / "venv").unlink(); (host.inst / "venv").symlink_to(env_third)
    (host.inst / ".venvs" / ".prev").write_text(f"{host.env_new}\n", encoding="utf-8")
    host.post_update(hold=True)
    host.mark("3.4.2")
    host.guard("--alive")
    assert not (host.inst / ".prev-code").exists() and not host.env_new.exists(), "промежуточная ступень осталась"
    assert (host.inst / ".hold-code" / "whose").read_text(encoding="utf-8") == "old" and host.env_old.is_dir()
    host.run("__update_revert")
    assert host.whose() == "old" and host.venv() == host.env_old and not env_third.exists()


def test_an_update_within_the_line_keeps_a_timezone_key_set_by_hand(tmp_path):
    """Ключ timezone в app.yaml с 3.4.0 — переопределение человека: обновление
    3.4.0 → 3.4.1 (и любой догон набора) его не трогает и уведомления о поясе
    не будет. Снимается он один раз — на переходе с выпуска ниже 3.4.0, где его
    писали шаблон и установщик."""
    host = _Host(tmp_path)                               # prev 3.4.0, new 3.4.1
    host.post_update()
    assert host.app.read_text(encoding="utf-8") == host.app_text, "ключ, вписанный руками, снят обновлением"
    assert not (host.data / ".tz-adopted").exists()
