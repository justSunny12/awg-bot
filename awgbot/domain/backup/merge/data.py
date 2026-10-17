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
# ключи server_state, текущие на любой линии: журнал снятых слотов, факты
# бота, кода и хоста (загрузка, старт контейнера — иначе ложное «хост
# перезагружен»)
STATE_ALWAYS_CURRENT = (_plan.REMOVED_KEY, "bot_identity", "restore_wait", "backup_event_due", "updates_muted",
                        "host_boot_id", "container_started_at", "routing_host_boot_id", "routing_host_boot_at")
STATE_ALWAYS_CURRENT_PREFIX = ("update_",)           # GLOB: «_» — буква, а не любой знак
# фраза — та, что человек знает сейчас; на другой линии без текущей — из бэкапа
PHRASE_KEYS = ("backup_passphrase", "backup_key")

# таблицы, которые держат device_id (удаление устройства бэкапа при конфликте адреса)
_DEVICE_TABLES = ("device_traffic", "device_friend", "traffic_samples", "rf_samples", "migration_cohort")


def _cols(con, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def _common(cur, con, table: str, skip=()) -> list[str]:
    """Колонки, которые есть в обеих базах: схемы текущей и бэкапа могут
    отличаться колонкой в пределах линии совместимости."""
    have = set(_cols(con, table))
    return [c for c in _cols(cur, table) if c in have and c not in skip]


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


def _keep_offer(copy_raw, cur_raw, created_at: str = "", live: bool = False):
    """gwkeep из бэкапа с пометкой offer (и датой бэкапа — для вопроса в чате
    шлюза); поле traffic у живого слота — текущее (нет текущего — нет и
    счёта: расход живого шлюза — его)."""
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
    elif cur is not None or live:
        keep.pop("traffic", None)
    return json.dumps(keep, ensure_ascii=False)


def _offer_keys(con, slots, *, copy_schema: str, cur_schema: str | None, created_at: str = "") -> None:
    """Ключи слота с пометкой OFFER — из бэкапа (с offer). В бэкапе ключа нет
    (слот добавлен позже, агент старше) — остаётся текущий."""
    for n in slots:
        for name in slotkeys.names(merge=slotkeys.OFFER):
            key = slotkeys.key(name, n)
            cur_raw = _state_get(con, key, cur_schema) if cur_schema else None
            offered = _keep_offer(_state_get(con, key, copy_schema), cur_raw, created_at, live=bool(cur_schema))
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
        # одна и та же сущность — тот же id и то же время создания (id мог
        # достаться новой строке, если прежнюю удалили и база переносилась)
        both = ("is_service = 0 AND EXISTS (SELECT 1 FROM cp.clients c WHERE c.id = clients.id "
                "AND c.created_at = clients.created_at)")
        for col in CLIENT_FROM_COPY:
            con.execute(f"UPDATE clients SET {col} = (SELECT c.{col} FROM cp.clients c WHERE c.id = clients.id) "
                        f"WHERE {both}")
        # лимиты трафика — текущие вместе с блоками по ним (решение: на той же
        # линии не откатываются; иначе бит TRAFFIC_* разошёлся бы с лимитом)
        skip = ",".join(str(i) for i in slot_devs) or "-1"
        same_dev = ("EXISTS (SELECT 1 FROM cp.devices d WHERE d.id = devices.id "
                    "AND d.created_at = devices.created_at)")
        for col in DEVICE_FROM_COPY:
            con.execute(f"UPDATE devices SET {col} = (SELECT d.{col} FROM cp.devices d WHERE d.id = devices.id) "
                        f"WHERE {same_dev} AND id NOT IN ({skip})")
        matched = ("SELECT c.id FROM clients c JOIN cp.clients k ON k.id = c.id AND k.created_at = c.created_at "
                   "WHERE c.is_service = 0")
        con.execute(f"DELETE FROM client_routing_domains WHERE client_id IN ({matched})")
        con.execute("INSERT OR IGNORE INTO client_routing_domains (client_id, domain, added_at) "
                    f"SELECT client_id, domain, added_at FROM cp.client_routing_domains WHERE client_id IN ({matched})")
        for key in STATE_FROM_COPY:
            _state_put(con, key, _state_get(con, key, "cp"))
        for key in STATE_RESET:
            _state_put(con, key, None)
        _offer_keys(con, p.offer_slots(), copy_schema="cp", cur_schema="main", created_at=p.created_at)
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
    cols = _common(cur, con, "clients", skip=("id",))
    src = cur.execute(f"SELECT id, {','.join(cols)} FROM clients WHERE tg_id = ?", (config.ADMIN_ID,)).fetchone()
    if src is None:
        raise RuntimeError("профиля админа нет ни в бэкапе, ни в текущей базе")
    con.execute(f"INSERT INTO clients ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [src[c] for c in cols])
    cid = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
    for table in ("client_subscription", "client_quota"):
        tc = _common(cur, con, table)
        row = cur.execute(f"SELECT {','.join(tc)} FROM {table} WHERE client_id = ?", (src["id"],)).fetchone()
        if row is not None:
            con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(tc)}) VALUES ({','.join('?' * len(tc))})",
                        [cid if c == "client_id" else row[c] for c in tc])
    return cid


