"""Приглашение в пару серверов `AWGP1.…` (util/pairtoken.py) и код
подтверждения пары (util/joinsas.py с доменом пары).

Приглашение админ пересылает руками в другой бот: строка должна пережить
пересылку целиком внутри текста, порча — распознаваться, а не давать линк
пары на чужих ключах. Код подтверждения пары и код подключения шлюза при
одних и тех же входах обязаны различаться: иначе код, подсмотренный при
сопряжении одного вида, годился бы для другого.

Цена ошибки: приглашение с испорченным ключом, принятое за целое, — линк пары
на ключах, которых нет у пригласившего, и сопряжение, висящее до срока;
неверный адрес пригласившего в /30 — канал пары стучится сам в себя;
обобщённый `joinsas`, поменявший прежние коды, — шлюзы, выпущенные прошлой
версией, не сходятся с сервером по коду.
"""
from __future__ import annotations

import base64
import hashlib
import struct

import pytest

from awgbot.util import joinsas, pairtoken

pytestmark = pytest.mark.unit

PID = "00112233aabbccdd"


def _tok(**over) -> pairtoken.PairToken:
    base = dict(version="4.0.0", pair_id=PID, expires=1_790_000_000, host="srv-a.example.net", pair_port=51830,
                peer_addr="10.99.98.1", inviter_pub=bytes(range(32)), peer_priv=bytes(range(100, 132)),
                jc=5, jmin=10, jmax=50, s=(20, 30, 40, 50),
                h=((100, 200), (500_000_100, 500_000_900), (1_000_000_100, 1_000_090_000),
                   (1_500_000_100, 1_999_999_999)),
                awg_gen=2)
    base.update(over)
    return pairtoken.PairToken(**base)


# ── вперёд и назад ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("over", [{}, {"version": "4.0.0.3"}, {"host": "198.51.100.7"},
                                  {"host": "a" * 63}, {"expires": 0}, {"expires": 2**32 - 1}])
def test_an_invitation_reads_back_field_for_field(over):
    tok = _tok(**over)
    text = pairtoken.encode(tok)
    assert text.startswith("AWGP1.") and "=" not in text, "в пересылке строка — без хвоста base64"
    assert pairtoken.decode(text) == tok


def test_the_expiry_travels_as_is():
    """Срок — тот, что выставил пригласивший: на принимающей стороне по нему
    решается «приглашение истекло»."""
    assert pairtoken.decode(pairtoken.encode(_tok(expires=1_800_000_123))).expires == 1_800_000_123


def test_the_inviter_address_is_the_other_host_of_the_slash_30():
    for mine, other in (("10.99.98.1", "10.99.98.2"), ("10.99.98.2", "10.99.98.1")):
        tok = pairtoken.decode(pairtoken.encode(_tok(peer_addr=mine)))
        assert tok.cidr == "10.99.98.0/30"
        assert tok.inviter_addr == other, f"адрес пригласившего при моём {mine} — не {tok.inviter_addr}"


def test_the_obfuscation_is_given_as_the_link_script_writes_it():
    obf = _tok().obfuscation()
    assert obf["Jc"] == "5" and obf["S4"] == "50"
    assert obf["H1"] == "100-200" and obf["H4"] == "1500000100-1999999999"
    assert set(obf) == {"Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"}, \
        "I1 и прочее в приглашение не входят — едут по каналу после кода"
    assert _tok().inviter_pub_b64 == base64.b64encode(bytes(range(32))).decode()


def test_the_invitation_is_found_inside_a_forwarded_message():
    text = pairtoken.encode(_tok())
    msg = f"Привет! Вот приглашение:\n\n{text}\n\nДействует час."
    assert pairtoken.find(msg) == text
    assert pairtoken.find("тут ничего нет") == "" and pairtoken.find(None) == ""


# ── порча и чужое ────────────────────────────────────────────────────────────

def test_any_corrupted_symbol_is_refused():
    """Контрольная сумма ловит замену любого знака тела: линк на испорченных
    ключах не должен подняться."""
    text = pairtoken.encode(_tok())
    body = text[len(pairtoken.PREFIX):]
    passed = []
    for i in range(len(body)):
        sub = "A" if body[i] != "A" else "B"
        bad = pairtoken.PREFIX + body[:i] + sub + body[i + 1:]
        try:
            got = pairtoken.decode(bad)
        except pairtoken.TokenError:
            continue
        if got != _tok():
            passed.append(i)
    assert not passed, f"порча в позициях {passed[:5]} прошла как целое приглашение"


@pytest.mark.parametrize("text", ["", "AWGJ1.abc", "AWGP1.", "AWGP1." + "A" * 39, "AWGP1." + "A" * 401,
                                  "AWGP1.!!!!" + "A" * 60, "просто текст"])
def test_not_an_invitation_is_refused(text):
    with pytest.raises(pairtoken.TokenError):
        pairtoken.decode(text)


