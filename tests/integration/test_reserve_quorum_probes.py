"""Кворум резерва, этап зондов, основной бот (domain/services/reserve/quorum:
members, probes; концепт «кворум» §2.2, §2.5, §3.1, §3.7.11): состав линии по
фактам и поколение, причины «не включено», лидер своей линии, ключи и адреса
зондов узлов, пиры-зонды и второй адрес на интерфейсе AWG, конфиги зондов
второму серверу (`qprobe`) и своим шлюзам (раздел `quorum` документа), свои
интерфейсы зондов, уборка при удалении пары, бэкап, счётчики клона.

Хост — подмены `_reserve_quorum_common`: конфиг awg0 файлом (настоящие
add_peer/remove_peer), скрипт зондов моделью, свои адреса серверов.

Цена ошибки: поколение, растущее без смены состава, — голоса шлюзов
сбрасываются окном прежнего поколения на каждом такте; шлюз без `quo1` в
составе — v больше, чем может ответить, и лидер лишается кворума ни за что;
пир-зонд не ровно одним /32 из блока — рельс 4.1.x после отката принимает его
за неизвестное устройство (тревога и карантин); конфиг зонда, который агент
отвергнет проверкой, — шлюз не голосует никогда; зонды при втором сервере
4.1.x — его гостевой канал рвётся на незнакомом `qview`; закрытые ключи
зондов в бэкапе — копия раздаёт голоса восстановленному чужому хосту.
"""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.core import config, settings
from awgbot.domain import gwcfg
from awgbot.domain.gwcfg import check as gwcheck
from awgbot.util import probenet, wgkeys
from awgbot.util.rsvpkg import vpn
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_quorum_common as qc

pytestmark = pytest.mark.integration
rsv = rc.rsv
quo = qc.quo


def _gw(s, slot: int):
    return next(g for g in s.db.gateways() if int(g.id) == slot)


def _ready(s):
    """Состав, ключи зондов и пиры на awg0 — как после первого такта."""
    m = s.reserve_quorum_refresh()
    s.reserve_probes_refresh(m)
    s.reserve_probe_peers_sync(m)
    return m


# ── состав ───────────────────────────────────────────────────────────────────

def test_the_line_is_both_servers_and_the_gateways_with_quo1(quo):
    m = quo.reserve_quorum_refresh()
    assert (m.l, m.g, m.members) == ("a", 1, ("a", "b", "a1", "a2")), m
    assert quo.reserve_quorum_side() == "a"


def test_the_generation_grows_only_when_the_members_change(quo):
    assert quo.reserve_quorum_refresh().g == 1
    assert quo.reserve_quorum_refresh().g == 1, "поколение выросло без смены состава"
    quo.db.set_state("gwlink_caps_3", "cfg1,rsv1,quo1")
    m = quo.reserve_quorum_refresh()
    assert (m.g, m.members) == (2, ("a", "b", "a1", "a2", "a3")), "агент обновился до quo1 — в состав не вошёл"
    quo.db.set_state("gwlink_caps_2", "cfg1,rsv1")
    m = quo.reserve_quorum_refresh()
    assert (m.g, m.members) == (3, ("a", "b", "a1", "a3")), "откат агента ниже quo1 — узел остался"


def test_a_slot_beyond_the_block_is_not_a_member_and_says_why(quo, make_active_client, monkeypatch):
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "add_peer", lambda *a, **k: None)      # устройство шлюза — не предмет теста
    qc.add_gateways(quo, make_active_client, {59: "cfg1,rsv1,quo1"}, tg_id=7002)
    m = quo.reserve_quorum_refresh()
    assert "a59" not in m.members
    assert "slot:59" in quo.reserve_quorum_off_reasons()


def test_without_a_pair_there_is_no_line(quo):
    quo.reserve_quorum_refresh()
    quo.db.reserve_peer_delete()
    assert quo.reserve_quorum_refresh() is None
    assert quo.rsv_get("rsv_quorum") == {}, "состав снятой пары остался"


def test_the_vote_is_on_with_a_quo1_peer_and_a_quo1_gateway(quo):
    """Пара с `quo1`, шлюзы `quo1`, состав подтверждён и ключи на месте — причин «не
    включено» нет: иначе голосование не включается никогда."""
    qc.enable(quo)
    assert quo.reserve_quorum_off_reasons() == [] and quo.reserve_quorum_active()


