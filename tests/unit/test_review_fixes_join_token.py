"""Токен подключения шлюза — по цепочке установщик → awg-bot.sh → агент
только окружением (AWG_JOIN_TOKEN), не в argv: argv виден в ps всем
пользователям хоста и попадает в журнал sudo.

Прогоняются настоящие куски: разбор ключей установщика и передача их
установщику из поставки, cmd_reconfigure --join и cmd_join из awg-bot.sh с
настоящим bot_tool (python окружения подставной — пишет свой argv и
окружение), main из tools/join.py.

Цена ошибки: токен в списке процессов и в /var/log/auth.log — любой, кто его
прочёл в течение часа, держит линк слота на ключах токена.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install" / "awg-bot-install.sh"
BOT = ROOT / "awg-bot.sh"
TOKEN = "AWGJ1.DUMMYTOKENDUMMYTOKEN"


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


# ── установщик: разбор ключей и передача установщику из поставки ────────────

def _installer(tmp_path, *args: str, env_token: str = "") -> tuple[list[str], dict]:
    """Разбор ключей настоящего установщика, затем вложенный вызов с
    PASS_ARGS — как установщик из трубы зовёт установщик из поставки; тот
    пишет свой argv и AWG_JOIN_TOKEN."""
    src = INSTALLER.read_text(encoding="utf-8")
    frag = src[src.index('ROLE="${AWG_BOT_ROLE:-client}"'):src.index('if [[ -n "$JOIN_TOKEN" ]]; then\n    # ровно')]
    child = tmp_path / "child.sh"
    child.write_text('#!/usr/bin/env bash\nfor a in "$@"; do echo "ARG $a"; done\necho "TOK=${AWG_JOIN_TOKEN-}"\n',
                     encoding="utf-8")
    prog = ("set -euo pipefail\n"
            'die() { echo "DIE $*" >&2; exit 1; }\njoin_parse() { [[ "$1" == AWGJ1.* ]]; }\n'
            'join_bad() { die "плохой токен"; }\n'
            'JOIN_TOKEN=""; ASSUME_YES=0\n' + frag
            + 'echo "EXTRA ${EXTRA[*]}"\n'
            + f'bash "{child}" --skip-verify ${{PASS_ARGS[@]+"${{PASS_ARGS[@]}}"}}\n')
    env = {"PATH": "/usr/bin:/bin"}
    if env_token:
        env["AWG_JOIN_TOKEN"] = env_token
    r = subprocess.run(["bash", "-c", prog, "installer", *args], capture_output=True, text=True, timeout=20,
                       env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    return [ln[4:] for ln in lines if ln.startswith("ARG ")], {
        "tok": next(ln[4:] for ln in lines if ln.startswith("TOK=")),
        "extra": next(ln[6:] for ln in lines if ln.startswith("EXTRA ")).split()}


@pytest.mark.parametrize("how", ["argv", "env"])
def test_the_installer_hands_the_token_on_only_in_the_environment(tmp_path, how):
    args = ["--yes", "--join", TOKEN] if how == "argv" else ["--yes", "--join"]
    argv, out = _installer(tmp_path, *args, env_token=TOKEN if how == "env" else "")
    assert TOKEN not in " ".join(argv), f"токен во вложенном argv: {argv}"
    assert argv == ["--skip-verify", "--yes", "--join"], argv
    assert out["tok"] == TOKEN, "установщик из поставки токена не получил"
    assert out["extra"] == ["--join"], "reconfigure --first-run получит токен в argv"


# ── awg-bot.sh: reconfigure --join и join ───────────────────────────────────

class _Bot:
    """Функции awg-bot.sh под bash; python окружения пишет argv и токен."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.inst = tmp_path / "opt"
        (self.inst / "awgbot").mkdir(parents=True)
        (self.inst / "awgbot" / "__main__.py").write_text("# main\n", encoding="utf-8")
        (self.inst / "venv" / "bin").mkdir(parents=True)
        self.log = tmp_path / "py.log"
        py = self.inst / "venv" / "bin" / "python"
        py.write_text(f'#!/bin/sh\necho "argv=$*" >> "{self.log}"\necho "tok=${{AWG_JOIN_TOKEN-}}" >> "{self.log}"\n',
                      encoding="utf-8")
        py.chmod(0o755)
        self.conf = tmp_path / "etc" / "conf"; self.conf.mkdir(parents=True)
        (self.conf / "app.yaml").write_text('role: "gateway"\n', encoding="utf-8")

    def run(self, body: str, env_token: str = "") -> subprocess.CompletedProcess:
        src = flat()
        stubs = "".join(f"{n}() {{ :; }}\n" for n in (
            "require_root", "ensure_awg_kernel", "ensure_python", "ensure_venv_ready", "build_venv", "seed_conf",
            "configure_gateway", "install_unit", "systemctl", "cleanup_delivery", "warn", "log"))
        prog = ("set -euo pipefail\n"
                f'INSTALL_DIR="{self.inst}"; ETC_DIR="{self.tmp / "etc"}"; CONF_DIR="{self.conf}"\n'
                f'ENV_FILE="{self.tmp / "etc" / "env"}"; DATA_DIR="{self.tmp / "data"}"; SERVICE=awg-bot; GW_JOIN=""\n'
                'die() { echo "DIE $*" >&2; exit 1; }\n' + stubs
                + "\n".join(_func(src, n) for n in ("yaml_get", "is_gateway_role", "bot_tool", "cmd_reconfigure",
                                                     "cmd_join"))
                + "\n" + body + "\n")
        env = {"PATH": "/usr/bin:/bin"}
        if env_token:
            env["AWG_JOIN_TOKEN"] = env_token
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20, env=env)

    def py(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def test_reconfigure_from_the_installer_starts_the_agent_with_the_token_in_the_environment(tmp_path):
    bot = _Bot(tmp_path)
    r = bot.run("cmd_reconfigure --first-run --role gateway --join", env_token=TOKEN)
    assert r.returncode == 0, r.stdout + r.stderr
    assert bot.py() == ["argv=-m tools.join", f"tok={TOKEN}"], bot.py()


@pytest.mark.parametrize("how", ["env", "argv"])
def test_join_passes_the_token_to_the_agent_only_in_the_environment(tmp_path, how):
    """`awg-bot join` — токен окружением (установщик, обновление с
    подключением) или строкой руками; python его получает только окружением."""
    bot = _Bot(tmp_path)
    r = bot.run("cmd_join" if how == "env" else f"cmd_join {TOKEN}", env_token=TOKEN if how == "env" else "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert bot.py() == ["argv=-m tools.join", f"tok={TOKEN}"], bot.py()


def test_join_without_a_token_says_how_and_status_needs_none(tmp_path):
    bot = _Bot(tmp_path)
    r = bot.run("cmd_join")
    assert r.returncode != 0 and "usage: awg-bot join" in r.stderr and bot.py() == []
    r = bot.run("cmd_join --status")
    assert r.returncode == 0 and bot.py() == ["argv=-m tools.join --status", "tok="], bot.py()


# ── tools/join.py: токен из окружения ───────────────────────────────────────

def test_the_agent_tool_reads_the_token_from_the_environment(monkeypatch, capsys):
    from tools import join as tjoin
    got = []
    monkeypatch.setattr(tjoin, "cmd_join", lambda text: got.append(text) or 0)
    monkeypatch.setattr(tjoin, "cmd_status", lambda: got.append("status") or 0)
    monkeypatch.setenv("AWG_JOIN_TOKEN", TOKEN)
    assert tjoin.main([]) == 0 and got == [TOKEN]
    assert tjoin.main(["--status"]) == 0 and got[-1] == "status"
    assert tjoin.main(["curl", "…", "--join", "AWGJ1.HAND"]) == 0 and got[-1] == "curl … --join AWGJ1.HAND", \
        "строка руками больше не принимается"
    monkeypatch.delenv("AWG_JOIN_TOKEN")
    assert tjoin.main([]) == 2 and "AWG_JOIN_TOKEN" in capsys.readouterr().err
