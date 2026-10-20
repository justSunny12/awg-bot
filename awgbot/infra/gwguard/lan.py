"""lan.py — локальная сеть: списки, свои домены, резолвер, сервисы соседних сетей."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
from typing import Optional

from awgbot.infra import systemd
from awgbot.infra import gwguard as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import LAN_SCRIPT_TIMEOUT


def lists_status() -> dict:
    """Что записал скрипт списков: updated_at, domains, nets, rc."""
    out = {}
    try:
        for line in pkg.Path(pkg.LAN_STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def lan_own_lists() -> tuple[int, int]:
    """(в туннель, напрямую) — персональные списки, строки nftset=."""
    def _count(name: str) -> int:
        try:
            text = pkg.Path(f"{pkg.DNSMASQ_D}/{name}").read_text(encoding="utf-8")
        except OSError:
            return 0
        return sum(1 for ln in text.splitlines() if ln.startswith("nftset="))
    return _count(pkg.OWN_LIST_FILES[0]), _count(pkg.OWN_LIST_FILES[1])


def dnsmasq_active() -> Optional[bool]:
    return systemd.is_active("dnsmasq")


def upstream_stats() -> Optional[dict[str, tuple[int, int]]]:
    """Статистика апстримов dnsmasq без единого запроса наружу: `servers.bind`
    в классе CHAOS отдаёт по каждому серверу «отправлено / отказов». None —
    dig нет или dnsmasq не ответил.

    Раньше апстрим проверялся живым запросом к github.com каждым тиком: при TTL
    в минуту dnsmasq почти каждый раз уходил наружу — один и тот же запрос раз
    в три минуты, тот самый периодический рисунок, от которого уходим."""
    try:
        proc = pkg.subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1",
                               "servers.bind", "CH", "TXT"], capture_output=True, timeout=15)
    except (OSError, pkg.subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, tuple[int, int]] = {}
    for m in re.finditer(r'"([0-9a-fA-F.:#@\w-]+)\s+(\d+)\s+(\d+)"',
                         proc.stdout.decode(errors="replace")):
        out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def iface_for_subnet(net: str) -> Optional[tuple[str, str]]:
    """(интерфейс, адрес) с адресом из подсети — тем же правилом, что
    lan_iface_for в скрипте обвязки; None — нет или не прочиталось."""
    try:
        want = ipaddress.ip_network(net, strict=False)
        proc = pkg.subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, timeout=10)
        links = json.loads(proc.stdout.decode(errors="replace") or "[]")
    except (ValueError, OSError, pkg.subprocess.SubprocessError, json.JSONDecodeError):
        return None
    for link in links:
        name = link.get("ifname", "")
        if name == "lo" or name.startswith(("awg", "veth", "br")):
            continue
        for a in link.get("addr_info") or []:
            try:
                if ipaddress.ip_address(a.get("local", "")) in want:
                    return name, a["local"]
            except ValueError:
                continue
    return None


def run_lan_lists(timeout: int = 600, from_dir: str = "") -> tuple[bool, str]:
    """Обновить списки скриптом обвязки. (ok, хвост вывода). from_dir — фиды
    не качать, а взять готовыми оттуда (их привёз канал линка)."""
    import os
    env = dict(os.environ)
    if from_dir:
        env["AWG_LAN_FROM"] = from_dir
    try:
        proc = pkg.subprocess.run([pkg.LAN_LISTS_SCRIPT], capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return False, "скрипта списков нет — «🚑 Восстановить»; не поможет — обнови агента шлюза: скрипты приезжают с ним"
    except pkg.subprocess.TimeoutExpired:
        return False, "таймаут обновления списков"
    except OSError as e:
        return False, str(e)
    tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def avahi_browse_available() -> bool:
    return shutil.which("avahi-browse") is not None


def avahi_active() -> Optional[bool]:
    """Запущен ли avahi-daemon: без него SMB-серверы этой сети соседям не
    видны. None — systemctl не ответил."""
    return systemd.is_active("avahi-daemon")


def avahi_browse(timeout: int = 15) -> Optional[str]:
    """`avahi-browse -rtpk _smb._tcp` — что объявляют по mDNS в локальной сети.
    Запрос не покидает её сегмент. None — утилиты нет или обзор не удался
    (промахом гистерезиса не считается)."""
    try:
        proc = pkg.subprocess.run(["avahi-browse", "-rtpk", "_smb._tcp"], capture_output=True,
                              timeout=timeout)
    except (OSError, pkg.subprocess.SubprocessError):   # нет утилиты, не ответила
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace")


def run_lan_services(path: str = "", timeout: int = LAN_SCRIPT_TIMEOUT) -> tuple[bool, str]:
    """Установить файл записей соседей в dnsmasq помощником обвязки (проверка
    построчно, --test, рестарт с откатом); без пути — снять файл. (ok, хвост)."""
    try:
        proc = pkg.subprocess.run([pkg.LAN_SERVICES_SCRIPT, *([path] if path else [])],
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, "отсутствует скрипт — «🚑 Восстановить»; не поможет — обнови агента шлюза: скрипты приезжают с ним"
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт не ответил за {timeout} с"
    except OSError as e:
        return False, str(e)
    tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def dns_local(name: str, qtype: str = "PTR") -> Optional[list[str]]:
    """Что отдаёт свой dnsmasq по имени: `dig +short @127.0.0.1`. None — dig
    нет или не ответил; пустой список — записи нет."""
    try:
        proc = pkg.subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1", name, qtype],
                              capture_output=True, timeout=15)
    except (OSError, pkg.subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [ln.strip() for ln in proc.stdout.decode(errors="replace").splitlines() if ln.strip()]


def lan_domain_has_sync() -> bool:
    """Скрипт своих списков умеет `sync` (метка в шапке): без неё обвязка
    старого образца — канон применять нечем, нужен реассерт."""
    try:
        with open(pkg.LAN_DOMAIN_SCRIPT, encoding="utf-8", errors="replace") as f:
            return "# awg-lan-domain: sync" in f.read(8192)
    except OSError:
        return False


def run_lan_domain(cmd: str, domains: list[str], timeout: int = LAN_SCRIPT_TIMEOUT) -> tuple[bool, str]:
    """Свои списки: add | ru | del | list | sync. (ok, вывод)."""
    try:
        # AWG_HTML=1 — домены в итоге моноширинным (<code>): вывод уходит в чат;
        # в терминале (awg-bot lan …) скрипт печатает голый текст
        proc = pkg.subprocess.run([pkg.LAN_DOMAIN_SCRIPT, cmd, *domains], capture_output=True, timeout=timeout,
                              env={**os.environ, "AWG_HTML": "1"})
    except FileNotFoundError:
        return False, "отсутствует скрипт — «🚑 Восстановить»; не поможет — обнови агента шлюза: скрипты приезжают с ним"
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт не ответил за {timeout} с"
    except (OSError, pkg.subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout + proc.stderr).decode(errors="replace").strip()