def test_without_a_quo1_gateway_the_line_is_the_two_servers_with_v_1(quo):
    """Шлюзов с агентом 4.2.0 нет — линия всё равно есть: состав {a, b},
    голосует только второй сервер; «нет шлюзов» — уже не причина выключения,
    а признак v < 2: решений по голосам нет."""
    for slot in (1, 2):
        quo.db.set_state(f"gwlink_caps_{slot}", "cfg1,rsv1")
    m = quo.reserve_quorum_refresh()
    assert m is not None and m.members == ("a", "b"), m
    assert "no_gateways" not in quo.reserve_quorum_off_reasons(), quo.reserve_quorum_off_reasons()
    assert not quo.reserve_quorum_decisions(), "v = 1: решения по голосам разрешены"


def test_timing_that_does_not_add_up_turns_the_vote_off(quo):
    settings.set_value("app.reserve.quorum", {**quo.reserve_quorum_settings(), "up_seconds": 30})
    quo.reserve_quorum_refresh()
    assert any(c.startswith("timing:") for c in quo.reserve_quorum_off_reasons())


def test_a_users_subnet_over_the_probe_block_turns_the_vote_off(quo, monkeypatch):
    import ipaddress
    monkeypatch.setattr(config, "routing_client_subnets", lambda: [ipaddress.ip_network("169.254.0.0/16")])
    quo.reserve_quorum_refresh()
    assert "overlap:169.254.0.0/16" in quo.reserve_quorum_off_reasons()


def test_the_leader_host_is_this_server_at_home_and_the_reserve_while_away(quo):
    assert quo.reserve_quorum_leader_host() == "a"
    quo.rsv_update("rsv_out", state="away")
    assert quo.reserve_quorum_leader_host() == "b", "в отъезде лидер линии — бот основы на резерве"


def test_st_carries_the_line_for_the_second_server(quo):
    qc.enable(quo)
    part = quo.reserve_st_body().get("quo")
    assert part == {"l": "a", "g": 1, "members": ["a", "b", "a1", "a2"], "active": True, "why": [], "port": 8789}, part


# ── ключи и адреса зондов ────────────────────────────────────────────────────

def test_every_member_gets_its_probe_key_and_address(quo):
    m = quo.reserve_quorum_refresh()
    line = quo.reserve_probes_refresh(m)
    assert (line["net"], line["resp"], line["resp_clone"], line["port"]) == ("169.254.88.0/26", "169.254.88.1", "169.254.88.2", 8789)
    nodes = line["nodes"]
    assert {n: r["addr"] for n, r in nodes.items()} == {"a": "169.254.88.3", "b": "169.254.88.4", "a1": "169.254.88.5",
                                                        "a2": "169.254.88.6"}
    for n, r in nodes.items():
        assert wgkeys.pubkey_of(r["priv"]) == r["pub"], f"открытый ключ зонда {n} не от его закрытого"
    assert len({r["psk"] for r in nodes.values()}) == 4, "PSK зондов совпадают"


def test_keys_are_not_regenerated_and_a_gone_member_loses_its_probe(quo):
    m = quo.reserve_quorum_refresh()
    first = quo.reserve_probes_refresh(m)["nodes"]
    again = quo.reserve_probes_refresh(m)["nodes"]
    assert again == first, "ключи зондов пересозданы без смены состава — конфиги у всех устарели"
    quo.db.set_state("gwlink_caps_2", "cfg1,rsv1")
    m = quo.reserve_quorum_refresh()
    nodes = quo.reserve_probes_refresh(m)["nodes"]
    assert "a2" not in nodes and nodes["a1"] == first["a1"], "снятый узел остался или ключ соседа сменился"


# ── пиры-зонды и второй адрес на awg0 ────────────────────────────────────────

def test_probe_peers_are_on_awg0_each_with_exactly_one_32_from_the_block(quo):
    _ready(quo)
    nodes = quo.reserve_probes()["a"]["nodes"]
    peers = quo.awg_host.peers()
    for n in ("b", "a1", "a2"):
        assert peers.get(nodes[n]["pub"]) == [f"{nodes[n]['addr']}/32"], f"пир-зонд {n} не ровно одним /32: {peers}"
    assert peers[qc.USER1_PUB] == ["10.8.1.2/32"] and peers[qc.USER2_PUB] == ["10.8.1.3/32"], "пользователи тронуты"


