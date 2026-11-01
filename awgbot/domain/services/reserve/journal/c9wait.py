"""
c9wait.py — часть пакета journal (см. __init__): ожидание правок бота на
резерве по отметке С9 (`rsv_c9`, journal/c9) без рычага — плашка карточки и
«🗑 Не ждать».

Без рычага (срыв переезда после передачи бота, снятый зависший вебхук) отметку
снимает факт: резерв живым словом после моего перехода домой гостя не несёт
(`reserve_c9_settle`) или говорит, что журнала моего бота у него больше нет
(`jc9none`, `reserve_c9_none_in`); журнал на его диске он шлёт сам (c9deliver).
Резерв, который не вернётся никогда, этого не скажет — отметка висела бы
бессрочно. Пока она стоит, карточка несёт плашку «жду события»; за C9_WAIT_S от
заведения не пришли ни журнал, ни «журнала нет» — со ссылкой на экран «не
ждать», где админ снимает её сам (правки бота на резерве, если были, сюда уже
не придут). Журнал идёт (часть перенесена за последние C9_PROGRESS_S) — ссылки
нет и снятие отказывает: «приходит — подожди». С рычагом (`rsv_out.await_merge`)
отметку ведёт возврат — здесь её не трогают.
"""
from __future__ import annotations

from datetime import datetime, timezone

from awgbot.core import config
from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification, ServiceError
from ..state import HOSTING

C9_WAIT_S = 360                   # отметка С9 без рычага старше — в карточке «не ждать?»; и срок быстрых повторов резерва
C9_PROGRESS_S = 60                # часть журнала бота на резерве перенесена не раньше — журнал идёт, «не ждать» нет


class ReserveC9WaitMixin:

    def _c9_since_ts(self, c9: dict) -> float | None:
        """Момент заведения отметки (unix): `at_ts`; у отметки прежней версии — по `at`
        (заводилась с ним всегда); нет и его — None (отсчёт начнёт такт дома)."""
        try:
            if c9.get("at_ts") is not None:
                return float(c9["at_ts"])
            if c9.get("at"):
                return timeutil.parse_iso(str(c9["at"])).timestamp()
        except (TypeError, ValueError):
            pass
        return None

    def reserve_c9_stamp(self) -> None:
        """Такт дома: у отметки без момента заведения — отсчёт с первого такта, который её
        видит (не раньше: срок «не ждать?» не наступает до времени)."""
        c9 = self.rsv_get("rsv_c9")
        if c9 and self._c9_since_ts(c9) is None:
            self.rsv_update("rsv_c9", at_ts=timeutil.now().timestamp())

    def _c9_wait_mark(self) -> dict:
        """Отметка С9 без рычага у основы дома — или пусто (рычаг, переезд, гость, нет отметки)."""
        if getattr(self, "guest", False):
            return {}
        c9 = self.rsv_get("rsv_c9")
        if not c9:
            return {}
        out = self._out()
        if str(out.get("state") or "home") != "home" or out.get("await_merge"):
            return {}
        return c9

    def _c9_progressing(self, c9: dict) -> bool:
        """Журнал бота на резерве идёт: часть перенесена не раньше C9_PROGRESS_S назад."""
        try:
            got = float(c9["got_ts"]) if c9.get("got_ts") is not None else None
        except (TypeError, ValueError):
            got = None
        return got is not None and timeutil.now().timestamp() - got < C9_PROGRESS_S

    def reserve_c9_drop_why(self) -> str:
        """Почему «не ждать» нельзя: правки бота на резерве ещё придут — резерв на связи и
        несёт моих (гость шлёт их сам), или его журнал идёт сюда прямо сейчас. Пусто — можно."""
        name = self.reserve_peer_name()
        c9 = self._c9_wait_mark()
        if c9 and self._c9_progressing(c9):
            return f"журнал с «{name}» приходит — подожди"
        peer = self.reserve_peer_state()
        if self._peer_alive(peer) and str(peer.get("inn") or "idle") in HOSTING:
            return f"«{name}» на связи и несёт твой бот — его правки придут сами"
        return ""

    def reserve_c9_wait_view(self) -> dict:
        """Плашка карточки и экран «не ждать»: с какого момента жду, истёк ли срок (за
        C9_WAIT_S ни журнала, ни «журнала нет», и журнал не идёт), что известно о резерве.
        Пусто — отметки без рычага нет."""
        c9 = self._c9_wait_mark()
        if not c9:
            return {}
        ts = self._c9_since_ts(c9)
        now = timeutil.now().timestamp()
        peer = self.reserve_peer_state()
        # срок — от заведения; журнал идёт (часть за C9_PROGRESS_S) — срок не вышел, пока он не встанет
        return {"since": timeutil.to_iso(datetime.fromtimestamp(ts, timezone.utc)) if ts is not None else "",
                "overdue": ts is not None and now - ts >= C9_WAIT_S and not self._c9_progressing(c9),
                "online": self._peer_alive(peer), "down_since": self.reserve_down_since(),
                "moved": int(c9.get("moved") or 0), "drop_why": self.reserve_c9_drop_why()}

    def reserve_c9_drop(self) -> list[Notification] | None:
        """«🗑 Не ждать»: снять отметку С9 без рычага — правки бота на резерве после
        передачи, если были, сюда не придут. None — отметки уже нет (снята фактом или
        прежним нажатием); отказ — ServiceError с причиной."""
        c9 = self._c9_wait_mark()
        if not c9:
            return None
        why = self.reserve_c9_drop_why()
        if why:
            raise ServiceError(why)
        self.rsv_put("rsv_c9", None)
        name = self.reserve_peer_name()
        log.warning("резерв: правки бота на «%s» эпохи %s после %s больше не жду — отметка С9 снята админом "
                    "(перенесено %s)", name, c9.get("e"), c9.get("upto"), int(c9.get("moved") or 0))
        from awgbot.domain.notices import reserve_tail as rt
        return [Notification(tg_id=int(config.ADMIN_ID), text=rt.c9_dropped(name))]


__all__ = ["C9_PROGRESS_S", "C9_WAIT_S", "ReserveC9WaitMixin"]
