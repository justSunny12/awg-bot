"""
plan.py — часть пакета backup (см. __init__): план восстановления — линия
базы, сопоставление шлюзов копии с текущими, числа для вопроса.

Одна функция на бота и терминал: бот показывает план в вопросе, а
tools/restore_merge.py по тому же плану сливает копию с текущим. Читает обе
базы только на чтение и файлы линков на диске; ничего не пишет.

Линия базы (db_line): та же — тот же сервер или его продолжение; другая —
чистый сервер (профилей нет, кроме служебного и админа), текущая база не
читается или копия чужого сервера на сервер со своими данными. Ключ
сопоставления шлюзов — номер слота; внутри слота ключи говорят, что
изменилось.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field

from awgbot.core import config
from awgbot.util import timeutil

from . import meta as _meta

SAME, CLEAN, UNREADABLE, FOREIGN = "same", "clean", "unreadable", "foreign"

# случаи слота (§ сопоставление)
C_SAME, C_REKEYED, C_DEVICE, C_TWIN = "same", "rekeyed", "device", "twin"
C_REMOVED, C_RESTORED, C_ADDED = "removed", "restored", "added"

REMOVED_KEY = "gw_removed"             # журнал снятых слотов (gateway_link/mark.py)


@dataclass
class SlotPlan:
    slot: int
    case: str
    name: str = ""
    label: str = ""
    link_if: str = ""


@dataclass
class Plan:
    line: str
    empty: bool = False                # слотов нет и ни один не снимался: шлюзовая часть — из копии целиком
    slots: list[SlotPlan] = field(default_factory=list)
    replaced: tuple[int, int] = (0, 0)  # чужая копия: (профили, устройства), которые пропадут
    expired_now: int = 0                # другая линия: подписки, истёкшие к моменту восстановления
    host_copy: str = ""
    host_cur: str = ""
    first_slot_back: bool = False      # база не читается: слот 1 по файлам — мог быть снят
    created_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["replaced"] = list(self.replaced)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        d = dict(d or {})
        d["slots"] = [SlotPlan(**s) for s in d.get("slots") or []]
        d["replaced"] = tuple(d.get("replaced") or (0, 0))
        return cls(**d)

    def signature(self) -> tuple:
        """Что человек видел в вопросе: вид линии и случай каждого слота."""
        return (self.line, bool(self.empty), tuple(sorted((s.slot, s.case) for s in self.slots)))

    def slot(self, n: int) -> SlotPlan | None:
        return next((s for s in self.slots if s.slot == n), None)

    @property
    def same_line(self) -> bool:
        return self.line == SAME

    def current_slots(self) -> list[int]:
        """Слоты, которые остаются текущими (копия к ним применяется с их
        связностью)."""
        return [s.slot for s in self.slots if s.case in (C_SAME, C_REKEYED, C_DEVICE, C_TWIN, C_ADDED)]

    def offer_slots(self) -> list[int]:
        """Текущие слоты, которым предлагается копия их настроек из бэкапа:
        добавленным после бэкапа (и снятым и назначенным заново) — нечего."""
        return [s.slot for s in self.slots if s.case in (C_SAME, C_REKEYED, C_DEVICE, C_TWIN)]

    def removed_slots(self) -> list[int]:
        """Слоты бэкапа, к которым он не применяется, и которых нет сейчас."""
        live = set(self.current_slots())
        return [s.slot for s in self.slots if s.case == C_REMOVED and s.slot not in live]

    def copy_slots(self) -> list[int]:
        """Слоты, которые встают из копии целиком (пустой сервер)."""
        return [s.slot for s in self.slots if s.case == C_RESTORED]


def _ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise sqlite3.OperationalError("нет файла базы")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("SELECT 1 FROM server_state LIMIT 1").fetchall()   # не база или чужая схема — DatabaseError
    return con


def _state(con, key: str) -> str:
    row = con.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else ""


def removed_journal(con) -> list[dict]:
    try:
        data = json.loads(_state(con, REMOVED_KEY) or "[]")
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _slots(con, awg_dir: str, root_dir: str) -> dict[int, dict]:
    """Слоты базы: {номер: {link_if, label, name, device_pub, twin_pub,
    link_gw_pub, link_vps_pub}} — ключи линка по файлам на диске."""
    out = {}
    for g in con.execute("SELECT id, device_id, link_if, label FROM gateways ORDER BY id"):
        dev = con.execute("SELECT id, name, public_key, twin_of FROM devices WHERE id = ?",
                          (g["device_id"],)).fetchone()
        twin = None
        if dev is not None:
            twin = con.execute("SELECT public_key FROM devices WHERE twin_of = ? OR id = ?",
                               (dev["id"], dev["twin_of"] or -1)).fetchone()
        gw_pub, vps_pub = _meta.link_pubs(g["link_if"], awg_dir=awg_dir, root_dir=root_dir)
        out[int(g["id"])] = {"link_if": g["link_if"], "label": g["label"] or "",
                             "name": dev["name"] if dev is not None else "",
                             "device_pub": dev["public_key"] if dev is not None else "",
                             "twin_pub": twin["public_key"] if twin is not None else "",
                             "link_gw_pub": gw_pub, "link_vps_pub": vps_pub}
    return out


def _profiles_besides_admin(con) -> int:
    return int(con.execute("SELECT COUNT(*) FROM clients WHERE is_service = 0 "
                           "AND (tg_id IS NULL OR tg_id != ?)", (config.ADMIN_ID,)).fetchone()[0])


def _expired_now(con, now) -> int:
    n = 0
    for r in con.execute("SELECT s.period_end FROM clients c JOIN client_subscription s ON s.client_id = c.id "
                         "WHERE c.is_service = 0 AND c.activation_status = 'active' "
                         "AND s.status = 'active' AND s.period_end IS NOT NULL"):
        try:
            if timeutil.remaining_seconds(timeutil.parse_iso(r[0]), now) <= 0:
                n += 1
        except (ValueError, TypeError):
            continue
    return n


def _case(copy: dict, cur: dict, *, by_files: bool) -> str:
    if not by_files and copy.get("device_pub") != cur.get("device_pub"):
        return C_DEVICE
    if (copy.get("link_gw_pub"), copy.get("link_vps_pub")) != (cur.get("link_gw_pub"), cur.get("link_vps_pub")):
        return C_REKEYED
    if not by_files and copy.get("twin_pub", "") != cur.get("twin_pub", ""):
        return C_TWIN
    return C_SAME


def _slot_plan(n: int, src: dict, case: str) -> SlotPlan:
    return SlotPlan(slot=n, case=case, name=str(src.get("name") or ""), label=str(src.get("label") or ""),
                    link_if=str(src.get("link_if") or ""))


def _readded(journal: list[dict], copy_gws: dict, created_at: str) -> set[int]:
    """Слоты бэкапа, снятые после него (журнал: снят позже даты бэкапа)."""
    out = set()
    try:
        made = timeutil.parse_iso(created_at)
    except (ValueError, TypeError):
        return out
    for r in journal:
        try:
            n = int(r.get("slot") or 0)
            when = timeutil.parse_iso(str(r.get("at") or ""))
        except (ValueError, TypeError):
            continue
        if n in copy_gws and timeutil.elapsed(made, when) >= 0:   # слот в бэкапе есть — снят не раньше
            out.add(n)
    return out


def plan(meta: dict, copy_db: str, cur_db: str, *, awg_dir: str | None = None,
         root_dir: str | None = None, host_copy: str = "", host_cur: str = "", now=None) -> Plan:
    """План по метке и базе копии и по текущей базе."""
    awg_dir = awg_dir or config.AWG_DIR
    root_dir = root_dir or _meta.ROOT_DIR
    now = now or timeutil.now()
    copy_gws = {int(g["slot"]): g for g in meta.get("gateways") or [] if isinstance(g, dict) and g.get("slot")}
    p = Plan(line=FOREIGN, created_at=str(meta.get("created_at") or ""),
             host_copy=host_copy if host_copy != host_cur else "",
             host_cur=host_cur if host_copy != host_cur else "")
    try:
        cur = _ro(cur_db)
    except sqlite3.Error:
        cur = None
    with sqlite3.connect(f"file:{copy_db}?mode=ro", uri=True) as cp:
        copy_line = str(meta.get("db_line") or "") or _state(cp, "db_line")
        if cur is None:
            p.line = UNREADABLE
        else:
            cur_line = _state(cur, "db_line")
            if cur_line and cur_line == copy_line:
                p.line = SAME
            elif _profiles_besides_admin(cur) == 0:
                p.line = CLEAN
            else:
                p.line = FOREIGN
        if p.line != SAME:
            p.expired_now = _expired_now(cp, now)
    try:
        if cur is None:
            # базы нет — слоты по файлам линков на диске: на них живут шлюзы
            cur_slots = {}
            for n, g in copy_gws.items():
                gw_pub, vps_pub = _meta.link_pubs(g.get("link_if", ""), awg_dir=awg_dir, root_dir=root_dir)
                if gw_pub and vps_pub:
                    cur_slots[n] = {**g, "link_gw_pub": gw_pub, "link_vps_pub": vps_pub}
            journal = []
        else:
            cur_slots = _slots(cur, awg_dir, root_dir)
            journal = removed_journal(cur)
        # пустой сервер — только на другой линии: на той же журнал снятых
        # чистится по сроку, и «слотов нет» значит «все сняли», а не «новый»
        p.empty = p.line != SAME and cur is not None and not cur_slots and not journal
        if p.line == FOREIGN and cur is not None:
            slot_devs = set()
            for n in cur_slots:
                row = cur.execute("SELECT device_id FROM gateways WHERE id = ?", (n,)).fetchone()
                if row is not None:
                    slot_devs.add(int(row[0]))
                    slot_devs.update(int(r[0]) for r in cur.execute(
                        "SELECT id FROM devices WHERE twin_of = ?", (int(row[0]),)))
            devs = sum(1 for r in cur.execute("SELECT d.id FROM devices d JOIN clients c ON c.id = d.client_id "
                                              "WHERE c.is_service = 0") if int(r[0]) not in slot_devs)
            p.replaced = (_profiles_besides_admin(cur), devs)
    finally:
        if cur is not None:
            cur.close()
    readded = _readded(journal, copy_gws, p.created_at)
    for n in sorted(set(copy_gws) | set(cur_slots)):
        copy, now_ = copy_gws.get(n), cur_slots.get(n)
        if copy and now_ and n in readded:
            # снят после бэкапа и назначен заново: в бэкапе — снятый, сейчас —
            # добавленный; настройки снятого новому не предлагаются
            p.slots.append(_slot_plan(n, copy, C_REMOVED))
            p.slots.append(_slot_plan(n, now_, C_ADDED))
            continue
        src = now_ or copy or {}
        sp = _slot_plan(n, src, "")
        if copy and now_:
            sp.case = _case(copy, now_, by_files=cur is None)
        elif copy:
            sp.case = C_RESTORED if p.empty else C_REMOVED
        else:
            sp.case = C_ADDED
        p.slots.append(sp)
    p.first_slot_back = cur is None and 1 in cur_slots
    return p


def plan_for_dir(meta: dict, copy_dir: str, *, cur_db: str | None = None, conf_dir: str | None = None,
                 awg_dir: str | None = None, root_dir: str | None = None, now=None) -> Plan:
    """План по распакованной копии (state/…) и текущему хосту: адреса сервера —
    из app.yaml копии и текущего."""
    cur_host = conf_host(os.path.join(conf_dir or str(config.CONF_DIR), "app.yaml"))
    return plan(meta, os.path.join(copy_dir, "state", "bot.db"), cur_db or str(config.DB_PATH),
                awg_dir=awg_dir, root_dir=root_dir,
                host_copy=conf_host(os.path.join(copy_dir, "state", "conf", "app.yaml")) or cur_host,
                host_cur=cur_host, now=now)


def conf_value(path: str, keys: tuple) -> str:
    """Значение из app.yaml по пути ключей; нет — пусто."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return ""
    for k in keys:
        if not isinstance(doc, dict):
            return ""
        doc = doc.get(k)
    return "" if doc is None or isinstance(doc, dict) else str(doc)


def conf_host(path: str) -> str:
    """network.server_host из app.yaml."""
    return conf_value(path, ("network", "server_host"))
