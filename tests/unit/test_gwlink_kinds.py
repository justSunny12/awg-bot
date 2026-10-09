"""
Виды сообщений канала ВПС ↔ шлюз: полнота и эталоны.

Канал — две стороны в разных пакетах (runtime/linkserver, runtime/linkclient),
и ничто, кроме этого набора, не держит их словари вместе: вид, который одна
сторона шлёт, а другая не разбирает, молча падает в «неизвестное — игнорирую».
Списки снимаются с кода разбором AST, не руками: новый вид без пары здесь
краснеет сам.

Эталоны — упакованная строка по каждому виду на заглушке ключа с
зафиксированными номером, временем и нонсом. Конверт детерминирован (добивка —
пробелы, подбор по длине), поэтому отпечаток строки — байт в байт то, что уйдёт
в линк. Сменился конверт, порядок полей или добивка — эталон скажет об этом
раньше, чем старый агент на шлюзе.
"""
from __future__ import annotations

import ast
import base64
import hashlib
from pathlib import Path

import pytest

from awgbot.util import gwlink

ROOT = Path(__file__).resolve().parents[2] / "awgbot" / "runtime"
SERVER = ROOT / "linkserver"
CLIENT = ROOT / "linkclient"

# Ключ линка — заглушка: 32 байта, ни одного настоящего секрета.
DUMMY_PRIV = base64.b64encode(b"DUMMY-GWLINK-KEY-FOR-KINDS-32B!!").decode()
NOW = 1_700_000_000
NONCE = bytes(range(16))


# ── списки с кода ────────────────────────────────────────────────────────────

