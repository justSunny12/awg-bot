"""Пакет переезда на стороне основы: отметки от записи базы, такт сборки,
таймер с джиттером, подтверждение и снимки `base-<gen>`; приём на резерве.

Цена ошибки: запись счётчиков, ставящая отметку, — пакет каждые 5 минут
(маячок между серверами и износ диска резерва); пропущенная запись данных
пользователей — резерв в аварию поднимет пользователей по устаревшим данным (нового
профиля нет, блокированный снова пускает); пакет, ушедший раньше 10 с или
без живой пары, — гонка с пачкой правок и сборка на оборванном канале;
отметка, снятая без `pkg_ok`, — правка потеряна, пока не сработает таймер;
принятый пакет с чужим отпечатком — резерв отвечает `pkg_ok` на мусор и
основа считает данные доставленными.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import stat
import types

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import package
from awgbot.infra import awg
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.integration


def _k(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


LIVE = f"""[Interface]
PrivateKey = {_k(1)}
Address = 10.8.1.1/24
ListenPort = 51820
Jc = 4
Jmin = 10
Jmax = 50
S1 = 20
S2 = 30
H1 = 100-200
H2 = 500000100-500000900
H3 = 1000000100-1000090000
H4 = 1500000100-1999999999

[Peer]
PublicKey = {_k(2)}
AllowedIPs = 10.8.1.2/32
"""


class _Clock:
    """Часы пакета: время идёт только когда тест говорит."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(package, "time", types.SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture()
