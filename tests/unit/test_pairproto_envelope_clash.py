"""Поля словарей каналов не должны совпадать с ключами конверта канала (`gwlink.pack`:
`t` — вид сообщения, `seq`, `ts`, `_` — добивка): такое поле тело теряло бы или
подменяло вид. Стенд нашёл это у `qkeys` (порог `t`): слово не проходило проверку
словаря, и кворум на настоящей паре не включался никогда."""
from __future__ import annotations

import pytest

from awgbot.util import gwlink, pairproto as pp

ENVELOPE = {"t", "seq", "ts", "_"}


def _dicts():
    for name in dir(pp):
        obj = getattr(pp, name)
        if isinstance(obj, dict) and obj and all(isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], dict)
                                                 for v in obj.values()):
            yield name, obj


def test_the_dictionaries_are_found():
    assert {n for n, _ in _dicts()} >= {"PAIR_KINDS"}


@pytest.mark.parametrize("name, kinds", list(_dicts()))
def test_no_body_field_is_named_like_an_envelope_key(name, kinds):
    clashes = [(kind, f) for kind, (_who, fields) in kinds.items() for f in fields if f in ENVELOPE]
    assert not clashes, f"{name}: поля тела совпадают с ключами конверта: {clashes}"


def test_qkeys_survives_the_envelope_round_trip():
    key = b"k" * 32
    body = {"l": "a", "m_gen": 2, "q_gen": 1, "thr": 2, "x": 4, "y": "ab" * 16, "qbox": "cd" * 56, "c_gen": 1,
            "cy": "ab" * 16, "cbox": "cd" * 56}
    line = gwlink.pack(key, "qkeys", body, seq=1, nonce=b"n" * 16)
    data = gwlink.unpack(key, line, nonce=b"n" * 16)
    assert data["t"] == "qkeys"
    data = {k: v for k, v in data.items() if k not in ENVELOPE}
    assert pp.check(pp.PAIR_PROTO, "qkeys", data, pp.INVITER)["thr"] == 2
