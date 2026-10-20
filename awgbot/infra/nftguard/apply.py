"""apply.py — часть пакета nftguard (см. __init__)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional
from awgbot.infra import nftjson, systemd
from awgbot.infra import nftguard as pkg       # имена пакета — через него: их подменяют тесты


# ── nft: применить, прочитать ────────────────────────────────────────────────

def _nft(args: list[str], timeout: int = 10, check: bool = True) -> subprocess.CompletedProcess:
    proc = nftjson.run(args, timeout=timeout, error=pkg.GuardError)
    if check and proc.returncode != 0:
        raise pkg.GuardError(f"nft {' '.join(args[:3])}: "
                         f"{proc.stderr.decode(errors='replace').strip()}")
    return proc


def read_file() -> str:
    try:
        return Path(pkg.RULES_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_file(text: str) -> None:
    d = Path(pkg.RULES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".awg-bot-guard.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, pkg.RULES_FILE)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise pkg.GuardError(f"не записать {pkg.RULES_FILE}: {e}")


def check_text(text: str) -> None:
    """Синтаксис — до записи: `nft -c -f`."""
    with tempfile.NamedTemporaryFile("w", suffix=".nft", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = f.name
    try:
        pkg._nft(["-c", "-f", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply_text(text: str) -> None:
    """Проверить, записать, применить атомарно. Порядок важен: файл — источник
    для загрузки при ребуте, и он не должен опережать проверку синтаксиса."""
    pkg.check_text(text)
    pkg.write_file(text)
    pkg._nft(["-f", pkg.RULES_FILE])


def nat_covers(subnet: str) -> bool:
    """Живая таблица маскарадит эту подсеть? Проверка для доктора условной
    маршрутизации: NAT переехал сюда из iptables, и «нет правила iptables»
    больше не означает «клиенты не выйдут наружу»."""
    proc = pkg._nft(["-j", "list", "table", pkg.TABLE_FAMILY, pkg.TABLE_NAME], check=False)
    if proc.returncode != 0:
        return False
    try:
        doc = json.loads(proc.stdout.decode(errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return False
    has_masq = any("masquerade" in json.dumps(x.get("rule", {}))
                   for x in doc.get("nftables", []) if "rule" in x)
    if not has_masq:
        return False
    live = pkg.live_set(pkg.SET_TUNNEL_NETS)
    return live is not None and subnet in live


def table_present() -> bool:
    return pkg._nft(["list", "table", pkg.TABLE_FAMILY, pkg.TABLE_NAME], check=False).returncode == 0


def live_set(name: str) -> Optional[set[str]]:
    """Элементы set из ядра; None — таблицы/set нет."""
    proc = pkg._nft(["-j", "list", "set", pkg.TABLE_FAMILY, pkg.TABLE_NAME, name], check=False)
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise pkg.GuardError(f"nft -j: {e}")
    out: set[str] = set()
    for item in doc.get("nftables", []):
        s = item.get("set")
        if not s:
            continue
        for el in s.get("elem", []) or []:
            out.add(nftjson.elem_str(el))
    return out


# ── сверка по тику ───────────────────────────────────────────────────────────

def reconcile(admin_ips, channel_extra=(), transit_links=()) -> str:
    """Привести таблицу к желаемой. Возвращает, что сделано:
    'disabled' | 'ok' | 'applied' (текст изменился) | 'restored' (таблицу
    снесли) | 'resynced' (set устройств разошёлся с файлом)."""
    if not pkg.enabled():
        # NAT-only форма, без фильтра. Файл ещё не существовал — первое
        # появление таблицы на хосте: закрепить загрузку с ребута.
        text = pkg.render(pkg.build_spec([], channel_extra, transit_links))
        first = not pkg.read_file()
        if text != pkg.read_file() or not pkg.table_present():
            pkg.apply_text(text)
            if first:
                try:
                    pkg.ensure_persistence()
                except pkg.GuardError as e:
                    pkg.log.warning("nat: персистентность не закреплена: %s", e)
            return "nat"
        return "ok"
    spec = pkg.build_spec(admin_ips, channel_extra, transit_links)
    text = pkg.render(spec)
    if text != pkg.read_file():
        pkg.apply_text(text)
        return "applied"
    live = pkg.live_set(pkg.SET_TUNNEL_ADMIN)
    if live is None:
        pkg.apply_text(text)
        return "restored"
    if live != set(spec.tunnel_admin4):
        pkg.apply_text(text)
        return "resynced"
    return "ok"


# ── персистентность, откат, снятие ───────────────────────────────────────────

def ensure_persistence() -> list[str]:
    """include нашего каталога в /etc/nftables.conf + nftables.service enabled.
    Возвращает список сделанного (для вывода в CLI)."""
    done: list[str] = []
    include = f'include "{pkg.RULES_DIR}/*.nft"'
    main = Path(pkg.MAIN_CONF)
    try:
        cur = main.read_text(encoding="utf-8") if main.exists() else ""
        if pkg.RULES_DIR not in cur:
            with main.open("a", encoding="utf-8") as f:
                if not cur:
                    f.write("#!/usr/sbin/nft -f\n")
                f.write(f"\n{include}\n")
            done.append(f"include в {pkg.MAIN_CONF}")
    except OSError as e:
        raise pkg.GuardError(f"{pkg.MAIN_CONF}: {e}")
    rc = subprocess.run(["systemctl", "enable", "nftables"], capture_output=True).returncode
    if rc == 0:
        done.append("nftables.service включён")
    else:
        done.append("ВНИМАНИЕ: systemctl enable nftables не удался — правила могут не пережить ребут")
    return done


def rollback_armed() -> bool:
    return systemd.is_active(f"{pkg.ROLLBACK_UNIT}.timer") is True


def arm_rollback(seconds: int, command: list[str], env: dict) -> None:
    """Таймер отката: через N секунд без `confirm` таблица снимается и
    firewall.enabled сбрасывается. Страховка от самозапирания."""
    pkg.disarm_rollback()
    args = ["systemd-run", f"--unit={pkg.ROLLBACK_UNIT}", f"--on-active={int(seconds)}s",
            "--collect", "--quiet"]
    for k, v in env.items():
        args.append(f"--setenv={k}={v}")
    proc = subprocess.run(args + command, capture_output=True)
    if proc.returncode != 0:
        raise pkg.GuardError("таймер отката не поставлен: "
                         + proc.stderr.decode(errors="replace").strip())


def disarm_rollback() -> bool:
    was = pkg.rollback_armed()
    for unit in (f"{pkg.ROLLBACK_UNIT}.timer", f"{pkg.ROLLBACK_UNIT}.service"):
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    return was


def remove(transit_links=()) -> list[str]:
    """Снять фильтр; SSH становится открыт всем (доступ не теряется). В
    таблица остаётся в NAT-only форме: без маскарада клиенты потеряли бы
    интернет — «выключить файервол» этого не обещает."""
    done: list[str] = []
    spec = pkg.build_spec([], transit_links=transit_links)
    spec.filter = False
    try:
        pkg.apply_text(pkg.render(spec))
        done.append("фильтр снят, NAT клиентов оставлен (таблица в форме NAT-only)")
    except pkg.GuardError as e:
        done.append(f"NAT-only форма не применена: {e}")
    return done


def ufw_active() -> bool:
    try:
        out = subprocess.run(["ufw", "status"], capture_output=True, timeout=10,
                             env={**os.environ, "LC_ALL": "C"}).stdout        # «Status: active» без перевода
    except (OSError, subprocess.SubprocessError):
        return False
    return b"Status: active" in out


def firewalld_active() -> bool:
    """Второй владелец правил на RHEL-семействе; на apt-системах редко, но
    ставится руками."""
    return systemd.is_active("firewalld") is True


def status(admin_ips, transit_links=()) -> dict:
    spec = pkg.build_spec(admin_ips, transit_links=transit_links)
    text = pkg.render(spec)
    present = pkg.table_present()
    return {
        "enabled": pkg.enabled(), "present": present, "file": pkg.read_file() == text,
        "live_admin": pkg.live_set(pkg.SET_TUNNEL_ADMIN) if present else None,
        "live_allow4": pkg.live_set(pkg.SET_ALLOW4) if present else None,
        "spec": spec, "rollback": pkg.rollback_armed(), "ufw": pkg.ufw_active(),
        "firewalld": pkg.firewalld_active(),
    }
