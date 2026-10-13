"""Таблица `inet awg_lan_map` — перевод адресов в туннеле локальных сетей
шлюзов на ВПС: текст из записей слотов, применение, присутствие.

Цена ошибки: двойник не в тот линк — пакет админа уходит в чужую сеть;
перевод не той длины — адрес в туннеле ведёт не на ту машину; цепочка не на
том приоритете — DNS к адресу в туннеле съедает перехват :53, а метка
ставится уже по настоящему адресу (который за двумя шлюзами сразу); текст,
зависящий от порядка записей, — таблица перевыставляется на каждом такте.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.infra import nftjson
from awgbot.infra import routing
from awgbot.infra.routing import lanmap

E = lanmap.Entry
SPACE = "10.240.0.0/12"
PREFIXES = {1: "10.240.0.0/16", 2: "10.241.0.0/16"}
SAME = [E(1, "awglink", "192.168.1.0/24", "10.240.1.0/24"), E(2, "awglink2", "192.168.1.0/24", "10.241.1.0/24")]

# Эталон для двух слотов с одной и той же подсетью: пары в цепочке slot_zone
# раньше одиночных, метка до перевода, перевод —
# префиксом, guard — пространство и линк по метке.
GOLDEN = """#!/usr/sbin/nft -f
# awg-bot: адреса в туннеле для локальных сетей шлюзов (таблица awg_lan_map).
# Текст собирает бот из БД и ставит сам — правки руками перезапишутся.
table inet awg_lan_map
delete table inet awg_lan_map
table inet awg_lan_map {
    set real1 { type ipv4_addr; flags interval; elements = { 192.168.1.0/24 } }
    set real2 { type ipv4_addr; flags interval; elements = { 192.168.1.0/24 } }

    chain slot_zone {
        type filter hook prerouting priority raw; policy accept;
        iifname != { "awglink", "awglink2" } ip daddr != 10.240.0.0/12 accept
        fib daddr type local accept
        iifname "awglink" ip saddr @real1 ip daddr 10.241.0.0/16 ct zone set 19 accept
        iifname "awglink2" ip saddr @real2 ip daddr 10.240.0.0/16 ct zone set 19 accept
        iifname "awglink" ip saddr @real1 ct zone set 2 accept
        iifname "awglink2" ip saddr @real2 ct zone set 3 accept
        ip daddr 10.240.0.0/16 ct zone set 2 accept
        ip daddr 10.241.0.0/16 ct zone set 3 accept
    }

    chain slot_mark {
        type filter hook prerouting priority mangle; policy accept;
        ip daddr 10.240.0.0/16 meta mark set meta mark or 0x2
        ip daddr 10.241.0.0/16 meta mark set meta mark or 0x4
    }

    chain twin_dnat {
        type nat hook prerouting priority dstnat - 10; policy accept;
        dnat ip prefix to ip daddr map { 10.240.1.0/24 : 192.168.1.0/24, 10.241.1.0/24 : 192.168.1.0/24 }
    }

    chain twin_snat {
        type nat hook postrouting priority srcnat - 10; policy accept;
        iifname "awglink" snat ip prefix to ip saddr map { 192.168.1.0/24 : 10.240.1.0/24 }
        iifname "awglink2" snat ip prefix to ip saddr map { 192.168.1.0/24 : 10.241.1.0/24 }
    }

    chain twin_guard {
        type filter hook forward priority filter - 10; policy accept;
        ip daddr 10.240.0.0/12 drop
        meta mark and 0x2 == 0x2 oifname != "awglink" drop
        meta mark and 0x4 == 0x4 oifname != "awglink2" drop
    }
}
"""


def test_two_slots_with_the_same_subnet_render_the_reference_table():
    assert lanmap.render(SAME, SPACE, PREFIXES) == GOLDEN


def test_chains_hook_where_the_scheme_needs_them():
    """raw — до conntrack (зона); mangle — до перевода (метка по адресу в
    туннеле); dstnat − 10 — раньше перехвата :53 (−100); srcnat − 10 — раньше
    маскарада клиентов; guard — раньше файервола ВПС."""
    text = lanmap.render(SAME, SPACE, PREFIXES)
    hooks = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("type ")]
    assert hooks == ["type filter hook prerouting priority raw; policy accept;",
                     "type filter hook prerouting priority mangle; policy accept;",
                     "type nat hook prerouting priority dstnat - 10; policy accept;",
                     "type nat hook postrouting priority srcnat - 10; policy accept;",
                     "type filter hook forward priority filter - 10; policy accept;"]
    assert "output" not in text, "своим процессам ВПС адреса в туннеле не нужны"


def test_the_text_depends_on_the_state_not_on_the_order_of_entries():
    """Текст равен тексту ⇔ состояние равно: сверка по state дешёвая, и
    перестановка записей не перевыставляет таблицу."""
    a = lanmap.render(SAME, SPACE, PREFIXES)
    assert lanmap.render(list(reversed(SAME)), SPACE, PREFIXES) == a
    other = [SAME[0], E(2, "awglink2", "192.168.68.0/24", "10.241.68.0/24")]
    assert lanmap.render(other, SPACE, PREFIXES) != a


def test_nothing_to_translate_means_no_table():
    assert lanmap.render([], SPACE, PREFIXES) == ""


def test_several_subnets_of_a_slot_share_its_set_and_its_maps():
    entries = [E(1, "awglink", "192.168.1.0/24", "10.240.1.0/24"), E(1, "awglink", "172.16.5.0/24", "10.240.5.0/24")]
    text = lanmap.render(entries, SPACE, PREFIXES)
    assert "set real1 { type ipv4_addr; flags interval; elements = { 192.168.1.0/24, 172.16.5.0/24 } }" in text
    assert "dnat ip prefix to ip daddr map { 10.240.1.0/24 : 192.168.1.0/24, 10.240.5.0/24 : 172.16.5.0/24 }" in text
    assert 'iifname "awglink" snat ip prefix to ip saddr map { 192.168.1.0/24 : 10.240.1.0/24, ' \
           '172.16.5.0/24 : 10.240.5.0/24 }' in text
    assert "ct zone set 2 accept" in text and "real2" not in text, "один слот — без пар"


# ── из слотов сервиса ────────────────────────────────────────────────────────

@pytest.fixture()
def two(services, make_active_client):
    from awgbot.core import config
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    return services


def test_a_slot_without_subnets_is_not_in_the_table(two, fake_routing):
    two.gateway_set_lan_subnets(1, "192.168.1.0/24")
    assert two.lan_map_entries() == [E(1, "awglink", "192.168.1.0/24", "10.240.1.0/24")]
    text = fake_routing.lanmap
    assert "real1" in text and "real2" not in text and "10.241.0.0/16" not in text and "awglink2" not in text


def test_a_subnet_in_the_space_or_with_a_clashing_twin_stays_out(two, fake_routing):
    """Данные до 3.3.0, которые ввод теперь не примет, в БД остаются, в
    таблицу не идут — карточка говорит причину."""
    two.db.gateway_update(1, lan_subnets=["192.168.1.0/24", "10.241.0.0/24", "10.10.1.0/24"])
    two.lan_map_ensure()
    assert two.lan_map_entries() == [E(1, "awglink", "192.168.1.0/24", "10.240.1.0/24")]
    assert "10.241.0.0/24" not in fake_routing.lanmap and "10.10.1.0/24" not in fake_routing.lanmap
    issues = two.lan_space_card(two.db.gateway(1))["issues"]
    assert [(i["kind"], i["net"]) for i in issues] == [("space", "10.241.0.0/24"), ("clash", "10.10.1.0/24")]
    assert issues[1]["other"] == "192.168.1.0/24" and issues[1]["twin"] == issues[1]["other_twin"] == "10.240.1.0/24"


# ── nft: применение, снятие, присутствие ────────────────────────────────────

def _nft_log(monkeypatch, rc=0, out=b"", err=b""):
    import shutil
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/sbin/{name}")

    def run(args, *, timeout=10, stdin="", error=RuntimeError):
        calls.append((list(args), stdin))
        return subprocess.CompletedProcess(["nft", *args], rc, out, err)
    monkeypatch.setattr(nftjson, "run", run)
    return calls


def test_apply_checks_first_then_loads_the_same_text(monkeypatch):
    calls = _nft_log(monkeypatch)
    lanmap.apply(GOLDEN)
    assert calls == [(["-c", "-f", "-"], GOLDEN), (["-f", "-"], GOLDEN)]


def test_a_refused_text_is_an_error_and_is_not_loaded(monkeypatch):
    calls = _nft_log(monkeypatch, rc=1, err=b"Error: syntax error")
    with pytest.raises(routing.RoutingError, match="syntax error"):
        lanmap.apply(GOLDEN)
    assert [a for a, _ in calls] == [["-c", "-f", "-"]]


def test_a_long_refusal_goes_to_the_log_in_full_once(monkeypatch, caplog):
    """Ошибка nft длиннее 300 знаков: уведомлению — начало, журналу — весь
    ответ (по началу не понять, есть ли ошибки дальше), и один раз, а не на
    каждом такте."""
    monkeypatch.setattr(lanmap, "_last_full_err", "")
    tail = "/dev/stdin:33:11-16: Error: syntax error, unexpected srcnat"
    _nft_log(monkeypatch, rc=1, err=("/dev/stdin:22:11-14: Error: syntax error\n" + "x" * 400 + "\n" + tail).encode())
    for _ in range(2):
        with pytest.raises(routing.RoutingError) as e:
            lanmap.apply(GOLDEN)
        assert tail not in str(e.value)
    full = [r for r in caplog.records if "полный ответ nft" in r.getMessage()]
    assert len(full) == 1 and tail in full[0].getMessage()
    # journald кладёт запись построчно: каждую строку находит подсказка карточки
    from awgbot.domain.notices.lanmap import lan_map_failed_line
    needle = lan_map_failed_line("x", markup=False).split("grep '")[1].split("'")[0]
    assert all(needle in ln for ln in full[0].getMessage().splitlines()), full[0].getMessage()
    # успех сбрасывает память: тот же отказ после него — снова целиком
    _nft_log(monkeypatch)
    lanmap.apply(GOLDEN)
    _nft_log(monkeypatch, rc=1, err=("/dev/stdin:22:11-14: Error: syntax error\n" + "x" * 400 + "\n" + tail).encode())
    with pytest.raises(routing.RoutingError):
        lanmap.apply(GOLDEN)
    assert len([r for r in caplog.records if "полный ответ nft" in r.getMessage()]) == 2


def test_presence_is_read_from_the_list_of_tables(monkeypatch):
    _nft_log(monkeypatch, out=b"table inet awg_bot_guard\ntable inet awg_lan_map\n")
    assert lanmap.present() is True
    _nft_log(monkeypatch, out=b"table inet awg_bot_guard\ntable inet awg_lan_map_old\n")
    assert lanmap.present() is False
    calls = _nft_log(monkeypatch, rc=1)
    assert lanmap.remove() is False and calls[0][0] == ["delete", "table", "inet", "awg_lan_map"]


def test_no_nft_on_the_host_is_unavailable_not_a_failure(monkeypatch):
    """docker-хост без nftables: таблицы нет и быть не может — RoutingUnavailable,
    сверка с пустым текстом отказом это не считает."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(routing.RoutingUnavailable, match="nft не найден"):
        lanmap.present()



# имена цепочек, проверенные живым nft старых выпусков (таблица awg_bot_guard с 3.2.1)
_KNOWN_CHAINS = {"input", "forward", "postrouting", "prerouting", "output"}


def test_chain_names_are_not_nft_keywords():
    """«chain mark {» nft 1.0.2 (Ubuntu 22.04) отвергает синтаксической
    ошибкой — «mark», «dstnat», «srcnat» у него ключевые слова, и таблица не
    ставится вовсе. Локально nft нет, поэтому правило — формой, по всем
    генераторам nft (код и скрипты поставки): имя цепочки либо из
    проверенных живьём, либо с «_» (ключевых слов с подчёркиванием у nft нет)."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2]
    files = [*root.joinpath("awgbot").rglob("*.py"), *root.joinpath("install").glob("*.sh"), root / "awg-bot.sh"]
    names = {(f.name, m) for f in files
             for m in re.findall(r"\bchain ([A-Za-z][\w-]*) \{", f.read_text(encoding="utf-8"))}
    assert ("lanmap.py", "slot_mark") in names, "генератор awg_lan_map не найден — тест ничего не проверяет"
    bad = sorted((f, n) for f, n in names if n not in _KNOWN_CHAINS and "_" not in n)
    assert not bad, bad
