"""rfpeer.py — РФ-доступ через второй сервер (резерв) на хосте: файл согласия для
обвязки файервола, правило маршрутизации транзитного, `AllowedIPs` линка пары у
просящего, рукопожатие линка пары.

Файл `reserve/rfpeer.json` в каталоге данных читает обвязка файервола без БД
(`nftguard/collect.py`): иначе `awg-bot firewall apply` из CLI рисовал бы
таблицу без правил транзита. Адреса линка пары — из `awgpair.conf`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from awgbot.core import config

from . import base

PAIR_IF = "awgpair"
RULE_PREF = 151                      # 150 — awggr-h зоны
HANDSHAKE_MAX_S = 180


def rfpeer_path() -> Path:
    return Path(config.DATA_DIR) / "reserve" / "rfpeer.json"


def read() -> dict:
    """{"offer": bool, "slot": bool, "at": iso}; нет файла — всё ложь."""
    try:
        data = json.loads(rfpeer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(offer: bool, slot: bool, at: str = "") -> None:
    p = rfpeer_path()
    os.makedirs(str(p.parent), mode=0o700, exist_ok=True)
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offer": bool(offer), "slot": bool(slot), "at": at}, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, str(p))


def remove() -> None:
    try:
        os.unlink(str(rfpeer_path()))
    except OSError:
        pass


def pair_conf() -> dict:
    """Адреса и ключ линка пары из его конфига: {"self4", "peer4", "pub"}; нет конфига — пусто."""
    try:
        text = Path(f"{config.AWG_DIR}/{PAIR_IF}.conf").read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    m = re.search(r"(?m)^\s*Address\s*=\s*([0-9.]+)", text)
    if m:
        out["self4"] = m.group(1)
    m = re.search(r"(?m)^\s*AllowedIPs\s*=\s*([0-9.]+)", text)
    if m:
        out["peer4"] = m.group(1)
    m = re.search(r"(?m)^\s*PublicKey\s*=\s*(\S+)", text)
    if m:
        out["pub"] = m.group(1)
    return out


# ── транзитный: правило маршрутизации с линка пары в таблицу РФ-доступа ─────

def _rule(peer4: str) -> list[str]:
    return ["from", peer4, "iif", PAIR_IF, "lookup", str(config.ROUTING_TABLE), "pref", str(RULE_PREF)]


def rule_present(peer4: str) -> bool:
    res = base._host(["ip", "rule", "show"], check=False, timeout=base._PROBE_TIMEOUT)
    text = res.stdout.decode(errors="replace")
    return any(f"from {peer4} iif {PAIR_IF}" in line for line in text.splitlines())


def ensure_rule(peer4: str) -> None:
    """Правило 151: трафик с адреса второй стороны из линка пары — в таблицу РФ-доступа. Идемпотентно."""
    if not rule_present(peer4):
        base._host(["ip", "rule", "add", *_rule(peer4)])


def drop_rule(peer4: str) -> None:
    while base._host_ok(["ip", "rule", "del", *_rule(peer4)]):
        pass


# ── просящий: AllowedIPs линка пары ──────────────────────────────────────────

def allowed_ips(pub: str) -> list[str]:
    res = base._host(["awg", "show", PAIR_IF, "allowed-ips"], check=False, timeout=base._PROBE_TIMEOUT)
    for line in res.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if parts and parts[0] == pub:
            return [p for p in parts[1:] if p != "(none)"]
    return []


def allowed_ips_wide(on: bool) -> bool:
    """Расширить (`0.0.0.0/0` сверх /32 второй стороны) или сузить `AllowedIPs` пира линка пары
    рантаймом (`awg set`), не в конфиге: там без `Table = off` маршрут по умолчанию ушёл бы в
    линк. True — что-то менялось."""
    conf = pair_conf()
    pub, peer4 = conf.get("pub"), conf.get("peer4")
    if not pub or not peer4:
        return False
    want = [f"{peer4}/32", "0.0.0.0/0"] if on else [f"{peer4}/32"]
    have = allowed_ips(pub)
    if set(have) == set(want):
        return False
    base._host(["awg", "set", PAIR_IF, "peer", pub, "allowed-ips", ",".join(want)])
    return True


def ifindex() -> int:
    """Индекс устройства линка пары; 0 — нет. Меняется при пересоздании интерфейса (--adopt,
    рестарт юнита пары): маршруты таблиц, висевшие на устройстве, пропали вместе с ним."""
    try:
        return int(Path(f"/sys/class/net/{PAIR_IF}/ifindex").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def link_stamp() -> tuple[int, int]:
    """(индекс устройства, mtime конфига линка): смена любого — линк пересоздан или переподнят
    из конфига, рантайм-состояние (широкие AllowedIPs, маршруты таблиц) надо утвердить заново."""
    try:
        mtime = int(Path(f"{config.AWG_DIR}/{PAIR_IF}.conf").stat().st_mtime_ns)
    except OSError:
        mtime = 0
    return ifindex(), mtime


def handshake_age() -> float | None:
    """Секунд с последнего рукопожатия линка пары; None — линка нет или рукопожатия не было."""
    res = base._host(["awg", "show", PAIR_IF, "latest-handshakes"], check=False, timeout=base._PROBE_TIMEOUT)
    if res.returncode != 0:
        return None
    for line in res.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            ts = int(parts[1])
            return (time.time() - ts) if ts else None
    return None


def pair_alive() -> bool:
    age = handshake_age()
    return age is not None and age <= HANDSHAKE_MAX_S
