"""watch.py — часть пакета migration (см. __init__): поздравление пользователю, тик
первых хендшейков, доклад о готовности."""
from __future__ import annotations

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg

from .common import log


class MigrationWatchMixin:
    """Поздравление, тик первых хендшейков, доклад о готовности."""

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
        from awgbot.domain.notices.migration import migration_hello
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
        return Notification(target, migration_hello(dev.name))

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
        from awgbot.domain.notices.migration import migration_ready
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
        return [Notification(config.ADMIN_ID, migration_ready(p))]
