"""
sshd.py — порт sshd хоста из чата (README §6b, «Изменить порт»).

Порт живёт в двух местах, и они обязаны совпадать: `network.ssh_port` в
conf/app.yaml (его фильтрует таблица nftguard) и сам sshd. Раньше второе
правили руками; здесь — обе стороны одним действием, с проверками до
рестарта и откатом файлов, если sshd новый конфиг не принял.

Как sshd берёт порт, зависит от дистрибутива:
  • классика (Debian, Ubuntu ≤ 22.04): `Port` в /etc/ssh/sshd_config, иногда
    в /etc/ssh/sshd_config.d/*.conf — директива накопительная, второй `Port`
    добавляет порт, а не заменяет, поэтому лишние строки гасятся;
  • socket-активация (Ubuntu ≥ 22.10: ssh.socket): порт в конфиге sshd
    игнорируется, слушает systemd — нужен drop-in к ssh.socket.
Итог в обоих случаях проверяется по факту: `sshd -T` называет ровно новый
порт, а после рестарта на нём есть слушающий сокет.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("awgbot.sshd")

SSHD_CONFIG = "/etc/ssh/sshd_config"
SSHD_DROPIN_DIR = "/etc/ssh/sshd_config.d"
SOCKET_UNIT = "ssh.socket"
SOCKET_DROPIN = "/etc/systemd/system/ssh.socket.d/awg-bot.conf"
_PORT_RE = re.compile(r"^(\s*)Port\s+\S+", re.IGNORECASE)
_ADDR_PORT_RE = re.compile(r"^\s*ListenAddress\s+.*:\d+\s*$", re.IGNORECASE)


class SshdError(Exception):
    pass


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SshdError(f"{args[0]}: {e}")


def valid_port(port: int) -> bool:
    return 1 <= int(port) <= 65535


_PROC_RE = re.compile(r'users:\(\("([^"]+)"')


def port_busy(port: int) -> str:
    """Имя процесса, который слушает порт (TCP или UDP, любой адрес) — из
    `users:(("nginx",pid=…))` в выводе `ss`; сокет есть, а имени не видно
    (не root, сокет ядра) — «?»; пусто — порт свободен. sshd на своём
    текущем порту тоже «занято»: менять на него нечего."""
    proc = _run(["ss", "-Hlntup", f"sport = :{int(port)}"])
    if proc.returncode != 0:
        raise SshdError("ss: " + (proc.stderr.strip() or f"код {proc.returncode}"))
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return ""
    m = _PROC_RE.search(lines[0])
    return m.group(1) if m else "?"


def effective_ports() -> list[int]:
    """Порты из `sshd -T` — то, что sshd реально применит из конфига."""
    proc = _run(["sshd", "-T"])
    if proc.returncode != 0:
        raise SshdError("sshd -T: " + (proc.stderr.strip() or f"код {proc.returncode}"))
    ports = []
    for ln in proc.stdout.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] == "port" and parts[1].isdigit():
            ports.append(int(parts[1]))
    return ports


_PID_RE = re.compile(r'\(\("([^"]+)",pid=(\d+)')


def _is_sshd_pid(pid: int) -> bool:
    """Сокет принадлежит sshd, а не процессу с таким же именем: имя (comm)
    выставляет кто угодно, а /proc/<pid>/exe и владелец uid 0 — нет."""
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return False
    return os.path.basename(exe.split(" (deleted)")[0]) == "sshd" and uid == 0


def listening_ports() -> list[int]:
    """Порты, которые sshd СЛУШАЕТ сейчас (факт, не конфиг): `ss -Hltnp`,
    сокеты, среди держателей которых есть процесс sshd (по /proc: бинарь sshd
    и uid 0 — имя процесса подделывается). При socket-активации слушает
    systemd — тогда `sshd -T`. Пусто — sshd не запущен. Конфиг может быть не
    применён (OMV записал, sshd не перезапущен; правили руками) — фильтр
    должен совпадать с тем, куда придёт пакет, поэтому первичен факт."""
    proc = _run(["ss", "-Hltnp"])
    if proc.returncode != 0:
        raise SshdError("ss: " + (proc.stderr.strip() or f"код {proc.returncode}"))
    ports: list[int] = []
    for ln in proc.stdout.splitlines():
        parts = ln.split()
        if len(parts) < 4:
            continue
        holders = [(name, int(pid)) for name, pid in _PID_RE.findall(ln)]
        if not any(name == "sshd" and _is_sshd_pid(pid) for name, pid in holders):
            continue
        port = parts[3].rsplit(":", 1)[-1]
        if port.isdigit() and int(port) not in ports:
            ports.append(int(port))
    if not ports and socket_activated():
        return effective_ports()
    return sorted(ports)


@dataclass
class SshdOwner:
    """Кто владеет sshd_config. kind: '' — никто (правит бот), 'omv' —
    openmediavault, 'generator' — иная программа (шапка «managed by / do not
    edit»). where — где менять порт человеку; port — порт по мнению
    владельца (OMV: из его базы), None — не узнать; detail — найденная строка."""
    kind: str = ""
    where: str = ""
    port: int | None = None
    detail: str = ""
    files: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.kind)


OMV_CONFIG = "/etc/openmediavault/config.xml"
_OMV_HEAD_RE = re.compile(r"openmediavault", re.IGNORECASE)
_GEN_HEAD_RE = re.compile(r"auto-?generated|managed by|do not edit", re.IGNORECASE)
# Своя шапка: мы судим о владельце по шапке чужого файла — и сами оставляем
# такую же метку, чтобы другой инструмент (и человек) видел, кто держит порт.
# Только про порт: остальное в файле бот не трогает и не генерирует.
OUR_HEAD = ("# Port managed by awg-bot (Telegram bot: «Доступ по SSH» → «Изменить порт»).\n"
            "# Change the SSH port in the bot, not here: it keeps the host firewall on this port.\n"
            "# Other settings in this file are not touched by awg-bot.\n")
_OUR_HEAD_RE = re.compile(r"awg-bot", re.IGNORECASE)
_HEAD_LINES = 5


def _head(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [next(f).rstrip("\n") for _ in range(_HEAD_LINES)]
    except (OSError, StopIteration):
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[:_HEAD_LINES]
        except OSError:
            return []


def omv_ssh_conf() -> dict | None:
    """`omv-confdbadm read conf.service.ssh` → {'enable', 'port', …}; None —
    не OMV или команда не ответила JSON."""
    try:
        proc = _run(["omv-confdbadm", "read", "conf.service.ssh"])
    except SshdError:
        return None
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.strip() or "null")
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def omv_firewall_rules() -> int:
    """Сколько правил файервола задано в OMV (Сеть → Файервол): они живут в
    ip filter рядом с нашей таблицей, и drop там окончателен."""
    try:
        proc = _run(["omv-confdbadm", "read", "conf.system.network.iptables.rule"])
        doc = json.loads(proc.stdout.strip() or "[]") if proc.returncode == 0 else []
    except (SshdError, ValueError):
        return 0
    return len(doc) if isinstance(doc, list) else 0


def owner() -> SshdOwner:
    """Владелец sshd_config. OMV — есть его база и (шапка файла говорит
    «openmediavault» или база отвечает про ssh); иная программа — шапка
    файла или drop-in'а «auto-generated / managed by / do not edit».
    cloud-init (drop-in только про пароли) и пакетный conffile Debian —
    не владельцы: порт там никто не перепишет."""
    files = [SSHD_CONFIG]
    d = Path(SSHD_DROPIN_DIR)
    if d.is_dir():
        files += sorted(str(p) for p in d.glob("*.conf"))
    heads = {f: _head(f) for f in files}
    main_head = "\n".join(heads.get(SSHD_CONFIG, []))
    if os.path.exists(OMV_CONFIG):
        by_head = bool(_OMV_HEAD_RE.search(main_head))
        conf = omv_ssh_conf()
        if by_head or conf is not None:
            port = (conf or {}).get("port")
            head0 = heads.get(SSHD_CONFIG) or [""]
            return SshdOwner("omv", "OMV: Службы → SSH → «Порт», затем «Применить»",
                             int(port) if str(port).isdigit() else None,
                             head0[0].strip("# ").strip(), [SSHD_CONFIG])
    for f, head in heads.items():
        if "cloud-init" in f:
            continue
        for ln in head:
            if _OUR_HEAD_RE.search(ln):
                continue                                  # наша метка — владелец бот
            if ln.lstrip().startswith("#") and _GEN_HEAD_RE.search(ln):
                return SshdOwner("generator", "", None, ln.strip("# ").strip(), [f])
    return SshdOwner()


def socket_activated() -> bool:
    rc = _run(["systemctl", "is-enabled", "--quiet", SOCKET_UNIT]).returncode
    return rc == 0


def service_unit() -> str:
    """ssh.service (Debian/Ubuntu) или sshd.service (остальные)."""
    for unit in ("ssh.service", "sshd.service"):
        proc = _run(["systemctl", "list-unit-files", "--no-legend", unit])
        if proc.returncode == 0 and unit in proc.stdout:
            return unit
    return "ssh.service"


# ── правка конфигов ──────────────────────────────────────────────────────────

def rewrite_port(text: str, port: int, primary: bool) -> str:
    """Текст конфига с одним `Port`. primary — главный файл: первая строка
    `Port` (или закомментированная `#Port`) становится `Port N`, остальные
    гасятся; ни одной нет — `Port N` встаёт после блока `Include` в начале
    (Include в Debian стоит первым, и drop-in'ы с портом иначе перекрыли бы
    нас). В drop-in'ах все `Port` только гасятся."""
    out, done = [], False
    for ln in text.splitlines():
        m = _PORT_RE.match(ln)
        if m and primary and not done:
            out.append(f"{m.group(1)}Port {port}")
            done = True
            continue
        if m:
            out.append(f"{m.group(1)}# {ln.strip()}  # выключено awg-bot: порт задаётся кнопкой")
            continue
        if primary and not done and re.match(r"^\s*#\s*Port\s+\d+\s*$", ln, re.IGNORECASE):
            out.append(f"Port {port}")
            done = True
            continue
        out.append(ln)
    if primary and not done:
        at = 0
        for i, ln in enumerate(out):
            if re.match(r"^\s*Include\b", ln, re.IGNORECASE):
                at = i + 1
        out.insert(at, f"Port {port}")
    res = "\n".join(out)
    res = res + "\n" if text.endswith("\n") or not text else res
    if primary and not any("managed by awg-bot" in ln for ln in res.splitlines()[:_HEAD_LINES]):
        res = OUR_HEAD + res                              # один раз, в первых строках
    return res