def test_the_responder_address_is_its_own_line_right_after_the_users_address(quo):
    _ready(quo)
    assert quo.awg_host.addresses() == ["10.8.1.1/24", "169.254.88.1/26"], quo.awg_host.addresses()
    assert "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26\n" in quo.awg_host.text
    assert ["ip", "addr", "replace", "169.254.88.1/26", "dev", "awg0"] in quo.awg_host.ip


def test_a_second_sync_changes_nothing(quo):
    m = _ready(quo)
    text = quo.awg_host.text
    applied = quo.awg_host.applied
    quo.reserve_probe_peers_sync(m)
    assert quo.awg_host.text == text and quo.awg_host.applied == applied, "повторная сверка переписала awg0"


def test_a_stale_probe_peer_in_the_block_is_removed_and_users_are_kept(quo):
    _ready(quo)
    stale = wgkeys.keypair()[1]
    quo.awg_host.path.write_text(quo.awg_host.text + f"\n[Peer]\nPublicKey = {stale}\nAllowedIPs = 169.254.88.9/32\n",
                                 encoding="utf-8")
    quo.reserve_probe_peers_sync(quo.reserve_quorum_mine())
    peers = quo.awg_host.peers()
    assert stale not in peers, "лишний пир-зонд блока остался на awg0"
    assert qc.USER1_PUB in peers and qc.USER2_PUB in peers


def test_a_removed_member_takes_its_probe_peer_with_it(quo):
    _ready(quo)
    pub = quo.reserve_probes()["a"]["nodes"]["a2"]["pub"]
    quo.db.set_state("gwlink_caps_2", "cfg1,rsv1")
    _ready(quo)
    assert pub not in quo.awg_host.peers()


def test_without_members_every_probe_peer_goes(quo):
    _ready(quo)
    quo.reserve_probe_peers_sync(None)
    assert set(quo.awg_host.peers()) == {qc.USER1_PUB, qc.USER2_PUB}


def test_the_clone_gets_the_probe_of_the_primary_itself(quo):
    """В отъезде лидер линии — бот основы на клоне; сама основа голосует о нём
    зондом `.3` (`awgqar`). Пиры клона — из части VPN, то есть с awg0: без пира
    `.3` на awg0 клон не примет пробу основы, и её голос не считается никогда."""
    _ready(quo)
    own = quo.reserve_probes()["a"]["nodes"]["a"]
    part = vpn.iface_part("awg0", quo.awg_host.text)
    assert part.get("probe_resp") == "169.254.88.2/26"
    assert own["pub"] in {p["pub"] for p in part.get("probes") or []}, \
        "зонд основы к своему клону (.3) не едет на клон — её голос о клоне не дойдёт"


def test_after_the_sync_the_part_vpn_still_takes_the_users_subnet(quo):
    _ready(quo)
    part = vpn.iface_part("awg0", quo.awg_host.text)
    assert part["addr"] == "10.8.1.1/24" and [p["ip"] for p in part["peers"]] == ["10.8.1.2", "10.8.1.3"]


# ── смешанная пара ───────────────────────────────────────────────────────────


def test_with_only_the_second_server_voting_its_probe_peer_stays(quo):
    """Шлюзы потеряли `quo1` — голосует только второй сервер: его зонд к моему
    AWG стоит (v = 1 живёт зондом, счётом и окном кода), пиров-зондов шлюзов нет."""
    _ready(quo)
    for slot in (1, 2):
        quo.db.set_state(f"gwlink_caps_{slot}", "cfg1,rsv1")
    quo.clock.t += 120
    quo.quorum_tick()
    probe = {a for _p, allowed in quo.awg_host.peers().items() if probenet.in_block(allowed) for a in allowed}
    assert f"{probenet.node_addr('a', 'b')}/32" in probe, f"при v = 1 зонда второго сервера на awg0 нет: {probe}"
    gws = {f"{probenet.node_addr('a', n)}/32" for n in ("a1", "a2")}
    assert not probe & gws, f"пиры-зонды шлюзов без quo1 остались: {probe & gws}"


# ── конфиги зондов ───────────────────────────────────────────────────────────

