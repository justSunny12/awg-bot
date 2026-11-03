"""
«Прочитал — решил — записал» под замком записи с первой строки
(transaction(immediate=True)) и биты блокировки — в SQL.

Стережём:
- транзакция immediate берёт замок записи на BEGIN, до первой записи; пустая
  (замок взят, писать не пришлось) — не коммит: ни счётчика, ни крючка;
- такт сроков не блокирует профиль, продлённый между чтением списка и записью
  «истекла» (раньше: продление админа терялось, профиль блокировался с
  уведомлением «истекла»);
- вход в паузу и выход из неё меняют бит PAUSED побитово: бит, поставленный
  другим потоком после чтения профиля (лимит, ручной блок), остаётся;
- перепривязка берёт лимит получателя из базы под замком, а не из
  прочитанного до неё профиля.
"""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from awgbot.core.blocks import ClientBlock
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


# ── ядро ─────────────────────────────────────────────────────────────────────

@pytest.mark.jrn_raw("второе соединение только пробует замок")
def test_immediate_takes_the_write_lock_before_any_write(db):
    other = sqlite3.connect(db.path, timeout=0)
    try:
        with db.transaction(immediate=True):
            db.get_state("x")                               # только чтение
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO server_state (key, value) VALUES ('o', '1')")
        other.execute("INSERT INTO server_state (key, value) VALUES ('o', '1')")   # отпущен
        other.commit()
    finally:
        other.close()


@pytest.mark.jrn_raw("второе соединение держит замок записи")
def test_a_refused_immediate_begin_leaves_the_thread_able_to_commit(db):
    """BEGIN IMMEDIATE отозвался «database is locked» (чужой замок дольше busy_timeout):
    глубина потока — назад. Раньше поток навсегда считал себя внутри транзакции —
    его записи не коммитили никогда, а первая держала замок записи до перезапуска
    бота (потоки to_thread переиспользуются)."""
    other = sqlite3.connect(db.path, timeout=0)
    db._connection().execute("PRAGMA busy_timeout = 50")
    try:
        other.execute("BEGIN IMMEDIATE")                        # чужой замок записи
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with db.transaction(immediate=True):
                pass
        other.rollback()
        assert getattr(db._txl, "depth", 0) == 0 and getattr(db._txl, "conn", None) is None, \
            "после отказа замка поток считает себя внутри транзакции"
        with db.transaction(immediate=True):                    # тот же поток — обычная транзакция
            db.set_state("after", "1")
        third = sqlite3.connect(db.path, timeout=0)
        try:
            row = third.execute("SELECT value FROM server_state WHERE key = 'after'").fetchone()
            assert row and row[0] == "1", "запись после отказа не закоммичена"
            third.execute("INSERT INTO server_state (key, value) VALUES ('o3', '1')")   # замок отпущен
            third.commit()
        finally:
            third.close()
    finally:
        other.close()


def test_an_empty_immediate_transaction_is_not_a_commit(db):
    seen = []
    db.set_commit_hook(seen.append)
    before = db.commits
    with db.transaction(immediate=True):
        db.get_state("x")
    assert db.commits == before and seen == []
    with db.transaction(immediate=True):
        db.set_state("x", "1")
    assert db.commits == before + 1 and seen


def test_deferred_stays_the_default(db):
    """Опросчик держит длинную транзакцию: по умолчанию замок — с первой записи,
    и чужая запись (timeout=0: замок отозвался бы «database is locked») проходит
    посреди открытой транзакции и видна после неё."""
    other = sqlite3.connect(db.path, timeout=0)
    try:
        with db.transaction():
            db.get_state("x")
            other.execute("INSERT INTO server_state (key, value) VALUES ('o2', '1')")
            other.commit()
    finally:
        other.close()
    assert db.get_state("o2") == "1", "чужая запись посреди отложенной транзакции не прошла"


# ── сроки против продления ───────────────────────────────────────────────────

