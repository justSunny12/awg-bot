"""data.py — часть пакета merge (см. __init__): база после восстановления.

Правило: бэкап возвращает решения админа и настройки; текущим остаётся всё,
что уже произошло во времени или ушло наружу (расход, сроки, блокировки,
отметки, погашенные коды, ключи устройств), и связность текущих шлюзов.

На той же линии базы основа — текущая база (состав профилей и устройств
текущий), в неё переносятся настройки из бэкапа для сущностей, которые есть в
обеих. На другой линии основа — база бэкапа, в неё переносятся текущие слоты
(строка, устройство и двойник, слотовые ключи) и то, что принадлежит хосту.
Пишет только в базу распакованного бэкапа; текущую читает.
"""
from __future__ import annotations

import json
import os
import sqlite3

from awgbot.core import config
from awgbot.domain.services.gateway_link import slotkeys

from .. import plan as _plan

# настройки сущностей, которые есть в обеих базах, — из бэкапа (та же линия)
CLIENT_FROM_COPY = ("name", "device_limit", "kind", "routing_allowed")
DEVICE_FROM_COPY = ("name", "routing_on")
# ключи server_state из бэкапа на той же линии; прочие — текущие
STATE_FROM_COPY = ("gwlink_own_lists", "email_login", "email_password", "private_dns_decision")
STATE_RESET = ("email_check",)            # почта из бэкапа проверится заново
# ключи server_state, текущие на любой линии: фраза, которую человек знает
# сейчас, журнал снятых слотов, факты бота и кода
STATE_ALWAYS_CURRENT = ("backup_passphrase", "backup_key", _plan.REMOVED_KEY, "bot_identity",
                        "restore_wait", "backup_event_due")
STATE_ALWAYS_CURRENT_PREFIX = ("update_",)

# таблицы, которые держат device_id (удаление устройства бэкапа при конфликте адреса)
_DEVICE_TABLES = ("device_traffic", "device_friend", "traffic_samples", "rf_samples", "migration_cohort")


