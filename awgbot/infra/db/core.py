"""core.py — ядро доступа: соединение на поток, транзакции, key-value
server_state и состояние переезда, от которого зависит видимость устройств.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from awgbot.core import access_cache as _access_cache
from awgbot.core import config

# Состояние переезда профилей. Константы живут ЗДЕСЬ, а domain/migration.py их
# импортирует: фильтру видимости устройств состояние нужно прямо в запросе, а
# infra не может тянуть domain. Дублирование строк уже успело разъехаться
# однажды — потому и константы.
MIGRATION_STATE_KEY = "migration_state"
MIGRATION_RUNNING = "running"


# ─────────────────────────────────────────────────────────────────────────────
# Подключение
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseCore:
    """Соединение и транзакции — основание, на котором стоят миксины Database."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        # Инициализируем соединение основного потока (для init_schema).
        self._connection()

    def _connection(self) -> sqlite3.Connection:
        """Соединение, привязанное к текущему потоку. services вызываются через
        asyncio.to_thread (пул потоков), поэтому соединение одно на все потоки
        использовать нельзя — sqlite3 не потокобезопасен на одном connection.
        WAL позволяет много читателей + одного писателя через РАЗНЫЕ соединения."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")     # иначе каскады не работают
            conn.execute("PRAGMA journal_mode = WAL")    # параллельное чтение
            # Каноничная пара к WAL: fsync только на чекпоинте, а не на каждом
            # commit. Целостность БД гарантирована при любом сбое; при потере
            # питания может пропасть только последняя транзакция (для наших
            # данных — одна 5-минутная выборка трафика, восполняется следующим
            # опросом). На VPS с медленным диском это главная экономия I/O.
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")   # мс: ждать до 5 с, а не падать на locked
            conn.commit()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """Транзакция с поддержкой вложенности: commit/rollback делает только
        внешний уровень. Внутри db.transaction() все операции — один атомарный
        commit (и один fsync вместо десятков)."""
        conn = self._connection()
        depth = getattr(self._local, "tx_depth", 0)
        self._local.tx_depth = depth + 1
        cur = conn.cursor()
        try:
            yield cur
            # Коммитим только если что-то писали: пустая транзакция (тик без
            # изменений) не должна стоить ни fsync, ни сброса кэша доступа.
            if depth == 0 and conn.in_transaction:
                conn.commit()
                self.__dict__["_commits"] = self.__dict__.get("_commits", 0) + 1
                _access_cache.invalidate_all()      # любая запись → middleware перечитает
        except Exception:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._local.tx_depth = depth
            cur.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Групповая транзакция: всё внутри — атомарно, один commit.
        Используется опросчиком трафика (целостность дельт + меньше fsync)."""
        with self._tx():
            yield


    # ── server_state: key-value ──────────────────────────────────────────────

    # Состояние переезда читает каждый list_devices/count_devices (видимость пар):
    # держим в памяти, инвалидация — в set_state. Ключи-«горячие» перечислены явно.
    _HOT_STATE_KEYS = (MIGRATION_STATE_KEY,)

    @property
    def _hot_state(self) -> dict:
        d = self.__dict__.get("_hot_state_map")
        if d is None:
            d = self.__dict__["_hot_state_map"] = {}
        return d

    def get_state(self, key: str) -> Optional[str]:
        if key in self._HOT_STATE_KEYS and key in self._hot_state:
            return self._hot_state[key]
        row = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (key,)
        ).fetchone()
        if key in self._HOT_STATE_KEYS:
            self._hot_state[key] = row["value"] if row else None
        return row["value"] if row else None

    def get_state_json(self, key: str, default):
        """Ключ state как JSON того же типа, что default (dict или list);
        пусто, мусор или другой тип — default. Дефолт не разделяется между
        вызовами: возвращается копия."""
        import json
        try:
            data = json.loads(self.get_state(key) or "null")
        except json.JSONDecodeError:
            data = None
        if isinstance(data, type(default)) and not isinstance(data, bool):
            return data
        return type(default)(default)

    @property
    def commits(self) -> int:
        """Сколько транзакций закоммичено этим экземпляром (для тестов и
        самопроверки: тик без изменений обязан стоить ноль коммитов)."""
        return self.__dict__.get("_commits", 0)

    def set_state(self, key: str, value: str) -> bool:
        """Записать ключ состояния; то же значение — НЕ пишется и не
        коммитится. Стрики, флаги и метки живости пишутся каждый тик, и до
        этого каждый вызов был транзакцией с fsync — ~10 000 записей в сутки на
        хост впустую, на малине это ресурс SD-карты. Возвращает, была ли запись."""
        row = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
        if row is not None and row["value"] == value:
            if key in self._HOT_STATE_KEYS:
                self._hot_state[key] = value
            return False
        if key in self._HOT_STATE_KEYS:
            self._hot_state.pop(key, None)
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO server_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
        return True

    def migration_visibility_running(self) -> bool:
        """Каким комплектом пары жить экранам и выдаче — новым или старым.

        Условие ОБЯЗАНО совпадать с services.migration_running: и состояние, и
        оба конфиг-ключа. Пока фильтр читал сырое состояние, очистка ключей в
        app.yaml (аварийный рубильник) выключала механику, но НЕ видимость —
        людям продолжали показываться двойники, чьи конфиги больше не выдаются.
        """
        return bool(config.MIGRATION_INTERFACE and config.MIGRATION_SUBNET_PREFIX
                    and (self.get_state(MIGRATION_STATE_KEY) or "") == MIGRATION_RUNNING)

    # Висячий twin_of (старую строку пары удалила сверка) читается как «пары
    # нет»: иначе после завершения переезда двойник с битой ссылкой пропадал бы
    # из ВСЕХ списков навсегда — пир работает, человек подключён, а устройства
    # нет ни у него, ни у админа.
    _TWIN_DANGLING_OK = ("(d.twin_of IS NULL OR NOT EXISTS "
                         "(SELECT 1 FROM devices o WHERE o.id = d.twin_of))")
