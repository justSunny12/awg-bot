"""Общий кадр токенов (util/tokenwire.py) — токен подключения шлюза `AWGJ1.…`
(util/jointoken.py) и приглашение в пару `AWGP1.…` (util/pairtoken.py):
эталонные строки байт в байт, разбор прежних эталонов поле в поле, тексты
отказов по-прежнему свои у каждого вида, имя хоста по одному правилу
(в том числе hostname из hello сопряжения шлюза, gwjoin/pairing.clean_host).

Эталоны сняты кодом до выноса кадра в общий модуль и не пересчитываются.

Цена ошибки: сдвиг хоть одного байта — токен, выпущенный прежним сервером, не
разбирается новым шлюзом (и наоборот), а установщик на bash
(install/awg-bot-install.sh, join_parse) читает слот, выпуск, хост и id по
жёстким смещениям — шлюз ставит не тот выпуск или не находит сервер;
приглашение пары, выпущенное прежней версией, — «повреждено» у второго
сервера; общий текст отказа вместо своего — админ шлюза видит «приглашение
повреждено» при подключении шлюза.
"""
from __future__ import annotations

import pytest

from awgbot.util import jointoken, pairtoken
from awgbot.domain.services.gwjoin.pairing import clean_host

pytestmark = pytest.mark.unit

_J = [
    (dict(slot=2, version="3.4.0", join_id="0123456789abcdef", expires=1_800_000_000,
          host="vpn-server.example.org", link_port=51822, channel_port=8787, gw_addr="10.99.99.6",
          vps_pub=bytes(range(32)), gw_priv=bytes(range(200, 232)), jc=5, jmin=10, jmax=60, s=(20, 30, 40, 50),
          h=((5, 100005), (500000001, 500100001), (1000000001, 1000100001), (1500000001, 1500100001))),
     "AWGJ1.AQIDBAAAASNFZ4mrze9rSdIAFnZwbi1zZXJ2ZXIuZXhhbXBsZS5vcmfKbiJTCmNjBgABAgMEBQYHCAkKCwwNDg8QERITFBUWFx"
     "gZGhscHR4fyMnKy8zNzs_Q0dLT1NXW19jZ2tvc3d7f4OHi4-Tl5ucFCjwUHigyAAAABQABhqUdzWUBHc7roTuaygE7nFChWWgvAVlpta"
     "Ga6hYl"),
    (dict(slot=255, version="4.2.0.7", join_id="fedcba9876543210", expires=2**32 - 1,
          host="198.51.100.7", link_port=65535, channel_port=1, gw_addr="10.99.0.2",
          vps_pub=b"\xff" * 32, gw_priv=b"\x00" * 32, jc=128, jmin=0, jmax=255, s=(0, 255, 1, 254),
          h=((0, 2**32 - 1), (1, 2), (3, 4), (2**31, 2**31 + 1))),
     "AWGJ1.Af8EAgAH_ty6mHZUMhD_____DDE5OC41MS4xMDAuN___AAEKYwAC__________________________________________8AAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAA_wD_Af4AAAAA_____wAAAAEAAAACAAAAAwAAAASAAAAAgAAAAUvZBDU"),
]

_P = [
    (dict(version="4.0.0", pair_id="00112233aabbccdd", expires=1_790_000_000, host="srv-a.example.net",
          pair_port=51830, peer_addr="10.99.98.1", inviter_pub=bytes(range(32)), peer_priv=bytes(range(100, 132)),
          jc=5, jmin=10, jmax=50, s=(20, 30, 40, 50),
          h=((100, 200), (500_000_100, 500_000_900), (1_000_000_100, 1_000_090_000), (1_500_000_100, 1_999_999_999)),
          awg_gen=2),
     "AWGP1.AQQAAAAAESIzqrvM3WqxO4ARc3J2LWEuZXhhbXBsZS5uZXTKdgpjYgEAAQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eH2"
     "RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn-AgYKDBQoyFB4oMgAAAGQAAADIHc1lZB3NaIQ7mspkO5wpkFloL2R3NZP_AqCLVAA"),
    (dict(version="4.1.1.3", pair_id="ffeeddccbbaa9988", expires=0, host="a" * 63, pair_port=1,
          peer_addr="10.99.98.2", inviter_pub=b"\x01" * 32, peer_priv=b"\xfe" * 32,
          jc=0, jmin=255, jmax=1, s=(255, 0, 255, 0), h=((0, 0), (2**32 - 1, 2**32 - 1), (7, 8), (9, 10)), awg_gen=1),
     "AWGP1.AQQBAQP_7t3Mu6qZiAAAAAA_YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYW"
     "FhYWFhYWFhAAEKY2ICAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_v7-_g"
     "D_Af8A_wAAAAAAAAAAAP__________AAAABwAAAAgAAAAJAAAACgEukDjz"),
]


