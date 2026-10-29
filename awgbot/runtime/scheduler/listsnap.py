"""listsnap.py — часть пакета scheduler: такт снимков списков и кнопки «Скрыть».

Раз в сутки после полуночи по поясу показа (00:01 плюс до LISTSNAP_JITTER
секунд — джиттер APScheduler только вперёд) и разово на старте через
30–120 с (догон: полночь проспали — всё равно): снимки с истёкшим сроком — в
отметки, ссылки на них в сообщениях — текстом, «Скрыть» старше суток — прочь
(bot/lists/msgs/heal.nightly: одна правка на сообщение). Задание
обязательное (MANDATORY): опоздавший такт не выбрасывается. Нечего делать —
ни одного запроса в Telegram. Один и тот же у основного бота, агента шлюза
(по поясу шлюза) и бота основы на резерве (своя база зоны)."""
from __future__ import annotations

import logging
import random

from apscheduler.triggers.cron import CronTrigger

from awgbot.core import config
from awgbot.util import timeutil

log = logging.getLogger(__name__)

LISTSNAP_JITTER = 300          # до 5 минут после 00:01
STARTUP_DELAY = (30, 120)      # догон на старте, секунд


def add_listsnap_nightly(scheduler, services, bot) -> None:
    """Такт после полуночи по поясу показа и разовый догон на старте."""
    from awgbot.runtime.scheduler.common import MANDATORY

    async def job_listsnap():
        try:
            from awgbot.bot.lists.msgs import heal
            await heal.nightly(bot, services)
        except Exception as e:                           # noqa: BLE001
            log.warning("lists: такт не прошёл: %s", e)

    scheduler.add_job(job_listsnap, CronTrigger(hour=0, minute=1, jitter=LISTSNAP_JITTER, timezone=config.TZ),
                      id="listsnap_nightly", max_instances=1, **MANDATORY)
    run_at = timeutil.shift(timeutil.now(), random.uniform(*STARTUP_DELAY))
    scheduler.add_job(job_listsnap, "date", run_date=run_at, id="listsnap_startup", max_instances=1, **MANDATORY)
