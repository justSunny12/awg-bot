"""card.py — строка канала в карточке слота, расхождение часов."""
from __future__ import annotations


from awgbot.util import timeutil



class CardMixin:
    # ── карточка слота ───────────────────────────────────────────────────────

    def gwlink_card(self, gw, handshake_age) -> dict:
        """Что карточка слота знает о канале: связь, код на той стороне, сверка
        конфигурации, взгляд шлюза на выход наружу. Без единого exec и без
        запросов к шлюзу — только то, что уже лежит в state.

        «На связи» — конъюнкция: сессия открыта И хендшейк линка свежий.
        Heartbeat'а в канале нет намеренно, поэтому полуоткрытая сессия (у
        шлюзу выдернули питание, RST не дошёл) перестаёт врать не позже, чем
        протухнет хендшейк.
        """
        from awgbot.infra import awglock
        from awgbot.util import gwlink
        sess = self.gwlink_session(gw.id)
        fresh = handshake_age is not None and handshake_age <= 180
        snap = self.gwlink_snapshot(gw.id)
        seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or ""
        try:
            mine_gen = awglock.generation()
        except Exception:                                 # noqa: BLE001
            mine_gen = None
        drift, drift_keys = self.gwlink_config_drift(gw, with_keys=True) if snap else ([], [])
        return {
            "online": bool(sess) and fresh,
            "ever": bool(seen or snap),
            "seen": seen,
            "age": self.gwlink_snapshot_age(gw.id),
            "agent": snap.get("agent_version", ""),
            "awg_gen": snap.get("awg_generation"),
            "awg_gen_mine": mine_gen,
            "drift": drift,
            "drift_items": self.gwlink_config_drift_items(gw) if snap else [],
            # что из расхождения довезёт канал, а что — только файл (подсети
            # соседей живут и в конфиге линка): экран не должен обещать
            # доставку, которой не будет
            "drift_bundle": any(k in gwlink.BUNDLE_ONLY_KEYS for k in drift_keys),
            "drift_channel": any(k in gwlink.SETTINGS_KEYS for k in drift_keys),
            "has_snap": bool(snap),
            "egress_gw": snap.get("egress_ok") if snap else None,
            "plumbing_gen": snap.get("plumbing_gen", "") if snap else "",
            # Снимок есть, а блока установленной конфигурации нет (старый или
            # урезанный агент): пустая сверка тогда значит «сказать нечего», а
            # не «совпадает» — экран обязан это различать.
            "has_bundle": isinstance(snap.get("bundle"), dict) if snap else False,
            "clock_skew": self._gwlink_clock_skew(gw.id),
            "ack": self.gwlink_ack(gw.id),
            "lists": self.gwlink_lists(gw.id) if gw.lan_mode else {},
            "error": (self.db.get_state(self._gwlink_key(self._GWLINK_ERROR_KEY, gw.id)) or ""
                      ).partition(" ")[2],
            "peer_nets": snap.get("peer_nets") if snap else None,
        }

    def _gwlink_clock_skew(self, slot_id: int) -> int | None:
        """Насколько часы шлюза расходятся с часами ВПС, секунд: `ts` снимка по
        часам шлюза против времени приёма по нашим. Доставка внутри линка —
        доли секунды, так что разница и есть расхождение часов. None — нечем
        мерить. Канал от часов не зависит (повтор отсекают нонсы сессии), но
        часы нужны TLS, расписаниям и срокам — строка на экране говорит о них
        раньше, чем сломается что-то ещё."""
        ts = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_TS_KEY, slot_id)) or ""
        at = self.db.get_state(self._gwlink_key(self._GWLINK_SNAP_AT_KEY, slot_id)) or ""
        if not ts or not at:
            return None
        try:
            return int((timeutil.parse_iso(ts) - timeutil.parse_iso(at)).total_seconds())
        except ValueError:
            return None
