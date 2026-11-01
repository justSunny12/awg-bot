"""
c9deliver.py — часть пакета journal (см. __init__): сторона резерва — журнал
данных гостя основе, которая ждёт его по отметке С9.

Основа взяла эпоху без released (journal/c9) и ждёт правки бота на резерве
эпохи `e` после `upto` — так и говорит в `st.jrn.mine.c9`, пока держатель у
неё. Гость поднят — шлёт их сам (guestj). Гостя нет (зона снята, резерв
перезагрузился), а его журнал данных лежит на диске (`jrn/` каталога гостя),
— доставляю я: записи эпохи `e` подряд после `upto` уходят `jev` как есть (под
K гостя; бот резерва видит только заголовки), не больше EV_BYTES за раз.
Основа продвинулась (её `jack` транзитом, новый `upto` в st, её `jneed`) —
следующая часть сразу. Без продвижения — повтор с нарастающей паузой
(C9_RETRY_FIRST_S, вдвое, ± 20 %), пока с последнего продвижения не прошло
C9_WAIT_S; дальше — раз в C9_RETRY_SLOW_S ± 20 %, пока основа ждёт.

Записей эпохи `e` после `upto` на диске нет — слово `jc9none{e, last}`
(«журнала гостя эпохи e у меня после last нет»; last — последняя его запись
этой эпохи, 0 — ни одной), с теми же повторами: основа по нему снимает
отметку фактом. Каталог есть, а не читается, — не «журнала нет»: молчу и
повторю. Отметка доставки — в памяти: после перезапуска резерв по первому
свежему `st` основы (до него `st` прошлого процесса устаревший, journal/epoch
`_peer_alive`) смотрит диск заново и сразу шлёт журнал или `jc9none`.
"""
from __future__ import annotations

import os
import time

from awgbot.util import backoff

from ...gateway_link.common import log
from .c9wait import C9_WAIT_S
from .ship import EV_BYTES

C9_RETRY_FIRST_S = 10             # первая пауза между повторами доставки; дальше — вдвое
C9_RETRY_SLOW_S = 300             # после C9_WAIT_S без продвижения — не чаще
C9_JITTER = 0.2                   # ± доля паузы: строго периодический повтор выглядит маячком


