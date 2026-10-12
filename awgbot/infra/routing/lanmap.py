"""lanmap.py — таблица `inet awg_lan_map`: перевод адресов в туннеле локальных
сетей шлюзов в настоящие и обратно (схема и формулы — util/lanspace).

Из записей «слот, линк, настоящая подсеть, двойник» собирается одна таблица:

  zone    prerouting, raw — зона conntrack по неупорядоченной паре сторон
          пакета: из сети какого слота он пришёл и в адреса какого слота идёт
          (z = 1 + 16·min + max; обе стороны 0 — зона 0, как у всего прочего).
          Без зон одинаковые настоящие адреса за разными шлюзами спорили бы
          не за маршрут, а за запись conntrack: после перевода ответные
          кортежи двух соединений совпадают;
  mark    prerouting, mangle — бит слота по адресу в туннеле ДО перевода:
          маршрутизатор увидит уже настоящий адрес, а он может быть за двумя
          шлюзами сразу. Бит выбирает таблицу слота (`fwmark → lookup 10N`,
          infra/routing/slots) — та же, что у зонда резерва;
  dstnat  prerouting, dstnat − 10 — двойник → настоящая подсеть; раньше
          перехвата :53 обвязки хоста (−100), чтобы DNS к адресу в туннеле
          доходил до DNS самой сети;
  srcnat  postrouting, srcnat − 10 — источник из сети слота, пришедший его
          линком, → его адрес в туннеле: снаружи шлюза сеть видна только так;
  guard   forward, filter − 10 — адрес в туннеле без настоящей подсети
          дальше не идёт; помеченное слоту — только в его линк (лежит линк —
          не к провайдеру).

Применение — атомарная замена (`table; delete table; table {…}` одним
`nft -f`), как у nftguard; текст равен тексту ⇔ состояние равно состоянию —
им сверяет сервис (state `lanmap_text`). Файла в /etc/nftables.d нет: без
таблиц и правил слотов, которые ставит бот, таблица после ребута была бы
половиной механизма — её ставит бот на старте.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

from awgbot.infra import nftjson

from . import base
from .slots import slot_mark

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_lan_map"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"


class Entry(NamedTuple):
    """Подсеть слота в таблице: номер слота, его линк, настоящая подсеть, двойник."""
    slot: int
    link: str
    real: str
    twin: str


def zone(a: int, b: int) -> int:
    """Зона conntrack пары сторон {a, b} (0 — не слот): 0 только у {0, 0};
    иначе 1 + 16·min + max — от 2 до 256 для слотов 0…15, пары различаются,
    порядок сторон не важен (ответ получает зону запроса)."""
    a, b = int(a), int(b)
    if a == 0 and b == 0:
        return 0
    return 1 + 16 * min(a, b) + max(a, b)


def _by_slot(entries: Iterable[Entry]) -> dict[int, tuple[str, list[Entry]]]:
    out: dict[int, tuple[str, list[Entry]]] = {}
    for e in sorted(entries, key=lambda x: (int(x.slot), x.twin)):
        out.setdefault(int(e.slot), (e.link, []))[1].append(e)
    return out


def render(entries: Iterable[Entry], space: str, prefixes: dict[int, str]) -> str:
    """Полный текст таблицы; записей нет — пусто (таблицы быть не должно).
    prefixes — {слот: его /16 в туннеле}, space — всё пространство (guard).
    Порядок правил цепочки zone важен: каждое завершает цепочку (accept),
    поэтому сначала пары «сеть слота A → адреса слота B», потом «из сети
    слота», потом «в адреса слота»."""
    slots = _by_slot(entries)
    if not slots:
        return ""
    ids = list(slots)
    out = [
        "#!/usr/sbin/nft -f",
        "# awg-bot: адреса в туннеле для локальных сетей шлюзов (таблица awg_lan_map).",
        "# Текст собирает бот из БД и ставит сам — правки руками перезапишутся.",
        f"table {TABLE}",
        f"delete table {TABLE}",
        f"table {TABLE} {{",
    ]
    for sid in ids:
        nets = ", ".join(e.real for e in slots[sid][1])
        out.append(f"    set real{sid} {{ type ipv4_addr; flags interval; elements = {{ {nets} }} }}")
    out += ["", "    chain zone {",
            "        type filter hook prerouting priority raw; policy accept;",
            "        fib daddr type local accept"]
    for a in ids:
        for b in ids:
            if a != b:
                out.append(f'        iifname "{slots[a][0]}" ip saddr @real{a} ip daddr {prefixes[b]} '
                           f"ct zone set {zone(a, b)} accept")
    for a in ids:
        out.append(f'        iifname "{slots[a][0]}" ip saddr @real{a} ct zone set {zone(a, 0)} accept')
    for b in ids:
        out.append(f"        ip daddr {prefixes[b]} ct zone set {zone(0, b)} accept")
    out += ["    }", "", "    chain mark {",
            "        type filter hook prerouting priority mangle; policy accept;"]
    for sid in ids:
        out.append(f"        ip daddr {prefixes[sid]} meta mark set meta mark or 0x{slot_mark(sid):x}")
    pairs = ", ".join(f"{e.twin} : {e.real}" for sid in ids for e in slots[sid][1])
    out += ["    }", "", "    chain dstnat {",
            "        type nat hook prerouting priority dstnat - 10; policy accept;",
            f"        dnat ip prefix to ip daddr map {{ {pairs} }}",
            "    }", "", "    chain srcnat {",
            "        type nat hook postrouting priority srcnat - 10; policy accept;"]
    for sid in ids:
        link, items = slots[sid]
        back = ", ".join(f"{e.real} : {e.twin}" for e in items)
        out.append(f'        iifname "{link}" snat ip prefix to ip saddr map {{ {back} }}')
    out += ["    }", "", "    chain guard {",
            "        type filter hook forward priority filter - 10; policy accept;",
            f"        ip daddr {space} drop"]
    for sid in ids:
        m = f"0x{slot_mark(sid):x}"
        out.append(f'        meta mark and {m} == {m} oifname != "{slots[sid][0]}" drop')
    out += ["    }", "}"]
    return "\n".join(out) + "\n"


# ── nft: применить, снять, проверить ─────────────────────────────────────────

def _nft(args: list[str], stdin: str = "") -> object:
    return nftjson.run(args, timeout=15, stdin=stdin, error=base.RoutingError)


def apply(text: str) -> None:
    """Проверить (`nft -c`) и применить атомарно. RoutingError — не принято."""
    for args in (["-c", "-f", "-"], ["-f", "-"]):
        proc = _nft(args, stdin=text)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise base.RoutingError(f"nft {' '.join(args)}: {err[:300]}")
    base.log.info("routing: таблица %s поставлена", TABLE)


def remove() -> bool:
    """Снять таблицу. True — была и снята; нет её — не ошибка."""
    return _nft(["delete", "table", TABLE_FAMILY, TABLE_NAME]).returncode == 0


def present() -> bool:
    """Стоит ли таблица в ядре — по списку таблиц, без её содержимого."""
    proc = _nft(["list", "tables"])
    if proc.returncode != 0:
        raise base.RoutingError("nft list tables: " + proc.stderr.decode(errors="replace").strip()[:200])
    return f"table {TABLE}" in proc.stdout.decode(errors="replace").splitlines()


__all__ = ["TABLE", "TABLE_FAMILY", "TABLE_NAME", "Entry", "zone", "render", "apply", "remove", "present"]
