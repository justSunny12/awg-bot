"""Установщик с командой подключения (`… | sudo bash -s -- --join AWGJ1.…`).

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


B2SUM = ('#!/bin/sh\n[ "$1" = "-l" ] || exit 2\nexec python3 -c \'import hashlib, sys; '
         'print(hashlib.blake2b(sys.stdin.buffer.read(), digest_size=int(sys.argv[1]) // 8).hexdigest(), "-")\' "$2"\n')


def _parse(token: str, *, b2sum: Path | None = None) -> subprocess.CompletedProcess:
    """join_parse под bash; b2sum — каталог с подставным b2sum (BLAKE2b с
    длиной дайджеста, как coreutils), иначе PATH без него."""
    src = INSTALLER.read_text(encoding="utf-8")
    prog = (_func(src, "join_sum_ok") + _func(src, "join_parse") + f'join_parse "{token}" && '
            'printf "%s|%s|%s|%s" "$JOIN_SLOT" "$JOIN_VER" "$JOIN_HOST" "$JOIN_ID"\n')
    path = f"{b2sum}:/usr/bin:/bin" if b2sum else "/usr/bin:/bin"
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20, env={"PATH": path})


@pytest.fixture()
def b2sum(tmp_path) -> Path:
    d = tmp_path / "b2"; d.mkdir()
    fake_cmd(d / "b2sum", B2SUM)
    return d


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
    assert r.stdout == f"{t.slot}|{t.version}|{t.host}|{t.join_id}", r.stdout


def test_the_shell_checks_the_sum_like_python(b2sum):
    """quar-9: испорченный байт при той же длине — отказ здесь («повреждена»), а не
    404 выпуска и ложное «нужен доступ к GitHub»."""
    import base64
    token = _token(version="3.4.0.2")
    assert _parse(token, b2sum=b2sum).returncode == 0
    raw = bytearray(base64.urlsafe_b64decode(token[len("AWGJ1."):] + "=="))
    raw[3] ^= 1                                       # выпуск 3.4.0.2 → 3.5.0.2: та же длина
    bad = "AWGJ1." + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    with pytest.raises(jointoken.TokenError):
        jointoken.decode(bad)
    assert _parse(bad, b2sum=b2sum).returncode != 0, "испорченный токен принят"


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
    """Выпуск — из токена, не latest; что ставим — строкой в начале; GitHub не
    ответил — отказ с отсылкой к пути без GitHub."""
    r, calls = _piped(tmp_path, "--join", _token(version="3.4.0.2"))
    assert r.returncode != 0
    urls = calls.read_text(encoding="utf-8").split()
    assert "https://github.com/justSunny12/awg-bot/releases/download/v3.4.0.2/awg-bot.tgz" in urls, urls
    assert not any("latest" in u for u in urls), urls
    out = r.stdout + r.stderr
    assert "подключение шлюза к серверу AWG vpn-server.example.org: слот 2, выпуск v3.4.0.2" in out, out
    assert "не скачался выпуск v3.4.0.2 с GitHub" in out and "sudo awg-bot gw-kit --stdout" in out, out


def test_a_broken_token_is_refused_before_the_network(tmp_path):
    """Команда повреждена (обрезали при копировании) — отказ до сети."""
    token = _token()
    r, calls = _piped(tmp_path, "--join", token[:-10])
    assert r.returncode != 0
    assert "команда подключения повреждена — скопируй её из бота сервера AWG" in r.stderr, r.stderr
    assert not calls.exists(), "с битым токеном ходили в сеть"


def _existing(tmp_path, have: str, want: str, answer: str, role: str = "gateway", *, tty: bool = True,
              yes: bool = False, state: str = "", update_from: str = "", tgz: Path | None = None,
              path: str = "/usr/bin:/bin"):
    """Фрагмент «агент уже стоит» с командой подключения: подставной
    awg-bot.sh пишет, чем его позвали; tty — есть ли терминал для вопроса
    (подменён дескриптором 3); state — файл сопряжения агента."""
    src = INSTALLER.read_text(encoding="utf-8")
    start = 'if [[ -n "${JOIN_TOKEN:-}" && -x "$INSTALL_DIR/venv/bin/python" ]]; then'
    assert start in src, "ветка «агент уже стоит» сменилась — тест устарел"
    frag = start + src.split(start, 1)[1].split("\nfi\n", 1)[0] + "\nfi\n"
    frag = frag.replace("< /dev/tty", "<&3")
    inst = tmp_path / "opt"; (inst / "venv" / "bin").mkdir(parents=True); (inst / "awgbot").mkdir()
    fake_cmd(inst / "venv" / "bin" / "python", "#!/bin/sh\n")
    (inst / "awgbot" / "__version__.py").write_text(f'__version__ = "{have}"\n', encoding="utf-8")
    log = tmp_path / "calls"
    fake_cmd(inst / "awg-bot.sh",
             f'#!/bin/sh\necho "awg-bot $* JOIN=${{AWG_UPDATE_THEN_JOIN-}} TOK=${{AWG_JOIN_TOKEN-}}" >> "{log}"\n')
    etc = tmp_path / "etc"; (etc / "conf").mkdir(parents=True)
    (etc / "conf" / "app.yaml").write_text(f'role: "{role}"\n' if role == "gateway" else "", encoding="utf-8")
    src_root = tmp_path / "src"; src_root.mkdir()
    (src_root / "x").write_text("x", encoding="utf-8")
    if update_from:                                       # минимум обновления поставки (UPDATE_FROM)
        (src_root / "awgbot" / "core").mkdir(parents=True)
        (src_root / "awgbot" / "core" / "compat.py").write_text(f'UPDATE_FROM = "{update_from}"\n', encoding="utf-8")
    ans = tmp_path / "answers"; ans.write_text(answer + "\n", encoding="utf-8")
    jf = tmp_path / "join.json"
    if state:
        jf.write_text(state, encoding="utf-8")
    # поставка распакована рядом или — архивом (кит шлюза), без распакованного дерева
    where = f'TGZ=""; SRC_ROOT="{src_root}"' if tgz is None else f'TGZ="{tgz}"; SRC_ROOT=""'
    prog = (
        "set -euo pipefail\n"
        f'INSTALL_DIR="{inst}"; ETC_DIR="{etc}"\n'
        "c_info=''; c_off=''\n"
        "log() { printf '[install] %s\\n' \"$*\"; }\n"
        "die() { printf '[install:ОШИБКА] %s\\n' \"$*\" >&2; exit 1; }\n"
        + "".join(_func(src, f) for f in ("ver_of", "ver_lt", "src_update_from", "update_from_why")) +
        f'JOIN_TOKEN="AWGJ1.DUMMY"; export AWG_JOIN_TOKEN="$JOIN_TOKEN"; JOIN_VER="{want}"; {where}\n'
        f'JOIN_ID="0123456789abcdef"; JOIN_STATE_FILE="{jf}"; ASSUME_YES={int(yes)}\n'
        + (f'exec 3<"{ans}"\n' if tty else "") + frag + "echo CONTINUE_INSTALL\n")
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": path, "TMPDIR": str(tmp_path)})
    return r, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def test_an_installed_agent_of_the_same_release_joins_without_a_menu(tmp_path):
    """Агент стоит, выпуск тот же — меню нет: вопрос и сразу `awg-bot
    join` (поставленное не ставится заново)."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "y")
    assert r.returncode == 0, r.stderr
    assert "агент шлюза уже установлен (v3.4.0) — подключаю это устройство заново" in r.stderr
    assert "Что делаем?" not in r.stdout + r.stderr
    assert calls == ["awg-bot join JOIN= TOK=AWGJ1.DUMMY"], calls


