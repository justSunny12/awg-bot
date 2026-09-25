"""
rfacct.py — учёт РФ-трафика на сервере AWG.

Сколько байт сервер прогнал между туннелем клиентов и линками шлюзов, то есть
выпустил в интернет с российского адреса шлюза: в целом и по каждому
устройству. Отдельная таблица nft `inet awg_bot_acct` без единого вердикта:
цепочка на хуке forward с `policy accept` сразу после файервола (`priority
filter + 10`) и четыре правила-счётчика. Почему не в `awg_bot_guard`: там
первым стоит `ct state established accept` (счётчик рядом видел бы только
первый пакет соединения), а сама таблица пересоздаётся через `delete table`
при каждом реассерте и обнуляла бы показания.

Что попадает в счётчики — по построению, без отдельных исключений: `saddr` из
клиентских подсетей (основная и подсеть переезда), `oifname` — линк, `daddr`
не частный; обратно зеркально. Доступ админа к локальным подсетям (частный
`daddr`), доступ между подсетями (`saddr` не клиентский), канал линка (не
forward), аплинк шлюза (уходит в WAN, не в линк) — мимо.

Разбивка по устройствам — именованные счётчики `d<id>_up`/`d<id>_dn` и две
объектные карты «адрес → счётчик»: счётчик привязан к id устройства, смена
хозяина адреса и перезапись цепочки его не обнуляют. Показания только
читаются (`list`), никогда не сбрасываются: дельты считает опрос по базам и
поколению `boot_id:handle таблицы` (перезагрузка, `flush ruleset` → счётчики с
нуля → всё текущее значение идёт в дельту).
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from typing import Optional

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_bot_acct"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
CHAIN = "forward"
SET_CLIENTS = "clients4"
SET_PRIVATE = "private4"
MAP_UP = "rf_dev_up"
MAP_DN = "rf_dev_dn"
COUNTER_UP = "rf_up"
COUNTER_DN = "rf_dn"
RULES = 4                               # правил в цепочке forward: по паре на направление
# тот же список, что PRIVATE_NETS обвязки шлюза (install/routing-gw-setup.sh)
PRIVATE_NETS = ("10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16")


class AcctError(RuntimeError):
    """nft не удался — опрос пишет ошибку в состояние, обычное потребление копится."""


@dataclass
class AcctState:
    """Что прочитано из ядра одним `nft -j list table`."""
    handle: int = 0
    counters: dict[str, int] = field(default_factory=dict)      # имя → байты
    up: dict[str, str] = field(default_factory=dict)            # адрес → имя счётчика (карта ↑)
    dn: dict[str, str] = field(default_factory=dict)            # адрес → имя счётчика (карта ↓)
    rules: int = 0                                              # правил в цепочке


def is_device_counter(name: str) -> bool:
    """d<ID>_up — счётчик устройства (парный d<ID>_dn — по имени)."""
    return name.startswith("d") and name.endswith("_up")


def counter_names(device_id: int) -> tuple[str, str]:
    return f"d{int(device_id)}_up", f"d{int(device_id)}_dn"


def _ifs(names: list[str]) -> str:
    return "{ " + ", ".join(f'"{n}"' for n in names) + " }"


def _nets(nets) -> list[str]:
    out: list[str] = []
    for n in nets or []:
        try:
            s = str(ipaddress.IPv4Network(str(n).strip(), strict=False))
        except ValueError:
            continue
        if s not in out:
            out.append(s)
    return out


def _addr(a: str) -> str:
    try:
        return str(ipaddress.IPv4Address(str(a).strip().split("/")[0]))
    except ValueError:
        return ""


def render_static(links: list[str], subnets: list[str]) -> str:
    """Неизменяемая при опросе часть: таблица, наборы, цепочка и правила. Без
    `delete table`: именованные счётчики и карты переживают `flush chain`."""
    links = [str(x) for x in links if x]
    subs = _nets(subnets)
    priv = ", ".join(PRIVATE_NETS)
    lines = [f"add table {TABLE}",
             f"add set {TABLE} {SET_CLIENTS} {{ type ipv4_addr; flags interval; }}",
             f"flush set {TABLE} {SET_CLIENTS}"]
    if subs:
        lines.append(f"add element {TABLE} {SET_CLIENTS} {{ {', '.join(subs)} }}")
    lines += [f"add set {TABLE} {SET_PRIVATE} {{ type ipv4_addr; flags interval; }}",
              f"flush set {TABLE} {SET_PRIVATE}",
              f"add element {TABLE} {SET_PRIVATE} {{ {priv} }}",
              f"add map {TABLE} {MAP_UP} {{ type ipv4_addr : counter; }}",
              f"add map {TABLE} {MAP_DN} {{ type ipv4_addr : counter; }}",
              f"add counter {TABLE} {COUNTER_UP}",
              f"add counter {TABLE} {COUNTER_DN}",
              f"add chain {TABLE} {CHAIN} {{ type filter hook forward priority filter + 10; policy accept; }}",
              f"flush chain {TABLE} {CHAIN}"]
    if links and subs:
        ifs = _ifs(links)
        lines += [
            f"add rule {TABLE} {CHAIN} oifname {ifs} ip saddr @{SET_CLIENTS} ip daddr != @{SET_PRIVATE} counter name \"{COUNTER_UP}\"",
            f"add rule {TABLE} {CHAIN} oifname {ifs} ip saddr @{SET_CLIENTS} ip daddr != @{SET_PRIVATE} counter name ip saddr map @{MAP_UP}",
            f"add rule {TABLE} {CHAIN} iifname {ifs} ip daddr @{SET_CLIENTS} ip saddr != @{SET_PRIVATE} counter name \"{COUNTER_DN}\"",
            f"add rule {TABLE} {CHAIN} iifname {ifs} ip daddr @{SET_CLIENTS} ip saddr != @{SET_PRIVATE} counter name ip daddr map @{MAP_DN}",
        ]
    return "\n".join(lines) + "\n"


def render_devices(state: Optional[AcctState], devices: list[tuple[int, str]]) -> str:
    """Дифф состава счётчиков и карт против прочитанного. Новое устройство —
    два `add counter` и элемент в обе карты; ушедшее — сначала элементы, потом
    счётчики (карта ссылается на счётчик); адрес сменился — перевставка."""
    st = state or AcctState()
    want: dict[str, tuple[int, str]] = {}          # имя счётчика ↑ → (id, адрес)
    for dev_id, addr in devices:
        a = _addr(addr)
        if a:
            want[counter_names(dev_id)[0]] = (int(dev_id), a)
    have_up = {v: k for k, v in st.up.items()}     # имя счётчика → адрес
    have_dn = {v: k for k, v in st.dn.items()}
    lines: list[str] = []
    removed = {MAP_UP: set(), MAP_DN: set()}     # элементы, уже снятые в этом скрипте

    def drop(mapname: str, addr: str) -> None:
        if addr not in removed[mapname]:
            removed[mapname].add(addr)
            lines.append(f"delete element {TABLE} {mapname} {{ {addr} }}")

    # ушедшие: по любому следу в ядре (счётчик или элемент карты)
    stale = {n for n in list(st.counters) + list(have_up) + list(have_dn)
             if is_device_counter(n) and n not in want}
    for up_name in sorted(stale):
        dn_name = up_name[:-3] + "_dn"
        if up_name in have_up:
            drop(MAP_UP, have_up[up_name])
        if dn_name in have_dn:
            drop(MAP_DN, have_dn[dn_name])
        if up_name in st.counters:
            lines.append(f"delete counter {TABLE} {up_name}")
        if dn_name in st.counters:
            lines.append(f"delete counter {TABLE} {dn_name}")
    for up_name, (dev_id, addr) in sorted(want.items()):
        dn_name = counter_names(dev_id)[1]
        if up_name not in st.counters:
            lines.append(f"add counter {TABLE} {up_name}")
        if dn_name not in st.counters:
            lines.append(f"add counter {TABLE} {dn_name}")
        for name, mapping, have, mapname in ((up_name, st.up, have_up, MAP_UP),
                                             (dn_name, st.dn, have_dn, MAP_DN)):
            cur_addr = have.get(name)
            if cur_addr == addr:
                continue
            if cur_addr is not None:
                drop(mapname, cur_addr)
            if mapping.get(addr) not in (None, name):
                # адрес перешёл к другому устройству — элемент старого хозяина снять
                drop(mapname, addr)
            lines.append(f"add element {TABLE} {mapname} {{ {addr} : \"{name}\" }}")
    return ("\n".join(lines) + "\n") if lines else ""


def _nft(args: list[str], stdin: str = "", timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["nft", *args], input=stdin.encode() if stdin else None,
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise AcctError("nft не найден — поставь пакет nftables")
    except subprocess.TimeoutExpired:
        raise AcctError(f"таймаут nft {' '.join(args[:3])}")
    except OSError as e:
        raise AcctError(str(e))


def _elem_key(el) -> str:
    if isinstance(el, str):
        return el
    if isinstance(el, dict):
        if "prefix" in el:
            return f"{el['prefix']['addr']}/{el['prefix']['len']}"
        if "elem" in el:
            return _elem_key(el["elem"].get("val") if isinstance(el["elem"], dict) else el["elem"])
        if "val" in el:
            return _elem_key(el["val"])
    return str(el)


def parse(doc: dict) -> Optional[AcctState]:
    """Разбор `nft -j list table`: handle таблицы, байты счётчиков, элементы
    карт, число правил. None — таблицы в документе нет."""
    st = AcctState()
    seen_table = False
    for item in doc.get("nftables", []) or []:
        if not isinstance(item, dict):
            continue
        if "table" in item and item["table"].get("name") == TABLE_NAME:
            seen_table = True
            st.handle = int(item["table"].get("handle") or 0)
        elif "counter" in item and item["counter"].get("table") == TABLE_NAME:
            c = item["counter"]
            st.counters[str(c.get("name") or "")] = int(c.get("bytes") or 0)
        elif "map" in item and item["map"].get("table") == TABLE_NAME:
            m = item["map"]
            target = st.up if m.get("name") == MAP_UP else st.dn if m.get("name") == MAP_DN else None
            if target is None:
                continue
            for el in m.get("elem", []) or []:
                if isinstance(el, list) and len(el) == 2:
                    target[_elem_key(el[0])] = str(el[1])
                elif isinstance(el, dict) and "elem" in el and isinstance(el["elem"], dict):
                    inner = el["elem"]
                    target[_elem_key(inner.get("val"))] = str(inner.get("val_obj") or inner.get("obj") or "")
        elif "rule" in item and item["rule"].get("table") == TABLE_NAME:
            st.rules += 1
    return st if seen_table else None


def read() -> Optional[AcctState]:
    """Состояние таблицы из ядра; None — таблицы нет."""
    proc = _nft(["-j", "list", "table", TABLE_FAMILY, TABLE_NAME])
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace")
        if "No such file or directory" in err or "does not exist" in err:
            return None
        raise AcctError(f"nft list table: {err.strip()[:200]}")
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise AcctError(f"nft -j: {e}")
    return parse(doc)


def apply(script: str) -> None:
    """Проверить `nft -c -f -` и применить `nft -f -` одной транзакцией."""
    if not script.strip():
        return
    check = _nft(["-c", "-f", "-"], stdin=script)
    if check.returncode != 0:
        raise AcctError(f"nft -c: {check.stderr.decode(errors='replace').strip()[:200]}")
    proc = _nft(["-f", "-"], stdin=script)
    if proc.returncode != 0:
        raise AcctError(f"nft -f: {proc.stderr.decode(errors='replace').strip()[:200]}")


_static_applied = ""                     # хэш последней применённой статики (в памяти)


def sync(state: Optional[AcctState], links: list[str], subnets: list[str],
         devices: list[tuple[int, str]]) -> bool:
    """Привести таблицу к желаемому составу. Статика (наборы, цепочка,
    правила) переписывается только когда изменилась с последнего применения
    в этом процессе или таблицы нет; счётчики и карты — по диффу. Возвращает,
    было ли что писать."""
    global _static_applied
    if not links:
        return False
    static = render_static(links, subnets)
    digest = hashlib.sha256(static.encode()).hexdigest()
    script = ""
    if state is None or digest != _static_applied or state.rules != RULES:
        script += static
    script += render_devices(state, devices)
    if not script:
        return False
    apply(script)
    _static_applied = digest
    return True


def delta(prev_gen: str, gen: str, prev: Optional[int], cur: int) -> int:
    """Сколько прибавить к месяцу по одному счётчику:
    поколения не было — только база; поколение другое — всё текущее; базы нет
    — всё текущее; показание упало — всё текущее; иначе разность."""
    if not prev_gen:
        return 0
    if prev_gen != gen or prev is None or cur < prev:
        return max(0, int(cur))
    return int(cur) - int(prev)