def _resum(body: bytes) -> str:
    raw = body + hashlib.blake2b(body, digest_size=4).digest()
    return pairtoken.PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _body(tok=None) -> bytes:
    text = pairtoken.encode(tok or _tok())
    b64 = text[len(pairtoken.PREFIX):]
    return base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))[:-4]


def test_another_format_version_asks_to_update_the_bot():
    body = bytearray(_body())
    body[0] = 2
    with pytest.raises(pairtoken.TokenError, match="обнови бот"):
        pairtoken.decode(_resum(bytes(body)))


def test_a_body_of_the_wrong_length_with_a_correct_sum_is_refused():
    """Сумма сходится, но тело короче или длиннее разметки — это не наше
    приглашение (или другой версии без смены номера формата)."""
    body = _body()
    for bad in (body[:-1], body + b"\x00"):
        with pytest.raises(pairtoken.TokenError):
            pairtoken.decode(_resum(bad))


def test_a_host_with_a_bad_symbol_inside_a_well_summed_body_is_refused():
    body = bytearray(_body(_tok(host="srv-a.example.net")))
    i = bytes(body).index(b"srv-a")
    body[i] = ord(" ")
    with pytest.raises(pairtoken.TokenError):
        pairtoken.decode(_resum(bytes(body)))


@pytest.mark.parametrize("over", [{"host": "a" * 64}, {"host": ""}, {"host": "srv a"}, {"host": "срв.example"},
                                  {"version": "4.0"}, {"version": "4.256.0"}, {"version": "4.0.0.0.1"},
                                  {"pair_id": "0011"}, {"inviter_pub": b"\x00" * 31}, {"peer_priv": b"\x00" * 33}])
def test_what_does_not_fit_the_invitation_is_not_encoded(over):
    with pytest.raises(pairtoken.TokenError):
        pairtoken.encode(_tok(**over))


def test_the_host_length_byte_is_bounded_on_reading():
    """Длина хоста больше 63 в теле с верной суммой — отказ, а не чтение
    ключей со сдвигом."""
    body = bytearray(_body())
    hpos = 1 + 4 + 8 + 4
    hlen = body[hpos]
    host = bytes(body[hpos + 1:hpos + 1 + hlen])
    longer = bytes(body[:hpos]) + struct.pack(">B", 64) + host + b"a" * (64 - hlen) + bytes(body[hpos + 1 + hlen:])
    with pytest.raises(pairtoken.TokenError):
        pairtoken.decode(_resum(longer))


# ── код подтверждения пары ───────────────────────────────────────────────────

def _keys():
    a_priv, a_pub = joinsas.new_pair()
    b_priv, b_pub = joinsas.new_pair()
    return a_pub, b_pub, joinsas.shared(a_priv, b_pub), joinsas.shared(b_priv, a_pub)


def test_both_sides_of_a_pair_get_one_code_and_one_key():
    a_pub, b_pub, ka, kb = _keys()
    t = joinsas.PAIR_SAS_TAG
    assert joinsas.code(PID, None, a_pub, b_pub, ka, tag=t) == joinsas.code(PID, None, a_pub, b_pub, kb, tag=t)
    assert joinsas.pair_key(PID, ka, tag=joinsas.PAIR_KEY_TAG) == joinsas.pair_key(PID, kb, tag=joinsas.PAIR_KEY_TAG)


def test_the_pair_code_and_key_differ_from_the_gateway_ones_on_the_same_inputs():
    """Те же id, эфемерные ключи и общий секрет: код пары ≠ коду шлюза (с
    любым слотом и без слота), ключ пары ≠ ключу сопряжения шлюза."""
    a_pub, b_pub, k, _ = _keys()
    pair = joinsas.code(PID, None, a_pub, b_pub, k, tag=joinsas.PAIR_SAS_TAG)
    gw = {joinsas.code(PID, s, a_pub, b_pub, k) for s in (1, 2, 3)} | {joinsas.code(PID, None, a_pub, b_pub, k)}
    assert pair not in gw, "код пары совпал с кодом подключения шлюза"
    assert joinsas.pair_key(PID, k, tag=joinsas.PAIR_KEY_TAG) != joinsas.pair_key(PID, k)


def test_the_gateway_code_and_key_are_what_they_were_before_the_pair():
    """Эталон посчитан прежним модулем (до обобщения тегом): выпущенные шлюзы
    и сервер новой версии должны получить тот же код из тех же входов."""
    g, s, k = bytes(range(32)), bytes(range(32, 64)), bytes(range(64, 96))
    assert joinsas.code("0123456789abcdef", 2, g, s, k) == "PN8K2G"
    assert joinsas.pair_key("0123456789abcdef", k).hex() == \
        "ed380ef8e4b748a21b3e088028c32baea6f21bee9a267e1648c60b26012dbf12"
