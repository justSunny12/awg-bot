"""
devices.py — устройства: добавление/удаление с откатом, конфиги, друзья
(держатели), перевыпуск ключей, перепривязка к другому профилю.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg
from awgbot.domain import configgen
from awgbot.core.blocks import DeviceBlock
from awgbot.core.enums import SubStatus, FriendStatus
from awgbot.domain.services.types import (
    DeviceCreated, FriendActivation, LimitReached, ServiceError,
)


log = logging.getLogger("awgbot.services")


class DevicesMixin:
    # ── Устройства ───────────────────────────────────────────────────────────

    def add_device(self, client_id: int, name: str, traffic_limit: int = 0) -> DeviceCreated:
        """Поток 2: генерация ключей → аллокация IP → БД → awg.add_peer →
        конфиг. При сбое применения — откат БД.

        """
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
            # аллокация: занятые из БД + из живого конфига (учёт чужих пиров)
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

        # Профилю разрешён РФ-доступ — новое устройство сразу в режиме: человеку
        # обещано «включено для всех твоих устройств», и новое — не исключение
        # (решение 16.09.2026; до этого приходило выключенным, чтобы не ходить
        # через шлюз молча — но выключить одно устройство проще, чем каждый раз
        # включать). Двойник переезда наследует флаг при рождении.
        if not client.is_service and client.routing_allowed:
            self.db.update_device_fields(device_id, routing_on=1)
            self.reconcile_routing()

        # В окне переезда устройство заводится ПАРОЙ, как все остальные, и
        # человеку выдаётся новый конфиг. Пара нужна не для красоты: до
        # завершения переезд можно отменить, а отмена возвращает людей на старые
        # пиры. Роди мы только новый — отменять для этого человека было бы нечем,
        # и он остался бы единственным, кого откат выбрасывает.
        if self.migration_running():
            try:
                twin_id = self._birth_twin(self.db.get_device(device_id))
            except Exception as e:                        # noqa: BLE001
                log.warning("add_device: двойник для %s не создан: %s", name, e)
            else:
                if client.tg_id == config.ADMIN_ID:
                    self.reconcile_ssh_access()
                twin = self.db.get_device(twin_id)
                cfg = self.generate_config(twin_id)
                return DeviceCreated(device_id=twin_id, address=twin.address,
                                     vpn=cfg["vpn"], conf=cfg["conf"])

        cfg = configgen.generate(priv, pub, ip, server_params, iface=config.AWG_INTERFACE)
        # новое устройство админа → сразу открыть ему SSH-к-хосту (не ждать цикла)
        if client.tg_id == config.ADMIN_ID:
            self.reconcile_ssh_access()
        return DeviceCreated(device_id=device_id, address=ip, vpn=cfg["vpn"], conf=cfg["conf"])

    def remove_device(self, device_id: int) -> Optional[int]:
        """Удаление устройства: сервер → БД (в таком порядке, чтобы не осталось
        записи в БД без реального снятия пира).
        Возвращает friend_tg_id, если у устройства был активный друг (для
        уведомления, что доступ прекращён), иначе None."""
        dev = self.db.get_device(device_id)
        if dev is None:
            return None
        self._refuse_if_gateway(dev)
        friend_tg = dev.friend_tg_id if dev.friend_status == FriendStatus.ACTIVE else None
        # Снятие ПАРНОЕ. В окне переезда у устройства два пира на двух
        # интерфейсах; снять только видимый значит оставить второй работать —
        # призрачный доступ у того, кого человек считает удалённым. Ровно тот
        # класс, ради которого сверка перестала усыновлять неизвестных.
        for peer in self._device_pair(dev):
            try:
                awg.remove_peer(peer.public_key, iface=awg.iface_of(peer.iface))
            except awg.AwgError as e:
                raise ServiceError(f"Не удалось снять устройство на сервере: {e}")
        # DROP снимаем ПОСЛЕ успешного снятия пира: если remove_peer упал,
        # устройство осталось в конфиге и должно остаться заблокированным.
        # Снять обязательно — иначе осиротевшее правило заблокирует будущего
        # владельца этого IP (аллокатор переиспользует освободившиеся адреса).
        if int(dev.block_reason) != 0:
            for peer in self._device_pair(dev):
                try:
                    awg.unblock_ip(peer.address)
                except awg.AwgError:
                    pass
        for peer in self._device_pair(dev):
            if peer.id != device_id:
                self.db.delete_device(peer.id, archive_reason=None)
        self.db.delete_device(device_id)
        return friend_tg

    def generate_config(self, device_id: int, *, for_bundle: bool = False) -> dict:
        """Перевыпуск конфига устройства. Только для устройств, созданных ботом:
        приватный ключ есть лишь у них. Шлюзу ссылку не выдаём: его конфиг едет
        только внутри конфигурации шлюза (for_bundle)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if not for_bundle:
            self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError(
                "Это устройство создавал не бот — приватного ключа у него нет, "
                "выдать ссылку не из чего. Удали его и добавь новое через бота."
            )
        # Параметры берём у ТОГО интерфейса, где живёт пир. Общие отдали бы
        # конфигу двойника старый порт, старый серверный ключ и старую
        # обфускацию: превью выглядит нормально, а не подключается никто.
        iface = awg.iface_of(dev.iface)
        server_params = awg.read_server_params(iface=iface)
        return configgen.generate(dev.private_key, dev.public_key, dev.address,
                                  server_params, iface=iface)

    def rename_device(self, device_id: int, new_name: str) -> None:
        """Переименование устройства. Имя живёт только в нашей БД: сервер про
        имена не знает, в конфиге пира их нет."""
        new_name = new_name.strip()
        if not new_name:
            raise ServiceError("Имя не может быть пустым")
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        # Парно: иначе отмена переезда вернула бы старую строку со старым
        # именем, молча откатив переименование.
        for peer in self._device_pair(dev):
            self.db.update_device_fields(peer.id, name=new_name)

    # ── Друзья (роль invited): приглашение на управление одним устройством ────

    def make_device_friendly(self, device_id: int) -> str:
        """Помечает СУЩЕСТВУЮЩЕЕ устройство гостевым: генерит код друга, ставит
        pending. Возвращает код для пересылки. Требует bot-устройство (у app нет
        ссылки — другу нечего было бы выдать)."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError("Это устройство создавал не бот — передать его нельзя: "
                               "у бота нет ссылки, которую можно было бы выдать другу")
        if dev.friend_status == FriendStatus.ACTIVE:
            raise ServiceError("Устройством уже управляет друг")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def reissue_friend_code(self, device_id: int) -> str:
        """Перевыдать код друга — только пока приглашение не активировано."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        if dev.friend_status != FriendStatus.PENDING:
            raise ServiceError("Перевыдать код можно только для неактивированного приглашения")
        code = self._gen_friend_code()
        self.db.set_device_friend(device_id, friend_code=code, friend_status=FriendStatus.PENDING)
        return code

    def activate_friend(self, code: str, tg_id: int, tg_name: str = "") -> "FriendActivation":
        """Активация кода F…. Держателем становится профиль
        этого tg: гость (заводится при первом коде, имя — из Telegram) или
        обычный клиент. Правило одного дарителя: у держателя уже есть устройства
        от другого владельца → отказ other_donor, код не сгорает. Админ и своё
        устройство — отказ."""
        dev = self.db.get_device_by_friend_code(code)
        if dev is None or dev.friend_status != FriendStatus.PENDING:
            return FriendActivation(ok=False, reason="invalid")
        if tg_id == config.ADMIN_ID:
            return FriendActivation(ok=False, reason="already_user")
        holder = self.db.get_client_by_tg(tg_id)
        if holder is not None and holder.is_service:
            holder = None
        if holder is not None and holder.id == dev.client_id:
            return FriendActivation(ok=False, reason="own_device", device_id=dev.id,
                                    device_name=dev.name, holder=holder)
        held = self.db.list_held_devices(holder.id) if holder is not None else []
        if held and held[0].client_id != dev.client_id:
            return FriendActivation(ok=False, reason="other_donor", device_id=dev.id,
                                    device_name=dev.name, holder=holder,
                                    donor=self.db.get_client(held[0].client_id), held=held)
        if holder is None:
            # профильное имя — раз, при рождении; дальше человека зовут по
            # имени аккаунта (clients.tg_name), его ведёт middleware
            hid = self.db.create_guest_client(tg_id, (tg_name or "Друг").strip()[:64])
            holder = self.db.get_client(hid)
        for peer in self._device_pair(dev):
            self.db.set_device_holder(peer.id, holder.id)
        if dev.routing_on:
            self.reconcile_routing()                  # адрес переезжает в набор держателя
        return FriendActivation(ok=True, reason="ok", device_id=dev.id, device_name=dev.name,
                                holder=holder, donor=self.db.get_client(dev.client_id),
                                held=self.db.list_held_devices(holder.id))

    def rekey_device(self, device_id: int) -> None:
        """Перевыпустить ключи устройства: прежний конфиг у людей перестаёт
        работать, имя и адрес те же. Нужно, когда устройство уходит от
        держателя к другому владельцу: иначе прежний
        держатель сохранил бы доступ по чужому теперь устройству. Сервер —
        снять старый пир, поставить новый; не поднялся — вернуть старый."""
        dev = self.db.get_device(device_id)
        if dev is None:
            raise ServiceError("Устройство не найдено")
        self._refuse_if_gateway(dev)
        if not dev.private_key:
            raise ServiceError("Это устройство создавал не бот — перевыпустить ключи нельзя")
        priv, pub = awg.gen_keypair()
        for peer in self._device_pair(dev):
            iface = awg.iface_of(peer.iface)
            try:
                awg.remove_peer(peer.public_key, iface=iface)
                awg.add_peer(pub, peer.preshared_key, peer.address, iface=iface)
            except awg.AwgError as e:
                try:
                    awg.add_peer(peer.public_key, peer.preshared_key, peer.address, iface=iface)
                except awg.AwgError:
                    pass
                raise ServiceError(f"Не удалось перевыпустить ключи на сервере: {e}")
            self.db.update_device_fields(peer.id, public_key=pub, private_key=priv)

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
        self._refuse_if_gateway(dev)
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
            # Парно: перенеси одну строку — и пара разорвётся между профилями,
            # старый пир останется у донора, а завершение переезда сольёт
            # трафик и заархивирует устройство не тому человеку.
            if dev.holder_client_id is not None and dev.holder_client_id != new_client_id:
                # Прежний держатель теряет не только управление, но и ДОСТУП:
                # ключи перевыпускаются (имя и адрес те же), его конфиг мёртв.
                # Внутри транзакции: не поднялся новый пир — БД не тронута.
                self.rekey_device(dev.id)
            for peer in self._device_pair(dev):
                self.db.reassign_device(peer.id, new_client_id)
                # Держатель снимается всегда: устройство переехало к другому
                # владельцу, а держать чужое можно только от одного дарителя
                # — оставить его значило бы нарушить это
                # правило руками админа. Стал владельцем сам — держать нечего.
                if dev.holder_client_id is not None:
                    self.db.set_device_holder(peer.id, None)
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
            # прежний держатель (если был и не стал владельцем) — ему сказать
            "holder_tg": (dev.holder_tg_id
                          if dev.holder_client_id not in (None, new_client_id) else None),
        }
