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
    gwlink.pack(key, "kind"…): строковый литерал среди первых двух аргументов.
    `pkg` — каталог пакета или один файл."""
    out: set[str] = set()
    for path in ([pkg] if pkg.is_file() else sorted(pkg.glob("*.py"))):
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


SERVER_SENDS = _sent_kinds(SERVER, ("send", "_join_send"))   # сессия сопряжения — своей отправкой
# проба живости сервера (резерв) уходит в канал конфигурации из такта гостевого
# канала агента — runtime/guestlink.py, через ту же отправку клиента линка
GUESTLINK = ROOT / "guestlink.py"
CLIENT_SENDS = _sent_kinds(CLIENT, ("_send", "_send_result", "pack")) | _sent_kinds(GUESTLINK, ("_send",))
SERVER_PARSES = _parsed_kinds(SERVER)
CLIENT_PARSES = _parsed_kinds(CLIENT)


def test_the_lists_came_off_the_code_and_are_not_empty():
    """Сам сборщик жив: регресс разбора AST дал бы пустые множества и
    «полноту» даром."""
    assert {"ask", "role", "cfg"} <= SERVER_SENDS
    assert {"hello", "snap", "delta"} <= CLIENT_SENDS
    assert {"hello", "snap", "delta"} <= SERVER_PARSES
    assert {"ask", "role", "cfg"} <= CLIENT_PARSES


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

_KEEP_ITEMS = {"conf": {"quiet_hours.quiet_hours_enabled": True}, "fw": {"SSH_FILTER": "1"}, "mail": None}

_SERVER_REFS: dict[str, tuple[dict | None, int, str]] = {
    "ask": ({"what": "snap"}, gwlink.PAD_DELTA,
            "379e649f69e82043c3c3d0db258bd3fc7460e19d6c79a5b8096bb35eeaf331cc"),
    "role": ({"active": True, "standby": False, "name": "Шлюз 1", "caps": [], "bot": "awg_srv_bot"}, gwlink.PAD_DELTA,
             "3ec184c0a234eb49b745e6f2d7c36feda89dd1c1159921124692b50a1b6279a6"),
    "rst_done": ({"id": "a1b2c3d4e5f6", "ok": False, "error": "бэкап пришёл повреждённым"}, gwlink.PAD_DELTA,
                 "0b9437e5bf25d130872a0f19fff097f7d5b7ed2409e2769986ca45eaf859e939"),
    "lists_ok": ({"hash": "ab" * 32}, gwlink.PAD_DELTA, "d50f7b8b9d204aacf66d8e7a267fffaeb9355235118044be51357b74b3f6c09c"),
    "lists": ({"hash": "ab" * 32, "z": "eJwDAAAAAAE="}, gwlink.PAD_DELTA, "6e76d9522e2d960f656a6822209c411a86ff273f827ff4ecfd62a7199f39ed15"),
    "lists_part": ({"hash": "ab" * 32, "i": 0, "n": 2, "z": "eJwDAAAAAAE="}, gwlink.PAD_DELTA, "71392f5a1d415cee37869bf40e2c63582e446d9d16d02b382b2f07765d6a4435"),
    "peer_svc": ({"hash": "cd" * 32, "items": [{"name": "nas", "ip": "192.168.2.10"}]},
                 gwlink.PAD_SNAP, "7b4622830f7e047c137512658198d211dbd95e08bb3ce6fd9c4f883fa1b7c238"),
    "own_set": ({"run": "r1", "hash": "ef" * 32, "sites": [], "nets": []}, gwlink.PAD_SNAP, "87980df107f4a2e2d088b8d1919e53ffc1a092703ad1345b877f9d2aa0b9ce04"),
    "cfg": ({"gen": 18, "hash": "9c41" * 4, "env": {"CLIENT_SUBNET": "10.8.1.0/24", "ADMIN_IPS": "10.8.1.2",
                                                   "LAN_MODE": "1", "LAN_SUBNETS": "192.168.68.0/24", "RESOLVER": "",
                                                   "PEER_LAN_NETS": "", "LINK_CHANNEL_PORT": "8787"},
             "link": {"conf": "# awg-bot: контракт линка 1\n[Interface]\nAddress = 10.99.99.2/30\n"},
             "server": {"name": "awg-srv"}}, gwlink.PAD_SNAP, "eed290d8bcf18ad9a6967d681f3da8964239d29955406d65d2813c2cd8d14102"),
    "link_next": ({"id": "a1b2c3d4e5f6", "conf": "# awg-bot: контракт линка 1\n[Interface]\nAddress = 10.99.99.2/30\n",
                   "fp": "ab12 cd34"}, gwlink.PAD_SNAP, "15f5775e30a55a37fe891444d779f82ad30ff4e8150a877c8def3c09801d2f99"),
    "link_go": ({"id": "a1b2c3d4e5f6"}, gwlink.PAD_DELTA, "45bc646bb865fcf1b4231e575c066573073ac6228cc1fa0ade758184c99a4d80"),
    "link_cancel": ({"id": "a1b2c3d4e5f6"}, gwlink.PAD_DELTA, "51024cb9d3a4c07b6189ecada0bc9e763b69653501cc51019bbfcb4f85578153"),
    "keep_ok": ({"hash": "ab12" * 4}, gwlink.PAD_DELTA, "6d3974b76f0f4aa9c070d6489982d9a3972cc5eac983b55150a04f06b2504735"),
    "keep_none": ({}, gwlink.PAD_DELTA, "be36fce9b1542870d397c738459f445703c95b30269e11f2a508d83292d3e81c"),
    "upd": ({"id": "a1b2c3d4", "target": "v3.5.0", "digest": "sha256:" + "ab" * 32, "why": "fleet", "hold": True},
            gwlink.PAD_DELTA, "89c70f9583e75250ff15d92e67b0dfbd8f29ee09ad9b1b1b89b907e870bf6cf4"),
    "upd_commit": ({"id": "a1b2c3d4"}, gwlink.PAD_DELTA,
                   "d523a246abe2df683c0bb4842b72ef99a0382f06094fdb8a2e653f3bc3bc0464"),
    "upd_revert": ({"id": "a1b2c3d4", "to": "v3.4.0"}, gwlink.PAD_DELTA,
                   "4e2b656a20f3ed19c4ee4de4e0822707a069faf759ee3468a38bc21c7269c284"),
    "keep_back": ({"why": "device", "from": "2026-10-12T08:40:00+03:00", "at": "2026-10-12T08:40:00+03:00",
                   "hash": "ab12" * 4, "items": _KEEP_ITEMS, "traffic": {"month": "2026-10", "rx": 10, "tx": 20}},
                  gwlink.PAD_DELTA, "2eea1716541cfd278b9fe3b46632fbc3b403f42e17fd21d21e323dcd9ceef77e"),
    # сессия сопряжения (подключение шлюза командой)
    "join_eph": ({"eph": "QUJD" * 10 + "QUI", "slot_name": "Шлюз 2", "until": "2026-10-18T13:07:00+03:00", "slot": 2},
                 gwlink.PAD_DELTA, "34271f79b32b38aa90c116685877e4d8c3da706519f9f6ed4e1dbfac1dd22917"),
    "join_wrong": ({"left": 2}, gwlink.PAD_DELTA, "7bfe3e63312a09f61ae87cba340a37e4b0abb2e0f9a02701cd7435470d48ac3b"),
    "join_refused": ({"why": "code"}, gwlink.PAD_DELTA,
                     "a7e4eea6e5e890470301d92d4057d438bbf826d8fef24f3bc20887ab0851bb8c"),
    "join_ok": ({"box": "ab" * 72}, gwlink.PAD_DELTA, "950f8e85c412be152246d4727dd0379fa490cff09302415cc137131833069db4"),
    "join_done_ok": ({"id": "0123456789abcdef"}, gwlink.PAD_DELTA,
                     "ae20782dcb00c8f35465397e169b7e4158f1e6a4b6bfe18edea118170a12e9ff"),
    "pong": ({"n": "0123456789abcdef"}, gwlink.PAD_DELTA,
             "2b8cb703253c7597c34e1003c038422492a82560be43169a290dbbaa68c35fdd"),
}

_CLIENT_REFS: dict[str, tuple[dict | None, int, str]] = {
    "hello": ({"proto": gwlink.PROTO, "agent": "3.2.1", "lists_hash": "", "svc_hash": "",
               "nonce": gwlink.nonce_b64(NONCE), "own_hash": "", "caps": []}, gwlink.PAD_DELTA, "040f0e6ac15a2634244fbc9ee6e20b33293715ee4e5416e70966b3366050e9cf"),
    "snap": ({"rev": 1, "agent_version": "3.2.1", "egress_ok": True}, gwlink.PAD_SNAP, "9902519eb44d9e20907ddaad47005413d666618eb56c828a34715c7c6ea6f61b"),
    "delta": ({"rev": 2, "egress_ok": False}, gwlink.PAD_DELTA, "f811a683952aa1667c5d71f340422647472e4733dcabe668dedd48a1c25ec3c4"),
    "svc": ({"items": [{"name": "nas", "ip": "192.168.1.10"}]}, gwlink.PAD_SNAP, "eacf61762c4d78d57c3e9e58eca4a699fa1b81625ac70acac32958bd43bbe50c"),
    "claim": ({"token": "DUMMY-CLAIM-TOKEN"}, gwlink.PAD_SNAP, "6a806f3c3570886086bf437d368d7ded94dfea295bc1b27254ff805558f7e64a"),
    "installed": ({}, gwlink.PAD_DELTA, "83b8edee8ca6d867a055c4a65fb050378b38a3ab7dcdc367e95b282e0e7807b8"),
    "lists_ack": ({"ok": True, "hash": "ab" * 32, "n": 3, "error": ""}, gwlink.PAD_DELTA, "7abd7f399ee12b0259c1e88874f7c565a5102b94c6761ee47ca72dba4c02d931"),
    "own_ev": ({"run": "r1", "ev": [{"op": "add", "site": "example.org"}]}, gwlink.PAD_SNAP, "7cf5ee60be1de7af98e7e7e17f2d5ac55c88b590189f42e9a4d1ea94315a64ab"),
    "own_ack": ({"ok": True, "hash": "ef" * 32, "n": 1, "error": ""}, gwlink.PAD_DELTA, "4a924daad721094f8ec95990bff48c238f7ec6f5011c02aa39ffc85bb279de00"),
    "cfg_ack": ({"gen": 18, "hash": "9c41" * 4, "applied": ["server", "env", "link"], "wait": {"mail": "ab12" * 4},
                 "declined": {}, "failed": {}}, gwlink.PAD_DELTA, "b4969fc23ac0c99ea28d4df78c5904b90219aab034fb330242536c1391ca1c95"),
    "link_ack": ({"id": "a1b2c3d4e5f6", "state": "ready", "error": ""}, gwlink.PAD_DELTA, "5470dfb609542e23645daa1d31040b8042e04a5bccc5e2fd4b390609b7742b57"),
    "link_done": ({"id": "a1b2c3d4e5f6", "ok": True, "keys": "new"}, gwlink.PAD_DELTA, "d8f443962ff9fe6133b81c9fc175597be909763e94be6a78254f58a5c2b7d7a9"),
    "peer_svc_ack": ({"ok": True, "hash": "cd" * 32, "n": 1, "error": ""}, gwlink.PAD_DELTA, "dc96a6446ba8e581278e95a66b06626191ca47f4e24a4b14d8b5349a47158137"),
    "rst_part": ({"id": "a1b2c3d4e5f6", "i": 0, "n": 1, "size": 6, "sha": "ab" * 32, "name": "awg-bot-backup-main.tgz.enc",
                  "z": "QVdHQksx"}, gwlink.PAD_DELTA, "edabf41c4e00270cdc9933a141aa7f46a24b24a06dcb2c05bd95a4e20f3c29ea"),
    "upd_ack": ({"id": "a1b2c3d4", "state": "done", "step": "v3.5.0", "version": "3.5.0", "error": ""},
                gwlink.PAD_DELTA, "8fa491c6c4083847708ae6a392bfb5690137184a09b25271f990574a19f8362f"),
    "join_done": ({"id": "0123456789abcdef"}, gwlink.PAD_DELTA,
                  "1944d915051f163afc10915041bce7bdd56791550ac781f4f04feab1b249be25"),
    "keep": ({"v": 1, "hash": "ab12" * 4, "at": "2026-10-12T08:40:00+03:00", "items": _KEEP_ITEMS,
              "traffic": {"month": "2026-10", "rx": 10, "tx": 20}}, gwlink.PAD_DELTA,
             "2b0dc7f7b047b2d31cb441cae22f111dcfe9f7e7dbea6c449c80316e51d663bd"),
}


def _line(kind: str, body: dict | None, pad: int) -> bytes:
    nonce = b"" if kind == "hello" else NONCE
    return gwlink.pack(gwlink.channel_key(DUMMY_PRIV), kind, body, seq=3, pad=pad, now=NOW, nonce=nonce)


# слова кворума (4.2.0): сервер → шлюз по каналу конфигурации (rsvactions «gwlink» → send_quorum),
# шлюз → сервер — взгляд голосующего (guestlink «link»)
_SERVER_REFS.update({
    "qask": ({'l': 'a', 'g': 4, 'tgt': 'r'}, gwlink.PAD_DELTA,
            "967f0fb1c55c004fb3358dbc4fe7fe8fc099d6602356c9a556a51e1ebda39ca1"),
    "qtake": ({'l': 'a', 'e': 12, 'tgt': 'r'}, gwlink.PAD_DELTA,
             "4fc1bc01dc63417fa611198be17b5e89ac6242b2ebf4d2d7624054e85e519202"),
    "qrel": ({'l': 'a', 'e': 12}, gwlink.PAD_DELTA,
            "91f47cb9b0cff0e82c95f78c3983d10ff40b211e338c65dad573987ec80758bb"),
    "qneed": ({'l': 'a', 'q_gen': 3, 'since': '2026-10-30T14:05:00+03:00'}, gwlink.PAD_DELTA,
             "fa63ba4fe86c85fb03b13bf8a9ec5acc08b7777755a322860ac32e66f4c4be00"),
    "qpause": ({'l': 'a', 'until': '2026-10-30T14:10:00+03:00'}, gwlink.PAD_DELTA,
              "9354ccbf5a76b1a516921e9fba9e333b5f19cfddb0412d4821ccfd1859326c6f"),
    "qstate": ({'l': 'a', 'e': 12, 'holder': 'self', 'r': 'lead'}, gwlink.PAD_DELTA,
              "4d59848b382eae5bf0c03bdc2b53d0573e2fadd8cb2cb090fe20559b36b89c26"),
})
_CLIENT_REFS.update({
    "qview": ({'l': 'a', 'g': 4, 'w': 'see', 'since': '2026-10-30T14:05:00+03:00', 'e': 12, 'tgt': 'h', 'i': {'hs': 37, 'flow': True, 'at': '2026-10-30T14:05:00+03:00'}}, gwlink.PAD_DELTA,
             "8f29ab74658a38c22758ac0ab5e7c46d04ad2f1442ec1135545a14454f2d231f"),
})


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
