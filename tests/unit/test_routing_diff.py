"""Условная маршрутизация: дифф перед записью (аудит, п.1).

Реконсиляция идёт каждый тик монитора; раньше она безусловно пересобирала все
наборы ipset и цепочку маркировки. Теперь состояние читается одним `ipset save`
и одним `iptables -S`, и пишется только расхождение. Разбор вывода проверяем на
образцах в формате iptables-nft/ipset, любое непонятное — пересборка."""
from __future__ import annotations

import subprocess


from awgbot.core import config
from awgbot.infra import routing


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out.encode(), stderr=b"")


IPSET_SAVE = """create vpn_u3 hash:net family inet hashsize 1024 maxelem 65536 bucketsize 12 initval 0x1a2b3c4d
add vpn_u3 5.255.255.0/24
add vpn_u3 77.88.8.8
create rt_src_u3 hash:ip family inet hashsize 1024 maxelem 65536 bucketsize 12 initval 0x0
add rt_src_u3 10.9.1.5
add rt_src_u3 10.9.1.6
create rt_src_u7 hash:ip family inet hashsize 1024 maxelem 65536
create rt_src_u3_tmp hash:ip family inet hashsize 1024 maxelem 65536
"""


def test_parse_ipset_save_members_and_empty_sets():
    sets = routing.parse_ipset_save(IPSET_SAVE)
    assert sets["vpn_u3"] == {"5.255.255.0/24", "77.88.8.8"}
    assert sets["rt_src_u3"] == {"10.9.1.5", "10.9.1.6"}
    assert sets["rt_src_u7"] == set(), "пустой набор — есть, но без членов"
    assert "rt_src_u3_tmp" in sets


MANGLE_S = (
    "-N AWGBOT_RT\n"
    "-A AWGBOT_RT -m set --match-set rt_src_u3 src -m set --match-set vpn_u3 dst -j MARK --set-xmark 0x1/0xffffffff\n"
    # iptables-nft печатает опции в своём порядке — dst раньше src
    "-A AWGBOT_RT -m set --match-set vpn_u7 dst -m set --match-set rt_src_u7 src -j MARK --set-xmark 0x1/0xffffffff\n"
)


def test_parse_chain_rules_is_structural(monkeypatch):
    monkeypatch.setattr(routing, "_MARK", "0x1/0xffffffff")
    assert routing.parse_chain_rules(MANGLE_S, "AWGBOT_RT") == [
        ("rt_src_u3", "vpn_u3"), ("rt_src_u7", "vpn_u7")]
    assert routing.parse_chain_rules("-N AWGBOT_RT\n", "AWGBOT_RT") == [], "пустая цепочка"
    # чужая форма правила или другая метка → None → пересборка
    assert routing.parse_chain_rules("-N AWGBOT_RT\n-A AWGBOT_RT -j ACCEPT\n", "AWGBOT_RT") is None
    other_mark = MANGLE_S.replace("0x1/0xffffffff", "0x2/0xffffffff")
    assert routing.parse_chain_rules(other_mark, "AWGBOT_RT") is None
    assert routing.parse_chain_rules("-A OTHER -j MARK --set-xmark 0x1/0xffffffff\n", "AWGBOT_RT") is None


def test_parse_nat_exempt():
    text = "-N AWGBOT_RTNAT\n-A AWGBOT_RTNAT -s 10.8.1.5/32 -j ACCEPT\n-A AWGBOT_RTNAT -s 10.8.1.9/32 -j ACCEPT\n"
    assert routing.parse_nat_exempt(text, "AWGBOT_RTNAT") == {"10.8.1.5", "10.8.1.9"}
    assert routing.parse_nat_exempt("-N AWGBOT_RTNAT\n", "AWGBOT_RTNAT") == set()
    assert routing.parse_nat_exempt("-A AWGBOT_RTNAT -j RETURN\n", "AWGBOT_RTNAT") is None


# ── replace_members / ensure_set ─────────────────────────────────────────────

