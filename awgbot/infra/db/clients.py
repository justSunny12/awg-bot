"""clients.py — клиенты: создание/активация, точечные обновления по
нормализованным таблицам, типизированные врайтеры pause/grace, пороги
уведомлений, гостевые профили и имена Telegram.
"""

from __future__ import annotations

from typing import Optional

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)
from awgbot.core import models

from .schema import _CLIENT_SELECT, _client_from_row


class ClientsMixin:

    # ── Клиенты ──────────────────────────────────────────────────────────────

    def create_client(
        self,
        name: str,
        device_limit: int,
        period_start: str,
        period_end: str,
        invite_code: str,
        traffic_limit: int = 0,
        period_kind: Optional[str] = None,
    ) -> int:
        """Создаёт клиента в статусе pending (ждёт активации инвайта). tg_id пока NULL.
        Заводит сопутствующие 1:1 подписку и квоту. Возвращает id нового клиента."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO clients
                   (tg_id, name, device_limit, activation_status, invite_code,
                    is_service, created_at)
                   VALUES (NULL, ?, ?, 'pending', ?, 0, ?)""",
                (name, device_limit, invite_code, _now_iso()))
            cid = cur.lastrowid
            cur.execute(
                "INSERT INTO client_subscription (client_id, period_start, period_end, "
                "period_kind, status) VALUES (?, ?, ?, ?, 'active')",
                (cid, period_start, period_end, period_kind))
            cur.execute(
                "INSERT INTO client_quota (client_id, traffic_limit) VALUES (?, ?)",
                (cid, traffic_limit))
            return cid

    def get_client(self, client_id: int):
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.id = ?", (client_id,)).fetchone())

    def get_client_by_tg(self, tg_id: int):
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.tg_id = ?", (tg_id,)).fetchone())

    def get_client_by_invite(self, invite_code: str):
        """Ищет pending-клиента по неигашеному инвайт-коду."""
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.invite_code = ? AND c.activation_status = 'pending'",
            (invite_code,)).fetchone())

    def list_clients(self, include_service: bool = False,
                     exclude_tg: Optional[int] = None,
                     admin_first_tg: Optional[int] = None,
                     paused_only: bool = False,
                     active_finite_only: bool = False,
                     include_guests: bool = False) -> list:
        """Профили-владельцы. Гости (kind = guest) по умолчанию исключены: у них
        нет подписки, квоты и лимита — проверкам сроков, трафика, спискам админа
        и рассылке там делать нечего."""
        q = _CLIENT_SELECT + " WHERE 1=1"
        params: list = []
        if not include_service:
            q += " AND c.is_service = 0"
        if not include_guests:
            q += " AND c.kind = 'owner'"
        if paused_only:
            q += " AND p.pause_active_since IS NOT NULL"
        if active_finite_only:
            # активированные с конечным периодом — кандидаты проверки сроков;
            # бессрочные и неактивированные в Python отсеивались после полного JOIN
            q += " AND c.activation_status = 'active' AND s.period_end IS NOT NULL"
        if exclude_tg is not None:
            q += " AND (c.tg_id IS NULL OR c.tg_id != ?)"
            params.append(exclude_tg)
        # admin_first_tg: профиль администратора всегда идёт первым в списке.
        if admin_first_tg is not None:
            q += " ORDER BY (c.tg_id = ?) DESC, c.is_service DESC, c.name COLLATE NOCASE"
            params.append(admin_first_tg)
        else:
            q += " ORDER BY c.is_service DESC, c.name COLLATE NOCASE"
        return [_client_from_row(r) for r in
                self._connection().execute(q, params).fetchall()]

    def activate_client(self, client_id: int, tg_id: int) -> None:
        """Привязывает tg_id к клиенту и гасит инвайт-код (одноразовость)."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE clients
                   SET tg_id = ?, activation_status = 'active', invite_code = NULL
                   WHERE id = ?""",
                (tg_id, client_id),
            )

    # Карта: поле → (таблица, ключевая-колонка, ленивая-ли). Ленивые (grace/pause)
    # создаются строкой при первой записи. Идентити-поля — в clients.
    _CLIENT_FIELD_TABLE = {
        # clients
        "name": "clients", "device_limit": "clients", "tg_id": "clients",
        "activation_status": "clients", "invite_code": "clients", "block_reason": "clients",
        "routing_allowed": "clients", "kind": "clients", "tg_name": "clients",
        "tg_name_at": "clients", "tg_username": "clients",
        # client_subscription
        "period_start": "client_subscription", "period_end": "client_subscription",
        "period_kind": "client_subscription", "status": "client_subscription",
        "notified_thresholds": "client_subscription",
        # client_quota
        "traffic_limit": "client_quota", "bonus_bytes": "client_quota",
        "bonus_granted_month": "client_quota", "traffic_notified": "client_quota",
        # client_grace (ленивая)
        "grace_used": "client_grace", "grace_pending_cut": "client_grace",
        # client_pause (ленивая)
        "pause_active_since": "client_pause", "pause_reserved_days": "client_pause",
        "pause_used_days": "client_pause", "pause_balance_days": "client_pause",
        "pause_mode": "client_pause",
        "pause_saved_end": "client_pause", "resume_code": "client_pause",
    }
    _CLIENT_LAZY = {"client_grace", "client_pause"}
    _CLIENT_KEY = {"clients": "id", "client_subscription": "client_id",
                   "client_quota": "client_id", "client_grace": "client_id",
                   "client_pause": "client_id"}

    # ── Типизированные врайтеры процессов (pause/grace/friend) ───────────────
    # Явная альтернатива update_client_fields(pause_*=...): принимают доменный
    # под-объект целиком, делают семантику видимой в вызывающем коде. clear_*
    # удаляют ленивую строку (технический сброс; аудит идёт отдельно archive_*).

    def save_pause(self, client_id: int, pause: "models.PauseState") -> None:
        """Upsert строки паузы из доменного объекта (ленивая 1:1)."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO client_pause
                   (client_id, pause_active_since, pause_reserved_days,
                    pause_used_days, pause_balance_days, pause_mode, pause_saved_end,
                    resume_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_id) DO UPDATE SET
                     pause_active_since=excluded.pause_active_since,
                     pause_reserved_days=excluded.pause_reserved_days,
                     pause_used_days=excluded.pause_used_days,
                     pause_balance_days=excluded.pause_balance_days,
                     pause_mode=excluded.pause_mode,
                     pause_saved_end=excluded.pause_saved_end,
                     resume_code=excluded.resume_code""",
                (client_id, pause.active_since, pause.reserved_days,
                 pause.used_days, pause.balance_days, str(pause.mode) if pause.mode else None,
                 pause.saved_end, pause.resume_code))

    def set_pause_balance(self, client_id: int, days: int) -> None:
        """Счёт дней паузы — отдельно от эпизода: строка паузы ленивая, и
        после сброса периода (archive_pause удаляет её) баланс кладётся заново."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO client_pause (client_id, pause_balance_days) VALUES (?, ?)
                   ON CONFLICT(client_id) DO UPDATE SET pause_balance_days=excluded.pause_balance_days""",
                (client_id, max(0, int(days))))

    def monthly_renewals(self, client_id: int) -> int:
        """Сколько раз подписка продлевалась с ежемесячного периода — по
        снимкам закрытых периодов (для разового расчёта счёта паузы)."""
        row = self._connection().execute(
            "SELECT COUNT(*) AS n FROM client_subscription_histories "
            " WHERE client_id = ? AND period_kind = 'month' AND close_reason = 'renewed'",
            (client_id,)).fetchone()
        return int(row["n"])

    def update_client_fields(self, client_id: int, **fields) -> None:
        """Точечное обновление полей клиента с маршрутизацией по нормализованным
        таблицам. Ленивые таблицы (grace/pause) создаются строкой при первой
        записи (INSERT OR IGNORE), затем UPDATE. Всё — в одной транзакции."""
        # сгруппировать поля по таблицам
        by_table: dict[str, dict] = {}
        for k, v in fields.items():
            table = self._CLIENT_FIELD_TABLE.get(k)
            if table is None:
                # неизвестное поле — почти всегда опечатка вызывающего; молча
                # проглотить = тихо потерять запись. Падаем громко.
                raise ValueError(f"update_client_fields: неизвестное поле {k!r}")
            by_table.setdefault(table, {})[k] = v
        if not by_table:
            return
        with self._tx() as cur:
            for table, cols in by_table.items():
                key = self._CLIENT_KEY[table]
                if table in self._CLIENT_LAZY:
                    # гарантировать строку ленивой таблицы
                    cur.execute(
                        f"INSERT OR IGNORE INTO {table} ({key}) VALUES (?)", (client_id,))
                assignments = ", ".join(f"{c} = ?" for c in cols)
                values = list(cols.values()) + [client_id]
                cur.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", values)

    def delete_client(self, client_id: int, archive_reason: str = "deleted") -> None:
        """Удаляет клиента. Устройства уносятся каскадом (ON DELETE CASCADE).
        Служебного клиента удалять нельзя. Перед удалением — снимки в историю
        (клиент + все его устройства + их friend-эпизоды), если archive_reason
        задан (None у технических откатов)."""
        row = self.get_client(client_id)
        if row and row.is_service:
            raise ValueError("Нельзя удалить служебного клиента")
        with self._tx() as cur:
            if archive_reason:
                for dr in cur.execute("SELECT id FROM devices WHERE client_id = ?",
                                      (client_id,)).fetchall():
                    self.archive_friend(dr["id"], archive_reason, cur)
                    self.archive_device_snapshot(dr["id"], archive_reason, cur)
                # закрыть/снять все эпизоды клиента на момент смерти — иначе
                # CASCADE унесёт их без следа в аудите
                self.archive_subscription(client_id, archive_reason, cur)
                self.archive_quota(client_id, archive_reason, cur)
                self.archive_pause(client_id, archive_reason, cur)
                self.archive_grace(client_id, archive_reason, cur)
                self.archive_client_snapshot(client_id, archive_reason, cur)
            cur.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    # ── Пороги уведомлений (запечатаны: наружу — множество int) ───────────────

    def get_notified(self, client_id: int) -> set[int]:
        """Возвращает множество уже отправленных порогов (в днях/условных единицах).

        Внутреннее представление — CSV-строка; наружу отдаём set[int], чтобы
        остальной код не знал про сериализацию. Если однажды заменим на отдельную
        таблицу — меняются только get_notified/add_notified/reset_notified.
        """
        row = self._connection().execute(
            "SELECT notified_thresholds FROM client_subscription WHERE client_id = ?",
            (client_id,)).fetchone()
        if not row or not row["notified_thresholds"]:
            return set()
        return {int(x) for x in row["notified_thresholds"].split(",") if x.strip()}

    def add_notified(self, client_id: int, threshold: int) -> None:
        """Добавляет порог в множество отправленных (идемпотентно)."""
        current = self.get_notified(client_id)
        if threshold in current:
            return
        current.add(threshold)
        # Храним отсортированно для читабельности файла БД при отладке.
        csv = ",".join(str(x) for x in sorted(current))
        self.update_client_fields(client_id, notified_thresholds=csv)

    def reset_notified(self, client_id: int) -> None:
        """Обнуляет отправленные пороги (вызывается при создании нового периода)."""
        self.update_client_fields(client_id, notified_thresholds="")

    def get_traffic_notified(self, client_id: int) -> set[str]:
        """Множество уже отправленных трафик-уведомлений (строковые метки:
        'cli80','cli_over','bonus','dev80:{id}','dev_over:{id}'). Сбрасывается
        1-го числа вместе с накоплением. CSV внутри — set[str] наружу."""
        row = self._connection().execute(
            "SELECT traffic_notified FROM client_quota WHERE client_id = ?",
            (client_id,)).fetchone()
        if not row or not row["traffic_notified"]:
            return set()
        return {x for x in row["traffic_notified"].split(",") if x.strip()}

    def add_traffic_notified(self, client_id: int, marker: str) -> None:
        """Помечает трафик-уведомление отправленным (идемпотентно)."""
        cur = self.get_traffic_notified(client_id)
        if marker in cur:
            return
        cur.add(marker)
        self.update_client_fields(client_id, traffic_notified=",".join(sorted(cur)))

    def reset_traffic_notified(self, client_id: int) -> None:
        """Сброс трафик-меток (1-го числа, новый месяц)."""
        self.update_client_fields(client_id, traffic_notified="")

    _service_client_id: Optional[int] = None      # за жизнь процесса не меняется

    def get_service_client_id(self) -> int:
        if self._service_client_id is not None:
            return self._service_client_id
        row = self._connection().execute(
            "SELECT id FROM clients WHERE is_service = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Служебный клиент не инициализирован — вызовите init_schema()")
        self._service_client_id = int(row["id"])
        return self._service_client_id

    def find_client_by_resume_code(self, code: str) -> "Optional[int]":
        """id клиента с активной паузой и данным resume-кодом (или None).
        Матч только по активной паузе (pause_active_since NOT NULL) — код вне
        паузы недействителен. Точное сравнение (без LIKE), код чувствителен к
        регистру."""
        if not code:
            return None
        row = self._connection().execute(
            "SELECT client_id FROM client_pause "
            "WHERE resume_code = ? AND pause_active_since IS NOT NULL LIMIT 1",
            (code,)).fetchone()
        return row["client_id"] if row else None

    # ── Гости и имена Telegram ───────────────────────────────────────────────

    def clients_needing_tg_name(self, older_than_iso: str) -> list:
        """Профили с Telegram-аккаунтом, чьё имя аккаунта не знаем или знаем
        давно (tg_name_at < порога) — кандидаты фонового обновления через
        get_chat. Гости включены: их имя показывается владельцу."""
        return [_client_from_row(r) for r in self._connection().execute(
            _CLIENT_SELECT + " WHERE c.tg_id IS NOT NULL AND c.is_service = 0 "
            "AND (c.tg_name = '' OR c.tg_name_at IS NULL OR c.tg_name_at < ?) "
            "ORDER BY c.id", (older_than_iso,)).fetchall()]

    @staticmethod
    def _insert_guest(cur, tg_id: int, name: str) -> int:
        """Строки гостевого профиля: clients + пустые подписка и квота (у гостя
        их нет, но 1:1-таблицы обязаны существовать). Возвращает id."""
        cur.execute(
            """INSERT INTO clients (tg_id, name, device_limit, activation_status,
               invite_code, is_service, created_at, kind)
               VALUES (?, ?, 0, 'active', NULL, 0, ?, 'guest')""",
            (tg_id, name, _now_iso()))
        cid = cur.lastrowid
        cur.execute("INSERT INTO client_subscription (client_id, status) VALUES (?, 'active')", (cid,))
        cur.execute("INSERT INTO client_quota (client_id) VALUES (?)", (cid,))
        return cid

    def create_guest_client(self, tg_id: int, name: str) -> int:
        """Гостевой профиль (концепт «гость»): без подписки и лимита, сразу
        активен — держит переданные устройства одного владельца."""
        with self._tx() as cur:
            return self._insert_guest(cur, tg_id, name)

    def any_active_resume_code(self) -> bool:
        """Есть ли кому присылать код: активная пауза с resume_code. Пока нет —
        опрашивать почту незачем."""
        row = self._connection().execute(
            "SELECT 1 FROM client_pause WHERE pause_active_since IS NOT NULL "
            "AND resume_code IS NOT NULL AND resume_code != '' LIMIT 1").fetchone()
        return row is not None