def test_an_installed_agent_of_another_release_is_updated_and_then_joins(tmp_path):
    r, calls = _existing(tmp_path, "3.3.1", "3.4.0", "y")
    assert r.returncode == 0, r.stderr
    assert len(calls) == 1 and calls[0].startswith("awg-bot update ") and "JOIN=AWGJ1.DUMMY" in calls[0], calls


def test_an_installed_agent_below_the_update_from_of_the_release_is_refused_before_the_update(tmp_path):
    """Агент ниже минимума выпуска токена (UPDATE_FROM поставки): шагов перехода
    в поставке нет — отказ с подсказкой, какой выпуск нужен сначала; update и
    join не зовутся."""
    r, calls = _existing(tmp_path, "3.4.1", "4.1.0", "y", update_from="4.0.0")
    assert r.returncode != 0 and calls == [], (r.stderr, calls)
    assert "с v3.4.1 сразу на v4.1.0 нельзя — сначала обнови до v4.0.0" in r.stderr, r.stderr
    r, calls = _existing(tmp_path / "ok", "4.0.2", "4.1.0", "y", update_from="4.0.0")
    assert r.returncode == 0 and len(calls) == 1 and calls[0].startswith("awg-bot update "), (r.stderr, calls)


# tar с поведением GNU tar для `-xzOf АРХИВ --wildcards ШАБЛОН` (на стенде и
# на хостах — GNU; bsdtar --wildcards не знает): содержимое совпавших членов —
# в stdout, вызов — в журнал. Прочие режимы фрагменту не нужны — код 2.
GNU_TAR = ('#!/usr/bin/env python3\n'
           'import fnmatch, sys, tarfile\n'
           'open(sys.argv[0] + ".log", "a").write(" ".join(sys.argv[1:]) + "\\n")\n'
           'a = sys.argv[1:]\n'
           'if a[:1] != ["-xzOf"] or "--wildcards" not in a: sys.exit(2)\n'
           'pats = a[a.index("--wildcards") + 1:]\n'
           'with tarfile.open(a[1], "r:gz") as t:\n'
           '    for m in t.getmembers():\n'
           '        if m.isfile() and any(fnmatch.fnmatch(m.name, p) for p in pats):\n'
           '            sys.stdout.buffer.write(t.extractfile(m).read())\n')


