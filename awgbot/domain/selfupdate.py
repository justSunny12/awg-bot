"""
selfupdate.py — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — выбрать цель, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.
"""
from __future__ import annotations

import logging

from awgbot.core import config
from awgbot.infra import updates

log = logging.getLogger("awgbot.selfupdate")


class SelfUpdateMixin:
    # ── окна обновления с кнопкой «В меню»: история, а не один id ────────────
    # Финишеров в цепочке ступеней несколько, между ними бывают «не применилось»
    # и «не удалось» с той же кнопкой. Снимать по одному предыдущему id значит
    # оставлять живые кнопки на всём, что выпало из цепочки; снимаем у всех.
    _UPD_REPORTS_KEY = "update_report_msgs"

    def remember_update_report(self, chat_id: int, message_id: int) -> None:
        import json
        ids = self.db.get_state_json(self._UPD_REPORTS_KEY, [])
        ids = [x for x in ids if x != [chat_id, message_id]]
        ids = (ids + [[chat_id, message_id]])[-20:]
        self.db.set_state(self._UPD_REPORTS_KEY, json.dumps(ids))

    def pop_update_reports(self) -> list:
        """[(chat_id, message_id), …] всех запомненных окон; история очищается."""
        ids = self.db.get_state_json(self._UPD_REPORTS_KEY, [])
        self.db.set_state(self._UPD_REPORTS_KEY, "[]")
        return [tuple(x) for x in ids if isinstance(x, list) and len(x) == 2]

    _MUTE_KEY = "updates_muted"
    _NOTIFIED_KEY = "update_notified_tag"

    def updates_muted(self) -> bool:
        return self.db.get_state(self._MUTE_KEY) == "1"

    def mute_updates(self) -> None:
        """Выключить автоуведомления и стартовую проверку об обновлениях.
        Ручная проверка «Обновление бота» продолжает работать."""
        self.db.set_state(self._MUTE_KEY, "1")

    def normalize_update_schedule(self) -> bool:
        """3.2.0: расписания «никогда» больше нет ни у одной роли — проверка
        идёт всегда ради строки «⬆️ Доступна vX». Прежнее never (старый conf,
        правка руками) → «месяц» и выключенные уведомления. Одно место на старт
        обеих ролей и на открытие раздела; планировщик never не знает вовсе.
        Возвращает, была ли перезапись."""
        from awgbot.core import settings
        if str(settings.get("updates.poll_schedule", "day")).lower() != "never":
            return False
        try:
            settings.set_value("updates.poll_schedule", "month")
        except settings.SettingsWriteError as e:
            log.warning("updates.poll_schedule=never не переписано: %s", e)
        self.mute_updates()
        return True

    def unmute_updates(self) -> None:
        """Включить автоуведомления об обновлениях обратно."""
        self.db.set_state(self._MUTE_KEY, "0")

    def _generation_ceiling(self):
        """Потолок поколения для цели обновления: идёт переезд — не дальше его
        цели; иначе потолка нет. Роль без переезда — потолка нет."""
        from awgbot.infra import awglock
        try:
            running = bool(self.migration_running())
        except Exception:                                 # noqa: BLE001
            return None                                   # роль без переезда
        return awglock.target_generation() if running else None

    def clear_update_pending(self) -> None:
        """Обновление не взлетело до апдейтера — снять флаг, иначе следующий
        старт принесёт ложное «не применилось»."""
        self.db.set_state("update_pending", "")

    def update_next(self):
        """Цель обновления (updates.Release) или None: последний релиз для роли
        с учётом обязательных ступеней и потолка поколения при идущем переезде.
        Сетевые ошибки гасим в None — фоновая задача/кнопка от них не падают."""
        try:
            rel = updates.next_release(max_generation=self._generation_ceiling())
        except updates.UpdateError:
            self.update_scan_failed = True
            return None
        self.update_scan_failed = False
        return rel

    _AVAILABLE_KEY = "update_available_tag"

    def update_scan(self):
        """Периодическая проверка: запомнить цель обновления (тег или пусто)
        независимо от того, включены ли уведомления, — шапка панели читает
        ключ без сетевого запроса. Возвращает Release или None. Сетевой сбой
        прежний тег не стирает: «актуальна» и «не проверилось» — разное;
        признак — update_scan_failed."""
        self.update_scan_failed = False
        nxt = self.update_next()
        if self.update_scan_failed:                       # сеть: прежний тег не трогаем
            return None
        self.db.set_state(self._AVAILABLE_KEY, nxt.tag if nxt is not None else "")
        return nxt

    update_scan_failed = False

    def update_available_tag(self) -> str:
        """Тег последней найденной версии (для строки «⬆️ Доступна vX»); пусто —
        обновляться не на что или ещё не проверяли."""
        return self.db.get_state(self._AVAILABLE_KEY) or ""

    def update_to_notify(self, scanned=...):
        """Для планировщика/старта: вернуть Release, о котором НАДО уведомить, и
        пометить его как уведомлённый (ровно один раз на версию). None, если
        уведомления заглушены, расписание «никогда», обновлять не на что, или про
        эту версию уже уведомляли. Проверка «никогда» здесь, а не только в UI —
        инвариант держится и при ручной правке conf/updates.yaml. Помечаем ДО
        отправки — «не более одного раза» важнее, чем «гарантированно доставить»
        (миссы закрывает ручная кнопка). scanned — итог уже сделанного update_scan,
        чтобы за такт не ходить к списку релизов дважды."""
        from awgbot.core import settings
        if str(settings.get("updates.poll_schedule", "day")).lower() == "never":
            return None
        if self.updates_muted():
            return None
        nxt = self.update_scan() if scanned is ... else scanned   # планировщик уже сходил
        if nxt is None:
            return None
        if self.db.get_state(self._NOTIFIED_KEY) == nxt.tag:
            return None
        if self.update_block_reason(nxt):
            # Поставка ждёт конца переезда. Молчим и НЕ помечаем уведомлённой:
            # иначе единственное уведомление о ней сгорело бы на кнопке, которая
            # сейчас ведёт в отказ, и после финала переезда о нём никто не
            # напомнил бы. Экран «Обновления» причину показывает.
            return None
        self.db.set_state(self._NOTIFIED_KEY, nxt.tag)
        return nxt

    def update_block_reason(self, release=None) -> str:
        """Почему обновляться нельзя прямо сейчас, или пустая строка.

        Единственная причина: переезд ИДЁТ, а поставка привезла поколение
        AmneziaWG дальше его цели. Разреши — и профили размажутся по трём
        интерфейсам, а мигрировать такое нечем. Поколение поставки читается из
        тела релиза (#awg_genN), то есть ДО скачивания.

        Без аргумента — про экран «Обновления»: цель под потолком есть → ничего
        не блокировано (она и предлагается); нет — но выше потолка что-то лежит
        → причина, почему до него не дотянуться."""
        from awgbot.infra import awglock
        if release is None:
            if self.update_next() is not None:
                return ""
            try:
                release = updates.next_release()          # без потолка
            except updates.UpdateError:
                return ""
        if release is None:
            return ""
        running = False
        try:
            running = bool(self.migration_running())
        except Exception:                                 # noqa: BLE001
            running = False                               # роль без переезда
        if not awglock.blocks_update(release.awg_generation(), running):
            return ""
        return ("идёт переезд профилей на поколение "
                f"{awglock.target_generation()}, а {release.tag} несёт ядро поколения "
                f"{release.awg_generation()}. Заверши переезд — и обновление станет "
                "доступно: профили на трёх интерфейсах мигрировать нечем.")

    def apply_update(self, release) -> None:
        """Скачать ассет следующей версии, сверить sha256 и запустить апдейтер
        (он остановит и заменит сервис). UpdateError пробрасывается — обработчик
        покажет пользователю причину, сервис остаётся жив.

        Перед запуском пишем update_pending=tag: на следующем старте
        confirm_applied_update() сверит фактическую версию и отчитается админу."""
        reason = self.update_block_reason(release)
        if reason:
            raise updates.UpdateError(reason)
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
            return _notification(config.ADMIN_ID, texts.update_applied(pending, body),
                                reply_markup=kb.update_done_menu())
        return _notification(config.ADMIN_ID, texts.update_not_applied(
            pending, config.INSTALLED_VERSION), reply_markup=kb.update_done_menu())

    # ── перезапуск бота: обещание «вернётся через несколько секунд» и его
    # исполнение новым процессом — одинаково у обеих ролей ──────────────

    def set_restart_wait(self, chat_id: int, message_id: int) -> None:
        """Запомнить сообщение «бот перезапускается»: новый процесс подменит его
        панелью. Без этого обещание «вернётся через несколько секунд» исполнить
        было некому — в чат после старта никто не пишет, и админ оставался с
        мёртвым сообщением до тех пор, пока сам не отправлял /start."""
        self.db.set_state("restart_wait", f"{chat_id}:{message_id}")

    def pop_restart_wait(self):
        """(chat_id, message_id) обещания или None. Одноразово: повторный старт
        не должен переписывать давно отработавшее сообщение."""
        raw = self.db.get_state("restart_wait")
        if not raw:
            return None
        self.db.set_state("restart_wait", "")
        try:
            chat_s, msg_s = raw.split(":", 1)
            return int(chat_s), int(msg_s)
        except ValueError:
            return None

    def restart_bot(self) -> None:
        """Перезапустить сам сервис бота. Как и self-update, рестарт идёт
        ОТДЕЛЬНО от нашего процесса (systemd-run вне cgroup), иначе `systemctl
        restart` убьёт нас на середине команды; без systemd-run — та же
        команда в новой сессии, best effort (юнит поднимет нас по
        Restart=on-failure)."""
        from awgbot.infra.detach import spawn_detached
        spawn_detached(["systemctl", "restart", "awg-bot"], unit_prefix="awg-bot-restart")


def _notification(*args, **kwargs):
    """Ленивый импорт: Notification живёт в services, а services подмешивает
    этот модуль — прямой импорт замкнул бы цикл на уровне модулей."""
    from awgbot.domain.services import Notification
    return Notification(*args, **kwargs)
