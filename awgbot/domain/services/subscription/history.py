"""history.py — чистка истории (таблицы *_histories) по ретеншну — задание планировщика."""
from __future__ import annotations

from awgbot.core import config
from awgbot.util import timeutil


class HistoryMixin:
    def purge_old_history(self) -> dict:
        """Scheduler: удалить историю старше ретеншна (relativedelta лет из конфига).
        Считаем cutoff по календарю (високосные корректно), удаляем батчами по всем
        _histories. Возвращает {таблица: удалено} для лога."""
        from dateutil.relativedelta import relativedelta
        cutoff = timeutil.now() - relativedelta(years=config.HISTORY_RETENTION_YEARS)
        return self.db.purge_histories(timeutil.to_iso(cutoff),
                                       config.HISTORY_PURGE_BATCH_SIZE)
