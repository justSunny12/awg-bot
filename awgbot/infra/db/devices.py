"""devices.py — устройства (AWG-пиры): создание, видимость в окне переезда,
держатели и приглашения, когорта переезда, шлюз, точечные обновления,
аллокация IP.
"""

from __future__ import annotations

from typing import Optional

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)

from .schema import _DEVICE_SELECT, _device_from_row, _gateway_from_row


class DevicesMixin:

    # ── Устройства ───────────────────────────────────────────────────────────

    def create_device(
        self,
        client_id: int,
        name: str,
        public_key: str,
        preshared_key: str,
        address: str,
        private_key: Optional[str] = None,
        traffic_limit: int = 0,
        iface: str = "",
        twin_of: Optional[int] = None,
        block_reason: int = 0,
    ) -> int:
        """Создаёт устройство. Без private_key — чужой пир из конфига (ключа у нас нет).
        Заводит сопутствующую 1:1 строку счётчиков. Возвращает id устройства.

        block_reason параметром, а не всегда нулём: двойник заблокированного
        устройства обязан родиться заблокированным. Ждать ближайшего
        reconcile_blocks значило бы дарить окно в минуты, за которое переезд
        оказывается амнистией.
        """
        self.__dict__.pop("_ifaces_cache", None)
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO devices
                   (client_id, name, private_key, public_key,
                    preshared_key, address, block_reason, iface, twin_of, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_id, name, private_key, public_key,
                 preshared_key, address, int(block_reason), iface or "", twin_of,
                 _now_iso()))
            did = cur.lastrowid
            cur.execute(
                "INSERT INTO device_traffic (device_id, traffic_limit) VALUES (?, ?)",
                (did, traffic_limit))
            return did

    def get_device(self, device_id: int):
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE d.id = ?", (device_id,)).fetchone())

    def get_device_by_friend_code(self, code: str):
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE f.friend_code = ?", (code,)).fetchone())

    def _friend_visible_where(self) -> str:
        """Гостевые выборки живут по ТОМУ ЖЕ правилу видимости, что и список
        владельца: активная связь при рождении двойника копируется на обе
        строки, и без фильтра друг видел бы устройство дважды — и мог бы
        вытащить конфиг уходящего интерфейса."""
        if self.migration_visibility_running():
            return ("d.id NOT IN "
                    "(SELECT twin_of FROM devices WHERE twin_of IS NOT NULL)")
        return self._TWIN_DANGLING_OK

    def list_held_devices(self, client_id: int) -> list:
        """Переданные устройства, которые держит профиль (чужие, но управляет он).
        По одной строке на устройство — то же правило видимости, что у списка
        владельца."""
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + " WHERE d.holder_client_id = ?"
            f" AND {self._friend_visible_where()} ORDER BY d.created_at, d.id",
            (client_id,)).fetchall()]

    def set_device_holder(self, device_id: int, holder_client_id: Optional[int]) -> None:
        """Назначить держателя (None — устройство снова своё у владельца).
        Ожидающее приглашение при этом снимается: оно исполнено или отменено."""
        with self._tx() as cur:
            cur.execute("UPDATE devices SET holder_client_id = ? WHERE id = ?",
                        (holder_client_id, device_id))
            cur.execute("DELETE FROM device_friend WHERE device_id = ?", (device_id,))

    def set_device_friend(self, device_id: int, *, friend_code=None, friend_status=None) -> None:
        """Ожидающее приглашение на устройстве. Ленивая 1:1: оба поля пусты —
        строку удаляем (приглашения нет); иначе upsert."""
        with self._tx() as cur:
            if friend_code is None and friend_status is None:
                cur.execute("DELETE FROM device_friend WHERE device_id = ?", (device_id,))
            else:
                cur.execute(
                    "INSERT INTO device_friend (device_id, friend_code, friend_status) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "friend_code=excluded.friend_code, friend_status=excluded.friend_status",
                    (device_id, friend_code, friend_status))

    def list_devices(self, client_id: int, all_rows: bool = False) -> list:
        """Устройства профиля — ПО ОДНОЙ строке на устройство.

        В окне переезда каждое устройство представлено парой строк на двух
        интерфейсах. Показывать обе значит показать человеку шесть устройств
        вместо трёх, а выдать конфиг не той — вручить пира уходящего интерфейса.
        Поэтому видимость решается здесь, а не у двенадцати вызывающих: пропусти
        одного — и он покажет лишнее или выдаст не то.

        Идёт переезд → видна НОВАЯ строка пары. Не идёт → СТАРАЯ: так выглядит
        состояние после отмены, где двойники живы, но выдаются старые конфиги.
        После завершения пар нет вовсе (twin_of обнулён), и правило вырождается.

        all_rows=True — для тех, кому нужны обе: потребление в окне размазано по
        паре, и лимит, посчитанный по одной строке, дал бы двойную квоту.
        """
        if all_rows:
            where = "WHERE d.client_id = ?"
        elif self.migration_visibility_running():
            where = ("WHERE d.client_id = ? AND d.id NOT IN "
                     "(SELECT twin_of FROM devices WHERE twin_of IS NOT NULL)")
        else:
            where = f"WHERE d.client_id = ? AND {self._TWIN_DANGLING_OK}"
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + f" {where} ORDER BY is_gateway_eff DESC, d.created_at",
            (client_id,)).fetchall()]

    def list_all_devices(self) -> list:
        return [_device_from_row(r) for r in
                self._connection().execute(_DEVICE_SELECT).fetchall()]

    # ── Когорта переезда ─────────────────────────────────────────────────────

    def cohort_set(self, device_ids) -> None:
        """Заморозить когорту. Идемпотентно: повторное включение рычага после
        сбоя на середине не задваивает и не теряет."""
        with self._tx() as cur:
            cur.execute("DELETE FROM migration_cohort")
            cur.executemany("INSERT INTO migration_cohort (device_id) VALUES (?)",
                            [(int(d),) for d in device_ids])

    def cohort_ids(self) -> set[int]:
        """id старых устройств когорты. Удалённые выпадают сами — ON DELETE
        CASCADE: устройство, снесённое в окне, не должно вечно держать
        знаменатель и делать завершение недостижимым."""
        return {int(r["device_id"]) for r in
                self._connection().execute("SELECT device_id FROM migration_cohort")}

    def cohort_clear(self) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM migration_cohort")

    def twins_by_origin(self) -> dict[int, int]:
        """twin_of → id двойника. Одним запросом вместо обхода: пара нужна и
        прогрессу, и слиянию истории, и парным операциям."""
        return {int(r["twin_of"]): int(r["id"]) for r in self._connection().execute(
            "SELECT id, twin_of FROM devices WHERE twin_of IS NOT NULL")}

    def normalize_default_iface(self, default: str) -> int:
        """Схлопнуть явное значение iface, равное дефолтному, в пустую строку.

        Эндшпиль переезда: админ переключает docker.interface на новый и
        перезапускает бота — с этого момента явное 'awg1' и пустое '' значат
        одно и то же, а две записи одного смысла это тот класс расхождения,
        который здесь уже стрелял. Зовётся на старте; вне эндшпиля — ноль строк,
        холостой UPDATE.
        """
        with self._tx() as cur:
            cur.execute("UPDATE devices SET iface = '' WHERE iface = ?", (default,))
            return cur.rowcount

    def distinct_ifaces(self) -> list[str]:
        """Сырые значения devices.iface, встречающиеся в базе. Пустая строка в
        выдаче остаётся пустой: разрешать её в имя — дело awg.iface_of, здесь мы
        не знаем и не должны знать, какой интерфейс сейчас дефолтный."""
        import time as _t
        cached = getattr(self, "_ifaces_cache", None)
        if cached and _t.monotonic() - cached[0] < 60:
            return list(cached[1])
        out = [r["iface"] for r in self._connection().execute(
            "SELECT DISTINCT iface FROM devices ORDER BY iface").fetchall()]
        self._ifaces_cache = (_t.monotonic(), out)
        return list(out)

    def _visible_where(self, all_rows: bool = False) -> str:
        """Предикат видимости строк устройства — тот же, что в list_devices."""
        if all_rows:
            return "WHERE d.client_id = ?"
        if self.migration_visibility_running():
            return ("WHERE d.client_id = ? AND d.id NOT IN "
                    "(SELECT twin_of FROM devices WHERE twin_of IS NOT NULL)")
        return f"WHERE d.client_id = ? AND {self._TWIN_DANGLING_OK}"

    def count_devices(self, client_id: int) -> int:
        """Столько устройств у человека с его точки зрения — по видимым строкам.
        Считать пары значило бы упереться в лимит вдвое раньше, чем следует.
        COUNT, а не len(list_devices): без JOIN и без сборки моделей.

        ШЛЮЗ СЧИТАЕТСЯ. Прежде он вычитался, и счётчик расходился со списком:
        «устройств добавлено 5», а в списке шесть строк. Шлюзом может быть
        только устройство админа, а его профиль безлимитный — прятать одну
        строку ради лимита, которого нет, незачем.
        """
        row = self._connection().execute(
            f"SELECT COUNT(*) AS n FROM devices d {self._visible_where()}",
            (client_id,)).fetchone()
        return int(row["n"])

    def client_has_online_device(self, client_id: int, threshold_seconds: int,
                                 ref_ts: Optional[int] = None) -> bool:
        """Есть ли у профиля устройство с хендшейком свежее порога — одним
        индексным запросом, без выборки всех устройств. ref_ts — момент, от
        которого считать порог (последний опрос пиров, см. services.online_ref);
        пусто — сейчас."""
        import time as _t
        floor = int(ref_ts if ref_ts is not None else _t.time()) - int(threshold_seconds)
        row = self._connection().execute(
            "SELECT 1 FROM devices d JOIN device_traffic t ON t.device_id = d.id "
            "WHERE d.client_id = ? AND t.last_handshake IS NOT NULL AND t.last_handshake >= ? LIMIT 1",
            (client_id, floor)).fetchone()
        return row is not None

    def blocked_addresses(self) -> list[str]:
        """Адреса устройств с любой причиной блокировки — для реконсиляции DROP."""
        return [r["address"] for r in self._connection().execute(
            "SELECT address FROM devices WHERE block_reason != 0")]

    def pending_twins(self) -> list:
        """Двойники переезда без единого хендшейка — те, кого ждёт окно."""
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + " WHERE d.twin_of IS NOT NULL "
            "AND (t.last_handshake IS NULL OR t.last_handshake = 0)").fetchall()]

    def admin_device_addresses(self, admin_tg_id: int) -> list[str]:
        """Адреса (10.8.1.X) всех устройств, чей владелец — админ (по tg_id).
        Источник вайтлиста для пер-пирного SSH-к-хосту (reconcile_ssh_access).
        Служебный клиент («устройства без профиля») исключён явно: сегодня у него
        нет tg_id, но SSH-вайтлист не должен зависеть от этого неявно."""
        rows = self._connection().execute(
            "SELECT d.address FROM devices d "
            "JOIN clients c ON c.id = d.client_id "
            "WHERE c.tg_id = ? AND c.is_service = 0", (admin_tg_id,)).fetchall()
        return [r["address"] for r in rows]

    _DEVICE_FIELD_TABLE = {
        "name": "devices", "private_key": "devices",
        "block_reason": "devices", "client_id": "devices",
        "routing_on": "devices", "holder_client_id": "devices",
        "iface": "devices", "twin_of": "devices",
        "public_key": "devices", "preshared_key": "devices", "address": "devices",
        "traffic_limit": "device_traffic", "traffic_rx_month": "device_traffic",
        "traffic_tx_month": "device_traffic", "traffic_rx_period": "device_traffic",
        "traffic_tx_period": "device_traffic", "last_handshake": "device_traffic",
        "missing_count": "device_traffic",
    }
    _DEVICE_KEY = {"devices": "id", "device_traffic": "device_id"}

    def update_device_fields(self, device_id: int, **fields) -> None:
        if "iface" in fields:
            self.__dict__.pop("_ifaces_cache", None)
        """Точечное обновление полей устройства с маршрутизацией: identity/crypto →
        devices, счётчики/лимит → device_traffic. Одна транзакция."""
        by_table: dict[str, dict] = {}
        for k, v in fields.items():
            table = self._DEVICE_FIELD_TABLE.get(k)
            if table is None:
                raise ValueError(f"update_device_fields: неизвестное поле {k!r}")
            by_table.setdefault(table, {})[k] = v
        if not by_table:
            return
        with self._tx() as cur:
            for table, cols in by_table.items():
                key = self._DEVICE_KEY[table]
                assignments = ", ".join(f"{c} = ?" for c in cols)
                values = list(cols.values()) + [device_id]
                cur.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", values)

    def claim_first_handshake(self, device_id: int, value: str) -> bool:
        """Записать ПЕРВЫЙ хендшейк устройства; True — только тому, кто записал.

        За этой строкой следят два такта — частый тик переезда и пятиминутный
        опрос трафика, — а «увидел впервые» обязано достаться ровно одному:
        иначе человек получит два одинаковых поздравления. Соединения у потоков
        РАЗНЫЕ (threading.local), так что гонка настоящая, а не теоретическая.

        Условие живёт в самом UPDATE, а не в проверке перед ним: между «прочитал
        пусто» и «записал» второй поток успевает сделать то же самое.
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE device_traffic SET last_handshake = ? WHERE device_id = ? "
                "AND (last_handshake IS NULL OR last_handshake = '')",
                (value, device_id))
            return cur.rowcount == 1

    # ── слоты шлюзов условной маршрутизации (концепт «резервный шлюз») ───────
    # Порядок везде один: предпочтительный первым, затем по номеру слота. Он же
    # решает споры — кандидат на переключение, домашняя подсеть у двух слотов.
    _GW_ORDER = " ORDER BY preferred DESC, id"

    def gateways(self) -> list:
        return [_gateway_from_row(r) for r in self._connection().execute(
            "SELECT * FROM gateways" + self._GW_ORDER).fetchall()]

    def gateway(self, slot_id: int):
        return _gateway_from_row(self._connection().execute(
            "SELECT * FROM gateways WHERE id = ?", (int(slot_id),)).fetchone())

    def gateway_by_device(self, device_id: int):
        """Слот устройства; в окне переезда двойник находит слот по оригиналу пары."""
        return _gateway_from_row(self._connection().execute(
            "SELECT g.* FROM gateways g JOIN devices d ON d.id = ? "
            "WHERE g.device_id = d.id OR (d.twin_of IS NOT NULL AND g.device_id = d.twin_of)",
            (int(device_id),)).fetchone())

    def gateway_add(self, device_id: int, link_if: str, link_port: int, link_cidr: str,
                    *, preferred: bool = False, slot_id: Optional[int] = None):
        """Новый слот: номер — следующий свободный (или заданный), предпочтительный
        — если попросили или это первый слот вовсе."""
        with self._tx() as cur:
            if slot_id is None:
                row = cur.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM gateways").fetchone()
                slot_id = int(row["n"])
            first = cur.execute("SELECT COUNT(*) AS n FROM gateways").fetchone()["n"] == 0
            if preferred or first:
                cur.execute("UPDATE gateways SET preferred = 0 WHERE preferred = 1")
            cur.execute(
                "INSERT INTO gateways(id, device_id, link_if, link_port, link_cidr, preferred, "
                "home_subnets, label, created_at) VALUES (?, ?, ?, ?, ?, ?, '', '', ?)",
                (int(slot_id), int(device_id), link_if, int(link_port), link_cidr,
                 1 if (preferred or first) else 0, _now_iso()))
        return self.gateway(slot_id)

    def gateway_update(self, slot_id: int, **fields) -> None:
        """Точечное обновление: device_id (финал переезда), home_subnets (список
        или строка), label."""
        allowed = {"device_id", "home_subnets", "label", "link_port", "lan_mode"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"gateway_update: неизвестные поля {sorted(bad)}")
        if not fields:
            return
        if "home_subnets" in fields and not isinstance(fields["home_subnets"], str):
            fields["home_subnets"] = " ".join(str(n) for n in fields["home_subnets"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as cur:
            cur.execute(f"UPDATE gateways SET {sets} WHERE id = ?",
                        (*fields.values(), int(slot_id)))

    def gateway_set_preferred(self, slot_id: Optional[int]) -> None:
        """Снять галочку со всех и поставить одному (None — только снять), одной
        транзакцией: частичный уникальный индекс не даст двух даже на миг."""
        with self._tx() as cur:
            cur.execute("UPDATE gateways SET preferred = 0 WHERE preferred = 1")
            if slot_id is not None:
                cur.execute("UPDATE gateways SET preferred = 1 WHERE id = ?", (int(slot_id),))

    def gateway_delete(self, slot_id: int) -> None:
        with self._tx() as cur:
            cur.execute("DELETE FROM gateways WHERE id = ?", (int(slot_id),))

    def twin_of_device(self, device_id: int):
        """Двойник устройства в окне переезда, если есть."""
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE d.twin_of = ?", (device_id,)).fetchone())

    def get_device_by_pubkey(self, public_key: str):
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE d.public_key = ?", (public_key,)).fetchone())

    def delete_device(self, device_id: int, archive_reason: str = "deleted") -> None:
        """Удаляет устройство. Перед удалением — снимок в историю + закрытие
        friend-эпизода, если archive_reason задан (None у технических откатов)."""
        with self._tx() as cur:
            if archive_reason:
                self.archive_friend(device_id, archive_reason, cur)
                self.archive_device_snapshot(device_id, archive_reason, cur)
            cur.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def reassign_device(self, device_id: int, new_client_id: int) -> None:
        """Привязка устройства без профиля к реальному клиенту."""
        self.update_device_fields(device_id, client_id=new_client_id)

    # ── Аллокация IP ─────────────────────────────────────────────────────────

    def allocate_ip(
        self,
        subnet_prefix: str = "10.8.1",
        occupied_extra: Optional[set[str]] = None,
        start_host: int = 1,
        end_host: int = 254,
    ) -> str:
        """Возвращает первый свободный адрес вида {subnet_prefix}.N.

        Занятые адреса берём из БД И из occupied_extra (адреса из живого awg0.conf,
        чтобы учесть пиры, которых может ещё не быть в БД). Это важно:
        приложение и бот делят один пул и одну логику «первый свободный», поэтому
        источником занятости должен быть реальный конфиг, а не только БД.

        start_host=1 потому что в докерной Amnezia сервер занимает .0, клиенты — с .1.
        """
        occupied: set[str] = {
            r["address"] for r in self._connection().execute("SELECT address FROM devices")
        }
        if occupied_extra:
            occupied |= occupied_extra
        for n in range(start_host, end_host + 1):
            candidate = f"{subnet_prefix}.{n}"
            if candidate not in occupied:
                return candidate
        raise RuntimeError("Пул IP исчерпан")
