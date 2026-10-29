"""addrs.py — часть пакета awg (см. __init__): второй адрес интерфейса AWG сервера —
ответчик кворума резерва (блок util/probenet): отдельной строкой `Address` после
основной, без комментария (инвариант записи концепта «кворум» §3.1: читатели
первой строки видят основной адрес), и на живом интерфейсе — `ip addr replace`
(syncconf адресов не ставит)."""
from __future__ import annotations

import re
import subprocess
from typing import Optional

from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты

_ADDRESS_LINE = re.compile(r"^[ \t]*Address[ \t]*=[ \t]*(.*)$")


def with_second(conf: str, cidr: str) -> tuple[str, bool]:
    """Текст конфига со строкой `Address = cidr` сразу после первой строки Address
    (уже есть — как был). (текст, добавлено ли)."""
    lines = conf.split("\n")
    first = None
    for i, line in enumerate(lines):
        m = _ADDRESS_LINE.match(line)
        if m is None:
            continue
        if first is None:
            first = i
        if str(m.group(1)).split("#", 1)[0].strip() == cidr:
            return conf, False
    if first is None:
        return conf, False
    lines.insert(first + 1, f"Address = {cidr}")
    return "\n".join(lines), True


def without(conf: str, cidr: str) -> tuple[str, bool]:
    lines = conf.split("\n")
    out = []
    dropped = False
    for line in lines:
        m = _ADDRESS_LINE.match(line)
        if m is not None and str(m.group(1)).split("#", 1)[0].strip() == cidr:
            dropped = True
            continue
        out.append(line)
    return "\n".join(out), dropped


def _ip(args: list[str]) -> bool:
    try:
        return pkg.subprocess.run(["ip", *args], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_second_address(cidr: str, iface: Optional[str] = None) -> bool:
    """Второй адрес на интерфейсе: в конфиге (отдельной строкой) и на живом
    интерфейсе. True — конфиг изменился."""
    name = pkg.iface_of(iface)
    path = pkg.conf_path(name)
    changed = False
    with pkg.mutation_lock:
        conf = pkg.read_file(path)
        new_conf, added = with_second(conf, cidr)
        if added:
            with pkg.writing():
                pkg._backup_conf(iface)
                pkg.write_file(path, new_conf)
            changed = True
    _ip(["addr", "replace", cidr, "dev", name])
    return changed


def drop_second_address(cidr: str, iface: Optional[str] = None) -> bool:
    name = pkg.iface_of(iface)
    path = pkg.conf_path(name)
    changed = False
    with pkg.mutation_lock:
        conf = pkg.read_file(path)
        new_conf, dropped = without(conf, cidr)
        if dropped:
            with pkg.writing():
                pkg._backup_conf(iface)
                pkg.write_file(path, new_conf)
            changed = True
    _ip(["addr", "del", cidr, "dev", name])
    return changed
