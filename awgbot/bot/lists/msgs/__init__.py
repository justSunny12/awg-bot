"""
msgs — учёт и правка сообщений со ссылками на снимки, кнопкой «Скрыть» и итогом
живого меню (часть пакета lists; хранилище — infra/db/listsnap, записи `m:`).

Единая точка — посредник запросов сессии бота (watch): видит каждую отправку,
правку и удаление у основного бота, агента и бота основы на резерве, после
удачного ответа Telegram ведёт запись сообщения (чат, номер, время отправки,
клавиатура, ключи снимков, отправленный HTML, итог). Правит сообщения он же
(heal): ссылка умершего снимка — текстом (вытесненный — тем же действием, до
ответа обработчику; истёкший — тактом после полуночи), «Скрыть» старше суток —
прочь (одной правкой на сообщение). Гашение прежнего меню с итогом — в след
(retire), нажатая «Скрыть» — снимки и запись прочь (hide).

Итог первой строкой экрана клеит screens.with_note; границу по готовому
тексту не восстановить, поэтому она запоминает итог в переменной контекста
текущей задачи (NOTE), а посредник, увидев отправку или правку, чей текст с
него начинается, кладёт его в запись и переменную очищает.

Части: scan — чистые разборы HTML и клавиатуры; watch — посредник; heal —
правка и такт; retire — прежнее меню с итогом в след; hide — нажатая «Скрыть».
"""
from __future__ import annotations

import contextvars
import logging

log = logging.getLogger(__name__)

# итог, который screens.with_note приклеила к экрану в этой задаче; снимает посредник
NOTE: contextvars.ContextVar[str] = contextvars.ContextVar("listsnap_note", default="")
# правка идёт от heal: запись он обновляет сам, посредник её не трогает (иначе снял бы итог)
OWN: contextvars.ContextVar[bool] = contextvars.ContextVar("listsnap_own", default=False)
# правку живого меню отклонил Telegram (чат, номер): обработчик шлёт экран заново новым
# сообщением той же задачей — посредник делает живым меню его; снимает посредник
REISSUE: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar("listsnap_reissue", default=None)


def attach(bot, services) -> None:
    """Повесить посредник на сессию бота (как alive.watch): сессия без
    посредников — тестовый двойник — ничего."""
    from .watch import ListMsgWatch
    mw = getattr(getattr(bot, "session", None), "middleware", None)
    if mw is not None and hasattr(mw, "register"):
        mw.register(ListMsgWatch(services))


__all__ = ["NOTE", "OWN", "REISSUE", "attach", "log"]
