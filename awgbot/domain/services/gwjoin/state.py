"""state.py — часть пакета gwjoin (см. __init__): запись сопряжения и память процесса.

`gw_join_<слот>` (server_state):
{"id": 16 hex, "kind": new | replace | same, "slot", "link_if", "port", "cidr",
 "device": id | "new", "prev_device": id, "name": имя слота для текстов,
 "version": выпуск для шлюза, "issued", "expires": ISO,
 "sessions", "attempts", "endpoints": [адрес], "hosts": {адрес: hostname},
 "msg": [чат, сообщение], "state": waiting | connected | accepted | linked | closing}

closing — возврат линка начат (срок, отмена, код, `why`) и не удался или ещё
не сделан: такт сопряжений повторяет его (pairing.gwjoin_tick).

Эфемерные ключи, код и ключ сопряжения в базу не пишутся — только здесь, в
памяти процесса: перезапуск сервера рвёт сессию, шлюз переподключается, новая
сессия даёт новый код (сообщение говорит об этом).
"""
from __future__ import annotations

import json
import threading

from awgbot.util import timeutil

JOIN_TTL = 3600            # команда — чтобы выполнить её сейчас, а не хранить
MAX_SESSIONS = 10          # сессий по токену
MAX_ATTEMPTS = 3           # неверных кодов на токен
JOIN_KEY = "gw_join"

# {слот: {"sid": id сессии, "code", "kp": ключ сопряжения, "eph": открытый ключ сервера}}
_LIVE: dict[int, dict] = {}
_LOCK = threading.RLock()


class GwJoinStateMixin:
    _JOIN_KEY = "gw_join"               # = JOIN_KEY: литералом — его видит сторож реестра слотовых ключей

    def gwjoin(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gw_slot_key(self._JOIN_KEY, slot_id)) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _gwjoin_save(self, slot_id: int, st: dict) -> None:
        self.db.set_state(self._gw_slot_key(self._JOIN_KEY, slot_id), json.dumps(st, ensure_ascii=False) if st else "")

    def gwjoin_all(self) -> list[dict]:
        """Живые сопряжения по всем слотам, которые могут быть: строки слотов и
        номера сверх них (новый слот строки ещё не имеет)."""
        from awgbot.core import config
        out = []
        for n in range(1, int(config.ROUTING_GATEWAYS_MAX) + 1):
            st = self.gwjoin(n)
            if st.get("id"):
                out.append(st)
        return out

    @staticmethod
    def _gwjoin_live(slot_id: int) -> dict:
        with _LOCK:
            return dict(_LIVE.get(int(slot_id)) or {})

    @staticmethod
    def _gwjoin_live_set(slot_id: int, data: dict | None) -> None:
        with _LOCK:
            if data:
                _LIVE[int(slot_id)] = dict(data)
            else:
                _LIVE.pop(int(slot_id), None)

    @staticmethod
    def _gwjoin_expired(st: dict) -> bool:
        try:
            return timeutil.elapsed(timeutil.parse_iso(str(st.get("expires") or ""))) >= 0
        except ValueError:
            return True