def _kit(tmp_path: Path, update_from: str) -> Path:
    """Архив поставки, как его собирает `tar czf … -C поставка .` (члены «./…»)."""
    import io
    import tarfile
    tgz = tmp_path / "kit" / "awg-bot.tgz"; tgz.parent.mkdir(parents=True)
    files = {"./awgbot/__version__.py": b'__version__ = "4.1.0"\n'}
    if update_from:
        files["./awgbot/core/compat.py"] = f'COMPAT_FLOOR = "4.0.0"\nUPDATE_FROM = "{update_from}"\n'.encode()
    with tarfile.open(tgz, "w:gz") as t:
        for name, raw in files.items():
            ti = tarfile.TarInfo(name); ti.size = len(raw); t.addfile(ti, io.BytesIO(raw))
    return tgz


def _gnu_tar(tmp_path: Path) -> tuple[str, Path]:
    d = tmp_path / "gnubin"; d.mkdir(parents=True)
    (d / "tar").write_text(GNU_TAR, encoding="utf-8")       # исполняемый Python сам, не телом sh (как у fake_cmd)
    (d / "tar").chmod(0o755)
    return f"{d}:/usr/bin:/bin", d / "tar.log"


@pytest.mark.parametrize("have, refused", [("3.4.1", True), ("4.0.2", False)], ids=["below", "at-or-above"])
def test_with_an_archive_the_join_path_reads_the_minimum_from_inside_the_archive(tmp_path, have, refused):
    """Кит шлюза — архив без распакованного дерева: минимум обновления берётся
    из compat.py внутри архива. Не прочти его — агент 3.4.1 поехал бы на 4.1.0
    прыжком через минимум (conf и обвязка прежней раскладки без единой ошибки)."""
    path, tar_log = _gnu_tar(tmp_path)
    tgz = _kit(tmp_path, "4.0.0")
    r, calls = _existing(tmp_path, have, "4.1.0", "y", tgz=tgz, path=path)
    reads = tar_log.read_text(encoding="utf-8").splitlines() if tar_log.exists() else []
    assert any(ln.startswith(f"-xzOf {tgz} ") and "compat.py" in ln for ln in reads), \
        f"минимум не читался из архива: {reads}"
    if refused:
        assert r.returncode != 0 and calls == [], (r.stderr, calls)
        assert "с v3.4.1 сразу на v4.1.0 нельзя — сначала обнови до v4.0.0" in r.stderr, r.stderr
    else:
        assert r.returncode == 0, r.stderr
        assert len(calls) == 1 and calls[0].startswith(f"awg-bot update {tgz} "), calls


