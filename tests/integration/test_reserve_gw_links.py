"""Постоянные линки шлюзов основы к резерву — обе стороны (gwside.py,
zonelinks.py): ключи шлюзовых сторон и `slots` у основы, стороны зоны и
`slots_ok` у резерва, раздел `reserve` документа шлюза, Т-РУ18.

Один и тот же сервис играет основу и резерв по очереди: ключи у них в
разных ключах состояния (`rsv_gw_links` / `rsv_guest_links`), так обмен
`slots` → `slots_ok` проходит целиком без сети. Зона и файервол — подмены.

Цена ошибки: стороны линка не сходятся ключами, PSK, адресами /30 или
портом — гостевой канал не поднимется, шлюз не голосует, и при падении
основы переезд — только по аварийному коду; конфиг стороны зоны не 0600 —
закрытый ключ читаем шире; раздел документа, который сам шлюз отклонит
проверкой, — линк к резерву не поднимется никогда; Т-РУ18 по каждому такту —
шум у админа основы.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.infra import rsvzone
from awgbot.infra.nftguard import collect
from awgbot.util import netns, rsvkeys, timeutil, wgconf, wgkeys
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv


@pytest.fixture()
def both(services, rsv, make_active_client, monkeypatch):
    """Пара записана; у «этого сервера» два шлюза с rsv1 и один прежний."""
    rc.pair_row(services)
    services.reserve_keys_init()
    cl = make_active_client(name="Шлюзы", tg_id=7001)
    for slot, caps in ((1, "cfg1,own1,rsv1"), (2, "cfg1,own1,rsv1"), (3, "cfg1,own1")):
        dev = services.add_device(cl.id, f"GW{slot}").device_id
        services.db.gateway_add(dev, "awglink" if slot == 1 else f"awglink{slot}", 443 + slot,
                                f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
        services.db.set_state(f"gwlink_caps_{slot}", caps)
    shells, dels = [], []
    monkeypatch.setattr(rsvzone, "shell", lambda: shells.append(1) or "")
    monkeypatch.setattr(netns, "run_in", lambda path, argv, timeout=10: dels.append(argv))
    services.shells, services.dels = shells, dels
    return services


def _links_dir() -> Path:
    from awgbot.domain.services.reserve import zonelinks
    return Path(zonelinks.links_dir())


def _exchange(s) -> tuple[dict, dict]:
    body = s.reserve_slots_body()
    s.reserve_slots_sent(body)
    ok = s.reserve_slots_in(body["slots"])
    s.reserve_slots_ok_in(ok["slots"])
    return body, ok


def test_slots_carry_keys_only_for_gateways_with_rsv1_and_the_public_key_matches(both):
    body = both.reserve_slots_body()
    assert sorted(s["slot"] for s in body["slots"]) == [1, 2], "в slots слот агента без rsv1 или без слота"
    links = both.reserve_gw_links()
    for item in body["slots"]:
        rec = links[str(item["slot"])]
        assert wgkeys.pubkey_of(rec["priv"]) == item["pub"], "открытый ключ шлюзовой стороны не от её закрытого"
        assert "priv" not in item, "закрытый ключ шлюзовой стороны ушёл резерву"


def test_slots_are_resent_not_more_often_than_every_5_minutes_and_stop_after_slots_ok(both, monkeypatch):
    body = both.reserve_slots_body()
    both.reserve_slots_sent(body)
    assert both.reserve_slots_body() is None, "slots повторился сразу без ответа"
    both.rsv_update("rsv_view", slots_at=timeutil.to_iso(timeutil.now().replace(year=2020)))
    assert both.reserve_slots_body() is not None, "без ответа через 5 мин slots не повторился"
    _exchange(both)
    assert both.reserve_slots_body() is None, "после slots_ok slots всё ещё шлётся"


def test_both_sides_of_each_link_match(both):
    """Сторона зоны (файл у резерва) и шлюзовая (раздел документа) — одна
    пара: ключи крест-накрест, PSK, адреса одного /30, порт, обфускация."""
    _exchange(both)
    for gw in both.db.gateways()[:2]:
        zone_text = (_links_dir() / ("awglink.conf" if gw.id == 1 else f"awglink{gw.id}.conf")).read_text(encoding="utf-8")
        zone = wgconf.parse(zone_text)
        sec = both.gwcfg_reserve(gw)
        side = wgconf.parse(sec["link"]["conf"])
        assert wgkeys.pubkey_of(side.get("Interface", "PrivateKey")) == zone.get("Peer", "PublicKey")
        assert wgkeys.pubkey_of(zone.get("Interface", "PrivateKey")) == side.get("Peer", "PublicKey")
        assert zone.get("Peer", "PresharedKey") == side.get("Peer", "PresharedKey")
        z_addr, g_addr = zone.get("Interface", "Address").split("/")[0], side.get("Interface", "Address").split("/")[0]
        # сторона зоны — единственный пир, через него РФ-трафик пользователей наружу;
        # сторона шлюза — адрес зоны (гостевой канал) и подсеть пользователей основы
        assert zone.get("Peer", "AllowedIPs") == "0.0.0.0/0", "РФ-трафик пользователей из зоны не уйдёт в линк шлюза"
        side_nets = [n.strip() for n in side.get("Peer", "AllowedIPs").split(",")]
        assert side_nets[0] == f"{z_addr}/32" and f"{config.SUBNET_PREFIX}.0/24" in side_nets, side_nets
        assert g_addr != z_addr
        assert side.get("Peer", "Endpoint") == f"{rc.HOST_A}:{zone.get('Interface', 'ListenPort')}"
        for k in ("Jc", "S1", "H4"):
            assert zone.get("Interface", k) == side.get("Interface", k), f"обфускация {k} сторон разошлась"
        assert side.get("Peer", "PersistentKeepalive") and zone.get("Peer", "PersistentKeepalive"), \
            "keepalive не на обеих сторонах — одна сторона не увидит другую"


def test_zone_side_configs_are_private_and_their_ports_open_on_the_host(both):
    _, ok = _exchange(both)
    files = sorted(_links_dir().glob("awglink*.conf"))
    assert [f.name for f in files] == ["awglink.conf", "awglink2.conf"]
    assert all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in files), "конфиг стороны зоны шире 0600"
    assert sorted(collect.zone_link_ports()) == sorted(s["port"] for s in ok["slots"])
    assert both.shells, "оболочка зоны не поднята после сторон"
    assert both.rsv_get("rsv_guest_links")["1"]["cidr"] != both.rsv_get("rsv_guest_links")["2"]["cidr"]


def test_a_repeated_slots_keeps_keys_and_ports_and_a_dropped_slot_is_removed(both):
    body, ok1 = _exchange(both)
    first = {s["slot"]: (s["pub"], s["port"], s["cidr"]) for s in ok1["slots"]}
    ok2 = both.reserve_slots_in(body["slots"])
    assert {s["slot"]: (s["pub"], s["port"], s["cidr"]) for s in ok2["slots"]} == first, \
        "повтор slots сменил ключ, порт или адреса стороны зоны"
    both.reserve_slots_in([s for s in body["slots"] if s["slot"] == 1])
    assert not (_links_dir() / "awglink2.conf").exists(), "сторона снятого слота осталась"
    assert ["ip", "link", "del", "awglink2"] in both.dels
    assert (_links_dir() / "awglink.conf").exists()


def test_the_document_section_passes_the_gateways_own_check(both):
    """Шлюз проверяет раздел своим check_reserve: конфиг по белому списку
    rsvlink, Table = off, Endpoint, доля (точка слот + 1, 16 байт, поколение)."""
    _exchange(both)
    gw = both.db.gateways()[1]
    sec = both.gwcfg_reserve(gw)
    parsed = gwcfg.parse({"gen": 1, "hash": "ab" * 8, "reserve": sec})["sections"]["reserve"]
    checked = gwcfg.check_reserve(parsed)
    gen, y = both.reserve_share_gateway(gw.id)
    assert checked["share"] == {"x": rsvkeys.gateway_x(gw.id), "y": y, "gen": gen}
    assert checked["state"] == "idle" and checked["slot"] == gw.id and checked["record"] == rc.HOST_A
    assert gwcfg.reserve_print(sec) and gwcfg.reserve_print(sec) == gwcfg.reserve_print(dict(sec))


def test_the_section_print_changes_with_state_move_and_share(both):
    _exchange(both)
    gw = both.db.gateways()[0]
    sec = both.gwcfg_reserve(gw)
    base = gwcfg.reserve_print(sec)
    for change in ({"state": "away"}, {"move": 7}, {"share": {**sec["share"], "gen": 9}}):
        assert gwcfg.reserve_print({**sec, **change}) != base, f"отпечаток не заметил {change}"


def test_no_section_for_a_slot_without_a_zone_side(both):
    assert both.gwcfg_reserve(both.db.gateways()[0]) is None, "раздел без стороны зоны — линк в никуда"


def test_unpairing_drops_the_zone_sides(both):
    _exchange(both)
    both.reserve_zone_links_drop()
    assert not list(_links_dir().glob("awglink*.conf")) and not both.reserve_guest_links()
    assert not collect.zone_link_ports(), "порты снятых сторон остались во входе хоста"


# ── Т-РУ18 ──────────────────────────────────────────────────────────────────

def test_a_gateway_off_the_reserve_for_10_minutes_is_told_once_and_its_return_too(both, monkeypatch):
    snaps = {1: {"rsv": {"seen": "ok"}}, 2: {"rsv": {"seen": "ok"}}}
    monkeypatch.setattr(both, "gwlink_snapshot", lambda slot: snaps.get(slot, {}))
    monkeypatch.setattr(both, "gwlink_session", lambda slot: {"since": "x", "caps": ["cfg1", "rsv1"] if slot != 3 else ["cfg1"]})
    assert not both.reserve_gw_links_tick()
    recent = timeutil.to_iso(timeutil.now())
    snaps[2] = {"rsv": {"seen": "down", "since": recent}}
    assert not both.reserve_gw_links_tick(), "Т-РУ18 раньше 10 минут"
    old = timeutil.to_iso(timeutil.now().replace(year=2025))
    snaps[2] = {"rsv": {"seen": "down", "since": old}}
    first = both.reserve_gw_links_tick()
    assert len(first) == 1 and "не на связи с резервом" in first[0].text and "«GW2»" in first[0].text
    assert not first[0].text.endswith(".")
    assert not both.reserve_gw_links_tick(), "Т-РУ18 повторилось"
    snaps[2] = {"rsv": {"seen": "ok"}}
    back = both.reserve_gw_links_tick()
    assert len(back) == 1 and "снова на связи" in back[0].text


def test_all_gateways_off_the_reserve_get_one_extra_line(both, monkeypatch):
    old = timeutil.to_iso(timeutil.now().replace(year=2025))
    monkeypatch.setattr(both, "gwlink_snapshot", lambda slot: {"rsv": {"seen": "down", "since": old}})
    monkeypatch.setattr(both, "gwlink_session", lambda slot: {"since": "x", "caps": ["cfg1", "rsv1"] if slot != 3 else ["cfg1"]})
    got = both.reserve_gw_links_tick()
    assert len(got) == 3 and "Ни один шлюз" in got[-1].text and "аварийному коду" in got[-1].text
    assert not both.reserve_gw_links_tick()


def test_the_section_says_away_only_while_clients_are_at_the_reserve(both):
    """Основа, не знающая о переезде, не может закрыть пересылку шлюзам: дома
    без переездов номер переезда 0 — любое serving резерва новее; away —
    только после подъёма клона (шаг записи и дальше)."""
    _exchange(both)
    gw = both.db.gateways()[0]
    sec = both.gwcfg_reserve(gw)
    assert (sec["state"], sec["move"]) == ("idle", 0), sec
    both.reserve_set_out("leaving", kind="button", step="up", gen=4)
    assert both.gwcfg_reserve(gw)["state"] == "idle", "away до подъёма клона — пересылка без пользователей у резерва"
    both.reserve_set_out("leaving", kind="button", step="record", gen=4)
    assert (both.gwcfg_reserve(gw)["state"], both.gwcfg_reserve(gw)["move"]) == ("away", 4)
    both.reserve_set_out("away", gen=4)
    assert both.gwcfg_reserve(gw)["state"] == "away"


# ── порт гостевого канала едет к шлюзу ──────────────────────────────────────

def test_the_guest_channel_port_of_the_reserve_rides_slots_ok_into_the_document_section(both):
    """Порт гостевого канала резерва (routing.link_channel_port у него) —
    в slots_ok, оттуда в раздел reserve документа шлюзу: иначе шлюз ходит на
    порт по умолчанию, а у резерва порт другой — канал молчит, шлюз не
    голосует."""
    from awgbot.core import settings
    settings.set_value("app.routing.link_channel_port", 8791)
    _body, ok = _exchange(both)
    assert {s["chan"] for s in ok["slots"]} == {8791}, ok["slots"]
    sec = both.gwcfg_reserve(both.db.gateways()[0])
    assert sec["chan"] == 8791
    parsed = gwcfg.parse({"gen": 1, "hash": "ab" * 8, "reserve": sec})["sections"]["reserve"]
    assert gwcfg.check_reserve(parsed)["chan"] == 8791, "порт канала не прошёл разбор и проверку шлюза"


# ── оболочка зоны после перезагрузки ────────────────────────────────────────

def test_the_zone_shell_is_raised_again_when_its_namespace_is_gone(both, rsv, monkeypatch):
    _exchange(both)
    both.shells.clear()
    rsv.fw.clear()
    monkeypatch.setattr(netns, "exists", lambda path: False)
    assert both.reserve_zone_shell_ensure() is True
    assert both.shells == [1], "после перезагрузки хоста зона-оболочка не поднята — --take откажет"
    assert rsv.fw, "после подъёма зоны файервол хоста не перерисован (порты сторон линков)"


def test_the_zone_shell_is_left_alone_while_its_namespace_exists(both, monkeypatch):
    _exchange(both)
    both.shells.clear()
    monkeypatch.setattr(netns, "exists", lambda path: True)
    assert both.reserve_zone_shell_ensure() is True and both.shells == [], "живую зону пересоздают каждую минуту"


def test_no_zone_shell_without_link_sides_or_without_a_pair(both, monkeypatch):
    monkeypatch.setattr(netns, "exists", lambda path: False)
    assert both.reserve_zone_shell_ensure() is False and both.shells == [], "оболочка без сторон линков"
    _exchange(both)
    both.shells.clear()
    both.reserve_unpair()
    assert both.reserve_zone_shell_ensure() is False and both.shells == []


def test_a_failing_zone_shell_is_reported_as_false_and_does_not_raise(both, rsv, monkeypatch):
    _exchange(both)
    rsv.fw.clear()
    monkeypatch.setattr(netns, "exists", lambda path: False)

    def boom():
        raise rsvzone.ZoneError("ip netns add: нет прав")
    monkeypatch.setattr(rsvzone, "shell", boom)
    assert both.reserve_zone_shell_ensure() is False
    assert not rsv.fw, "зона не поднялась, а файервол перерисован под неё"