def _cols(con, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def _state_get(con, key: str, schema: str = "main"):
    row = con.execute(f"SELECT value FROM {schema}.server_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row[0]


def _state_put(con, key: str, value) -> None:
    if value is None:
        con.execute("DELETE FROM server_state WHERE key = ?", (key,))
    else:
        con.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def snapshot(src: str, dst: str) -> None:
    """Согласованная копия базы (backup API: вместе с незачекпоинченным WAL)."""
    s = sqlite3.connect(src)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode = DELETE")
        finally:
            d.close()
    finally:
        s.close()


def _slot_devices(con, slot: int, schema: str = "main") -> list[int]:
    """Устройство слота и его двойник переезда."""
    row = con.execute(f"SELECT device_id FROM {schema}.gateways WHERE id = ?", (slot,)).fetchone()
    if row is None:
        return []
    dev = int(row[0])
    out = [dev]
    for r in con.execute(f"SELECT id FROM {schema}.devices WHERE twin_of = ? OR id = "
                         f"(SELECT twin_of FROM {schema}.devices WHERE id = ?)", (dev, dev)):
        if int(r[0]) not in out:
            out.append(int(r[0]))
    return out


def _keep_offer(copy_raw, cur_raw, created_at: str = ""):
    """gwkeep из бэкапа с пометкой offer (и датой бэкапа — для вопроса в чате
    шлюза); поле traffic — текущее."""
    try:
        keep = json.loads(copy_raw) if copy_raw else None
    except ValueError:
        keep = None
    if not isinstance(keep, dict):
        return None
    keep.pop("asked", None)
    keep["offer"] = "restore"
    keep["offer_from"] = str(created_at or "")
    try:
        cur = json.loads(cur_raw) if cur_raw else None
    except ValueError:
        cur = None
    if isinstance(cur, dict) and "traffic" in cur:
        keep["traffic"] = cur["traffic"]
    elif cur is not None:
        keep.pop("traffic", None)
    return json.dumps(keep, ensure_ascii=False)


def _offer_keys(con, slots, *, copy_schema: str, cur_schema: str | None, created_at: str = "") -> None:
    """Ключи слота с пометкой OFFER — из бэкапа (с offer). В бэкапе ключа нет
    (слот добавлен позже, агент старше) — остаётся текущий."""
    for n in slots:
        for name in slotkeys.names(merge=slotkeys.OFFER):
            key = slotkeys.key(name, n)
            cur_raw = _state_get(con, key, cur_schema) if cur_schema else None
            offered = _keep_offer(_state_get(con, key, copy_schema), cur_raw, created_at)
            _state_put(con, key, offered if offered is not None else cur_raw)


# ── та же линия ──────────────────────────────────────────────────────────────

def merge_same(result: str, copy_db: str, cur_db: str, p) -> None:
    """Основа — текущая база; из бэкапа — настройки профилей и устройств,
    которые есть в обеих базах, личные списки, ящик почты, решения админа."""
    snapshot(cur_db, result)
    con = sqlite3.connect(result)
    try:
        con.execute("ATTACH DATABASE ? AS cp", (copy_db,))
        slot_devs = set()
        for n in [g[0] for g in con.execute("SELECT id FROM gateways")]:
            slot_devs.update(_slot_devices(con, n))
        both = "id IN (SELECT id FROM cp.clients) AND is_service = 0"
        for col in CLIENT_FROM_COPY:
            con.execute(f"UPDATE clients SET {col} = (SELECT c.{col} FROM cp.clients c WHERE c.id = clients.id) "
                        f"WHERE {both}")
        con.execute("UPDATE client_quota SET traffic_limit = (SELECT q.traffic_limit FROM cp.client_quota q "
                    "WHERE q.client_id = client_quota.client_id) "
                    "WHERE client_id IN (SELECT client_id FROM cp.client_quota)")
        skip = ",".join(str(i) for i in slot_devs) or "-1"
        for col in DEVICE_FROM_COPY:
            con.execute(f"UPDATE devices SET {col} = (SELECT d.{col} FROM cp.devices d WHERE d.id = devices.id) "
                        f"WHERE id IN (SELECT id FROM cp.devices) AND id NOT IN ({skip})")
        con.execute("UPDATE device_traffic SET traffic_limit = (SELECT t.traffic_limit FROM cp.device_traffic t "
                    "WHERE t.device_id = device_traffic.device_id) "
                    f"WHERE device_id IN (SELECT device_id FROM cp.device_traffic) AND device_id NOT IN ({skip})")
        con.execute("DELETE FROM client_routing_domains WHERE client_id IN (SELECT id FROM cp.clients)")
        con.execute("INSERT OR IGNORE INTO client_routing_domains SELECT * FROM cp.client_routing_domains "
                    "WHERE client_id IN (SELECT id FROM clients)")
        for key in STATE_FROM_COPY:
            _state_put(con, key, _state_get(con, key, "cp"))
        for key in STATE_RESET:
            _state_put(con, key, None)
        _offer_keys(con, p.current_slots(), copy_schema="cp", cur_schema="main", created_at=p.created_at)
        con.commit()
        con.execute("DETACH DATABASE cp")
    finally:
        con.close()


# ── другая линия ─────────────────────────────────────────────────────────────

def _admin_client(con, cur) -> int:
    """Профиль админа в базе бэкапа; нет — переносится текущий."""
    row = con.execute("SELECT id FROM clients WHERE tg_id = ?", (config.ADMIN_ID,)).fetchone()
    if row is not None:
        return int(row[0])
    src = cur.execute("SELECT * FROM clients WHERE tg_id = ?", (config.ADMIN_ID,)).fetchone()
    if src is None:
        raise RuntimeError("профиля админа нет ни в бэкапе, ни в текущей базе")
    cols = [c for c in _cols(cur, "clients") if c != "id"]
    con.execute(f"INSERT INTO clients ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [src[c] for c in cols])
    cid = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    for table in ("client_subscription", "client_quota"):
        row = cur.execute(f"SELECT * FROM {table} WHERE client_id = ?", (src["id"],)).fetchone()
        if row is not None:
            tc = _cols(cur, table)
            con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(tc)}) VALUES ({','.join('?' * len(tc))})",
                        [cid if c == "client_id" else row[c] for c in tc])
    return cid


def _drop_device(con, dev_id: int) -> None:
    for table in _DEVICE_TABLES:
        con.execute(f"DELETE FROM {table} WHERE device_id = ?", (dev_id,))
    con.execute("UPDATE devices SET twin_of = NULL WHERE twin_of = ?", (dev_id,))
    con.execute("DELETE FROM devices WHERE id = ?", (dev_id,))