def test_an_archive_without_update_from_is_not_refused_on_the_join_path(tmp_path):
    """Поставка до появления минимума (compat.py без UPDATE_FROM или его нет в
    архиве) — сравнивать не с чем: обновление и подключение, как раньше."""
    path, tar_log = _gnu_tar(tmp_path)
    tgz = _kit(tmp_path, "")
    r, calls = _existing(tmp_path, "3.4.1", "4.1.0", "y", tgz=tgz, path=path)
    assert r.returncode == 0 and len(calls) == 1 and calls[0].startswith(f"awg-bot update {tgz} "), (r.stderr, calls)


def test_no_means_nothing_changes(tmp_path):
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "n")
    assert r.returncode == 0 and calls == [] and "отмена — ничего не изменено" in r.stdout
    assert "CONTINUE_INSTALL" not in r.stdout


def test_a_server_is_not_turned_into_a_gateway(tmp_path):
    """Команду для шлюза запустили на сервере AWG — отказ, ничего не тронуто."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "y", role="client")
    assert r.returncode != 0 and calls == []
    assert "здесь установлен сервер AWG, а команда подключения — для шлюза" in r.stderr


def test_the_same_command_still_running_is_not_asked_again(tmp_path):
    """flow-11: та же команда ещё идёт (id в файле сопряжения) — без вопроса,
    сразу `awg-bot join` (покажет код и ход)."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "n",
                         state='{"id": "0123456789abcdef", "state": "pairing"}')
    assert r.returncode == 0, r.stderr
    assert "уже идёт — показываю ход" in r.stdout and "Продолжить?" not in r.stdout + r.stderr
    assert calls == ["awg-bot join JOIN= TOK=AWGJ1.DUMMY"], calls
    (tmp_path / "other").mkdir()
    r, calls = _existing(tmp_path / "other", "3.4.0", "3.4.0", "n",
                         state='{"id": "ffffffffffffffff", "state": "pairing"}')
    assert calls == [] and "отмена — ничего не изменено" in r.stdout


def test_without_a_terminal_it_asks_for_yes(tmp_path):
    """flow-6: трубой кита терминала нет — не молчаливое «нет» и не падение
    read, а внятный отказ; с --yes — подключение без вопроса."""
    r, calls = _existing(tmp_path, "3.4.0", "3.4.0", "y", tty=False)
    assert r.returncode != 0 and calls == []
    assert "нет терминала" in r.stderr and "--yes" in r.stderr, r.stderr
    (tmp_path / "y").mkdir()
    r, calls = _existing(tmp_path / "y", "3.4.0", "3.4.0", "", tty=False, yes=True)
    assert r.returncode == 0 and calls == ["awg-bot join JOIN= TOK=AWGJ1.DUMMY"], (r.stderr, calls)


def _local(tmp_path, ver: str, token: str) -> subprocess.CompletedProcess:
    """Установщик из распакованной поставки версии ver (кит сервера) с токеном."""
    root = tmp_path / "kit"
    (root / "install").mkdir(parents=True); (root / "awgbot").mkdir()
    src = INSTALLER.read_text(encoding="utf-8")
    root_check = 'if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then'
    src = src.replace(root_check, "if false; then", 1)
    src = src.replace('INSTALL_DIR="/opt/awg-bot"', f'INSTALL_DIR="{tmp_path}/opt"', 1)
    (root / "install" / "awg-bot-install.sh").write_text(src, encoding="utf-8")
    (root / "awgbot" / "__main__.py").write_text("", encoding="utf-8")
    (root / "awgbot" / "__version__.py").write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
    (root / "awg-bot.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return subprocess.run(["bash", str(root / "install" / "awg-bot-install.sh"), "--skip-verify", "--join", token],
                          capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                          env={"PATH": "/usr/bin:/bin"})


def test_a_kit_of_another_version_is_refused(tmp_path):
    """flow-5: кит везёт код сервера v3.4.1, а команда ставит агента v3.4.0
    (выпуск набора) — отказ до установки."""
    r = _local(tmp_path, "3.4.1", _token(version="3.4.0"))
    assert r.returncode != 0
    assert "поставка — v3.4.1, а команда подключения ставит агента v3.4.0" in r.stderr, r.stderr
