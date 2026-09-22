"""
resolver.py — свой DNS-резолвер клиентов на сервере (dnsmasq на <подсеть>.1).

Конфиг и демон ведёт install/awg-resolver-setup.sh; здесь — обёртка над ним
(install/add/remove/status) и проба «отвечает ли адрес» без внешних утилит:
монитор зовёт её на каждом тике, и тянуть ради этого dig незачем.

Зачем адрес приватный — в шапке скрипта и docs/ROADMAP.md §3: публичный
резолвер в поле DNS Chrome молча апгрейдит до DoH мимо любого перехвата, и
условная маршрутизация у такого клиента работает «через раз».
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import subprocess
from pathlib import Path
from typing import Optional

from awgbot.core import config

log = logging.getLogger(__name__)

CONF_PATH = Path("/etc/dnsmasq.d/awgbot-resolver.conf")


class ResolverError(RuntimeError):
    """Скрипт резолвера отказал; текст — его последние строки."""


def resolver_addr(prefix: str) -> str:
    """«10.8.1» → «10.8.1.1»: собственный адрес сервера в туннеле."""
    return f"{prefix}.1" if prefix else ""


def is_private(addr: str) -> bool:
    """Адрес из приватного диапазона — значит отвечать на нём должен этот хост."""
    a = (addr or "").strip()
    try:
        o = [int(x) for x in a.split(".")]
    except ValueError:
        return False
    if len(o) != 4:
        return False
    return o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31) or (o[0] == 192 and o[1] == 168)


def listen_addrs(path: Path = CONF_PATH) -> list[str]:
    """Адреса из конфига резолвера; пусто — резолвера бота на хосте нет."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if line.startswith("listen-address="):
            out.append(line.split("=", 1)[1].strip())
    return out


def installed() -> bool:
    return bool(listen_addrs())


def _script() -> Path:
    return config.BASE_DIR / "install" / "awg-resolver-setup.sh"


def run(mode: str, addr: str = "") -> str:
    """install|add|remove|status — через скрипт. ResolverError при ненулевом коде
    (кроме status: там код — состояние, не ошибка)."""
    args = ["bash", str(_script()), mode] + ([addr] if addr else [])
    try:
        proc = subprocess.run(args, capture_output=True, timeout=240,
                              env={**os.environ, "RESOLVER_CONF": str(CONF_PATH)})
    except (OSError, subprocess.SubprocessError) as e:
        raise ResolverError(f"скрипт резолвера не запустился: {e}")
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0 and mode != "status":
        raise ResolverError("\n".join(out.splitlines()[-4:]) or f"код {proc.returncode}")
    return out


def ensure_dropin() -> str:
    """Подтянуть override юнита dnsmasq к версии из поставки. Зовётся на старте
    бота: режим install при обновлении не повторяется, и правка override'а
    (например, снятый лимит попыток systemd) иначе не доезжает до хостов,
    поставленных прежними версиями. Скрипт переписывает файл только при
    расхождении."""
    return run("dropin")


def add(addr: str) -> None:
    """Слушать ещё и addr; резолвера нет — поставить с ним."""
    run("add" if installed() else "install", addr)


def remove(addr: str) -> None:
    """Снять addr, если его слушали (последний адрес скрипт снимать откажется)."""
    if addr in listen_addrs():
        run("remove", addr)


def probe(addr: str, timeout: float = 2.0, name: str = "example.com", port: int = 53) -> bool:
    """Один UDP-запрос A-записи на addr:53. True — пришёл ответ с нашим id и
    флагом QR (что в нём — неважно: NXDOMAIN тоже значит «резолвер жив»)."""
    if not addr:
        return False
    qid = os.urandom(2)
    header = qid + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(packet, (addr, port))
            data, _ = s.recvfrom(512)
    except (OSError, socket.timeout):
        return False
    return len(data) >= 12 and data[:2] == qid and bool(data[2] & 0x80)


def service_active(service: str = "dnsmasq") -> Optional[bool]:
    """Жив ли юнит dnsmasq; None — systemctl недоступен (не тот хост)."""
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", service],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0
