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
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg
from awgbot.util import timeutil

log = logging.getLogger("awgbot.migration")

# Состояние живёт в server_state одним ключом. Значения намеренно строковые и
# читаемые: в базу заглядывают руками, и «running» понятнее единицы.
# Константы — в infra/db/core.py: фильтру видимости устройств состояние нужно прямо
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

    # ── подготовка переезда из интерфейса (docs/ROADMAP.md, п.8) ────────────
    # Прежде второй интерфейс заводили руками в app.yaml, и рычаг появлялся
    # только после этого. То есть о самой возможности человек узнавал, лишь
    # если уже знал.

    def migration_generation_driven(self) -> bool:
        """Идущий переезд затеян сменой поколения ядра? Такой нельзя ни
        подменить своим, ни догнать следующим обновлением: двойники рождены
        под конкретное поколение, и цель у переезда ровно одна."""
        from awgbot.infra import awglock
        return self.migration_running() and awglock.target_generation() > awglock.applied_generation()

    def migration_blocked_reason(self) -> str:
        """Почему нельзя затеять переезд по кнопке прямо сейчас. Пустая строка
        — можно."""
        from awgbot.infra import awglock
        if self.migration_generation_driven():
            return (f"идёт переезд на поколение {awglock.target_generation()}: "
                    "он начат сменой ядра и обязан завершиться первым")
        if self.migration_running():
            return "переезд уже идёт"
        if self.migration_available():
            return ("второй интерфейс уже поднят — начни переезд в «Обслуживании» "
                    "или отмени его")
        return ""

    _MIG_IF_CANDIDATES = tuple(f"awg{i}" for i in range(1, 10))

    def migration_prepare(self, port: Optional[int] = None,
                          subnet_prefix: str = "") -> dict:
        """Поднять второй интерфейс под переезд и записать ключи в app.yaml.

        Порт и подсеть — то, ради чего переезд и затевают; не названы — берём
        случайный высокий порт и первую свободную подсеть. Константы config
        читаются при старте, поэтому вызывающий обязан перезапустить бота.
        """
        import subprocess
        from awgbot.core import settings
        reason = self.migration_blocked_reason()
        if reason:
            raise ServiceErrorMigration(reason.capitalize())
        if port is not None and not (1 <= int(port) <= 65535):
            raise ServiceErrorMigration("порт — число от 1 до 65535")

        conf_dir = config.AWG_DIR
        busy_ifaces = {config.AWG_INTERFACE} | {
            p.stem for p in Path(conf_dir).glob("*.conf")} if Path(conf_dir).is_dir() else {
            config.AWG_INTERFACE}
        new_if = next((n for n in self._MIG_IF_CANDIDATES if n not in busy_ifaces), "")
        if not new_if:
            raise ServiceErrorMigration("свободного имени интерфейса не нашлось")
        new_prefix = subnet_prefix or self._free_subnet_prefix()
        if not new_prefix:
            raise ServiceErrorMigration("свободной подсети не нашлось")

        env = {**os.environ, "AWG_IF": new_if, "SUBNET_PREFIX": new_prefix,
               "AWG_QUICK_DIR": conf_dir}
        if port is not None:
            env["LISTEN_PORT"] = str(int(port))
        script = str(config.BASE_DIR / "install" / "awg-server-init.sh")
        try:
            proc = subprocess.run(["bash", script], capture_output=True, timeout=180, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            raise ServiceErrorMigration(f"второй интерфейс не поднят: {e}")
        out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
        if proc.returncode != 0:
            raise ServiceErrorMigration("второй интерфейс не поднят:\n"
                                        + "\n".join(out.splitlines()[-5:]))
        got_port = ""
        for line in proc.stdout.decode(errors="replace").splitlines():
            if line.startswith("LISTEN_PORT="):
                got_port = line.split("=", 1)[1].strip()
        try:
            settings.set_value("app.docker.migration_interface", new_if)
            settings.set_value("app.docker.migration_subnet_prefix", new_prefix)
        except Exception as e:                            # noqa: BLE001
            raise ServiceErrorMigration(f"ключи переезда не записаны в app.yaml: {e}")
        log.info("переезд: поднят %s (%s.0/24, порт %s)", new_if, new_prefix, got_port)
        return {"iface": new_if, "subnet": f"{new_prefix}.0/24", "port": got_port}

    def _free_subnet_prefix(self) -> str:
        """Первые три октета подсети, которой нет ни в одном конфиге awg."""
        base = config.SUBNET_PREFIX or "10.8.1"
        parts = base.split(".")
        if len(parts) != 3 or not parts[1].isdigit():
            return ""
        used = ""
        try:
            for conf in Path(config.AWG_DIR).glob("*.conf"):
                used += conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        for step in range(1, 10):
            cand = f"{parts[0]}.{int(parts[1]) + step}.{parts[2]}"
            if f"{cand}." not in used:
                return cand
        return ""

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
            raise ServiceErrorMigration(f"резолвер клиентов на новом интерфейсе не поднят: {e}")

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
            # держатель — тот же (docs/guest-role.md): set_device_holder снимает
            # приглашение, а у двойника его и нет
            self.db.set_device_holder(new_id, dev.holder_client_id)
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

        Зовётся РОВНО в момент, когда у двойника впервые появился хендшейк —
        из migration_watch или из опроса трафика, смотря кто увидел раньше.
        Отдельного хранилища отметок для этого не нужно: переход «было пусто →
        стало значение» случается по определению один раз, и право объявить его
        своим разыгрывается атомарно (claim_first_handshake).

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

    def migration_watch(self) -> list:
        """Частый тик: кто из двойников подключился впервые прямо сейчас.

        ЗАЧЕМ ОТДЕЛЬНО ОТ ОПРОСА ТРАФИКА. Ядро о хендшейках не уведомляет —
        узнать можно только спросив, и «сразу» упирается в частоту вопроса.
        Опрос трафика ходит раз в пять минут, и учащать его ради этого нельзя:
        он читает ВСЕ интерфейсы и на каждом устройстве считает дельты одной
        длинной транзакцией. Здесь же работы ровно на один вопрос — и только
        пока есть кого ждать.

        Поэтому: список ждущих сначала, dump потом. Когда переехали все (а это
        нормальное состояние почти всего окна переезда), тик не делает ни
        одного exec — выходит на первой строке. Переехавших не перепроверяем в
        принципе: у них хендшейк уже записан, и в выборку они не попадают.

        Считать здесь потребление незачем — это забота опроса, и пересечься мы
        можем только на одном поле, за которое честно тянем жребий.
        """
        if not self.migration_running() or not config.MIGRATION_INTERFACE:
            return []
        pending = self.db.pending_twins()             # фильтр в SQL, не в Python
        if not pending:
            return []
        iface = awg.iface_of(config.MIGRATION_INTERFACE)
        try:
            peers = {p["public_key"]: p for p in awg.show_dump(iface)}
        except awg.AwgError as e:
            log.warning("migration_watch: %s не опрошен: %s", iface, e)
            return []
        notes = []
        for dev in pending:
            p = peers.get(dev.public_key)
            if p is None or not p["last_handshake"]:
                continue
            if not self.db.claim_first_handshake(dev.id, p["last_handshake"]):
                continue                      # опрос трафика успел раньше
            note = self.migration_greeting(dev)
            if note is not None:
                notes.append(note)
        return notes

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
        self.private_dns_on_cancel()
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
            # флаг шлюза живёт на исходной строке (уникальный индекс не даст
            # двух); на финале он переезжает к двойнику вместе с остальным
            _real = self.db.get_device(old.id)
            if _real is not None and self.db.gateway_device() is not None \
                    and self.db.gateway_device().id == old.id:
                self.db.set_gateway(twin.id)
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
            self._promote_migration_interface()
        self.reconcile_ssh_access()
        return removed, dropped, failed

    _PROMOTED_KEY = "awg_promoted_iface"

    def _promote_migration_interface(self) -> str:
        """Второй интерфейс становится ОСНОВНЫМ: все устройства уже на нём.

        Без этого шага переезд заканчивался наполовину: пиры жили на новом
        интерфейсе, а бот по-прежнему считал основным старый — и каждое НОВОЕ
        устройство рождалось там, то есть на параметрах, ради ухода от которых
        всё и затевалось. При смене поколения это хуже вдвойне: новое
        устройство получало бы ядро, которое вот-вот перестанет обслуживаться.

        Переписываем деплой-значения в app.yaml (интерфейс, подсеть, порт,
        идентификатор протокола нового поколения), гасим старый интерфейс и
        поднимаем применённое поколение до цели переезда. Константы config
        читаются при старте, поэтому вызывающий обязан перезапустить бота —
        имя интерфейса возвращается именно для этого.
        """
        from awgbot.core import settings
        from awgbot.infra import awglock
        old_if, new_if = config.AWG_INTERFACE, config.MIGRATION_INTERFACE
        new_prefix = config.MIGRATION_SUBNET_PREFIX
        if not new_if or not new_prefix or new_if == old_if:
            return ""
        try:
            port = int(awg.read_server_params(iface=new_if)["listen_port"])
        except (awg.AwgError, KeyError, TypeError, ValueError) as e:
            log.warning("promote: порт %s не прочитан, основной интерфейс не меняю: %s",
                        new_if, e)
            return ""
        target, applied = awglock.target_generation(), awglock.applied_generation()
        try:
            settings.set_value("app.docker.interface", new_if)
            settings.set_value("app.network.subnet_prefix", new_prefix)
            settings.set_value("app.network.subnet_cidr", f"{new_prefix}.0/24")
            settings.set_value("app.network.server_port", port)
            # Идентификатор протокола вморожен в каждую выданную ссылку. Меняем
            # только вместе с поколением: у двойников он уже новый, и теперь
            # таким же должны рождаться все следующие устройства.
            if target > applied and awglock.protocol_id():
                settings.set_value("app.docker.app_container", awglock.protocol_id())
            settings.set_value("app.docker.migration_interface", "")
            settings.set_value("app.docker.migration_subnet_prefix", "")
        except Exception as e:                            # noqa: BLE001
            log.warning("promote: app.yaml не переписан: %s", e)
            return ""
        # DNS клиентов: адрес двойников становится адресом всех, старый — со
        # старым интерфейсом — снимается с резолвера.
        try:
            self.private_dns_on_promote(config.SUBNET_PREFIX)
        except Exception as e:                            # noqa: BLE001
            log.warning("promote: DNS клиентов не переписан: %s", e)
        if target > applied:
            awglock.write_state(applied=target, target=0)
        else:
            awglock.write_state(target=0)
        self._retire_interface(old_if)
        if target > applied:
            # Смена поколения: прежнюю сборку ядра держали до этого момента —
            # пока старый интерфейс обслуживал людей, путь назад был нужен.
            # Теперь она мусор в /usr/src и в DKMS.
            self._prune_old_kernel_builds()
        self.db.set_state(self._PROMOTED_KEY, new_if)
        log.info("переезд: основным интерфейсом стал %s (поколение %s)", new_if, target)
        return new_if

    @staticmethod
    def _prune_old_kernel_builds() -> None:
        import subprocess
        script = config.BASE_DIR / "install" / "awg-kernel-install.sh"
        lock = config.BASE_DIR / "install" / "awg.lock"
        try:
            proc = subprocess.run(["bash", str(script), "prune"], capture_output=True,
                                  timeout=120, env={**os.environ, "AWG_LOCK": str(lock)})
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("prune: не запустилось: %s", e)
            return
        if proc.returncode != 0:
            log.warning("prune: прежние сборки не убраны: %s",
                        proc.stderr.decode(errors="replace").strip()[-200:])
        else:
            log.info("prune: %s", proc.stdout.decode(errors="replace").strip().splitlines()[-1:])

    @staticmethod
    def _retire_interface(name: str) -> None:
        """Опустить и снять с автозагрузки интерфейс, на котором не осталось
        пиров. Конфиг НЕ удаляем: он единственный след прежних параметров, и
        стоит копейки, а понадобиться может при разборе."""
        import subprocess
        for argv in (["awg-quick", "down", name],
                     ["systemctl", "disable", f"awg-quick@{name}"]):
            try:
                subprocess.run(argv, capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("promote: %s не выполнено: %s", " ".join(argv), e)

    def pop_promoted_iface(self) -> str:
        """Имя интерфейса, ставшего основным на финале переезда, — один раз.
        Вызывающий показывает это админу и перезапускает бота: деплой-значения
        читаются при старте."""
        name = self.db.get_state(self._PROMOTED_KEY) or ""
        if name:
            self.db.set_state(self._PROMOTED_KEY, "")
        return name


class ServiceErrorMigration(Exception):
    """Отказ механики переезда. Отдельный тип, чтобы не утонуть в общих
    except ServiceError у вызывающих: здесь отказ означает «не начали», и
    молча продолжать нельзя."""
