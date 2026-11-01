"""period.py — продление периода, прямая правка дат, +N дней объявлением и
отсрочка годовой: всё, что двигает границы периода подписки (пауза — в pause)."""
from __future__ import annotations

import datetime

from awgbot.core import config
from awgbot.util import timeutil
from awgbot.core.blocks import DeviceBlock, ClientBlock, DEVICE_TRAFFIC_ANY
from awgbot.core.enums import SubStatus, PauseMode, PeriodKind, FriendStatus
from awgbot.domain.services.types import (
    DaysExtension, ExtendResult, Notification, SECONDS_PER_DAY, ServiceError,
)
from awgbot.domain.services.blocks import _friend_unblocked_text
from awgbot.domain.services.traffic import _TXT_EXPIRED_CLIENT
from awgbot.domain.notices.subscription import pause_credit_line


_TXT_EXTENDED = "✅ Подписка продлена до {end}"
_TXT_EXTENDED_FOREVER = "Подписка теперь бессрочная 🎉"


class PeriodMixin:
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
            raise ServiceError("Пользователь не найден")

        # Пользователь на паузе (любой режим) → корректно закрыть паузу ДО продления:
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
                   else _TXT_EXTENDED.format(end=timeutil.fmt_end_ui(new_end)))
            line = pause_credit_line(pause_credit)         # что стало со счётом паузы
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
        now_expired = new_end is not None and timeutil.elapsed(new_end) >= 0
        status = SubStatus.EXPIRED if now_expired else SubStatus.ACTIVE
        end_iso = timeutil.to_iso(new_end) if new_end else None
        if client.pause_mode == PauseMode.ADMIN_OPEN and client.pause_active_since:
            # Открытая админ-пауза: period_end пуст (временно бессрочная), а
            # настоящий конец — сохранённый; его и правим, как продление на N
            # дней. Запись в period_end выход из паузы затёр бы сохранённым.
            self.db.update_client_fields(client_id, period_start=timeutil.to_iso(new_start),
                                         pause_saved_end=end_iso, status=status)
        else:
            self.db.update_client_fields(client_id, period_start=timeutil.to_iso(new_start),
                                         period_end=end_iso, status=status)
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
                       else _TXT_EXTENDED.format(end=timeutil.fmt_end_ui(new_end)))
                notifications.append(Notification(client.tg_id, msg))
        elif not was_expired and now_expired:
            # админ поставил прошлую дату → истекло: заблокировать как watchdog
            fresh = self.db.get_client(client_id)
            notifications.extend(self._block_client(fresh))
            if client.tg_id:
                notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT, action=("sub", 0)))
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
            if c.status == SubStatus.EXPIRED or timeutil.elapsed(old, now) >= 0:
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
        """Пользователь сам продлевает годовую подписку на `days` дней (один раз за
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
