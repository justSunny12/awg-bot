"""Учёт РФ-трафика в опросе и месячном сбросе (концепт «учёт РФ-трафика» §3.2,
§4.2): дельты по счётчикам таблицы `awg_bot_acct` копятся в устройство и в
итог сервера, поколение отсекает скачки, отказ учёта не трогает обычное
потребление, 1-го числа месяц уходит в архив.

Ядро подменено маленьким `_Kernel`: что лежит в таблице (handle, счётчики,
карты), чем её «синхронизировали», и рычаги отказа. `awg show dump` — подмена
как в tests/e2e/test_sync.py. Ни одного вызова nft."""
from __future__ import annotations

import datetime
import logging

import pytest

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.infra import awg, rfacct
from awgbot.infra.rfacct import AcctState
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


class _Kernel:
    """Таблица учёта в ядре ВПС. sync ведёт себя как настоящая по составу:
    создаёт таблицу (новый handle) и нулевые счётчики новых устройств,
    снимает счётчики ушедших."""

    def __init__(self):
        self.boot = "boot-A"
        self.exists = False
        self.handle = 40
        self.counters: dict[str, int] = {}
        self.up: dict[str, str] = {}
        self.dn: dict[str, str] = {}
        self.read_error = ""
        self.sync_error = ""
        self.reads = 0
        self.syncs: list[dict] = []

    # подмены rfacct.read / rfacct.sync
    def read(self):
        self.reads += 1
        if self.read_error:
            raise rfacct.AcctError(self.read_error)
        if not self.exists:
            return None
        return AcctState(handle=self.handle, counters=dict(self.counters),
                         up=dict(self.up), dn=dict(self.dn), rules=4)

    def sync(self, state, links, subnets, devices):
        self.syncs.append({"state": state, "links": list(links), "subnets": list(subnets),
                           "devices": list(devices)})
        if self.sync_error:
            raise rfacct.AcctError(self.sync_error)
        if not self.exists:
            self.exists = True
            self.handle += 1
            self.counters = {rfacct.COUNTER_UP: 0, rfacct.COUNTER_DN: 0}
            self.up, self.dn = {}, {}
        want = {dev_id: addr for dev_id, addr in devices}
        for name in [n for n in self.counters if n.startswith("d")]:
            if int(name[1:].split("_")[0]) not in want:
                del self.counters[name]
        for m in (self.up, self.dn):
            for a in [a for a, n in m.items() if int(n[1:].split("_")[0]) not in want]:
                del m[a]
        for dev_id, addr in want.items():
            up, dn = rfacct.counter_names(dev_id)
            self.counters.setdefault(up, 0)
            self.counters.setdefault(dn, 0)
            self.up[addr], self.dn[addr] = up, dn
        return True

    # события в мире
    def traffic(self, dev_id=None, up=0, dn=0):
        """Устройство сходило через шлюз: растут его счётчики и итог. dev_id=None
        — трафик адреса, которого в карте нет (итог растёт, разбивка — нет)."""
        self.counters[rfacct.COUNTER_UP] += up
        self.counters[rfacct.COUNTER_DN] += dn
        if dev_id is not None:
            a, b = rfacct.counter_names(dev_id)
            if a in self.counters:
                self.counters[a] += up
                self.counters[b] += dn

    def reboot(self):
        """Перезагрузка ВПС: другой boot_id, таблицы нет."""
        self.boot += "'"
        self.exists = False
        self.counters, self.up, self.dn = {}, {}, {}

    def flush_ruleset(self):
        """`systemctl restart nftables`: тот же boot_id, таблица пропала."""
        self.exists = False
        self.counters, self.up, self.dn = {}, {}, {}


@pytest.fixture()
def kernel(monkeypatch):
    k = _Kernel()
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(rfacct, "read", k.read)
    monkeypatch.setattr(rfacct, "sync", k.sync)
    monkeypatch.setattr(gwsnapshot, "boot_id", lambda: k.boot)
    return k


class _Dump:
    """`awg show dump`: байты туннеля пиров."""

    def __init__(self, monkeypatch):
        self.peers: dict[str, dict] = {}
        monkeypatch.setattr(awg, "show_dump", lambda iface=None: [dict(p) for p in self.peers.values()],
                            raising=False)

    def put(self, dev, rx, tx):
        self.peers[dev.public_key] = {"public_key": dev.public_key, "endpoint": None,
                                      "allowed_ips": f"{dev.address}/32", "address": dev.address,
                                      "last_handshake": None, "rx": rx, "tx": tx}


