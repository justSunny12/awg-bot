"""versions.py — версии агентов слотов и свежесть связи: другая сторона
основного бота для условия обновления (`roles.main.needs_gw` в метаданных
релиза, domain/selfupdate) и кого ждать после старта процесса."""
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
                    fresh = timeutil.elapsed(timeutil.parse_iso(seen), now) < UPDATE_PEER_FRESH_DAYS * 86400
                except ValueError:
                    fresh = False
            has_device = self.db.get_device(gw.device_id) is not None
            out.append(UpdatePeer(slot=gw.id, name=self._gw_name(gw), version=ver,
                                  fresh=fresh and has_device, seen=seen))
        return out

    def update_hold_peers(self) -> list:
        """Кого ждать после старта процесса (окно domain/selfupdate/hold.py):
        слоты с устройством, бывшие на связи за UPDATE_PEER_FRESH_DAYS перед
        остановкой. `gwlink_seen` — правда (пишется и пока сессия жива), а не
        отметка старта: лежавших дольше не ждём."""
        from awgbot.domain.selfupdate import UPDATE_PEER_FRESH_DAYS
        now = timeutil.now()
        out = []
        for gw in self.db.gateways():
            seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or ""
            try:
                recent = bool(seen) and timeutil.elapsed(timeutil.parse_iso(seen), now) \
                    < UPDATE_PEER_FRESH_DAYS * 86400
            except ValueError:
                recent = False
            if recent and self.db.get_device(gw.device_id) is not None:
                out.append(gw.id)
        return out

    def update_hold_left(self, initial: list, start) -> list:
        """Кто из ожидаемых ещё не выходил на связь после начала окна (start):
        gwlink_seen раньше него — имена для экрана. «Выходил» — сессия
        открывалась или жила (время пишут открытие и такт канала), а не «сессия
        есть прямо сейчас»: шлюз, переподключившийся и снова отвалившийся,
        уже сказал свою версию. Снятый слот не ждём."""
        out = []
        for slot in initial:
            gw = self.db.gateway(slot)
            if gw is None:
                continue
            seen = self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot)) or ""
            try:
                fresh = bool(seen) and timeutil.elapsed(start, timeutil.parse_iso(seen)) >= 0
            except ValueError:
                fresh = False
            if not fresh:
                out.append(self._gw_name(gw))
        return out