def _drop_device(con, dev_id: int) -> None:
    for table in _DEVICE_TABLES:
        con.execute(f"DELETE FROM {table} WHERE device_id = ?", (dev_id,))
    con.execute("UPDATE devices SET twin_of = NULL WHERE twin_of = ?", (dev_id,))
    con.execute("DELETE FROM devices WHERE id = ?", (dev_id,))


def _transplant(con, cur, slot: int, admin: int, lost: list[dict], dead: set[str]) -> dict[str, str]:
    """Текущий слот — в базу бэкапа: строка слота, устройство и двойник с
    ключами и счётчиками, слотовые ключи CURRENT. Устройство бэкапа, занявшее
    адрес устройства слота, не восстанавливается — оно в итоге; устройство
    слота из бэкапа, на место которого встаёт текущее, — мёртвое (его ключи у
    прежнего шлюза), его пир уходит из конфига (dead). Возвращает
    {ключ перенесённого устройства: интерфейс}."""
    copy_devs = set(_slot_devices(con, slot))
    con.execute("DELETE FROM gateways WHERE id = ?", (slot,))
    dcols = _common(cur, con, "devices", skip=("id", "client_id", "twin_of", "holder_client_id"))
    admin_cur = cur.execute("SELECT id FROM clients WHERE tg_id = ?", (config.ADMIN_ID,)).fetchone()
    id_map: dict[int, int] = {}
    moved: dict[str, str] = {}
    cur_devs = _slot_devices(cur, slot)
    for dev_id in cur_devs:
        d = cur.execute(f"SELECT {','.join(dcols)}, holder_client_id FROM devices WHERE id = ?", (dev_id,)).fetchone()
        holder = admin if admin_cur is not None and d["holder_client_id"] == admin_cur[0] else None
        for c in con.execute("SELECT d.id, d.name, d.address, c.name, d.public_key, c.id, c.tg_id FROM devices d "
                             "JOIN clients c ON c.id = d.client_id "
                             "WHERE d.address = ? AND d.public_key != ?", (d["address"], d["public_key"])).fetchall():
            # id профиля — в итоговой базе (она и станет живой): ссылка в отчёте
            lost.append({"device": c[1], "client": c[3], "address": c[2], "pub": c[4],
                         "client_id": int(c[5]), "client_tg": int(c[6] or 0)})
            _drop_device(con, int(c[0]))
        same = con.execute("SELECT id FROM devices WHERE public_key = ?", (d["public_key"],)).fetchone()
        if same is not None:
            new_id = int(same[0])
            con.execute(f"UPDATE devices SET {', '.join(c + ' = ?' for c in dcols)}, client_id = ?, "
                        "holder_client_id = ? WHERE id = ?", [d[c] for c in dcols] + [admin, holder, new_id])
        else:
            con.execute(f"INSERT INTO devices ({','.join(dcols)}, client_id, holder_client_id) "
                        f"VALUES ({','.join('?' * len(dcols))}, ?, ?)", [d[c] for c in dcols] + [admin, holder])
            new_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0])
        id_map[dev_id] = new_id
        moved[d["public_key"]] = str(d["iface"] or "") if "iface" in d.keys() else ""
        con.execute("DELETE FROM device_traffic WHERE device_id = ?", (new_id,))
        tc = _common(cur, con, "device_traffic")
        t = cur.execute(f"SELECT {','.join(tc)} FROM device_traffic WHERE device_id = ?", (dev_id,)).fetchone()
        if t is not None:
            con.execute(f"INSERT INTO device_traffic ({','.join(tc)}) VALUES ({','.join('?' * len(tc))})",
                        [new_id if c == "device_id" else t[c] for c in tc])
    for old_id in copy_devs - set(id_map.values()):
        row = con.execute("SELECT public_key FROM devices WHERE id = ?", (old_id,)).fetchone()
        if row is not None:
            dead.add(row[0])
            _drop_device(con, old_id)
    for dev_id in cur_devs:
        twin = cur.execute("SELECT twin_of FROM devices WHERE id = ?", (dev_id,)).fetchone()[0]
        con.execute("UPDATE devices SET twin_of = ? WHERE id = ?",
                    (id_map.get(int(twin)) if twin is not None else None, id_map[dev_id]))
    gc = _common(cur, con, "gateways")
    g = cur.execute(f"SELECT {','.join(gc)} FROM gateways WHERE id = ?", (slot,)).fetchone()
    con.execute(f"INSERT INTO gateways ({','.join(gc)}) VALUES ({','.join('?' * len(gc))})",
                [id_map[int(g['device_id'])] if c == "device_id" else g[c] for c in gc])
    for name in slotkeys.names(merge=slotkeys.CURRENT):
        key = slotkeys.key(name, slot)
        row = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
        _state_put(con, key, row[0] if row is not None else None)
    return moved


