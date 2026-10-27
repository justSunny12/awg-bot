"""
epoch.py — часть пакета journal (см. __init__): эпоха линии данных.

Держатель эпохи пишет решения; основа, чьи пользователи и бот у резерва, —
следует: база отказывает ей в записи решений (временные триггеры, util/rsvjrn),
задания решений в планировщике выключены. Эпоха передаётся фактами, которые
и сейчас передают пользователей и бот, — отдельного голосования нет:

  к резерву  финальная реплика собрана (передача бота кнопкой и по согласию),
             Telegram сказал «вебхук стоит» (409, getWebhookInfo), резерв
             несёт пользователей мимо меня (st: serving)
  ко мне     реплика возврата пришла (бот основы на резерве остановлен),
             «⚠️ Да, вернуть», срыв переезда, снятый зависший вебхук

Страховка: пары нет или пользователи и Telegram здесь (`home`, поллинг мой) —
держатель здесь, что бы ни было записано.
"""
from __future__ import annotations

from ...gateway_link.common import log


class ReserveEpochMixin:

    def reserve_following(self) -> bool:
        """Основа следует за ботом на резерве: решения здесь не пишутся."""
        try:
            return bool(self.db.jrn_following())
        except Exception:                                     # noqa: BLE001 — база старше эпохи (тесты, CLI)
            return False

    def reserve_epoch_follow(self, why: str) -> None:
        """Право записи — боту основы на резерве (без пары — некому: чужой вебхук на моём
        токене — не переезд)."""
        if not self.reserve_paired():
            return
        meta = self.db.jrn_pass(holder="peer", role="follower", why=why)
        if meta is not None:
            log.warning("резерв: эпоха %s у «%s» — здесь следование (%s)", meta["epoch"], self.reserve_peer_name(), why)

    def reserve_epoch_hold(self, why: str) -> None:
        """Право записи — снова здесь."""
        meta = self.db.jrn_pass(holder="self", role="holder", why=why)
        if meta is not None:
            log.warning("резерв: эпоха %s здесь (%s)", meta["epoch"], why)
            self.reserve_follow_clear()

    def reserve_epoch_check(self) -> None:
        """Такт: следование без пары или при пользователях и Telegram здесь — держатель здесь."""
        if not self.reserve_following():
            return
        if not self.reserve_paired():
            self.reserve_epoch_hold("пары нет")
        elif self.reserve_out() == "home" and self.reserve_poll() == "mine":
            self.reserve_epoch_hold("пользователи и Telegram здесь")
