"""routing.py — условная маршрутизация: личные списки доменов, наборы адресов
для ipset, устройства и счётчики субъекта.
"""

from __future__ import annotations

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)

from .schema import _DEVICE_SELECT, _device_from_row


class RoutingMixin:

    # ── Условная маршрутизация ───────────────────────────────────────────────
    # Личные списки доменов и выборка адресов для реконсиляции наборов ipset.
    # Базовая часть (национальные зоны) здесь не живёт: она статична и лежит в
    # conf — в БД хранится только то, что завёл пользователь.

    # Все методы личных списков принимают РЕЖИМ. Списки у режимов раздельные:
    # запись означает «исключение из умолчания», а умолчания противоположны, и
    # перенос записи из одного списка в другой поменял бы её смысл на обратный.
    # Пользователю режим не показывается — он видит свой список того режима,
    # который выбрал админ, и знать про второй ему незачем.
    def move_routing_domains(self, src_client_id: int, dst_client_id: int) -> None:
        """Личный список адресов — с профиля на профиль (гость стал владельцем).
        Дубли у получателя не задваиваются (PK (client_id, domain))."""
        with self._tx() as cur:
            cur.execute("INSERT OR IGNORE INTO client_routing_domains (client_id, domain, added_at) "
                        "SELECT ?, domain, added_at FROM client_routing_domains WHERE client_id = ?",
                        (dst_client_id, src_client_id))
            cur.execute("DELETE FROM client_routing_domains WHERE client_id = ?", (src_client_id,))

    def list_routing_domains(self, client_id: int) -> list[str]:
        """Личный список клиента, в порядке добавления."""
        return [r["domain"] for r in self._connection().execute(
            "SELECT domain FROM client_routing_domains WHERE client_id = ? "
            "ORDER BY added_at, domain", (client_id,)).fetchall()]

    def add_routing_domain(self, client_id: int, domain: str) -> bool:
        """True — добавлен, False — уже был. Дедуп даёт PK, отдельной проверки
        (с гонкой между SELECT и INSERT) не требуется."""
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO client_routing_domains "
                "(client_id, domain, added_at) VALUES (?, ?, ?)",
                (client_id, domain, _now_iso()))
            return cur.rowcount > 0

    def remove_routing_domain(self, client_id: int, domain: str) -> bool:
        """True — удалён, False — такого и не было."""
        with self._tx() as cur:
            cur.execute(
                "DELETE FROM client_routing_domains "
                "WHERE client_id = ? AND domain = ?", (client_id, domain))
            return cur.rowcount > 0

    def clear_routing_domains(self, client_id: int) -> int:
        """Очистить список целиком. Возвращает число удалённых записей."""
        with self._tx() as cur:
            cur.execute("DELETE FROM client_routing_domains WHERE client_id = ?",
                        (client_id,))
            return cur.rowcount

    def routing_domains_by_client(self) -> dict[int, list[str]]:
        """{client_id: [домены]} по ВСЕМ клиентам — для генерации dnsmasq-конфига.
        Клиенты без личных доменов в результат не попадают."""
        out: dict[int, list[str]] = {}
        for r in self._connection().execute(
                "SELECT client_id, domain FROM client_routing_domains "
                "ORDER BY client_id, added_at, domain").fetchall():
            out.setdefault(int(r["client_id"]), []).append(r["domain"])
        return out

    def routing_allowed_client_ids(self, admin_tg_id: int = 0) -> list[int]:
        """Профили, которым РФ-доступ РАЗРЕШЁН, независимо от их тумблера.

        Отдельно от routing_active_addresses намеренно. Тот отвечает «чей трафик
        метить прямо сейчас» и меняется от каждого нажатия пользователя. Этот —
        «у кого вообще может быть свой набор», и меняется только решением админа.
        На первом держится состав ipset-правил, на втором — конфиг dnsmasq,
        который нельзя дёргать часто: его применение стоит рестарта резолвера.
        """
        rows = self._connection().execute(
            "SELECT id FROM clients "
            " WHERE is_service = 0 AND (routing_allowed = 1 OR tg_id = ?) "
            "UNION "
            # держатели чужих устройств от разрешённого владельца — свой набор
            # у каждого субъекта (docs/guest-role.md)
            "SELECT DISTINCT d.holder_client_id FROM devices d "
            "  JOIN clients oc ON oc.id = d.client_id "
            " WHERE d.holder_client_id IS NOT NULL "
            "   AND (oc.routing_allowed = 1 OR oc.tg_id = ?)",
            (admin_tg_id, admin_tg_id)).fetchall()
        return sorted(int(r["id"]) for r in rows)

    def routing_active_addresses(self, admin_tg_id: int = 0) -> dict[int, list[str]]:
        """{client_id: [адреса]} устройств с ВКЛЮЧЁННЫМ режимом.
        Источник истины для src-наборов ipset.

        Режим — свойство УСТРОЙСТВА, но только поверх разрешения админа: отзыв
        разрешения гасит эффект, не трогая флаги устройств. Вернул разрешение —
        у человека всё как было.

        Заблокированные устройства не отфильтровываем: DROP по адресу стоит
        раньше стадии маркировки, до неё пакет не доходит. Убирать их из набора
        значило бы дублировать инвариант блокировок вторым механизмом, который
        может с ним разойтись.
        """
        # Субъект — ДЕРЖАТЕЛЬ (docs/guest-role.md): переданное устройство идёт
        # в набор того, кто им управляет; разрешение — владельца устройства.
        out: dict[int, list[str]] = {}
        for r in self._connection().execute(
                """SELECT COALESCE(d.holder_client_id, d.client_id) AS subject, d.address
                     FROM devices d
                     JOIN clients c ON c.id = d.client_id
                    WHERE d.routing_on = 1
                      AND (c.routing_allowed = 1 OR c.tg_id = ?)
                      AND """ + self._NOT_GATEWAY + """
                    ORDER BY subject, d.address""",
                (admin_tg_id,)).fetchall():
            out.setdefault(int(r["subject"]), []).append(r["address"])
        return out

    # Устройства СУБЪЕКТА маршрутизации (docs/guest-role.md): свои, которые
    # никому не переданы, плюс чужие, которые он держит.
    _SUBJECT_WHERE = ("((d.client_id = ? AND d.holder_client_id IS NULL) "
                      "OR d.holder_client_id = ?)")
    # Устройство-шлюз (и его двойник в окне переезда) в РФ-доступе не участвует:
    # его российский трафик на ВПС вернулся бы по линку на ту же малину. Ни в
    # списке переключателей, ни в счётчиках «включено на N из M», ни в наборе
    # маркировки — а не только «снято при назначении»: флаг мог остаться от
    # прежней версии, где шлюз в списке был (и первым).
    _NOT_GATEWAY = ("NOT EXISTS (SELECT 1 FROM gateways g WHERE g.device_id = d.id "
                    "OR (d.twin_of IS NOT NULL AND g.device_id = d.twin_of))")

    def set_devices_routing(self, client_id: int, on: bool) -> int:
        """Включить/выключить режим на всех устройствах СУБЪЕКТА (свои
        непереданные + удерживаемые). Возвращает число изменённых строк — по
        нему видно, было ли действие холостым."""
        with self._tx() as cur:
            cur.execute("UPDATE devices AS d SET routing_on = ? "
                        f" WHERE {self._SUBJECT_WHERE} AND routing_on <> ? AND {self._NOT_GATEWAY}",
                        (1 if on else 0, client_id, client_id, 1 if on else 0))
            return cur.rowcount

    def routing_device_counts(self, client_id: int, admin_tg_id: int = 0) -> tuple[int, int]:
        """(включено, всего) по устройствам СУБЪЕКТА с разрешённым РФ-доступом.

        Состояние профиля ВЫВОДИТСЯ отсюда, отдельной колонки под него нет:
        «профиль включён» ⇔ включено хоть одно устройство. Пока состояние
        хранилось и на профиле, и на устройствах, эти двое могли разойтись —
        а сойтись обратно им было негде.

        Считаем по ВИДИМЫМ строкам: в окне переезда у каждого устройства их две,
        и сырой COUNT показывал бы человеку «включено на 6 из 12» при шести
        устройствах. Флаг у пары синхронный (сеттер парный), поэтому счёт по
        видимой половине точен.
        """
        row = self._connection().execute(
            f"SELECT COALESCE(SUM(d.routing_on), 0) AS on_, COUNT(*) AS n FROM devices d "
            f"{self._subject_visible_where(admin_tg_id)}",
            (client_id, client_id, admin_tg_id)).fetchone()
        return int(row["on_"]), int(row["n"])

    def _subject_visible_where(self, admin_tg_id: int) -> str:
        """Видимые устройства субъекта, у которых РФ-доступ РАЗРЕШЁН владельцем
        (свои — по своему разрешению, удерживаемые — по разрешению дарителя)."""
        elig = ("EXISTS (SELECT 1 FROM clients oc WHERE oc.id = d.client_id "
                "AND (oc.routing_allowed = 1 OR oc.tg_id = ?))")
        if self.migration_visibility_running():
            vis = "d.id NOT IN (SELECT twin_of FROM devices WHERE twin_of IS NOT NULL)"
        else:
            vis = self._TWIN_DANGLING_OK
        return f"WHERE {self._SUBJECT_WHERE} AND {elig} AND {vis} AND {self._NOT_GATEWAY}"

    def list_routing_devices(self, client_id: int, admin_tg_id: int = 0) -> list:
        """Устройства субъекта для экрана переключателей: свои непереданные,
        затем удерживаемые; только те, чей владелец разрешил РФ-доступ."""
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + f" {self._subject_visible_where(admin_tg_id)} "
            "ORDER BY (d.holder_client_id IS NOT NULL), is_gateway_eff DESC, d.created_at",
            (client_id, client_id, admin_tg_id)).fetchall()]

    def list_lent_out_devices(self, client_id: int) -> list:
        """Свои устройства профиля, переданные другим (управляет держатель)."""
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + " WHERE d.client_id = ? AND d.holder_client_id IS NOT NULL "
            f"AND {self._friend_visible_where()} ORDER BY d.created_at, d.id",
            (client_id,)).fetchall()]

    def set_owner_devices_routing(self, client_id: int, on: bool) -> int:
        """Все устройства ВЛАДЕЛЬЦА, включая переданные, — при выдаче
        разрешения: держателям обещано «включено для всех твоих устройств»."""
        with self._tx() as cur:
            cur.execute("UPDATE devices SET routing_on = ? "
                        " WHERE client_id = ? AND routing_on <> ?",
                        (1 if on else 0, client_id, 1 if on else 0))
            return cur.rowcount
