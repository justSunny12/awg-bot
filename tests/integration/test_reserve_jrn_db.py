"""Запрет записи решений у следующего — в самой базе (infra/db/jrn.py,
util/rsvjrn: временные триггеры каждого соединения по реестру классов данных).

Основа, чьи пользователи и бот у резерва, следует за ботом основы на резерве:
решения (профили, устройства, сроки, паузы, квоты, приглашения, маски
блокировок, ключи `server_state` из белого списка реплики) ей пишет только
держатель эпохи. База отказывает `IntegrityError` с префиксом `jrn:`, а
счётчики (трафик, рукопожатие) и местное (сэмплы, `rsv_*`, меню чатов,
`online_count`) пишутся как прежде.

Цена ошибки: запрет, который пропускает хоть одну колонку решения, — основа
в отъезде перерешает её по устаревшей базе (истечёт продлённую подписку,
заблокирует того, кому на резерве подняли лимит), и слияние при возврате
унесёт это решение в общую базу; запрет, который задевает счётчики, — опрос
трафика падает целиком, и месячный расход пользователей, ещё сидящих на
основе, теряется; отказ, откативший только часть транзакции, — дельта
трафика записана без своей базы и при следующем опросе посчитается дважды;
триггер, ушедший в файл базы, — бэкап и реплика несут запрет на другой
сервер, и там решения не пишет уже никто; отметка `applying`, пережившая
падение процесса, — запрет молча выключен до следующего перезапуска.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import os
import shutil
import sqlite3

import pytest

from awgbot.infra.db import Database
from awgbot.util import rsvjrn, timeutil
from awgbot.util.rsvjrn import classes, triggers

pytestmark = pytest.mark.integration


def follow(db) -> None:
    db.jrn_pass(holder="peer", role="follower", why="тест: следование")


def hold(db) -> None:
    db.jrn_pass(holder="self", role="holder", why="тест: держатель")


def refused(fn) -> bool:
    """True — база отказала запретом эпохи; другой IntegrityError — наверх."""
    try:
        fn()
    except sqlite3.IntegrityError as e:
        if rsvjrn.is_refusal(e):
            return True
        raise
    return False


@pytest.fixture()
def world(services, make_active_client):
    """Держатель завёл профиль «Иван» с устройством, приглашением на устройство,
    доменом маршрутизации, паузой и отсрочкой; затем основа следует."""
    db = services.db
    ivan = make_active_client(name="Иван", tg_id=7100)
    dev = services.add_device(ivan.id, "Телефон")
    db.set_device_friend(dev.device_id, friend_code="DUMMY-code", friend_status="pending")
    db.add_routing_domain(ivan.id, "example.org")
    db.cohort_set([dev.device_id])
    db.set_pause_balance(ivan.id, 5)
    with db._tx() as cur:
        cur.execute("INSERT OR IGNORE INTO client_grace (client_id, grace_used) VALUES (?, 0)", (ivan.id,))
    follow(db)
    return type("W", (), {"svc": services, "db": db, "cid": ivan.id, "did": dev.device_id})


def _month(db, did) -> tuple[int, int]:
    d = db.get_device(did)
    return int(d.traffic_rx_month), int(d.traffic_tx_month)


# ── эпоха: строка, роль, переход ────────────────────────────────────────────

def test_a_fresh_base_holds_the_epoch_and_a_pass_bumps_it(db):
    """Новая база — держатель здесь, эпоха 0; каждая передача — +1, повтор той
    же роли — не передача (None, номер не растёт)."""
    meta = db.jrn_meta()
    assert (meta["epoch"], meta["holder"], meta["role"]) == (0, "self", "holder"), meta
    assert db.jrn_following() is False
    first = db.jrn_pass(holder="peer", role="follower", why="тест")
    assert first is not None and (first["epoch"], first["role"]) == (1, "follower"), first
    assert db.jrn_following() is True
    assert db.jrn_pass(holder="peer", role="follower", why="повтор") is None, "повтор той же роли — новая эпоха"
    assert db.jrn_meta()["epoch"] == 1
    back = db.jrn_pass(holder="self", role="holder", why="тест")
    assert back is not None and back["epoch"] == 2 and db.jrn_following() is False, back


def test_the_pass_commits_under_full_sync_and_puts_normal_back(db):
    """Переход коммитится с synchronous = FULL, а после — снова NORMAL: события
    и опрос трафика не должны платить fsync каждого коммита."""
    seen: list[str] = []
    conn = db._connection()
    conn.set_trace_callback(seen.append)
    try:
        follow(db)
    finally:
        conn.set_trace_callback(None)
    upper = [s.strip().upper() for s in seen]
    full = next((i for i, s in enumerate(upper) if s.startswith("PRAGMA SYNCHRONOUS = FULL")), None)
    commit = next((i for i, s in enumerate(upper) if s.startswith("COMMIT") and full is not None and i > full), None)
    assert full is not None and commit is not None, f"переход закоммичен не под FULL: {seen}"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1, "после перехода synchronous не вернулся к NORMAL"


def test_the_pass_survives_a_power_loss_copy_of_the_base_files(db, tmp_path):
    """«Пропажа питания»: копия файлов базы, снятая сразу после перехода (без
    закрытия соединения), уже хранит следование — на диске не осталось
    прежней роли."""
    follow(db)
    dst = tmp_path / "copy"
    dst.mkdir()
    for suffix in ("", "-wal", "-shm"):
        src = db.path + suffix
        if os.path.exists(src):
            shutil.copyfile(src, str(dst / ("bot.db" + suffix)))
    con = sqlite3.connect(str(dst / "bot.db"))
    try:
        role, epoch = con.execute("SELECT role, epoch FROM jrn_meta WHERE id = 1").fetchone()
    finally:
        con.close()
    assert (role, epoch) == ("follower", 1), "после пропажи питания база снова считает себя держателем"


# ── решения: отказ ──────────────────────────────────────────────────────────

_FUTURE = "2099-01-01T00:00:00+03:00"

DECISIONS = {
    "create_client": lambda w: w.db.create_client("Вера", 3, "2026-01-01T00:00:00+03:00", _FUTURE, "INV-DUMMY"),
    "period_end": lambda w: w.db.update_client_fields(w.cid, period_end=_FUTURE),
    "status": lambda w: w.db.update_client_fields(w.cid, status="expired"),
    "tg_identity": lambda w: w.db.set_tg_identity(w.cid, "Иван Петров", "ivan_p"),
    "client_mask": lambda w: w.db.client_mask_apply(w.cid, or_bits=1),
    "pause_balance": lambda w: w.db.set_pause_balance(w.cid, 9),
    "expiry_threshold_mark": lambda w: w.db.add_notified(w.cid, 60),
    "traffic_mark": lambda w: w.db.add_traffic_notified(w.cid, "dev80:1"),
    "delete_client": lambda w: w.db.delete_client(w.cid),
    "create_device": lambda w: w.db.create_device(w.cid, "Ноутбук", "DUMMY-pub", "DUMMY-psk", "10.8.1.77"),
    "device_name": lambda w: w.db.update_device_fields(w.did, name="Планшет"),
    "device_limit": lambda w: w.db.update_device_fields(w.did, traffic_limit=123456),
    "device_mask": lambda w: w.db.device_mask_apply(w.did, or_bits=1),
    "device_friend": lambda w: w.db.set_device_friend(w.did, friend_code="DUMMY-other", friend_status="pending"),
    "delete_device": lambda w: w.db.delete_device(w.did),
    "routing_domain": lambda w: w.db.add_routing_domain(w.cid, "example.net"),
    "migration_cohort": lambda w: w.db.cohort_clear(),
}


@pytest.mark.parametrize("what", sorted(DECISIONS))
def test_every_decision_write_of_the_follower_is_refused_by_the_base(world, what):
    before = world.db._connection().total_changes
    assert refused(lambda: DECISIONS[what](world)), f"{what}: следующий записал решение мимо держателя"
    hold(world.db)
    # тот же вызов у держателя проходит — значит, отказ был запретом эпохи, а не поломкой вызова
    assert not refused(lambda: DECISIONS[what](world)), f"{what}: держатель тоже не может записать"
    assert world.db._connection().total_changes > before


@pytest.mark.parametrize("key", classes.STATE_DECISION)
def test_the_decision_keys_of_the_server_state_are_refused_too(world, key):
    """Ключи белого списка реплики (почта выхода из паузы, решение о резолвере,
    «переподключи VPN», окно переезда) — решения: следующему отказ."""
    assert refused(lambda: world.db.set_state(key, "DUMMY-value")), f"ключ {key} записан следующим"
    assert world.db.get_state(key) != "DUMMY-value"


@pytest.mark.parametrize("table", sorted(t for t, spec in classes.TABLES.items()
                                         if t != "server_state"
                                         and any(classes.class_of(t, c) in classes.DENIED
                                                 for c in (["*"] if isinstance(spec, str) else list(spec)))))
def test_every_decision_column_of_the_live_schema_is_refused(world, table):
    """Сторож по схеме: в каждой таблице с решениями каждая колонка решения
    (кроме ключей и внешних ссылок) меняется у следующего только с отказом,
    а колонки-счётчики и местные — свободно. Новая колонка решения, которую
    генератор триггеров не охватил, роняет этот тест."""
    conn = world.db._connection()
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    fks = {r[3] for r in conn.execute(f'PRAGMA foreign_key_list("{table}")')}
    row = conn.execute(f'SELECT rowid, * FROM "{table}" LIMIT 1').fetchone()
    assert row is not None, f"в мире теста нет строки {table} — сторож её не проверит"
    rowid = row[0]
    checked = 0
    for c in cols:
        name, ctype, pk = c[1], (c[2] or "").upper(), c[5]
        if pk or name in fks:
            continue
        cur = conn.execute(f'SELECT "{name}" FROM "{table}" WHERE rowid = ?', (rowid,)).fetchone()[0]
        if "INT" in ctype:
            new = (int(cur) + 1) if cur is not None and str(cur).lstrip("-").isdigit() else 1
        else:
            new = (str(cur) + "x") if cur is not None else "x"

        def write(name=name, new=new):
            with world.db._tx() as c2:
                c2.execute(f'UPDATE "{table}" SET "{name}" = ? WHERE rowid = ?', (new, rowid))
        denied = classes.class_of(table, name) in classes.DENIED
        assert refused(write) is denied, \
            f"{table}.{name}: {'решение записано следующим' if denied else 'счётчик или местное получили отказ'}"
        checked += 1
    if all(classes.class_of(table, c[1]) in classes.DENIED for c in cols):
        # строка целиком — решение: и удалить её следующий не может
        def drop():
            with world.db._tx() as c2:
                c2.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))
        assert refused(drop), f"{table}: строку-решение удалил следующий"
        checked += 1
    assert checked, f"{table}: ни одной колонки не проверено"


def test_whole_decision_rows_cannot_be_inserted_or_deleted_by_the_follower(world):
    """Строку-решение нельзя ни завести, ни удалить в обход колонок — вставка и
    удаление тоже решения."""
    conn = world.db._connection()

    def ins():
        with world.db._tx() as c:
            c.execute("INSERT INTO client_routing_domains (client_id, domain, added_at) VALUES (?, ?, ?)",
                      (world.cid, "example.com", timeutil.now_iso()))

    def dele():
        with world.db._tx() as c:
            c.execute("DELETE FROM client_routing_domains WHERE client_id = ?", (world.cid,))
    assert refused(ins) and refused(dele)
    left = conn.execute("SELECT domain FROM client_routing_domains WHERE client_id = ?", (world.cid,)).fetchall()
    assert [r[0] for r in left] == ["example.org"], left


def test_writing_the_same_value_is_not_a_decision(world):
    """Запись того же значения (UPSERT формы, повтор правки) не отказ: иначе
    обычные сохранения экранов падали бы в следовании без всякой правки."""
    dev = world.db.get_device(world.did)
    assert not refused(lambda: world.db.update_device_fields(world.did, name=dev.name)), \
        "запись того же имени устройства получила отказ"


# ── счётчики и местное: пишутся ─────────────────────────────────────────────

def test_counters_and_local_state_are_written_by_the_follower(world):
    """Опрос трафика, рукопожатие, сэмплы, онлайн-счётчик, ключи резерва, меню
    чатов, выбор слота шлюзов и чистка истории у следующего пишутся."""
    db, did = world.db, world.did
    db.add_traffic_bulk([(did, 1500, 700)])
    assert _month(db, did) == (1500, 700), "дельта трафика следующего не записана"
    db.update_device_fields(did, last_handshake=1_700_000_000)
    assert int(db.get_device(did).last_handshake) == 1_700_000_000
    db.set_samples([(did, 10, 20)])
    db.set_state("online_count", "3")
    db.set_state("rsv_follow", '{"blocks": []}')
    db.set_state("routing_active_gateway", "2")
    db.nav_touch(4242, 17)
    assert db.get_state("online_count") == "3" and db.get_state("routing_active_gateway") == "2"
    assert db._connection().execute("SELECT nav_message_id FROM ui_state WHERE chat_id = 4242").fetchone()[0] == 17
    world.svc.purge_old_history()                            # чистка по сроку — своя у каждой стороны


# ── отказ: вид и откат ──────────────────────────────────────────────────────

def test_the_refusal_is_an_integrity_error_with_the_jrn_prefix(world):
    with pytest.raises(sqlite3.IntegrityError) as ei:
        world.db.update_client_fields(world.cid, period_end=_FUTURE)
    assert str(ei.value).startswith("jrn:"), f"отказ без префикса — обработчики примут его за конфликт: {ei.value}"
    assert rsvjrn.is_refusal(ei.value)
    assert not rsvjrn.is_refusal(sqlite3.IntegrityError("UNIQUE constraint failed: devices.address"))


def test_the_refusal_rolls_back_the_whole_transaction_with_its_counters(world):
    """Отказ внутри групповой транзакции откатывает её целиком: записанные до
    него дельта трафика и местный ключ не остаются полутранзакцией."""
    db, did = world.db, world.did
    before = _month(db, did)
    with pytest.raises(sqlite3.IntegrityError, match="^jrn:"):
        with db.transaction():
            db.add_traffic_bulk([(did, 900, 900)])
            db.set_state("online_count", "9")
            with db.transaction():                           # вложенная — тоже откатывается внешним уровнем
                db.update_client_fields(world.cid, period_end=_FUTURE)
    assert _month(db, did) == before, "дельта осталась без своей транзакции — следующий опрос посчитает её дважды"
    assert db.get_state("online_count") != "9"
    db.add_traffic_bulk([(did, 5, 5)])                       # соединение после отказа живо и не в транзакции
    assert _month(db, did) == (before[0] + 5, before[1] + 5)


def test_the_temporary_triggers_never_reach_the_base_file(world):
    """Триггеры — только в памяти соединения: в файле базы их нет (их не
    унесут ни реплика, ни бэкап), сырое соединение (слияние) их не видит и
    пишет решения."""
    conn = world.db._connection()
    assert conn.execute("SELECT count(*) FROM main.sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
    temp = [r[0] for r in conn.execute("SELECT name FROM sqlite_temp_master WHERE type = 'trigger'")]
    assert any(n.startswith("jrn_deny_") for n in temp), f"запрет на соединение не поставлен: {temp}"
    raw = sqlite3.connect(world.db.path)
    try:
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
        raw.execute("UPDATE client_subscription SET period_end = ? WHERE client_id = ?", (_FUTURE, world.cid))
        raw.commit()
    finally:
        raw.close()
    assert world.db.get_client(world.cid).period_end == _FUTURE, "сырое соединение слияния не записало решение"


def test_a_connection_of_another_thread_sees_the_refusal_after_the_pass(db, make_active_client, services):
    """Соединения у потоков свои (threading.local): поток, открывший соединение,
    пока основа держала эпоху, после перехода в следование тоже получает отказ —
    роль читается из базы, а не запоминается при открытии."""
    ivan = make_active_client(name="Иван", tg_id=7100)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(lambda: db.get_client(ivan.id).name).result() == "Иван"   # соединение потока открыто
        follow(db)
        verdict = pool.submit(lambda: refused(lambda: db.update_client_fields(ivan.id, period_end=_FUTURE))).result()
    finally:
        pool.shutdown()
    assert verdict is True, "поток с соединением до перехода пишет решения следующему"


def test_a_new_thread_connection_after_the_pass_is_refused_as_well(world):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        verdict = pool.submit(lambda: refused(lambda: world.db.update_client_fields(world.cid, period_end=_FUTURE))).result()
    finally:
        pool.shutdown()
    assert verdict is True, "соединение нового потока (asyncio.to_thread) открыто без запрета"


# ── applying ────────────────────────────────────────────────────────────────

def test_a_stale_applying_mark_is_cleared_on_start(world):
    """`applying = 1`, ушедшее в коммит (процесс упал посреди применения), глушит
    запрет; старт процесса (init_schema) сбрасывает отметку — запрет снова в
    силе."""
    raw = sqlite3.connect(world.db.path)
    try:
        raw.execute("UPDATE jrn_meta SET applying = 1 WHERE id = 1")
        raw.commit()
    finally:
        raw.close()
    assert not refused(lambda: world.db.update_client_fields(world.cid, period_end=_FUTURE)), \
        "под отметкой применителя запись должна проходить (иначе тест не про то)"
    world.db.init_schema()
    assert world.db._connection().execute("SELECT applying FROM jrn_meta WHERE id = 1").fetchone()[0] == 0
    later = timeutil.to_iso(timeutil.now() + datetime.timedelta(days=400))
    assert refused(lambda: world.db.update_client_fields(world.cid, period_end=later)), \
        "после старта отметка applying по-прежнему глушит запрет"


def test_a_restart_keeps_the_follower_role(world):
    """Перезапуск бота основы в отъезде (новое соединение, init_schema) не
    делает её держателем: роль живёт в базе."""
    path = world.db.path
    world.db.close()
    again = Database(path)
    try:
        again.init_schema()
        assert again.jrn_following() is True, "после перезапуска основа снова пишет решения"
        assert refused(lambda: again.update_client_fields(world.cid, period_end=_FUTURE))
    finally:
        again.close()


# ── реестр классов ──────────────────────────────────────────────────────────

def test_every_table_and_column_of_the_live_schema_has_a_class(db):
    """Сторож реестра: новая таблица или колонка без класса — тест красный, пока
    ей не назначат класс (иначе она молча станет «никакой» — ни запрета, ни
    решения)."""
    schema = triggers.schema_of(db._connection())
    assert classes.unclassified(schema) == [], classes.unclassified(schema)
    assert set(t for t in schema if not t.startswith("sqlite_")) <= set(classes.TABLES), \
        set(schema) - set(classes.TABLES)


def test_the_registry_sentinel_catches_a_table_without_a_class():
    assert classes.unclassified({"clients": ["id"], "brand_new": ["id"]}) == ["brand_new"]
    assert classes.unclassified({"sqlite_sequence": ["name"]}) == []


def test_the_routing_keys_are_not_decisions_of_the_follower():
    """Выбор слота шлюзов меняет живость шлюзов самого хоста, итог проверки ящика пишут
    запасной канал алертов и предстартовая проверка — у следующего они свои;
    остальные ключи белого списка реплики — решения."""
    from awgbot.util.rsvpkg.bot import GUEST_STATE
    assert set(classes.STATE_DECISION) == set(GUEST_STATE) - {"routing_active_gateway", "routing_manual_hold",
                                                               "email_check"}


def test_a_schema_older_than_the_epoch_gets_no_triggers():
    """База без jrn_meta (старше эпохи) — запрещать не по чему: триггеров нет,
    соединение не падает."""
    assert triggers.statements({"clients": ["id", "name"]}) == []