def _transplant(con, cur, slot: int, admin: int, lost: list[dict]) -> None:
    """Текущий слот — в базу бэкапа: строка слота, устройство и двойник с
    ключами и счётчиками, слотовые ключи CURRENT. Устройство бэкапа, занявшее
    адрес устройства слота, не восстанавливается — оно в итоге."""
    con.execute("DELETE FROM gateways WHERE id = ?", (slot,))
    dcols = [c for c in _cols(cur, "devices") if c not in ("id", "client_id", "twin_of")]
    id_map: dict[int, int] = {}
    cur_devs = _slot_devices(cur, slot)
    for dev_id in cur_devs:
        d = cur.execute("SELECT * FROM devices WHERE id = ?", (dev_id,)).fetchone()
        for c in con.execute("SELECT d.id, d.name, d.address, c.name, d.public_key FROM devices d "
                             "JOIN clients c ON c.id = d.client_id "
                             "WHERE d.address = ? AND d.public_key != ?", (d["address"], d["public_key"])).fetchall():
            lost.append({"device": c[1], "client": c[3], "address": c[2], "pub": c[4]})
            _drop_device(con, int(c[0]))
        same = con.execute("SELECT id FROM devices WHERE public_key = ?", (d["public_key"],)).fetchone()
        if same is not None:
            new_id = int(same[0])
            con.execute(f"UPDATE devices SET {', '.join(c + ' = ?' for c in dcols)}, client_id = ? WHERE id = ?",
                        [d[c] for c in dcols] + [admin, new_id])
        else:
            con.execute(f"INSERT INTO devices ({','.join(dcols)}, client_id) VALUES ({','.join('?' * len(dcols))}, ?)",
                        [d[c] for c in dcols] + [admin])
            new_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        id_map[dev_id] = new_id
        con.execute("DELETE FROM device_traffic WHERE device_id = ?", (new_id,))
        t = cur.execute("SELECT * FROM device_traffic WHERE device_id = ?", (dev_id,)).fetchone()
        if t is not None:
            tc = _cols(cur, "device_traffic")
            con.execute(f"INSERT INTO device_traffic ({','.join(tc)}) VALUES ({','.join('?' * len(tc))})",
                        [new_id if c == "device_id" else t[c] for c in tc])
    for dev_id in cur_devs:
        twin = cur.execute("SELECT twin_of FROM devices WHERE id = ?", (dev_id,)).fetchone()[0]
        con.execute("UPDATE devices SET twin_of = ? WHERE id = ?",
                    (id_map.get(int(twin)) if twin is not None else None, id_map[dev_id]))
    g = cur.execute("SELECT * FROM gateways WHERE id = ?", (slot,)).fetchone()
    gc = _cols(cur, "gateways")
    con.execute(f"INSERT INTO gateways ({','.join(gc)}) VALUES ({','.join('?' * len(gc))})",
                [id_map[int(g['device_id'])] if c == "device_id" else g[c] for c in gc])
    for name in slotkeys.names(merge=slotkeys.CURRENT):
        key = slotkeys.key(name, slot)
        row = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
        _state_put(con, key, row[0] if row is not None else None)


def _forget_slot(con, slot: int) -> None:
    """Слот бэкапа, снятый после него: ни строки, ни ключей; устройство
    остаётся устройством профиля админа."""
    con.execute("DELETE FROM gateways WHERE id = ?", (slot,))
    for name in slotkeys.KEYS:
        _state_put(con, slotkeys.key(name, slot), None)


def merge_other(result: str, copy_db: str, cur_db: str | None, p) -> list[dict]:
    """Основа — база бэкапа; текущие слоты и ключи хоста — из текущей.
    Возвращает устройства бэкапа, которые не встали (конфликт адреса)."""
    lost: list[dict] = []
    snapshot(copy_db, result)
    con = sqlite3.connect(result)
    cur = None
    try:
        if cur_db and p.line != _plan.UNREADABLE:
            cur = sqlite3.connect(cur_db)
            cur.row_factory = sqlite3.Row
        for s in p.slots:
            if s.case == _plan.C_REMOVED:
                _forget_slot(con, s.slot)
        if cur is not None:
            live = [s.slot for s in p.slots if s.case in (_plan.C_SAME, _plan.C_REKEYED, _plan.C_DEVICE,
                                                          _plan.C_TWIN, _plan.C_ADDED)]
            if live:
                admin = _admin_client(con, cur)
                for n in live:
                    _transplant(con, cur, n, admin, lost)
            for key in STATE_ALWAYS_CURRENT:
                row = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
                _state_put(con, key, row[0] if row is not None else None)
            con.execute("DELETE FROM server_state WHERE " +
                        " OR ".join("key LIKE ?" for _ in STATE_ALWAYS_CURRENT_PREFIX),
                        [f"{x}%" for x in STATE_ALWAYS_CURRENT_PREFIX])
            for x in STATE_ALWAYS_CURRENT_PREFIX:
                for k, v in cur.execute("SELECT key, value FROM server_state WHERE key LIKE ?", (f"{x}%",)):
                    _state_put(con, k, v)
            con.commit()
            con.execute("ATTACH DATABASE ? AS cur", (cur_db,))
            _offer_keys(con, live, copy_schema="main", cur_schema="cur", created_at=p.created_at)
            con.commit()
            con.execute("DETACH DATABASE cur")
            rest = [s.slot for s in p.slots if s.case == _plan.C_RESTORED]
        else:
            rest = [s.slot for s in p.slots if s.case != _plan.C_REMOVED]
        _offer_keys(con, rest, copy_schema="main", cur_schema=None, created_at=p.created_at)
        con.commit()
    finally:
        con.close()
        if cur is not None:
            cur.close()
    return lost


def replace_db(copy_dir: str, build) -> str:
    """База распакованного бэкапа подменяется собранной: build(result_path)
    пишет во временный файл рядом; прежняя база бэкапа читается из .copy."""
    state = os.path.join(copy_dir, "state")
    copy_db = os.path.join(state, "bot.db")
    keep = os.path.join(state, ".copy-bot.db")
    os.replace(copy_db, keep)
    tmp = os.path.join(state, ".merged-bot.db")
    try:
        build(tmp, keep)
        os.replace(tmp, copy_db)
    except BaseException:
        os.replace(keep, copy_db)
        for x in (tmp, tmp + "-journal"):
            try:
                os.unlink(x)
            except OSError:
                pass
        raise
    os.unlink(keep)
    return copy_db
