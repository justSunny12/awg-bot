"""I1 линка: значение — строка
`#I1` клиентского интерфейса сервера как есть, тем же разбором, что у
генератора конфигов (`parsers._extract_param`); у всех слотов одно; строки
нет или она пустая — линк без I1, уведомление «нет I1»; сменили клиентский I1 — у
каждого слота с другим I1 назначается автоматическая смена ключей.
"""
from __future__ import annotations

import pytest

from awgbot.infra import awg
from awgbot.infra.awg import parsers
from tests.unit import test_gwlink_rekey as _vps
from tests.unit.test_gwlink_rekey import ADMIN

pytestmark = pytest.mark.unit

slot, vps = _vps.slot, _vps.vps

I1 = "<b 0xc70000000108ce1bc7e1d8a3b4a9><r 16><t><rc 8>"
AWG1 = f"""[Interface]
PrivateKey = DUMMY-SERVER-PRIVATE-KEY=
ListenPort = 43125
Jc = 4
H4 = 4
#I1 = {I1}
#I2 =
"""


def _client(monkeypatch, text: str) -> None:
    """Клиентский интерфейс сервера с этим текстом — разбор настоящий."""
    params = parsers.parse_interface_params(text)
    base = getattr(awg, "_i1_test_base", None) or awg.read_server_params
    monkeypatch.setattr(awg, "_i1_test_base", base, raising=False)
    monkeypatch.setattr(awg, "read_server_params", lambda force=False, iface=None: {
        **base(), "obfuscation": {k: params.get(k, "") for k in parsers._OBFUSCATION_KEYS}})


def _caps(services, n: int) -> None:
    from awgbot.util import gwlink
    services.gwlink_session_opened(n, "3.4.0", gwlink.PROTO, caps=("cfg1", "confirm1", "upd1", "rekey2"))


@pytest.mark.parametrize("line", [f"#I1 = {I1}", f"I1 = {I1}", f"# I1 = {I1}"])
def test_the_value_is_the_client_line_byte_for_byte(services, monkeypatch, line):
    _client(monkeypatch, AWG1.replace(f"#I1 = {I1}", line))
    assert services.gateway_client_i1() == (True, I1)


@pytest.mark.parametrize("text", [AWG1.replace(f"#I1 = {I1}\n", ""), AWG1.replace(f"#I1 = {I1}", "#I1 =")])
def test_no_line_or_an_empty_one_is_no_i1(services, monkeypatch, text):
    _client(monkeypatch, text)
    assert services.gateway_client_i1() == (True, "")


def test_an_unreadable_interface_is_not_no_i1(services, monkeypatch):
    def boom(**_):
        raise awg.AwgError("нет интерфейса")
    monkeypatch.setattr(awg, "read_server_params", boom)
    assert services.gateway_client_i1() == (False, "")


def test_every_slot_gets_the_same_value(services, slot, vps, monkeypatch):
    _client(monkeypatch, AWG1)
    _vps._second_slot(services)
    services.gateway_rekey_stage(1)
    env = [env for mode, env in vps.runs if mode == "--rekey-stage"][0]
    gws = services.db.gateways()
    assert env["LINK_I1"] == I1
    assert {services._rekey_i1_for(gw, "i1") for gw in gws} == {I1}, "у слотов разный I1"


def test_without_a_client_i1_the_link_goes_without_one_and_the_card_says_so(services, slot, vps, monkeypatch):
    _client(monkeypatch, AWG1.replace(f"#I1 = {I1}\n", ""))
    services.gateway_rekey_stage(1)
    env = [env for mode, env in vps.runs if mode == "--rekey-stage"][0]
    assert "LINK_I1" not in env
    from awgbot.domain.services.gateway_link.rekey_i1 import T_L2
    assert services.gateway_i1_line(1) is None, "«нет I1» — постоянной строкой карточки"
    monkeypatch.setattr(services, "gateway_link_i1", lambda gw: (True, ""))
    _, notes = services.gateway_i1_tick()
    assert [n.text for n in notes] == [T_L2] and notes[0].silent and notes[0].tg_id == ADMIN
    assert services.gateway_i1_tick()[1] == [], "«нет I1» сказано второй раз"


def test_a_changed_client_i1_plans_a_rekey_for_every_slot_with_another(services, slot, vps, monkeypatch):
    _client(monkeypatch, AWG1)
    _vps._second_slot(services)
    _caps(services, 1)
    _caps(services, 2)
    have = {1: I1, 2: "<b 0x01>"}
    monkeypatch.setattr(services, "gateway_link_i1", lambda gw: (True, have[gw.id]))
    services.gateway_i1_tick()
    assert services.gateway_i1_plan(1) == {} and services.gateway_i1_plan(2).get("want") == I1
    _client(monkeypatch, AWG1.replace(I1, "<b 0x02>"))       # клиентский I1 сменили на хосте
    services.gateway_i1_tick()
    assert services.gateway_i1_plan(1).get("want") == "<b 0x02>" and services.gateway_i1_plan(2).get("want") == "<b 0x02>"