@pytest.fixture()
def dump(monkeypatch):
    return _Dump(monkeypatch)


def _dev(services, client, name="Тел"):
    return services.db.get_device(services.add_device(client.id, name).device_id)


def _rf(services, dev_id):
    d = services.db.get_device(dev_id)
    return d.rf_rx_month, d.rf_tx_month


def _total(services):
    t = services.rf_month_total()
    return t["rx"], t["tx"]


# ── опрос ────────────────────────────────────────────────────────────────────

def test_first_poll_creates_table_and_counts_nothing_yet(services, kernel, dump, make_active_client):
    """Первый опрос после обновления: таблицы нет — синхронизация её создаёт,
    месяц не трогается. Начало учёта — с первого прочитанного показания
    (следующий такт), см. test_since_is_written_once."""
    c = make_active_client(tg_id=9100)
    d = _dev(services, c)
    services.poll_traffic()
    assert kernel.exists and kernel.syncs[0]["state"] is None
    assert kernel.syncs[0]["links"] == ["awglink"]
    assert (d.id, d.address) in kernel.syncs[0]["devices"]
    assert _total(services) == (0, 0) and _rf(services, d.id) == (0, 0)
    assert services.db.get_state("rf_acct_error") in (None, "")
    services.poll_traffic()
    assert services.db.get_state("rf_acct_since"), "начало учёта не отмечено"
    assert _total(services) == (0, 0)


def test_device_and_server_total_accumulate_across_polls(services, kernel, dump, make_active_client):
    """↑ устройства копится в rf_rx, ↓ — в rf_tx, итог сервера — своей парой."""
    c = make_active_client(tg_id=9101)
    d = _dev(services, c)
    services.poll_traffic()                   # таблица создана
    services.poll_traffic()                   # поколение записано, базы 0
    kernel.traffic(d.id, up=1000, dn=20000)
    services.poll_traffic()
    assert _rf(services, d.id) == (1000, 20000)
    assert _total(services) == (1000, 20000)
    kernel.traffic(d.id, up=500, dn=1500)
    kernel.traffic(None, up=7, dn=9)          # адрес не в карте — только итог
    services.poll_traffic()
    assert _rf(services, d.id) == (1500, 21500), "повторная дельта или потеря"
    assert _total(services) == (1507, 21509)
    services.poll_traffic()                   # ничего не прошло — ничего не прибавилось
    assert _rf(services, d.id) == (1500, 21500) and _total(services) == (1507, 21509)


def test_existing_counters_without_generation_are_not_attributed(services, kernel, dump,
                                                                 make_active_client):
    """Старая БД (восстановление из бэкапа до учёта) при живой таблице с
    накопленным: без поколения это только базы, а не месячный скачок."""
    c = make_active_client(tg_id=9102)
    d = _dev(services, c)
    services.poll_traffic()
    kernel.traffic(d.id, up=10 ** 9, dn=10 ** 9)
    services.db.set_state("rf_acct_gen", "")          # поколения «нет»
    services.poll_traffic()
    assert _total(services) == (0, 0) and _rf(services, d.id) == (0, 0), \
        "накопленное в ядре приписано месяцу"
    kernel.traffic(d.id, up=5, dn=6)
    services.poll_traffic()
    assert _total(services) == (5, 6) and _rf(services, d.id) == (5, 6)


def test_new_device_counts_from_its_first_counter_value(services, kernel, dump, make_active_client):
    """Счётчик нового устройства родился нулём при синхронизации: всё, что на
    нём к следующему опросу, — его трафик (база не нужна). До синхронизации
    трафик устройства — только в итоге."""
    c = make_active_client(tg_id=9103)
    a = _dev(services, c, "A")
    services.poll_traffic(); services.poll_traffic()
    b = _dev(services, c, "B")
    kernel.traffic(b.id, up=300, dn=400)        # счётчика ещё нет — только итог
    services.poll_traffic()                     # синхронизация рождает d<b>_*
    assert _rf(services, b.id) == (0, 0) and _total(services) == (300, 400)
    kernel.traffic(b.id, up=70, dn=80)
    services.poll_traffic()
    assert _rf(services, b.id) == (70, 80), "первое показание нового счётчика потеряно"
    assert _rf(services, a.id) == (0, 0)
    assert _total(services) == (370, 480)


