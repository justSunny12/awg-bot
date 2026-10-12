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
некого), update_hold_left(список, начало окна) — кто из них ещё не сказал о
себе после начала окна (имена для экрана). Без этих методов окна нет вовсе.

Закрывает окно не читатель (экраны, кнопка), а выбор цели под замком —
иначе экран, первым заметивший конец окна, закрыл бы его без пересчёта, и
цель так и осталась бы не посчитанной до следующей проверки.
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
        start = timeutil.now().replace(microsecond=0)        # как пишется gwlink_seen — до секунды
        until = start + datetime.timedelta(minutes=UPDATE_HOLD_MINUTES)
        self.__dict__["_upd_hold"] = {"start": start, "until": until, "initial": initial}
        log.info("обновление: после старта жду другую сторону (%s) — до %s",
                 len(initial), until.strftime("%H:%M"))

    def _update_hold_state(self):
        """(окно, кого ещё ждём) без изменения окна; окно None — закрыто."""
        hold = self.__dict__.get("_upd_hold")
        if not hold:
            return None, []
        left_fn = getattr(self, "update_hold_left", None)
        left = list(left_fn(hold["initial"], hold["start"])) if callable(left_fn) else []
        return hold, left

    def _update_hold_satisfied(self, hold, left) -> bool:
        return not left or timeutil.now() >= hold["until"]

    def update_hold(self) -> Optional[dict]:
        """Открыто ли окно: {"until": datetime, "waiting": [имена], "role"} или
        None. Читатель окно не закрывает: дождались или предел вышел — здесь
        только «не открыто»; закрывает выбор цели (cache._update_choose) и
        такт канала через update_hold_due — с гарантированным пересчётом."""
        hold, left = self._update_hold_state()
        if hold is None or self._update_hold_satisfied(hold, left):
            return None
        from awgbot.core import config
        return {"until": hold["until"], "waiting": left, "role": config.ROLE or ""}

    def _update_hold_close(self, due: bool) -> None:
        """Закрыть окно, если оно открыто (зовёт выбор под замком). due —
        закрыл не пересчёт: он всё равно нужен, такт канала его сделает."""
        hold = self.__dict__.get("_upd_hold")
        if not hold:
            return
        _, left = self._update_hold_state()
        self.__dict__["_upd_hold"] = None
        if due:
            self.__dict__["_upd_hold_due"] = True
        log.info("обновление: окно после старта закрыто — %s",
                 "предел вышел" if left else "другая сторона на связи")

    def update_hold_due(self) -> bool:
        """Пора пересчитать цель (такт канала): окно ещё не закрыто, но ждать
        уже некого или предел вышел; или его закрыл не пересчёт. Флаг снимает
        тот, кто пересчитал (cache.update_regate)."""
        if self.__dict__.get("_upd_hold_due"):
            return True
        hold, left = self._update_hold_state()
        return hold is not None and self._update_hold_satisfied(hold, left)
