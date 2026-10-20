"""Словарь сообщений канала пары и гостевого канала (util/pairproto.py):
виды, кто их шлёт, обязательные поля, типы и потолки; нарезка шифротекста на
части; ключи подписи обоих каналов.

На том конце канала — только наш же код, поэтому любое расхождение со
словарём рвёт сессию (`ProtocolError`), а не «принимается как получится».

Цена ошибки: вид, принятый не с той стороны, — резерв, который сам себе
присылает `go` от имени основы; `True` вместо числа поколения (в Python
bool — это int) — поколение «1» у доли, которая к нему не относится; поле
длиннее потолка — память процесса под чужую строку; пакет, который не
режется на части в пределах потолка, — резерв без пакета молча; одинаковые
ключи двух каналов — подпись гостевого канала годится для канала пары.
"""
from __future__ import annotations

import base64

import pytest

from awgbot.util import pairproto as pp
from awgbot.util.gwlink import ProtocolError

pytestmark = pytest.mark.unit

PRIV = base64.b64encode(bytes(range(32))).decode()
PSK = base64.b64encode(bytes(range(32, 64))).decode()


def _hello(**over) -> dict:
    body = {"pair": "00112233aabbccdd", "eph": "ab" * 32, "name": "Сервер 2", "version": "4.0.0", "caps": ["rsv1"],
            "gen": 2, "ports": [51820], "seen": "srv-b.example.net", "bot": "srv_b_bot", "gw": True,
            "nonce": "n" * 32}
    body.update(over)
    return body


def test_a_valid_message_passes_and_comes_back_without_envelope_fields():
    body = _hello(t=123, s="подпись", extra="лишнее")
    out = pp.check(pp.PAIR_PROTO, "hello", body, pp.ACCEPTOR)
    assert out == _hello(), "служебные поля конверта и лишние поля должны отсечься"


def test_an_unknown_kind_breaks_the_session():
    with pytest.raises(ProtocolError, match="неизвестный вид"):
        pp.check(pp.PAIR_PROTO, "rm_rf", {}, pp.INVITER)


@pytest.mark.parametrize("proto, kind, body, sender", [
    (pp.PAIR_PROTO, "hello", _hello(), pp.INVITER),               # hello шлёт только принимающий
    (pp.PAIR_PROTO, "pair_ok", {"sealed": "x"}, pp.ACCEPTOR),      # pair_ok — только пригласивший
    (pp.PAIR_PROTO, "ping", {"n": "1"}, pp.RESERVE),               # стороны другого канала
    (pp.GUEST_PROTO, "view", {"seen": "", "voters": [], "in": "", "pkg_gen": 1}, pp.GATEWAY),
    (pp.GUEST_PROTO, "share", {"share": "x", "m_gen": 1}, pp.RESERVE),   # долю шлёт только шлюз
    (pp.GUEST_PROTO, "ping", {"n": "1"}, pp.BOTH),
])
def test_a_kind_from_the_wrong_side_breaks_the_session(proto, kind, body, sender):
    with pytest.raises(ProtocolError, match="не с той стороны"):
        pp.check(proto, kind, body, sender)


def test_kinds_for_both_sides_are_accepted_from_either_side():
    for sender in (pp.INVITER, pp.ACCEPTOR):
        assert pp.check(pp.PAIR_PROTO, "ping", {"n": "7"}, sender) == {"n": "7"}
        assert pp.check(pp.PAIR_PROTO, "link_ack", {}, sender) == {}


def test_a_missing_field_breaks_the_session():
    body = _hello()
    del body["gen"]
    with pytest.raises(ProtocolError, match="нет поля 'gen'"):
        pp.check(pp.PAIR_PROTO, "hello", body, pp.ACCEPTOR)


@pytest.mark.parametrize("field, value", [
    ("gen", True),          # bool — не число поколения
    ("gen", "2"),
    ("gw", 1),              # число — не флаг
    ("caps", "rsv1"),       # строка — не список
    ("name", 5),
    ("ports", None),
])
def test_a_field_of_the_wrong_type_breaks_the_session(field, value):
    with pytest.raises(ProtocolError, match="не того вида"):
        pp.check(pp.PAIR_PROTO, "hello", _hello(**{field: value}), pp.ACCEPTOR)


@pytest.mark.parametrize("field, value", [
    ("name", "x" * (pp.MAX_NAME + 1)),
    ("caps", ["c"] * 17),
    ("ports", [1, 2, 3, 4, 5]),
    ("seen", "h" * (pp.MAX_HOST + 1)),
])
def test_a_field_longer_than_its_ceiling_breaks_the_session(field, value):
    with pytest.raises(ProtocolError, match="длиннее предела"):
        pp.check(pp.PAIR_PROTO, "hello", _hello(**{field: value}), pp.ACCEPTOR)


def test_a_field_exactly_at_the_ceiling_passes():
    out = pp.check(pp.PAIR_PROTO, "hello", _hello(name="x" * pp.MAX_NAME, ports=[1, 2, 3, 4]), pp.ACCEPTOR)
    assert len(out["name"]) == pp.MAX_NAME


def test_the_guest_channel_checks_its_own_dictionary():
    out = pp.check(pp.GUEST_PROTO, "share", {"share": "ab" * 16, "m_gen": 3}, pp.GATEWAY)
    assert out == {"share": "ab" * 16, "m_gen": 3}
    with pytest.raises(ProtocolError):
        pp.check(pp.GUEST_PROTO, "pkg", {}, pp.GATEWAY)          # вид канала пары в гостевом — неизвестен
    with pytest.raises(ValueError):
        pp.check("pair9", "ping", {"n": "1"}, pp.INVITER)


# ── нарезка ──────────────────────────────────────────────────────────────────

def test_a_blob_is_cut_into_parts_that_join_back():
    blob = "ab" * (pp.MAX_BLOB + 7)
    parts = pp.chunks(blob)
    assert "".join(parts) == blob and all(len(p) <= pp.MAX_BLOB for p in parts) and len(parts) == 3
    assert pp.chunks("") == [""], "пустое тело — одна пустая часть, а не ни одной"


def test_the_package_ceiling_fits_into_the_parts_and_one_more_part_does_not():
    """Пакет 16 МБ (32 М знаков hex) укладывается; больше 256 частей —
    отказ, а не бесконечная отправка."""
    assert len(pp.chunks("0" * (32 * 1024 * 1024))) <= pp.MAX_CHUNKS
    assert len(pp.chunks("0" * (pp.MAX_CHUNKS * pp.MAX_BLOB))) == pp.MAX_CHUNKS
    with pytest.raises(ProtocolError):
        pp.chunks("0" * (pp.MAX_CHUNKS * pp.MAX_BLOB + 1))


# ── ключи подписи ────────────────────────────────────────────────────────────

def test_the_two_channels_sign_with_different_keys_from_the_same_secret():
    assert pp.pair_key(PRIV) != pp.guest_key(PRIV), "домены каналов не разведены"
    assert pp.pair_key(PRIV) == pp.pair_key(PRIV) and len(pp.pair_key(PRIV)) == 32
    assert pp.pair_key(PRIV) != pp.pair_key(PSK) and pp.guest_key(PSK) != pp.guest_key(PRIV)
