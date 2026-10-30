"""slot.py — часть пакета rfpeer: сторона просящего — виртуальный слот «шлюзы второго»."""
from __future__ import annotations

import time

from awgbot.core import config, models, settings
from awgbot.infra import routing
from awgbot.infra.routing import rfpeer as rfp
from awgbot.infra.routing import slots as slotpolicy
from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification

RF_PEER_ID = 29                   # 1 << 29 — метка зонда; таблица ROUTING_TABLE + 29 (у гостя запасной — 30)
RF_PEER_IF = rfp.PAIR_IF


def lost_why(code: str, peer: str) -> str:
    """Причина Т-РТ8 словами: коды отказа второй стороны (`why_words`: тумблер, нет шлюзов,
    РФ-доступ не задействован, сам ходит через мои) и свои — обрыв линка пары, удаление пары."""
    from .offer import why_words
    words = {**why_words(peer), "down": f"шлюз «{peer}» не отвечает",
             "link": f"нет связи с «{peer}»", "unpaired": "резерв удалён"}
    return words.get(code, f"нет связи с «{peer}»")


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
        cache = self.__dict__.setdefault("_rf_pair_alive_cache", {})
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
        """Наличие слота по смене — `AllowedIPs`, файл, файервол, политика слота; слот пропал при
        активном — сразу на первый свой (Т-РТ8 при мёртвых своих, Т-РТ7 при живом); факт
        пользования — `st.rfp.use`; переходы на слот и обратно — Т-РТ6/Т-РТ7."""
        from awgbot.domain.notices import reserve as rn
        notes: list[Notification] = []
        slot = self.rt_peer_slot()                              # рукопожатие — из кэша такта живости (10 с)
        cur = self.rsv_get("rsv_rfpeer")
        had = bool(cur.get("slot"))
        has = slot is not None
        name = self.reserve_peer_name()
        tick = int(self.__dict__.get("_rf_slot_tick", 0))
        self.__dict__["_rf_slot_tick"] = tick + 1
        # линк пары пересоздан (--adopt, рестарт юнита: маршруты таблиц пропали с устройством) или переподнят
        # из конфига (--reassert, смена ключей: AllowedIPs снова узкие) — штамп устройства и конфига
        stamp = rfp.link_stamp()
        recreated = has and had and self.__dict__.get("_rf_pair_stamp") not in (None, stamp)
        self.__dict__["_rf_pair_stamp"] = stamp
        if has != had:
            if not has:
                self._rf_allowed_narrow()                       # сначала узкие AllowedIPs, потом файервол без входа «до established»
            self.rsv_update("rsv_rfpeer", slot=has, peer4=str(rfp.pair_conf().get("peer4") or cur.get("peer4") or ""))
            self._rf_file_sync()
            self._rf_firewall_refresh()
            if not has:
                notes += self._rf_slot_gone(cur)
                try:
                    slotpolicy.drop_slot_policy(RF_PEER_ID, RF_PEER_IF)
                except Exception as e:                        # noqa: BLE001
                    log.info("транзит: политика слота %s: %s", RF_PEER_ID, e)
            else:
                self._rf_slot_policy()                          # правило по метке и таблица — до зонда слота
        elif recreated:
            log.info("транзит: линк пары пересоздан — политика слота «%s» и маршрут заново", name)
            self._rf_slot_policy()
            if (self.db.get_state(self._RT_ACTIVE_KEY) or "") == str(RF_PEER_ID):
                try:
                    routing.switch_active(RF_PEER_IF)           # маршрут таблицы РФ-доступа — снова в awgpair
                except routing.RoutingError as e:
                    log.warning("транзит: маршрут в линк пары не восстановлен: %s", e)
        if has and (has != had or recreated or tick % 10 == 0):
            # широкие AllowedIPs — по смене, после пересоздания линка и страховочно раз в 10 тактов
            try:
                if rfp.allowed_ips_wide(True):
                    log.info("транзит: AllowedIPs awgpair — %s/32, 0.0.0.0/0", str(rfp.pair_conf().get("peer4") or ""))
            except Exception as e:                            # noqa: BLE001
                log.warning("транзит: AllowedIPs линка пары: %s", e)
        elif not has and tick % 10 == 0:
            self._rf_allowed_narrow(quiet=True)                 # страховка: сужение могло не пройти при пропаже слота
        # факт пользования: активный слот — виртуальный
        active = self.active_gateway() if has else None
        using = active is not None and int(active.id) == RF_PEER_ID
        use = dict(self.rsv_get("rsv_rfpeer").get("use") or {})
        if using and not use:
            since = timeutil.now_iso()
            self.rsv_update("rsv_rfpeer", use={"since": since}, lost=None)   # новый эпизод: Т-РТ8 прежнего не в счёт
            if hasattr(self, "reserve_st_sent_reset"):
                self.reserve_st_sent_reset()
            log.warning("транзит: РФ-доступ через шлюзы «%s» — свои не отвечают", name)
            notes.append(Notification(tg_id=int(config.ADMIN_ID), silent=True,
                                      text=rn.rf_transit_on(name, since, self._rf_min_interval_until())))
        elif not using and use:
            self.rsv_update("rsv_rfpeer", use=None)
            if hasattr(self, "reserve_st_sent_reset"):
                self.reserve_st_sent_reset()
            own = self.active_gateway()
            if own is not None and not self.rt_is_peer_slot(own) and self._rf_own_alive(own):
                log.info("транзит: свой слот %s ожил — РФ-доступ обратно", own.id)
                notes.append(Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.rf_transit_back(name, *self._rf_gw_title(own))))
        return notes

    def _rf_allowed_narrow(self, quiet: bool = False) -> None:
        try:
            if rfp.allowed_ips_wide(False) and not quiet:
                log.info("транзит: AllowedIPs awgpair — только /32 второй стороны")
        except Exception as e:                                # noqa: BLE001
            log.warning("транзит: AllowedIPs линка пары: %s", e)

    def _rf_slot_policy(self) -> None:
        """Слот 29 появился: правило по метке и таблица слота — сразу, а не 10-м тактом живости: без
        правила зонд с меткой ушёл бы основной таблицей через WAN и «прошёл»; кэш вердикта слота — прочь."""
        try:
            self._ensure_gateway_policy()
        except Exception as e:                                # noqa: BLE001
            log.warning("транзит: политика слота %s: %s", RF_PEER_ID, e)
        for key in ("_rt_spare_last", "_rt_spare_next"):
            cache = self.__dict__.get(key)
            if isinstance(cache, dict):
                cache.clear()

    def _rf_own_alive(self, g) -> bool:
        """Свой слот жив: три хороших подряд, либо не падал и не недоступен. Окно после
        переключения свежее и само по себе о мёртвом слоте не говорит — смотрим на отметку падения."""
        up = int(self.db.get_state(f"routing_gw_{g.id}_up_streak") or 0)
        if up >= self._RT_UP_STREAK:
            return True
        down_since = self.db.get_state(f"routing_gw_{g.id}_down_since") or ""
        return not down_since and not self._rt_unavailable(g.id)

    def _rf_min_interval_until(self) -> str:
        mins = settings.get_int("app.routing.failover.min_interval_minutes", 10)
        return timeutil.to_iso(timeutil.shift(timeutil.now(), mins * 60))

    def _rf_slot_gone(self, cur: dict, why_code: str = "") -> list[Notification]:
        """Слот пропал (отзыв согласия, обрыв линка пары, удаление пары): при активном 29 —
        сразу на первый свой; свои мертвы — Т-РТ8 один раз на причину, живой — Т-РТ7."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        raw = self.db.get_state(self._RT_ACTIVE_KEY) or ""
        if raw != str(RF_PEER_ID):
            return []
        own = self.db.gateways()
        if not own:
            return []
        alive = [g for g in own if self._rf_own_alive(g)]     # до любых сбросов окон
        own_dead = not alive
        pool = alive or own                                   # первый живой свой (предпочтительный из живых); все мертвы — предпочтительный
        first = next((g for g in pool if g.preferred), pool[0])
        self.db.set_state(self._RT_ACTIVE_KEY, str(first.id))
        try:
            routing.switch_active(first.link_if)
        except routing.RoutingError as e:
            log.warning("транзит: трафик на слот %s не переведён: %s", first.id, e)
        self._rt_window_reset(RF_PEER_ID)
        if not own_dead:
            self.db.set_state(self._rt_keys(first.id)[1], "0")   # как путь автомата: «снова в строю» своего не будет
        self.rsv_update("rsv_rfpeer", use=None)               # эпизод кончился здесь: такт не объявит второй раз
        if hasattr(self, "reserve_st_sent_reset"):
            self.reserve_st_sent_reset()
        log.warning("транзит: слот «%s» пропал при активном — назад на слот %s", name, first.id)
        try:
            engaged = bool(self.routing_engaged())
        except Exception:                                     # noqa: BLE001
            engaged = True
        if not engaged:
            return []                                         # РФ-доступ выключил сам админ — не о чем сообщать
        if not why_code:
            peer = self.reserve_peer_state()
            code = str((peer.get("rfp") or {}).get("why") or "")
            why_code = code if code in ("off", "down", "no_gw", "rt_off", "peer") else \
                ("unpaired" if not self.reserve_paired() else "link")
        if not own_dead:
            return [Notification(tg_id=int(config.ADMIN_ID), silent=True, text=rn.rf_transit_back(name, *self._rf_gw_title(first)))]
        why = lost_why(why_code, name)
        lost = dict(cur.get("lost") or {})
        if lost.get("why") == why and lost.get("told"):
            return []
        self.rsv_update("rsv_rfpeer", lost={"at": timeutil.now_iso(), "why": why, "told": True})
        return [Notification(tg_id=int(config.ADMIN_ID), critical=True, text=rn.rf_transit_lost(name, why))]

    # ── для текстов ─────────────────────────────────────────────────────────
    def _rf_gw_title(self, g) -> tuple[str, str]:
        """(имя, подпись) своего шлюза для Т-РТ7 — «Имя» (подпись), как в карточке и диалогах."""
        name = self._gw_name(g) if hasattr(self, "_gw_name") else ""
        label = str(getattr(g, "label", "") or "") if getattr(g, "device_id", 0) else ""
        return name, label

    def reserve_rf_no_backup_code(self) -> str:
        """Код причины, почему запасного РФ-доступа через второй сервер нет (off, no_gw, rt_off, down,
        peer; `wait` — второй сервер пока не ответил); пусто — есть или не о чем."""
        if not self.reserve_paired() or not self.db.gateways():
            return ""
        rfp_peer = dict(self.reserve_peer_state().get("rfp") or {})
        if rfp_peer.get("offer"):
            return ""
        return str(rfp_peer.get("why") or "") or "wait"

    def reserve_rf_no_backup_why(self) -> str:
        """Та же причина словами (Т-РТ2/Т-РТ9); пусто — есть или не о чем."""
        from .offer import why_words
        code = self.reserve_rf_no_backup_code()
        if not code:
            return ""
        return why_words(self.reserve_peer_name()).get(code, "второй сервер пока не ответил")