def _forget_slot(con, slot: int) -> None:
    """Слот бэкапа, снятый после него: ни строки, ни ключей; устройство
    остаётся устройством профиля админа."""
    con.execute("DELETE FROM gateways WHERE id = ?", (slot,))
    for name in slotkeys.KEYS:
        _state_put(con, slotkeys.key(name, slot), None)


def merge_other(result: str, copy_db: str, cur_db: str | None, p, out: dict | None = None) -> list[dict]:
    """Основа — база бэкапа; текущие слоты и ключи хоста — из текущей.
    Возвращает устройства бэкапа, которые не встали (конфликт адреса); в out —
    перенесённые устройства слотов с интерфейсами (moved) и мёртвые пиры (dead)."""
    lost: list[dict] = []
    out = out if out is not None else {}
    out.setdefault("moved", {})
    out.setdefault("dead", set())
    snapshot(copy_db, result)
    con = sqlite3.connect(result)
    cur = None
    try:
        if cur_db and p.line != _plan.UNREADABLE:
            cur = sqlite3.connect(cur_db)
            cur.row_factory = sqlite3.Row
        for n in p.removed_slots():
            _forget_slot(con, n)
        if cur is not None:
            live = p.current_slots()
            if live:
                admin = _admin_client(con, cur)
                for n in live:
                    out["moved"].update(_transplant(con, cur, n, admin, lost, out["dead"]))
            for key in STATE_ALWAYS_CURRENT:
                row = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
                _state_put(con, key, row[0] if row is not None else None)
            if cur.execute("SELECT value FROM server_state WHERE key = 'backup_passphrase' AND value != ''").fetchone():
                for key in PHRASE_KEYS:                   # фраза задана сейчас — она и остаётся
                    row = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
                    _state_put(con, key, row[0] if row is not None else None)
            for x in STATE_ALWAYS_CURRENT_PREFIX:
                con.execute("DELETE FROM server_state WHERE key GLOB ?", (f"{x}*",))
                for k, v in cur.execute("SELECT key, value FROM server_state WHERE key GLOB ?", (f"{x}*",)):
                    _state_put(con, k, v)
            con.commit()
            con.execute("ATTACH DATABASE ? AS cur", (cur_db,))
            _offer_keys(con, [n for n in p.offer_slots()], copy_schema="main", cur_schema="cur",
                        created_at=p.created_at)
            con.commit()
            con.execute("DETACH DATABASE cur")
            rest = [s.slot for s in p.slots if s.case == _plan.C_RESTORED]
        else:
            rest = [n for n in sorted({s.slot for s in p.slots}) if n not in p.removed_slots()]
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
