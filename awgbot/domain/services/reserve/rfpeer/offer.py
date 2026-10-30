"""offer.py — часть пакета rfpeer: сторона транзитного сервера."""
from __future__ import annotations

from awgbot.core import config, settings
from awgbot.infra.routing import rfpeer as rfp
from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification

WHY_WORDS = {"off": "его админ выключил «Мои шлюзы для резерва»", "no_gw": "у него нет шлюзов",
             "rt_off": "у него не задействован РФ-доступ", "down": "его шлюз сейчас не отвечает",
             "peer": "он сам ходит в РФ через шлюзы этого сервера"}


class ReserveRfOfferMixin:

    # ── правило согласия ────────────────────────────────────────────────────
    def reserve_rf_offer(self) -> tuple[bool, str]:
        """(даю ли свои шлюзы второй стороне, причина отказа кодом): тумблер, свои слоты,
        РФ-доступ задействован и проходит, активный слот — свой (не виртуальный) и доступен.
        То же правило решает запасной слот гостя в зоне (rfzone)."""
        if not settings.get_bool("app.reserve.rf_via_mine", True):
            return False, "off"
        if not self.db.gateways():
            return False, "no_gw"
        try:
            if not self.routing_engaged():
                return False, "rt_off"
            active = self.active_gateway()
            if active is None or not self.routing_link_ok() or self._rt_unavailable(active.id):
                return False, "down"
            if hasattr(self, "rt_is_spare") and self.rt_is_spare(active):
                return False, "peer"
        except Exception as e:                                # noqa: BLE001
            log.info("РФ-доступ через второй сервер: согласие не посчиталось: %s", e)
            return False, "down"
        return True, ""

    def reserve_rfp_st_part(self) -> dict:
        """Поле `rfp` в `st`: согласие и причина, факт пользования чужими шлюзами."""
        if not self.reserve_paired():
            return {}
        cur = self.rsv_get("rsv_rfpeer")
        offer = dict(cur.get("offer") or {})
        use = dict(cur.get("use") or {})
        return {"offer": bool(offer.get("on")), "why": str(offer.get("why") or ""), "use": str(use.get("since") or "")}

    # ── такт (после такта живости слотов) ───────────────────────────────────
    def reserve_rfpeer_tick(self) -> list[Notification]:
        """Согласие по смене (файл, файервол, правило, `st`), эпизод второй стороны
        (Т-РТ10/Т-РТ11), слот просящего (rfpeer/slot)."""
        if not self.reserve_paired():
            return []
        notes: list[Notification] = []
        try:
            notes += self._rf_offer_sync()
        except Exception as e:                                # noqa: BLE001
            log.warning("РФ-доступ через второй сервер: согласие: %s", e)
        try:
            notes += self._rf_peer_use_sync()
        except Exception as e:                                # noqa: BLE001
            log.warning("РФ-доступ через второй сервер: эпизод второй стороны: %s", e)
        if hasattr(self, "reserve_rf_slot_sync"):
            try:
                notes += self.reserve_rf_slot_sync()
            except Exception as e:                            # noqa: BLE001
                log.warning("РФ-доступ через второй сервер: слот: %s", e)
        return notes

    def _rf_offer_sync(self) -> list[Notification]:
        on, why = self.reserve_rf_offer()
        cur = self.rsv_get("rsv_rfpeer")
        prev = dict(cur.get("offer") or {})
        if prev.get("on") == on and str(prev.get("why") or "") == why and prev.get("since"):
            return []
        self.rsv_update("rsv_rfpeer", offer={"on": bool(on), "why": why, "since": timeutil.now_iso()})
        self._rf_file_sync()
        conf = rfp.pair_conf()
        peer4 = str(conf.get("peer4") or "")
        if peer4:
            try:
                rfp.ensure_rule(peer4) if on else rfp.drop_rule(peer4)
            except Exception as e:                            # noqa: BLE001
                log.warning("РФ-доступ через второй сервер: правило маршрутизации: %s", e)
        self._rf_firewall_refresh()
        if hasattr(self, "reserve_st_sent_reset"):
            self.reserve_st_sent_reset()                      # st второй стороне — по смене согласия
        if on:
            log.info("транзит: даю шлюзы «%s»", self.reserve_peer_name())
        else:
            log.info("транзит: не даю шлюзы «%s»: %s", self.reserve_peer_name(), why)
        return []

    def _rf_file_sync(self) -> None:
        cur = self.rsv_get("rsv_rfpeer")
        offer = bool((cur.get("offer") or {}).get("on"))
        slot = bool(cur.get("slot"))
        if not offer and not slot:
            rfp.remove()
            return
        rfp.write(offer, slot, timeutil.now_iso())

    def _rf_firewall_refresh(self) -> None:
        if hasattr(self, "_gw_firewall_refresh"):
            try:
                self._gw_firewall_refresh()
            except Exception as e:                            # noqa: BLE001
                log.warning("РФ-доступ через второй сервер: файервол не перерисован: %s", e)

    def _rf_peer_use_sync(self) -> list[Notification]:
        """Вторая сторона ходит через мои шлюзы (`st.rfp.use`): начало — Т-РТ10 один раз на
        эпизод, конец — Т-РТ11 с объёмом за эпизод."""
        from awgbot.domain.notices import reserve as rn
        peer = self.reserve_peer_state()
        rfp_peer = dict(peer.get("rfp") or {})
        use_since = str(rfp_peer.get("use") or "")
        cur = self.rsv_get("rsv_rfpeer")
        ep = dict(cur.get("peer_use") or {})
        name = self.reserve_peer_name()
        if use_since and not ep:
            base = self.reserve_rf_peer_counters()
            self.rsv_update("rsv_rfpeer", peer_use={"since": use_since, "told": True, "base": base})
            log.warning("транзит: «%s» ходит через мои шлюзы с %s", name, use_since)
            return [Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.rf_peer_use(name, use_since))]
        if not use_since and ep:
            up, dn = self._rf_peer_episode_bytes(ep)
            self.rsv_update("rsv_rfpeer", peer_use=None)
            log.info("транзит: «%s» снова ходит своими шлюзами (через мои прошло ↑%d ↓%d)", name, up, dn)
            return [Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.rf_peer_done(name, up, dn))]
        return []

    def reserve_rf_peer_counters(self) -> dict:
        """Показания счётчиков транзита (байт ↑ в линки слотов, ↓ обратно); нет таблицы — нули."""
        try:
            from awgbot.infra import rfacct
            st = rfacct.read()
            return {"up": int(st.counters.get(rfacct.COUNTER_RFP_UP) or 0), "dn": int(st.counters.get(rfacct.COUNTER_RFP_DN) or 0)}
        except Exception:                                     # noqa: BLE001
            return {"up": 0, "dn": 0}

    def _rf_peer_episode_bytes(self, ep: dict) -> tuple[int, int]:
        now = self.reserve_rf_peer_counters()
        base = dict(ep.get("base") or {})
        up = int(now.get("up") or 0) - int(base.get("up") or 0)
        dn = int(now.get("dn") or 0) - int(base.get("dn") or 0)
        return (max(0, up), max(0, dn))                       # счётчики с нуля после перезагрузки — не отрицательно

    def reserve_rf_peer_episode(self) -> dict:
        """Эпизод второй стороны для карточки: {since, up, dn}; пусто — нет."""
        ep = dict(self.rsv_get("rsv_rfpeer").get("peer_use") or {})
        if not ep:
            return {}
        up, dn = self._rf_peer_episode_bytes(ep)
        return {"since": str(ep.get("since") or ""), "up": up, "dn": dn}

    # ── удаление пары ───────────────────────────────────────────────────────
    def reserve_rfpeer_drop(self) -> None:
        """Пара снята: правило, файл согласия, широкие `AllowedIPs` — прочь."""
        conf = rfp.pair_conf()
        if conf.get("peer4"):
            try:
                rfp.drop_rule(str(conf["peer4"]))
            except Exception as e:                            # noqa: BLE001
                log.info("транзит: правило при удалении пары: %s", e)
        try:
            rfp.allowed_ips_wide(False)
        except Exception as e:                                # noqa: BLE001
            log.info("транзит: AllowedIPs при удалении пары: %s", e)
        rfp.remove()
        self.rsv_put("rsv_rfpeer", None)