def test_the_probe_conf_to_this_server(quo):
    _ready(quo)
    rec = quo.reserve_probes()["a"]["nodes"]["a1"]
    conf = quo.reserve_probe_conf("a1", "h")
    lines = conf.splitlines()
    assert lines[:3] == ["[Interface]", "Address = 169.254.88.5/32", f"PrivateKey = {rec['priv']}"], lines
    for k in ("Jc", "H4", "I1"):
        assert f"{k} = {qc.OBF[k]}" in lines, f"обфускация зонда не та, что у пользователей ({k})"
    peer = conf.split("[Peer]", 1)[1]
    assert f"PublicKey = {qc.SRV_PUB}" in peer and f"PresharedKey = {rec['psk']}" in peer
    assert "AllowedIPs = 169.254.88.1/32" in peer and f"Endpoint = {qc.OWN_IP}:43125" in peer
    assert "PersistentKeepalive" not in conf and "ListenPort" not in conf


def test_the_probe_conf_to_the_clone_points_at_the_second_server_with_the_same_port(quo):
    _ready(quo)
    peer = quo.reserve_probe_conf("a1", "r").split("[Peer]", 1)[1]
    assert "AllowedIPs = 169.254.88.2/32" in peer and f"Endpoint = {qc.PEER_IP}:43125" in peer
    assert f"PublicKey = {qc.SRV_PUB}" in peer, "у клона тот же ключ awg0 — конфиг к клону с другим ключом"


def test_the_probe_conf_passes_the_agents_check_and_the_scripts_check(quo, tmp_path):
    import subprocess
    from tests.unit.test_quorum_probe_script import _functions
    _ready(quo)
    for target in ("h", "r"):
        conf = quo.reserve_probe_conf("a1", target)
        sec = {"l": "a", "g": 1, "members": ["a", "b", "a1", "a2"], "probes": {"h": conf}}
        assert gwcheck.check_quorum(sec)["probes"]["h"], f"агент отвергнет конфиг зонда ({target})"
        f = tmp_path / f"{target}.conf"
        f.write_text(conf, encoding="utf-8")
        r = subprocess.run(["sh", "-c", _functions() + f'conf_check "{f}"'], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"скрипт зондов отвергнет конфиг ({target}): {r.stderr}\n{conf}"


@pytest.mark.parametrize("broken", ["own", "keys"])
def test_without_an_own_address_or_keys_there_is_no_probe_conf(quo, monkeypatch, broken):
    _ready(quo)
    if broken == "own":
        monkeypatch.setattr(quo, "reserve_own_ip", lambda: "")
        assert quo.reserve_probe_conf("a1", "h") == ""
    else:
        assert quo.reserve_probe_conf("a9", "h") == ""


# ── qprobe второму серверу ───────────────────────────────────────────────────

def test_qprobe_carries_the_second_servers_probe_and_goes_once(quo):
    _ready(quo)
    body = quo.reserve_qprobe_body()
    assert (body["l"], body["g"]) == ("a", 1) and body["conf"] == quo.reserve_probe_conf("b", "h")
    assert "Address = 169.254.88.4/32" in body["conf"]
    quo.reserve_qprobe_sent(body["_digest"])
    assert quo.reserve_qprobe_body() is None, "qprobe повторяется без смены"
    quo.reserve_qprobe_reset()
    assert quo.reserve_qprobe_body() is not None, "новая сессия пары — qprobe не переслан"


def test_qprobe_follows_a_new_generation(quo):
    _ready(quo)
    quo.reserve_qprobe_sent(quo.reserve_qprobe_body()["_digest"])
    quo.db.set_state("gwlink_caps_2", "cfg1,rsv1")
    _ready(quo)
    assert quo.reserve_qprobe_body()["g"] == 2


def _other_side_conf(**over) -> str:
    """Конфиг зонда, который прислала бы основа линии b («Beta») для моего узла b → .68."""
    v = {"addr": "169.254.88.68/32", "resp": "169.254.88.65/32", "extra": ""}
    v.update(over)
    priv, _ = wgkeys.keypair()
    return (f"[Interface]\nAddress = {v['addr']}\nPrivateKey = {priv}\nJc = 4\n{v['extra']}\n[Peer]\n"
            f"PublicKey = {wgkeys.keypair()[1]}\nPresharedKey = {wgkeys.psk()}\nAllowedIPs = {v['resp']}\n"
            f"Endpoint = {qc.PEER_IP}:45001\n")


def test_qprobe_in_stores_my_probe_to_the_other_line(quo):
    conf = _other_side_conf()
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": conf})
    mine = quo.reserve_probes()["mine"]
    assert mine["awgqbh"]["conf"] == conf and mine["awgqbh"]["g"] == 2
    want = quo.reserve_quorum_probe_ifaces()
    assert want["awgqbh"]["resp"] == "169.254.88.65" and want["awgqbh"]["node_addr"] == "169.254.88.68"


def test_qprobe_for_my_own_line_is_ignored(quo):
    quo.reserve_quorum_pair_in("qprobe", {"l": "a", "g": 2, "conf": _other_side_conf()})
    assert not (quo.reserve_probes().get("mine") or {})


@pytest.mark.parametrize("over, why", [
    ({"resp": "169.254.88.65/32, 10.8.1.0/24"}, "два значения AllowedIPs"),
    ({"resp": "169.254.88.64/26"}, "AllowedIPs шире /32"),
    ({"resp": "10.8.1.1/32"}, "ответчик вне блока"),
    ({"addr": "169.254.88.68/26"}, "адрес зонда не /32"),
    ({"extra": "PostUp = id\n"}, "хук"),
    ({"extra": "Table = off\n"}, "Table"),
])
def test_qprobe_with_a_conf_that_does_not_fit_is_refused(quo, over, why):
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf(**over)})
    assert not (quo.reserve_probes().get("mine") or {}), f"конфиг зонда принят: {why}"