def _host_recorder(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(routing, "_host", lambda args, **kw: calls.append(list(args)) or _cp())
    return calls


def test_replace_members_skips_when_snapshot_matches(monkeypatch):
    calls = _host_recorder(monkeypatch)
    routing.replace_members("rt_src_u3", "hash:ip", ["10.9.1.6", "10.9.1.5"],
                            current={"10.9.1.5", "10.9.1.6"})
    assert calls == [], "состав тот же — ни одного exec"


def test_replace_members_rewrites_when_differs_and_skips_create_if_known(monkeypatch):
    calls = _host_recorder(monkeypatch)
    routing.replace_members("rt_src_u3", "hash:ip", ["10.9.1.5"], current={"10.9.1.5", "10.9.1.6"})
    flat = [" ".join(c) for c in calls]
    assert not any(f.startswith("ipset create rt_src_u3 ") for f in flat), "набор уже есть в снимке"
    assert any(f.startswith("ipset create rt_src_u3_tmp") for f in flat)
    assert any(f.startswith("ipset restore") for f in flat)
    assert "ipset swap rt_src_u3_tmp rt_src_u3" in flat
    # окно без набора не появляется: flush нигде нет
    assert not any("flush" in f for f in flat)


def test_replace_members_without_snapshot_behaves_as_before(monkeypatch):
    calls = _host_recorder(monkeypatch)
    routing.replace_members("rt_src_u3", "hash:ip", ["10.9.1.5"])
    flat = [" ".join(c) for c in calls]
    assert flat[0].startswith("ipset create rt_src_u3 hash:ip -exist")
    assert "ipset swap rt_src_u3_tmp rt_src_u3" in flat


def test_empty_desired_set_is_still_written_when_live_has_members(monkeypatch):
    """Выключенный профиль: желаемый src-набор пуст, а в ядре ещё адреса —
    это расхождение, и его обязаны записать (иначе режим не выключится)."""
    calls = _host_recorder(monkeypatch)
    routing.replace_members("rt_src_u3", "hash:ip", [], current={"10.9.1.5"})
    assert any(c[:2] == ["ipset", "swap"] for c in calls)


def test_ensure_set_skips_exec_when_known(monkeypatch):
    calls = _host_recorder(monkeypatch)
    routing.ensure_set("vpn_u3", "hash:net", exists=True)
    assert calls == []
    routing.ensure_set("vpn_u3", "hash:net")
    assert calls == [["ipset", "create", "vpn_u3", "hash:net", "-exist"]]


def test_snapshot_sets_none_on_failure(monkeypatch):
    monkeypatch.setattr(routing, "_host", lambda args, **kw: _cp(1))
    assert routing.snapshot_sets() is None
    monkeypatch.setattr(routing, "_host", lambda args, **kw: _cp(0, IPSET_SAVE))
    assert routing.snapshot_sets()["rt_src_u3"] == {"10.9.1.5", "10.9.1.6"}


# ── rebuild_chain ────────────────────────────────────────────────────────────

def _mangle_recorder(monkeypatch, listing):
    calls: list[list[str]] = []

    def fake(args, **kw):
        calls.append(list(args))
        if args[:1] == ["-S"]:
            return _cp(1) if listing is None else _cp(0, listing)
        return _cp()
    monkeypatch.setattr(routing, "_mangle", fake)
    monkeypatch.setattr(routing, "_MARK", "0x1/0xffffffff")
    monkeypatch.setattr(config, "ROUTING_CHAIN", "AWGBOT_RT")
    monkeypatch.setattr(config, "ROUTING_SET_SRC_PREFIX", "rt_src_u")
    monkeypatch.setattr(config, "ROUTING_SET_USER_PREFIX", "vpn_u")
    return calls


def test_rebuild_chain_is_a_noop_when_rules_match(monkeypatch):
    calls = _mangle_recorder(monkeypatch, MANGLE_S)
    routing.rebuild_chain([7, 3])
    assert calls == [["-S", "AWGBOT_RT"]], "совпало — один exec на чтение и ни одного на запись"


def test_rebuild_chain_rebuilds_on_difference_missing_chain_or_foreign_rule(monkeypatch):
    for listing in (MANGLE_S, None, "-N AWGBOT_RT\n-A AWGBOT_RT -j ACCEPT\n"):
        calls = _mangle_recorder(monkeypatch, listing)
        routing.rebuild_chain([3])
        assert ["-F", "AWGBOT_RT"] in calls, listing
        adds = [c for c in calls if c[:1] == ["-A"]]
        assert len(adds) == 1 and "rt_src_u3" in adds[0] and "vpn_u3" in adds[0]
        # Правило метит то, что В НАБОРЕ, — без `!`. Разница с упразднённой
        # обратной моделью — ровно один символ, обе версии собираются и выглядят
        # рабочими, а трафик едет в противоположные стороны.
        assert "--match-set" in adds[0] and "!" not in adds[0], \
            "вернулась инверсия: набор снова означал бы заграницу"
        # порядок: чтение → создать → флаш → правила
        assert calls.index(["-F", "AWGBOT_RT"]) < calls.index(adds[0])


def test_rebuild_chain_order_matters(monkeypatch):
    """Тот же набор правил в другом порядке — не совпадение: порядок в
    цепочке определяет, чьё правило сработает первым."""
    swapped = MANGLE_S.replace("rt_src_u3 src -m set --match-set vpn_u3", "X").replace(
        "vpn_u7 dst -m set --match-set rt_src_u7 src", "rt_src_u3 src -m set --match-set vpn_u3 dst").replace(
        "X", "vpn_u7 dst -m set --match-set rt_src_u7 src")
    calls = _mangle_recorder(monkeypatch, swapped)
    routing.rebuild_chain([3, 7])
    assert ["-F", "AWGBOT_RT"] in calls


# ── sync_nat_exempt (docker-режим) ───────────────────────────────────────────

def test_nat_exempt_skips_rebuild_when_addresses_match(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    monkeypatch.setattr(config, "ROUTING_NAT_CHAIN", "AWGBOT_RTNAT")
    seen: list[list[str]] = []

    def cont(args, **k):
        seen.append(list(args))
        if "-S" in args:
            return _cp(0, "-N AWGBOT_RTNAT\n-A AWGBOT_RTNAT -s 10.8.1.5/32 -j ACCEPT\n")
        return _cp()
    monkeypatch.setattr(routing, "_cont", cont)
    monkeypatch.setattr(routing, "_cont_ok", lambda args: True)
    routing.sync_nat_exempt(["10.8.1.5"])
    assert not any("-F" in a or "-A" in a for a in seen), "состав тот же — цепочку не трогаем"
    seen.clear()
    routing.sync_nat_exempt(["10.8.1.5", "10.8.1.6"])
    assert any("-F" in a for a in seen) and sum("-A" in a for a in seen) == 2


# ── services: снимок доезжает до replace_members ─────────────────────────────

def test_routing_apply_passes_live_snapshot(services, make_active_client, fake_routing, monkeypatch):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")           # профиль известен → есть src-набор
    seen: list[dict] = []
    orig = routing.replace_members
    monkeypatch.setattr(routing, "replace_members",
                        lambda name, kind, members, current=None: (seen.append({"name": name, "current": current}), orig(name, kind, members, current))[1])
    monkeypatch.setattr(routing, "snapshot_sets", lambda: {routing.src_set(c.id): {"10.0.0.1"}})
    services.reconcile_routing()
    assert any(s["name"] == routing.src_set(c.id) and s["current"] == {"10.0.0.1"} for s in seen)
    seen.clear()
    monkeypatch.setattr(routing, "snapshot_sets", lambda: None)   # снимок не прочитался
    services.reconcile_routing()
    assert all(s["current"] is None for s in seen), "без снимка — безусловная перезапись, как раньше"
