"""start.py — часть пакета migration (см. __init__): когорта, включение рычага,
рождение двойников."""
from __future__ import annotations

import sqlite3

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg
from awgbot.util import timeutil

from .common import log, _STATE_KEY, STATE_RUNNING, LIVE_DAYS, MigrationStart, ServiceErrorMigration


class MigrationStartMixin:
    """Когорта, включение рычага, рождение двойников."""

    # ── когорта ──────────────────────────────────────────────────────────────

    def _is_live(self, dev) -> bool:
        """Хендшейк не старше LIVE_DAYS. Опора — device_traffic.last_handshake:
        он персистентный и намеренно «не затирается пустым», то есть переживает
        пересоздание интерфейса. Живой сигнал из ядра для этого не годится — он
        обнуляется вместе с интерфейсом."""
        hs = dev.last_handshake
        if not hs:
            return False
        return (timeutil.now().timestamp() - int(hs)) <= LIVE_DAYS * 86400

    def _migratable(self) -> list:
        """Устройства, которым положен двойник: все, КРОМЕ карантина.

        Мёртвым двойники тоже нужны: переезд задуман бесшовным, отказаться от
        него нельзя, а оживший пир должен найти себя на новом интерфейсе без
        отдельного вмешательства.

        Карантин исключён по существу: у подхваченных с сервера пиров нет
        приватного ключа, генерить им конфиг нечем, и они вообще не наши.
        """
        service_id = self.db.get_service_client_id()
        return [d for d in self.db.list_all_devices()
                if d.client_id != service_id
                and d.twin_of is None
                and awg.iface_of(d.iface) != config.MIGRATION_INTERFACE
                and d.private_key]

    # ── включение рычага ─────────────────────────────────────────────────────

    def migration_start_preview(self) -> tuple[int, int, int]:
        """Что произойдёт при включении: (профилей, устройств в когорте,
        сколько профилей предстоит создать). НИЧЕГО не меняет.

        Когорта считается ровно так же, как её заморозит старт, — иначе
        подтверждение обещало бы один знаменатель, а прогресс показывал другой.
        Уже замороженную (повтор после сбоя) берём как есть, не пересобирая.
        """
        origins = self._migratable()
        frozen = self.db.cohort_ids()
        live = ([d for d in origins if d.id in frozen] if frozen
                else [d for d in origins if self._is_live(d)])
        existing = self.db.twins_by_origin()
        to_birth = sum(1 for d in origins if d.id not in existing)
        return len({d.client_id for d in live}), len(live), to_birth

    def migration_start(self) -> MigrationStart:
        """Включить рычаг: заморозить когорту и родить двойников.

        ИДЕМПОТЕНТНО. Рождение N двойников — это N раз «сгенерировать ключи →
        выделить адрес → записать в БД → добавить пира», а не одна транзакция.
        Сбой на середине (сервер моргнул, адреса кончились) обязан лечиться
        повторным включением: докидываем тех, у кого двойника ещё нет, по
        twin_of, ничего не задваивая.
        """
        if not self.migration_available():
            raise ServiceErrorMigration("Переезд не настроен: пустые ключи в app.yaml")
        # Свой DNS двойников — ДО рождения: конфиг двойника рождается с этим
        # адресом, и выдать его раньше, чем на нём кто-то отвечает, нельзя.
        try:
            self.private_dns_on_migration_start(config.MIGRATION_SUBNET_PREFIX)
        except Exception as e:                            # noqa: BLE001
            raise ServiceErrorMigration(f"резолвер пользователей на новом интерфейсе не поднят: {e}")

        origins = self._migratable()
        existing = self.db.twins_by_origin()
        res = MigrationStart()

        # Когорту замораживаем ПЕРВЫМ делом и только при первом включении:
        # повторный заход после сбоя не должен пересобрать её по свежим
        # хендшейкам — за это время кто-то мог ожить или замолчать, и
        # знаменатель поехал бы.
        if not self.db.cohort_ids():
            live = [d for d in origins if self._is_live(d)]
            self.db.cohort_set([d.id for d in live])
            res.cohort_devices = len(live)
            res.cohort_clients = len({d.client_id for d in live})
        else:
            ids = self.db.cohort_ids()
            res.cohort_devices = len(ids)
            res.cohort_clients = len({d.client_id for d in origins if d.id in ids})

        for dev in origins:
            if dev.id in existing:
                res.already += 1
                continue
            try:
                self._birth_twin(dev)
                res.born += 1
            except Exception as e:                        # noqa: BLE001
                log.warning("migration: двойник для %s (id=%s) не создан: %s",
                            dev.name, dev.id, e)
                res.failed.append(dev.name)

        if res.born == 0 and res.already == 0 and res.failed:
            # Не родился НИ ОДИН, и опереться не на что — рычаг не включаем.
            # Когорту размораживаем: она заморожена этим же заходом, а к
            # следующей попытке состав живых мог измениться.
            self.db.cohort_clear()
            res.started = False
            return res

        self.db.set_state(_STATE_KEY, STATE_RUNNING)
        self.db.set_state(self._READY_ANNOUNCED, "")   # следующий переезд доложит сам
        # Админ переезжает первым и проверяет собой всю затею — SSH-фильтр
        # обязан знать его новый адрес ДО того, как он переимпортирует конфиг.
        self.reconcile_ssh_access()
        return res

    def _birth_twin(self, dev) -> int:
        """Родить двойника одному устройству — копию по всему, кроме транспорта.

        Каждая неперенесённая строка это молчаливая регрессия, а не заметный
        отказ: человек подключается, всё «работает», и только потом выясняется,
        что режим выключился, лимит пропал или друг потерял доступ.
        """
        iface = config.MIGRATION_INTERFACE
        with awg.mutation_lock:
            occupied_live = awg.read_occupied_ips(iface)
            # Второй интерфейс ставит awg-server-init.sh: сервер на .1 всегда,
            # даже если у первого (прежняя раскладка) он на .0 и ip_host_start=1
            ip = self.db.allocate_ip(
                subnet_prefix=config.MIGRATION_SUBNET_PREFIX,
                occupied_extra=occupied_live,
                start_host=max(int(config.IP_HOST_START), 2),
                end_host=config.IP_HOST_END,
            )
            priv, pub = awg.gen_keypair()
            psk = awg.read_server_params(iface=iface)["psk"]
            try:
                new_id = self.db.create_device(
                    dev.client_id, dev.name, pub, psk, ip, private_key=priv,
                    traffic_limit=dev.traffic_limit,
                    iface=iface, twin_of=dev.id,
                    # Заблокированный обязан родиться заблокированным: ждать
                    # ближайшего reconcile_blocks значило бы подарить окно в
                    # минуты, за которое переезд оказывается амнистией.
                    block_reason=int(dev.block_reason),
                )
            except sqlite3.IntegrityError as e:
                refused = self.reserve_refusal(e) if hasattr(self, "reserve_refusal") else None
                if refused is not None:                    # следование: решения пишет бот на резерве
                    raise refused from e
                raise ServiceErrorMigration(f"конфликт адресов: {e}")
            self.db.update_device_fields(new_id, routing_on=int(dev.routing_on))
            try:
                awg.add_peer(pub, psk, ip, iface=iface)
            except awg.AwgError:
                self.db.delete_device(new_id, archive_reason=None)   # откат, не архив
                raise
            # Инвайт-код переносится ПОСЛЕ пира: раньше он стирался у оригинала
            # до add_peer, и откат оставлял ссылку друга мёртвой
            self._inherit_friend(dev, new_id)
        if int(dev.block_reason) != 0:
            try:
                awg.block_ip(ip)
            except awg.AwgError:
                pass                                      # доберёт reconcile_blocks
        return new_id

    def _inherit_friend(self, dev, new_id: int) -> None:
        """Гостевой доступ у двойника.

        Активная связь КОПИРУЕТСЯ — друг остаётся другом того же устройства.

        Невыбранный инвайт-код ПЕРЕНОСИТСЯ, а не копируется: поиск устройства по
        коду делает fetchone по неуникальной колонке, и с двумя строками под
        одним кодом активация попала бы в неопределённую из них. Отказ был бы
        отложенным и необъяснимым — всплыл бы, когда человек наконец нажмёт на
        присланную ссылку.
        """
        if not dev.friend_status:
            return
        if dev.friend_status == FriendStatus.ACTIVE:
            # держатель — тот же: set_device_holder снимает
            # приглашение, а у двойника его и нет
            self.db.set_device_holder(new_id, dev.holder_client_id)
            return
        self.db.set_device_friend(new_id, friend_code=dev.friend_code,
                                  friend_status=dev.friend_status)
        self.db.set_device_friend(dev.id)                 # снять код со старого
