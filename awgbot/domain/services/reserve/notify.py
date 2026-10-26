"""
notify.py — часть пакета reserve (см. __init__): уведомления пользователям о
переезде (Т-РК1, шлёт бот основы на резерве) и о возврате (Т-РК2, шлёт бот
основы на основе). Только тем, кто на связи (Р21): последнее рукопожатие не
старше `notify_online_minutes` к моменту опроса; минус заблокированные и
админ; при переезде — минус уже переехавшие (рукопожатие на клоне после
подъёма); при возврате — только идущие через резерв (эндпоинт — адрес
резерва). Один раз на переезд и на возврат. Звук — как у остальных
уведомлений пользователям: со звуком вне тихих часов (Р34).
"""
from __future__ import annotations

from awgbot.core import config, settings
from awgbot.util import timeutil

from ..types import Notification
from ..gateway_link.common import log

MOVE_NOTIFIED_KEY = "move_notified"          # server_state гостя (едет в реплике — GUEST_STATE)
RETURN_NOTIFY_DELAY_S = 600


class ReserveNotifyMixin:
    def _online_window(self) -> int:
        return settings.get_int("app.reserve.notify_online_minutes", 15) * 60

    def _recipients(self, device_ids: list[int]) -> list[int]:
        """tg_id владельцев и держателей устройств, без админа; без повторов."""
        if not device_ids:
            return []
        ids: set[int] = set()
        clients = {c.id: c for c in self.db.list_clients(include_service=True)}
        for d in self.db.list_all_devices():
            if d.id not in device_ids:
                continue
            for cid in (d.client_id, getattr(d, "holder_client_id", None)):
                c = clients.get(cid) if cid else None
                if c is not None and c.tg_id and int(c.tg_id) != int(config.ADMIN_ID) and not getattr(c, "is_service", 0):
                    ids.add(int(c.tg_id))
        return sorted(ids)

    # ── переезд (гость) ─────────────────────────────────────────────────────
    def guest_move_notify(self, rf: bool = False) -> list[Notification]:
        """Т-РК1 один раз: по `last_handshake` реплики, минус переехавшие."""
        from awgbot.domain.notices import reserve as rn
        from awgbot.infra import awg, rsvzone
        if self.db.get_state(MOVE_NOTIFIED_KEY):
            return []
        try:
            polled = int(self.db.get_state("online_polled_at") or 0)
        except ValueError:
            polled = 0
        if not polled:
            polled = int(timeutil.now().timestamp())
        window = self._online_window()
        taken_at = 0
        try:
            st = rsvzone.state()
            taken_at = int(timeutil.parse_iso(str(st.get("taken_at") or "")).timestamp()) if st.get("taken_at") else 0
        except ValueError:
            taken_at = 0
        moved: set[str] = set()
        try:
            for p in awg.show_dump(config.AWG_INTERFACE):
                if p.get("last_handshake") and taken_at and int(p["last_handshake"]) >= taken_at:
                    moved.add(str(p["public_key"]))
        except Exception as e:                                # noqa: BLE001
            log.info("гость: дамп клона не прочитан — считаю, что никто ещё не переехал: %s", e)
        targets = []
        for d in self.db.list_all_devices():
            hs = getattr(d.traffic, "last_handshake", None) if getattr(d, "traffic", None) else None
            if not hs or polled - int(hs) > window or getattr(d, "block_reason", 0) or getattr(d, "is_gateway", 0):
                continue
            if d.public_key in moved:
                continue
            targets.append(d.id)
        self.db.set_state(MOVE_NOTIFIED_KEY, timeutil.now_iso())
        text = rn.client_moved(rf=rf)
        notes = [Notification(tg_id=tg, text=text) for tg in self._recipients(targets)]
        log.info("гость: «переподключи VPN» — %d адресатов из %d устройств на связи", len(notes), len(targets))
        return notes

    # ── возврат (основа) ────────────────────────────────────────────────────
    def reserve_return_notify_tick(self) -> list[Notification]:
        """Т-РК2 через 10 мин после факта записи: устройства, идущие через резерв."""
        from awgbot.domain.notices import reserve as rn
        from awgbot.infra import awg
        out = self.rsv_get("rsv_out")
        if str(out.get("state") or "") != "home" or out.get("notified") is not False:
            return []
        fact = str(out.get("back_fact_at") or "")
        if not fact:
            self.rsv_update("rsv_out", notified=True)
            return []
        try:
            if timeutil.elapsed(timeutil.parse_iso(fact)) < RETURN_NOTIFY_DELAY_S:
                return []
        except ValueError:
            self.rsv_update("rsv_out", notified=True)
            return []
        self.rsv_update("rsv_out", notified=True)
        peer_ip = self.reserve_peer_ip()
        now = int(timeutil.now().timestamp())
        window = self._online_window()
        targets = []
        try:
            dump = awg.show_dump(config.AWG_INTERFACE)
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: дамп для Т-РК2 не прочитан: %s", e)
            return []
        by_pub = {d.public_key: d for d in self.db.list_all_devices()}
        for p in dump:
            ep = str(p.get("endpoint") or "")
            hs = p.get("last_handshake")
            if not ep or not hs or now - int(hs) > window:
                continue
            if peer_ip and ep.rsplit(":", 1)[0].strip("[]") != peer_ip:
                continue
            d = by_pub.get(p.get("public_key"))
            if d is not None and not getattr(d, "block_reason", 0) and not getattr(d, "is_gateway", 0):
                targets.append(d.id)
        text = rn.client_returned(str(out.get("relay_until") or ""))
        notes = [Notification(tg_id=tg, text=text) for tg in self._recipients(targets)]
        log.info("резерв: «сервер вернулся» — %d адресатов", len(notes))
        return notes
