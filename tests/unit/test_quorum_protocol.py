"""Протокол кворума резерва (концепт «кворум» §3.3): новые виды канала пары и
гостевого канала (util/pairproto), раздел `quorum` документа конфигурации шлюза
(domain/gwcfg: разбор, проверка, отпечаток), поле `quo` снимка шлюза
(domain/gwsnapshot).

Цена ошибки: вид не с той стороны, принятый каналом, — шлюз изображает
резерв (`qtake`, `qask`) или резерв изображает шлюз (`qview`, `qshare`); поле
не того типа, прошедшее проверку, — домен падает на `int()` и рвёт сессию;
конфиг зонда с `Table`, вторым пиром или адресом вне блока, принятый агентом,
— зонд маршрутизирует больше ответчика; отпечаток раздела, не меняющийся от
поколения или конфига, — сервер считает новый состав доставленным, а шлюз
голосует по старому; отпечаток, плавающий от пробелов, — документ шлётся по
кругу; мусор во взгляде снимка — карточка показывает «видит» по подделке.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwcfg, gwsnapshot
from awgbot.domain.gwcfg import check as gwcheck
from awgbot.domain.gwcfg import prints as gwprints
from awgbot.domain.gwcfg.doc import CfgError
from awgbot.util import pairproto as pp
from awgbot.util import wgkeys

pytestmark = pytest.mark.unit


# ── канал пары и гостевой канал ──────────────────────────────────────────────

PAIR_BODIES = {
    "qmem": {"l": "a", "g": 3, "members": ["a", "b", "a1"]},
    "qmem_ok": {"l": "a", "g": 3},
    "qprobe": {"l": "a", "g": 3, "conf": "[Interface]\n"},
    "qkeys": {"l": "a", "m_gen": 2, "q_gen": 1, "t": 2, "x": 4, "y": "ab" * 16, "qbox": "cd" * 40, "c_gen": 1,
              "cy": "ef" * 16, "cbox": "01" * 40},
    "qkeys_ok": {"l": "a", "m_gen": 1, "q_gen": 1, "c_gen": 1},
    "qview": {"l": "b", "g": 2, "w": "see", "since": "2026-11-02T14:05:00", "e": 12, "i": {"hs": 3}},
    "qstate": {"l": "a", "e": 12, "holder": "self", "r": "lead"},
    "qpause": {"l": "a", "until": "2026-11-02T14:10:00"},
}


@pytest.mark.parametrize("kind", sorted(PAIR_BODIES))
@pytest.mark.parametrize("sender", [pp.INVITER, pp.ACCEPTOR])
def test_every_new_pair_kind_goes_both_ways(kind, sender):
    assert pp.check(pp.PAIR_PROTO, kind, PAIR_BODIES[kind], sender) == PAIR_BODIES[kind]


def test_qview_over_the_pair_link_may_come_without_the_info():
    body = {k: v for k, v in PAIR_BODIES["qview"].items() if k != "i"}
    assert pp.check(pp.PAIR_PROTO, "qview", body, pp.INVITER) == body


@pytest.mark.parametrize("kind, field, bad", [
    ("qmem", "g", "3"), ("qmem", "g", True), ("qmem", "members", "a,b"), ("qview", "w", 1),
    ("qview", "e", 1.5), ("qprobe", "conf", "x" * 4097), ("qmem", "l", "ab"), ("qstate", "holder", "x" * 9),
])
def test_a_pair_kind_with_a_field_of_the_wrong_kind_is_refused(kind, field, bad):
    with pytest.raises(pp.ProtocolError):
        pp.check(pp.PAIR_PROTO, kind, {**PAIR_BODIES[kind], field: bad}, pp.INVITER)


@pytest.mark.parametrize("kind", ["qmem", "qprobe", "qview", "qstate"])
def test_a_pair_kind_without_a_required_field_is_refused(kind):
    body = dict(PAIR_BODIES[kind])
    body.pop("l")
    with pytest.raises(pp.ProtocolError, match="нет поля"):
        pp.check(pp.PAIR_PROTO, kind, body, pp.INVITER)


GUEST = {
    "qview": (pp.GATEWAY, {"l": "a", "g": 1, "w": "uns", "since": "2026-11-02T14:05:00", "e": 0}),
    "qshare": (pp.GATEWAY, {"l": "a", "q_gen": 1, "x": 5, "y": "ab" * 16}),
    "qask": (pp.RESERVE, {"l": "a", "g": 1}),
    "qtake": (pp.RESERVE, {"l": "a", "e": 12}),
    "qrel": (pp.RESERVE, {"l": "a", "e": 12}),
    "qneed": (pp.RESERVE, {"l": "a", "q_gen": 1, "since": "2026-11-02T14:05:00"}),
    "qpause": (pp.RESERVE, {"l": "a", "until": "2026-11-02T14:10:00"}),
    "qstate": (pp.RESERVE, {"l": "a", "e": 12, "holder": "peer", "r": "follow"}),
}


@pytest.mark.parametrize("kind", sorted(GUEST))
def test_every_new_guest_kind_comes_only_from_its_own_side(kind):
    side, body = GUEST[kind]
    assert pp.check(pp.GUEST_PROTO, kind, body, side) == body
    other = pp.RESERVE if side == pp.GATEWAY else pp.GATEWAY
    with pytest.raises(pp.ProtocolError, match="не с той стороны"):
        pp.check(pp.GUEST_PROTO, kind, body, other)


def test_state_of_the_guest_channel_takes_the_epoch_and_still_works_without_it():
    base = {"state": "away", "move": 3, "reason": ""}
    assert pp.check(pp.GUEST_PROTO, "state", base, pp.RESERVE) == base, "state без e (прежний резерв) отвергнут"
    assert pp.check(pp.GUEST_PROTO, "state", {**base, "e": 12}, pp.RESERVE)["e"] == 12
    with pytest.raises(pp.ProtocolError):
        pp.check(pp.GUEST_PROTO, "state", {**base, "e": "12"}, pp.RESERVE)


def test_st_takes_the_quorum_part_and_works_without_it():
    """Поле `quo` в `st` — словарь и необязательное (st прежнего выпуска его не несёт)."""
    spec = pp.PAIR_KINDS["st"][1]
    assert spec["quo"][0] is dict and "quo" in pp.OPTIONAL["st"]


# ── раздел quorum документа ──────────────────────────────────────────────────

def _conf(*, addr="169.254.88.5/32", resp="169.254.88.1/32", extra_if="", extra_peer="", endpoint=True, peers=1) -> str:
    priv, _ = wgkeys.keypair()
    _, pub = wgkeys.keypair()
    peer = (f"\n[Peer]\nPublicKey = {pub}\nPresharedKey = {wgkeys.psk()}\nAllowedIPs = {resp}\n"
            + ("Endpoint = 203.0.113.10:43125\n" if endpoint else "") + extra_peer)
    return f"[Interface]\nAddress = {addr}\nPrivateKey = {priv}\nJc = 5\n{extra_if}" + peer * peers


def _sec(**over) -> dict:
    sec = {"l": "a", "g": 4, "members": ["a", "b", "a1", "a2"], "t": 2, "port": 8789, "e": 12,
           "probes": {"h": _conf(), "r": _conf(resp="169.254.88.2/32")}, "point": {}}
    sec.update(over)
    return sec


def test_a_proper_quorum_section_passes_and_keeps_its_fields():
    out = gwcheck.check_quorum(_sec())
    assert (out["l"], out["g"], out["members"], out["t"], out["port"], out["e"]) == ("a", 4, ["a", "b", "a1", "a2"], 2, 8789, 12)
    assert set(out["probes"]) == {"h", "r"} and "AllowedIPs = 169.254.88.1/32" in out["probes"]["h"]
    assert out["point"] == {}


def test_a_quorum_section_with_a_point_keeps_it():
    out = gwcheck.check_quorum(_sec(point={"x": 5, "y": "ab" * 16, "q_gen": 1}))
    assert out["point"] == {"x": 5, "y": "ab" * 16, "q_gen": 1}


@pytest.mark.parametrize("over, why", [
    ({"l": "c"}, "линия"), ({"l": ""}, "линия"),
    ({"members": []}, "состав"), ({"members": ["a", "b", "c1"]}, "состав"), ({"members": ["a", "b", "a100"]}, "состав"),
    ({"probes": {"r": _conf(resp="169.254.88.2/32")}}, "нет конфига к серверу"),
    ({"probes": {"h": _conf(resp="10.8.1.1/32")}}, "ответчик вне блока"),
    ({"probes": {"h": _conf(addr="10.8.1.5/32")}}, "адрес зонда вне блока"),
    ({"probes": {"h": _conf(resp="169.254.88.1/32, 10.8.1.0/24")}}, "два значения AllowedIPs"),
    ({"probes": {"h": _conf(resp="169.254.88.0/26")}}, "AllowedIPs шире /32"),
    ({"probes": {"h": _conf(extra_if="Table = off\n")}}, "Table"),
    ({"probes": {"h": _conf(extra_if="PostUp = id\n")}}, "хук"),
    ({"probes": {"h": _conf(peers=2)}}, "второй пир"),
    ({"probes": {"h": _conf(endpoint=False)}}, "нет Endpoint"),
    ({"probes": {"h": "мусор без секций"}}, "не конфиг"),
    ({"point": {"x": 1, "y": "ab" * 16, "q_gen": 1}}, "точка x=1"),
    ({"point": {"x": 255, "y": "ab" * 16, "q_gen": 1}}, "точка x=255 (это код)"),
    ({"point": {"x": 5, "y": "zz" * 16, "q_gen": 1}}, "точка не hex"),
    ({"point": {"x": 5, "y": "ab" * 16, "q_gen": 0}}, "точка без поколения"),
    ({"port": 70000}, "порт"),
])
def test_a_quorum_section_that_does_not_fit_is_refused(over, why):
    with pytest.raises(CfgError):
        gwcheck.check_quorum(_sec(**over))
        pytest.fail(f"раздел quorum принят: {why}")


def test_parse_cuts_the_section_to_its_fields_and_sizes():
    msg = {"gen": 1, "hash": "ab" * 8, "quorum": {**_sec(), "l": "abc", "members": ["a", "b", "a1toolong", 7] + ["a1"] * 100,
                                                    "probes": {"h": "x" * 20000, "r": "", "evil": "PostUp"},
                                                    "point": {"x": "5", "y": "не hex", "q_gen": 1}, "junk": "x"}}
    sec = gwcfg.parse(msg)["sections"]["quorum"]
    assert sec["l"] == "a", "линия не обрезана до одной буквы"
    assert len(sec["members"]) == 64 and "a1to" in sec["members"] and 7 not in sec["members"]
    assert set(sec["probes"]) == {"h"} and len(sec["probes"]["h"]) == 16 * 1024, "лишний конфиг или без предела длины"
    assert sec["point"] == {"x": 5, "y": "", "q_gen": 1}
    assert "junk" not in sec


def test_parse_without_the_section_has_none():
    assert "quorum" not in gwcfg.parse({"gen": 1, "hash": "ab" * 8})["sections"]
    assert "quorum" not in gwcfg.parse({"gen": 1, "hash": "ab" * 8, "quorum": "строка"})["sections"]


def test_check_doc_routes_the_quorum_section_to_its_check():
    from tests.unit.test_gwcfg import ENV
    env = gwcfg.parse({"gen": 1, "hash": "ab" * 8, "env": ENV})["sections"]["env"]
    parsed = {"gen": 1, "hash": "ab" * 8, "sections": {"env": env, "quorum": _sec(probes={"h": _conf(extra_if="Table = off\n")})}}
    with pytest.raises(CfgError, match="Table"):
        gwcheck.check_doc(parsed, link_cidr="10.99.99.0/30")
    parsed["sections"]["quorum"] = _sec()
    assert gwcheck.check_doc(parsed, link_cidr="10.99.99.0/30")["quorum"]["l"] == "a"


def test_the_quorum_print_is_stable_and_follows_what_matters():
    sec = gwcheck.check_quorum(_sec())
    p = gwprints.quorum_print(sec)
    assert len(p) == gwcfg.PRINT_LEN and p == gwprints.quorum_print(dict(sec)), "отпечаток плавает"
    spaced = {**sec, "probes": {k: v.replace(" = ", "=") for k, v in sec["probes"].items()}}
    assert gwprints.quorum_print(spaced) == p, "отпечаток зависит от пробелов в конфиге — документ по кругу"
    for over in ({"g": 5}, {"members": ["a", "b", "a1"]}, {"t": 3}, {"port": 9911}, {"e": 13},
                 {"point": {"x": 5, "y": "ab" * 16, "q_gen": 1}},
                 {"probes": {**sec["probes"], "h": gwcheck.check_quorum(_sec())["probes"]["h"]}},
                 {"probes": {"h": sec["probes"]["h"]}}):
        assert gwprints.quorum_print({**sec, **over}) != p, f"отпечаток не заметил смены: {over}"


def test_without_a_section_the_print_is_empty():
    assert gwprints.quorum_print(None) == "" and gwprints.quorum_print({}) == ""
    assert gwprints.quorum_print({"l": "a", "probes": {"h": "мусор"}}) == "", "битый конфиг дал отпечаток"


def test_the_package_exports_what_the_roles_call():
    """Сервер (gwchannel/cfg) и агент (gateway/reserve/quorum) зовут
    `gwcfg.quorum_print`: без него отпечатки документа шлюза в составе не
    считаются вовсе."""
    assert callable(getattr(gwcfg, "quorum_print", None)), "gwcfg.quorum_print нет в пакете"
    assert callable(getattr(gwcfg, "check_quorum", None)), "gwcfg.check_quorum нет в пакете"


def test_the_human_name_of_the_section():
    assert "quorum" in gwcfg.SECTIONS and gwcfg.human("quorum")


# ── снимок ───────────────────────────────────────────────────────────────────

def test_the_snapshot_sections_are_the_document_sections():
    assert set(gwsnapshot._CFG_SECTIONS) == set(gwcfg.SECTIONS), "отпечаток раздела не доедет в снимке"


def test_the_snapshot_quo_is_cleaned():
    out = gwsnapshot.sanitize({"quo": {"w": "see", "since": "2026-11-02T14:05:00", "g": 4, "e": 12, "evil": "x"}})
    assert out["quo"] == {"w": "see", "since": "2026-11-02T14:05:00", "g": 4, "e": 12, "q": 0}, \
        "чужое поле прошло чистку или нет поколения точки (q = 0 — точки не было)"


@pytest.mark.parametrize("raw, want", [
    ({"w": "lead"}, {"w": "", "since": "", "g": 0, "e": 0, "q": 0}),
    ({"w": "see", "g": "4; rm", "e": -3}, {"w": "see", "since": "", "g": 0, "e": 0, "q": 0}),
    ({"w": "uns", "since": "x" * 100}, {"w": "uns", "since": "x" * 40, "g": 0, "e": 0, "q": 0}),
])
def test_garbage_in_the_snapshot_quo_never_becomes_a_view(raw, want):
    assert gwsnapshot.sanitize({"quo": raw})["quo"] == want


def test_a_cleared_snapshot_quo_stays_cleared():
    assert gwsnapshot.sanitize({"quo": None})["quo"] is None
    assert "quo" not in gwsnapshot.sanitize({})
