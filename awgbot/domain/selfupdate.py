"""
selfupdate.py — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — следующая ступень, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.infra import updates


class SelfUpdateMixin:
    # ── окна обновления с кнопкой «В меню»: история, а не один id ────────────
    # Финишеров в цепочке ступеней несколько, между ними бывают «не применилось»
    # и «не удалось» с той же кнопкой. Снимать по одному предыдущему id значит
    # оставлять живые кнопки на всём, что выпало из цепочки; снимаем у всех.
    _UPD_REPORTS_KEY = "update_report_msgs"

    def remember_update_report(self, chat_id: int, message_id: int) -> None:
        import json
        ids = json.loads(self.db.get_state(self._UPD_REPORTS_KEY) or "[]")
        ids = [x for x in ids if x != [chat_id, message_id]]
        ids = (ids + [[chat_id, message_id]])[-20:]
        self.db.set_state(self._UPD_REPORTS_KEY, json.dumps(ids))

    def pop_update_reports(self) -> list:
        """[(chat_id, message_id), …] всех запомненных окон; история очищается."""
        import json
        ids = json.loads(self.db.get_state(self._UPD_REPORTS_KEY) or "[]")
        self.db.set_state(self._UPD_REPORTS_KEY, "[]")
        return [tuple(x) for x in ids]

    _MUTE_KEY = "updates_muted"
    _NOTIFIED_KEY = "update_notified_tag"

    def updates_muted(self) -> bool:
        return self.db.get_state(self._MUTE_KEY) == "1"

    def mute_updates(self) -> None:
        """Выключить автоуведомления и стартовую проверку об обновлениях.
        Ручная проверка «Обновление бота» продолжает работать."""
        self.db.set_state(self._MUTE_KEY, "1")

    def unmute_updates(self) -> None:
        """Включить автоуведомления об обновлениях обратно."""
        self.db.set_state(self._MUTE_KEY, "0")

    def update_next(self):
        """Следующая доступная версия (updates.Release) или None. Сетевые ошибки
        гасим в None — фоновая задача/кнопка от них не падают."""
        try:
            return updates.next_release()
        except updates.UpdateError:
            return None

    def update_to_notify(self):
        """Для планировщика/старта: вернуть Release, о котором НАДО уведомить, и
        пометить его как уведомлённый (ровно один раз на версию). None, если
        уведомления заглушены, расписание «никогда», обновлять не на что, или про
        эту версию уже уведомляли. Проверка «никогда» здесь, а не только в UI —
        инвариант держится и при ручной правке conf/updates.yaml. Помечаем ДО
        отправки — «не более одного раза» важнее, чем «гарантированно доставить»
        (миссы закрывает ручная кнопка)."""
        from awgbot.core import settings
        if str(settings.get("updates.poll_schedule", "day")).lower() == "never":
            return None
        if self.updates_muted():
            return None
        nxt = self.update_next()
        if nxt is None:
            return None
        if self.db.get_state(self._NOTIFIED_KEY) == nxt.tag:
            return None
        self.db.set_state(self._NOTIFIED_KEY, nxt.tag)
        return nxt

    def apply_update(self, release) -> None:
        """Скачать ассет следующей версии, сверить sha256 и запустить апдейтер
        (он остановит и заменит сервис). UpdateError пробрасывается — обработчик
        покажет пользователю причину, сервис остаётся жив.

        Перед запуском пишем update_pending=tag: на следующем старте
        confirm_applied_update() сверит фактическую версию и отчитается админу."""
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



def _notification(*args, **kwargs):
    """Ленивый импорт: Notification живёт в services, а services подмешивает
    этот модуль — прямой импорт замкнул бы цикл на уровне модулей."""
    from awgbot.domain.services import Notification
    return Notification(*args, **kwargs)
