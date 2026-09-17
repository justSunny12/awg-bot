"""
reconcile.py — сверка состава пиров (вотчдог), реконсиляция блокировок и
SSH-фильтра после рестарта, детект рестарта сервиса.
"""
from __future__ import annotations

import logging
import sqlite3

from awgbot.core import config
from awgbot.infra import awg
from awgbot.infra import routing
from awgbot.core.enums import FriendStatus
from awgbot.domain.services.types import Notification


log = logging.getLogger("awgbot.services")


# Пир, которого нет в БД. Создавать пиры больше некому, кроме бота, — значит это
# либо ручная правка конфига, либо чужое вмешательство. Тревога, а не находка.
_TXT_UNKNOWN_PEER = (
    "🚨 В конфиге сервера пир, которого нет в базе: {ip}.\n\n"
    "Пиры создаёт только бот — значит это ручная правка конфига или чужое "
    "вмешательство. Пир помещён в карантин («Устройства без профиля»): "
    "проверь и либо привяжи к профилю, либо удали.")
# Пир исчез из конфига, а запись в БД осталась: сам бот так не удаляет —
# он снимает пира и строку разом. Значит конфиг правили мимо бота.
_TXT_PEER_GONE = ("Устройство «{name}» клиента «{client}» пропало из конфига сервера — бот его не удалял. Запись убрана, чтобы база сошлась с сервером.")

_TXT_FRIEND_DEVICE_GONE = ("Устройство, которым ты управлял, удалено владельцем — "
                           "доступ по нему больше не работает.")


