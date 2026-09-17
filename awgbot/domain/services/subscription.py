"""
subscription.py — подписка: продление, правка дат, +N дней объявлением,
отсрочка, приостановка и счёт дней паузы, миграция балансов, чистка истории.
"""
from __future__ import annotations

import datetime
import logging

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import email_resume
from awgbot.core.blocks import DeviceBlock, ClientBlock, DEVICE_TRAFFIC_ANY
from awgbot.core import models
from awgbot.core.enums import SubStatus, PauseMode, PeriodKind, FriendStatus
from awgbot.domain.services.types import (
    DaysExtension, ExtendResult, Notification, PauseCredit, SECONDS_PER_DAY, ServiceError,
)
from awgbot.domain.services.base import _e
from awgbot.domain.services.blocks import _friend_unblocked_text
from awgbot.domain.services.traffic import _TXT_EXPIRED_CLIENT


log = logging.getLogger("awgbot.services")


_TXT_EXTENDED = "Подписка продлена до {end}"
_TXT_EXTENDED_FOREVER = "Подписка теперь бессрочная 🎉"

# ── Тексты приостановки подписки ─────────────────────────────────────────────

def _pause_auto_ended_client(actual_days: int, new_end) -> str:
    return (f"▶️ Приостановка завершена автоматически (истёк максимальный срок). "
            f"Учтено {actual_days} дн. паузы, подписка активна до "
            f"{timeutil.fmt_dt(new_end)}.")


def _pause_friend_started(device_name: str) -> str:
    return (f"⏸ Доступ к устройству «{_e(device_name)}» приостановлен владельцем "
            "(подписка на паузе).")


def _pause_friend_ended(device_name: str) -> str:
    return f"▶️ Доступ к устройству «{_e(device_name)}» снова активен."


