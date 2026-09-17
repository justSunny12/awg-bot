"""nav.py — ui_state: активное нав-сообщение чата, история меню и
контент-сообщения, которые убираются при возврате в меню.
"""

from __future__ import annotations

from typing import Optional


class NavMixin:

    def get_nav_message_id(self, chat_id: int):
        row = self._connection().execute(
            "SELECT nav_message_id FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["nav_message_id"] if row else None

    def nav_touch(self, chat_id: int, message_id: int) -> Optional[int]:
        """Сделать message_id активным меню и дописать его в историю ОДНОЙ
        транзакцией; вернуть прежний активный id (для гашения его кнопок).
        Раньше это были три хопа в поток и четыре запроса на каждый переход."""
        import json
        with self._tx() as cur:
            row = cur.execute("SELECT nav_message_id FROM ui_state WHERE chat_id = ?",
                              (chat_id,)).fetchone()
            prev = row["nav_message_id"] if row else None
            if prev != message_id:
                cur.execute(
                    "INSERT INTO ui_state (chat_id, nav_message_id) VALUES (?, ?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET nav_message_id = excluded.nav_message_id",
                    (chat_id, message_id))
            key = f"nav_history:{chat_id}"
            hrow = cur.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
            ids = json.loads(hrow["value"]) if hrow and hrow["value"] else []
            if message_id not in ids:
                ids = (ids + [message_id])[-self._NAV_HISTORY_CAP:]
                cur.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (key, json.dumps(ids)))
        return prev

    def set_nav_message_id(self, chat_id: int, message_id) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO ui_state (chat_id, nav_message_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET nav_message_id = excluded.nav_message_id",
                (chat_id, message_id),
            )

    # ── история меню: все нав-сообщения чата, а не только последнее ─────────
    # nav_message_id знает одно активное меню; /start обязан убрать ВСЕ прошлые,
    # включая те, с которых кнопки уже сняты, — иначе после нескольких сессий
    # чат превращается в стопку мёртвых панелей. Cap на 30: старше Telegram всё
    # равно не даст удалить, а хранить бесконечно незачем.
    _NAV_HISTORY_CAP = 30

    def pop_nav_history(self, chat_id: int) -> list:
        import json
        key = f"nav_history:{chat_id}"
        ids = json.loads(self.get_state(key) or "[]")
        self.set_state(key, "[]")
        return ids

    def add_content_msg_id(self, chat_id: int, message_id: int) -> None:
        """Запомнить id выданного контент-сообщения (ссылка/QR/файл/инструкция),
        чтобы удалить его при возврате в меню."""
        import json
        row = self._connection().execute(
            "SELECT content_msg_ids FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        ids = json.loads(row["content_msg_ids"]) if row and row["content_msg_ids"] else []
        if message_id not in ids:
            ids.append(message_id)
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO ui_state (chat_id, content_msg_ids) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET content_msg_ids = excluded.content_msg_ids",
                (chat_id, json.dumps(ids)),
            )

    def pop_content_msg_ids(self, chat_id: int) -> list:
        """Забрать и очистить список id контент-сообщений (для удаления)."""
        import json
        row = self._connection().execute(
            "SELECT content_msg_ids FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        ids = json.loads(row["content_msg_ids"]) if row and row["content_msg_ids"] else []
        if ids:
            with self._tx() as cur:
                cur.execute("UPDATE ui_state SET content_msg_ids = NULL WHERE chat_id = ?",
                            (chat_id,))
        return ids

