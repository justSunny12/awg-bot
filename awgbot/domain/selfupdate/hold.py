"""hold.py — часть пакета selfupdate (см. __init__): окно ожидания после
старта процесса.

После рестарта роль знает другую сторону только по старым данным: основной
бот — версии шлюзов из прежних снимков, агент — версию сервера из прежнего
`role`. Решать по ним сразу — значит счесть только что обновлённый, но ещё не
переподключившийся шлюз старым (или молчащим). Отметить всех «на связи» при
старте — подделка состояния; так не делаем. Вместо этого после старта цель не
считается и не объявляется, пока другая сторона не скажет о себе заново:
основной бот ждёт переподключения шлюзов, бывших на связи перед остановкой
(gwlink_seen моложе UPDATE_PEER_FRESH_DAYS), агент — первого `role`. Предел —
UPDATE_HOLD_MINUTES; затем — обычное правило по последним известным данным.

Окно живёт в памяти процесса: новый процесс — новое окно. Кого ждать, роль
говорит сама: update_hold_peers() — список при старте (пусто — ждать
некого), update_hold_left(список) — кого из них ещё нет (имена для экрана).
Без этих методов окна нет вовсе.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from awgbot.util import timeutil

log = logging.getLogger("awgbot.selfupdate")

# Предел окна — с запасом против бэкоффа переподключения агента: шаги 5–300 с
# с разбросом ±40 % (runtime/linkclient), до ~7 минут между попытками.
UPDATE_HOLD_MINUTES = 15


class SelfUpdateHoldMixin:
    """Окно ожидания другой стороны после старта процесса."""

    def update_hold_start(self) -> None:
        """Открыть окно — один раз, на старте процесса (runtime/main)."""
        fn = getattr(self, "update_hold_peers", None)
        initial = list(fn()) if callable(fn) else []
        if not initial:
            return
        until = timeutil.now() + datetime.timedelta(minutes=UPDATE_HOLD_MINUTES)
        self.__dict__["_upd_hold"] = {"until": until, "initial": initial}
        log.info("обновление: после старта жду другую сторону (%s) — до %s",
                 len(initial), until.strftime("%H:%M"))

    def update_hold(self) -> Optional[dict]:
        """Открыто ли окно: {"until": datetime, "waiting": [имена], "role"} или
        None. Все дождались или предел вышел — окно закрывается здесь же."""
        hold = self.__dict__.get("_upd_hold")
        if not hold:
            return None
        left_fn = getattr(self, "update_hold_left", None)
        left = list(left_fn(hold["initial"])) if callable(left_fn) else []
        expired = timeutil.now() >= hold["until"]
        if not left or expired:
            self.__dict__["_upd_hold"] = None
            log.info("обновление: окно после старта закрыто — %s",
                     "предел вышел" if left else "другая сторона на связи")
            return None
        from awgbot.core import config
        return {"until": hold["until"], "waiting": left, "role": config.ROLE or ""}

    def update_hold_due(self) -> bool:
        """Окно было открыто и закрылось бы сейчас — пора пересчитать цель
        (такт канала: предел вышел без события)."""
        return bool(self.__dict__.get("_upd_hold")) and self.update_hold() is None
