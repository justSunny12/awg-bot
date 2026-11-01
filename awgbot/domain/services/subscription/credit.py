"""credit.py — счёт дней паузы: начисление при оплате периода, пороги типов,
сколько дней можно взять сейчас. Отдельно от входа и выхода (pause): правила
счёта читают и продление (period), и создание профиля (clients)."""
from __future__ import annotations

from awgbot.core import settings
from awgbot.core.enums import SubStatus, PeriodKind
from awgbot.domain.services.types import PauseCredit


class CreditMixin:
    # ── счёт дней паузы (docs: README, §6a «пауза подписки») ───────────────
    # Годовая: +28 при создании на год и при каждом продлении на год (даже
    # несвоевременном), копится до двух таких. Ежемесячная: +2 за каждый
    # своевременно оплаченный месяц (создание и продление, пока подписка не
    # истекла и без отсрочки), копится до 12 таких. Смена типа: остаток
    # переносится в пределах максимума нового типа. День/неделя: счёт не
    # пополняется, остаток живёт. Бессрочно: останавливать нечего — счёт ноль.

    @staticmethod
    def pause_year_days() -> int:
        return settings.get_int("pause.pause_max_total_days", 28)

    @staticmethod
    def pause_month_days() -> int:
        return settings.get_int("pause.monthly_pause_days", 2)

    @classmethod
    def pause_month_cap(cls) -> int:
        return 12 * cls.pause_month_days()

    @classmethod
    def pause_year_cap(cls) -> int:
        return 2 * cls.pause_year_days()

    def _pause_credit(self, client, new_kind: str) -> "PauseCredit":
        """Счёт после оплаты периода new_kind. client — состояние ДО (None при
        создании). Ежемесячно «своевременно» = подписка не истекла и без
        отсрочки в закрываемом периоде; годовое начисление — безусловное.
        Накопленное сверх порога нового типа не сгорает — просто выше порога
        не начисляется."""
        bal = int(client.pause_balance_days) if client is not None else 0
        if new_kind == PeriodKind.YEAR:
            add, cap = self.pause_year_days(), self.pause_year_cap()
        elif new_kind == PeriodKind.MONTH:
            add, cap = self.pause_month_days(), self.pause_month_cap()
            if client is not None and client.status == SubStatus.EXPIRED:
                return PauseCredit(new_kind, bal, bal, cap, "expired")
            if client is not None and client.grace_used:
                return PauseCredit(new_kind, bal, bal, cap, "grace")
        elif new_kind == PeriodKind.NEVER:
            return PauseCredit(new_kind, bal, 0, 0, "none")
        else:
            return PauseCredit(new_kind, bal, bal, 0, "none")
        if bal >= cap:
            return PauseCredit(new_kind, bal, bal, cap, "cap")
        return PauseCredit(new_kind, bal, min(bal + add, cap), cap, "credited")

    def pause_available_days(self, client_id: int) -> int:
        """Сколько дней приостановки пользователь может взять ПРЯМО СЕЙЧАС — его счёт.
        Бессрочной паузе нечего останавливать — 0. Ни остатком подписки, ни
        «максимумом за один вход» не ограничиваем."""
        client = self.db.get_client(client_id)
        if client is None or not client.period_end:
            return 0
        return max(0, int(client.pause_balance_days))
