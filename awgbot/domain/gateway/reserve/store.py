"""
store.py — часть пакета reserve агента (см. __init__): раздел `reserve`
документа конфигурации на шлюзе — файл `/var/lib/awg-gw/reserve.json` (0600:
конфиг линка к резерву, доля ключа переезда, адрес основы, состояние
пересылки от основы с номером переезда), подъём линка `awgrsv` скриптом,
решение «пересылка открыта или закрыта», поле `rsv` снимка для сервера.

Пересылку с `awgrsv` открывает только сам шлюз: по слову основы (`state =
away` в документе) или по `state{serving}` от резерва вместе с фактом
записи DNS на адрес резерва. Закрывает: `state = idle` от основы — только
если его номер переезда не старше последнего `serving` резерва, `state{idle}`
от резерва, раздел пропал. Состояние переживает перезагрузку (файл).
`state{idle, reason}` после отданной этим шлюзом доли — переезд не
состоялся: админу шлюза одно уведомление (Т-РА5), отметка доли снимается.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import timeutil

from ..base import log

STORE_PATH = Path("/var/lib/awg-gw/reserve.json")


def _read(path: Path = STORE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


class ReserveStoreMixin:
    _RSV_STORE = STORE_PATH

    def reserve_section(self) -> dict:
        return _read(self._RSV_STORE)

    def reserve_present(self) -> bool:
        return bool(self.reserve_section().get("conf"))

    def reserve_print(self) -> str:
        """Отпечаток стоящего раздела — в снимок (`cfg.reserve`)."""
        from awgbot.domain import gwcfg
        sec = self.reserve_section()
        if not sec.get("conf"):
            return ""
        return gwcfg.reserve_print({"link": {"conf": sec["conf"]}, "share": sec.get("share") or {},
                                    "state": sec.get("state") or "idle", "move": int(sec.get("move") or 0),
                                    "self": sec.get("self") or "", "chan": int(sec.get("chan") or 0)})

    # ── применение раздела ──────────────────────────────────────────────────
    def reserve_apply(self, checked: dict | None) -> dict:
        """Раздел из документа (проверенный) или None — раздела у сервера нет.
        {"changed": bool, "note": str}."""
        cur = self.reserve_section()
        if checked is None:
            if not cur:
                return {"changed": False, "note": ""}
            self.reserve_remove()
            return {"changed": True, "note": "🛟 Резерв у сервера AWG снят — линк к резерву опущен"}
        new = {"conf": str(checked.get("conf_text") or ""), "share": dict(checked.get("share") or {}),
               "state": str(checked.get("state") or "idle"), "move": int(checked.get("move") or 0),
               "self": str(checked.get("self") or ""), "name": str(checked.get("name") or ""),
               "slot": int(checked.get("slot") or cur.get("slot") or 0),
               "record": str(checked.get("record") or ""), "peer_host": str(checked.get("peer_host") or ""),
               "chan": int(checked.get("chan") or 0), "admins": list(self._reserve_admin_ips()),
               "peer_addr": _peer_addr(str(checked.get("conf_text") or "")),
               "reserve_state": cur.get("reserve_state") or "idle", "reserve_move": int(cur.get("reserve_move") or 0),
               "applied_at": timeutil.now_iso()}
        changed = any(cur.get(k) != new[k] for k in ("conf", "share", "state", "move", "self", "chan"))
        # адреса админов сменились — тоже повод переподнять: набор rsv_admin4 заполняет только --up
        link_changed = cur.get("conf") != new["conf"] or not rsvlink.present() or cur.get("admins") != new["admins"]
        _write(new, self._RSV_STORE)
        note = ""
        if link_changed:
            rsvlink.up(new["conf"], self._reserve_admin_ips())
            note = "🛟 Линк к резерву поднят" if not cur.get("conf") else "🛟 Линк к резерву переподнят на новых ключах"
            log.info("reserve: линк к резерву %s", "поднят" if not cur.get("conf") else "переподнят")
        if cur.get("share", {}).get("gen") != new["share"].get("gen") and new["share"]:
            log.info("reserve: доля ключа переезда поколения %s принята", new["share"].get("gen"))
        self.reserve_forward_decide()
        return {"changed": changed, "note": note}

    def reserve_remove(self) -> None:
        try:
            rsvlink.remove()
        except rsvlink.RsvError as e:
            log.warning("reserve: линк к резерву не снят: %s", e)
        try:
            os.unlink(str(self._RSV_STORE))
        except OSError:
            pass

    def _reserve_admin_ips(self) -> list[str]:
        from awgbot.infra import gwguard
        try:
            raw = gwguard.unit_env("ADMIN_IPS")
        except Exception:                                     # noqa: BLE001
            raw = ""
        return [a.strip() for a in str(raw or "").replace(",", " ").split() if a.strip()]

    # ── состояние резерва и пересылка ───────────────────────────────────────
    def reserve_state_in(self, state: str, move: int, reason: str = "") -> list[tuple]:
        """`state{state, move[, reason]}` от резерва по гостевому каналу. `idle` с
        причиной после отданной этим шлюзом доли — переезд не состоялся: Т-РА5."""
        cur = self.reserve_section()
        if not cur:
            return []
        if state not in ("serving", "idle"):
            return []
        actions: list[tuple] = []
        if state == "idle" and reason and cur.get("share_given_at"):
            from awgbot.core import config
            from awgbot.domain.notices import reserve as rn
            from awgbot.domain.services import Notification
            _primary, since = self.reserve_primary_seen() if hasattr(self, "reserve_primary_seen") else ("", "")
            actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID),
                                                    text=rn.gateway_move_failed(str(cur.get("name") or ""), since or str(cur.get("share_given_at") or ""), reason))]))
            cur["share_given_at"] = ""
        cur["reserve_state"], cur["reserve_move"] = state, int(move)
        cur["reserve_state_at"] = timeutil.now_iso()
        _write(cur, self._RSV_STORE)
        return actions + self.reserve_forward_decide()

    def reserve_forward_decide(self) -> list[tuple]:
        """Открыть или закрыть пересылку с `awgrsv` по правилу раздела; действия
        для runtime: [("open", [подсети])] / [("close",)] / [] — без смены ничего."""
        cur = self.reserve_section()
        if not cur:
            return []
        want_open = False
        primary_away = cur.get("state") == "away"
        reserve_serving = cur.get("reserve_state") == "serving"
        if primary_away:
            want_open = True
        elif reserve_serving and self.reserve_record_fact():
            want_open = True
        if cur.get("state") == "idle" and int(cur.get("move") or 0) >= int(cur.get("reserve_move") or 0) and reserve_serving:
            # основа вернула клиентов и её слово новее: закрыть, даже если резерв ещё говорит serving
            want_open = False
        is_open = rsvlink.is_open()
        if want_open and not is_open:
            srcs = self._reserve_sources()
            cur["open_at"] = timeutil.now_iso()
            _write(cur, self._RSV_STORE)
            log.info("reserve: пересылка с awgrsv открыта для %s", ",".join(srcs))
            return [("open", srcs)]
        if not want_open and is_open:
            log.info("reserve: пересылка с awgrsv закрыта")
            return [("close",)]
        return []

    def _reserve_sources(self) -> list[str]:
        from awgbot.infra import gwguard
        try:
            net = str(gwguard.unit_env("CLIENT_SUBNET") or "").strip()
        except Exception:                                     # noqa: BLE001
            net = ""
        return [net] if net else []

    def reserve_record_fact(self) -> bool:
        """Запись для клиентов основы указывает на адрес резерва — факт у
        авторитетного сервера (кэш в record.py)."""
        from . import record
        cur = self.reserve_section()
        name = str(cur.get("record") or "")
        target = str(cur.get("peer_host") or "")
        if not name or not target:
            return False
        return record.points_to(name, target)

    # ── панель (Т-РА1) ──────────────────────────────────────────────────────
    def reserve_panel_view(self) -> dict:
        """Строка панели и здоровье: имя резерва, связь по awgrsv (ok | suspect |
        down | absent, с какого момента), пересылка, счётчики линка, доля.
        Пусто — раздела нет."""
        cur = self.reserve_section()
        if not cur:
            return {}
        seen, since = self.reserve_seen() if hasattr(self, "reserve_seen") else ("", "")
        c = rsvlink.counters() or (0, 0)
        return {"name": str(cur.get("name") or ""), "seen": seen or "absent", "since": since,
                "open": rsvlink.is_open(), "state": str(cur.get("reserve_state") or "idle"),
                "since_open": str(cur.get("open_at") or ""), "rx": int(c[0]), "tx": int(c[1]),
                "share": bool((cur.get("share") or {}).get("y"))}

    # ── снимок для сервера ──────────────────────────────────────────────────
    def reserve_snapshot(self) -> dict | None:
        """Поле `rsv` снимка: связь с резервом, поколение доли, пересылка,
        состояние резерва, взгляд на основу. None — раздела нет."""
        cur = self.reserve_section()
        if not cur:
            return None
        seen, since = self.reserve_seen() if hasattr(self, "reserve_seen") else ("", "")
        primary, _ = self.reserve_primary_seen() if hasattr(self, "reserve_primary_seen") else ("", "")
        return {"seen": seen or "absent", "since": since, "gen": int((cur.get("share") or {}).get("gen") or 0),
                "open": rsvlink.is_open(), "state": str(cur.get("reserve_state") or "idle"), "primary": primary}


def _peer_addr(conf_text: str) -> str:
    """Адрес стороны зоны в /30 — первый AllowedIPs пира (/32)."""
    import re
    m = re.search(r"(?m)^\s*AllowedIPs\s*=\s*([0-9.]+)", conf_text or "")
    return m.group(1) if m else ""
