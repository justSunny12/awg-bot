"""slot.py — часть пакета rfpeer: сторона просящего — виртуальный слот «шлюзы второго»."""
from __future__ import annotations

from awgbot.core import config, models, settings
from awgbot.infra.routing import rfpeer as rfp
from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification

RF_PEER_ID = 29                   # 1 << 29 — метка зонда; таблица ROUTING_TABLE + 29 (у гостя запасной — 30)
RF_PEER_IF = rfp.PAIR_IF


class ReserveRfSlotMixin:

    # ── слот ────────────────────────────────────────────────────────────────
    def rt_peer_slot(self):
        """Слот «шлюзы «Beta»» с линком пары — пока пара есть, процесс не гость, свои слоты
        есть, РФ-доступ задействован, вторая сторона даёт шлюзы (последний `st` на диске)
        и линк пары жив (рукопожатие). Иначе None."""
        if config.GUEST or not self.reserve_paired():
            return None
        if not self.db.gateways():
            return None
        try:
            if not self.routing_engaged():
                return None
        except Exception:                                     # noqa: BLE001
            return None
        peer = self.reserve_peer_state()
        if not bool((peer.get("rfp") or {}).get("offer")):
            return None
        if not self._rf_pair_alive():
            return None
        return models.Gateway(id=RF_PEER_ID, device_id=0, link_if=RF_PEER_IF, link_port=0,
                              link_cidr=str(settings.get("app.reserve.pair_cidr", "10.99.98.0/30") or ""),
                              label=f"шлюзы «{self.reserve_peer_name()}»")

    @staticmethod
    def rt_is_peer_slot(gw) -> bool:
        return gw is not None and int(getattr(gw, "id", 0) or 0) == RF_PEER_ID

    def _rf_pair_alive(self) -> bool:
        cache = self.__dict__.setdefault("_rf_pair_alive", {})
        import time
        now = time.monotonic()
        if cache.get("at") and now - cache["at"] < 10:
            return bool(cache.get("ok"))
        try:
            ok = rfp.pair_alive()
        except Exception:                                     # noqa: BLE001
            ok = False
        cache.update(at=now, ok=ok)
        return ok

    # ── такт (после такта живости слотов) ───────────────────────────────────
    def reserve_rf_slot_sync(self) -> list[Notification]:
        """Наличие слота по смене — `AllowedIPs`, файл, файервол; слот пропал при активном —
        сразу на первый свой (Т-РТ8 при мёртвых своих, Т-РТ7 при живом); факт пользования —
        `st.rfp.use`; переходы на слот и обратно — Т-РТ6/Т-РТ7."""
        from awgbot.domain.notices import reserve as rn
        notes: list[Notification] = []
        slot = self.rt_peer_slot()
        cur = self.rsv_get("rsv_rfpeer")
        had = bool(cur.get("slot"))
        has = slot is not None
        name = self.reserve_peer_name()
        if has != had:
            self.rsv_update("rsv_rfpeer", slot=has)
            try:
                if rfp.allowed_ips_wide(has):
                    log.info("транзит: AllowedIPs awgpair — %s", "+ 0.0.0.0/0" if has else "только /32 второй стороны")
            except Exception as e:                            # noqa: BLE001
                log.warning("транзит: AllowedIPs линка пары: %s", e)
            self._rf_file_sync()
            self._rf_firewall_refresh()
            if not has:
                notes += self._rf_slot_gone(cur)
        elif has and self.__dict__.get("_rf_tick", 0) % 10 == 0:
            try:
                rfp.allowed_ips_wide(True)                    # линк переподняли из конфига — вернуть ширину
            except Exception as e:                            # noqa: BLE001
                log.info("транзит: AllowedIPs линка пары: %s", e)
        self.__dict__["_rf_tick"] = int(self.__dict__.get("_rf_tick", 0)) + 1
        # факт пользования: активный слот — виртуальный
        active = self.active_gateway() if has else None
        using = active is not None and int(active.id) == RF_PEER_ID
        use = dict(self.rsv_get("rsv_rfpeer").get("use") or {})
        if using and not use:
            since = timeutil.now_iso()
            self.rsv_update("rsv_rfpeer", use={"since": since, "from": int(self.db.get_state("routing_gw_from") or 0)})
            if hasattr(self, "reserve_st_sent_reset"):
                self.reserve_st_sent_reset()
            log.warning("транзит: РФ-доступ через шлюзы «%s» — свои не отвечают", name)
            until = self._rf_min_interval_until()
            notes.append(Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.rf_transit_on(name, since, until)))
        elif not using and use:
            self.rsv_update("rsv_rfpeer", use=None)
            if hasattr(self, "reserve_st_sent_reset"):
                self.reserve_st_sent_reset()
            own = self.active_gateway()
            if own is not None and not self._rt_unavailable(own.id):
                log.info("транзит: свой слот %s ожил — РФ-доступ обратно", own.id)
                notes.append(Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                          text=rn.rf_transit_back(name, self._gw_display(own) if hasattr(self, "_gw_display") else "")))
        return notes

    def _rf_min_interval_until(self) -> str:
        mins = settings.get_int("app.routing.failover.min_interval_minutes", 10)
        return timeutil.to_iso(timeutil.shift(timeutil.now(), mins * 60))

    def _rf_slot_gone(self, cur: dict) -> list[Notification]:
        """Слот пропал (отзыв согласия, обрыв линка пары, удаление пары): при активном 29 —
        сразу на первый свой; свои мертвы — Т-РТ8 один раз на эпизод."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        raw = self.db.get_state(self._RT_ACTIVE_KEY) or ""
        if raw != str(RF_PEER_ID):
            return []
        own = self.db.gateways()
        if not own:
            return []
        first = next((g for g in own if g.preferred), own[0])
        self._rt_set_active(first)
        self._rt_window_reset()
        log.warning("транзит: слот «%s» пропал при активном — назад на слот %s", name, first.id)
        peer = self.reserve_peer_state()
        why_code = str((peer.get("rfp") or {}).get("why") or "")
        why = {"off": "его админ выключил «Мои шлюзы для резерва»", "down": "шлюз второго сервера не отвечает"}.get(why_code)
        if not why:
            why = "резерв удалён" if not self.reserve_paired() else "нет связи со вторым сервером"
        if any(not self._rt_unavailable(g.id) for g in own):
            return [Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                 text=rn.rf_transit_back(name, self._gw_display(first) if hasattr(self, "_gw_display") else ""))]
        lost = dict(cur.get("lost") or {})
        if lost.get("why") == why and lost.get("told"):
            return []
        self.rsv_update("rsv_rfpeer", lost={"at": timeutil.now_iso(), "why": why, "told": True})
        return [Notification(tg_id=int(config.ADMIN_ID), critical=True, text=rn.rf_transit_lost(name, why))]

    # ── для текстов ─────────────────────────────────────────────────────────
    def reserve_rf_no_backup_why(self) -> str:
        """Почему запасного РФ-доступа через второй сервер нет (Т-РТ2/Т-РТ9); пусто — есть или не о чем."""
        from .offer import WHY_WORDS
        if not self.reserve_paired() or not self.db.gateways():
            return ""
        peer = self.reserve_peer_state()
        rfp_peer = dict(peer.get("rfp") or {})
        if rfp_peer.get("offer"):
            return ""
        return WHY_WORDS.get(str(rfp_peer.get("why") or ""), "второй сервер пока не ответил")
