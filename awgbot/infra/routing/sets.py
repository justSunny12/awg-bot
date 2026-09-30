"""sets.py — плечо контейнера — исключения из MASQUERADE; наборы ipset."""

from __future__ import annotations

from typing import Optional

from awgbot.core import config
from awgbot.infra import awg

from . import base

# ─────────────────────────────────────────────────────────────────────────────
# Плечо КОНТЕЙНЕРА: исключения из MASQUERADE
# ─────────────────────────────────────────────────────────────────────────────

def sync_nat_exempt(addresses) -> None:
    """Пересобрать цепочку исключений из MASQUERADE в контейнере.

    ACCEPT, а не RETURN: RETURN вернул бы пакет в POSTROUTING, где его подобрало
    бы следующее правило — то самое MASQUERADE, от которого мы уходим. ACCEPT в
    таблице nat завершает обход цепочки, и адрес остаётся настоящим.

    Хук вставляется ПЕРВЫМ правилом POSTROUTING: любое правило перед ним могло бы
    замаскарадить пакет раньше, чем мы до него доберёмся.

    В host-режиме — пусто. Не «нечего делать по случайности»: там MASQUERADE один
    и наш, клиент приходит на маркировку с настоящим адресом, и отменять нечего.
    Механизм исключений существовал ровно ради чужого MASQUERADE контейнера.
    """
    if not awg.in_container():
        return
    chain = config.ROUTING_NAT_CHAIN
    want = set()
    for addr in addresses:
        awg._validate_ip(addr)
        want.add(addr)
    # дифф перед записью: один `-S` вместо флаша и правила на устройство
    have = nat_exempt_addresses()
    if have != want:
        base._cont(["iptables", "-t", "nat", "-N", chain], check=False)   # есть → код 1
        base._cont(["iptables", "-t", "nat", "-F", chain])
        for addr in sorted(want):
            base._cont(["iptables", "-t", "nat", "-A", chain,
                   "-s", f"{addr}/32", "-j", "ACCEPT"])
    if not base._cont_ok(["iptables", "-t", "nat", "-C", "POSTROUTING", "-j", chain]):
        base._cont(["iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-j", chain])


def parse_nat_exempt(text: str, chain: str) -> Optional[set[str]]:
    """Адреса из `iptables -t nat -S <chain>`; None — цепочки нет или в ней
    правило чужой формы (тогда безопаснее пересобрать)."""
    out: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("-N", "-P"):
            continue
        if parts[:2] != ["-A", chain] or "-s" not in parts or parts[-2:] != ["-j", "ACCEPT"]:
            return None
        out.add(parts[parts.index("-s") + 1].split("/")[0])
    return out


def nat_exempt_addresses() -> Optional[set[str]]:
    proc = base._cont(["iptables", "-t", "nat", "-S", config.ROUTING_NAT_CHAIN], check=False)
    if proc.returncode != 0:
        return None
    return parse_nat_exempt(proc.stdout.decode(errors="replace"), config.ROUTING_NAT_CHAIN)


def ensure_set(name: str, kind: str, exists: bool = False) -> None:
    """Создать набор, если его нет. Содержимое НЕ трогает.

    Для доменных наборов это единственная допустимая операция со стороны бота:
    наполняет их dnsmasq по мере резолва, и любая перезапись стирала бы всё
    накопленное. Бот отвечает лишь за то, чтобы набор существовал к моменту,
    когда на него сошлётся правило или директива ipset=.
    exists=True — вызывающий уже видел набор в снимке `ipset save`: exec не нужен.
    """
    if exists:
        return
    base._host(["ipset", "create", name, kind, "-exist"])


def parse_ipset_save(text: str) -> dict[str, set[str]]:
    """`ipset save` → {набор: множество членов}. Пустой набор — пустое
    множество: строка `create` есть, строк `add` нет."""
    sets: dict[str, set[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "create":
            sets.setdefault(parts[1], set())
        elif len(parts) >= 3 and parts[0] == "add":
            sets.setdefault(parts[1], set()).add(parts[2])
    return sets


def snapshot_sets(only: Optional[list[str]] = None) -> Optional[dict[str, set[str]]]:
    """Состав наборов; None — не смогли прочитать (тогда вызывающий
    пересобирает всё безусловно, как раньше). С only — имена всех наборов
    (ipset list -n) и состав только перечисленных: полный `ipset save` каждый
    тик вычитывал тысячи адресов доменных наборов ради имён и src."""
    if only is None:
        proc = base._host(["ipset", "save"], check=False, timeout=base._PROBE_TIMEOUT)
        if proc.returncode != 0:
            return None
        return parse_ipset_save(proc.stdout.decode(errors="replace"))
    proc = base._host(["ipset", "list", "-n"], check=False, timeout=base._PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    sets: dict[str, set[str]] = {n.strip(): set() for n in proc.stdout.decode(errors="replace").splitlines()
                                 if n.strip()}
    for name in only:
        if name not in sets:
            continue
        one = base._host(["ipset", "save", name], check=False, timeout=base._PROBE_TIMEOUT)
        if one.returncode != 0:
            return None
        sets.update(parse_ipset_save(one.stdout.decode(errors="replace")))
    return sets



def list_sets() -> list[str]:
    proc = base._host(["ipset", "list", "-n"], check=False, timeout=base._PROBE_TIMEOUT)
    return [l.strip() for l in proc.stdout.decode(errors="replace").splitlines() if l.strip()]


def replace_members(name: str, kind: str, members, current=None) -> None:
    """Атомарно заменить содержимое набора.

    Через временный набор и `ipset swap`, а не flush+add: flush оставляет окно, в
    котором набор пуст, и в это окно трафик уходит мимо маршрута. Для src-набора
    это означало бы моргание режима у пользователя на каждой реконсиляции.

    current — состав набора из снимка `ipset save` (None — снимка нет или
    набора в нём нет). Совпадает с желаемым — шесть exec не нужны.
    """
    members = list(members)
    if current is not None and set(members) == set(current):
        return
    tmp = f"{name}_tmp"
    ensure_set(name, kind, exists=current is not None)
    base._host(["ipset", "destroy", tmp], check=False)
    ensure_set(tmp, kind)
    payload = "".join(f"add {tmp} {m}\n" for m in members)
    if payload:
        base._host(["ipset", "restore", "-exist"], input_data=payload.encode())
    base._host(["ipset", "swap", tmp, name])
    base._host(["ipset", "destroy", tmp], check=False)


def destroy_set(name: str) -> None:
    base._host(["ipset", "destroy", name], check=False)
