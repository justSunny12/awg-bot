"""
services.py — бизнес-логика: склейка db + awg + configgen.

Слой синхронный (db и awg блокирующие). Async-слой (хендлеры, планировщик)
вызывает эти методы через asyncio.to_thread, чтобы docker exec не морозил loop.
Поэтому services НЕ шлют сообщения сами, а возвращают список Notification —
их рассылает async-слой.

Здесь живут потоки, спроектированные ранее: создание клиента с инвайтом,
активация, добавление/удаление устройства с откатом, продление с остатком,
опрос трафика, проверка сроков, реконсиляция состава пиров и блокировок,
реставрация app-устройства.
"""

from __future__ import annotations

import datetime
import secrets
import string
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import awg
from awgbot.infra import email_resume
from awgbot.infra import updates
from awgbot.domain import configgen
from awgbot.core.blocks import DeviceBlock, ClientBlock, DEVICE_TRAFFIC_ANY
from awgbot.core import models
from awgbot.core.enums import SubStatus, ActivationStatus, PauseMode, PeriodKind, FriendStatus


# Байт в гигабайте — физическая константа (не настройка), потому в коде, не в
# config. Лимиты храним в байтах, вводим/показываем пользователю в ГБ.
# У texts.py есть свой приватный дубль (_BYTES_PER_GB) — намеренно, см. там.
BYTES_PER_GB = 1024 ** 3

# Секунд в сутках — физическая константа. Резервы/сроки храним в днях, но «долг»
# отсрочки и длительность паузы считаем в секундах (унифицировано с ISO-датами).
SECONDS_PER_DAY = 86400


# ─────────────────────────────────────────────────────────────────────────────
# Исключения и результаты
# ─────────────────────────────────────────────────────────────────────────────

class ServiceError(Exception):
    """Ошибка бизнес-операции (показывается пользователю)."""


class LimitReached(ServiceError):
    pass


@dataclass
class Notification:
    tg_id: int
    text: str
    reply_markup: object = None        # опциональная inline-клавиатура
    grace_offer_client_id: int = 0     # >0 → прикрепить кнопку отсрочки (делает scheduler)
    force_sound: bool = False          # True → слать со звуком даже в тихие часы


@dataclass
class ClientCreated:
    client_id: int
    invite_code: str
    period_end: object            # datetime


@dataclass
class ActivationResult:
    ok: bool
    reason: str = ""              # invalid | already_has_access | ok
    client: Optional[object] = None


@dataclass
class DeviceCreated:
    device_id: int
    address: str
    vpn: str
    conf: str


@dataclass
class FriendActivation:
    ok: bool
    reason: str = ""             # invalid | already_user | ok
    device_id: Optional[int] = None
    device_name: Optional[str] = None


@dataclass
class ExtendResult:
    new_end: object              # datetime
    notifications: list = field(default_factory=list)


# Тексты уведомлений (сухие, без слов про оплату — ТЗ 6.5).
_TXT_EXTENDED = "Подписка продлена до {end}"
_TXT_EXTENDED_FOREVER = "Подписка теперь бессрочная 🎉"
_TXT_EXPIRED_CLIENT = "Срок действия подписки истёк. Доступ приостановлен."
_TXT_EXPIRING_CLIENT = "Внимание: подписка истекает через {label}."
_TXT_EXPIRING_ADMIN = "Клиент «{name}»: подписка истекает через {label}."
_TXT_EXPIRED_ADMIN = "Клиент «{name}»: подписка истекла, доступ приостановлен."
_TXT_NEW_APP_DEVICE = ("🆕 Обнаружено новое устройство из приложения: «{name}» ({ip}). "
                       "Его можно привязать к клиенту в разделе «Устройства без клиента».")
_TXT_APP_DEVICE_GONE = "Устройство «{name}» клиента «{client}» удалено из приложения."
_TXT_FRIEND_DEVICE_GONE = ("Устройство, которым ты управлял, удалено владельцем — "
                           "доступ по нему больше не работает.")


def _e(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _friend_blocked_text(device_name: str) -> str:
    return (f"🔴 Доступ к устройству «{_e(device_name)}» приостановлен: "
            "у владельца доступа закончилась подписка.")


def _friend_unblocked_text(device_name: str) -> str:
    return f"🟢 Доступ к устройству «{_e(device_name)}» снова активен."


# ── Тексты ручных блокировок (админ/клиент) ──────────────────────────────────

def _manual_device_blocked_client(name: str) -> str:
    return f"🛑 Устройство «{_e(name)}» заблокировано."


def _manual_device_unblocked_client(name: str) -> str:
    return f"🟢 Устройство «{_e(name)}» разблокировано."


# Клиенту сообщаем о его же блокировке — имя не подставляем (адресат и есть
# субъект), поэтому текст без параметров.
def _manual_client_blocked() -> str:
    return "🛑 Твой доступ приостановлен администратором."


def _manual_client_unblocked() -> str:
    return "🟢 Твой доступ восстановлен."


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


# ── Тексты месячного сброса лимитов ──────────────────────────────────────────

def _gb_limit(num_bytes: int) -> str:
    """Лимит в целых ГБ для уведомлений: «500 ГБ». 0 = безлимит (в перечислениях
    такие не показываем, но на всякий)."""
    return f"{int(round(num_bytes / BYTES_PER_GB))} ГБ"


def _reset_client_text(total_limit: int, device_lines: list[str]) -> str:
    """Профилю: сброс + доступный лимит профиля + список лимитных устройств.
    total_limit>0 — показываем строку профиля; device_lines — уже отфильтрованы
    (только лимитные)."""
    parts = ["Начался новый месяц — лимит расхода по твоему профилю сброшен 🙂"]
    if total_limit > 0:
        parts.append(f"Доступный лимит на текущий месяц: {_gb_limit(total_limit)}")
    if device_lines:
        parts.append("\nДоступные лимиты по устройствам:\n" + "\n".join(device_lines))
    return "\n".join(parts)


def _reset_friend_text(device_lines: list[str]) -> str:
    """Другу: сброс + список ЕГО лимитных устройств."""
    return ("Начался новый месяц — лимиты расхода по твоим устройствам сброшены 🙂\n"
            "Доступные лимиты на текущий месяц:\n" + "\n".join(device_lines))


# ── Тексты уведомлений о потреблении (ТЗ 7-8) ────────────────────────────────


def _dev_warn_text(name: str, pct: int) -> str:
    return (f"⚠️ Устройство «{_e(name)}»: израсходовано ~{pct}% месячного лимита "
            "потребления.")


def _dev_over_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}»: месячный лимит потребления исчерпан. "
            f"Доступ приостановлен до {until} или пока лимит не увеличат.")


def _friend_dev_over_host_text(name: str, until: str) -> str:
    return (f"🔴 Устройство «{_e(name)}» (передано другу): лимит потребления "
            f"исчерпан, доступ приостановлен до {until}.")


def _cli_warn_text(pct: int) -> str:
    return f"⚠️ Израсходовано ~{pct}% месячного лимита потребления по всем устройствам."


def _cli_bonus_text(bonus_gb: int, until: str) -> str:
    return (f"📈 Месячный лимит потребления исчерпан. Тебе добавлено "
            f"{bonus_gb} ГБ до конца месяца — это разово, больше в этом месяце "
            "квота не увеличится. Лимит обновится "
            f"{until}.")


def _cli_bonus_admin_text(name: str, bonus_gb: int) -> str:
    return (f"📈 Клиенту «{_e(name)}» исчерпан лимит — выдано {bonus_gb} ГБ "
            "до конца месяца (разово).")


def _cli_over_text(until: str) -> str:
    return (f"🔴 Дополнительная квота исчерпана. Доступ ко всем устройствам "
            f"приостановлен до {until}.")


def _cli_over_admin_text(name: str) -> str:
    return f"🔴 Клиент «{_e(name)}» исчерпал лимит и доп.квоту — доступ приостановлен."


def _admin_self_over_text() -> str:
    return "🔴 Твой месячный лимит потребления исчерпан (уведомление, доступ не тронут)."


# ─────────────────────────────────────────────────────────────────────────────
# Services
# ─────────────────────────────────────────────────────────────────────────────