def test_generation_change_takes_the_whole_counter_without_a_jump(services, kernel, dump,
                                                                  make_active_client):
    """Перезагрузка / flush ruleset: счётчики с нуля, прежние базы чужие.
    В месяц идёт ровно то, что накопилось в новой таблице, — ни минуса, ни
    старой базы сверху."""
    c = make_active_client(tg_id=9104)
    d = _dev(services, c)
    services.poll_traffic(); services.poll_traffic()
    kernel.traffic(d.id, up=5000, dn=9000)
    services.poll_traffic()
    assert _total(services) == (5000, 9000)

    kernel.flush_ruleset()
    services.poll_traffic()                     # таблицы нет → создана, handle новый
    kernel.traffic(d.id, up=8000, dn=100)       # больше старой базы по ↑, меньше по ↓
    services.poll_traffic()
    assert _total(services) == (13000, 9100), "после смены handle — скачок или потеря"
    assert _rf(services, d.id) == (13000, 9100)

    kernel.reboot()
    services.poll_traffic()
    kernel.traffic(d.id, up=1, dn=2)
    services.poll_traffic()
    assert _total(services) == (13001, 9102), "после перезагрузки — скачок или потеря"
    assert _rf(services, d.id) == (13001, 9102)


def test_bot_restart_with_the_same_generation_continues_without_a_jump(services, kernel, dump,
                                                                      make_active_client):
    """Перезапуск бота: хэш статики в памяти пропал, таблица и поколение те же —
    дельта обычная, показания ядра второй раз не прибавляются."""
    c = make_active_client(tg_id=9105)
    d = _dev(services, c)
    services.poll_traffic(); services.poll_traffic()
    kernel.traffic(d.id, up=100, dn=200)
    services.poll_traffic()
    from awgbot.domain.services import Services
    again = Services(services.db)
    kernel.traffic(d.id, up=1, dn=1)
    again.poll_traffic()
    assert _total(services) == (101, 201) and _rf(services, d.id) == (101, 201)


def test_removed_device_leaves_the_total_and_its_rows_go_by_cascade(services, kernel, dump,
                                                                    make_active_client):
    """Удалённое устройство: его доля остаётся в итоге («вне профилей»),
    база дельт уходит каскадом, счётчики снимаются ближайшей синхронизацией."""
    c = make_active_client(tg_id=9106)
    a = _dev(services, c, "A")
    b = _dev(services, c, "B")
    services.poll_traffic(); services.poll_traffic()
    kernel.traffic(a.id, up=100, dn=100)
    kernel.traffic(b.id, up=40, dn=60)
    services.poll_traffic()
    assert set(services.db.rf_samples_all()) == {a.id, b.id}

    services.remove_device(b.id)
    assert b.id not in services.db.rf_samples_all(), "rf_samples не ушла каскадом"
    services.poll_traffic()
    assert _total(services) == (140, 160), "удаление устройства уменьшило итог сервера"
    assert all(dev_id != b.id for dev_id, _ in kernel.syncs[-1]["devices"])
    assert rfacct.counter_names(b.id)[0] not in kernel.counters
    assert _rf(services, a.id) == (100, 100)


def test_gateway_device_gets_no_counters_and_its_link_is_counted(services, kernel, dump,
                                                                 make_active_client):
    """Устройство-шлюз РФ-трафиком не является — в карты не попадает; его линк
    из БД — в правилах рядом с линком из конфига."""
    c = make_active_client(tg_id=9107)
    gw = _dev(services, c, "Малина")
    d = _dev(services, c, "Тел")
    services.db.gateway_add(gw.id, "awglink2", 443, "10.99.99.4/30")
    services.poll_traffic()
    call = kernel.syncs[-1]
    assert [dev_id for dev_id, _ in call["devices"]] == [d.id], "устройство-шлюз получило счётчики"
    assert sorted(call["links"]) == ["awglink", "awglink2"]
    assert call["subnets"] == [n for n, _i in config.routing_client_subnets()]


