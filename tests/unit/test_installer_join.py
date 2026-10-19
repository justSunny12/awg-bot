"""Установщик с командой подключения (`… | sudo bash -s -- --join AWGJ1.…`,
§3.6, Т-Т1, Т-Т9…Т-Т11 концепта «единые обновления»).

Разбор токена — на bash (base64url → od, поля по смещениям util/jointoken):
он обязан дать те же выпуск, слот и хост, что Python, иначе шлюз поставит
не тот выпуск. Качается ровно выпуск из токена, а не latest; битый токен —
отказ до сети; GitHub не ответил — отказ с отсылкой к пути без GitHub;
агент уже стоит — без меню: вопрос и сразу `awg-bot join` (или обновление
до выпуска токена и подключение следом).

Сеть подменена: curl — заглушка, пишущая, что у неё попросили.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awgbot.util import jointoken
from tests.conftest import fake_cmd

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install" / "awg-bot-install.sh"


def _token(**kw) -> str:
    base = dict(slot=2, version="3.4.0", join_id="0123456789abcdef", expires=1_800_000_000,
                host="vpn-server.example.org", link_port=8443, channel_port=8787, gw_addr="10.99.99.6",
                vps_pub=bytes(32), gw_priv=bytes(range(32)), jc=4, jmin=40, jmax=70, s=(10, 20, 0, 0),
                h=((100, 200), (300, 400), (500, 600), (700, 800)))
    base.update(kw)
    return jointoken.encode(jointoken.JoinToken(**base))


def _func(src: str, name: str) -> str:
    head = f"{name}() {{"
    assert head in src, f"нет функции {name}() — тест устарел"
    return head + src.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


def _parse(token: str) -> subprocess.CompletedProcess:
    src = INSTALLER.read_text(encoding="utf-8")
    prog = (_func(src, "join_parse") + f'join_parse "{token}" && '
            'printf "%s|%s|%s" "$JOIN_SLOT" "$JOIN_VER" "$JOIN_HOST"\n')
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("kw", [
    {},
    {"slot": 7, "version": "3.4.0.2"},
    {"host": "a" * 63},
    {"host": "203.0.113.10", "version": "12.0.15"},
])
def test_the_shell_reads_the_token_like_python(kw):
    token = _token(**kw)
    t = jointoken.decode(token)
    r = _parse(token)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{t.slot}|{t.version}|{t.host}", r.stdout


@pytest.mark.parametrize("token", ["AWGJ1.", "AWGJ2." + "A" * 60, "AWGJ1." + "A" * 60, "AWGJ1.!!!" + "A" * 60,
                                   "not a token"])
def test_the_shell_refuses_what_is_not_a_token(token):
    assert _parse(token).returncode != 0


def _piped(tmp_path, *args, curl_rc: int = 22) -> tuple[subprocess.CompletedProcess, Path]:
    """Установщик из трубы, как его запускает команда подключения; проверка
    root снята (тест не под root), curl — заглушка."""
    src = INSTALLER.read_text(encoding="utf-8")
    root_check = 'if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then'
    assert root_check in src, "проверка root сменилась — тест устарел"
    src = src.replace(root_check, "if false; then", 1)
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    calls = tmp_path / "curl.log"
    fake_cmd(bin_dir / "curl", f'#!/bin/sh\necho "$*" >> "{calls}"\nexit {curl_rc}\n')
    fake_cmd(bin_dir / "mktemp", f'#!/bin/sh\nd="{tmp_path}/tmp.$$"; mkdir -p "$d" && echo "$d"\n')
    r = subprocess.run(["bash", "-s", "--", *args], input=src, capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    return r, calls


def test_join_downloads_exactly_the_release_from_the_token(tmp_path):
    """Выпуск — из токена (Р14), не latest; Т-Т1 — строкой в начале; GitHub не
    ответил — Т-Т9 с отсылкой к пути без GitHub."""
    r, calls = _piped(tmp_path, "--join", _token(version="3.4.0.2"))
    assert r.returncode != 0
    urls = calls.read_text(encoding="utf-8").split()
    assert "https://github.com/justSunny12/awg-bot/releases/download/v3.4.0.2/awg-bot.tgz" in urls, urls
    assert not any("latest" in u for u in urls), urls
    out = r.stdout + r.stderr
    assert "подключение шлюза к серверу AWG vpn-server.example.org: слот 2, выпуск v3.4.0.2" in out, out
    assert "не скачался выпуск v3.4.0.2 с GitHub" in out and "«Шлюз без доступа к GitHub»" in out, out


def test_a_broken_token_is_refused_before_the_network(tmp_path):
    """Т-Т10: команда повреждена (обрезали при копировании) — отказ до сети."""
    token = _token()
    r, calls = _piped(tmp_path, "--join", token[:-10])
    assert r.returncode != 0
    assert "команда подключения повреждена — скопируй её из бота сервера AWG" in r.stderr, r.stderr
    assert not calls.exists(), "с битым токеном ходили в сеть"


def _existing(tmp_path, have: str, want: str, answer: str, role: str = "gateway"):
    """Фрагмент «агент уже стоит» с командой подключения: подставной
    awg-bot.sh пишет, чем его позвали."""
    src = INSTALLER.read_text(encoding="utf-8")
    start = 'if [[ -n "${JOIN_TOKEN:-}" && -x "$INSTALL_DIR/venv/bin/python" ]]; then'
    assert start in src, "ветка «агент уже стоит» сменилась — тест устарел"
    frag = start + src.split(start, 1)[1].split("\nfi\n", 1)[0] + "\nfi\n"
    frag = frag.replace("< /dev/tty", "<&3")
    inst = tmp_path / "opt"; (inst / "venv" / "bin").mkdir(parents=True); (inst / "awgbot").mkdir()
    fake_cmd(inst / "venv" / "bin" / "python", "#!/bin/sh\n")
    (inst / "awgbot" / "__version__.py").write_text(f'__version__ = "{have}"\n', encoding="utf-8")
    log = tmp_path / "calls"
    fake_cmd(inst / "awg-bot.sh", f'#!/bin/sh\necho "awg-bot $* JOIN=${{AWG_UPDATE_THEN_JOIN-}}" >> "{log}"\n')
    etc = tmp_path / "etc"; (etc / "conf").mkdir(parents=True)
    (etc / "conf" / "app.yaml").write_text(f'role: "{role}"\n' if role == "gateway" else "", encoding="utf-8")
    src_root = tmp_path / "src"; src_root.mkdir()
    (src_root / "x").write_text("x", encoding="utf-8")
    ans = tmp_path / "answers"; ans.write_text(answer + "\n", encoding="utf-8")
    prog = (
        "set -euo pipefail\n"
        f'INSTALL_DIR="{inst}"; ETC_DIR="{etc}"\n'
        "c_info=''; c_off=''\n"
        "log() { printf '[install] %s\\n' \"$*\"; }\n"
        "die() { printf '[install:ОШИБКА] %s\\n' \"$*\" >&2; exit 1; }\n"
        + _func(src, "ver_of") +
        f'JOIN_TOKEN="AWGJ1.DUMMY"; JOIN_VER="{want}"; TGZ=""; SRC_ROOT="{src_root}"\n'
        f'exec 3<"{ans}"\n' + frag + "echo CONTINUE_INSTALL\n")
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)})
    return r, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def test_an_installed_agent_of_the_same_release_joins_without_a_menu(tmp_path):
    """Т-Т11: агент стоит, выпуск тот же — меню нет: вопрос и сразу `awg-bot
    join` (поставленное не ставится заново)."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "y")
    assert r.returncode == 0, r.stderr
    assert "агент шлюза уже установлен (v3.4.0) — подключаю это устройство заново" in r.stderr
    assert "Что делаем?" not in r.stdout + r.stderr
    assert calls == ["awg-bot join AWGJ1.DUMMY JOIN="], calls


def test_an_installed_agent_of_another_release_is_updated_and_then_joins(tmp_path):
    r, calls = _existing(tmp_path, "3.3.1", "3.4.0", "y")
    assert r.returncode == 0, r.stderr
    assert len(calls) == 1 and calls[0].startswith("awg-bot update ") and calls[0].endswith("JOIN=AWGJ1.DUMMY"), calls


def test_no_means_nothing_changes(tmp_path):
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "n")
    assert r.returncode == 0 and calls == [] and "отмена — ничего не изменено" in r.stdout
    assert "CONTINUE_INSTALL" not in r.stdout


def test_a_server_is_not_turned_into_a_gateway(tmp_path):
    """Команду для шлюза запустили на сервере AWG — отказ, ничего не тронуто."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "y", role="client")
    assert r.returncode != 0 and calls == []
    assert "здесь установлен сервер AWG, а команда подключения — для шлюза" in r.stderr
