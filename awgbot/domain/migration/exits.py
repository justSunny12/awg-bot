"""exits.py — часть пакета migration (см. __init__): выходы — отмена, завершение,
повышение второго интерфейса до основного."""
from __future__ import annotations

import os

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg

from .common import log, _STATE_KEY, STATE_OFF, LEVER


class MigrationExitsMixin:
    """Выходы: отмена, завершение, повышение интерфейса."""

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
        переезд на себе. Но не поверх идущего включения или завершения (замок
        рычага) и не пока пользователи на резерве: отмена переписала бы базу,
        которой сейчас не верят.
        """
        if hasattr(self, "reserve_refuse_if_away"):
            self.reserve_refuse_if_away()
        with LEVER("отменяется"):
            return self._migration_cancel_locked()

    def _migration_cancel_locked(self) -> int:
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

    def migration_finish_preview(self) -> tuple[int, list[str], list[str]]:
        """Что будет при завершении: (сколько пар закроется, кого уронит, кому
        двойник только предстоит родиться на завершении).

        Отдельно от самого завершения, потому что подтверждение обязано
        называть уронённых ДО нажатия, а не после: между решением и нажатием
        легко забыть, о ком речь, а отказ необратим.
        """
        pairs, dropped, twinless = self._finish_plan()
        return len(pairs), dropped, [self._owned_name(d) for d in twinless]

    def _owned_name(self, dev, owners: dict | None = None) -> str:
        """«профиль — устройство»; owners — кэш имён профилей на один проход."""
        if owners is None:
            owners = {}
        if dev.client_id not in owners:
            owner = self.db.get_client(dev.client_id)
            owners[dev.client_id] = owner.name if owner else "?"
        return f"{owners[dev.client_id]} — {dev.name}"

    def _finish_plan(self) -> tuple[list[tuple], list[str], list]:
        """Пары к закрытию, имена тех, кто ещё не переехал, и исходные
        устройства БЕЗ двойника (рождение сорвалось при включении — адреса
        кончились, сервер моргнул). Общий расчёт для предпросмотра и самого
        завершения — чтобы подтверждение не могло разойтись с тем, что
        произойдёт.

        Без третьего списка устройство без пары финал не видел вовсе: «все на
        новом интерфейсе», старый гасится, его пир остаётся на погашенном, а
        сверка потом удаляет строку — устройство пропадает у человека. Его
        двойник ещё не рождён, значит и на новом интерфейсе его нет: в
        уронённых оно названо, как любое без рукопожатия на двойнике."""
        twins = self.db.twins_by_origin()
        by_id = {d.id: d for d in self.db.list_all_devices()}
        pairs: list[tuple] = []
        dropped: list[str] = []
        owners: dict[int, str] = {}
        for old_id, twin_id in twins.items():
            old, twin = by_id.get(old_id), by_id.get(twin_id)
            if old is None or twin is None:
                continue
            pairs.append((old, twin))
            if not twin.last_handshake:
                dropped.append(self._owned_name(old, owners))
        twinless = [d for d in self._migratable() if d.id not in twins]
        dropped += [self._owned_name(d, owners) for d in twinless]
        return pairs, dropped, twinless

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

        Под замком рычага (второе нажатие поверх идущего — отказ) и не пока
        пользователи на резерве: снятые пиры и слитая история разошлись бы с
        базой гостя.
        """
        if hasattr(self, "reserve_refuse_if_away"):
            self.reserve_refuse_if_away()
        with LEVER("завершается"):
            return self._migration_finish_locked()

    def _migration_finish_locked(self) -> tuple[int, list[str], list[str]]:
        pairs, dropped, twinless = self._finish_plan()
        removed = 0
        failed: list[str] = []
        owners: dict[int, str] = {}                       # имя профиля — один запрос на профиль

        # Устройствам без двойника — родить его СЕЙЧАС, до снятия пар: иначе
        # промоушен погасит интерфейс, на котором они живут, а на новом их нет.
        # Не вышло — в failed: рычаг не гасится, промоушена нет, повтор
        # завершения попробует снова.
        for dev in twinless:
            try:
                self._birth_twin(dev)
            except Exception as e:                        # noqa: BLE001
                log.warning("migration_finish: двойник для %s (id=%s) не создан, завершение "
                            "без промоушена: %s", dev.name, dev.id, e)
                failed.append(self._owned_name(dev, owners))
        if twinless:
            pairs, dropped, _ = self._finish_plan()       # рождённые — тоже пары к закрытию

        for old, twin in pairs:
            try:
                awg.remove_peer(old.public_key, iface=awg.iface_of(old.iface))
            except awg.AwgError as e:
                log.warning("migration_finish: пир %s не снят, пара оставлена: %s",
                            old.name, e)
                failed.append(self._owned_name(old, owners))
                continue
            removed += 1
            if int(old.block_reason) != 0:
                try:
                    awg.unblock_ip(old.address)           # осиротевший DROP снять
                except awg.AwgError:
                    pass
            # Всё по базе для пары — одной транзакцией: шаги отдельными
            # записями давали окно, где крах между слиянием и удалением строки
            # заставлял повторное завершение сложить трафик второй раз.
            # remove_peer уже снятого пира идемпотентен, повтор безопасен.
            with self.db.transaction():
                self.db.merge_traffic(old.id, twin.id)
                # слот шлюза указывает на исходную строку пары; на финале он
                # переезжает к двойнику вместе с остальным
                _slot = self.db.gateway_by_device(old.id)
                if _slot is not None and _slot.device_id == old.id:
                    self.db.gateway_update(_slot.id, device_id=twin.id)
                    if hasattr(self, "gwkeep_device_moved"):
                        self.gwkeep_device_moved(_slot.id, old.public_key, twin.public_key)
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
            settings.set_value("app.awg.interface", new_if)
            settings.set_value("app.network.subnet_prefix", new_prefix)
            settings.set_value("app.network.subnet_cidr", f"{new_prefix}.0/24")
            settings.set_value("app.network.server_port", port)
            # Идентификатор протокола вморожен в каждую выданную ссылку. Меняем
            # только вместе с поколением: у двойников он уже новый, и теперь
            # таким же должны рождаться все следующие устройства.
            if target > applied and awglock.protocol_id():
                settings.set_value("app.awg.protocol_id", awglock.protocol_id())
            settings.set_value("app.awg.migration_interface", "")
            settings.set_value("app.awg.migration_subnet_prefix", "")
        except Exception as e:                            # noqa: BLE001
            log.warning("promote: app.yaml не переписан: %s", e)
            return ""
        # DNS пользователей: адрес двойников становится адресом всех, старый — со
        # старым интерфейсом — снимается с резолвера.
        try:
            self.private_dns_on_promote(config.SUBNET_PREFIX)
        except Exception as e:                            # noqa: BLE001
            log.warning("promote: DNS пользователей не переписан: %s", e)
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
        # Отметка «ждёт перезапуска» живёт до старта нового процесса: config.*
        # прочитаны прежним, и до перезапуска бот считает основным погашенный
        # интерфейс — монитор по ней молчит, выдача устройств отказывает.
        self.db.set_state(self._RESTART_KEY, new_if)
        log.info("переезд: основным интерфейсом стал %s (поколение %s)", new_if, target)
        return new_if

    _RESTART_KEY = "awg_restart_needed"

    def migration_restart_pending(self) -> str:
        """Имя интерфейса, ставшего основным на финале, пока бот не перезапущен
        (константы config ещё от погашенного интерфейса); пусто — ждать нечего."""
        return self.db.get_state(self._RESTART_KEY) or ""

    def migration_restart_done(self) -> None:
        """Старт нового процесса: деплой-значения перечитаны — отметка снимается."""
        if self.db.get_state(self._RESTART_KEY):
            self.db.set_state(self._RESTART_KEY, "")

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
        from awgbot.infra import systemd
        unit = f"awg-quick@{name}"
        try:
            subprocess.run(["awg-quick", "down", name], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("promote: awg-quick down %s не выполнено: %s", name, e)
        try:
            systemd.disable(unit, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("promote: systemctl disable %s не выполнено: %s", unit, e)

    def pop_promoted_iface(self) -> str:
        """Имя интерфейса, ставшего основным на финале переезда, — один раз.
        Вызывающий показывает это админу и перезапускает бота: деплой-значения
        читаются при старте."""
        name = self.db.get_state(self._PROMOTED_KEY) or ""
        if name:
            self.db.set_state(self._PROMOTED_KEY, "")
        return name