def paired(services, db, tmp_path, monkeypatch, clock):
    """Основа в паре с живым линком, ключи выданы, конфиг интерфейса — подставной,
    каталог данных — временный. Крючок коммита установлен, как при старте бота."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(awg, "read_file", lambda path: LIVE, raising=False)
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    db.reserve_peer_set(name="Бета", host="198.51.100.7", pair_if="awgpair", pair_port=51830, peer_port=51831,
                        pair_cidr="10.99.98.0/30", listener=True)
    services.reserve_keys_init()
    services.rsv_put("rsv_peer_state", {"session": True})
    services.reserve_pkg_hook_install()
    return services


def _tick(svc, clock, seconds=11):
    clock.advance(seconds)
    return svc.reserve_pkg_tick()


def _ack(svc, built):
    svc.reserve_pkg_ok(built["gen"], built["fp"])


def _quiet_write(db, sql: str) -> None:
    """Запись мимо базы бота — крючок не видит: изменение появится в пакете только по таймеру."""
    import sqlite3
    with sqlite3.connect(db.path) as con:
        con.execute(sql)


def _base_gens(tmp_path) -> set[int]:
    out = tmp_path / "reserve" / "out"
    return {int(n[5:-3]) for n in os.listdir(out)} if out.exists() else set()


# ── отметки от записи базы ───────────────────────────────────────────────────

@pytest.fixture()
def ctx(services, make_active_client):
    """Пользователь с устройством и пустой журнал видов транзакций (хук писем в список)."""
    client = make_active_client(name="Профиль", tg_id=7001)
    dev = services.add_device(client.id, "Телефон")
    kinds: list[str] = []
    services.db.set_commit_hook(kinds.append)
    return types.SimpleNamespace(db=services.db, cid=client.id, did=dev.device_id, kinds=kinds)


DATA_WRITES = {
    "create_client": lambda c: c.db.create_client("Новый", 3, "2026-10-01", "2027-10-01", "INVITE1"),
    "update_client_fields": lambda c: c.db.update_client_fields(c.cid, name="Другое имя"),
    "client_mask_apply": lambda c: c.db.client_mask_apply(c.cid, or_bits=4),
    "set_pause_balance": lambda c: c.db.set_pause_balance(c.cid, 5),
    "add_notified": lambda c: c.db.add_notified(c.cid, 7),
    "create_device": lambda c: c.db.create_device(c.cid, "Ноутбук", "DUMMY-PUB", "DUMMY-PSK", "10.8.1.77"),
    "update_device_fields": lambda c: c.db.update_device_fields(c.did, name="Планшет"),
    "device_mask_apply": lambda c: c.db.device_mask_apply(c.did, or_bits=4),
    "delete_device": lambda c: c.db.delete_device(c.did),
    "delete_client": lambda c: c.db.delete_client(c.cid),
    "add_routing_domain": lambda c: c.db.add_routing_domain(c.cid, "example.org"),
    "set_nav_message_id": lambda c: c.db.set_nav_message_id(7001, 4242),
    "set_state_whitelisted": lambda c: c.db.set_state("email_login", "box@example.org"),
}
COUNTER_WRITES = {
    "set_samples": lambda c: c.db.set_samples([(c.did, 10, 20)]),
    "add_traffic_bulk": lambda c: c.db.add_traffic_bulk([(c.did, 10, 20)]),
    "rf_set_samples": lambda c: c.db.rf_set_samples([(c.did, 10, 20)]),
    "rf_add_bulk": lambda c: c.db.rf_add_bulk([(c.did, 10, 20)]),
    "update_device_fields_last_handshake": lambda c: c.db.update_device_fields(c.did, last_handshake="2026-10-21T10:00:00+05:00"),
    "set_state_online_polled_at": lambda c: c.db.set_state("online_polled_at", "1792000000"),
    "set_state_rsv_key": lambda c: c.db.set_state("rsv_pkg_sent", '{"gen": 1}'),
}


@pytest.mark.parametrize("name", sorted(DATA_WRITES))
def test_any_write_of_client_data_is_reported_as_data(ctx, name):
    DATA_WRITES[name](ctx)
    assert ctx.kinds and set(ctx.kinds) == {"data"}, f"{name}: виды коммитов {ctx.kinds}"


@pytest.mark.parametrize("name", sorted(COUNTER_WRITES))
def test_counter_writes_are_reported_as_counters_and_never_as_data(ctx, name):
    """Опрос пишет счётчики каждые 5 минут (трафик, РФ-трафик, рукопожатия,
    метки опроса); если хоть один из них считается данными, пакет уходит
    каждые 5 минут."""
    COUNTER_WRITES[name](ctx)
    assert ctx.kinds and set(ctx.kinds) == {"counters"}, f"{name}: виды коммитов {ctx.kinds}"


def test_a_data_write_inside_a_counter_transaction_still_counts_as_data(ctx):
    """Старшинство у «данных» внутри вложенных транзакций: опрос, который заодно
    заблокировал устройство, обязан попасть в пакет."""
    with ctx.db.transaction():
        ctx.db.set_samples([(ctx.did, 1, 2)])
        ctx.db.device_mask_apply(ctx.did, or_bits=4)
    assert ctx.kinds == ["data"]


def test_a_rolled_back_write_calls_no_hook(ctx):
    with pytest.raises(RuntimeError):
        with ctx.db.transaction():
            ctx.db.client_mask_apply(ctx.cid, or_bits=4)
            raise RuntimeError("откат")
    assert ctx.kinds == [], "откатанная запись не должна ставить отметку"


def test_a_failing_hook_does_not_break_the_write(services, make_active_client):
    client = make_active_client(name="П", tg_id=7002)

    def boom(_kind):
        raise RuntimeError("крючок упал")
    services.db.set_commit_hook(boom)
    services.db.update_client_fields(client.id, name="Новое")
    assert services.db.get_client(client.id).name == "Новое"


def test_a_poll_cycle_leaves_no_mark_but_a_new_profile_does(paired, make_active_client, clock, monkeypatch):
    """Сквозь настоящий опрос трафика: dump с ростом трафика и новым рукопожатием."""
    import time as real_time
    client = make_active_client(name="Профиль", tg_id=7003)
    dev = paired.add_device(client.id, "Телефон")
    pub = paired.db.get_device(dev.device_id).public_key
    clock.advance(30)
    built = paired.reserve_pkg_tick()
    assert built is not None, "новый профиль и устройство — повод для пакета"
    _ack(paired, built)
    assert paired.__dict__.get("_rsv_pkg_due_at") is None
    peers = [{"public_key": pub, "rx": 5000, "tx": 9000, "last_handshake": int(real_time.time()) - 60}]
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: peers, raising=False)
    paired.poll_traffic()
    peers[0].update(rx=8000, tx=12000, last_handshake=int(real_time.time()))
    paired.poll_traffic()
    dev_row = paired.db.get_device(dev.device_id)
    assert dev_row.traffic_rx_month == 3000 and dev_row.last_handshake, "опрос не отработал — тест ничего не проверяет"
    assert paired.__dict__.get("_rsv_pkg_due_at") is None, "опрос счётчиков поставил отметку пакета"
    assert _tick(paired, clock) is None


# ── такт сборки: 10 секунд и живая пара ──────────────────────────────────────

def test_the_package_waits_ten_seconds_after_the_last_write(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    clock.advance(9)
    assert paired.reserve_pkg_tick() is None, "пачка правок ещё идёт — пакет рано"
    clock.advance(1.5)
    built = paired.reserve_pkg_tick()
    assert built is not None and built["gen"] == 1


def test_every_new_write_pushes_the_batch_ten_seconds_further(paired, make_active_client, clock):
    make_active_client(name="П1", tg_id=7001)
    clock.advance(8)
    make_active_client(name="П2", tg_id=7002)
    clock.advance(8)
    assert paired.reserve_pkg_tick() is None, "вторая правка 8 с назад — пачка не закончилась"
    assert paired.reserve_pkg_tick() is None
    clock.advance(3)
    assert paired.reserve_pkg_tick() is not None


@pytest.mark.parametrize("state, why", [
    ({"session": False}, "сессия канала не поднята"),
    ({}, "резерв ни разу не отвечал"),
])
def test_no_package_leaves_without_a_live_pair_session(paired, make_active_client, clock, state, why, tmp_path):
    make_active_client(name="Профиль", tg_id=7001)
    paired.rsv_put("rsv_peer_state", state or None)
    assert _tick(paired, clock) is None, why
    assert _base_gens(tmp_path) == set(), "без пары снимок base-<gen> не создаётся"
    paired.rsv_put("rsv_peer_state", {"session": True})
    assert paired.reserve_pkg_tick() is not None, "пара ожила — отметка не потеряна"


def test_no_package_when_unpaired_or_while_the_clients_are_away(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    paired.reserve_set_out("away")
    assert _tick(paired, clock) is None, "пользователи уже у резерва — данные ведёт бот основы на резерве"
    paired.reserve_set_out("home")
    paired.db.reserve_peer_delete()
    assert paired.reserve_pkg_tick() is None


def test_without_keys_no_package_is_built(paired, make_active_client, clock, tmp_path):
    make_active_client(name="Профиль", tg_id=7001)
    paired.rsv_put("rsv_keys", None)
    assert _tick(paired, clock) is None
    assert not (tmp_path / "reserve").exists()


def test_the_second_tick_does_not_build_while_the_first_package_is_in_flight(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    first = _tick(paired, clock)
    assert first is not None
    assert _tick(paired, clock) is None, "пакет ушёл и ждёт ответа — второй не собирается"


def test_a_package_without_an_answer_is_given_up_after_two_minutes_and_built_again(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    first = _tick(paired, clock)
    clock.advance(60)
    assert paired.reserve_pkg_sending_stale() is False
    clock.advance(70)
    assert paired.reserve_pkg_sending_stale() is True
    again = paired.reserve_pkg_tick()
    assert again is not None and again["gen"] == first["gen"] + 1, "отметка держалась, пока не было pkg_ok"


# ── таймер с джиттером ───────────────────────────────────────────────────────

def test_the_timer_delay_stays_within_forty_percent_of_the_setting():
    base = package.settings.get_int("app.reserve.pkg_minutes", 30) * 60
    delays = [package.ReservePackageMixin._pkg_timer_delay() for _ in range(400)]
    assert min(delays) >= 0.6 * base - 1e-6 and max(delays) <= 1.4 * base + 1e-6, \
        f"джиттер вышел за ±40 %: {min(delays) / base:.2f}…{max(delays) / base:.2f} от интервала"
    assert max(delays) - min(delays) > 0.2 * base, "джиттера нет: строго периодический запрос выглядит маячком"


def test_the_timer_does_not_send_when_the_content_has_not_changed(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    _ack(paired, built)
    base = package.settings.get_int("app.reserve.pkg_minutes", 30) * 60
    paired.reserve_pkg_tick()                          # первый такт заводит таймер
    clock.advance(1.5 * base)
    assert paired.reserve_pkg_tick() is None, "по таймеру без изменений пакет не уходит"
    clock.advance(1.5 * base)
    assert paired.reserve_pkg_tick() is None


def test_the_timer_sends_when_the_content_has_changed_without_an_event(paired, make_active_client, clock):
    """Расход трафика и сроки «в пути» едут таймерным пакетом, хотя событием не были."""
    client = make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    _ack(paired, built)
    base = package.settings.get_int("app.reserve.pkg_minutes", 30) * 60
    paired.reserve_pkg_tick()
    _quiet_write(paired.db, f"UPDATE device_traffic SET traffic_rx_month = 12345 WHERE device_id IN "
                            f"(SELECT id FROM devices WHERE client_id = {client.id})")
    _quiet_write(paired.db, f"UPDATE clients SET name = 'Тихо' WHERE id = {client.id}")
    clock.advance(1.5 * base)
    again = paired.reserve_pkg_tick()
    assert again is not None and again["gen"] == built["gen"] + 1


# ── подтверждение ────────────────────────────────────────────────────────────

def test_the_mark_stays_until_the_peer_confirms_and_goes_after_pkg_ok(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "до pkg_ok отметка снята"
    _ack(paired, built)
    assert paired.__dict__.get("_rsv_pkg_due_at") is None
    assert paired.rsv_get("rsv_pkg_sent")["gen"] == built["gen"]
    assert _tick(paired, clock) is None, "подтверждённое второй раз не уходит"


def test_pkg_bad_keeps_the_mark_and_the_package_is_built_again(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    paired.reserve_pkg_bad(built["gen"], "отпечаток не сошёлся")
    assert paired.rsv_get("rsv_pkg_sent").get("gen") in (None, 0), "отвергнутый пакет не стал отправленным"
    again = _tick(paired, clock)
    assert again is not None and again["gen"] == built["gen"] + 1


@pytest.mark.parametrize("gen_delta, fp", [(0, "00" * 32), (1, None)])
def test_a_confirmation_for_another_package_is_ignored(paired, make_active_client, clock, gen_delta, fp):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    paired.reserve_pkg_ok(built["gen"] + gen_delta, fp or built["fp"])
    assert not paired.rsv_get("rsv_pkg_sent").get("gen"), "подтверждение чужого поколения принято"
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "отметка снята чужим подтверждением"


def test_a_write_made_after_the_build_is_not_swallowed_by_the_confirmation(paired, make_active_client, clock):
    """Пакет собран в момент T, правка сделана в T+11 с (в пакет она не вошла),
    pkg_ok пришёл в T+22. Отметка этой правки обязана остаться: иначе она
    ждёт таймера — до 42 минут вместо 30 секунд."""
    make_active_client(name="П1", tg_id=7001)
    built = _tick(paired, clock)
    clock.advance(11)
    make_active_client(name="П2", tg_id=7002)
    clock.advance(11)
    _ack(paired, built)
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "правка после сборки потеряла отметку"
    again = _tick(paired, clock)
    assert again is not None, "правка после сборки не ушла следующим пакетом"


def test_a_write_during_the_wait_for_the_answer_survives_the_confirmation(paired, make_active_client, clock):
    make_active_client(name="П1", tg_id=7001)
    built = _tick(paired, clock)
    clock.advance(2)
    make_active_client(name="П2", tg_id=7002)
    clock.advance(2)
    _ack(paired, built)
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None
    assert _tick(paired, clock) is not None


# ── содержимое и снимки ──────────────────────────────────────────────────────

def test_the_package_opens_with_k_from_m_and_carries_no_host_secrets(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    paired.db.set_state("backup_passphrase", "DUMMY-phrase")
    (config.DATA_DIR / "routing-tun_domains.lst").write_text("gosuslugi.ru\n", encoding="utf-8")
    built = _tick(paired, clock)
    blob = bytes.fromhex(built["blob"])
    keys = paired.reserve_keys()
    head = seal.header(blob)
    assert (head.pkg_gen, head.m_gen, head.digest) == (built["gen"], int(keys["m_gen"]), built["fp"])
    assert seal.open_k(blob, bytes.fromhex(keys["m"])) == bytes.fromhex(keys["k"])
    parts = seal.open_parts(blob, bytes.fromhex(keys["k"]))
    assert set(parts) == {"vpn", "bot"}
    assert set(parts["bot"]) == {"db", "conf", "app_guest", "env", "lists"}
    assert parts["bot"]["lists"].get("tun_domains") == ["gosuslugi.ru"], \
        "кэш списков РФ-доступа не уехал гостю — маршрутизация в зоне без базового списка"
    assert parts["vpn"]["ifaces"][0]["addr"] == "10.8.1.1/24"
    assert "DUMMY-phrase" not in bytes.fromhex(parts["bot"]["db"]).decode("latin-1"), "фраза бэкапов уехала в реплике"
    other = seal.new_k()
    with pytest.raises(seal.PackageError):
        seal.open_parts(blob, other)


def test_the_share_that_goes_with_the_package_is_the_reserves_point_of_the_current_generation(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    gen, share = paired.reserve_share_reserve()
    assert (built["m_gen"], built["share"]) == (gen, share)


def test_the_replica_that_went_out_is_kept_as_base_until_the_next_one_is_confirmed(paired, make_active_client, clock, tmp_path):
    make_active_client(name="П1", tg_id=7001)
    b1 = _tick(paired, clock)
    assert _base_gens(tmp_path) == {1}, "снимок для слияния при возврате не сохранён до подтверждения"
    mode = stat.S_IMODE(os.stat(tmp_path / "reserve" / "out" / "base-1.db").st_mode)
    assert mode == 0o600, f"снимок с данными пользователей читаем шире 0600: {oct(mode)}"
    _ack(paired, b1)
    make_active_client(name="П2", tg_id=7002)
    b2 = _tick(paired, clock)
    assert _base_gens(tmp_path) == {1, 2}, "до pkg_ok на второй пакет первый снимок обязан лежать"
    _ack(paired, b2)
    make_active_client(name="П3", tg_id=7003)
    b3 = _tick(paired, clock)
    _ack(paired, b3)
    gens = _base_gens(tmp_path)
    assert 3 in gens and 1 not in gens, f"старые снимки копятся: {sorted(gens)}"


def test_the_base_snapshot_is_the_replica_of_that_package(paired, make_active_client, clock, tmp_path):
    import sqlite3
    make_active_client(name="Первый", tg_id=7001)
    b1 = _tick(paired, clock)
    con = sqlite3.connect(tmp_path / "reserve" / "out" / "base-1.db")
    try:
        names = {r[0] for r in con.execute("SELECT name FROM clients")}
    finally:
        con.close()
    assert "Первый" in names
    parts = seal.open_parts(bytes.fromhex(b1["blob"]), paired.reserve_k())
    assert (tmp_path / "reserve" / "out" / "base-1.db").read_bytes() == bytes.fromhex(parts["bot"]["db"])


# ── приём на резерве ─────────────────────────────────────────────────────────

@pytest.fixture()
def built(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    return _tick(paired, clock)


def _receive(svc, b, **over):
    args = dict(gen=b["gen"], fp=b["fp"], blob=bytes.fromhex(b["blob"]), share=b["share"], m_gen=b["m_gen"])
    args.update(over)
    return svc.reserve_pkg_in(args["gen"], args["fp"], args["blob"], args["share"], args["m_gen"])


def test_a_good_package_is_stored_privately_with_the_share_and_confirmed(paired, built, tmp_path):
    assert _receive(paired, built) == ("pkg_ok", "")
    path = tmp_path / "reserve" / "guest.pkg"
    assert path.read_bytes() == bytes.fromhex(built["blob"])
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    rec = paired.rsv_get("rsv_pkg_in")
    assert (rec["gen"], rec["fp"], rec["share"], rec["m_gen"]) == (built["gen"], built["fp"], built["share"], built["m_gen"])
    assert _receive(paired, built) == ("pkg_ok", ""), "повторная доставка того же пакета — не ошибка"


@pytest.mark.parametrize("over, word", [
    ({"fp": "00" * 32}, "отпечат"),
    ({"gen": 99}, "поколение"),
    ({"m_gen": 7}, "поколение ключа"),
    ({"share": "ab" * 5}, "доля"),
    ({"share": "не-hex"}, "доля"),
    ({"blob": b"AWGK" + b"\x00" * 100}, ""),
    ({"blob": b""}, ""),
])
def test_a_package_that_does_not_match_its_header_is_refused_and_not_stored(paired, built, tmp_path, over, word):
    reply, why = _receive(paired, built, **over)
    assert reply == "pkg_bad" and word in why, (reply, why)
    assert not (tmp_path / "reserve" / "guest.pkg").exists(), "негодный пакет лёг на диск"
    assert not paired.rsv_get("rsv_pkg_in"), "негодный пакет записан в состояние"


def test_a_flipped_byte_breaks_the_fingerprint(paired, built):
    blob = bytearray(bytes.fromhex(built["blob"]))
    blob[100] ^= 0x01
    reply, _why = _receive(paired, built, blob=bytes(blob))
    assert reply == "pkg_bad"


def test_dropping_the_received_package_removes_file_and_record(paired, built, tmp_path):
    _receive(paired, built)
    paired.reserve_pkg_in_drop()
    assert not (tmp_path / "reserve" / "guest.pkg").exists() and not paired.rsv_get("rsv_pkg_in")
    paired.reserve_pkg_in_drop()                       # повтор — не ошибка


# ── части по каналу ──────────────────────────────────────────────────────────

@pytest.fixture()
def small_chunks(monkeypatch):
    """Часть в 4096 знаков: пакет из одного профиля короче настоящей части."""
    from awgbot.util import pairproto
    monkeypatch.setattr(pairproto, "MAX_BLOB", 4096)


def test_the_package_is_cut_into_chunks_that_fit_the_line_and_join_back(built, small_chunks):
    from awgbot.util import pairproto
    chunks = package.ReservePackageMixin.pkg_chunks(built["blob"])
    assert len(chunks) > 20, "пакет ступени 64 КБ в частях по 2 КБ"
    assert "".join(chunks) == built["blob"]
    assert all(len(c) <= pairproto.MAX_BLOB for c in chunks)


def test_the_chunk_limit_is_exactly_the_package_limit():
    from awgbot.util import pairproto
    assert len(pairproto.chunks("a" * (seal.MAX_SIZE * 2))) == pairproto.MAX_CHUNKS, "пакет предельного размера не режется"
    with pytest.raises(pairproto.ProtocolError):
        pairproto.chunks("a" * (seal.MAX_SIZE * 2 + 1))


async def test_send_package_sends_numbered_chunks_and_stops_at_the_first_failure(built, small_chunks, monkeypatch):
    from awgbot.runtime import pairlink
    sent: list[tuple[str, dict]] = []
    answers = iter([True, False] + [True] * 100)

    async def fake_send(kind, body=None):
        sent.append((kind, body))
        return next(answers)
    monkeypatch.setattr(pairlink, "send", fake_send)
    assert await pairlink.send_package(built) is False
    assert len(sent) == 2, "после оборванной части остальные уходить не должны"
    assert [(k, b["i"]) for k, b in sent] == [("pkg", 0), ("pkg", 1)]
    assert sent[0][1]["n"] == len(package.ReservePackageMixin.pkg_chunks(built["blob"]))
    assert sent[0][1]["share"] == built["share"] and sent[0][1]["fp"] == built["fp"]


async def test_send_package_reports_success_when_every_chunk_went(built, small_chunks, monkeypatch):
    from awgbot.runtime import pairlink
    sent = []

    async def fake_send(kind, body=None):
        sent.append(body)
        return True
    monkeypatch.setattr(pairlink, "send", fake_send)
    assert await pairlink.send_package(built) is True
    assert "".join(b["blob"] for b in sent) == built["blob"]


class _Inbox:
    """Приёмная сторона канала пары без сети: ответы собираются в список."""

    def __init__(self, services, monkeypatch):
        from awgbot.runtime.pairlink import inbound
        self.out: list[tuple[str, dict]] = []

        async def fake_line(sess, kind, body=None):
            self.out.append((kind, body))
        monkeypatch.setattr(inbound, "send_line", fake_line)
        self.obj = types.SimpleNamespace(services=services)
        self.sess = types.SimpleNamespace(parts={})
        self.handler = inbound.InboundMixin._in_pkg

    async def deliver(self, built, order=None, **over):
        chunks = package.ReservePackageMixin.pkg_chunks(built["blob"])
        for i in (order if order is not None else range(len(chunks))):
            body = {"gen": built["gen"], "fp": built["fp"], "i": i, "n": len(chunks), "blob": chunks[i],
                    "share": built["share"], "m_gen": built["m_gen"]}
            body.update(over)
            await self.handler(self.obj, self.sess, body)


async def test_chunks_arriving_in_any_order_make_one_package_and_one_confirmation(paired, built, small_chunks, monkeypatch, tmp_path):
    inbox = _Inbox(paired, monkeypatch)
    n = len(package.ReservePackageMixin.pkg_chunks(built["blob"]))
    await inbox.deliver(built, order=list(reversed(range(n))))
    assert [k for k, _ in inbox.out] == ["pkg_ok", "st"], "ответ — один pkg_ok и st с новым отпечатком"
    assert inbox.out[0][1] == {"gen": built["gen"], "fp": built["fp"]}
    assert (tmp_path / "reserve" / "guest.pkg").exists()
    assert inbox.sess.parts == {}, "собранный пакет не вычищен из сессии"


async def test_an_incomplete_package_gets_no_answer_and_writes_nothing(paired, built, small_chunks, monkeypatch, tmp_path):
    inbox = _Inbox(paired, monkeypatch)
    await inbox.deliver(built, order=[0])
    assert inbox.out == [] and not (tmp_path / "reserve" / "guest.pkg").exists()


async def test_a_package_with_the_wrong_fingerprint_gets_pkg_bad(paired, built, small_chunks, monkeypatch, tmp_path):
    inbox = _Inbox(paired, monkeypatch)
    await inbox.deliver(built, fp="00" * 32)
    assert [k for k, _ in inbox.out] == ["pkg_bad"] and inbox.out[0][1]["gen"] == built["gen"]
    assert not (tmp_path / "reserve" / "guest.pkg").exists()


@pytest.mark.parametrize("i, n", [(0, 0), (5, 2), (-1, 2), (0, 257)])
async def test_chunk_numbers_out_of_range_are_a_protocol_error(paired, built, monkeypatch, i, n):
    from awgbot.util import pairproto
    inbox = _Inbox(paired, monkeypatch)
    with pytest.raises(pairproto.ProtocolError):
        await inbox.handler(inbox.obj, inbox.sess, {"gen": 1, "fp": "x", "i": i, "n": n, "blob": "", "share": "", "m_gen": 1})


# ── досылка при расхождении и копия в бэкапе ─────────────────────────────────

def test_a_peer_that_names_another_package_than_the_confirmed_one_gets_the_mark_again(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    _ack(paired, built)
    assert paired.__dict__.get("_rsv_pkg_due_at") is None
    paired.rsv_put("rsv_peer_state", {"session": True, "pkg": {"fp": built["fp"]}})
    paired.reserve_pkg_resend_if_stale()
    assert paired.__dict__.get("_rsv_pkg_due_at") is None, "резерв держит то, что подтверждено, — досылать нечего"
    paired.rsv_put("rsv_peer_state", {"session": True, "pkg": {"fp": "00" * 32}})
    paired.reserve_pkg_resend_if_stale()
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "резерв потерял пакет — основа обязана прислать заново"
    again = _tick(paired, clock)
    assert again is not None and again["gen"] == built["gen"] + 1


def test_a_peer_that_has_no_package_yet_is_not_a_reason_to_resend_what_was_never_confirmed(paired):
    paired.rsv_put("rsv_peer_state", {"session": True, "pkg": {}})
    paired.reserve_pkg_resend_if_stale()
    assert paired.__dict__.get("_rsv_pkg_due_at") is None


def test_the_share_of_the_other_servers_key_does_not_go_into_a_backup_of_this_one():
    """Запись о пакете второй стороны в копию не идёт целиком: половинная
    (поколение и отпечаток без доли) после восстановления выглядела бы для
    основы как «пакет доставлен», и основа не прислала бы его заново."""
    import json
    from awgbot.domain.backup import archive
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO server_state VALUES ('rsv_pkg_in', ?)",
                (json.dumps({"gen": 4, "fp": "ab", "share": "DUMMY-share-hex", "m_gen": 2}),))
    con.execute("INSERT INTO server_state VALUES ('rsv_out', '{\"state\": \"home\"}')")
    con.execute("INSERT INTO server_state VALUES ('rsv_pkg_sent', '{\"gen\": 9}')")
    archive._strip_foreign_share(con)
    assert con.execute("SELECT value FROM server_state WHERE key = 'rsv_pkg_in'").fetchone() is None, \
        "запись о чужом пакете доехала в копию"
    assert dict(con.execute("SELECT key, value FROM server_state").fetchall()) == {
        "rsv_out": '{"state": "home"}', "rsv_pkg_sent": '{"gen": 9}'}, "вычищены свои записи"
    archive._strip_foreign_share(con)                  # повтор ничего не портит
    assert con.execute("SELECT COUNT(*) FROM server_state").fetchone()[0] == 2


@pytest.mark.parametrize("value", [None, "", "not json", "[1]", "{}"])
def test_stripping_the_share_tolerates_a_missing_empty_or_foreign_value(value):
    con = sqlite3.connect(":memory:")
    from awgbot.domain.backup import archive
    con.execute("CREATE TABLE server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO server_state VALUES ('own', 'DUMMY')")
    if value is not None:
        con.execute("INSERT INTO server_state VALUES ('rsv_pkg_in', ?)", (value,))
    archive._strip_foreign_share(con)
    assert con.execute("SELECT value FROM server_state WHERE key = 'rsv_pkg_in'").fetchone() is None
    assert con.execute("SELECT value FROM server_state WHERE key = 'own'").fetchone() == ("DUMMY",)


def test_stripping_the_share_tolerates_a_base_without_server_state():
    from awgbot.domain.backup import archive
    archive._strip_foreign_share(sqlite3.connect(":memory:"))