def _str_arg(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _sent_kinds(pkg: Path, senders: tuple[str, ...]) -> set[str]:
    """Виды из вызовов self.send(slot, "kind"…) / self._send("kind"…) /
    gwlink.pack(key, "kind"…): строковый литерал среди первых двух аргументов."""
    out: set[str] = set()
    for path in sorted(pkg.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in senders:
                continue
            for arg in node.args[:2]:
                kind = _str_arg(arg)
                if kind is not None:
                    out.add(kind)
    return out


def _parsed_kinds(pkg: Path) -> set[str]:
    """Виды из сравнений kind == "x" / kind in ("x", "y") / msg.get("t") == "x"."""
    out: set[str] = set()

    def _is_kind(expr: ast.AST) -> bool:
        if isinstance(expr, ast.Name) and expr.id == "kind":
            return True
        return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "get" and len(expr.args) == 1 and _str_arg(expr.args[0]) == "t")

    for path in sorted(pkg.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Compare) or not _is_kind(node.left):
                continue
            for right in node.comparators:
                if isinstance(right, ast.Tuple):
                    out.update(k for k in map(_str_arg, right.elts) if k)
                else:
                    kind = _str_arg(right)
                    if kind:
                        out.add(kind)
    return out


SERVER_SENDS = _sent_kinds(SERVER, ("send",))
CLIENT_SENDS = _sent_kinds(CLIENT, ("_send", "_send_result", "pack"))
SERVER_PARSES = _parsed_kinds(SERVER)
CLIENT_PARSES = _parsed_kinds(CLIENT)


def test_the_lists_came_off_the_code_and_are_not_empty():
    """Сам сборщик жив: регресс разбора AST дал бы пустые множества и
    «полноту» даром."""
    assert {"ask", "role", "settings"} <= SERVER_SENDS
    assert {"hello", "snap", "delta"} <= CLIENT_SENDS
    assert {"hello", "snap", "delta"} <= SERVER_PARSES
    assert {"ask", "role", "settings"} <= CLIENT_PARSES


def test_every_kind_the_server_sends_the_client_parses():
    assert SERVER_SENDS <= CLIENT_PARSES, sorted(SERVER_SENDS - CLIENT_PARSES)


def test_every_kind_the_client_sends_the_server_parses():
    assert CLIENT_SENDS <= SERVER_PARSES, sorted(CLIENT_SENDS - SERVER_PARSES)


def test_no_side_parses_a_kind_nobody_sends():
    """Мёртвая ветка разбора — след снятого вида: либо снять ветку, либо
    вернуть отправителя."""
    assert CLIENT_PARSES <= SERVER_SENDS, sorted(CLIENT_PARSES - SERVER_SENDS)
    assert SERVER_PARSES <= CLIENT_SENDS, sorted(SERVER_PARSES - CLIENT_SENDS)


# ── эталоны ──────────────────────────────────────────────────────────────────
#
# Тело — представительное для вида (поля те же, что собирает отправитель),
# добивка — та же, что у отправителя. Отпечаток считается от строки целиком,
# с переводом строки.

_SERVER_REFS: dict[str, tuple[dict | None, int, str]] = {
    "ask": ({"what": "snap"}, gwlink.PAD_DELTA,
            "379e649f69e82043c3c3d0db258bd3fc7460e19d6c79a5b8096bb35eeaf331cc"),
    "role": ({"active": True, "standby": False, "name": "Шлюз 1"}, gwlink.PAD_DELTA,
             "5741f17f097065a6231ca1fd974f65fc511f68d1600f9355bbbb1993985809f7"),
    "settings": ({"values": {"ADMIN_IPS": "10.8.0.2", "HOME_SUBNETS": "192.168.1.0/24",
                             "LAN_MODE": "1", "RESOLVER": ""}}, gwlink.PAD_SNAP, "79007f081a634f85f1449f22ad80407bd9a2a5747099723ee693b84045c87fd9"),
    "lists_ok": ({"hash": "ab" * 32}, gwlink.PAD_DELTA, "d50f7b8b9d204aacf66d8e7a267fffaeb9355235118044be51357b74b3f6c09c"),
    "lists": ({"hash": "ab" * 32, "z": "eJwDAAAAAAE="}, gwlink.PAD_DELTA, "6e76d9522e2d960f656a6822209c411a86ff273f827ff4ecfd62a7199f39ed15"),
    "lists_part": ({"hash": "ab" * 32, "i": 0, "n": 2, "z": "eJwDAAAAAAE="}, gwlink.PAD_DELTA, "71392f5a1d415cee37869bf40e2c63582e446d9d16d02b382b2f07765d6a4435"),
    "peer_svc": ({"hash": "cd" * 32, "items": [{"name": "nas", "ip": "192.168.2.10"}]},
                 gwlink.PAD_SNAP, "7b4622830f7e047c137512658198d211dbd95e08bb3ce6fd9c4f883fa1b7c238"),
    "own_set": ({"run": "r1", "hash": "ef" * 32, "sites": [], "nets": []}, gwlink.PAD_SNAP, "87980df107f4a2e2d088b8d1919e53ffc1a092703ad1345b877f9d2aa0b9ce04"),
    "applied_ack": ({"fp": "12" * 32, "at": "2026-10-10T00:00:00"}, gwlink.PAD_DELTA, "bef24a2ec608e0986ffcb0d73a22f8aeefbc9ca917fcbb8254d038675fa2298c"),
}

_CLIENT_REFS: dict[str, tuple[dict | None, int, str]] = {
    "hello": ({"proto": gwlink.PROTO, "agent": "3.2.1", "lists_hash": "", "svc_hash": "",
               "nonce": gwlink.nonce_b64(NONCE), "own_hash": ""}, gwlink.PAD_DELTA, "3721e807a97a58bc1a582f38d1be1f7e58e48e8c61a7d3bbfddbadfd6dd09fb9"),
    "snap": ({"rev": 1, "agent_version": "3.2.1", "egress_ok": True}, gwlink.PAD_SNAP, "9902519eb44d9e20907ddaad47005413d666618eb56c828a34715c7c6ea6f61b"),
    "delta": ({"rev": 2, "egress_ok": False}, gwlink.PAD_DELTA, "f811a683952aa1667c5d71f340422647472e4733dcabe668dedd48a1c25ec3c4"),
    "ack": ({"ok": True, "hash": "34" * 32, "error": ""}, gwlink.PAD_DELTA, "32abe6fdc26cece36076c97231cfc748ef1a13d5d43333c26e9b37fdbb17b70d"),
    "applied": ({"ok": True, "error": "", "fp": "12" * 32, "at": "2026-10-10T00:00:00"},
                gwlink.PAD_DELTA, "a765178dac034ff05b8d40ec3e1076551f49da95fc2451c27924e3f198b95747"),
    "svc": ({"items": [{"name": "nas", "ip": "192.168.1.10"}]}, gwlink.PAD_SNAP, "eacf61762c4d78d57c3e9e58eca4a699fa1b81625ac70acac32958bd43bbe50c"),
    "claim": ({"token": "DUMMY-CLAIM-TOKEN"}, gwlink.PAD_SNAP, "6a806f3c3570886086bf437d368d7ded94dfea295bc1b27254ff805558f7e64a"),
    "installed": ({}, gwlink.PAD_DELTA, "83b8edee8ca6d867a055c4a65fb050378b38a3ab7dcdc367e95b282e0e7807b8"),
    "lists_ack": ({"ok": True, "hash": "ab" * 32, "n": 3, "error": ""}, gwlink.PAD_DELTA, "7abd7f399ee12b0259c1e88874f7c565a5102b94c6761ee47ca72dba4c02d931"),
    "own_ev": ({"run": "r1", "ev": [{"op": "add", "site": "example.org"}]}, gwlink.PAD_SNAP, "7cf5ee60be1de7af98e7e7e17f2d5ac55c88b590189f42e9a4d1ea94315a64ab"),
    "own_ack": ({"ok": True, "hash": "ef" * 32, "n": 1, "error": ""}, gwlink.PAD_DELTA, "4a924daad721094f8ec95990bff48c238f7ec6f5011c02aa39ffc85bb279de00"),
    "peer_svc_ack": ({"ok": True, "hash": "cd" * 32, "n": 1, "error": ""}, gwlink.PAD_DELTA, "dc96a6446ba8e581278e95a66b06626191ca47f4e24a4b14d8b5349a47158137"),
}


def _line(kind: str, body: dict | None, pad: int) -> bytes:
    nonce = b"" if kind == "hello" else NONCE
    return gwlink.pack(gwlink.channel_key(DUMMY_PRIV), kind, body, seq=3, pad=pad, now=NOW, nonce=nonce)


def test_the_reference_table_names_every_kind_and_nothing_else():
    """Новый вид обязан получить эталон; снятый — потерять его."""
    assert set(_SERVER_REFS) == SERVER_SENDS, sorted(set(_SERVER_REFS) ^ SERVER_SENDS)
    assert set(_CLIENT_REFS) == CLIENT_SENDS, sorted(set(_CLIENT_REFS) ^ CLIENT_SENDS)


@pytest.mark.parametrize("kind,body,pad,digest", [(k, *v) for k, v in {**_SERVER_REFS, **_CLIENT_REFS}.items()],
                         ids=list(_SERVER_REFS) + list(_CLIENT_REFS))
def test_each_kind_packs_byte_for_byte_as_the_reference_and_comes_back_whole(kind, body, pad, digest):
    line = _line(kind, body, pad)
    assert hashlib.sha256(line).hexdigest() == digest, (
        f"{kind}: конверт изменился — {hashlib.sha256(line).hexdigest()}")
    assert pad == 0 or len(line) % pad == 0, f"{kind}: длина не кратна добивке"
    msg = gwlink.unpack(gwlink.channel_key(DUMMY_PRIV), line,
                        nonce=b"" if kind == "hello" else NONCE)
    assert msg.pop("t") == kind and msg.pop("seq") == 3 and msg.pop("ts") == NOW
    assert msg == (body or {}), f"{kind}: тело не пережило конверт"
