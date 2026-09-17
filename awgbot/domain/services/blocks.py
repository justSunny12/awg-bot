"""
blocks.py — блокировки: биты причин, ручные блокировки (админ/клиент),
изменение лимитов потребления с авто-разблокировкой.
"""
from __future__ import annotations

from typing import Optional

from awgbot.core import config
from awgbot.infra import awg
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core.enums import PauseMode, FriendStatus
from awgbot.domain.services.types import Notification, ServiceError
from awgbot.domain.services.base import _e


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


class BlocksMixin:
    # ── Блокировки (битовые маски причин) ────────────────────────────────────
    # is_blocked как отдельного поля нет: заблокирован ⇔ block_reason != 0.
    # IP физически режем/снимаем по ИТОГОВОМУ состоянию маски: DROP ставим, когда
    # появляется хоть один бит; снимаем — только когда сброшены ВСЕ.

    def _device_pair(self, dev, twins: Optional[dict] = None) -> list:
        """Устройство и его двойник по переезду — в порядке [сам, второй].

        Вне окна переезда список из одного элемента, и все операции ведут себя
        ровно как прежде. Внутри окна у устройства ДВА пира на двух интерфейсах,
        и мутации обязаны быть парными: заблокировать один, сняв другой,
        значит подарить доступ; удалить один — оставить призрачный.
        """
        pair = [dev]
        if dev.twin_of:
            other = self.db.get_device(dev.twin_of)
            if other is not None:
                pair.append(other)
            return pair
        twin_id = (twins if twins is not None else self.db.twins_by_origin()).get(dev.id)
        if twin_id:
            other = self.db.get_device(twin_id)
            if other is not None:
                pair.append(other)
        return pair

    def _device_set_block(self, device_id: int, bit: DeviceBlock, twins: Optional[dict] = None) -> None:
        """Установить причину блокировки устройства (бит) и наложить DROP.
        twins — заранее снятая карта пар: в циклах избавляет от скана devices
        на каждое устройство."""
        dev = self.db.get_device(device_id)
        if dev is None or dev.is_gateway:
            return                                # шлюз не блокируется ни одной причиной
        new_mask = int(dev.block_reason) | int(bit)
        if new_mask == int(dev.block_reason):
            return
        for peer in self._device_pair(dev, twins):
            self.db.update_device_fields(peer.id, block_reason=new_mask)
            try:
                awg.block_ip(peer.address)      # идемпотентно
            except awg.AwgError:
                pass

    def _device_clear_block(self, device_id: int, bit: DeviceBlock, twins: Optional[dict] = None) -> None:
        """Снять причину (бит). Если не осталось причин — снять DROP."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return
        new_mask = int(dev.block_reason) & ~int(bit)
        if new_mask == int(dev.block_reason):
            return
        for peer in self._device_pair(dev, twins):
            self.db.update_device_fields(peer.id, block_reason=new_mask)
            if new_mask == 0:
                try:
                    awg.unblock_ip(peer.address)   # идемпотентно
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

    _TXT_GATEWAY_LOCKED = ("Это устройство — шлюз условной маршрутизации: его нельзя "
                           "заблокировать, удалить, передать или выдать ему ссылку. "
                           "Сначала «🛑 Не шлюз?» в карточке устройства.")

    def _refuse_if_gateway(self, dev) -> None:
        if dev is not None and dev.is_gateway:
            raise ServiceError(self._TXT_GATEWAY_LOCKED)

    def block_device_manual(self, device_id: int, bit: DeviceBlock,
                            notify: bool) -> list["Notification"]:
        """Ручной блок устройства заданным битом (ADMIN_SILENT/NOTIFIED/USER).
        notify=True → уведомить пользователя (клиента-владельца и/или друга).
        Тихий блок (notify=False) уведомлений не шлёт."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return []
        self._refuse_if_gateway(dev)
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
        if prev is not None:
            # Парно: проверка лимита считает СУММУ по паре против лимита пары,
            # и разъехавшиеся лимиты строк сделали бы её бессмысленной.
            for peer in self._device_pair(prev):
                self.db.update_device_fields(peer.id, traffic_limit=limit_bytes)
        else:
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
