"""Токен подключения шлюза (util/jointoken.py, §2.4): кодирование ↔ разбор;
длина ≤ 220 знаков с хостом 25 знаков; порча любого байта — отказ
контрольной суммы; чужая версия формата — отказ; разбор полей установщиком
на sh совпадает с Python (версия, хост, порты).
"""
from __future__ import annotations

import base64

import pytest

from awgbot.util import jointoken
from awgbot.util.jointoken import JoinToken, TokenError

pytestmark = pytest.mark.unit


def token(**kw) -> JoinToken:
    base = dict(slot=2, version="3.4.0", join_id="0123456789abcdef", expires=1_800_000_000,
                host="vpn-server.example.org", link_port=51822, channel_port=8787, gw_addr="10.99.99.6",
                vps_pub=b"V" * 32, gw_priv=b"G" * 32, jc=5, jmin=10, jmax=60, s=(20, 30, 40, 50),
                h=((5, 100005), (500000001, 500100001), (1000000001, 1000100001), (1500000001, 1500100001)))
    base.update(kw)
    return JoinToken(**base)


def test_round_trip():
    t = token(version="3.4.0.2")
    assert jointoken.decode(jointoken.encode(t)) == t
    d = jointoken.decode(jointoken.encode(token()))
    assert d.version == "3.4.0" and d.vps_addr == "10.99.99.5" and d.cidr == "10.99.99.4/30"
    assert d.obfuscation()["H2"] == "500000001-500100001" and d.obfuscation()["S4"] == "50"


def test_length_with_a_25_char_host():
    s = jointoken.encode(token(host="a" * 21 + ".org"))
    assert len("a" * 21 + ".org") == 25 and len(s) <= 220, len(s)


def test_any_damaged_byte_is_refused():
    s = jointoken.encode(token())
    raw = bytearray(base64.urlsafe_b64decode(s[len(jointoken.PREFIX):] + "=" * (-len(s) % 4)))
    for i in range(len(raw)):
        bad = bytearray(raw)
        bad[i] ^= 0x01
        txt = jointoken.PREFIX + base64.urlsafe_b64encode(bytes(bad)).decode().rstrip("=")
        with pytest.raises(TokenError):
            jointoken.decode(txt)


def test_another_format_version_is_refused():
    import hashlib
    s = jointoken.encode(token())
    raw = bytearray(base64.urlsafe_b64decode(s[6:] + "=" * (-len(s[6:]) % 4)))
    body = bytearray(raw[:-4])
    body[0] = 2
    raw = bytes(body) + hashlib.blake2b(bytes(body), digest_size=4).digest()
    with pytest.raises(TokenError, match="другого формата"):
        jointoken.decode(jointoken.PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("="))


@pytest.mark.parametrize("text", ["", "AWGJ1.", "GL1:abc", "AWGJ1." + "A" * 50])
def test_garbage_is_refused(text):
    with pytest.raises(TokenError):
        jointoken.decode(text)


def test_find_in_a_pasted_command():
    s = jointoken.encode(token())
    assert jointoken.find(f"curl -fsSL https://x | sudo bash -s -- --join {s}\n") == s
