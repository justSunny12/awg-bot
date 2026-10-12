"""versions.py — версии агентов слотов и свежесть связи: другая сторона
основного бота для условия обновления (#main_needs_gw_X, domain/selfupdate)."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.infra import updates


class VersionsMixin:
    def update_peers(self) -> list:
        """[UpdatePeer по слоту]: версия агента — из сессии, без неё — из
        последнего снимка (он живёт до снятия слота, сессия — только пока шлюз
        на связи); «свежий» — сессия есть или связь была за
        UPDATE_PEER_FRESH_DAYS. Слот без устройства свежим не бывает: его
        держать нечем, о нём — предупреждение. Только state, без сети."""
        from awgbot.domain.selfupdate import UpdatePeer, UPDATE_PEER_FRESH_DAYS
        out = []
        now = timeutil.now()
        for gw in self.db.gateways():
            sess = self.gwlink_session(gw.id)
            ver = str(sess.get("agent") or "") if sess else ""
            if updates.parse_version(ver) is None:
                ver = str(self.gwlink_snapshot(gw.id).get("agent_version") or "")
            if updates.parse_version(ver) is None:
                ver = ""
            seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or ""
            fresh = bool(sess)
            if not fresh and seen:
                try:
                    fresh = (now - timeutil.parse_iso(seen)).total_seconds() < UPDATE_PEER_FRESH_DAYS * 86400
                except ValueError:
                    fresh = False
            has_device = self.db.get_device(gw.device_id) is not None
            out.append(UpdatePeer(slot=gw.id, name=self._gw_name(gw), version=ver,
                                  fresh=fresh and has_device, seen=seen))
        return out