class ReserveC9DeliverMixin:

    def _c9_deliver_want(self) -> tuple[str, int, int] | None:
        """(линия, эпоха, upto): основа ждёт журнал гостя эпохи e после upto, а доставить его
        мне — гостя у меня нет, основа на связи свежим `st`. Иначе None."""
        if getattr(self, "guest", False) or self.reserve_hosting():
            return None
        peer = self.reserve_peer_state()
        if not self._peer_alive(peer):
            return None
        mine = peer.get("jrn_mine") if isinstance(peer.get("jrn_mine"), dict) else {}
        c9 = mine.get("c9")
        line = self.reserve_keep_line()
        if not isinstance(c9, dict) or not line or str(mine.get("line") or "") != line:
            return None
        try:
            e, upto = int(c9.get("e")), int(c9.get("upto") or 0)
        except (TypeError, ValueError):
            return None
        if e < 0 or upto < 0:
            return None
        return line, e, upto

    def _c9d(self, line: str, e: int, upto: int, now: float) -> dict:
        """Отметка доставки (в памяти) под эпоху; основа продвинулась — с её номера, сразу."""
        d = self.__dict__.get("_c9d_s")
        if not d or d["key"] != (line, e):
            d = self.__dict__["_c9d_s"] = {"key": (line, e), "upto": upto, "since": now, "n": 0, "next": 0.0}
        elif upto > int(d["upto"]):
            d.update(upto=upto, since=now, n=0, next=0.0)
        return d

    @staticmethod
    def _c9d_pause(d: dict, now: float) -> float:
        """Пауза до следующего повтора: вдвое от C9_RETRY_FIRST_S, пока с последнего
        продвижения не прошло C9_WAIT_S, дальше — C9_RETRY_SLOW_S; ± C9_JITTER."""
        if now - float(d["since"]) >= C9_WAIT_S:
            base = C9_RETRY_SLOW_S
        else:
            base = min(C9_RETRY_FIRST_S * 2 ** max(0, int(d["n"]) - 1), C9_RETRY_SLOW_S)
        return backoff.jitter(base, C9_JITTER)

    def reserve_c9_deliver_tick(self) -> list[tuple]:
        """Такт журнала: основа ждёт журнал гостя, гостя нет — часть журнала с диска или
        `jc9none`, когда подошёл срок повтора."""
        want = self._c9_deliver_want()
        if want is None:
            self.__dict__.pop("_c9d_s", None)
            return []
        now = time.monotonic()
        d = self._c9d(*want, now)
        if now < float(d["next"]):
            return []
        d["n"] = int(d["n"]) + 1
        d["next"] = now + self._c9d_pause(d, now)
        return self._c9d_acts(d)

    def reserve_c9_deliver_acked(self, i: int) -> None:
        """`jack` основы транзитом, а гостя у меня нет: подтверждение моей доставки —
        продвижение, следующая часть уйдёт ближайшим тактом."""
        d = self.__dict__.get("_c9d_s")
        if d and int(i) > int(d["upto"]):
            d.update(upto=int(i), since=time.monotonic(), n=0, next=0.0)

    def reserve_c9_deliver_need(self, frm: int) -> list[tuple] | None:
        """`jneed` основы по её линии, пока я доставляю журнал гостя: с её номера — из
        журнала гостя эпохи, которую она ждёт. Таких записей нет — None (просьба не о нём:
        прежний разбор)."""
        want = self._c9_deliver_want()
        if want is None or int(frm) < 1:
            return None
        recs = self._c9d_records(want[0], want[1], int(frm) - 1)
        if not recs:
            return None
        d = self._c9d(*want, time.monotonic())
        d["upto"] = int(frm) - 1                              # её слово о себе точнее моего
        return self._c9d_jev(want[0], want[1], recs)

    def _c9d_records(self, line: str, e: int, after: int) -> list[bytes] | None:
        """Записи журнала гостя на диске линии `line` эпохи `e` с номером больше `after`;
        None — каталог есть, а не читается (не «журнала нет»)."""
        from awgbot.util.rsvjrn import event
        store = self.guest_jrn_store()
        try:
            os.listdir(store.path)
        except FileNotFoundError:
            return []                                         # каталога нет — журнала гостя нет
        except OSError as ex:
            if self.__dict__.get("_c9d_oserr") != str(ex):
                self.__dict__["_c9d_oserr"] = str(ex)
                log.warning("резерв: журнал бота «%s» на диске не читается: %s — доставку повторю",
                            self.reserve_peer_name(), ex)
            return None
        self.__dict__.pop("_c9d_oserr", None)
        return [r for r in store.events(after=after, epoch=e) if event.head(r).line == line]

    def _c9d_jev(self, line: str, e: int, recs: list[bytes]) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        group = event.runs(recs, EV_BYTES)[0]
        g0 = event.head(group[0])
        return [("send", "jev", {"line": line, "e": e, "from": g0.n, "n": len(group), "blob": b"".join(group).hex()})]

    def _c9d_acts(self, d: dict) -> list[tuple]:
        """Часть журнала после подтверждённого основой — или `jc9none`, если её нет."""
        from awgbot.util.rsvjrn import event
        line, e = d["key"]
        upto = int(d["upto"])
        recs = self._c9d_records(line, e, upto)
        if recs is None:
            return []
        name = self.reserve_peer_name()
        if recs:
            first = event.head(recs[0]).n
            if first != upto + 1:
                # с её номера записей нет (журнал гостя на диске с дырой): дыру не прикрывать — она
                # ждёт подряд; повторяю, а админ основы по сроку решит сам («не ждать?»)
                if self.__dict__.get("_c9d_hole") != (e, upto):
                    self.__dict__["_c9d_hole"] = (e, upto)
                    log.warning("резерв: в журнале бота «%s» на диске нет записи %s эпохи %s (первая — %s) — "
                                "основе досылать её нечем", name, upto + 1, e, first)
                return []
            acts = self._c9d_jev(line, e, recs)
            if int(d["n"]) == 1:
                log.info("резерв: «%s» ждёт правки своего бота эпохи %s после %s — отдаю с диска (%d записей)",
                         name, e, upto, int(acts[0][2]["n"]))
            return acts
        tops = self._c9d_records(line, e, 0)
        if tops is None:
            return []
        last = event.head(tops[-1]).n if tops else 0
        if int(d["n"]) == 1:
            log.info("резерв: «%s» ждёт правки своего бота эпохи %s после %s — у меня их нет (последняя %s), "
                     "говорю ей", name, e, upto, last)
        return [("send", "jc9none", {"line": line, "e": e, "last": last})]


__all__ = ["C9_RETRY_FIRST_S", "C9_RETRY_SLOW_S", "ReserveC9DeliverMixin"]
