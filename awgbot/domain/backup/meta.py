"""
meta.py — часть пакета backup (см. __init__): метка копии схемы 2, опись
шлюзов и сторожа метки.

Метка лежит ВНУТРИ архива (state/backup-meta.json): имя файла переименуют, а
роль, дату, версию и линию базы восстановление читает отсюда. Опись шлюзов —
то, что сервер знает о каждом слоте на момент копии, в том числе о шлюзе не на
связи (seen, keep_at говорят, насколько свежо); по ней план восстановления
сопоставляет копию с текущими слотами.

Чтения схемы 1 нет: копии ниже версии совместимости (core/compat) не
применяются вовсе.
"""
from __future__ import annotations

import json
import os
import socket

from awgbot.core import compat, config
from awgbot.util import timeutil

SCHEMA = 2
META_NAME = "state/backup-meta.json"
ROOT_DIR = "/root"                     # выданные шлюзовые стороны линков: /root/gw-<линк>.conf


def _peer_pub(path: str) -> str:
    """PublicKey первого [Peer] конфига интерфейса; файла нет — пусто."""
    from awgbot.util import wgconf
    try:
        with open(path, encoding="utf-8") as f:
            return wgconf.parse(f.read()).get("Peer", "PublicKey")
    except (OSError, wgconf.WgConfError):
        return ""


def link_pubs(link_if: str, *, awg_dir: str | None = None, root_dir: str | None = None) -> tuple[str, str]:
    """(ключ шлюзовой стороны линка, ключ стороны ВПС): пир в конфиге ВПС —
    шлюз, пир в выданном конфиге шлюза — ВПС."""
    awg_dir = awg_dir or config.AWG_DIR
    root_dir = root_dir or ROOT_DIR
    return (_peer_pub(os.path.join(awg_dir, f"{link_if}.conf")),
            _peer_pub(os.path.join(root_dir, f"gw-{link_if}.conf")))


def _state(db, key: str) -> str:
    return db.get_state(key) or ""


def _state_json(db, key: str) -> dict:
    raw = _state(db, key)
    try:
        data = json.loads(raw) if raw.startswith("{") else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _agent(db, slot: int) -> str:
    """Версия агента: из сессии, без неё — из последнего снимка."""
    from awgbot.infra.updates.model import parse_version
    sess = _state(db, f"gwlink_session_{slot}")
    for part in sess.split():
        if part.startswith("agent="):
            ver = part[len("agent="):]
            if parse_version(ver) is not None:
                return ver
    ver = str(_state_json(db, f"gwlink_snap_{slot}").get("agent_version") or "")
    return ver if parse_version(ver) is not None else ""


def gateways(db, *, awg_dir: str | None = None, root_dir: str | None = None) -> list[dict]:
    """Опись слотов для метки."""
    out = []
    if db is None:
        return out
    for gw in db.gateways():
        dev = db.get_device(gw.device_id)
        twin = next((d for d in db.list_all_devices()
                     if dev is not None and (d.twin_of == dev.id or dev.twin_of == d.id)), None)
        gw_pub, vps_pub = link_pubs(gw.link_if, awg_dir=awg_dir, root_dir=root_dir)
        keep = _state_json(db, f"gwkeep_{gw.id}")
        out.append({"slot": gw.id, "link_if": gw.link_if,
                    "name": dev.name if dev is not None else "", "label": gw.label or "",
                    "device_pub": dev.public_key if dev is not None else "",
                    "twin_pub": twin.public_key if twin is not None else "",
                    "link_gw_pub": gw_pub, "link_vps_pub": vps_pub,
                    "agent": _agent(db, gw.id), "seen": _state(db, f"gwlink_seen_{gw.id}"),
                    "keep_at": str(keep.get("at") or "")})
    return out


def installed_digest(db) -> str:
    """digest установленного релиза («sha256:<hex>»): его сохраняет
    обновление из релиза вместе с версией; установка не из релиза — пусто."""
    if db is None:
        return ""
    ver, _, hexd = _state(db, "update_installed_digest").partition(" ")
    return f"sha256:{hexd}" if ver == config.INSTALLED_VERSION and hexd else ""


def build(db, role: str) -> dict:
    from awgbot.infra import awglock
    return {"schema": SCHEMA, "role": role, "created_at": timeutil.now_iso(),
            "hostname": socket.gethostname(), "version": config.INSTALLED_VERSION,
            "awg_gen": awglock.generation(), "digest": installed_digest(db),
            "db_line": _state(db, "db_line") if db is not None else "",
            "gateways": gateways(db) if role == "main" else []}


def read(plain: bytes) -> dict | None:
    """Метка из распакованного архива; не архив или метки нет — None."""
    import io
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:*") as tar:
            f = tar.extractfile(META_NAME)
            meta = json.loads(f.read().decode()) if f else None
    except Exception:                                  # noqa: BLE001 — не архив, не JSON
        return None
    return meta if isinstance(meta, dict) else None


def env_admin_id(raw_env: bytes | str | None) -> str:
    """ADMIN_ID из env копии; нет — пусто."""
    text = raw_env.decode("utf-8", "replace") if isinstance(raw_env, bytes) else str(raw_env or "")
    for line in text.splitlines():
        if line.startswith("ADMIN_ID="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def check(meta: dict | None, *, admin_id_copy: str = "", chat: bool = True) -> str:
    """Сторожа метки — до остановки сервиса и в чате: «» — годится, иначе
    причина словами (без HTML). admin_id_copy — ADMIN_ID из env копии: из чата
    чужую копию не восстанавливают (chat=False — терминал, можно)."""
    from awgbot.infra import awglock
    if not meta:
        return "не похоже на бэкап сервера AWG"
    if str(meta.get("role") or "") == "gw":
        return "это бэкап шлюза прежних версий — шлюзы теперь восстанавливаются из бэкапа сервера AWG"
    ver = str(meta.get("version") or "")
    if meta.get("schema") != SCHEMA or compat.below_floor(ver):
        if not ver:
            return f"бэкап снят версией до {compat.COMPAT_FLOOR} — восстановиться из него не получится"
        return f"бэкап снят версией {ver} — бэкапы версий до {compat.COMPAT_FLOOR} не применяются"
    # версия бэкапа другая (новее или старше кода) — не отказ: восстановление
    # переходит на выпуск бэкапа (domain/fleet/restore.py)
    gen = meta.get("awg_gen")
    if isinstance(gen, int) and gen and awglock.generation() and gen != awglock.generation():
        return "бэкап снят на другом поколении AmneziaWG — восстановление из него невозможно"
    if chat and admin_id_copy and str(admin_id_copy) != str(config.ADMIN_ID):
        return "бэкап другого админа — из чата его не восстановить; на сервере: sudo awg-bot restore <файл>"
    return ""