def test_expiry_tick_does_not_block_a_profile_extended_after_its_read(services, make_active_client, monkeypatch):
    c = make_active_client(name="a", tg_id=7001, period_kind="month")
    past = timeutil.now() - datetime.timedelta(minutes=1)
    services.db.update_client_fields(c.id, period_end=timeutil.to_iso(past))
    stale = services.db.list_clients(include_service=False, active_finite_only=True)   # такт прочёл список
    services.extend_period(c.id, "month", keep_remainder=False)                          # админ продлил
    real = services.db.list_clients
    calls = {"n": 0}

    def once(*a, **k):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(*a, **k)

    monkeypatch.setattr(services.db, "list_clients", once)
    notes = services.check_expiry()
    x = services.db.get_client(c.id)
    assert x.status != "expired" and int(x.block_reason) == 0
    assert not [n for n in notes if "истекла" in n.text.lower()]


def test_expiry_tick_still_expires_a_really_past_profile(services, make_active_client):
    c = make_active_client(name="b", tg_id=7002, period_kind="month")
    services.db.update_client_fields(c.id, period_end=timeutil.to_iso(timeutil.now() - datetime.timedelta(minutes=1)))
    services.check_expiry()
    x = services.db.get_client(c.id)
    assert x.status == "expired" and int(x.block_reason) & int(ClientBlock.EXPIRY)


# ── паузы: бит PAUSED побитово ───────────────────────────────────────────────

def _stale_reads(monkeypatch, services, client_id):
    """get_client отдаёт профиль, прочитанный ДО чужой записи бита."""
    stale = services.db.get_client(client_id)
    real = services.db.get_client
    monkeypatch.setattr(services.db, "get_client",
                        lambda cid, *a, **k: stale if cid == client_id else real(cid, *a, **k))
    return real


def test_enter_and_exit_pause_keep_a_bit_set_after_the_read(services, make_active_client, monkeypatch, fake_awg):
    c = make_active_client(name="p", tg_id=7003, period_kind="year")
    services.db.set_pause_balance(c.id, 10)
    real = _stale_reads(monkeypatch, services, c.id)
    services.db.client_mask_apply(c.id, or_bits=int(ClientBlock.TRAFFIC_CLIENT))   # другой поток
    ok, reserved, _n, _code = services.enter_pause(c.id, 3)
    assert ok and reserved == 3
    mask = int(real(c.id).block_reason)
    assert mask & int(ClientBlock.PAUSED) and mask & int(ClientBlock.TRAFFIC_CLIENT)
    monkeypatch.undo()

    real = _stale_reads(monkeypatch, services, c.id)
    services.db.client_mask_apply(c.id, or_bits=int(ClientBlock.ADMIN_SILENT))
    ok, *_ = services.exit_pause(c.id, auto=False)
    assert ok
    mask = int(real(c.id).block_reason)
    assert not mask & int(ClientBlock.PAUSED)
    assert mask & int(ClientBlock.TRAFFIC_CLIENT) and mask & int(ClientBlock.ADMIN_SILENT)


# ── перепривязка: лимит получателя под замком ───────────────────────────────

def test_reassign_reads_the_recipients_limit_under_the_lock(services, make_active_client, fake_awg):
    from awgbot.domain.services.types import LimitReached
    donor = make_active_client(name="d", tg_id=7004, device_limit=3)
    rcpt = make_active_client(name="r", tg_id=7005, device_limit=3)
    dev = services.db.get_device(services.add_device(donor.id, "Телефон").device_id)
    stale_rcpt = services.db.get_client(rcpt.id)                  # кнопка показана при лимите 3
    services.db.update_client_fields(rcpt.id, device_limit=1)
    services.add_device(rcpt.id, "Ноутбук")                       # слот занят
    with pytest.raises(LimitReached):
        services._reassign_tx(dev, stale_rcpt, rcpt.id, add_slot=False)
