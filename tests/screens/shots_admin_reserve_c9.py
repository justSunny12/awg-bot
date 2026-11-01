"""Снимки админа: ожидание правок бота на резерве по отметке С9 без рычага
(срыв переезда после передачи бота, снятый зависший вебхук) — плашка карточки
основы «жду события» до срока и после него (со ссылкой «не ждать?»), экран
«не ждать» по ссылке, «🗑 Не ждать» (итог уведомлением, карточка без плашки),
повторное нажатие, когда отметки уже нет, отказ, пока резерв несёт бот, и
карточка и отказ, пока журнал бота на резерве приходит (часть перенесена только
что) — срок вышел, а ссылки нет.

Состояние — ключом `rsv_c9` (как после `reserve_c9_start`) и `rsv_peer_state`;
часы заморожены (NOW), момент заведения отметки — от NOW."""
from __future__ import annotations

from awgbot.bot.callbacks import RsvCB

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import _iso
from tests.screens.shots_admin_reserve import RSV, _peer_down, _rsv

C9_LINK = "rsv-c9"
DROP = RsvCB(action="c9_drop")


def _c9(age_s: int, *, moved: int = 0, got_s: int | None = None):
    """Отметка С9 заведена age_s секунд назад (эпоха гостя 4, перенесено moved; got_s —
    последняя часть журнала перенесена столько секунд назад)."""
    def step(services, mp):
        at = NOW.timestamp() - age_s
        services.rsv_put("rsv_c9", {"e": 4, "F": 10, "upto": 10 + moved, "moved": moved, "disputed": [], "total": 0,
                                    "at": _iso(NOW), "at_ts": at, "k": "33" * 32,
                                    **({"got_ts": NOW.timestamp() - got_s} if got_s is not None else {})})
    return step


def _hosting(services, mp):
    """Резерв на связи и несёт мой бот."""
    services.rsv_update("rsv_peer_state", **{"inn": "serving"})


def _paired(*then):
    return _rsv(ready=True, gw="rsv", paired=True, then=list(then))


RESERVE_C9 = [
    Shot("adm.rsv.card.c9", role="admin", press=[RSV], data=_paired(_c9(60), _peer_down),
         title="карточка основы: отметка С9 — «жду события с «Beta»», срок не вышел"),
    Shot("adm.rsv.card.c9.overdue", role="admin", press=[RSV], data=_paired(_c9(400), _peer_down),
         title="карточка основы: отметка С9 дольше 6 минут — «(не ждать?)» ссылкой"),
    Shot("adm.rsv.c9", role="admin", start=C9_LINK, data=_paired(_c9(400, moved=3), _peer_down),
         title="экран «не ждать» по ссылке: что потеряется, «Beta» не на связи, перенесено 3"),
    Shot("adm.rsv.c9.drop", role="admin", steps=[("start", C9_LINK), ("press", DROP)],
         data=_paired(_c9(400), _peer_down), title="«🗑 Не ждать»: карточка без плашки, итог уведомлением"),
    Shot("adm.rsv.c9.gone", role="admin", steps=[("start", C9_LINK), ("call", lambda s, b: _drop_now(s)), ("press", DROP)],
         data=_paired(_c9(400), _peer_down), title="«🗑 Не ждать», а отметки уже нет: всплывашка и карточка"),
    Shot("adm.rsv.c9.busy", role="admin", steps=[("start", C9_LINK), ("press", DROP)],
         data=_paired(_c9(400), _hosting), title="«🗑 Не ждать», пока «Beta» несёт бот: отказ всплывашкой"),
    Shot("adm.rsv.card.c9.arriving", role="admin", press=[RSV], data=_paired(_c9(400, moved=3, got_s=10), _peer_down),
         title="карточка основы: отметке С9 больше 6 минут, но журнал с «Beta» приходит — без «(не ждать?)»"),
    Shot("adm.rsv.c9.arriving", role="admin", steps=[("start", C9_LINK), ("press", DROP)],
         data=_paired(_c9(400, moved=3, got_s=10), _peer_down),
         title="«🗑 Не ждать», пока журнал с «Beta» приходит: отказ всплывашкой"),
]


async def _drop_now(services):
    """Отметку сняло событие (такт дома) между показом экрана и нажатием."""
    services.rsv_put("rsv_c9", None)
