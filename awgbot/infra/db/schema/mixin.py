"""mixin.py — часть пакета schema (см. __init__): SchemaMixin с init_schema,
служебным пользователем и линией базы (db_line)."""
from __future__ import annotations

import secrets

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (хранение — UTC+3)

from .ddl import SCHEMA
from .selects import LEGACY_SERVICE_CLIENT_NAME, SERVICE_CLIENT_NAME


class SchemaMixin:
    """Инициализация схемы. Требует ядро (_connection/_tx) и _insert_guest
    из ClientsMixin (служебный пользователь)."""

    # ── Инициализация ────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Создаёт таблицы (идемпотентно), гарантирует служебного пользователя и
        линию базы.

        Схема нормализована (identity/подписка/квота/grace/pause разведены по
        таблицам). Боевую БД не пересоздаём. Миграций нет — минимальная
        поддерживаемая версия 3.3.0 (README-bot §9a), её схема полная, всё,
        что доводило БД версий старше, снято: хост старше проходит через
        3.3.0. Копии ниже версии совместимости (core/compat) не применяются
        вовсе — схему копии сторожит её версия, а не колонки.
        """
        with self._tx() as cur:
            cur.executescript(SCHEMA)
        self._ensure_service_client()
        self._ensure_db_line()

    DB_LINE_KEY = "db_line"

    def _ensure_db_line(self) -> None:
        """Линия базы — случайная метка, которую база получает один раз и
        уносит с собой при восстановлении: по ней восстановление отличает
        «тот же сервер или его продолжение» от нового сервера и чужой копии."""
        cur = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (self.DB_LINE_KEY,)).fetchone()
        if cur is None or not cur[0]:
            with self._tx() as c:
                c.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (self.DB_LINE_KEY, secrets.token_hex(8)))

    def _ensure_service_client(self) -> None:
        """Служебный пользователь «Устройства без пользователя» — ровно один, создаётся один раз.

        К нему цепляются подхваченные с сервера пиры, пока админ не привяжет их к реальному
        пользователю. У него нет tg_id/периода, is_service=1, и логика уведомлений/
        блокировок его игнорирует. Подписка/квота (1:1) создаются вместе с ним.
        """
        row = self._connection().execute(
            "SELECT id FROM clients WHERE is_service = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            with self._tx() as cur:
                cur.execute(
                    """INSERT INTO clients
                       (tg_id, name, device_limit, activation_status, invite_code,
                        is_service, created_at)
                       VALUES (NULL, ?, 0, 'active', NULL, 1, ?)""",
                    (SERVICE_CLIENT_NAME, _now_iso()),
                )
                cid = cur.lastrowid
                cur.execute(
                    "INSERT INTO client_subscription (client_id, period_start, "
                    "period_end, period_kind, status) VALUES (?, NULL, NULL, NULL, 'active')",
                    (cid,))
                cur.execute("INSERT INTO client_quota (client_id) VALUES (?)", (cid,))
        else:
            # прежнее имя — только если админ его не менял
            with self._tx() as cur:
                cur.execute("UPDATE clients SET name = ? WHERE id = ? AND name = ?",
                            (SERVICE_CLIENT_NAME, row[0], LEGACY_SERVICE_CLIENT_NAME))