# ── раздел quorum документа шлюза ────────────────────────────────────────────

def test_a_member_gateway_gets_the_quorum_section_and_the_agent_accepts_it(quo):
    _ready(quo)
    sec = quo.gwcfg_quorum(_gw(quo, 1))
    assert (sec["l"], sec["g"], sec["members"], sec["t"], sec["port"]) == ("a", 1, ["a", "b", "a1", "a2"], 2, 8789), sec
    assert set(sec["probes"]) == {"h", "r"}
    assert "Address = 169.254.88.5/32" in sec["probes"]["h"] and "AllowedIPs = 169.254.88.2/32" in sec["probes"]["r"]
    parsed = gwcfg.parse({"gen": 1, "hash": "ab" * 8, "quorum": sec})["sections"]["quorum"]
    assert gwcheck.check_quorum(parsed)["l"] == "a", "агент отвергнет раздел, собранный сервером"


def test_a_gateway_outside_the_line_gets_no_section(quo):
    _ready(quo)
    assert quo.gwcfg_quorum(_gw(quo, 3)) is None


def test_the_document_of_a_member_gateway_is_built_with_the_quorum_print(quo):
    """Документ слота в составе собирается с отпечатком раздела quorum: иначе у
    такого шлюза документа нет вовсе."""
    _ready(quo)
    doc = quo.gwcfg_doc(_gw(quo, 1))
    assert doc["prints"]["quorum"], "отпечаток раздела quorum пуст"


# ── свои интерфейсы зондов ───────────────────────────────────────────────────