def _write_atomic(path: str, text: str) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".awg-bot.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _config_files() -> list[str]:
    files = [SSHD_CONFIG]
    d = Path(SSHD_DROPIN_DIR)
    if d.is_dir():
        files += sorted(str(p) for p in d.glob("*.conf"))
    return files


def set_port(port: int) -> list[str]:
    """Перевести sshd на порт: конфиги → `sshd -T` называет только его →
    рестарт → сокет слушает. Любой провал — файлы как были, sshd не
    трогали (или перезапущен обратно), SshdError с причиной. Возвращает
    строки для журнала."""
    port = int(port)
    if not valid_port(port):
        raise SshdError(f"порт {port} вне диапазона 1–65535")
    saved: dict[str, str] = {}
    lines: list[str] = []

    def restore() -> None:
        for path, text in saved.items():
            try:
                _write_atomic(path, text)
            except OSError as e:
                log.error("sshd: не вернул %s: %s", path, e)
        if SOCKET_DROPIN in saved and saved[SOCKET_DROPIN] == "":
            try:
                os.unlink(SOCKET_DROPIN)
            except FileNotFoundError:
                pass

    for path in _config_files():
        try:
            text = Path(path).read_text()
        except FileNotFoundError:
            if path != SSHD_CONFIG:
                continue
            text = ""
        new = rewrite_port(text, port, primary=(path == SSHD_CONFIG))
        if any(_ADDR_PORT_RE.match(ln) for ln in text.splitlines()):
            raise SshdError(f"в {path} порт задан через ListenAddress — поправь руками")
        if new != text:
            saved[path] = text
            try:
                _write_atomic(path, new)
            except OSError as e:
                restore()
                raise SshdError(f"{path}: {e}")
            lines.append(f"{path}: Port {port}")
    try:
        chk = _run(["sshd", "-t"])
        if chk.returncode != 0:
            raise SshdError("sshd -t: " + (chk.stderr.strip() or chk.stdout.strip()))
        eff = effective_ports()
        if eff != [port]:
            shown = (("порт " if len(eff) == 1 else "порты ") + ", ".join(map(str, eff))) \
                if eff else "порт по умолчанию"
            raise SshdError(f"sshd применил бы {shown}, а не {port} — в конфиге есть "
                            "что-то, чего я не понял; поправь руками")
    except SshdError:
        restore()
        raise

    sock = socket_activated()
    if sock:
        prev = Path(SOCKET_DROPIN).read_text() if os.path.exists(SOCKET_DROPIN) else ""
        saved[SOCKET_DROPIN] = prev
        try:
            _write_atomic(SOCKET_DROPIN, f"[Socket]\nListenStream=\nListenStream={port}\n")
        except OSError as e:
            restore()
            raise SshdError(f"{SOCKET_DROPIN}: {e}")
        lines.append(f"{SOCKET_UNIT}: ListenStream={port}")
        _run(["systemctl", "daemon-reload"])
        cmds = [["systemctl", "stop", "ssh.service"], ["systemctl", "restart", SOCKET_UNIT]]
    else:
        cmds = [["systemctl", "restart", service_unit()]]
    for cmd in cmds:
        proc = _run(cmd, timeout=60)
        if proc.returncode != 0 and cmd[1] != "stop":
            restore()
            if sock:
                _run(["systemctl", "daemon-reload"])
            _run(cmd, timeout=60)
            raise SshdError(f"{' '.join(cmd)}: {proc.stderr.strip() or proc.returncode}")
    if not _listening(port):
        restore()
        if sock:
            _run(["systemctl", "daemon-reload"])
        for cmd in cmds:
            _run(cmd, timeout=60)
        raise SshdError(f"после перезапуска порт {port} никто не слушает — вернул как было")
    lines.append(f"sshd слушает {port}")
    return lines


def _listening(port: int, wait: float = 5.0) -> bool:
    end = time.monotonic() + wait
    while True:
        proc = _run(["ss", "-Hltn", f"sport = :{int(port)}"])
        if proc.returncode == 0 and proc.stdout.strip():
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)