class SubscriptionMixin:
    # ── Продление ────────────────────────────────────────────────────────────

    def remaining_for(self, client_id: int) -> int:
        """Секунд до конца текущего периода (для диалога сохранения остатка)."""
        client = self.db.get_client(client_id)
        if client is None or not client.period_end:
            return 0
        end = timeutil.parse_iso(client.period_end)
        return max(0, timeutil.remaining_seconds(end))

    def extend_period(self, client_id: int, period_kind: str, keep_remainder: bool) -> ExtendResult:
        """Поток 3: закрыть текущий период, создать новый (+остаток если keep),
        обнулить ПЕРИОДНЫЙ трафик, снять блокировку если был истёкшим, уведомить."""
        if period_kind not in config.PERIOD_CHOICES:
            raise ServiceError(f"Неизвестный период: {period_kind}")
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")

        # Клиент на паузе (любой режим) → корректно закрыть паузу ДО продления:
        # exit_pause пересчитает period_end по факту и снимет PAUSED-каскад с
        # устройств. Без этого archive_pause ниже снёс бы строку паузы, а биты
        # PAUSED остались бы навечно (снять их через UI больше нечем).
        pause_exit_notes: list[Notification] = []
        if client.pause_active_since:
            _, _, _, pause_exit_notes = self.exit_pause(client_id, auto=False)
            client = self.db.get_client(client_id)

        extra = self.remaining_for(client_id) if keep_remainder else 0
        pause_credit = self._pause_credit(client, period_kind)   # по СТАРОМУ состоянию
        new_start = timeutil.now()
        never = period_kind == PeriodKind.NEVER
        # «долг» отсрочки вычитается из нового периода (никогда — из «never»:
        # безлимитному вычитать не из чего). Фильтр периодов в UI гарантирует, что
        # выбранный период длиннее долга, так что в минус не уходим.
        pending_cut = 0 if never else int(client.grace_pending_cut)
        new_end = None if never else timeutil.add_period(
            new_start, period_kind, extra_seconds=extra - pending_cut)

        # периодный трафик обнуляем; месячный НЕ трогаем (свой цикл)
        self.db.reset_period_traffic(client_id)

        # возврат из истёкшего → снять причину EXPIRY со всех устройств. Именно
        # бит, не «разблокировать всё»: устройство может быть заблокировано ещё и
        # по трафику — эту причину продление подписки снимать не должно.
        friend_unblock_notes = []
        if client.status == SubStatus.EXPIRED:
            self._client_clear_block(client_id, ClientBlock.EXPIRY)
            for dev in self.db.list_devices(client_id):
                had_traffic = int(dev.block_reason) & int(DEVICE_TRAFFIC_ANY)
                self._device_clear_block(dev.id, DeviceBlock.EXPIRY)
                # уведомляем друга только если доступ РЕАЛЬНО вернулся (не остался
                # заблокирован по трафику)
                if (not had_traffic and dev.friend_status == FriendStatus.ACTIVE
                        and dev.friend_tg_id):
                    friend_unblock_notes.append(Notification(
                        dev.friend_tg_id, _friend_unblocked_text(dev.name)))

        # аудит перед сменой периода: снимок закрываемой подписки + закрытие
        # эпизодов grace/pause со сбросом (новый период — права заново)
        self.db.archive_subscription(client_id, "renewed")
        self.db.archive_grace(client_id, "new_period")   # снесёт строку, если была
        self.db.archive_pause(client_id, "new_period")   # снимок эпизода + сброс used_days
        self.db.set_pause_balance(client_id, pause_credit.after)   # счёт — заново (0 у бессрочной)
        self.db.update_client_fields(
            client_id,
            period_start=timeutil.to_iso(new_start),
            period_end=timeutil.to_iso(new_end) if new_end else None,
            period_kind=period_kind,
            status=SubStatus.ACTIVE,
        )
        self.db.reset_notified(client_id)                # новый период — пороги заново

        notifications = pause_exit_notes + friend_unblock_notes
        if client.tg_id:
            msg = (_TXT_EXTENDED_FOREVER if new_end is None
                   else _TXT_EXTENDED.format(end=timeutil.fmt_dt(new_end)))
            from awgbot.bot import texts                   # ленивый, как в соседних миксинах
            line = texts.pause_credit_line(pause_credit)   # что стало со счётом паузы
            if line:
                msg += f".\n{line}" if not msg.endswith(".") else f"\n{line}"
            notifications.append(Notification(client.tg_id, msg))
        return ExtendResult(new_end=new_end, notifications=notifications, pause=pause_credit)

    def set_subscription_dates(self, client_id: int, new_start, new_end):
        """Прямая правка дат подписки админом (не продление): пишем ровно
        заданные даты. Статус пересчитываем по new_end относительно now:
        будущее → active, прошлое → expired. При СМЕНЕ статуса приводим в
        порядок блокировки устройств (как watchdog/extend), иначе получим
        рассинхрон «подписка активна, а устройства заблокированы по EXPIRY»
        (или наоборот). period_kind и pause НЕ трогаем. Возвращает
        (start, end, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            raise ServiceError("Профиль не найден")
        was_expired = client.status == SubStatus.EXPIRED
        # new_end=None → бессрочная (никогда не истекает) → всегда active
        now_expired = new_end is not None and new_end <= timeutil.now()
        status = SubStatus.EXPIRED if now_expired else SubStatus.ACTIVE
        self.db.update_client_fields(
            client_id,
            period_start=timeutil.to_iso(new_start),
            period_end=timeutil.to_iso(new_end) if new_end else None,
            status=status,
        )
        self.db.reset_notified(client_id)     # период сменился — пороги истечения заново

        notifications: list[Notification] = []
        if was_expired and not now_expired:
            # реактивация: снять причину EXPIRY (но не трогать блок по трафику)
            self._client_clear_block(client_id, ClientBlock.EXPIRY)
            for dev in self.db.list_devices(client_id):
                had_traffic = int(dev.block_reason) & int(DEVICE_TRAFFIC_ANY)
                self._device_clear_block(dev.id, DeviceBlock.EXPIRY)
                if (not had_traffic and dev.friend_status == FriendStatus.ACTIVE
                        and dev.friend_tg_id):
                    notifications.append(Notification(
                        dev.friend_tg_id, _friend_unblocked_text(dev.name)))
            if client.tg_id:
                msg = (_TXT_EXTENDED_FOREVER if new_end is None
                       else _TXT_EXTENDED.format(end=timeutil.fmt_dt(new_end)))
                notifications.append(Notification(client.tg_id, msg))
        elif not was_expired and now_expired:
            # админ поставил прошлую дату → истекло: заблокировать как watchdog
            fresh = self.db.get_client(client_id)
            notifications.extend(self._block_client(fresh))
            if client.tg_id:
                notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT))
        return new_start, new_end, notifications


    # ── объявление с продлением: правая граница периода уезжает на N дней ────

    def extension_plan(self, client_ids, days: int) -> list["DaysExtension"]:
        """Что даст продление на days дней каждому профилю — без записи, для
        превью. Активная — конец + N; истёкшая — сегодня + N (плюшка за простой
        или «неделька на слюнки» — от старого конца она была бы пустой);
        открытая админ-пауза — сохранённый конец (period_end на ней пуст);
        бессрочная — ничего. Порядок — по имени профиля."""
        now = timeutil.now()
        delta = datetime.timedelta(days=int(days))
        out: list[DaysExtension] = []
        for cid in sorted({int(c) for c in (client_ids or ())}):
            c = self.db.get_client(cid)
            if c is None or c.is_service:
                continue
            end_iso = c.effective_period_end
            if not end_iso:
                out.append(DaysExtension(c))
                continue
            old = timeutil.parse_iso(end_iso)
            if c.status == SubStatus.EXPIRED or old <= now:
                out.append(DaysExtension(c, old, now + delta, from_now=True))
            else:
                out.append(DaysExtension(c, old, old + delta))
        out.sort(key=lambda e: e.client.name.lower())
        return out

    def extend_days(self, client_ids, days: int) -> tuple[list["DaysExtension"], list["Notification"]]:
        """Применить extension_plan — одной транзакцией. Начало и тип периода,
        периодный трафик, долг отсрочки не трогаются: только правая граница
        уезжает на N дней. Истёкшим снимается EXPIRY (как при правке дат),
        держателям их устройств — «доступ вернулся»; штатное «Подписка
        продлена до …» владельцу глушится — эту роль играет шапка объявления."""
        plan = self.extension_plan(client_ids, days)
        notes: list[Notification] = []
        with self.db.transaction():
            for e in plan:
                if e.unlimited:
                    continue
                c = e.client
                if c.pause_mode == PauseMode.ADMIN_OPEN and c.pause_saved_end:
                    self.db.update_client_fields(c.id, pause_saved_end=timeutil.to_iso(e.new_end))
                    continue
                start = timeutil.parse_iso(c.period_start) if c.period_start else timeutil.now()
                _, _, ns = self.set_subscription_dates(c.id, start, e.new_end)
                notes.extend(n for n in ns if n.tg_id != c.tg_id)
        return plan, notes

    def activate_grace(self, client_id: int, days: int):
        """Клиент сам продлевает годовую подписку на `days` дней (один раз за
        период). Возвращает (ok, end|None): ok=False если предложение протухло
        (истёк / уже использовано / не годовой). Долг фиксируем снимком в секундах
        — вычтется при следующем продлении."""
        client = self.db.get_client(client_id)
        if client is None:
            return (False, None)
        if (client.status == SubStatus.EXPIRED or client.grace_used
                or client.period_kind != PeriodKind.YEAR or not client.period_end):
            return (False, None)
        end = timeutil.parse_iso(client.period_end)
        new_end = end + datetime.timedelta(days=days)
        self.db.update_client_fields(
            client_id,
            period_end=timeutil.to_iso(new_end),
            grace_used=1,
            grace_pending_cut=days * SECONDS_PER_DAY,
        )
        return (True, new_end)

    # ── Приостановка подписки («в отпуск») ───────────────────────────────────

    # ── счёт дней паузы (docs: README «Приостановка подписки») ──────────────
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
        """Сколько дней приостановки клиент может взять ПРЯМО СЕЙЧАС — его счёт.
        Бессрочной паузе нечего останавливать — 0. Ни остатком подписки, ни
        «максимумом за один вход» не ограничиваем."""
        client = self.db.get_client(client_id)
        if client is None or not client.period_end:
            return 0
        return max(0, int(client.pause_balance_days))

    _PAUSE_BALANCE_MIGRATED = "pause_balance_migrated"

    def migrate_pause_balances(self) -> int:
        """Разово после обновления на счёт паузы: годовым — остаток старого
        лимита (28 − использовано − резерв текущей паузы), ежемесячным — по
        числу оплаченных месяцев (создание + продления с месячного, все
        считаются своевременными) в пределах максимума, минус то же. Возвращает
        число профилей, получивших счёт."""
        if self.db.get_state(self._PAUSE_BALANCE_MIGRATED) == "1":
            return 0
        n = 0
        with self.db.transaction():
            for c in self.db.list_clients(include_service=False):
                spent = int(c.pause_used_days)
                if c.is_paused and c.pause_mode == PauseMode.USER:
                    spent += int(c.pause_reserved_days)
                if c.period_kind == PeriodKind.YEAR:
                    bal = self.pause_year_days() - spent
                elif c.period_kind == PeriodKind.MONTH:
                    months = 1 + self.db.monthly_renewals(c.id)
                    bal = min(months * self.pause_month_days(), self.pause_month_cap()) - spent
                else:
                    continue
                if bal > 0:
                    self.db.set_pause_balance(c.id, bal)
                    n += 1
            self.db.set_state(self._PAUSE_BALANCE_MIGRATED, "1")
        if n:
            log.info("пауза: счёт дней выдан %d профилям", n)
        return n

    def enter_pause(self, client_id: int, days: int = None):
        """Клиентский самоблок (mode=user). Резервирует `days` дней вперёд
        (сдвигает period_end), ставит PAUSED клиенту и каскадом устройствам.
        days=None — берёт весь доступный максимум (обратная совместимость);
        иначе резервирует ровно min(days, доступное). Возвращает
        (ok, reserved_days, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            return (False, 0, [], None)
        if client.pause_active_since or int(client.block_reason) & int(ClientBlock.PAUSED):
            return (False, 0, [], None)
        avail = self.pause_available_days(client_id)
        reserved = avail if days is None else max(0, min(int(days), avail))
        if reserved <= 0:
            return (False, 0, [], None)
        now = timeutil.now()
        end = timeutil.parse_iso(client.period_end)
        new_end = end + datetime.timedelta(days=reserved)
        # одноразовый код email-выхода — генерим всегда при входе (даже если
        # email-выход выключен: код безвреден, а включат фичу позже — сработает).
        code = email_resume.generate_code()
        # процесс + сопутствующие поля — атомарно (вложенные _tx коммитятся разом)
        with self.db.transaction():
            self.db.save_pause(client_id, models.PauseState(
                active_since=timeutil.to_iso(now), reserved_days=reserved,
                mode=PauseMode.USER,
                used_days=int(client.pause_used_days),   # накопленное за период
                balance_days=int(client.pause_balance_days) - reserved,   # резерв списан
                resume_code=code))
            self.db.update_client_fields(
                client_id,
                period_end=timeutil.to_iso(new_end),
                block_reason=int(client.block_reason) | int(ClientBlock.PAUSED))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_set_block(dev.id, DeviceBlock.PAUSED)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _pause_friend_started(dev.name)))
        return (True, reserved, notes, code)

    def enter_admin_pause(self, client_id: int, days: int):
        """Приостановка подписки при АДМИНСКОМ блоке клиента. days>0 — срочная
        (admin_fixed: +days вперёд, авто-выход по сроку); days==0 — бессрочная
        (admin_open: period_end→NULL temp, снимок в pause_saved_end, пересчёт при
        снятии). Без лимита 28 и без привязки к годовой. НЕ ставит сам блок-бит
        (это делает вызывающий block_client_manual) и НЕ шлёт уведомлений
        (уведомляет вызывающий по notify-флагу). Возвращает reserved (0 у open)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service or client.pause_active_since:
            return 0
        now = timeutil.now()
        with self.db.transaction():
            if days > 0:
                # срочная: сдвигаем период вперёд, как самоблок
                self.db.save_pause(client_id, models.PauseState(
                    active_since=timeutil.to_iso(now), reserved_days=days,
                    mode=PauseMode.ADMIN_FIXED,
                    used_days=int(client.pause_used_days),
                    balance_days=int(client.pause_balance_days)))
                if client.period_end:
                    end = timeutil.parse_iso(client.period_end)
                    self.db.update_client_fields(client_id, period_end=timeutil.to_iso(
                        end + datetime.timedelta(days=days)))
            else:
                # бессрочная: подписка temp-бессрочная, конец сохраняем для пересчёта
                self.db.save_pause(client_id, models.PauseState(
                    active_since=timeutil.to_iso(now), reserved_days=0,
                    mode=PauseMode.ADMIN_OPEN, saved_end=client.period_end,
                    used_days=int(client.pause_used_days),
                    balance_days=int(client.pause_balance_days)))
                self.db.update_client_fields(client_id, period_end=None)
        return days

    def preview_exit_pause(self, client_id: int):
        """Read-only предпросчёт для диалога подтверждения возобновления:
        сколько дней пауза УЖЕ длилась (спишется при выходе) против
        зарезервированных. Ничего не меняет в БД. Возвращает (actual, reserved)
        или None, если клиент не на паузе."""
        client = self.db.get_client(client_id)
        if client is None or not client.pause_active_since:
            return None
        mode = client.pause_mode or PauseMode.USER
        since = timeutil.parse_iso(client.pause_active_since)
        now = timeutil.now()
        actual = timeutil.ceil_days((now - since).total_seconds())
        if mode == PauseMode.ADMIN_OPEN:
            return actual, 0          # бессрочная админ-пауза — резерва вперёд не было
        reserved = int(client.pause_reserved_days)
        return max(0, min(actual, reserved)), reserved

    def exit_pause(self, client_id: int, *, auto: bool):
        """Выход из приостановки (любой режим). auto=True — по истечении срока
        (только user/admin_fixed). Пересчитывает фактическую длительность, правит
        period_end по режиму, снимает PAUSED-каскад. Возвращает
        (ok, actual_days, new_end, notifications)."""
        client = self.db.get_client(client_id)
        if client is None or not client.pause_active_since:
            return (False, 0, None, [])
        mode = client.pause_mode or PauseMode.USER
        since = timeutil.parse_iso(client.pause_active_since)
        now = timeutil.now()
        actual = timeutil.ceil_days((now - since).total_seconds())
        if mode == PauseMode.ADMIN_OPEN:
            # temp-бессрочная: восстанавливаем сохранённый конец + фактические дни
            saved = client.pause_saved_end
            if saved:
                new_end = timeutil.parse_iso(saved) + datetime.timedelta(days=actual)
                new_end_iso = timeutil.to_iso(new_end)
            else:
                new_end, new_end_iso = None, None       # была бессрочной и осталась
            used_add = 0
        else:
            # user / admin_fixed: резерв был добавлен вперёд, откатываем неисп.
            reserved = int(client.pause_reserved_days)
            actual = max(0, min(actual, reserved))
            new_end = None
            new_end_iso = client.period_end
            if client.period_end:
                end = timeutil.parse_iso(client.period_end)
                new_end = end - datetime.timedelta(days=reserved - actual)
                new_end_iso = timeutil.to_iso(new_end)
            used_add = actual if mode == PauseMode.USER else 0  # счёт списывает только user
        # неиспользованный резерв — обратно на счёт (только свой самоблок)
        balance = int(client.pause_balance_days)
        if mode == PauseMode.USER:
            balance += int(client.pause_reserved_days) - actual
        # атомарно: снимок эпизода в аудит + гашение активности паузы (used_days
        # периода сохраняем «спящим») + правка периода/блока
        with self.db.transaction():
            self.db.snapshot_pause(client_id, "auto" if auto else "manual")
            self.db.save_pause(client_id, models.PauseState(
                active_since=None, reserved_days=0, mode=None, saved_end=None,
                used_days=int(client.pause_used_days) + used_add,
                balance_days=max(0, balance)))
            self.db.update_client_fields(
                client_id,
                period_end=new_end_iso,
                block_reason=int(client.block_reason) & ~int(ClientBlock.PAUSED))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_clear_block(dev.id, DeviceBlock.PAUSED)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                # «доступ снова активен» — только если устройство РЕАЛЬНО
                # разблокировано: при выходе из паузы могут оставаться другие
                # биты (админ-блок при admin_fixed, лимит трафика) — тогда
                # доступ не вернулся и радовать друга рано. О снятии этих битов
                # уведомит их собственный поток (unblock_client_manual и т.п.).
                fresh = self.db.get_device(dev.id)
                if fresh is not None and int(fresh.block_reason) == 0:
                    notes.append(Notification(dev.friend_tg_id,
                                 _pause_friend_ended(dev.name)))
        # клиенту — только при АВТО-выходе клиентского самоблока (по макс. сроку)
        if auto and mode == PauseMode.USER and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _pause_auto_ended_client(actual, new_end)))
        return (True, actual, new_end, notes)

    def resume_by_email_code(self, code: str):
        """Аварийный email-выход: найти клиента с активной паузой и данным
        одноразовым кодом, снять паузу (тот же exit_pause). Возвращает
        (ok, notifications): ok=False если код не найден/не на паузе (тогда
        вызывающий молчит — письмо помечается прочитанным без ответа).
        Код одноразовый: exit_pause обнуляет resume_code (новый PauseState без
        кода), повторное письмо с тем же кодом уже не сматчится."""
        cid = self.db.find_client_by_resume_code((code or "").strip())
        if cid is None:
            return (False, [])
        client = self.db.get_client(cid)
        if client is None or not client.is_paused or client.pause_mode != PauseMode.USER:
            return (False, [])
        ok, actual, new_end, notes = self.exit_pause(cid, auto=False)
        if not ok:
            return (False, [])
        if client.tg_id:
            notes.append(Notification(
                client.tg_id,
                f"▶️ Приостановка снята по коду из письма. Использовано {actual} дн."))
        return (True, notes)

    def check_pauses(self) -> list["Notification"]:
        """Scheduler: авто-выход из СРОЧНЫХ приостановок (user/admin_fixed), у
        которых истёк зарезервированный срок. admin_open (бессрочные) — не трогаем,
        их снимает только админ."""
        notes: list[Notification] = []
        now = timeutil.now()
        for client in self.db.list_clients(include_service=False, paused_only=True):
            if not client.pause_active_since:
                continue
            mode = client.pause_mode or PauseMode.USER
            if mode == PauseMode.ADMIN_OPEN:
                continue
            reserved = int(client.pause_reserved_days)
            since = timeutil.parse_iso(client.pause_active_since)
            if (now - since).total_seconds() >= reserved * SECONDS_PER_DAY:
                ok, _, _, n = self.exit_pause(client.id, auto=True)
                if ok:
                    notes += n
        return notes

    def purge_old_history(self) -> dict:
        """Scheduler: удалить историю старше ретеншна (relativedelta лет из конфига).
        Считаем cutoff по календарю (високосные корректно), удаляем батчами по всем
        _histories. Возвращает {таблица: удалено} для лога."""
        from dateutil.relativedelta import relativedelta
        cutoff = timeutil.now() - relativedelta(years=config.HISTORY_RETENTION_YEARS)
        return self.db.purge_histories(timeutil.to_iso(cutoff),
                                       config.HISTORY_PURGE_BATCH_SIZE)