def test_at_home_only_the_probe_to_the_other_server_stands(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    assert set(quo.reserve_quorum_probe_ifaces()) == {"awgqbh"}


def test_while_away_the_probe_to_my_own_clone_stands_too(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    quo.rsv_update("rsv_out", state="away")
    want = quo.reserve_quorum_probe_ifaces()
    assert set(want) == {"awgqbh", "awgqar"}, want
    assert want["awgqar"]["resp"] == "169.254.88.2" and want["awgqar"]["node_addr"] == "169.254.88.3"
    assert f"Endpoint = {qc.PEER_IP}:43125" in want["awgqar"]["conf"]


def test_probes_apply_raises_the_wanted_and_takes_down_the_rest(quo):
    _ready(quo)
    quo.probe_host.standing = {"awgqzz": "старый"}
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    quo.reserve_probes_apply()
    assert quo.probe_host.downs == ["awgqzz"] and set(quo.probe_host.standing) == {"awgqbh"}


def test_probes_apply_is_not_repeated_without_a_change(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    quo.reserve_probes_apply()
    n = len(quo.probe_host.ups)
    quo.clock.t += 60
    quo.reserve_probes_apply()
    assert len(quo.probe_host.ups) == n, "--up зондов на каждом такте"
    from awgbot.domain.services.reserve.quorum import probes as probesmod
    quo.clock.t += probesmod.APPLY_EVERY_S
    quo.reserve_probes_apply()
    assert len(quo.probe_host.ups) == n + 1


def test_a_new_qprobe_is_applied_on_the_next_tick(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    quo.reserve_probes_apply()
    new = _other_side_conf()
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 3, "conf": new})
    quo.reserve_probes_apply()
    assert quo.probe_host.standing["awgqbh"] == new


# ── уборка ───────────────────────────────────────────────────────────────────

def test_dropping_the_quorum_takes_probe_peers_the_address_and_the_namespace(quo):
    _ready(quo)
    quo.reserve_quorum_drop()
    assert set(quo.awg_host.peers()) == {qc.USER1_PUB, qc.USER2_PUB}, "пиры-зонды пережили удаление пары"
    assert quo.awg_host.addresses() == ["10.8.1.1/24"], "второй адрес пережил удаление пары"
    assert quo.probe_host.wipes == 1


def test_unpair_drops_the_quorum_and_its_keys(quo):
    _ready(quo)
    quo.reserve_unpair("user")
    assert set(quo.awg_host.peers()) == {qc.USER1_PUB, qc.USER2_PUB}
    assert "169.254.88.1/26" not in quo.awg_host.addresses()
    assert quo.probe_host.wipes >= 1
    for k in ("rsv_quorum", "rsv_probes", "rsv_qkeys", "rsv_qlog"):
        assert quo.rsv_get(k) == {}, f"{k} пережил удаление пары"


def test_the_quorum_keys_never_go_into_a_backup():
    import json
    from awgbot.domain.backup import archive
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE server_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for k in ("rsv_quorum", "rsv_probes", "rsv_qkeys", "rsv_qlog"):
        con.execute("INSERT INTO server_state VALUES (?, ?)", (k, json.dumps({"priv": "DUMMY"})))
    con.execute("INSERT INTO server_state VALUES ('rsv_out', '{}')")
    archive._strip_foreign_share(con)
    assert dict(con.execute("SELECT key, value FROM server_state").fetchall()) == {"rsv_out": "{}"}, \
        "ключи кворума доехали в копию"


def test_the_quorum_keys_are_reserve_keys():
    from awgbot.domain.services.reserve import state
    assert {"rsv_quorum", "rsv_probes", "rsv_qkeys", "rsv_qlog"} <= set(state.KEYS)


# ── счётчики клона ───────────────────────────────────────────────────────────

def test_clone_counters_do_not_count_probe_peers(quo, monkeypatch):
    import types
    from awgbot.infra import rsvzone
    from awgbot.util import netns
    monkeypatch.setattr(rsvzone, "state", lambda *a, **k: {"ifaces": "awg0 awg1"})
    dumps = {
        "awg0": "PRIV\tPUB\t43125\toff\n"
                "U1\t(none)\t203.0.113.5:1\t10.8.1.2/32\t1800000000\t1000\t2000\t25\n"
                "Q4\t(none)\t198.51.100.7:1\t169.254.88.4/32\t1800000000\t500000\t600000\toff\n"
                "U2\t(none)\t(none)\t10.8.1.3/32\t0\t0\t0\toff\n",
        "awg1": "PRIV\tPUB\t43126\toff\n"
                "U3\t(none)\t203.0.113.6:1\t10.8.2.2/32\t1800000000\t30\t40\toff\n"
                "Q5\t(none)\t203.0.113.7:1\t169.254.88.5/32\t1800000000\t7\t9\toff\n",
    }

    def run_in(path, argv, timeout=10):
        assert argv[:2] == ["awg", "show"] and argv[3] == "dump", argv
        return types.SimpleNamespace(returncode=0, stdout=dumps[argv[2]].encode())
    monkeypatch.setattr(netns, "run_in", run_in)
    assert quo._clone_counters() == (1030, 2040), "трафик зондов посчитан трафиком пользователей клона"


def test_clone_counters_give_nothing_when_awg_fails(quo, monkeypatch):
    import types
    from awgbot.infra import rsvzone
    from awgbot.util import netns
    monkeypatch.setattr(rsvzone, "state", lambda *a, **k: {"ifaces": "awg0"})
    monkeypatch.setattr(netns, "run_in", lambda path, argv, timeout=10: types.SimpleNamespace(returncode=1, stdout=b""))
    assert quo._clone_counters() == (None, None)
    monkeypatch.setattr(rsvzone, "state", lambda *a, **k: {})
    assert quo._clone_counters() == (None, None)
