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

# Состояние переезда профилей. Константы живут ЗДЕСЬ, а domain/migration/ их
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
        self._local = threading.local()                # соединение на поток; close() подменяет хранилище целиком
        self._txl = threading.local()                  # транзакция потока (глубина, вид, её соединение); close() не подменяет
        # Инициализируем соединение основного потока (для init_schema).
        self._connection()

    def _connection(self) -> sqlite3.Connection:
        """Соединение, привязанное к текущему потоку. services вызываются через
        asyncio.to_thread (пул потоков), поэтому соединение одно на все потоки
        использовать нельзя — sqlite3 не потокобезопасен на одном connection.
        WAL позволяет много читателей + одного писателя через РАЗНЫЕ соединения.
        Внутри транзакции — её соединение, даже если close() уже подменил
        хранилище: новое соединение того же потока ждало бы замок записи,
        который держит его же транзакция (5 с и «database is locked»)."""
        conn = getattr(self._txl, "conn", None)
        if conn is not None:
            return conn
        conn = getattr(self._local, "conn", None)
        if conn is not None and not getattr(self._local, "jrn_ok", False) and self.__dict__.get("_jrn_ready"):
            self._jrn_triggers(conn)                    # соединение открыто до init_schema — триггеры теперь
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
            self._jrn_triggers(conn)
        return conn

    def _jrn_triggers(self, conn: sqlite3.Connection | None = None) -> None:
        """Запрет записи решений не держателю эпохи — временными триггерами на
        соединение (util/rsvjrn): мимо них из процесса не пишет никто. До
        init_schema таблиц нет — init_schema зовёт снова."""
        from awgbot.util.rsvjrn import triggers
        conn = conn or self._connection()
        try:
            self._local.jrn_ok = triggers.install(conn, guest=bool(getattr(config, "GUEST", False)))
        except sqlite3.Error as e:                          # повреждённая схема — пусть падает запрос, а не соединение
            self._local.jrn_ok = True                       # не повторять на каждом запросе
            import logging
            logging.getLogger("awgbot.db").error("журнал: триггеры запрета не поставлены: %s", e)

    def close(self) -> None:
        """Закрыть соединение этого потока и отпустить соединения остальных.
        services ходят в базу из пула потоков asyncio.to_thread: соединение
        пула, оставшееся открытым, держит файлы базы, WAL и shm (в эталонах
        экранов с базой на каждый снимок это упиралось в предел открытых
        файлов). Закрывать чужое соединение из этого потока нельзя: поток пула
        может быть внутри запроса на нём — CPython падает (Segmentation fault).
        Поэтому хранилище соединений подменяется целиком: у прежнего не остаётся
        ссылок, кроме как из потока, который сейчас им пользуется, и оно
        закрывается по счётчику ссылок, как только тот закончит; следующий
        запрос любого потока открывает новое соединение. Транзакцию потока
        подмена не задевает: она доживает на своём соединении (оно у неё,
        а не в хранилище), своё же соединение посреди транзакции закрывается
        на выходе из неё."""
        own = getattr(self._txl, "conn", None)
        local, self._local = self._local, threading.local()
        conn = getattr(local, "conn", None)
        if conn is not None:
            local.conn = None
            if conn is own:
                self._txl.close_after = True            # своя транзакция идёт — закроем на выходе из неё
            else:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        del local

    # ── вид транзакции и крючок коммита (пакет переезда резерву) ─────────────
    # Любая запись в данные пользователей — повод обновить пакет переезда у резерва;
    # запись счётчиков (трафик, рукопожатия, отметки опроса) — нет: её пишет
    # каждый опрос, и пакет каждые 5 минут был бы маячком. Поэтому писатели
    # счётчиков помечают транзакцию «counters», всё прочее — «data»; крючок
    # коммита получает вид внешней транзакции. Внутри вложенных — старшинство
    # у «data». Крючок — лёгкий: отметка в памяти, не запись в базу.
    _TX_COUNTERS = "counters"
    _TX_DATA = "data"

    def _tx_note(self, kind: str) -> None:
        """Писатели счётчиков зовут ПОСЛЕ своих записей: запоминаем счётчик
        изменений соединения — всё, что запишут после, считается данными
        (непомеченный писатель ничего не отмечает, но пишет)."""
        if kind == self._TX_DATA or getattr(self._txl, "kind", None) is None:
            self._txl.kind = kind
        if kind == self._TX_COUNTERS:
            self._txl.counters_changes = self._connection().total_changes

    def set_commit_hook(self, fn) -> None:
        """fn(kind) — после каждого коммита внешней транзакции; None — снять."""
        self.__dict__["_commit_hook"] = fn

    @contextmanager
    def _tx(self, immediate: bool = False) -> Iterator[sqlite3.Cursor]:
        """Транзакция с поддержкой вложенности: commit/rollback делает только
        внешний уровень. Внутри db.transaction() все операции — один атомарный
        commit (и один fsync вместо десятков). immediate — см. transaction()."""
        conn = self._connection()
        depth = getattr(self._txl, "depth", 0)
        self._txl.depth = depth + 1
        quiet = -1                                     # total_changes на BEGIN IMMEDIATE
        if depth == 0:
            self._txl.conn = conn
            self._txl.kind = None
            self._txl.counters_changes = -1
            if immediate and not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                quiet = conn.total_changes
        cur = conn.cursor()
        try:
            yield cur
            if depth == 0 and conn.in_transaction and conn.total_changes == quiet:
                conn.commit()                          # замок взят, писать не пришлось — только отпустить
            # Коммитим только если что-то писали: пустая транзакция (тик без
            # изменений) не должна стоить ни fsync, ни сброса кэша доступа.
            elif depth == 0 and conn.in_transaction:
                conn.commit()
                self.__dict__["_commits"] = self.__dict__.get("_commits", 0) + 1
                self._hot_state_drop()
                _access_cache.invalidate_all()      # любая запись → middleware перечитает
                hook = self.__dict__.get("_commit_hook")
                if hook is not None:
                    kind = getattr(self._txl, "kind", None) or self._TX_DATA
                    if kind == self._TX_COUNTERS and conn.total_changes != getattr(self._txl, "counters_changes", -1):
                        kind = self._TX_DATA           # после отметки счётчиков писали ещё — это данные
                    try:
                        hook(kind)
                    except Exception:                  # noqa: BLE001 — крючок не роняет запись
                        pass
        except Exception:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._txl.depth = depth
            cur.close()
            if depth == 0:
                self._txl.conn = None
                if getattr(self._txl, "close_after", False):
                    self._txl.close_after = False
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[None]:
        """Групповая транзакция: всё внутри — атомарно, один commit.
        Используется опросчиком трафика (целостность дельт + меньше fsync).

        immediate — замок записи с первой строки (BEGIN IMMEDIATE), а не с
        первой записи: для «прочитал — решил — записал», где решение по
        прочитанному до замка затёрло бы параллельную правку (сроки против
        продления, лимиты против месячного сброса, паузы, перепривязка). Не по
        умолчанию: длинная транзакция опросчика держала бы замок все свои
        чтения. Внутри уже идущей транзакции — без эффекта (её вид решил
        внешний уровень)."""
        with self._tx(immediate):
            yield


    # ── server_state: key-value ──────────────────────────────────────────────

    # Состояние переезда читает каждый list_devices/count_devices (видимость пар):
    # держим в памяти, инвалидация — на каждом коммите (_tx и сырые коммиты
    # применителя журнала): горячий ключ пишет не только set_state, но и
    # применение событий держателя, и перенос хвоста. Ключи-«горячие» — явно.
    _HOT_STATE_KEYS = (MIGRATION_STATE_KEY,)

    def _hot_state_drop(self) -> None:
        """Забыть горячие ключи после коммита. Поколение — против гонки: поток,
        прочитавший базу ДО коммита, не кладёт в кэш прочитанное после сброса."""
        self.__dict__["_hot_gen"] = self.__dict__.get("_hot_gen", 0) + 1
        self._hot_state.clear()

    @property
    def _hot_state(self) -> dict:
        d = self.__dict__.get("_hot_state_map")
        if d is None:
            d = self.__dict__["_hot_state_map"] = {}
        return d

    def get_state(self, key: str) -> Optional[str]:
        if key in self._HOT_STATE_KEYS and key in self._hot_state:
            return self._hot_state[key]
        gen = self.__dict__.get("_hot_gen", 0)
        row = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (key,)
        ).fetchone()
        if key in self._HOT_STATE_KEYS and gen == self.__dict__.get("_hot_gen", 0):
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
        хост впустую, на шлюзе это ресурс SD-карты. Возвращает, была ли запись."""
        row = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
        if row is not None and row["value"] == value:
            if key in self._HOT_STATE_KEYS:
                self._hot_state[key] = value
            return False
        with self._tx() as cur:
            # состояние — данные пользователей только для ключей белого списка пакета
            # (почта выхода из паузы и подобное); стрики, отметки и ключи резерва — счётчики
            from awgbot.util.rsvpkg.bot import GUEST_STATE
            cur.execute(
                """INSERT INTO server_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )
            self._tx_note(self._TX_DATA if key in GUEST_STATE else self._TX_COUNTERS)   # отметка — ПОСЛЕ записи
        if key in self._HOT_STATE_KEYS:
            # ПОСЛЕ записи: сброс до неё давал другому потоку положить в кэш
            # старое значение, и двойники не рождались до следующей записи
            self._hot_state[key] = value
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
