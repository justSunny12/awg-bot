"""Сообщения переезда и возврата в словаре канала пары (util/pairproto.py):
`go`, `up`, `down`, `released`, `taken`, `blocks`, `back_req`, `back`,
`relay`, `wiped`, `evict` и необязательные признаки здоровья в `st`.

Цена ошибки: `st` без необязательных полей, который словарь отвергает, —
сессия с резервом старого выпуска рвётся при каждом сообщении и пара
«недоступна»; `st` с признаком не того типа, который принимается, —
кнопка возврата у резерва зажигается от строки; реплика длиннее потолка
части — сессия падает на возврате, когда клиенты уже у резерва.
"""
from __future__ import annotations

import pytest

from awgbot.util import pairproto as pp
from awgbot.util.gwlink import ProtocolError

pytestmark = pytest.mark.unit

KINDS = {
    "go": {"k": "ab" * 32, "gen": 7, "kind": "button"},
    "up": {},
    "down": {"reason": "порт занят"},
    "released": {"gen": 7, "i": 0, "n": 1, "blob": "00ff"},
    "taken": {},
    "blocks": {"addrs": ["10.8.1.2"]},
    "back_req": {},
    "back": {},
    "relay": {"until": "2026-10-28T00:00:00+05:00"},
    "wiped": {},
    "evict": {},
}


@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("side", [pp.INVITER, pp.ACCEPTOR])
def test_every_move_message_passes_from_either_side(kind, side):
    assert pp.check(pp.PAIR_PROTO, kind, dict(KINDS[kind]), side) == KINDS[kind]


@pytest.mark.parametrize("kind", [k for k, b in sorted(KINDS.items()) if b])
def test_a_move_message_without_its_field_is_a_protocol_error(kind):
    for name in KINDS[kind]:
        body = {k: v for k, v in KINDS[kind].items() if k != name}
        with pytest.raises(ProtocolError):
            pp.check(pp.PAIR_PROTO, kind, body, pp.INVITER)


def test_the_key_in_go_has_a_ceiling_so_a_foreign_string_cannot_pose_as_the_package_key():
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "go", {"k": "a" * 65, "gen": 1, "kind": "button"}, pp.INVITER)


def test_a_bool_is_not_a_generation():
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "go", {"k": "ab" * 32, "gen": True, "kind": "button"}, pp.INVITER)
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "released", {"gen": 7, "i": False, "n": 1, "blob": ""}, pp.INVITER)


def test_the_blocks_list_and_the_down_reason_have_ceilings():
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "blocks", {"addrs": ["10.8.1.2"] * 4097}, pp.INVITER)
    pp.check(pp.PAIR_PROTO, "blocks", {"addrs": ["10.8.1.2"] * 4096}, pp.INVITER)
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "down", {"reason": "x" * (pp.MAX_REASON + 1)}, pp.INVITER)


def test_a_replica_part_longer_than_the_ceiling_is_refused():
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "released", {"gen": 7, "i": 0, "n": 1, "blob": "0" * (pp.MAX_BLOB + 1)}, pp.INVITER)


def test_a_replica_is_cut_into_parts_within_the_ceiling_and_glues_back():
    blob = "ab" * (pp.CHUNK_BYTES * 2 + 5)
    parts = pp.chunks(blob)
    assert len(parts) == 3 and all(len(p) <= pp.MAX_BLOB for p in parts) and "".join(parts) == blob
    assert pp.chunks("") == [""], "пустая реплика — одна пустая часть, иначе released не уйдёт вовсе"


def _st(**over) -> dict:
    body = {"ready": True, "name": "Бета", "version": "4.0.0", "caps": ["pair1"], "gen": 0, "ports": [45871],
            "self": "198.51.100.7", "out": "home", "in": "idle", "pkg": {}, "voters": [], "admin_id": 1,
            "admin_opened": False, "gw": True}
    body.update(over)
    return body


def test_an_st_without_the_health_flags_still_passes_because_an_older_peer_does_not_send_them():
    out = pp.check(pp.PAIR_PROTO, "st", _st(), pp.INVITER)
    assert "healthy" not in out and "can_return" not in out


def test_an_st_with_the_health_flags_carries_them():
    out = pp.check(pp.PAIR_PROTO, "st", _st(healthy=True, can_return=False), pp.ACCEPTOR)
    assert out["healthy"] is True and out["can_return"] is False


@pytest.mark.parametrize("field", ["healthy", "can_return"])
@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_health_flag_of_a_wrong_type_is_refused(field, value):
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "st", _st(**{field: value}), pp.INVITER)


def test_the_optional_fields_are_only_those_three_and_only_for_st():
    assert pp.OPTIONAL == {"st": frozenset({"healthy", "can_return", "in_kind"})}
    body = _st()
    del body["admin_id"]
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "st", body, pp.INVITER)


def test_in_kind_is_optional_and_carries_the_kind_of_the_move():
    assert "in_kind" not in pp.check(pp.PAIR_PROTO, "st", _st(), pp.INVITER)
    assert pp.check(pp.PAIR_PROTO, "st", _st(in_kind="dead"), pp.INVITER)["in_kind"] == "dead"
    with pytest.raises(ProtocolError):
        pp.check(pp.PAIR_PROTO, "st", _st(in_kind=5), pp.INVITER)
