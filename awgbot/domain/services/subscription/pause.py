"""pause.py — приостановка подписки («в отпуск»): вход пользователя и админа,
выход (руками, по сроку, по коду из письма), авто-выход планировщиком и
каскад PAUSED на устройства с уведомлениями держателям."""
from __future__ import annotations

import datetime

from awgbot.util import timeutil
from awgbot.infra import email_resume
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core import models
from awgbot.core.enums import PauseMode, FriendStatus
from awgbot.domain.services.types import Notification, SECONDS_PER_DAY
from awgbot.domain.services.base import _e


# ── Тексты приостановки подписки ─────────────────────────────────────────────

def _pause_auto_ended_client(actual_days: int, new_end) -> str:
    return (f"▶️ Пауза завершена автоматически (истёк максимальный срок). "
            f"Учтено {actual_days} дн. паузы, подписка активна до "
            f"{timeutil.fmt_end_ui(new_end)}")


def _pause_friend_started(device_name: str) -> str:
    return (f"⏸️ Доступ к устройству «{_e(device_name)}» приостановлен владельцем "
            "(подписка на паузе)")


def _pause_friend_ended(device_name: str) -> str:
    return f"▶️ Доступ к устройству «{_e(device_name)}» снова активен"


class PauseMixin:
    # ── Приостановка подписки («в отпуск») ───────────────────────────────────

    def enter_pause(self, client_id: int, days: int = None):
        """Самоблок пользователя (mode=user). Резервирует `days` дней вперёд
        (сдвигает period_end), ставит PAUSED пользователю и каскадом устройствам.
        days=None — берёт весь доступный максимум (обратная совместимость);
        иначе резервирует ровно min(days, доступное). Возвращает
        (ok, reserved_days, notifications)."""
        # чтение и запись под одним замком: продление или вторая пауза из другого
        # потока ждут, а не затираются прочитанным раньше
        with self.db.transaction(immediate=True):
            got = self._enter_pause_tx(client_id, days)
        if got is None:
            return (False, 0, [], None)
        reserved, code = got
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_set_block(dev.id, DeviceBlock.PAUSED)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _pause_friend_started(dev.name)))
        return (True, reserved, notes, code)

    def _enter_pause_tx(self, client_id: int, days):
        """Запись паузы пользователя; None — паузу взять нельзя, иначе (reserved, code)."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            return None
        if client.pause_active_since or int(client.block_reason) & int(ClientBlock.PAUSED):
            return None
        avail = self.pause_available_days(client_id)
        reserved = avail if days is None else max(0, min(int(days), avail))
        if reserved <= 0:
            return None
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
            self.db.update_client_fields(client_id, period_end=timeutil.to_iso(new_end))
            # бит — в SQL, не маской из прочитанного: чужой бит, поставленный после чтения, цел
            self.db.client_mask_apply(client_id, or_bits=int(ClientBlock.PAUSED))
        return reserved, code

    def enter_admin_pause(self, client_id: int, days: int):
        """Приостановка подписки при АДМИНСКОМ блоке пользователя. days>0 — срочная
        (admin_fixed: +days вперёд, авто-выход по сроку); days==0 — бессрочная
        (admin_open: period_end→NULL temp, снимок в pause_saved_end, пересчёт при
        снятии). Без лимита 28 и без привязки к годовой. НЕ ставит сам блок-бит
        (это делает вызывающий block_client_manual) и НЕ шлёт уведомлений
        (уведомляет вызывающий по notify-флагу). Возвращает reserved (0 у open)."""
        # чтение и запись под одним замком: срок, продлённый из другого потока между
        # чтением и записью, не затирается прочитанным раньше
        with self.db.transaction(immediate=True):
            client = self.db.get_client(client_id)
            if client is None or client.is_service or client.pause_active_since:
                return 0
            now = timeutil.now()
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

    def exit_pause(self, client_id: int, *, auto: bool):
        """Выход из приостановки (любой режим). auto=True — по истечении срока
        (только user/admin_fixed). Пересчитывает фактическую длительность, правит
        period_end по режиму, снимает PAUSED-каскад. Возвращает
        (ok, actual_days, new_end, notifications)."""
        # чтение и запись под одним замком (см. enter_pause)
        with self.db.transaction(immediate=True):
            got = self._exit_pause_tx(client_id, auto)
        if got is None:
            return (False, 0, None, [])
        client, mode, actual, new_end = got
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
        # пользователю — только при АВТО-выходе самоблока пользователя (по макс. сроку)
        if auto and mode == PauseMode.USER and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _pause_auto_ended_client(actual, new_end)))
        return (True, actual, new_end, notes)

    def _exit_pause_tx(self, client_id: int, auto: bool):
        """Запись выхода из паузы; None — паузы нет, иначе (client, mode, actual, new_end)."""
        client = self.db.get_client(client_id)
        if client is None or not client.pause_active_since:
            return None
        mode = client.pause_mode or PauseMode.USER
        since = timeutil.parse_iso(client.pause_active_since)
        now = timeutil.now()
        actual = timeutil.ceil_days(timeutil.elapsed(since, now))
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
            self.db.update_client_fields(client_id, period_end=new_end_iso)
            # бит — в SQL, не маской из прочитанного: чужой бит, поставленный после чтения, цел
            self.db.client_mask_apply(client_id, and_bits=~int(ClientBlock.PAUSED))
        return client, mode, actual, new_end

    def resume_by_email_code(self, code: str):
        """Аварийный email-выход: найти пользователя с активной паузой и данным
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
                f"▶️ Пауза снята по коду из письма. Использовано {actual} дн."))
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
            if timeutil.elapsed(since, now) >= reserved * SECONDS_PER_DAY:
                ok, _, _, n = self.exit_pause(client.id, auto=True)
                if ok:
                    notes += n
        return notes