class Services:
    def __init__(self, db):
        self.db = db

    # ── Блокировки (битовые маски причин) ────────────────────────────────────
    # is_blocked как отдельного поля нет: заблокирован ⇔ block_reason != 0.
    # IP физически режем/снимаем по ИТОГОВОМУ состоянию маски: DROP ставим, когда
    # появляется хоть один бит; снимаем — только когда сброшены ВСЕ.

    def _device_set_block(self, device_id: int, bit: DeviceBlock) -> None:
        """Установить причину блокировки устройства (бит) и наложить DROP."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        new_mask = int(dev.block_reason) | int(bit)
        if new_mask == int(dev.block_reason):
            return
        self.db.update_device_fields(device_id, block_reason=new_mask)
        try:
            awg.block_ip(dev.address)           # идемпотентно
        except awg.AwgError:
            pass

    def _device_clear_block(self, device_id: int, bit: DeviceBlock) -> None:
        """Снять причину (бит). Если не осталось причин — снять DROP."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        new_mask = int(dev.block_reason) & ~int(bit)
        if new_mask == int(dev.block_reason):
            return
        self.db.update_device_fields(device_id, block_reason=new_mask)
        if new_mask == 0:
            try:
                awg.unblock_ip(dev.address)     # идемпотентно
            except awg.AwgError:
                pass

    def _client_set_block(self, client_id: int, bit: ClientBlock) -> None:
        """Установить причину блокировки клиента (только маска клиента; физически
        трафик режется по устройствам — этим занимается вызывающий код)."""
        c = self.db.get_client(client_id)
        if c is None:
            return
        self.db.update_client_fields(
            client_id, block_reason=int(c.block_reason) | int(bit))

    def _client_clear_block(self, client_id: int, bit: ClientBlock) -> None:
        c = self.db.get_client(client_id)
        if c is None:
            return
        old_mask = int(c.block_reason)
        new_mask = old_mask & ~int(bit)
        self.db.update_client_fields(client_id, block_reason=new_mask)
        # эпизод бана завершён (клиент полностью разблокирован) → в аудит
        if old_mask and new_mask == 0:
            self.db.archive_block(client_id, old_mask, "unblocked")

    # ── Ручные блокировки (админ / клиент) ───────────────────────────────────

    def block_device_manual(self, device_id: int, bit: DeviceBlock,
                            notify: bool) -> list["Notification"]:
        """Ручной блок устройства заданным битом (ADMIN_SILENT/NOTIFIED/USER).
        notify=True → уведомить пользователя (клиента-владельца и/или друга).
        Тихий блок (notify=False) уведомлений не шлёт."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return []
        self._device_set_block(device_id, bit)
        notes: list[Notification] = []
        if notify:
            owner = self.db.get_client(dev.client_id)
            if owner and owner.tg_id:
                notes.append(Notification(owner.tg_id,
                             _manual_device_blocked_client(dev.name)))
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        return notes

    def unblock_device_manual(self, device_id: int, bit: DeviceBlock,
                              notify: bool) -> list["Notification"]:
        """Снять ручной бит с устройства. notify → уведомить, если после снятия
        устройство разблокировано полностью (не осталось других причин)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return []
        self._device_clear_block(device_id, bit)
        fresh = self.db.get_device(device_id)
        fully_free = int(fresh.block_reason) == 0
        notes: list[Notification] = []
        if notify and fully_free:
            owner = self.db.get_client(dev.client_id)
            if owner and owner.tg_id:
                notes.append(Notification(owner.tg_id,
                             _manual_device_unblocked_client(dev.name)))
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_unblocked_text(dev.name)))
        return notes

    def block_client_manual(self, client_id: int, bit: ClientBlock,
                           notify: bool, pause_days=None) -> list["Notification"]:
        """Ручной блок клиента админом. Каскадит ТЕМ ЖЕ типом (silent/notified) на
        все устройства (физический DROP). notify → уведомить клиента и друзей.
        pause_days: None — без приостановки подписки; 0 — бессрочная приостановка
        (admin_open); N>0 — срочная (admin_fixed). Приостановка тикания подписки
        реализуется через enter_admin_pause + бит PAUSED."""
        client = self.db.get_client(client_id)
        if client is None or client.is_service:
            return []
        if client.tg_id == config.ADMIN_ID:
            return []                    # клиент админа не блокируется (defense-in-depth)
        self._client_set_block(client_id, bit)
        # приостановка подписки (если запрошена) — до каскада, чтобы PAUSED тоже лёг
        if pause_days is not None:
            self.enter_admin_pause(client_id, pause_days)
            self._client_set_block(client_id, ClientBlock.PAUSED)
        dev_bit = DeviceBlock(int(bit))
        notes: list[Notification] = []
        for dev in self.db.list_devices(client_id):
            self._device_set_block(dev.id, dev_bit)
            if pause_days is not None:
                self._device_set_block(dev.id, DeviceBlock.PAUSED)
            if notify and dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        if notify and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _manual_client_blocked()))
        return notes

    def unblock_client_manual(self, client_id: int, bit: ClientBlock,
                             notify: bool) -> list["Notification"]:
        """Снять ручной бит с клиента и каскадно с устройств (тем же типом).
        Если активна админская приостановка — сначала закрываем её через exit_pause
        (пересчёт периода по факту), затем снимаем биты."""
        client = self.db.get_client(client_id)
        if client is None:
            return []
        notes: list[Notification] = []
        had_admin_pause = (client.pause_active_since
                           and client.pause_mode in (PauseMode.ADMIN_FIXED, PauseMode.ADMIN_OPEN))
        if had_admin_pause:
            _, _, _, pause_notes = self.exit_pause(client_id, auto=False)
            notes += pause_notes           # друзьям — о снятии паузы (из exit_pause)
        self._client_clear_block(client_id, bit)
        dev_bit = DeviceBlock(int(bit))
        for dev in self.db.list_devices(client_id):
            self._device_clear_block(dev.id, dev_bit)
            fresh = self.db.get_device(dev.id)
            if (notify and int(fresh.block_reason) == 0
                    and dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id):
                notes.append(Notification(dev.friend_tg_id,
                             _friend_unblocked_text(dev.name)))
        # клиенту — одно уведомление о снятии, если полностью разблокирован и notify
        fresh_c = self.db.get_client(client_id)
        if notify and int(fresh_c.block_reason) == 0 and client.tg_id:
            notes.append(Notification(client.tg_id,
                         _manual_client_unblocked()))
        return notes

    # ── Изменение лимитов потребления (с авто-разблокировкой) ────────────────

    def set_device_traffic_limit(self, device_id: int, limit_bytes: int) -> None:
        """Задать/изменить лимит устройства. Если новый лимит выше текущего
        расхода (или снят в безлимит) — снять причину TRAFFIC_USER (свой лимит
        устройства). Каскад клиента (TRAFFIC_CLIENT) НЕ трогаем."""
        prev = self.db.get_device(device_id)
        if prev is not None and int(prev.traffic_limit) != int(limit_bytes):
            # аудит: снимок старого лимита перед изменением
            self.db.archive_device_quota(device_id, "limit_changed")
        self.db.update_device_fields(device_id, traffic_limit=limit_bytes)
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
        if (limit_bytes == 0 or used < limit_bytes) and \
                (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
            self._device_clear_block(device_id, DeviceBlock.TRAFFIC_USER)
        # снятая метка «over» — чтобы уведомление могло прийти повторно при
        # новом исчерпании после поднятия лимита
        self._forget_traffic_marker(dev.client_id, f"dev_over:{device_id}")
        self._forget_traffic_marker(dev.client_id, f"dev80:{device_id}")

    def set_client_traffic_limit(self, client_id: int, limit_bytes: int) -> None:
        """Задать/изменить тотал-лимит клиента. Если новый лимит выше текущего
        расхода — снять КАСКАДНУЮ причину (TRAFFIC_CLIENT) с клиента и устройств.
        Собственный лимит устройства (DeviceBlock.TRAFFIC_USER) НЕ трогаем — его
        снимет только поднятие лимита самого устройства."""
        prev = self.db.get_client(client_id)
        if prev is not None and prev.tg_id == config.ADMIN_ID:
            return                       # клиент админа не ограничивается (defense-in-depth)
        if prev is not None and int(prev.traffic_limit) != int(limit_bytes):
            # аудит: снимок старой квоты перед изменением
            self.db.archive_quota(client_id, "limit_changed")
        self.db.update_client_fields(client_id, traffic_limit=limit_bytes)
        client = self.db.get_client(client_id)
        if client is None:
            return
        devices = self.db.list_devices(client_id)
        total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month)
                    for d in devices)
        effective = limit_bytes + int(client.bonus_bytes)
        if limit_bytes == 0 or total < effective:
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client_id, ClientBlock.TRAFFIC_CLIENT)
            for dev in devices:
                if int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT):
                    self._device_clear_block(dev.id, DeviceBlock.TRAFFIC_CLIENT)
            self._forget_traffic_marker(client_id, "cli_over")
            self._forget_traffic_marker(client_id, "cli80")

    def _forget_traffic_marker(self, client_id: int, marker: str) -> None:
        """Снять одну метку трафик-уведомления (чтобы уведомление могло прийти
        снова после поднятия лимита и повторного исчерпания)."""
        cur = self.db.get_traffic_notified(client_id)
        if marker in cur:
            cur.discard(marker)
            self.db.update_client_fields(
                client_id, traffic_notified=",".join(sorted(cur)))

    # ── Инвайты / клиенты ────────────────────────────────────────────────────

    # Формат инвайт-кода: префикс (C=клиент / F=друг) + 11 символов [A-Za-z0-9].
    # Всего 12. Префикс определяет тип при активации — без сверки по таблицам.
    _CODE_ALPHABET = string.ascii_letters + string.digits   # 62 символа
    _CODE_BODY_LEN = 11

    def _gen_code_body(self) -> str:
        return "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(self._CODE_BODY_LEN))

    def _gen_invite(self) -> str:
        """Клиентский код (префикс C), уникальный среди всех неиспользованных."""
        while True:
            code = "C" + self._gen_code_body()
            if self.db.get_client_by_invite(code) is None \
               and self.db.get_device_by_friend_code(code) is None:
                return code

    def _gen_friend_code(self) -> str:
        """Код друга (префикс F), уникальный среди всех неиспользованных."""
        while True:
            code = "F" + self._gen_code_body()
            if self.db.get_client_by_invite(code) is None \
               and self.db.get_device_by_friend_code(code) is None:
                return code

    def create_client(self, name: str, device_limit: int, period_kind: str,
                      traffic_limit: int = 0) -> ClientCreated:
        if period_kind not in config.PERIOD_CHOICES:
            raise ServiceError(f"Неизвестный период: {period_kind}")
        now = timeutil.now()
        end = None if period_kind == PeriodKind.NEVER else timeutil.add_period(now, period_kind)
        invite = self._gen_invite()
        cid = self.db.create_client(
            name, device_limit, timeutil.to_iso(now),
            timeutil.to_iso(end) if end else None, invite,
            traffic_limit=traffic_limit, period_kind=period_kind,
        )
        return ClientCreated(client_id=cid, invite_code=invite, period_end=end)

    def activate_client(self, invite_code: str, tg_id: int) -> ActivationResult:
        existing = self.db.get_client_by_tg(tg_id)
        if existing is not None and not existing.is_service:
            return ActivationResult(ok=False, reason="already_has_access", client=existing)
        row = self.db.get_client_by_invite(invite_code)
        if row is None:
            return ActivationResult(ok=False, reason="invalid")
        self.db.activate_client(row.id, tg_id)
        return ActivationResult(ok=True, reason="ok", client=self.db.get_client(row.id))

    def regenerate_invite(self, client_id: int) -> str:
        """Перевыпуск инвайта для pending-клиента (потерял ссылку до активации)."""
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        if client.activation_status != ActivationStatus.PENDING:
            raise ServiceError("Клиент уже активирован — инвайт не нужен")
        code = self._gen_invite()
        self.db.update_client_fields(client_id, invite_code=code)
        return code

    # ── Устройства ───────────────────────────────────────────────────────────

    def add_device(self, client_id: int, name: str, traffic_limit: int = 0) -> DeviceCreated:
        """Поток 2: генерация ключей → аллокация IP → БД → awg.add_peer →
        clientsTable → конфиг. При сбое применения — откат БД."""
        client = self.db.get_client(client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        if not client.is_service:
            limit = client.device_limit
            if limit != 0 and self.db.count_devices(client_id) >= limit:  # 0 = безлимит
                raise LimitReached("Достигнут лимит устройств")

        # Весь блок «аллокация IP → запись в БД → применение в контейнере» под
        # мьютексом мутаций: закрывает гонку двух одновременных добавлений
        # (одинаковый IP / потерянный peer при конкурентной правке конфига).
        with awg.mutation_lock:
            # аллокация: занятые из БД + из живого конфига (учёт app-устройств)
            occupied_live = awg.read_occupied_ips()
            ip = self.db.allocate_ip(
                subnet_prefix=config.SUBNET_PREFIX,
                occupied_extra=occupied_live,
                start_host=config.IP_HOST_START,
                end_host=config.IP_HOST_END,
            )
            priv, pub = awg.gen_keypair()
            server_params = awg.read_server_params()
            psk = server_params["psk"]

            # БД сначала (дешёвый откат)
            try:
                device_id = self.db.create_device(
                    client_id, name, pub, psk, ip, private_key=priv,
                    traffic_limit=traffic_limit,
                )
            except sqlite3.IntegrityError as e:
                raise ServiceError(f"Конфликт при создании устройства, попробуй ещё раз: {e}")
            try:
                awg.add_peer(pub, psk, ip)
            except awg.AwgError as e:
                self.db.delete_device(device_id, archive_reason=None)  # откат, не архивируем
                raise ServiceError(f"Не удалось применить конфиг на сервере: {e}")

        # если клиент сейчас истёкший — новое устройство тоже блокируем (EXPIRY)
        if not client.is_service and client.status == SubStatus.EXPIRED:
            self._device_set_block(device_id, DeviceBlock.EXPIRY)

        # clientsTable — некритично (VPN работает и без неё)
        try:
            awg.clientstable_upsert(pub, name)
        except Exception:
            pass

        cfg = configgen.generate(priv, pub, ip, server_params)
        # новое устройство админа → сразу открыть ему SSH-к-хосту (не ждать цикла)
        if client.tg_id == config.ADMIN_ID:
            self.reconcile_ssh_access()
        return DeviceCreated(device_id=device_id, address=ip, vpn=cfg["vpn"], conf=cfg["conf"])

    def remove_device(self, device_id: int) -> Optional[int]:
        """Удаление устройства: контейнер → clientsTable → БД (в таком порядке,
        чтобы не осталось записи в БД без реального снятия пира).
        Возвращает friend_tg_id, если у устройства был активный друг (для
        уведомления, что доступ прекращён), иначе None."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        friend_tg = dev.friend_tg_id if dev.friend_status == FriendStatus.ACTIVE else None
        try:
            awg.remove_peer(dev.public_key)
        except awg.AwgError as e:
            raise ServiceError(f"Не удалось снять устройство на сервере: {e}")
        # DROP снимаем ПОСЛЕ успешного снятия пира: если remove_peer упал,
        # устройство осталось в конфиге и должно остаться заблокированным.
        # Снять обязательно — иначе осиротевшее правило заблокирует будущего
        # владельца этого IP (аллокатор переиспользует освободившиеся адреса).
        if int(dev.block_reason) != 0:
            try:
                awg.unblock_ip(dev.address)
            except awg.AwgError:
                pass
        try:
            awg.clientstable_remove(dev.public_key)
        except Exception:
            pass
        self.db.delete_device(device_id)
        return friend_tg

    def generate_config(self, device_id: int) -> dict:
        """Перевыпуск конфига устройства. Только для bot-устройств (у app нет
        приватного ключа)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if not dev.private_key:
            raise ServiceError(
                "Устройство создано во внешнем приложении — перевыпуск ссылки недоступен. "
                "Пропишите строку подключения, чтобы включить полный доступ."
            )
        server_params = awg.read_server_params()
        return configgen.generate(dev.private_key, dev.public_key, dev.address, server_params)

    def restore_app_device(self, device_id: int, vpn_link: str) -> str:
        """Реставрация app-устройства КЛИЕНТСКОЙ ссылкой: валидируем priv↔pub и
        записываем приватный ключ. Full-access ссылку сюда НЕ принимаем — для неё
        отдельный путь attach_full_access (иные инварианты). Возвращает "client"."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.private_key:
            raise ServiceError("Устройство уже под полным управлением")
        info = configgen.classify_vpn_link(vpn_link)     # ValueError на мусоре
        if info["kind"] == "full_access":
            raise ServiceError("IS_FULL_ACCESS")         # ФА-ссылка → в attach_full_access
        priv = info["client_priv_key"]
        if awg.pubkey_of(priv) != dev.public_key:
            raise ServiceError("WRONG_DEVICE")
        self.db.update_device_fields(device_id, private_key=priv)
        return "client"

    def rename_device(self, device_id: int, new_name: str) -> None:
        """Переименование устройства ботом (п.3): бот — источник истины, пишем
        имя в ОБЕ базы — БД бота и clientsTable awg (чтобы приложение показывало
        то же имя). clientsTable-запись некритична: если контейнер недоступен,
        имя в БД всё равно обновим, а clientsTable подтянется при следующем
        add/rename."""
        new_name = new_name.strip()
        if not new_name:
            raise ServiceError("Имя не может быть пустым")
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self.db.update_device_fields(device_id, name=new_name)
        try:
            awg.clientstable_upsert(dev.public_key, new_name)
        except awg.AwgError:
            pass                         # контейнер недоступен — БД уже обновлена

    def find_full_access_device(self):
        """Текущее ФА-устройство (или None). ФА-устройство только одно."""
        for d in self.db.list_all_devices():
            if d.is_admin:
                return d
        return None

    def attach_full_access(self, device_id: int, vpn_link: str,
                           *, transfer: bool = False) -> str:
        """Прикрепить ФА-ссылку к устройству. Инварианты:
          • ссылка ИМЕННО full-access (иначе NOT_FULL_ACCESS);
          • шифрование обязательно (NEED_ENCRYPTION), ключ из env;
          • устройство принадлежит клиенту-администратору или ничейно (app в
            служебном пуле) — иначе NOT_ADMIN_DEVICE (чтобы root-ключ не попал к
            чужому tg_id);
          • ФА-устройство ТОЛЬКО ОДНО: есть другое → нужен transfer=True
            (автоперенос: со старого снимаем метку → обычное bot-устройство),
            иначе EXISTS:<имя>.
        Ключа у ФА нет (private_key NULL) — приложение генерит его само."""
        info = configgen.classify_vpn_link(vpn_link)     # ValueError на мусоре
        if info["kind"] != "full_access":
            raise ServiceError("NOT_FULL_ACCESS")
        if not config.BACKUP_ENCRYPTION_ENABLED:
            raise ServiceError("NEED_ENCRYPTION")
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        admin_cid = self.ensure_admin_client()
        service_id = self.db.get_service_client_id()
        if dev.client_id not in (admin_cid, service_id):
            raise ServiceError("NOT_ADMIN_DEVICE")
        existing = self.find_full_access_device()
        if existing is not None and existing.id != device_id:
            if not transfer:
                raise ServiceError(f"EXISTS:{existing.name}")
            self.db.update_device_fields(existing.id, full_access_link=None)
        from awgbot.util import secrets_util
        blob = secrets_util.encrypt(vpn_link.strip().encode(), **self._backup_enc_kwargs())
        enc_b64 = secrets_util.b64e(blob)
        self.db.update_device_fields(device_id, full_access_link=enc_b64,
                                     client_id=admin_cid)
        return "full_access"

    def reveal_full_access_link(self, device_id: int) -> str:
        """Расшифровать и вернуть сохранённую full-access ссылку (для выдачи
        QR/файлом/строкой). Ключ — из env (как для бэкапов). Поднимает
        ServiceError, если устройство не admin или ключ недоступен."""
        dev = self.db.get_device(device_id)
        if dev is None or not dev.full_access_link:
            raise ServiceError("Устройство не найдено или без ссылки полного доступа")
        if not config.BACKUP_ENCRYPTION_ENABLED:
            raise ServiceError("NEED_ENCRYPTION")
        from awgbot.util import secrets_util
        blob = secrets_util.b64d(dev.full_access_link)
        return secrets_util.decrypt(blob, **self._backup_enc_kwargs()).decode()

    def clear_full_access(self, device_id: int) -> None:
        """Снять метку полного доступа: стереть сохранённую ссылку. Устройство
        перестаёт быть is_admin и возвращается к обычному поведению (гостевой
        пир снова управляем, app-пир падает к служебному «без клиента»). Выход
        из дедлока, когда админ назначил ФА не тому пиру. Ссылку восстановить
        нельзя — она утрачивается."""
        dev = self.db.get_device(device_id)
        if dev is None or not dev.full_access_link:
            raise ServiceError("Устройство не найдено или без ссылки полного доступа")
        # Снятие метки: стираем ссылку, возвращаем в служебный пул. Ключа у
        # бота нет — снова «чужой пир без профиля», реставрируемый/удаляемый.
        service_id = self.db.get_service_client_id()
        self.db.update_device_fields(device_id, full_access_link=None,
                                     client_id=service_id)

    # ── Друзья (роль invited): приглашение на управление одним устройством ────

    def make_device_friendly(self, device_id: int) -> str:
        """Помечает СУЩЕСТВУЮЩЕЕ устройство гостевым: генерит код друга, ставит
        pending. Возвращает код для пересылки. Требует bot-устройство (у app нет
        ссылки — другу нечего было бы выдать)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if not dev.private_key:
            raise ServiceError("Устройство из приложения нельзя передать: у бота нет его ссылки")
        if dev.friend_status == FriendStatus.ACTIVE:
            raise ServiceError("Устройством уже управляет друг")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_tg_id=None,
                                  friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def reissue_friend_code(self, device_id: int) -> str:
        """Перевыдать код друга — только пока приглашение не активировано."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.friend_status != FriendStatus.PENDING:
            raise ServiceError("Перевыдать код можно только для неактивированного приглашения")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_tg_id=None,
                                  friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def activate_friend(self, code: str, tg_id: int) -> "FriendActivation":
        """Активация кода друга. Проверки: код существует и pending; этот tg ещё
        не действующий пользователь/админ (одна роль на человека)."""
        # уже клиент или админ → другом быть не может
        existing = self.db.get_client_by_tg(tg_id)
        if tg_id == config.ADMIN_ID or (existing is not None and not existing.is_service):
            return FriendActivation(ok=False, reason="already_user")
        dev = self.db.get_device_by_friend_code(code)
        if dev is None or dev.friend_status != FriendStatus.PENDING:
            return FriendActivation(ok=False, reason="invalid")
        self.db.set_device_friend(dev.id, friend_tg_id=tg_id,
                                  friend_code=None, friend_status=FriendStatus.ACTIVE)
        return FriendActivation(ok=True, reason="ok", device_id=dev.id,
                                device_name=dev.name)

    def friend_devices(self, tg_id: int) -> list:
        """ВСЕ активные устройства друга (мультидружба)."""
        return self.db.get_devices_by_friend_tg(tg_id)

    def friend_device_by_id(self, tg_id: int, device_id: int):
        """Устройство друга по id С ПРОВЕРКОЙ, что оно действительно его (защита
        от чужого device_id в callback). Возвращает Row или None."""
        for dev in self.db.get_devices_by_friend_tg(tg_id):
            if dev.id == device_id:
                return dev
        return None

    def reassign_device(self, device_id: int, new_client_id: int,
                        add_slot: bool = False) -> dict:
        """Перепривязка устройства к клиенту. Если add_slot — заодно поднимаем
        лимит на 1 (когда у получателя не было свободного слота).
        Возвращает данные для уведомления ОБОИХ сторон:
          name, donor{tg_id,count,limit}, recipient{tg_id,count,limit},
          added_slot."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        client = self.db.get_client(new_client_id)
        if client is None:
            raise ServiceError("Клиент не найден")
        donor = self.db.get_client(dev.client_id)          # прежний владелец
        with self.db.transaction():
            new_limit = client.device_limit
            # add_slot поднимает лимит, НО безлимит (0) не трогаем: протухшая
            # кнопка «добавить слот» не должна превращать безлимит в лимит-1
            slot_bumped = add_slot and new_limit != 0
            if slot_bumped:
                new_limit += 1
                self.db.update_client_fields(new_client_id, device_limit=new_limit)
            else:
                # Повторная проверка лимита ВНУТРИ транзакции — закрывает TOCTOU
                # между has_free_slot в хендлере и фактической привязкой. Без слота
                # и без add_slot привязывать нельзя (иначе появляется «3 из 2»).
                if new_limit != 0 and self.db.count_devices(new_client_id) >= new_limit:
                    raise LimitReached(
                        "У клиента нет свободного слота — привязка отклонена")
            self.db.reassign_device(device_id, new_client_id)
        # счётчики ПОСЛЕ перепривязки (живой COUNT — уже актуальны)
        donor_count = self.db.count_devices(donor.id) if donor else 0
        recip_count = self.db.count_devices(new_client_id)
        return {
            "name": dev.name,
            "added_slot": slot_bumped,
            "donor": None if (donor is None or donor.is_service) else {
                "tg_id": donor.tg_id, "count": donor_count, "limit": donor.device_limit,
            },
            "recipient": {
                "tg_id": client.tg_id, "count": recip_count, "limit": new_limit,
            },
        }

    def ensure_admin_client(self) -> int:
        """Гарантирует существование клиентской записи админа (он тоже юзер VPN).
        Бессрочная (period_end NULL), безлимит (device_limit 0), сразу active,
        привязана к ADMIN_ID. Опознаётся по tg_id == ADMIN_ID (без новой колонки),
        скрыта из списка клиентов. Идемпотентно — зовётся при старте."""
        existing = self.db.get_client_by_tg(config.ADMIN_ID)
        if existing is not None:
            return existing.id
        now = timeutil.now()
        cid = self.db.create_client(
            "Администратор", 0, timeutil.to_iso(now), None, self._gen_invite(),
        )
        self.db.update_client_fields(
            cid, tg_id=config.ADMIN_ID, activation_status=ActivationStatus.ACTIVE, status=SubStatus.ACTIVE,
        )
        return cid

    def admin_client(self):
        """Клиентская запись админа (или None, если ещё не создана)."""
        return self.db.get_client_by_tg(config.ADMIN_ID)

    def has_free_slot(self, client_id: int) -> bool:
        client = self.db.get_client(client_id)
        if client is None:
            return False
        if client.device_limit == 0:            # 0 = безлимит
            return True
        return self.db.count_devices(client_id) < client.device_limit

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
            notifications.append(Notification(client.tg_id, msg))
        return ExtendResult(new_end=new_end, notifications=notifications)

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

    def pause_available_days(self, client_id: int) -> int:
        """Сколько дней приостановки клиент может взять ПРЯМО СЕЙЧАС = остаток
        суммарного лимита за период (PAUSE_MAX_TOTAL_DAYS − уже использовано).
        Только годовая; иначе 0. Ни остатком подписки, ни «максимумом за один
        вход» НЕ ограничиваем — единственный лимит суммарный, за период."""
        client = self.db.get_client(client_id)
        if client is None or client.period_kind != PeriodKind.YEAR or not client.period_end:
            return 0
        return max(0, settings.get_int("pause.pause_max_total_days", 28) - int(client.pause_used_days))

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
                    used_days=int(client.pause_used_days)))
                if client.period_end:
                    end = timeutil.parse_iso(client.period_end)
                    self.db.update_client_fields(client_id, period_end=timeutil.to_iso(
                        end + datetime.timedelta(days=days)))
            else:
                # бессрочная: подписка temp-бессрочная, конец сохраняем для пересчёта
                self.db.save_pause(client_id, models.PauseState(
                    active_since=timeutil.to_iso(now), reserved_days=0,
                    mode=PauseMode.ADMIN_OPEN, saved_end=client.period_end,
                    used_days=int(client.pause_used_days)))
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
            used_add = actual if mode == PauseMode.USER else 0  # лимит 28 копит только user
        # атомарно: снимок эпизода в аудит + гашение активности паузы (used_days
        # периода сохраняем «спящим») + правка периода/блока
        with self.db.transaction():
            self.db.snapshot_pause(client_id, "auto" if auto else "manual")
            self.db.save_pause(client_id, models.PauseState(
                active_since=None, reserved_days=0, mode=None, saved_end=None,
                used_days=int(client.pause_used_days) + used_add))
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
        for client in self.db.list_clients(include_service=False):
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

    # ── Опрос трафика (поток 4) ──────────────────────────────────────────────

    def poll_traffic(self) -> None:
        """Каждые 5 мин: dump → дельты с обработкой отката счётчика → накопление.
        last_handshake обновляем только при наличии (не затираем пустым).

        ВСЯ обработка — одна транзакция: (а) целостность — упади бот между
        накоплением и записью базы дельт, при раздельных коммитах дельта
        посчиталась бы дважды; (б) один fsync вместо 3-4 на устройство."""
        peers = {p["public_key"]: p for p in awg.show_dump()}
        # бесплатный побочный продукт: онлайн-счётчик для статусного блока
        # (dump уже в руках — не тратим отдельный exec в мониторе)
        online = sum(1 for p in peers.values()
                     if timeutil.handshake_is_online(p["last_handshake"]))
        with self.db.transaction():
            if self.db.get_state("online_count") != str(online):
                self.db.set_state("online_count", str(online))   # только при изменении
            for dev in self.db.list_all_devices():
                p = peers.get(dev.public_key)
                if p is None:
                    continue                          # состав пиров — забота reconcile
                rx_now, tx_now = p["rx"], p["tx"]
                sample = self.db.get_sample(dev.id)
                if sample is None:
                    self.db.set_sample(dev.id, rx_now, tx_now)   # первая база
                else:
                    drx = rx_now - sample["last_rx"]
                    dtx = tx_now - sample["last_tx"]
                    if drx < 0:                       # счётчик упал (рестарт awg)
                        drx = rx_now
                    if dtx < 0:
                        dtx = tx_now
                    if drx or dtx:
                        self.db.add_traffic(dev.id, drx, dtx)
                    self.db.set_sample(dev.id, rx_now, tx_now)
                if p["last_handshake"] and p["last_handshake"] != dev.last_handshake:
                    # пишем только при изменении: оффлайн-устройство не должно
                    # генерить UPDATE тем же значением каждые 5 минут
                    self.db.update_device_fields(dev.id, last_handshake=p["last_handshake"])

    # ── Лимиты потребления (ТЗ 7-8) ──────────────────────────────────────────

    def check_traffic_limits(self) -> list["Notification"]:
        """После накопления дельт: проверка лимитов устройств и тоталов клиентов.
        Возвращает уведомления (превышения, пред-уведомления 80%, доп.квота).

        Всё в рамках календарного месяца (счётчики _month сбрасываются 1-го).
        Меряем против СУММЫ up+down. Блокировки — битом TRAFFIC (не трогая EXPIRY).
        """
        notes: list[Notification] = []
        warn_pct = settings.get_int("limits.traffic_warn_percent", 80)
        until = timeutil.first_of_next_month_str()
        admin_id = config.ADMIN_ID

        for client in self.db.list_clients(include_service=False):
            if client.activation_status != ActivationStatus.ACTIVE:
                continue
            devices = self.db.list_devices(client.id)
            sent = self.db.get_traffic_notified(client.id)

            # ── лимиты устройств (независимо от клиентского) ──
            for dev in devices:
                dlim = dev.traffic_limit
                if dlim == 0:
                    continue
                used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
                over_marker = f"dev_over:{dev.id}"
                warn_marker = f"dev80:{dev.id}"
                if used >= dlim:
                    if not (int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)):
                        self._device_set_block(dev.id, DeviceBlock.TRAFFIC_USER)
                    if over_marker not in sent:
                        is_friend_dev = (dev.friend_status == FriendStatus.ACTIVE
                                         and dev.friend_tg_id)
                        # хозяину: спец-текст с пометкой «друг», если устройство
                        # передано; другу — обычный текст про его устройство
                        host_text = (_friend_dev_over_host_text(dev.name, until)
                                     if is_friend_dev else _dev_over_text(dev.name, until))
                        notes.append(Notification(client.tg_id, host_text))
                        if is_friend_dev:
                            notes.append(Notification(
                                dev.friend_tg_id, _dev_over_text(dev.name, until)))
                        self.db.add_traffic_notified(client.id, over_marker)
                elif used >= dlim * warn_pct // 100:
                    if warn_marker not in sent:
                        notes.append(Notification(
                            client.tg_id, _dev_warn_text(dev.name, warn_pct)))
                        if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                            notes.append(Notification(
                                dev.friend_tg_id, _dev_warn_text(dev.name, warn_pct)))
                        self.db.add_traffic_notified(client.id, warn_marker)

            # ── тотал клиента ──
            climit = client.traffic_limit
            if climit == 0:
                continue
            total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month)
                        for d in devices)
            effective = climit + int(client.bonus_bytes)
            is_admin_client = (client.tg_id == admin_id)

            if total >= effective:
                # исчерпан текущий потолок (базовый или уже с доп.квотой)
                if is_admin_client:
                    if "cli_over" not in sent:
                        notes.append(Notification(admin_id, _admin_self_over_text()))
                        self.db.add_traffic_notified(client.id, "cli_over")
                    continue
                if not client.bonus_granted_month:
                    # первая доп.квота этого месяца
                    bonus = settings.get_int("limits.traffic_bonus_gb", 100) * BYTES_PER_GB
                    # аудит: снимок квоты до выдачи разовой доп.квоты
                    self.db.archive_quota(client.id, "bonus_granted")
                    self.db.update_client_fields(
                        client.id,
                        bonus_bytes=int(client.bonus_bytes) + bonus,
                        bonus_granted_month=1)
                    notes.append(Notification(
                        client.tg_id,
                        _cli_bonus_text(settings.get_int("limits.traffic_bonus_gb", 100), until)))
                    notes.append(Notification(
                        admin_id, _cli_bonus_admin_text(client.name, settings.get_int("limits.traffic_bonus_gb", 100))))
                    self.db.add_traffic_notified(client.id, "bonus")
                else:
                    # доп.квота уже выдавалась и тоже исчерпана → блок всех устройств
                    # КАСКАДНЫМ битом (TRAFFIC_CLIENT), не собственным TRAFFIC:
                    # так поднятие лимита устройства не снимет блок «по клиенту».
                    if "cli_over" not in sent:
                        self._client_set_block(client.id, ClientBlock.TRAFFIC_CLIENT)
                        for dev in devices:
                            self._device_set_block(dev.id, DeviceBlock.TRAFFIC_CLIENT)
                            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                                notes.append(Notification(
                                    dev.friend_tg_id, _dev_over_text(dev.name, until)))
                        notes.append(Notification(client.tg_id, _cli_over_text(until)))
                        notes.append(Notification(
                            admin_id, _cli_over_admin_text(client.name)))
                        self.db.add_traffic_notified(client.id, "cli_over")
            elif total >= effective * warn_pct // 100:
                if "cli80" not in sent and not is_admin_client:
                    notes.append(Notification(client.tg_id, _cli_warn_text(warn_pct)))
                    self.db.add_traffic_notified(client.id, "cli80")

        return notes

    # ── Проверка сроков + уведомления (поток из ТЗ 7) ────────────────────────

    def _block_client(self, client) -> list["Notification"]:
        """Блокирует все устройства клиента по причине EXPIRY (подписка истекла).
        Ставит бит и клиенту. Возвращает уведомления друзьям переданных (active)
        устройств — доступ приостановлен."""
        notes: list[Notification] = []
        self._client_set_block(client.id, ClientBlock.EXPIRY)
        for dev in self.db.list_devices(client.id):
            self._device_set_block(dev.id, DeviceBlock.EXPIRY)
            if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                notes.append(Notification(dev.friend_tg_id,
                             _friend_blocked_text(dev.name)))
        return notes

    def check_expiry(self) -> list[Notification]:
        now = timeutil.now()
        notifications: list[Notification] = []
        for client in self.db.list_clients(include_service=False):
            if client.activation_status != ActivationStatus.ACTIVE or not client.period_end:
                continue
            end = timeutil.parse_iso(client.period_end)
            start = timeutil.parse_iso(client.period_start)
            secs = timeutil.remaining_seconds(end, now)
            period_len_min = timeutil.period_minutes(start, end)

            # истёк
            if secs <= 0:
                if client.status != SubStatus.EXPIRED:
                    friend_notes = self._block_client(client)
                    self.db.update_client_fields(client.id, status=SubStatus.EXPIRED)
                    if client.tg_id:
                        notifications.append(Notification(client.tg_id, _TXT_EXPIRED_CLIENT))
                    notifications.append(Notification(
                        config.ADMIN_ID, _TXT_EXPIRED_ADMIN.format(name=client.name)))
                    notifications.extend(friend_notes)   # друзьям — доступ приостановлен
                continue

            # пороги приближения (строго меньше длительности периода).
            # Если бот «проспал» несколько порогов, шлём ТОЛЬКО самый строгий
            # (ближайший к концу) из пересечённых, остальные молча помечаем —
            # иначе клиент получит простыню «30 дней»+«14»+«7»+«1» разом.
            already = self.db.get_notified(client.id)
            mins_left = secs // 60
            crossed = [
                (th_min, label) for th_min, label in config.NOTIFY_THRESHOLDS_MINUTES
                if th_min < period_len_min and mins_left <= th_min and th_min not in already
            ]
            if crossed:
                # самый строгий = наименьший порог по времени
                tightest_min, tightest_label = min(crossed, key=lambda x: x[0])
                if client.tg_id:
                    # кнопка отсрочки: только КЛИЕНТУ (не другу — друзья идут иным
                    # путём), только на ГОДОВОМ периоде и один раз за период.
                    grace_offer = (client.period_kind == PeriodKind.YEAR
                                   and not client.grace_used)
                    notifications.append(Notification(
                        client.tg_id, _TXT_EXPIRING_CLIENT.format(label=tightest_label),
                        grace_offer_client_id=client.id if grace_offer else 0))
                notifications.append(Notification(
                    config.ADMIN_ID,
                    _TXT_EXPIRING_ADMIN.format(name=client.name, label=tightest_label)))
                # помечаем ВСЕ пересечённые отправленными (включая пропущенные крупные)
                for th_min, _ in crossed:
                    self.db.add_notified(client.id, th_min)
        return notifications

    # ── Сбросы ───────────────────────────────────────────────────────────────

    def reset_monthly_traffic(self) -> list["Notification"]:
        """1-е число: обнулить месячные счётчики + доп.квоту + трафик-метки, и
        снять причину TRAFFIC со всех клиентов и устройств (разблокировать, если
        не осталось других причин). Причину EXPIRY НЕ трогаем — подписка живёт
        своим циклом. Возвращает уведомления о сбросе (профилям и друзьям);
        безлимитные позиции не показываем, пустые уведомления не шлём."""
        # аудит-метрика: снимок потребления завершившегося месяца ПЕРЕД обнулением.
        # Метка месяца — предыдущий календарный (сброс идёт 1-го числа за прошлый).
        _now = timeutil.now()
        _prev_month = (_now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        self.db.snapshot_monthly_traffic(_prev_month)
        self.db.reset_month_traffic_all()
        notes: list[Notification] = []
        friend_devs: dict[int, list] = {}     # friend_tg → [device rows] для их уведомлений
        for client in self.db.list_clients(include_service=False):
            self.db.update_client_fields(
                client.id, bonus_bytes=0, bonus_granted_month=0)
            self.db.reset_traffic_notified(client.id)
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client.id, ClientBlock.TRAFFIC_CLIENT)
            own_lines = []                    # лимитные СВОИ (не переданные) устройства
            for dev in self.db.list_devices(client.id):
                # месячный сброс снимает ОБЕ трафик-причины (свою и каскад клиента)
                for _tbit in (DeviceBlock.TRAFFIC_USER, DeviceBlock.TRAFFIC_CLIENT):
                    if int(dev.block_reason) & int(_tbit):
                        self._device_clear_block(dev.id, _tbit)
                lim = int(dev.traffic_limit)
                if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                    if lim > 0:               # друг увидит в своём уведомлении
                        friend_devs.setdefault(dev.friend_tg_id, []).append(dev)
                elif lim > 0:
                    own_lines.append(f"{dev.name} — {_gb_limit(lim)}")
            # профилю шлём, если есть что показать: лимит профиля ИЛИ лимитные устройства
            total_limit = int(client.traffic_limit)
            if client.tg_id and (total_limit > 0 or own_lines):
                notes.append(Notification(
                    client.tg_id, _reset_client_text(total_limit, own_lines)))
        # друзьям — по их лимитным устройствам
        for friend_tg, devs in friend_devs.items():
            lines = [f"{d.name} — {_gb_limit(int(d.traffic_limit))}" for d in devs]
            notes.append(Notification(friend_tg, _reset_friend_text(lines)))
        return notes

    # ── Реконсиляция состава пиров (вотчдог) ─────────────────────────────────

    @staticmethod
    def _peers_with_ip(conf_text: str) -> dict[str, str]:
        """pubkey → ip из живого awg0.conf."""
        header, peers = awg._split_conf(conf_text)
        result: dict[str, str] = {}
        for p in peers:
            if not p["pubkey"]:
                continue
            ip = None
            for line in p["lines"]:
                if line.strip().startswith("AllowedIPs"):
                    ip = line.split("=", 1)[1].strip().split("/")[0]
            if ip:
                result[p["pubkey"]] = ip
        return result

    def reconcile_peers(self) -> list[Notification]:
        """Сверка живого конфига с БД. Новые пиры (созданные в приложении) →
        на служебного клиента + уведомление админу. Пропавшие → грациозный
        детект удаления (MISSING_SWEEPS_THRESHOLD сверок подряд)."""
        conf = awg.read_file(config.CONF_PATH)
        live = self._peers_with_ip(conf)                  # pub → ip
        table = {e.get("clientId"): e.get("userData", {}).get("clientName", "")
                 for e in awg.read_clients_table()}
        db_devices = {d.public_key: d for d in self.db.list_all_devices()}
        service_id = self.db.get_service_client_id()
        try:
            psk = awg.read_server_params()["psk"]
        except awg.AwgError:
            psk = ""
        notifications: list[Notification] = []

        # новые пиры из приложения
        for pub, ip in live.items():
            if pub in db_devices:
                continue
            name = table.get(pub) or f"Устройство {ip}"
            self.db.create_device(service_id, name, pub, psk, ip, private_key=None)
            notifications.append(Notification(
                config.ADMIN_ID, _TXT_NEW_APP_DEVICE.format(name=name, ip=ip)))

        # пропавшие пиры + обновление имён app-устройств из приложения
        for pub, dev in db_devices.items():
            if pub in live:
                if dev.missing_count:
                    self.db.update_device_fields(dev.id, missing_count=0)
                # переименование в приложении → подхватываем для ВСЕХ пиров
                # (имя в clientsTable — источник истины приложения).
                new_name = table.get(pub)
                if new_name and new_name != dev.name:
                    self.db.update_device_fields(dev.id, name=new_name)
                continue
            mc = dev.missing_count + 1
            if mc >= config.MISSING_SWEEPS_THRESHOLD:
                client = self.db.get_client(dev.client_id)
                # снять осиротевший DROP: iptables-правило без пира заблокирует
                # БУДУЩЕГО владельца этого IP (аллокатор переиспользует адреса)
                if int(dev.block_reason) != 0:
                    try:
                        awg.unblock_ip(dev.address)
                    except awg.AwgError:
                        pass
                friend_tg = (dev.friend_tg_id
                             if dev.friend_status == FriendStatus.ACTIVE else None)
                self.db.delete_device(dev.id)
                if client and not client.is_service:
                    notifications.append(Notification(
                        config.ADMIN_ID,
                        _TXT_APP_DEVICE_GONE.format(name=dev.name, client=client.name)))
                if friend_tg:
                    notifications.append(Notification(friend_tg, _TXT_FRIEND_DEVICE_GONE))
            else:
                self.db.update_device_fields(dev.id, missing_count=mc)
        return notifications

    # ── Реконсиляция блокировок после рестарта контейнера ────────────────────

    def reconcile_blocks(self) -> None:
        """iptables-DROP'ы эфемерны — после рестарта переналагаем их на всех,
        у кого block_reason != 0 в БД (любая причина блокировки)."""
        for dev in self.db.list_all_devices():
            if int(dev.block_reason) != 0:
                try:
                    if not awg.is_blocked(dev.address):
                        awg.block_ip(dev.address)
                except awg.AwgError:
                    pass

    def reconcile_ssh_access(self) -> None:
        """Пер-пирный SSH-к-хосту для устройств админа. Пересобирает фильтр в
        контейнере (цепочка AWGBOT_SSH): ACCEPT SSH только с адресов админских
        устройств на адреса хоста, DROP остальным. Идемпотентно и эфемерно, как
        блокировки, поэтому реассертится в тех же точках (старт, рестарт
        контейнера, монитор-цикл) плюс сразу при создании админского устройства.

        Пер-тик реассерт закрывает и удаление админского устройства, и
        переиспользование его IP другим (не-админским) — иначе осиротевший ACCEPT
        стал бы дырой."""
        try:
            targets = awg.host_ssh_targets()
            admin_ips = self.db.admin_device_addresses(config.ADMIN_ID)
            awg.ssh_reconcile(admin_ips, targets)
            # fail-closed на контейнере: DROP-по-умолчанию при подъёме awg0 (до
            # бота), чтобы окно «контейнер взлетел, бот ещё не реассертил» не
            # пускало никого на SSH-к-хосту. Идемпотентно.
            awg.ensure_ssh_failsafe()
        except awg.AwgError:
            pass

    # ── Обновления бота (self-update) ────────────────────────────────────────

    _MUTE_KEY = "updates_muted"
    _NOTIFIED_KEY = "update_notified_tag"

    def updates_muted(self) -> bool:
        return self.db.get_state(self._MUTE_KEY) == "1"

    def mute_updates(self) -> None:
        """Выключить автоуведомления и стартовую проверку об обновлениях.
        Ручная проверка «Обновление бота» продолжает работать (единственный путь
        узнать/обновиться до будущего пункта настроек)."""
        self.db.set_state(self._MUTE_KEY, "1")

    def update_next(self):
        """Следующая доступная версия (updates.Release) или None. Сетевые ошибки
        гасим в None — фоновая задача/кнопка от них не падают."""
        try:
            return updates.next_release()
        except updates.UpdateError:
            return None

    def update_to_notify(self):
        """Для планировщика/старта: вернуть Release, о котором НАДО уведомить, и
        пометить его как уведомлённый (ровно один раз на версию). None, если
        уведомления заглушены, обновлять не на что, или про эту версию уже
        уведомляли. Помечаем ДО отправки — «не более одного раза» важнее, чем
        «гарантированно доставить» (миссы закрывает ручная кнопка)."""
        if self.updates_muted():
            return None
        nxt = self.update_next()
        if nxt is None:
            return None
        if self.db.get_state(self._NOTIFIED_KEY) == nxt.tag:
            return None
        self.db.set_state(self._NOTIFIED_KEY, nxt.tag)
        return nxt

    def apply_update(self, release) -> None:
        """Скачать ассет следующей версии, сверить sha256 и запустить апдейтер
        (он остановит и заменит сервис). UpdateError пробрасывается — обработчик
        покажет пользователю причину, сервис остаётся жив.

        Перед запуском пишем update_pending=tag: на следующем старте
        confirm_applied_update() сверит фактическую версию и отчитается админу."""
        blob = updates.download_asset(release)
        self.db.set_state("update_pending", release.tag)
        updates.apply(blob)

    def set_update_wait(self, chat_id: int, message_id: int) -> None:
        """Запомнить «дождись завершения»-сообщение: после рестарта новый процесс
        удалит его перед итоговым сообщением."""
        self.db.set_state("update_wait", f"{chat_id}:{message_id}")

    def pop_update_wait(self):
        """(chat_id, message_id) «дождись»-сообщения или None. Одноразово."""
        raw = self.db.get_state("update_wait")
        if not raw:
            return None
        self.db.set_state("update_wait", "")
        try:
            chat_s, msg_s = raw.split(":", 1)
            return int(chat_s), int(msg_s)
        except ValueError:
            return None

    def confirm_applied_update(self):
        """Стартовая сверка результата self-update. Если перед рестартом было
        запущено обновление (update_pending) — вернуть Notification с итогом и
        стереть флаг; иначе None. Успех: «успешно обновлен до X» + changelog
        установленной версии под катом + кнопка «В меню» (сообщение остаётся в
        истории; кнопка снимается своим хендлером, не редактируя текст).
        Сравнение семантическое (v1.1.1 == 1.1.1)."""
        pending = self.db.get_state("update_pending")
        if not pending:
            return None
        self.db.set_state("update_pending", "")
        want = updates.parse_version(pending)
        have = updates.parse_version(config.INSTALLED_VERSION)
        from awgbot.bot import texts
        from awgbot.bot import keyboards as kb
        if want is not None and want == have:
            body = updates.release_body(pending)
            return Notification(config.ADMIN_ID, texts.update_applied(pending, body),
                                reply_markup=kb.update_done_menu())
        return Notification(config.ADMIN_ID, texts.update_not_applied(
            pending, config.INSTALLED_VERSION), reply_markup=kb.update_done_menu())

    def detect_and_handle_restart(self) -> bool:
        """Сверяет StartedAt контейнера с сохранённым. Если изменился (был
        рестарт) — реконсиляция блокировок. Возвращает True, если был рестарт."""
        current = awg.container_started_at()
        if not current:
            return False
        stored = self.db.get_state("container_started_at")
        if current != stored:
            self.db.set_state("container_started_at", current)
            if stored is not None:                        # не первый запуск
                self.reconcile_blocks()
                self.reconcile_ssh_access()               # SSH-фильтр тоже слетел
                return True
        return False

    # ── Вьюхелперы для отображения ───────────────────────────────────────────

    def count_unassigned_app_devices(self) -> int:
        service_id = self.db.get_service_client_id()
        return self.db.count_devices(service_id)

    _FA_HINT_DISMISSED = "admin_fa_hint_dismissed"

    def admin_has_full_access_device(self) -> bool:
        """Есть ли назначенное ФА-устройство (оно всегда одно и на админ-клиенте)."""
        return self.find_full_access_device() is not None


    def admin_fa_hint_needed(self) -> bool:
        """Показывать ли подсветку «назначь устройство полного доступа»: пока
        админ её не проигнорировал И full-access устройство ещё не назначено.
        Проверяется при КАЖДОМ старте (не теряется при перезапуске до решения)."""
        if self.db.get_state(self._FA_HINT_DISMISSED) == "1":
            return False
        return not self.admin_has_full_access_device()

    def dismiss_admin_fa_hint(self) -> None:
        """«Игнорировать» — заглушить подсветку навсегда."""
        self.db.set_state(self._FA_HINT_DISMISSED, "1")

    def profile_traffic_limit(self, client_id: int) -> int:
        """Лимит трафика профиля-владельца (байты, 0 = безлимит) — для подсказки
        при задании лимита устройства."""
        c = self.db.get_client(client_id)
        return int(c.traffic_limit) if c else 0

    def clientstable_names(self) -> dict:
        """pubkey → имя из clientsTable awg (там full-access значится как
        «Admin [платформа]») — для подсказки админу при выборе устройства."""
        return {e.get("clientId"): e.get("userData", {}).get("clientName", "")
                for e in awg.read_clients_table()}

    def client_is_online(self, client_id: int) -> bool:
        for dev in self.db.list_devices(client_id):
            if timeutil.handshake_is_online(dev.last_handshake):
                return True
        return False

    def device_slots(self, client_id: int) -> tuple[int, int]:
        """(добавлено, лимит) — для подсветки «M из N»."""
        client = self.db.get_client(client_id)
        if client is None:
            return (0, 0)
        return (self.db.count_devices(client_id), client.device_limit)

    def is_only_device(self, device_id: int) -> bool:
        """True, если это единственное устройство своего клиента (удаление =
        потеря доступа к VPN и, возможно, к боту). Для устройств «без профиля»
        (служебный клиент) — всегда False: у них нет tg_id-владельца, который
        «потеряет доступ через этот VPN», предупреждение неприменимо."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return False
        if dev.client_id == self.db.get_service_client_id():
            return False
        return self.db.count_devices(dev.client_id) <= 1

    # ── Бэкап ────────────────────────────────────────────────────────────────

    def make_backup(self) -> list[str]:
        """Складывает в BACKUP_DIR копию БД + серверный awg0.conf + clientsTable
        (единственные копии этих файлов вне контейнера!). Возвращает пути.

        Если включено шифрование (config.BACKUP_ENCRYPTION_ENABLED) — каждый
        артефакт шифруется SecretBox и пишется как «*.enc»; открытые копии на
        диск НЕ ложатся (в БД приватные ключи bot-устройств — их нельзя оставлять
        в открытом виде ни на диске бот-хоста, ни при доставке в чат).
        Расшифровка — restore_backup.py на awg-хосте."""
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = timeutil.now().strftime("%Y%m%d_%H%M%S")

        # (имя_без_расширения, расширение, сырые байты) — собираем всё, потом
        # единообразно пишем: открытыми или зашифрованными.
        artifacts: list[tuple[str, bytes]] = []
        try:
            artifacts.append((f"bot_{stamp}.db", config.DB_PATH.read_bytes()))
        except OSError:
            pass
        try:
            conf = awg.read_file(config.CONF_PATH)
            artifacts.append((f"awg0_{stamp}.conf", conf.encode("utf-8")))
        except awg.AwgError:
            pass
        try:
            table = awg.read_file(config.CLIENTS_TABLE_PATH)
            artifacts.append((f"clientsTable_{stamp}.json", table.encode("utf-8")))
        except awg.AwgError:
            pass

        paths: list[str] = []
        encrypt = config.BACKUP_ENCRYPTION_ENABLED
        enc_kwargs = self._backup_enc_kwargs() if encrypt else None
        for name, raw in artifacts:
            try:
                if encrypt:
                    from awgbot.util import secrets_util
                    blob = secrets_util.encrypt(raw, **enc_kwargs)
                    dst = config.BACKUP_DIR / f"{name}.enc"
                    dst.write_bytes(blob)
                else:
                    dst = config.BACKUP_DIR / name
                    dst.write_bytes(raw)
                paths.append(str(dst))
            except Exception:                            # noqa: BLE001
                # один сбойный артефакт не должен ронять остальной бэкап
                continue
        return paths

    @staticmethod
    def _backup_enc_kwargs() -> dict:
        """kwargs для secrets_util.encrypt из конфига (passphrase важнее key,
        если по недосмотру заданы оба — детерминированнее для восстановления)."""
        from awgbot.util import secrets_util
        if config.BACKUP_PASSPHRASE:
            return {"passphrase": config.BACKUP_PASSPHRASE}
        return {"key": secrets_util.b64d(config.BACKUP_KEY)}

    # ── Статус сервера (мониторинг) ──────────────────────────────────────────

    def check_resource_alerts(self, metrics: dict) -> list["Notification"]:
        """Гистерезис загрузки хоста по СТРИКАМ (замерам подряд). Вызывается на
        каждом тике монитора с локальным снимком {cpu, ram, disk} (% или None).

        На каждый ресурс держим два счётчика в state: подряд-превышений и
        подряд-нормы. Значение ≥ порога двигает превышения (+1) и обнуляет норму;
        < порога — наоборот. Алерт «высокая загрузка» — когда превышения достигают
        RESOURCE_ALERT_STREAK и алерт ещё не активен; «отбой» — когда норма
        достигает того же порога и алерт активен. Симметрично вверх/вниз.

        При streak=5 и тике монитора 3 мин реакция ≤ 15 мин. Обычные Notification
        (force_sound=False) → в тихие часы без звука. None-метрика не двигает
        счётчики (нет данных ≠ норма)."""
        if not settings.get_bool("resource_alerts.enabled", True):
            return []
        streak_n = settings.get_int("app.monitoring.alert_streak", 5)
        thresholds = {
            "cpu": (settings.get_int("resource_alerts.thresholds_percent.cpu", 80), "CPU", "🖥"),
            "ram": (settings.get_int("resource_alerts.thresholds_percent.ram", 80), "RAM", "🧠"),
            "disk": (settings.get_int("resource_alerts.thresholds_percent.disk", 80), "Диск", "💽"),
        }
        notes: list[Notification] = []
        for key, (threshold, label, icon) in thresholds.items():
            value = metrics.get(key)
            if value is None:
                continue                       # нет данных — счётчики не трогаем
            hi_key = f"res_hi_{key}"           # подряд-превышений
            lo_key = f"res_lo_{key}"           # подряд-нормы
            armed_key = f"res_alert_{key}"     # "1" ⇔ алерт активен
            hi = int(self.db.get_state(hi_key) or 0)
            lo = int(self.db.get_state(lo_key) or 0)
            armed = self.db.get_state(armed_key) == "1"
            if value >= threshold:
                hi, lo = hi + 1, 0
                if hi >= streak_n and not armed:
                    self.db.set_state(armed_key, "1")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"⚠️ {icon} Высокая загрузка: {label} {value:.0f}% "
                        f"(порог {threshold}%, держится ≥{streak_n} замеров)."))
            else:
                lo, hi = lo + 1, 0
                if lo >= streak_n and armed:
                    self.db.set_state(armed_key, "0")
                    notes.append(Notification(
                        config.ADMIN_ID,
                        f"✅ {icon} {label} вернулся в норму: {value:.0f}% "
                        f"(ниже порога {threshold}%)."))
            self.db.set_state(hi_key, str(hi))
            self.db.set_state(lo_key, str(lo))
        return notes

    def server_status_cached(self) -> dict:
        """Статусный блок из state — ноль docker exec. Живость awg пишет монитор,
        started_at — детект рестарта, online_count — опросчик трафика, а метрики
        железа (CPU/RAM/диск) монитор снимает ЛОКАЛЬНО (co-located) — hostmetrics. Возраст
        метрик показываем в инфобоксе (обновляет монитор каждый тик, локально)."""
        from awgbot.runtime import hostmetrics
        ok_raw = self.db.get_state("last_server_ok")
        ok = None if ok_raw is None else (ok_raw == "1")
        started = timeutil.parse_docker_time(self.db.get_state("container_started_at") or "")
        uptime = timeutil.fmt_uptime(started) if started else None
        online_raw = self.db.get_state("online_count")
        online = int(online_raw) if online_raw is not None else None
        cpu = ram = disk = age_seconds = None
        snap = hostmetrics.get_host_metrics(self.db)
        if snap:
            cpu, ram, disk = snap.get("cpu"), snap.get("ram"), snap.get("disk")
            age_seconds = snap.get("age_seconds")
        return {"ok": ok, "uptime": uptime, "online_count": online,
                "cpu": cpu, "ram": ram, "disk": disk, "age_seconds": age_seconds}

    def server_ok(self) -> bool:
        """Живая проверка: если awg внутри контейнера ответил — контейнер
        заведомо запущен, отдельный container_running не нужен (1 exec, не 2)."""
        return awg.awg_responding()

    def server_ok_cached(self) -> bool:
        """Статус из state (пишет monitor-задача каждые MONITOR_MINUTES и старт).
        Для приветствий/меню: 0 docker exec, свежесть ≤3 мин — для строки
        «сервер работает» более чем достаточно."""
        cached = self.db.get_state("last_server_ok")
        if cached is not None:
            return cached == "1"
        return self.server_ok()          # state ещё не прогрет (первый старт)

    def refresh_status_now(self) -> None:
        """Внеплановое обновление статусного блока по требованию (кнопка админа):
        живой снимок awg-статуса + локальных метрик железа прямо в state, минуя
        ожидание следующего тика монитора. Блокирующий (docker exec + /proc) —
        вызывать через asyncio.to_thread. Меню/инфобокс потом читают из state
        как обычно (0 docker exec на показ)."""
        from awgbot.runtime import hostmetrics
        self.db.set_state("last_server_ok", "1" if self.server_ok() else "0")
        started = awg.container_started_at()
        if started:
            self.db.set_state("container_started_at", started)
        hostmetrics.collect_and_store(self.db)

    def restart_service(self) -> None:
        """Перезапуск контейнера (ТЗ 9.3). После — реконсиляция блокировок
        (DROP'ы слетели) через detect_and_handle_restart на следующем цикле,
        но сделаем и сразу."""
        awg.restart_container()
        # StartedAt изменится → сохранить и переналожить блокировки
        self.detect_and_handle_restart()
        self.reconcile_blocks()


__all__ = [
    "Services", "ServiceError", "LimitReached", "Notification",
    "ClientCreated", "ActivationResult", "DeviceCreated", "ExtendResult",
]
