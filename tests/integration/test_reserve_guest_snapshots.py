"""Шифрованный снимок бота основы на резерве (`rep.bin`) и состояние основы
глазами резерва (guest.py сервисов).

Цена ошибки: `rep.bin` открытым текстом — база клиентов основы лежит на диске
резерва, и `--wipe` с памятью не спасёт; снимок не по каждой записи — после
перезагрузки резерва во время обслуживания теряются профили, заведённые за
последние минуты; снимок под не тем ключом — после перезагрузки зона не
поднимется, клиенты основы остаются без VPN; снимок на каждую запись —
диск резерва в постоянной записи.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import types

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import guest as guest_mod
from awgbot.infra import rsvzone
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.integration

K = bytes.fromhex("11" * 32)


class _Clock:
    def __init__(self):
        self.t = 5000.0

    def monotonic(self) -> float:
        return self.t


@pytest.fixture()
def guest(services, db, tmp_path, monkeypatch):
    """Бот основы на резерве: хранилище гостя — во временном каталоге, ключ пакета
    положен, часы подставные, крючок снимков стоит."""
    run, var = tmp_path / "run", tmp_path / "var"
    (run / "data").mkdir(parents=True)
    var.mkdir()
    monkeypatch.setattr(rsvzone, "RUN_DIR", run)
    monkeypatch.setattr(rsvzone, "VAR_DIR", var)
    monkeypatch.setattr(rsvzone, "K_FILE", run / "k")
    monkeypatch.setattr(rsvzone, "REP_PATH", var / "rep.bin")
    monkeypatch.setattr(config, "DATA_DIR", run / "data")
    monkeypatch.setattr(config, "DB_PATH", db.path)
    rsvzone.write_private(run / "k", K.hex())
    rsvzone.write_private(run / "pkg.json", '{"pkg_gen": 7}')
    clock = _Clock()
    monkeypatch.setattr(guest_mod, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    services.guest_snapshot_hook_install()
    services._clock = clock
    return services


def _make(svc, make_active_client, name="Аня", tg=9001):
    return make_active_client(name=name, tg_id=tg)


def test_the_snapshot_waits_for_the_batch_window_and_then_is_written_once(guest, make_active_client):
    clock = guest._clock
    _make(guest, make_active_client)
    clock.t += 29
    assert guest.guest_snapshot_tick() is False and not rsvzone.REP_PATH.exists()
    clock.t += 2
    assert guest.guest_snapshot_tick() is True and rsvzone.REP_PATH.exists()
    stamp = rsvzone.REP_PATH.stat().st_mtime_ns
    clock.t += 120
    assert guest.guest_snapshot_tick() is False, "записей не было — снимок не переписывается"
    assert rsvzone.REP_PATH.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("what", ["counters", "data"])
def test_every_write_after_a_snapshot_makes_the_next_snapshot_due(guest, make_active_client, what):
    """В гостевом режиме крючок ставит отметку на любую запись: счётчики гостя
    ценны (на резерве их больше нигде нет). Второй и все следующие снимки
    должны появляться так же, как первый."""
    client = _make(guest, make_active_client)
    dev = guest.add_device(client.id, "Телефон")
    guest._clock.t += 31
    assert guest.guest_snapshot_tick() is True
    for i in range(3):
        if what == "counters":
            guest.db.add_traffic_bulk([(dev.device_id, 100, 200)])
        else:
            guest.db.update_client_fields(client.id, name=f"Имя {i}")
        guest._clock.t += 31
        assert guest.guest_snapshot_tick() is True, f"запись № {i + 2} ({what}) не попала в снимок"


def test_the_first_snapshot_is_due_half_a_minute_after_the_start_even_without_writes(guest):
    guest._clock.t += 29
    assert guest.guest_snapshot_due() is False
    guest._clock.t += 2
    assert guest.guest_snapshot_due() is True


def test_the_snapshot_is_ciphertext_private_and_opens_only_with_the_package_key(guest, make_active_client):
    _make(guest, make_active_client, name="СекретноеИмяПрофиля")
    assert guest.guest_snapshot_tick(force=True) is True
    blob = rsvzone.REP_PATH.read_bytes()
    assert stat.S_IMODE(os.stat(rsvzone.REP_PATH).st_mode) == 0o600
    assert b"SQLite format" not in blob and "СекретноеИмяПрофиля".encode() not in blob, "снимок читается без ключа"
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(bytes.fromhex("22" * 32), blob)
    snap = seal.snapshot_open(K, blob)
    assert snap["pkg_gen"] == 7, "поколение пакета, с которого открыто, — для сверки «снимок новее пакета»"
    out = rsvzone.REP_PATH.with_name("check.db")
    out.write_bytes(bytes.fromhex(snap["db"]))
    con = sqlite3.connect(out)
    try:
        assert ("СекретноеИмяПрофиля",) in con.execute("SELECT name FROM clients").fetchall()
    finally:
        con.close()


def test_the_snapshot_keeps_the_history_of_the_guest_period(guest, make_active_client):
    """В отличие от пакета основы, снимок гостя несёт всю базу: история, начатая на
    резерве, при возврате дописывается основе."""
    client = _make(guest, make_active_client)
    guest.db.delete_client(client.id)
    guest.guest_snapshot_tick(force=True)
    snap = seal.snapshot_open(K, rsvzone.REP_PATH.read_bytes())
    path = rsvzone.REP_PATH.with_name("h.db")
    path.write_bytes(bytes.fromhex(snap["db"]))
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM clients_histories").fetchone()[0] >= 1
    finally:
        con.close()


def test_no_temporary_copy_of_the_base_stays_in_the_storage(guest):
    guest.guest_snapshot_tick(force=True)
    assert [n for n in os.listdir(rsvzone.RUN_DIR / "data") if n.startswith("rsv-snap")] == []


def test_without_the_key_file_no_snapshot_is_written_and_nothing_crashes(guest):
    rsvzone.K_FILE.unlink()
    assert guest.guest_snapshot_tick(force=True) is False
    assert not rsvzone.REP_PATH.exists()


def test_a_failed_write_keeps_the_stale_mark_so_the_next_tick_retries(guest, monkeypatch):
    guest.guest_snapshot_mark()
    guest._clock.t += 31
    boom = {"on": True}
    real = rsvzone.write_private

    def flaky(path, data, mode=0o600):
        if boom["on"]:
            raise OSError("диск в памяти полон")
        real(path, data, mode)
    monkeypatch.setattr(rsvzone, "write_private", flaky)
    assert guest.guest_snapshot_tick() is False
    boom["on"] = False
    assert guest.guest_snapshot_tick() is True, "отметка снята после неудачной записи — снимок потерян"


# ── состояние основы глазами резерва ─────────────────────────────────────────

def test_the_state_from_the_reserve_bot_replaces_the_view_and_fills_the_own_name(services, monkeypatch):
    monkeypatch.setattr(config, "SERVER_NAME", "Альфа")
    assert services.guest_state() == {"own_name": "Альфа"}
    services.guest_state_in({"name": "Бета", "since": "2026-10-21T03:12:00+05:00", "reason": "dead",
                             "primary": {"status": "dead"}, "return": "button", "junk": "x"})
    d = services.guest_state()
    assert d["name"] == "Бета" and d["primary"] == {"status": "dead"} and "junk" not in d, "лишние поля канала не принимаются"
    assert d["own_name"] == "Альфа"
    services.guest_state_in({"name": "Бета", "own_name": "Альфа-2"})
    assert services.guest_state()["own_name"] == "Альфа-2"


def test_the_state_view_of_the_reserve_reports_the_primary_by_the_pair_liveness(services, db):
    db.reserve_peer_set(name="Альфа", host="198.51.100.7", pair_if="awgpair", pair_port=51830, peer_port=51831,
                        pair_cidr="10.99.98.0/30", listener=False)
    services.rsv_put("rsv_in", {"state": "serving", "since": "2026-10-21T03:12:00+05:00", "why": "dead"})
    v = services.reserve_guest_state_view()
    assert v["primary"]["status"] == "alive" and v["since"] == "2026-10-21T03:12:00+05:00" and v["reason"] == "dead"
    assert v["own_name"] == "Альфа" and v["return"] == "immediate"
    services._lv.state = "suspect"
    assert services.reserve_guest_state_view()["primary"]["status"] == "suspect"
    services._lv.state = "down"
    services.rsv_update("rsv_peer_state", down_since="2026-10-21T03:10:00+05:00")
    v = services.reserve_guest_state_view()
    assert v["primary"] == {"status": "dead", "since": "2026-10-21T03:10:00+05:00"}


def test_the_screen_data_counts_devices_online_and_survives_a_failing_count(services, monkeypatch):
    monkeypatch.setattr(services, "online_devices", lambda: [1, 2, 3], raising=False)
    assert services.guest_screen_data()["online"] == 3
    monkeypatch.setattr(services, "online_devices", lambda: (_ for _ in ()).throw(OSError("нет")), raising=False)
    assert services.guest_screen_data()["online"] == 0


def test_the_return_lever_is_offered_only_by_the_state_the_reserve_bot_sent(services):
    assert services.guest_can_return() is False, "состояния от бота резерва нет — рычага нет"
