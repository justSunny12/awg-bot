"""
admindns.py — резолвер админа: второй экземпляр dnsmasq на собственном адресе
(по умолчанию 10.255.53.53/32), только для устройств админа. Отдаёт записи
SMB локальных сетей шлюзов под awg.internal («SMB в туннеле»), остальное —
резолверу пользователей. Конфиг, юнит и файл записей ведёт
install/awg-admin-dns-setup.sh; здесь — обёртка над ним и проба.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from awgbot.core import config, settings

log = logging.getLogger(__name__)

DEFAULT_ADDR = "10.255.53.53"
# частные диапазоны — те же, что проверяет awg-bot.sh (admin_dns_addr); is_private
# у ipaddress шире: документационные и служебные сети он тоже считает частными
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
ADMIN_DIR = Path("/etc/awgbot-admin-dns")
CONF_PATH = ADMIN_DIR / "dnsmasq.conf"
UNIT = "awgbot-admin-dns"


class AdminDnsError(RuntimeError):
    """Скрипт резолвера админа отказал; текст — его последние строки."""


def addr() -> str:
    """Адрес резолвера админа (app.client_config.admin_dns). Годится только
    частный адрес вне подсетей пользователей: адрес из них (например, резолвер
    пользователей) правило «:53 — только админу» отрезало бы от DNS всех. Иначе —
    умолчание с предупреждением в журнале."""
    raw = str(settings.get("app.client_config.admin_dns", DEFAULT_ADDR) or DEFAULT_ADDR).strip()
    try:
        ip = ipaddress.IPv4Address(raw)
    except ValueError:
        log.warning("app.client_config.admin_dns «%s» — не адрес, беру %s", raw, DEFAULT_ADDR)
        return DEFAULT_ADDR
    if not any(ip in n for n in _RFC1918) or _in_client_nets(ip):
        log.warning("app.client_config.admin_dns %s — нужен частный адрес вне подсетей пользователей, беру %s",
                    ip, DEFAULT_ADDR)
        return DEFAULT_ADDR
    return str(ip)


def _in_client_nets(ip) -> bool:
    try:
        from awgbot.infra import nftguard
        nets = nftguard.tunnel_nets()
    except Exception:                                     # noqa: BLE001
        return False
    for n in nets:
        try:
            if ip in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def _script() -> Path:
    return config.BASE_DIR / "install" / "awg-admin-dns-setup.sh"


def run(mode: str, arg: str = "") -> str:
    """install|records|remove|status — через скрипт. AdminDnsError при
    ненулевом коде (кроме status: там код — состояние)."""
    args = ["bash", str(_script()), mode] + ([arg] if arg else [])
    env = {**os.environ, "ADMIN_ADDR": addr(), "ADMIN_DIR": str(ADMIN_DIR)}
    try:
        proc = subprocess.run(args, capture_output=True, timeout=120, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise AdminDnsError(f"скрипт резолвера админа не запустился: {e}")
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0 and mode != "status":
        raise AdminDnsError("\n".join(out.splitlines()[-3:]) or f"код {proc.returncode}")
    return out


def installed() -> bool:
    """Конфиг экземпляра лежит — резолвер админа поставлен."""
    return CONF_PATH.exists()


def upstream() -> str:
    """Куда резолвер админа отдаёт всё, кроме awg.internal (server= конфига)."""
    try:
        for line in CONF_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("server="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def ensure(client_resolver: str) -> bool:
    """Поставить или подтянуть к адресу резолвера пользователей (переезд меняет его),
    к адресу из настройки и к юниту из поставки. Скрипт зовётся всегда: он
    идемпотентен и трогает демон только при изменении конфига или юнита.
    True — стоит."""
    if not client_resolver:
        return False
    run("install", client_resolver)
    return True


def serving() -> bool:
    """Поставлен и жив: только тогда его адрес идёт в конфиги устройств админа
    (поставленный, но лежащий оставил бы их без DNS)."""
    return installed() and service_active() is True


def apply_records(text: str) -> None:
    """Файл записей (строки проверены вызывающим) — или снять его (пусто)."""
    if not text:
        run("records", "-")
        return
    fd, path = tempfile.mkstemp(prefix="awg-admin-dns-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        run("records", path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def nat_exempt(subnets) -> list[str]:
    """Адрес резолвера админа — мимо перехвата :53 обвязки маршрутизации
    (routing-host-setup.sh, шаг 3: DNAT всего DNS пользователей на резолвер
    пользователей). Обвязка ставит RETURN сама при прогоне, но живые хосты
    прогоняют её только при загрузке — бот ставит то же на старте. Только если
    перехват есть; RETURN вставляется в начало цепочки, то есть выше DNAT.
    Возвращает, для каких подсетей правило поставлено."""
    try:
        out = subprocess.run(["iptables", "-t", "nat", "-S", "PREROUTING"], capture_output=True,
                             timeout=10).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    if "--dport 53" not in out or "DNAT" not in out:
        return []
    done = []
    for net in subnets:
        for proto in ("udp", "tcp"):
            spec = ["-s", str(net), "-d", addr(), "-p", proto, "--dport", "53", "-j", "RETURN"]
            try:
                exists = subprocess.run(["iptables", "-t", "nat", "-C", "PREROUTING", *spec],
                                        capture_output=True, timeout=10).returncode == 0
                if not exists:
                    subprocess.run(["iptables", "-t", "nat", "-I", "PREROUTING", *spec],
                                   capture_output=True, timeout=10, check=True)
                    done.append(f"{net}/{proto}")
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("резолвер админа: исключение из перехвата :53 не поставлено: %s", e)
    return done


def probe(timeout: float = 2.0) -> bool:
    """Отвечает ли резолвер админа на своём адресе (та же проба, что у резолвера пользователей)."""
    from awgbot.infra import resolver
    return resolver.probe(addr(), timeout=timeout)


def service_active():
    from awgbot.infra import systemd
    return systemd.is_active(UNIT)