class ReconcileMixin:
    # ── Реконсиляция состава пиров (вотчдог) ─────────────────────────────────

    def _migration_ifaces(self) -> list[str]:
        """Интерфейсы, которые бот обязан обходить: те, на которых реально живут
        устройства, плюс заданный конфигом интерфейс переезда.

        Второй нужен отдельно: между поднятием интерфейса и рождением первого
        двойника устройств на нём ещё нет, а карантин на нём уже должен
        работать — иначе чужой пир, заведённый руками в этом окне, останется
        незамеченным.

        Возвращаем СЫРЫЕ значения (пустая строка = дефолт), разрешает их
        awg.iface_of: дефолтный интерфейс может смениться, и держать в списке
        одновременно '' и 'awg0' значило бы обойти один конфиг дважды.
        """
        raw = set(self.db.distinct_ifaces())
        raw.add("")                                   # дефолтный обходим всегда
        if config.MIGRATION_INTERFACE:
            names = {awg.iface_of(x) for x in raw}
            if config.MIGRATION_INTERFACE not in names:
                raw.add(config.MIGRATION_INTERFACE)
        return sorted(raw)

    @staticmethod
    def _peers_with_ip(conf_text: str) -> dict[str, str]:
        """pubkey → ip из живого conf интерфейса."""
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
        """Сверка живого конфига с БД. Пропавшие пиры → грациозный детект
        удаления (MISSING_SWEEPS_THRESHOLD сверок подряд). Неизвестные →
        карантин на служебном профиле + ТРЕВОГА админу.

        Раньше неизвестный пир считался находкой: приложение Amnezia могло
        завести его в обход бота, и сверка молча его усыновляла. Приложение с
        серверной стороны снято, создавать пиры больше некому, кроме нас, —
        значит пир, которого нет в базе, это ручная правка конфига или чужое
        вмешательство. Принять его молча означало бы узаконить чужой доступ.

        ПОРЯДОК ПРОХОДОВ ЗНАЧИМ. Пропавшие разбираем ПЕРВЫМИ, неизвестных
        заводим после: devices.address UNIQUE, а «первый свободный» адрес любой
        сторонний инструмент выдаст по тому же правилу, что и мы. Сняли пир,
        следом завели новый — он получит освободившийся адрес, которым в нашей
        БД ещё владеет уходящая запись. В обратном порядке заведение падало бы
        на UNIQUE ДО прохода по пропавшим, то есть запись-держатель адреса не
        удалялась бы никогда: сверка встаёт колом насовсем.

        СВЕРКА ИДЁТ ПО СВОЕМУ ИНТЕРФЕЙСУ У КАЖДОГО УСТРОЙСТВА. Пока интерфейс
        был один, хватало одного конфига; с двумя одиночное чтение объявило бы
        «пропавшими» ВСЕХ, кто живёт на соседнем, и через MISSING_SWEEPS_THRESHOLD
        сверок бот удалил бы их сам — молча и необратимо.

        Нечитаемый конфиг (интерфейс задан, но лежит; файл ещё не создан)
        пропускаем ЦЕЛИКОМ, не трогая missing_count: отсутствие конфига — это
        «не знаю», а не «пиров нет», и накапливать по нему пропажи значит
        готовить то же самое удаление, только медленнее.
        """
        db_devices = {d.public_key: d for d in self.db.list_all_devices()}
        service_id = self.db.get_service_client_id()
        notifications: list[Notification] = []

        # conf каждого задействованного интерфейса + тех, что заданы конфигом.
        # Интерфейс запоминаем ВМЕСТЕ с пиром: карантинная запись обязана знать,
        # где её пир живёт, иначе снимать его пойдут не с того конфига.
        live: dict[str, tuple[str, str]] = {}             # pub → (ip, iface)
        readable: set[str] = set()                        # интерфейсы, чей conf прочли
        for raw in self._migration_ifaces():
            name = awg.iface_of(raw)
            try:
                conf = awg.read_file(awg.conf_path(name))
            except awg.AwgError as e:
                log.warning("reconcile_peers: конфиг %s не прочитан, пропускаю: %s", name, e)
                continue
            readable.add(name)
            for pub, ip in self._peers_with_ip(conf).items():
                live[pub] = (ip, name)
        try:
            psk = awg.read_server_params()["psk"]
        except awg.AwgError:
            psk = ""

        # пропавшие пиры
        for pub, dev in db_devices.items():
            if awg.iface_of(dev.iface) not in readable:
                continue                                  # конфиг не прочли — не судим
            if pub in live:
                if dev.missing_count:
                    self.db.update_device_fields(dev.id, missing_count=0)
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
                        _TXT_PEER_GONE.format(name=dev.name, client=client.name)))
                if friend_tg:
                    notifications.append(Notification(friend_tg, _TXT_FRIEND_DEVICE_GONE))
            else:
                self.db.update_device_fields(dev.id, missing_count=mc)

        # неизвестные пиры → карантин + тревога
        for pub, (ip, iface) in live.items():
            if pub in db_devices:
                continue
            name = f"Неизвестный пир {ip}"
            try:
                self.db.create_device(service_id, name, pub, psk, ip, private_key=None,
                                      iface=iface)
            except sqlite3.IntegrityError as e:
                # Адрес ещё за уходящей записью (порог MISSING_SWEEPS_THRESHOLD
                # не выбран). Пропускаем ЭТОТ пир, а не всю сверку: он попадёт в
                # карантин на сверке, где прежний владелец адреса удалится.
                log.warning("reconcile_peers: пир %s (%s) пока не в карантине: %s",
                            name, ip, e)
                continue
            # force_sound: это событие безопасности, а не информационная строка.
            # Тихие часы для него — не та цена, которую стоит платить за сон.
            notifications.append(Notification(
                config.ADMIN_ID, _TXT_UNKNOWN_PEER.format(ip=ip), force_sound=True))
        return notifications

    # ── Реконсиляция блокировок после рестарта контейнера ────────────────────

    def reconcile_blocks(self) -> None:
        """iptables-DROP'ы эфемерны — после рестарта переналагаем их на всех,
        у кого block_reason != 0 в БД (любая причина блокировки). Один
        `iptables -S` вместо -C на каждое устройство; block_ip сам идемпотентен."""
        try:
            present = awg.blocked_ips()
        except awg.AwgError:
            present = set()
        for address in self.db.blocked_addresses():
            if address in present:
                continue
            try:
                awg.block_ip(address)
            except awg.AwgError:
                pass

    def reconcile_ssh_access(self) -> None:
        """SSH-к-хосту из туннеля — только устройствам админа. Единственная
        точка: nft-таблица awg_bot_guard (infra/nftguard). Бот держит в ней set
        адресов админских устройств и сверяет его с желаемым в тех же точках,
        что и блокировки (старт, рестарт, тик монитора) плюс сразу при
        создании/удалении админского устройства: удаление устройства или
        переиспользование его IP другим профилем закрывается в пределах тика.

        Пока firewall.enabled=false (таблицу ещё не включали через
        `awg-bot firewall setup`), фильтр не ставим — включение файервола
        делается человеком с таймером отката, не ботом. Но NAT клиентов в
        host-режиме таблица держит всегда (nftguard: форма NAT-only)."""
        from awgbot.infra import nftguard
        try:
            admin_ips = self.db.admin_device_addresses(config.ADMIN_ID)
            res = nftguard.reconcile(admin_ips)
        except nftguard.GuardError as e:
            log.warning("firewall: %s", e)
            return
        if res != "ok":
            log.info("firewall: таблица awg_bot_guard — %s", res)

    # ── Детект рестарта сервиса ──────────────────────────────────────────────

    def detect_and_handle_restart(self) -> bool:
        """Сверяет метку старта сервиса с сохранённой. Изменилась (был рестарт) —
        реконсиляция блокировок. Возвращает True, если был рестарт.

        Что считать меткой, решает awg.service_started_at: в контейнере это его
        StartedAt, на хосте — время загрузки системы. Ключ в state исторически
        зовётся container_started_at и переименованию не подлежит — иначе первая
        же сверка после обновления не найдёт сохранённого значения."""
        current = awg.service_started_at()
        if not current:
            return False
        stored = self.db.get_state("container_started_at")
        if current != stored:
            self.db.set_state("container_started_at", current)
            if stored is not None:                        # не первый запуск
                self.reconcile_blocks()
                self.reconcile_ssh_access()               # SSH-фильтр тоже слетел
                routing.invalidate_self_check()           # обвязка могла подняться/лечь
                return True
        return False
