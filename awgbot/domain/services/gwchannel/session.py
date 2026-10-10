"""session.py — сессия: открыта/закрыта, последний раз на связи, причина обрыва."""
from __future__ import annotations


from awgbot.util import timeutil

from .common import log


class SessionMixin:
    _GWLINK_SESSION_KEY = "gwlink_session"
    _GWLINK_SEEN_KEY = "gwlink_seen"
    _GWLINK_SNAP_KEY = "gwlink_snap"
    _GWLINK_SNAP_REV_KEY = "gwlink_snap_rev"
    _GWLINK_SNAP_AT_KEY = "gwlink_snap_at"
    _GWLINK_SNAP_TS_KEY = "gwlink_snap_ts"

    def _gwlink_key(self, key: str, slot_id: int) -> str:
        return f"{key}_{int(slot_id)}"

    # ── сессия ───────────────────────────────────────────────────────────────

    def gwlink_session_opened(self, slot_id: int, agent: str, proto: int, caps=()) -> None:
        agent = " ".join(str(agent or "").split())[:32]     # одна строка: state и журнал по строкам
        caps_s = ",".join(c for c in caps if c and "," not in c and " " not in c)
        now = timeutil.to_iso(timeutil.now())
        self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id),
                          f"since={now} agent={agent} proto={int(proto)} caps={caps_s}")
        self.db.set_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id), now)
        self.db.set_state(self._gwlink_key(self._GWLINK_ERROR_KEY, slot_id), "")
        log.info("канал линка: слот %s на связи (агент %s, proto %s)", slot_id, agent, proto)

    def gwlink_session_closed(self, slot_id: int) -> None:
        self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id), "")
        self.db.set_state(self._gwlink_key(self._GWLINK_SEEN_KEY, slot_id),
                          timeutil.to_iso(timeutil.now()))
        # Нумерация снимка живёт в сессии: следующая начнётся с полного.
        self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_REV_KEY, slot_id), "")

    _GWLINK_ERROR_KEY = "gwlink_error"

    def gwlink_sessions_reset(self) -> None:
        """Старт процесса: ни одной живой сессии быть не может — сокеты умерли
        вместе с прежним процессом. Без сброса строка сессии пережила бы
        рестарт, и карточка зажигала бы «на связи» у шлюза, который ещё не
        переподключился, — а при выключенном канале или лежащем линке врала бы
        бесконечно. Время «последний раз» не трогаем: оно правдиво."""
        for gw in self.db.gateways():
            self.db.set_state(self._gwlink_key(self._GWLINK_SESSION_KEY, gw.id), "")
            self.db.set_state(self._gwlink_key(self._GWLINK_SNAP_REV_KEY, gw.id), "")

    def gwlink_note_error(self, slot_id: int, reason: str) -> None:
        """Почему сервер оборвал сессию. Без этого «нет связи» оставалось бы без
        причины — а самая коварная из них, разошедшиеся часы, иначе не видна
        вовсе: каждое сообщение отвергается окном времени, и канал просто молчит."""
        self.db.set_state(self._gwlink_key(self._GWLINK_ERROR_KEY, slot_id),
                          f"{timeutil.to_iso(timeutil.now())} {reason[:200]}")

    def gwlink_session(self, slot_id: int) -> dict:
        """{'since', 'agent', 'proto', 'caps': [...]} или пусто — не на связи."""
        raw = self.db.get_state(self._gwlink_key(self._GWLINK_SESSION_KEY, slot_id)) or ""
        out: dict = {}
        for part in raw.split():
            name, _, val = part.partition("=")
            if name in ("since", "agent", "proto"):
                out[name] = val
            elif name == "caps":
                out["caps"] = [c for c in val.split(",") if c]
        if out and "caps" not in out:
            out["caps"] = []
        return out