@pytest.mark.parametrize("fields, text", _J, ids=["join-plain", "join-edges"])
def test_a_join_token_is_the_same_bytes_and_reads_back(fields, text):
    tok = jointoken.JoinToken(**fields)
    assert jointoken.encode(tok) == text, "двоичная запись токена подключения сдвинулась"
    assert jointoken.decode(text) == tok
    assert jointoken.find(f"curl … | sudo bash -s -- --join {text} --yes") == text


@pytest.mark.parametrize("fields, text", _P, ids=["pair-plain", "pair-edges"])
def test_an_invitation_is_the_same_bytes_and_reads_back(fields, text):
    tok = pairtoken.PairToken(**fields)
    assert pairtoken.encode(tok) == text, "двоичная запись приглашения сдвинулась"
    assert pairtoken.decode(text) == tok
    assert pairtoken.find(f"Приглашение:\n«{text}»") == text


def test_the_obfuscation_view_is_the_same_for_both_kinds():
    j = jointoken.decode(_J[0][1]).obfuscation()
    p = pairtoken.decode(_P[0][1]).obfuscation()
    assert j == {"Jc": "5", "Jmin": "10", "Jmax": "60", "S1": "20", "S2": "30", "S3": "40", "S4": "50",
                 "H1": "5-100005", "H2": "500000001-500100001", "H3": "1000000001-1000100001",
                 "H4": "1500000001-1500100001"}
    assert p["Jmax"] == "50" and p["H4"] == "1500000100-1999999999"


def test_one_kind_is_not_taken_for_the_other():
    assert jointoken.find(_P[0][1]) == "" and pairtoken.find(_J[0][1]) == ""
    with pytest.raises(jointoken.TokenError, match="это не команда подключения"):
        jointoken.decode(_P[0][1])
    with pytest.raises(pairtoken.TokenError, match="это не приглашение в пару"):
        pairtoken.decode(_J[0][1])


@pytest.mark.parametrize("mod, text, damaged, other", [
    (jointoken, _J[0][1], "команда подключения повреждена", "обнови установщик"),
    (pairtoken, _P[0][1], "приглашение повреждено", "обнови бот")], ids=["join", "pair"])
def test_each_kind_keeps_its_own_refusals(mod, text, damaged, other):
    import base64
    b64 = text[len(mod.PREFIX):]
    flip = "B" if b64[20] != "B" else "C"
    with pytest.raises(mod.TokenError, match=damaged):
        mod.decode(mod.PREFIX + b64[:20] + flip + b64[21:])
    with pytest.raises(mod.TokenError, match=damaged):
        mod.decode(mod.PREFIX + "!" * 50)
    raw = bytearray(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    raw[0] = 2
    body = bytes(raw[:-4])
    import hashlib
    raw = body + hashlib.blake2b(body, digest_size=4).digest()
    with pytest.raises(mod.TokenError, match=other):
        mod.decode(mod.PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("="))


@pytest.mark.parametrize("host", ["", "a" * 64, "vpn_server.example.org", "vpn.example.org\n", "впн.рф"])
def test_a_host_outside_the_rule_is_refused_everywhere(host):
    with pytest.raises(jointoken.TokenError):
        jointoken.encode(jointoken.JoinToken(**{**_J[0][0], "host": host}))
    with pytest.raises(pairtoken.TokenError):
        pairtoken.encode(pairtoken.PairToken(**{**_P[0][0], "host": host}))
    assert clean_host(host) == ""


def test_a_host_within_the_rule_is_kept():
    assert clean_host("gw-1.lan") == "gw-1.lan" and clean_host("a" * 63) == "a" * 63
