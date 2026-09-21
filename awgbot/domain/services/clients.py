"""
clients.py — инвайты и клиенты: создание, активация, апгрейд гостя,
клиентская запись админа.
"""
from __future__ import annotations

import secrets
import string
from typing import Optional

from awgbot.core import config
from awgbot.util import timeutil
from awgbot.core.blocks import DeviceBlock
from awgbot.core.enums import SubStatus, ActivationStatus, PeriodKind
from awgbot.domain.services.types import (
    ActivationResult, ClientCreated, DeviceCreated, GuestUpgrade, ServiceError,
)


class ClientsMixin:
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
        credit = self._pause_credit(None, period_kind).after   # первый оплаченный период
        if credit:
            self.db.set_pause_balance(cid, credit)
        return ClientCreated(client_id=cid, invite_code=invite, period_end=end)

    def activate_client(self, invite_code: str, tg_id: int) -> ActivationResult:
        existing = self.db.get_client_by_tg(tg_id)
        if existing is not None and not existing.is_service and not existing.is_guest:
            return ActivationResult(ok=False, reason="already_has_access", client=existing)
        row = self.db.get_client_by_invite(invite_code)
        if row is None:
            return ActivationResult(ok=False, reason="invalid")
        upgrade = None
        # Переход гостя и активация — один коммит: гостевой профиль удаляется
        # раньше (tg_id занят), и упасть между этим и активацией значило бы
        # оставить человека без профиля, а устройства — у никого.
        with self.db.transaction():
            if existing is not None and existing.is_guest:
                upgrade = self._upgrade_guest(existing, row.id)
            self.db.activate_client(row.id, tg_id)
        if upgrade is not None and upgrade.moved:
            self.reconcile_routing()                  # наборы — после коммита
        return ActivationResult(ok=True, reason="ok", client=self.db.get_client(row.id),
                                upgrade=upgrade)

    def _upgrade_guest(self, guest, new_client_id: int) -> "GuestUpgrade":
        """Гость становится владельцем (концепт «гость»): ВСЕ переданные ему
        устройства переходят в новый профиль независимо от лимита («3 из 2»
        честно), слоты дарителю возвращаются, управлять ими он больше не может.
        Пиры не трогаются. Личный список адресов едет с гостем. Гостевой
        профиль закрывается — иначе tg_id занят и активация не пройдёт.
        Только БД: вызывается внутри транзакции activate_client."""
        held = self.db.list_held_devices(guest.id)
        donor = self.db.get_client(held[0].client_id) if held else None
        for dev in held:
            for peer in self._device_pair(dev):
                self.db.update_device_fields(peer.id, client_id=new_client_id,
                                             holder_client_id=None)
            self._recompute_device_blocks(dev.id, new_client_id)
        self.db.move_routing_domains(guest.id, new_client_id)
        self.db.delete_client(guest.id, archive_reason="upgraded")
        return GuestUpgrade(donor=donor, moved=held)

    def _recompute_device_blocks(self, device_id: int, client_id: int) -> None:
        """Каскадные биты прежнего владельца (истечение, пауза) с устройства
        снять и наложить по новому: устройство переехало вместе с записью, а
        бит — это состояние подписки, которая осталась у прежнего."""
        client = self.db.get_client(client_id)
        if client is None or self.db.get_device(device_id) is None:
            return
        for bit, want in ((DeviceBlock.EXPIRY, client.status == SubStatus.EXPIRED),
                          (DeviceBlock.PAUSED, client.is_paused)):
            if want:
                self._device_set_block(device_id, bit)
            else:
                self._device_clear_block(device_id, bit)

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

    # ── клиентская запись админа ─────────────────────────────────────────────

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

    _ADMIN_BOOTSTRAP_KEY = "admin_bootstrap_device"

    def bootstrap_admin_device(self) -> Optional["DeviceCreated"]:
        """Первое устройство админа заводим САМИ, ровно один раз.

        Прямое следствие карантина: пир, созданный в обход бота, больше не
        усыновляется, а становится тревогой. Значит взять доступ «снаружи» —
        приложением или руками в конфиге — больше нельзя, а он нужен: админ
        ходит в Telegram через этот же VPN, и без устройства он не достучится
        до бота, чтобы завести себе устройство. Замкнутый круг размыкаем здесь.

        Ровно один раз, и по двум признакам сразу: метка в state И отсутствие
        устройств. Метка одна не годится для уже работающих установок (её там
        нет, а устройства есть — получили бы лишнее). Отсутствие устройств одно
        не годится для админа, который СОЗНАТЕЛЬНО удалил у себя всё: бот
        возвращал бы удалённое на каждом рестарте.

        Метку ставим только после успеха: не поднялся awg — повторим на
        следующем старте, а не потеряем бутстрап навсегда.
        """
        if self.db.get_state(self._ADMIN_BOOTSTRAP_KEY) == "1":
            return None
        cid = self.ensure_admin_client()
        if self.db.count_devices(cid):
            self.db.set_state(self._ADMIN_BOOTSTRAP_KEY, "1")   # уже есть — считаем сделанным
            return None
        created = self.add_device(cid, "Админ")
        self.db.set_state(self._ADMIN_BOOTSTRAP_KEY, "1")
        return created

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
