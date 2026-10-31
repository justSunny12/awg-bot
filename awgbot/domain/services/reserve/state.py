"""
state.py — часть пакета reserve (см. __init__): пара в БД и ключи `rsv_*`
состояния, состояния направлений.

Пара — строка `reserve_peer` (одна). Всё остальное о паре — ключи
`server_state` с началом `rsv_` (перечень — KEYS): JSON-объекты, читаются и
пишутся через `rsv_get`/`rsv_put`. В пакет переезда они не едут никогда
(сторож `util/rsvpkg/bot.FORBIDDEN_STATE`).

Направления: `rsv_out` — мои пользователи (`home` / `leaving` / `away` /
`returning`), `rsv_in` — пользователи резерва у меня (`idle` / `taking` /
`serving` / `releasing` / `relay`), `rsv_poll` — право отвечать в Telegram
(`mine` / `away`).
"""
from __future__ import annotations

import json
import logging
import threading
import time

from awgbot.util import timeutil

log = logging.getLogger(__name__)

KEYS: tuple[str, ...] = (
    "rsv_invite", "rsv_peer_state", "rsv_out", "rsv_in", "rsv_poll", "rsv_keys", "rsv_view",
    "rsv_pkg_due", "rsv_pkg_sent", "rsv_pkg_in", "rsv_peer_admin", "rsv_guest_links",
    "rsv_gw_links", "rsv_traffic", "rsv_dns", "rsv_record_watch", "rsv_follow", "rsv_jrn_out", "rsv_jrn_in", "rsv_tail", "rsv_tail_done", "rsv_c9",
    "rsv_quorum", "rsv_probes", "rsv_qkeys", "rsv_qlog", "rsv_rfpeer",
)
# Отметки «отправлено» слов второй стороне по каналу пары, по видам слова: в памяти
# процесса (`__dict__`) и в `rsv_view`. Ставятся при сборке слова; не ушло — снимаются
# (`reserve_pair_unsent`), новая сессия начинает их заново (`reserve_pair_sent_reset`).
# st — не сессионная: его тело уходит в начале каждой сессии само, отметка — отпечаток
# содержимого (снимается только неудачей отправки). Сторож — tests/integration/test_reserve_sent_marks.
PAIR_SENT_MEM: dict[str, tuple[str, ...]] = {
    "qkeys": ("_qkeys_sent", "_qkeys_sent_at"), "qmem": ("_qmem_sent",), "blocks": ("_rsv_blocks_sent",),
    "jtail": ("_tail_sent_at",), "st": ("_rsv_st_sent",),
}
PAIR_SENT_VIEW: dict[str, dict] = {"qprobe": {"qprobe_sent": ""}, "slots": {"slots_sent": [], "slots_at": ""}}
PAIR_SENT_NOT_SESSION = frozenset({"st"})
OUT_STATES = ("home", "leaving", "away", "returning")
IN_STATES = ("idle", "taking", "serving", "releasing", "relay")
HOSTING = ("taking", "serving", "releasing")     # у стороны — пользователи и бот второй (`rsv_in` и `st.in`)
POLL_STATES = ("mine", "away")

_LOCK = threading.RLock()


