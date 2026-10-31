"""Отпечаток главного ключа `M` рядом с номером поколения (util/rsvkeys.m_print,
quorum/keys.py, opening.py, package.py, канал пары): после восстановления основы из
копии `rsv_qkeys` в копию не идут — ящик кода создаётся заново, `M` новое, а номера
`m_gen`/`c_gen`/`q_gen` идут заново и совпадают с выданными до копии. Сверки по
одному номеру принимали чужое поколение за своё:

- резерв клал ящики нового `M` поверх ящиков пакета под тем же номером (без
  `peer_prev`) и отдавал их процессу открытия под пакет прежнего `M` — код и точки
  «подходили» по номеру, а ящик пакета не открывался;
- пакет нового `M` при ящиках прежнего под тем же номером считался готовым к
  открытию — отказ всплывал уже в аварии, а не строкой «данных для переезда нет»;
- основа принимала `qkeys_ok` о чужом `M` того же номера за подтверждение своих ключей.

Совместимость: поле `mf` необязательное (pairproto.OPTIONAL) в `qkeys`, `qkeys_ok` и
`pkg`; прежняя сторона его не шлёт и не возвращает — сверка по номеру, как раньше.
Заголовок пакета (seal) не меняется: прежний резерв читает новые пакеты, новый —
прежние; отпечаток пакета резерв берёт из слова `pkg`, пришедшего вместе с ним.

Миры: линия «Alpha» (`line`, tests/integration/_reserve_quorum.py) — сторона основы;
резерв с пакетом «Beta» (`box`, tests/integration/_reserve_vote_common.py).

Цена ошибки: переезд по коду или по кворуму, решённый вовремя, не открывает пакет —
пользователи без VPN, хотя всё «было на месте»; основа считает ключи у второго
сервера подтверждёнными и не шлёт их, а у него ящики чужого `M`.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.util import pairproto, rsvkeys
from awgbot.util.rsvpkg import seal

from tests.integration import _reserve_common as rc
from tests.integration._reserve_quorum import gens, line
from tests.integration._reserve_vote_common import LINE, M_GEN, Q_GEN, QBOX, RSV_Y, box

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["line", "box"]

M_A = bytes([1]) * rsvkeys.M_LEN                    # `M` до восстановления из копии
M_B = bytes([2]) * rsvkeys.M_LEN                    # `M` после — под тем же номером
MF_A, MF_B = rsvkeys.m_print(M_A), rsvkeys.m_print(M_B)


# ── отпечаток ───────────────────────────────────────────────────────────────

def test_the_print_is_eight_bytes_of_hash_and_tells_two_keys_apart():
    assert len(MF_A) == 16 and rsvkeys.m_print_of(MF_A) == MF_A, MF_A
    assert MF_A != MF_B, "разные M — один отпечаток"
    assert rsvkeys.m_print(M_A) == MF_A, "отпечаток не постоянен"
    assert M_A.hex() not in MF_A and M_A.hex()[:16] != MF_A, "отпечаток — кусок самого M"
    with pytest.raises(ValueError):
        rsvkeys.m_print(b"short")


@pytest.mark.parametrize("v", ["", None, "ab", "zz" * 8, "ab" * 9, 12345])
def test_a_garbled_print_counts_as_absent(v):
    assert rsvkeys.m_print_of(v) == ""


@pytest.mark.parametrize("a, b, same", [
    ({"m_gen": 3, "mf": MF_A}, {"m_gen": 3, "mf": MF_A}, True),
    ({"m_gen": 3, "mf": MF_A}, {"m_gen": 3, "mf": MF_B}, False),
    ({"m_gen": 3, "mf": MF_A}, {"m_gen": 4, "mf": MF_A}, False),
    ({"m_gen": 3, "mf": MF_A}, {"m_gen": 3}, True),          # прежняя сторона — по номеру
    ({"m_gen": 3}, {"m_gen": 3, "mf": ""}, True),
    ({"m_gen": 3}, {"m_gen": 4}, False),
], ids=["same", "same_number_other_key", "other_number", "no_print_one_side", "no_print_both", "no_print_other_number"])
def test_one_generation_is_the_number_and_the_print_when_both_have_it(a, b, same):
    assert rsvkeys.same_m(a, b) is same


# ── канал пары: поле необязательное ─────────────────────────────────────────

@pytest.mark.parametrize("kind", ["qkeys", "qkeys_ok", "pkg"])
def test_the_print_is_an_optional_field_of_the_pair_words(kind):
    assert "mf" in pairproto.PAIR_KINDS[kind][1] and "mf" in pairproto.OPTIONAL[kind], \
        f"{kind}: отпечаток не необязательное поле — прежняя сторона рвала бы сессию"


def test_words_with_and_without_the_print_pass_the_channel_check():
    ok = {"l": "a", "m_gen": 2, "q_gen": 0, "c_gen": 1}
    assert pairproto.check(pairproto.PAIR_PROTO, "qkeys_ok", dict(ok), pairproto.INVITER) == ok, "слово прежней стороны"
    assert pairproto.check(pairproto.PAIR_PROTO, "qkeys_ok", {**ok, "mf": MF_A}, pairproto.INVITER)["mf"] == MF_A
    with pytest.raises(pairproto.ProtocolError):
        pairproto.check(pairproto.PAIR_PROTO, "qkeys_ok", {**ok, "mf": "ab" * 9}, pairproto.INVITER)
    pkg = {"gen": 1, "fp": "ab" * 32, "i": 0, "n": 1, "blob": ""}
    pairproto.check(pairproto.PAIR_PROTO, "pkg", dict(pkg), pairproto.INVITER)
    assert pairproto.check(pairproto.PAIR_PROTO, "pkg", {**pkg, "mf": MF_A}, pairproto.INVITER)["mf"] == MF_A


# ── основа: qkeys и qkeys_ok ────────────────────────────────────────────────

def _mine(s) -> str:
    return rsvkeys.m_print(bytes.fromhex(s.reserve_keys()["m"]))


def test_qkeys_carries_the_print_of_the_current_m(line):
    body = line.s.reserve_qkeys_body()
    assert body is not None and body["mf"] == _mine(line.s), body
    pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.INVITER)


def test_qkeys_ok_with_my_print_confirms_the_keys(line):
    line.propose()
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "mf": _mine(line.s)})
    assert line.s.reserve_qkeys()["acked"]["mf"] == _mine(line.s)
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), "подтверждение своего M не принято"
    line.advance(120)
    assert line.s.reserve_qkeys_body() is None, "после подтверждения своего M ключи шлются снова"


def test_qkeys_ok_of_another_m_under_my_number_is_not_a_confirmation(line):
    """Основа восстановлена из копии: номер поколения тот же, `M` другое — подтверждение
    ящиков прежнего `M` не закрывает ключи, `qkeys` уходят заново."""
    line.propose()
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "mf": MF_A})
    assert MF_A != _mine(line.s), "предпосылка: M линии случайно совпало с заглушкой"
    assert not line.s.reserve_qkeys().get("acked"), "подтверждение чужого M записано как моё"
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())
    line.advance(120)
    assert line.s.reserve_qkeys_body() is not None, "ключи не уходят второму серверу заново"


def test_a_stale_ack_with_another_print_does_not_hold_after_the_keys_changed(line):
    """Подтверждение записано с отпечатком, `M` под тем же номером сменилось (копия) —
    сверка ключей видит расхождение, а не совпавшие номера."""
    line.propose()
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "mf": _mine(line.s)})
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), "предпосылка: ключи не подтверждены"
    line.s.rsv_update("rsv_keys", m=M_B.hex())          # то же m_gen, другое M
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), "чужое M сочтено подтверждённым"
    line.advance(120)
    body = line.s.reserve_qkeys_body()
    assert body is not None and body["mf"] == MF_B, body


def test_qkeys_ok_of_an_older_peer_without_the_print_confirms_by_numbers(line):
    """Прежний второй сервер отпечатка не возвращает — подтверждение по номерам, как раньше."""
    line.propose()
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s)})
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())
    line.advance(120)
    assert line.s.reserve_qkeys_body() is None


# ── резерв: ящики рядом с пакетом ───────────────────────────────────────────

def _qkeys(mf: str = "", m_gen: int = M_GEN) -> dict:
    body = {"l": LINE, "m_gen": m_gen, "q_gen": Q_GEN, "thr": 2, "x": rsvkeys.RESERVE_X, "y": RSV_Y, "qbox": QBOX,
            "c_gen": 1, "cy": RSV_Y, "cbox": QBOX}
    if mf:
        body["mf"] = mf
    return body


@pytest.fixture()
def held(box):
    """Резерв держит пакет `M_A` поколения M_GEN и ящики того же `M`."""
    box.s.rsv_update("rsv_pkg_in", mf=MF_A)
    acts = box.s.reserve_qkeys_in(_qkeys(MF_A))
    assert acts and acts[0][2].get("mf") == MF_A, f"подготовка: {acts}"
    assert box.s.reserve_open_block() == "", f"подготовка: открытие закрыто: {box.s.reserve_open_block()}"
    return box


def test_qkeys_ok_echoes_the_print_it_took(held):
    assert held.s.reserve_qkeys_in(_qkeys(MF_A))[0][2] == {"l": LINE, "m_gen": M_GEN, "q_gen": Q_GEN, "c_gen": 1,
                                                          "mf": MF_A}
    assert "mf" not in held.s.reserve_qkeys_in(_qkeys())[0][2], "отпечаток выдуман за прежнюю основу"


def test_boxes_of_another_m_under_the_same_number_keep_the_old_ones_for_the_package(held):
    """Основа восстановлена из копии: `qkeys` того же номера с другим отпечатком — другое
    поколение; пока пакет прежний, процессу открытия — ящики прежнего `M`."""
    held.s.reserve_qkeys_in({**_qkeys(MF_B), "cbox": "11" * rsvkeys.BOX_LEN})
    prev = held.s.reserve_qkeys().get("peer_prev") or {}
    assert prev.get("mf") == MF_A, f"ящики прежнего M под тем же номером затёрты: {prev}"
    assert held.s.reserve_quorum_peer_keys()["mf"] == MF_A, "под пакет прежнего M отданы ящики нового"
    assert held.s.reserve_open_block() == ""
    assert held.s.reserve_quorum_code_secret("X")["cbox"] == QBOX, "процессу открытия по коду — ящик чужого M"


def test_the_new_package_switches_to_the_boxes_of_its_m(held):
    held.s.reserve_qkeys_in({**_qkeys(MF_B), "cbox": "11" * rsvkeys.BOX_LEN})
    held.s.rsv_update("rsv_pkg_in", gen=4, mf=MF_B)
    assert held.s.reserve_quorum_peer_keys()["mf"] == MF_B, "пакет нового M пришёл, а ящики прежние"
    assert held.s.reserve_quorum_code_secret("X")["cbox"] == "11" * rsvkeys.BOX_LEN


def test_a_package_of_another_m_without_its_boxes_is_not_ready_to_open(held):
    """Пакет нового `M` пришёл раньше его ящиков (номер тот же) — «данных для переезда
    нет» сейчас, а не отказ открытия в аварии."""
    held.s.rsv_update("rsv_pkg_in", gen=4, mf=MF_B)
    assert held.s.reserve_open_block() == "данных для переезда нет"
    assert held.s.reserve_quorum_code_secret("X") is None


def test_without_prints_the_boxes_still_match_by_number(box):
    """Прежняя основа: ни в пакете, ни в `qkeys` отпечатка нет — по номеру, как раньше."""
    box.s.reserve_qkeys_in(_qkeys())
    assert box.s.reserve_open_block() == ""
    box.s.reserve_qkeys_in(_qkeys())
    assert not box.s.reserve_qkeys().get("peer_prev"), "повтор того же поколения записан как прежнее"


# ── пакет: отпечаток со словом pkg ──────────────────────────────────────────

def _blob() -> bytes:
    return seal.seal({"vpn": {}}, k=seal.new_k(), m=M_A, pkg_gen=7, m_gen=M_GEN)


def test_the_reserve_keeps_the_print_of_the_package_from_the_word(box, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    blob = _blob()
    assert box.s.reserve_pkg_in(7, seal.digest(blob), blob, None, mf=MF_A) == ("pkg_ok", "")
    assert box.s.rsv_get("rsv_pkg_in")["mf"] == MF_A
    assert box.s.reserve_pkg_in(7, seal.digest(blob), blob, None) == ("pkg_ok", ""), "пакет прежней основы"
    assert box.s.rsv_get("rsv_pkg_in")["mf"] == "", "отпечаток прежнего пакета остался у нового"
    assert box.s.reserve_pkg_in(7, seal.digest(blob), blob, None, mf="zz") == ("pkg_ok", "")
    assert box.s.rsv_get("rsv_pkg_in")["mf"] == "", "негодный отпечаток записан"


async def test_the_print_rides_with_every_chunk_and_reaches_the_reserve(monkeypatch):
    """Основа шлёт отпечаток с частями пакета; приёмная сторона отдаёт его сервисам, а
    часть без отпечатка (прежняя основа) — без лишнего аргумента."""
    from awgbot.runtime import pairlink
    from awgbot.runtime.pairlink import inbound
    sent = []

    async def fake_send(kind, body=None):
        sent.append(body)
        return True
    monkeypatch.setattr(pairlink, "send", fake_send)
    blob = _blob()
    built = {"gen": 7, "fp": seal.digest(blob), "blob": blob.hex(), "mf": MF_A}
    assert await pairlink.send_package(built) is True
    assert sent and all(b["mf"] == MF_A for b in sent), "часть пакета без отпечатка M"

    calls = []

    def reserve_pkg_in(gen, fp, blob, jrn=None, **kw):
        calls.append(kw)
        return "pkg_ok", ""

    async def fake_line(sess, kind, body=None):
        pass
    monkeypatch.setattr(inbound, "send_line", fake_line)
    obj = types.SimpleNamespace(services=types.SimpleNamespace(reserve_pkg_in=reserve_pkg_in,
                                                               reserve_st_body=lambda: {}))
    for body in sent:
        await inbound.InboundMixin._in_pkg(obj, types.SimpleNamespace(parts={}), dict(body))
    await inbound.InboundMixin._in_pkg(obj, types.SimpleNamespace(parts={}),
                                       {k: v for k, v in sent[0].items() if k != "mf"} | {"n": 1, "i": 0})
    assert calls[0] == {"mf": MF_A} and calls[-1] == {}, calls
