"""sets.py — наборы ipset."""

from __future__ import annotations

from typing import Optional

from . import base


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