class ReserveStateMixin:

    # ── ключи состояния ─────────────────────────────────────────────────────

    def rsv_get(self, key: str) -> dict:
        if key not in KEYS:
            raise KeyError(f"не ключ резерва: {key}")
        raw = self.db.get_state(key) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def rsv_put(self, key: str, data: dict | None) -> None:
        if key not in KEYS:
            raise KeyError(f"не ключ резерва: {key}")
        self.db.set_state(key, json.dumps(data, ensure_ascii=False) if data else "")

    def rsv_update(self, key: str, **fields) -> dict:
        with _LOCK:
            cur = self.rsv_get(key)
            cur.update(fields)
            self.rsv_put(key, cur)
            return cur

    def rsv_clear_all(self) -> None:
        """Удаление пары: все ключи резерва прочь (кроме месячного итога в таблице)."""
        for k in KEYS:
            self.rsv_put(k, None)

    # ── пара ─────────────────────────────────────────────────────────────────

    def reserve_peer(self) -> dict | None:
        return self.db.reserve_peer()

    def reserve_paired(self) -> bool:
        return self.reserve_peer() is not None

    def reserve_peer_name(self) -> str:
        p = self.reserve_peer()
        return (p or {}).get("name") or "резерв"

    def reserve_peer_state(self) -> dict:
        """Последний `st` резерва о себе (версия, возможности, готовность, порты, голосующие)."""
        return self.rsv_get("rsv_peer_state")

    # ── направления ─────────────────────────────────────────────────────────

    def reserve_out(self) -> str:
        st = self.rsv_get("rsv_out").get("state") or "home"
        return st if st in OUT_STATES else "home"

    def reserve_in(self) -> str:
        st = self.rsv_get("rsv_in").get("state") or "idle"
        return st if st in IN_STATES else "idle"

    def reserve_poll(self) -> str:
        st = self.rsv_get("rsv_poll").get("state") or "mine"
        return st if st in POLL_STATES else "mine"

    def reserve_set_out(self, state: str, **why) -> dict:
        if state not in OUT_STATES:
            raise ValueError(f"не состояние направления: {state}")
        prev = str(self.rsv_get("rsv_out").get("state") or "home")
        if prev != state:
            why.setdefault("stuck_since", "")             # счёт зависшего вебхука — заново в новом состоянии (outbound_stuck)
        if state in ("home", "leaving") or (state == "away" and prev in ("home", "leaving")):
            # пульт (pult/primary) — только эпизода, где пользователи у резерва: дома и в новом эпизоде его нет;
            # из returning обратно в away (возврат сорвался) — остаётся
            why.setdefault("pult_at", "")
            why.setdefault("pult_bad", {})
            why.setdefault("pult_give_mono", 0)
        # at_ts — порядок с точностью до долей секунды: роль эпохи сверяет «что было раньше»
        out = self.rsv_update("rsv_out", state=state, since=timeutil.now_iso(), at_ts=time.time(), **why)
        self.reserve_epoch_sync()                         # право записи — по новому состоянию
        return out

    def _out_is(self, state: str, steps=()) -> bool:
        """`rsv_out` сейчас в состоянии `state` (и на одном из шагов `steps`, если даны).
        Звать под `_LOCK` — иначе ответ устаревает к моменту записи."""
        out = self.rsv_get("rsv_out")
        if str(out.get("state") or "home") != state:
            return False
        return not steps or str(out.get("step") or "") in steps

    def _out_cas(self, state: str, steps=(), *, to: str = "", **fields) -> bool:
        """Переход автомата «мои пользователи» «проверил — записал» одним замком: обработчики
        слов канала пары (to_thread) и такт читают `rsv_out`, решают и пишут из разных потоков —
        без перечитывания под замком второй писал бы свой переход поверх чужого (`taken` и
        срыв по сроку: away и home разом). `to` — новое состояние (`reserve_set_out`), без
        него — правка полей. Ложь — автомат уже не там, где ожидалось: переход не мой,
        ничего не делать."""
        with _LOCK:
            if not self._out_is(state, steps):
                return False
            if to and to != state:
                self.reserve_set_out(to, **fields)
            else:
                self.rsv_update("rsv_out", **fields)
            return True

    def reserve_set_in(self, state: str, **why) -> dict:
        if state not in IN_STATES:
            raise ValueError(f"не состояние направления: {state}")
        return self.rsv_update("rsv_in", state=state, since=timeutil.now_iso(), **why)

    def reserve_set_poll(self, state: str, **why) -> dict:
        if state not in POLL_STATES:
            raise ValueError(f"не право ответа: {state}")
        prev = str(self.rsv_get("rsv_poll").get("state") or "")
        # why/url — только этого решения: прежнее «webhook» не должно пережить «mine»
        out = self.rsv_update("rsv_poll", state=state, since=timeutil.now_iso(), at_ts=time.time(),
                              **{"why": "", "url": "", **why})
        self.reserve_epoch_sync()                         # право записи — по новому состоянию (не по слову Telegram)
        if state != prev and self.rsv_get("rsv_out").get("stuck_since"):
            self.rsv_update("rsv_out", stuck_since="")    # счёт зависшего вебхука — заново (outbound_stuck)
        if state == "away" and prev != "away":
            # бот на резерве правит живые меню и снимает кнопки уведомлений сам: сохранённые
            # клавиатуры таких сообщений перестают быть правдой — записи прочь (снимки живут до срока)
            try:
                n = self.db.list_msg_drop_live()
            except Exception as e:                        # noqa: BLE001 — передача права важнее учёта
                log.warning("lists: записи сообщений с кнопками действия не сняты: %s", e)
            else:
                if n:
                    log.info("lists: право отвечать у резерва — записи сообщений с кнопками действия сняты: %d", n)
        return out

    def reserve_busy(self) -> str:
        """Идёт переезд или возврат (в любую сторону) — человеку словами; пусто — нет."""
        out, inn = self.reserve_out(), self.reserve_in()
        if out in ("leaving", "returning"):
            return "идёт переезд пользователей этого сервера" if out == "leaving" else "идёт возврат пользователей на этот сервер"
        if inn in ("taking", "releasing"):
            return "принимаю пользователей резерва" if inn == "taking" else "возвращаю пользователей резерву"
        return ""

    def reserve_hosting(self) -> bool:
        """У меня сейчас пользователи и бот резерва."""
        return self.reserve_in() in HOSTING

    def reserve_zone_free(self) -> bool:
        """Зона свободна для нового открытия: idle — или relay (пересылка хвоста
        уступает: `_relay_yield` снимает её перед открытием)."""
        return self.reserve_in() in ("idle", "relay")

    def reserve_away(self) -> bool:
        """Мои пользователи и бот — у резерва."""
        return self.reserve_out() in ("leaving", "away", "returning")
