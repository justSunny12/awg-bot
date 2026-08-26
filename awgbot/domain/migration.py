"""
migration.py — переезд профилей на второй интерфейс (docs/ROADMAP.md, п.3).

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНТЕРФЕЙС. Всё, ради чего затевается перевыпуск — ротация
`Jc/S/H`, `RandomTrailers`, смена `ListenPort` и `dns1`, — это параметры уровня
ИНТЕРФЕЙСА. Два набора на одном интерфейсе не живут, поэтому переезд на месте
означал бы флаг-день: в момент переключения падают все, кто не успел. Причём
падают ровно те, до кого потом не достучаться — у кого Telegram работает через
этот же туннель.

Отсюда конструкция: рядом со старым интерфейсом поднимается новый, каждому
устройству рождается двойник, человек переимпортирует конфиг когда удобно, а
старый пир всё это время работает.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Здесь доменная механика: состояние, когорта, рождение
двойников, оба выхода. Показ прогресса и уведомления — в слое бота; рассылку
объявления админ делает руками существующим механизмом.

Примесь к Services, а не свободные функции: механика насквозь опирается на
self.db и на уже написанные операции (_device_set_block, reconcile_ssh_access), а
services.py к этому моменту и без того на две с половиной тысячи строк.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg
from awgbot.util import timeutil

log = logging.getLogger("awgbot.migration")

# Состояние живёт в server_state одним ключом. Значения намеренно строковые и
# читаемые: в базу заглядывают руками, и «running» понятнее единицы.
# Константы — в infra/db.py: фильтру видимости устройств состояние нужно прямо
# в запросе, а infra не может тянуть domain.
from awgbot.infra.db import MIGRATION_STATE_KEY as _STATE_KEY  # noqa: E402
from awgbot.infra.db import MIGRATION_RUNNING as STATE_RUNNING  # noqa: E402
STATE_OFF = ""

# Живым считается устройство, чей хендшейк не старше этого срока. Одно правило
# вместо пяти: неактивированные, бесустройственные и никогда не подключавшиеся
# отсекаются им же — у них хендшейка нет вовсе.
LIVE_DAYS = 14


@dataclass
class MigrationStart:
    """Итог включения рычага."""
    born: int = 0                 # сколько двойников рождено этим заходом
    already: int = 0              # сколько уже было (повторное включение)
    cohort_devices: int = 0
    cohort_clients: int = 0
    failed: list[str] = field(default_factory=list)
    # Рычаг реально включился? False — ни одного двойника не существует и ни
    # один не родился: типично «awg1 ещё не поднят». Включаться в этом состоянии
    # значило бы молча фоллбэчить всю выдачу на старые конфиги при включённом
    # рычаге.
    started: bool = True


@dataclass
class MigrationProgress:
    """Готовность переезда. Считается по ЗАМОРОЖЕННОЙ когорте."""
    clients_done: int = 0
    clients_total: int = 0
    devices_done: int = 0
    devices_total: int = 0

    @property
    def complete(self) -> bool:
        # Пустая когорта — это завершённость, а не «ещё не начали»: живых пиров
        # не было вовсе, переезжать некому.
        return self.devices_done >= self.devices_total


class MigrationMixin:
    """Механика переезда. Подмешивается в Services."""

    # ── состояние ────────────────────────────────────────────────────────────

    def migration_available(self) -> bool:
        """Рычаг вообще существует? Нужны ОБА ключа: интерфейс без подсети
        нечем адресовать, подсеть без интерфейса не к чему привязать."""
        return bool(config.MIGRATION_INTERFACE and config.MIGRATION_SUBNET_PREFIX)

    def migration_state(self) -> str:
        if not self.migration_available():
            return STATE_OFF
        return self.db.get_state(_STATE_KEY) or STATE_OFF

    def migration_running(self) -> bool:
        return self.migration_state() == STATE_RUNNING

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
            ip = self.db.allocate_ip(
                subnet_prefix=config.MIGRATION_SUBNET_PREFIX,
                occupied_extra=occupied_live,
                start_host=config.IP_HOST_START,
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
                raise ServiceErrorMigration(f"конфликт адресов: {e}")
            self.db.update_device_fields(new_id, routing_on=int(dev.routing_on))
            self._inherit_friend(dev, new_id)
            try:
                awg.add_peer(pub, psk, ip, iface=iface)
            except awg.AwgError:
                self.db.delete_device(new_id, archive_reason=None)   # откат, не архив
                raise
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
            self.db.set_device_friend(new_id, friend_tg_id=dev.friend_tg_id,
                                      friend_status=FriendStatus.ACTIVE)
            return
        self.db.set_device_friend(new_id, friend_code=dev.friend_code,
                                  friend_status=dev.friend_status)
        self.db.set_device_friend(dev.id)                 # снять код со старого

    # ── прогресс ─────────────────────────────────────────────────────────────

    def migration_progress(self) -> MigrationProgress:
        """Готовность по замороженной когорте. Переехал ⇔ на двойнике был
        хендшейк; клиент переехал ⇔ переехали ВСЕ его живые устройства («хотя бы
        одно» дало бы число, которое врёт)."""
        cohort = self.db.cohort_ids()
        if not cohort:
            return MigrationProgress()
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}

        done_by_client: dict[int, list[bool]] = {}
        devices_done = 0
        for old_id in cohort:
            old = by_id.get(old_id)
            if old is None:
                continue                                  # удалено в окне — выбыло
            twin = by_id.get(twins.get(old_id, -1))
            moved = bool(twin and twin.last_handshake)
            devices_done += int(moved)
            done_by_client.setdefault(old.client_id, []).append(moved)

        return MigrationProgress(
            clients_done=sum(1 for flags in done_by_client.values() if all(flags)),
            clients_total=len(done_by_client),
            devices_done=devices_done,
            devices_total=sum(len(f) for f in done_by_client.values()),
        )

    def migration_pending(self) -> list[tuple[str, str]]:
        """Кто ещё не переехал: [(профиль, устройство), ...].

        Реальность выглядит как «11 из 12» в течение недели, и админу нужно
        видеть не только что кто-то отстал, но и кто именно.
        """
        cohort = self.db.cohort_ids()
        if not cohort:
            return []
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}
        out: list[tuple[str, str]] = []
        for old_id in sorted(cohort):
            old = by_id.get(old_id)
            if old is None:
                continue
            twin = by_id.get(twins.get(old_id, -1))
            if twin and twin.last_handshake:
                continue
            client = self.db.get_client(old.client_id)
            out.append((client.name if client else "?", old.name))
        return out

    def migration_client_progress(self, client_id: int) -> tuple[int, int, int]:
        """(переехало, живых в когорте, всего устройств) для одного профиля.

        Третье число считается по НОВОМУ интерфейсу — парами, а не строками: в
        окне каждое устройство задвоено, и наивный счёт показал бы «всего 6» на
        трёхустройственном профиле.
        """
        cohort = self.db.cohort_ids()
        twins = self.db.twins_by_origin()
        devices = [d for d in self.db.list_all_devices() if d.client_id == client_id]
        by_id = {d.id: d for d in devices}
        live_ids = [d.id for d in devices if d.id in cohort]
        done = 0
        for old_id in live_ids:
            twin_id = twins.get(old_id)
            twin = by_id.get(twin_id) if twin_id else None
            done += int(bool(twin and twin.last_handshake))
        # «Всего» — по ВИДИМЫМ строкам, а не по парам: непарная старая строка
        # (рождение двойника упало) видна человеку и обязана попасть в счёт.
        total = len(self.db.list_devices(client_id))
        return done, len(live_ids), total

    # ── поздравление пользователю ────────────────────────────────────────────

    def migration_greeting(self, dev):
        """Notification «всё получилось» по устройству, если она уместна.

        Зовётся из poll_traffic РОВНО в момент, когда у двойника впервые
        появился хендшейк. Отдельного хранилища отметок для этого не нужно:
        переход «было пусто → стало значение» случается по определению один раз,
        и опрос сам его и обнаруживает. Периодическая перепроверка всех устройств
        существовала только затем, чтобы этот момент не пропустить, — а он не
        пропускается.

        Адресат — тот, у кого конфиг НА РУКАХ: для расшаренного устройства это
        друг, а не владелец. Владелец его отдал, в приложении оно у друга, и
        просьба удалить старый профиль осмысленна только для него.

        Громким не помечаем — в тихие часы уйдёт беззвучно, как и всё, что не
        требует немедленной реакции.
        """
        from awgbot.domain.services import Notification
        from awgbot.bot import texts
        if dev.twin_of is None or not self.migration_running():
            return None
        target = (dev.friend_tg_id
                  if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id
                  else None)
        if target is None:
            client = self.db.get_client(dev.client_id)
            target = client.tg_id if client else None
        if not target:
            return None                       # профиль без Telegram — некому
        return Notification(target, texts.migration_hello(dev.name))

    # ── уведомление о готовности ─────────────────────────────────────────────

    _READY_ANNOUNCED = "migration_ready_announced"

    def migration_ready_alerts(self) -> list:
        """Все живые пиры переехали — сказать один раз.

        Та же дисциплина, что у докладов об источниках списков: доклад на СМЕНУ
        состояния, не на тик. Иначе «готово» приходило бы каждые три минуты, а
        флаг сбрасывается при следующем включении рычага — следующий переезд
        сообщит о своей готовности сам.

        Уведомление ничего не завершает: снести старый интерфейс и переключить
        дефолт — решение админа, и принимать его за него нельзя.
        """
        from awgbot.domain.services import Notification
        from awgbot.bot import texts
        if not self.migration_running():
            if self.db.get_state(self._READY_ANNOUNCED):
                self.db.set_state(self._READY_ANNOUNCED, "")
            return []
        p = self.migration_progress()
        if p.clients_total == 0 or not p.complete:
            return []
        if self.db.get_state(self._READY_ANNOUNCED) == "1":
            return []
        self.db.set_state(self._READY_ANNOUNCED, "1")
        return [Notification(config.ADMIN_ID, texts.migration_ready(p))]

    # ── выходы ───────────────────────────────────────────────────────────────

    def migration_cancel(self) -> int:
        """Отменить переезд. Возвращает число уже переехавших устройств.

        Коннекты новых пиров НЕ роняем: люди на них сидят прямо сейчас, и рвать
        связь ради отката — ровно тот вред, которого отмена должна избежать. Они
        остаются жить, но выдача возвращается на старые конфиги, а из
        пользовательского списка новые прячутся (фильтрация — в слое бота).
        С ними вопрос решается индивидуально.

        Выключение обязано быть безопасным в ЛЮБОЙ момент: это путь отката всей
        затеи, и он же страховка первого включения, когда админ проверяет
        переезд на себе.
        """
        devices = {d.id: d for d in self.db.list_all_devices()}
        moved = 0
        for d in devices.values():
            if d.twin_of is None:
                continue
            if d.last_handshake:
                moved += 1
            # Невыбранный инвайт-код при рождении ПЕРЕНОСИЛИ на двойника — при
            # отмене переносим обратно: двойник прячется, и активация ссылки
            # включила бы невидимую строку, а владелец видел бы устройство
            # не-гостевым.
            if d.friend_code and d.friend_status != FriendStatus.ACTIVE \
                    and d.twin_of in devices:
                self.db.set_device_friend(d.twin_of, friend_code=d.friend_code,
                                          friend_status=d.friend_status)
                self.db.set_device_friend(d.id)
        self.db.set_state(_STATE_KEY, STATE_OFF)
        self.db.cohort_clear()
        return moved

    def migration_moved_devices(self) -> list:
        """Двойники, на которых уже был хендшейк, — переехавшие. Без оглядки на
        состояние рычага: подтверждение отмены обязано знать их число, а
        orphan-список отвечает на другой вопрос и только после отмены."""
        return [d for d in self.db.list_all_devices()
                if d.twin_of is not None and d.last_handshake]

    def migration_finish_preview(self) -> tuple[int, list[str]]:
        """Что будет при завершении: (сколько пар закроется, кого уронит).

        Отдельно от самого завершения, потому что подтверждение обязано
        называть уронённых ДО нажатия, а не после: между решением и нажатием
        легко забыть, о ком речь, а отказ необратим.
        """
        pairs, dropped = self._finish_plan()
        return len(pairs), dropped

    def _finish_plan(self) -> tuple[list[tuple], list[str]]:
        """Пары к закрытию и имена тех, кто ещё не переехал. Общий расчёт для
        предпросмотра и самого завершения — чтобы подтверждение не могло
        разойтись с тем, что произойдёт."""
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}
        pairs: list[tuple] = []
        dropped: list[str] = []
        for old_id, twin_id in twins.items():
            old, twin = by_id.get(old_id), by_id.get(twin_id)
            if old is None or twin is None:
                continue
            pairs.append((old, twin))
            if not twin.last_handshake:
                client = self.db.get_client(old.client_id)
                dropped.append(f"{client.name if client else '?'} — {old.name}")
        return pairs, dropped

    def migration_orphan_twins(self) -> list:
        """Двойники, пережившие отмену, — те, на которых успели подключиться.

        Список нужен админу, чтобы «решать индивидуально» было где: из
        пользовательского интерфейса они скрыты, и иначе о них просто негде
        вспомнить.
        """
        if self.migration_running():
            return []
        return [d for d in self.db.list_all_devices()
                if d.twin_of is not None and d.last_handshake]

    def migration_finish(self) -> tuple[int, list[str], list[str]]:
        """Завершить переезд: снять старые пиры, слить историю, погасить рычаг.

        Возвращает (снято, уронены поимённо, НЕ закрыты из-за сервера).

        Непереехавшие теряют коннект — это цена завершения, и подтверждение
        обязано называть их поимённо ДО нажатия. Но потеря не навсегда: двойники
        у них живы, вернуть человека значит выдать ему конфиг.

        Порядок в паре — СЕРВЕР → БД, как у remove_device: снятие пира не
        удалось — пару не трогаем вовсе. Иначе пир остаётся в конфиге без строки
        в БД, и следующая сверка тащит его в карантин с тревогой. Слияние
        истории — только ПОСЛЕ успешного снятия: сделай его до, и повторное
        завершение сложило бы трафик дважды.

        Остались незакрытые пары — рычаг НЕ гасим: повторное завершение доберёт
        только их (закрытые уже без twin_of), а «завершено» при живых старых
        пирах было бы неправдой.

        Строки уходят в архив с явной причиной, а не удаляются тихо: иначе потом
        не восстановить, кто отвалился и почему.
        """
        pairs, dropped = self._finish_plan()
        removed = 0
        failed: list[str] = []

        for old, twin in pairs:
            try:
                awg.remove_peer(old.public_key, iface=awg.iface_of(old.iface))
            except awg.AwgError as e:
                log.warning("migration_finish: пир %s не снят, пара оставлена: %s",
                            old.name, e)
                failed.append(old.name)
                continue
            removed += 1
            self.db.merge_traffic(old.id, twin.id)
            if int(old.block_reason) != 0:
                try:
                    awg.unblock_ip(old.address)           # осиротевший DROP снять
                except awg.AwgError:
                    pass
            self.db.delete_device(old.id, archive_reason="миграция")
            self.db.update_device_fields(twin.id, twin_of=None)

        # Висячие ссылки расцепляем ЗДЕСЬ же: старую строку пары могла удалить
        # сверка ещё в окне, и _finish_plan такую пару не видит. Оставь twin_of —
        # и после выключения рычага фильтр видимости спрячет двойника из всех
        # списков навсегда: пир работает, а устройства нет ни у кого.
        by_id = {d.id for d in self.db.list_all_devices()}
        for d in self.db.list_all_devices():
            if d.twin_of is not None and d.twin_of not in by_id:
                self.db.update_device_fields(d.id, twin_of=None)

        if not failed:
            self.db.set_state(_STATE_KEY, STATE_OFF)
            self.db.cohort_clear()
        self.reconcile_ssh_access()
        return removed, dropped, failed


class ServiceErrorMigration(Exception):
    """Отказ механики переезда. Отдельный тип, чтобы не утонуть в общих
    except ServiceError у вызывающих: здесь отказ означает «не начали», и
    молча продолжать нельзя."""