def test_no_links_means_no_accounting_at_all(services, kernel, dump, make_active_client, monkeypatch):
    """Ни слотов, ни интерфейса в конфиге: в nft не ходим, ключей учёта не
    заводим, обычное потребление копится."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    c = make_active_client(tg_id=9108)
    d = _dev(services, c)
    dump.put(d, 100, 100)
    services.poll_traffic()
    dump.put(d, 150, 170)
    services.poll_traffic()
    assert kernel.reads == 0 and kernel.syncs == []
    assert services.db.get_device(d.id).traffic_rx_month == 50
    for key in ("rf_acct_gen", "rf_acct_since", "rf_acct_error", "rf_total_sample"):
        assert not services.db.get_state(key), f"{key} заведён без учёта"


def test_read_failure_keeps_awg_accounting_and_marks_error(services, kernel, dump,
                                                           make_active_client, caplog):
    """Нет nft / таблица не читается: обычное потребление копится как прежде,
    РФ-итог не трогается, строка главной узнаёт «учёт не идёт»; в журнал —
    одна строка на смену состояния, а не на каждый такт."""
    c = make_active_client(tg_id=9109)
    d = _dev(services, c)
    dump.put(d, 100, 100)
    services.poll_traffic(); services.poll_traffic()
    kernel.traffic(d.id, up=10, dn=20)
    services.poll_traffic()
    assert _total(services) == (10, 20)

    kernel.read_error = "nft не найден — поставь пакет nftables"
    kernel.sync_error = "nft не найден — поставь пакет nftables"
    kernel.traffic(d.id, up=1, dn=1)
    dump.put(d, 400, 900)
    with caplog.at_level(logging.INFO, logger="awgbot"):
        services.poll_traffic()
        services.poll_traffic()
    fresh = services.db.get_device(d.id)
    assert (fresh.traffic_rx_month, fresh.traffic_tx_month) == (300, 800), \
        "отказ учёта РФ уронил обычное потребление"
    assert _total(services) == (10, 20)
    assert "nft не найден" in services.db.get_state("rf_acct_error")
    assert services.rf_month_total()["error"]
    warns = [r for r in caplog.records if "учёт РФ-трафика" in r.getMessage()]
    assert len(warns) == 1, [r.getMessage() for r in warns]

    kernel.read_error = kernel.sync_error = ""
    with caplog.at_level(logging.INFO, logger="awgbot"):
        services.poll_traffic()
    assert services.db.get_state("rf_acct_error") == "", "ошибка не снялась после восстановления"
    assert _total(services) == (11, 21), "байты периода отказа (в том же поколении) потеряны"
    assert len([r for r in caplog.records if "учёт РФ-трафика" in r.getMessage()]) == 2


def test_sync_failure_marks_error_but_reading_still_counts(services, kernel, dump,
                                                           make_active_client):
    """Чтение прошло, а применение состава отвергнуто (`nft -c`): показания
    этого такта учтены, ошибка видна."""
    c = make_active_client(tg_id=9110)
    d = _dev(services, c)
    services.poll_traffic(); services.poll_traffic()
    kernel.traffic(d.id, up=3, dn=4)
    kernel.sync_error = "nft -c: Error: syntax error"
    services.poll_traffic()
    assert _total(services) == (3, 4) and _rf(services, d.id) == (3, 4)
    assert "nft -c" in services.db.get_state("rf_acct_error")


def test_since_is_written_once(services, kernel, dump, make_active_client, monkeypatch):
    """«Учёт — с DD.MM» — момент первого удачного опроса, а не последнего."""
    make_active_client(tg_id=9111)
    t0 = timeutil.now()
    monkeypatch.setattr(timeutil, "now", lambda: t0)
    services.poll_traffic(); services.poll_traffic()
    first = services.db.get_state("rf_acct_since")
    assert first
    monkeypatch.setattr(timeutil, "now", lambda: t0 + datetime.timedelta(days=3))
    services.poll_traffic()
    assert services.db.get_state("rf_acct_since") == first


def test_since_is_not_written_while_the_table_cannot_be_read(services, kernel, dump,
                                                             make_active_client):
    make_active_client(tg_id=9112)
    kernel.read_error = kernel.sync_error = "nft не найден"
    services.poll_traffic()
    assert not services.db.get_state("rf_acct_since"), "учёт «начался», не прочитав ни одного счётчика"


# ── месяц ────────────────────────────────────────────────────────────────────

def _prev_month() -> str:
    now = timeutil.now()
    return (now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")


def test_monthly_reset_archives_rf_and_zeroes_it(services, kernel, dump, make_active_client):
    """1-го числа: РФ устройства — в traffic_monthly.rf_*, итог сервера — строкой
    server_traffic_monthly, текущий месяц — с нуля. Базы не трогаются: следующий
    опрос прибавит только новое."""
    c = make_active_client(tg_id=9113)
    a = _dev(services, c, "A")
    only_rf = _dev(services, c, "B")
    services.poll_traffic(); services.poll_traffic()
    services.db.add_traffic_bulk([(a.id, 111, 222)])
    kernel.traffic(a.id, up=10, dn=20)
    kernel.traffic(only_rf.id, up=3, dn=0)       # обычного потребления у B нет
    kernel.traffic(None, up=1, dn=1)
    services.poll_traffic()

    services.reset_monthly_traffic()
    con = services.db._connection()
    rows = {r["device_id"]: (r["month"], r["rx"], r["tx"], r["rf_rx"], r["rf_tx"])
            for r in con.execute("SELECT * FROM traffic_monthly")}
    month = _prev_month()
    assert rows[a.id] == (month, 111, 222, 10, 20)
    assert rows[only_rf.id] == (month, 0, 0, 3, 0), "месяц с одним РФ-трафиком не заархивирован"
    srv = [tuple(r) for r in con.execute("SELECT month, rf_rx, rf_tx FROM server_traffic_monthly")]
    assert srv == [(month, 14, 21)]
    assert _total(services) == (0, 0)
    assert _rf(services, a.id) == (0, 0) and _rf(services, only_rf.id) == (0, 0)

    kernel.traffic(a.id, up=5, dn=5)
    services.poll_traffic()
    assert _total(services) == (5, 5) and _rf(services, a.id) == (5, 5), \
        "после сброса опрос прибавил весь счётчик ядра"


def test_monthly_reset_with_zero_rf_writes_nothing(services, kernel, dump, make_active_client):
    c = make_active_client(tg_id=9114)
    _dev(services, c)
    services.poll_traffic(); services.poll_traffic()
    services.reset_monthly_traffic()
    con = services.db._connection()
    assert con.execute("SELECT COUNT(*) FROM server_traffic_monthly").fetchone()[0] == 0, \
        "нулевой месяц сервера записан в архив"
    assert con.execute("SELECT COUNT(*) FROM traffic_monthly").fetchone()[0] == 0


def test_server_rf_archive_is_a_history_table(services):
    """Архив итога чистится ретеншном вместе с остальной историей."""
    from awgbot.infra.db.schema import HISTORY_TABLES
    assert "server_traffic_monthly" in HISTORY_TABLES
    services.db.snapshot_server_rf("2020-01", 5, 5)
    services.db.purge_histories("2030-01-01T00:00:00+03:00")
    assert services.db._connection().execute(
        "SELECT COUNT(*) FROM server_traffic_monthly").fetchone()[0] == 0


# ── запросы для экранов этапа 2 ──────────────────────────────────────────────

def test_profile_rf_sums_devices_without_gateways(services, make_active_client):
    """РФ профиля — сумма его устройств без устройств-шлюзов, тем же фильтром,
    что обычное потребление профиля."""
    c = make_active_client(tg_id=9115)
    other = make_active_client(tg_id=9116, name="Другой")
    a, b, gw = _dev(services, c, "A"), _dev(services, c, "B"), _dev(services, c, "GW")
    o = _dev(services, other, "O")
    services.db.gateway_add(gw.id, "awglink", 443, "10.99.99.0/30")
    services.db.rf_add_bulk([(a.id, 1, 2), (b.id, 10, 20), (gw.id, 1000, 1000), (o.id, 5, 5)])
    assert services.db.get_client_rf(c.id) == {"rx": 11, "tx": 22}
    by = services.db.rf_by_client()
    assert by[c.id] == (11, 22) and by[other.id] == (5, 5)
    assert services.db.get_total_month_rf() == {"rx": 1016, "tx": 1027}
