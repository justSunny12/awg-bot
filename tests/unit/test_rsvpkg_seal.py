"""Упаковка пакета переезда (util/rsvpkg/seal.py), формат 2: заголовок с `K`
под `M`, части-JSON и двоичные части (реплика базы `db` — байтами, не hex внутри
JSON) под `K`, конверт DNS под `M`, отпечаток, добивка до ступени 64 КБ, потолок
16 МБ; шифрованный снимок бота основы на резерве под `K` — метаданные и база
двоичной частью, размер базы открыт.

Цена ошибки: заголовок, который не открывается без ключей, — резерв не
может сверить поколение и отпечаток с тем, что сказала основа, и шлёт
`pkg_bad` на каждый целый пакет; размер не ступенями — по размеру пакета
видно, сколько у основы пользователей; чужой `M` или порча байта, прошедшие
молча, — клон из мусора в аварию; пакет больше потолка, собранный молча, —
канал пары, который никогда его не довезёт; база hex внутри JSON — процесс
открытия держит её в памяти восемь–четырнадцать раз и гибнет по OOM посреди
аварийного переезда; распаковка без предела — сжатая бомба в хранилище гостя;
пакет прежнего формата, прочитанный «как-нибудь», — клон из чужих полей.
"""
from __future__ import annotations

import io
import os
import struct
import zlib

import pytest

from awgbot.util import rsvkeys
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.unit

PARTS = {"vpn": {"ifaces": [{"name": "awg0", "port": 51820}]}, "bot": {"env": {"ADMIN_ID": 1}}}
DB = b"SQLite format 3\x00" + b"\x00\xff" * 5000
DNS = {"provider": "cloudflare", "name": "vpn.example.net", "token": "DUMMY-cf", "self": "198.51.100.7"}


@pytest.fixture()
def keys():
    return rsvkeys.new_master(), seal.new_k()


def _blob(keys, **kw):
    m, k = keys
    return seal.seal(kw.pop("parts", PARTS), k=k, m=m, pkg_gen=kw.pop("pkg_gen", 7), m_gen=kw.pop("m_gen", 3),
                     dns=kw.pop("dns", DNS), blobs=kw.pop("blobs", {seal.DB_PART: DB}))


def test_the_package_opens_back_with_m_and_k(keys):
    m, k = keys
    blob = _blob(keys)
    assert seal.open_k(blob, m) == k
    assert seal.open_parts(blob, k) == PARTS, "двоичная часть попала к частям-JSON"
    assert seal.open_dns(blob, m) == DNS
    db = seal.open_blob(blob, k, seal.DB_PART)
    assert db.size == len(DB) and db.read() == DB


def test_the_header_opens_without_any_key(keys):
    blob = _blob(keys)
    h = seal.header(blob)
    assert (h.pkg_gen, h.m_gen, h.size, h.has_dns) == (7, 3, len(blob), True)
    assert h.digest == seal.digest(blob) and len(h.digest) == 64
    assert seal.header(_blob(keys, dns=None)).has_dns is False


def test_the_size_is_a_multiple_of_64_kib_whatever_the_content(keys):
    sizes = set()
    for n in (0, 1, 1000, 70_000, 200_000):
        blob = _blob(keys, blobs={seal.DB_PART: os.urandom(n)})
        assert len(blob) % seal.STEP == 0, f"содержимое {n} байт дало размер не ступенью: {len(blob)}"
        sizes.add(len(blob))
    assert min(sizes) == seal.STEP


def test_another_m_does_not_open_the_key_or_the_dns_envelope(keys):
    blob = _blob(keys)
    other = rsvkeys.new_master()
    with pytest.raises(seal.PackageError):
        seal.open_k(blob, other)
    with pytest.raises(seal.PackageError):
        seal.open_dns(blob, other)


def test_k_does_not_open_the_dns_envelope_and_another_k_does_not_open_the_parts(keys):
    """Переезд кнопкой отдаёт резерву только K: токен DNS под M ему не
    виден."""
    m, k = keys
    blob = _blob(keys)
    with pytest.raises(seal.PackageError):
        seal.open_dns(blob, k[:16])
    with pytest.raises(seal.PackageError):
        seal.open_parts(blob, seal.new_k())


def test_any_damaged_byte_is_caught_by_the_digest(keys):
    m, k = keys
    blob = _blob(keys)
    for pos in (0, 5, 20, 60, 200, len(blob) // 2, len(blob) - 40, len(blob) - 1):
        bad = bytearray(blob)
        bad[pos] ^= 0x01
        with pytest.raises(seal.PackageError, match="отпечаток"):
            seal.open_parts(bytes(bad), k)
        with pytest.raises(seal.PackageError, match="отпечаток"):
            seal.header(bytes(bad))


@pytest.mark.parametrize("blob", [b"", b"AWGK", b"\x00" * 100])
def test_not_a_package_is_refused(blob):
    with pytest.raises(seal.PackageError):
        seal.header(blob)


def test_a_package_over_the_ceiling_is_not_built():
    """Несжимаемые 17 МБ: сборка отказывает, а не отдаёт каналу пакет,
    который тот не довезёт."""
    m, k = rsvkeys.new_master(), seal.new_k()
    with pytest.raises(seal.PackageError, match="16 МБ"):
        seal.seal({"bot": {}}, k=k, m=m, pkg_gen=1, m_gen=1, blobs={seal.DB_PART: os.urandom(17 * 1024 * 1024)})


def test_a_blob_over_the_ceiling_is_refused_on_reading():
    with pytest.raises(seal.PackageError):
        seal.header(b"\x00" * (seal.MAX_SIZE + 1))


def test_a_wrong_length_k_is_refused():
    with pytest.raises(seal.PackageError):
        seal.seal(PARTS, k=b"\x00" * 16, m=rsvkeys.new_master(), pkg_gen=1, m_gen=1)


def test_two_seals_of_the_same_content_differ():
    """Одинаковое содержимое — разные шифротексты и отпечатки: по отпечатку
    резерв не должен узнавать «ничего не поменялось» без ключа."""
    m, k = rsvkeys.new_master(), seal.new_k()
    a = seal.seal(PARTS, k=k, m=m, pkg_gen=1, m_gen=1)
    b = seal.seal(PARTS, k=k, m=m, pkg_gen=1, m_gen=1)
    assert a != b and seal.digest(a) != seal.digest(b)


def test_the_snapshot_on_the_reserve_opens_only_with_its_k():
    k = seal.new_k()
    blob = seal.snapshot_seal(k, {"pkg_gen": 3, "at": "x"}, DB)
    meta, db = seal.snapshot_open(k, blob)
    assert meta == {"pkg_gen": 3, "at": "x"} and db.read() == DB
    assert DB[:16] not in blob, "база снимка открытой"
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(seal.new_k(), blob)
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(k, blob[:-1] + bytes([blob[-1] ^ 1]))


def test_the_base_size_of_a_snapshot_is_open_and_bound_to_the_ciphertext():
    """Бот резерва ключа не имеет, а потолок памяти гостя считает по базе снимка — размер
    открыт; подменённый размер в заголовке снимок не открывает."""
    k = seal.new_k()
    blob = seal.snapshot_seal(k, {"pkg_gen": 3}, DB)
    assert seal.snapshot_db_size(blob) == len(DB)
    forged = blob[:5] + struct.pack(">Q", 1) + blob[13:]
    assert seal.snapshot_db_size(forged) == 1
    with pytest.raises(seal.PackageError, match="размер"):
        seal.snapshot_open(k, forged)
    for junk in (b"", b"AWGS", b"\x00" * 100):
        assert seal.snapshot_db_size(junk) == 0


# ── формат 2: база — двоичной частью ────────────────────────────────────────

def test_the_base_travels_as_bytes_not_as_hex_inside_json(keys):
    """Несжимаемая база 3 МБ: пакет — база и заголовок до ступени, часть бота — без базы."""
    m, k = keys
    db = os.urandom(3 * 1024 * 1024)
    blob = _blob(keys, blobs={seal.DB_PART: db})
    assert len(blob) < 3.2 * 1024 * 1024, f"база в пакете не байтами: {len(blob)}"
    assert "db" not in seal.open_parts(blob, k)["bot"]
    assert seal.open_blob(blob, k).read() == db


def test_the_base_streams_into_a_file_in_steps_without_a_whole_copy(keys):
    """Распаковка — потоком: файл получает базу кусками не больше мегабайта, ни одного
    куска с базу целиком."""
    _m, k = keys
    db = b"\x00" * (6 * 1024 * 1024) + os.urandom(4096)
    blob = _blob(keys, blobs={seal.DB_PART: db})

    class Sink(io.BytesIO):
        sizes: list[int] = []

        def write(self, b):
            self.sizes.append(len(b))
            return super().write(b)
    out = Sink()
    assert seal.open_blob(blob, k).write_to(out) == len(db)
    assert out.getvalue() == db
    assert max(out.sizes) <= 1024 * 1024 and len(out.sizes) >= 6, out.sizes


def _forged_blob(keys, plain: bytes) -> bytes:
    """Пакет, чья двоичная часть — произвольный открытый текст под верным K."""
    m, k = keys
    body = (seal.MAGIC + struct.pack(">BII", seal.FORMAT, 1, 1))
    ek = seal._box(rsvkeys.box_key(m), k)
    ct = seal._box(k, plain)
    body += struct.pack(">H", len(ek)) + ek + struct.pack(">I", 0) + struct.pack(">I", 1)
    body += struct.pack(">B", 2) + b"db" + seal.KIND_BYTES + struct.pack(">I", len(ct)) + ct
    body += b"\x00" * ((-(len(body) + 32)) % seal.STEP)
    import hashlib
    return body + hashlib.blake2b(body, digest_size=32, person=seal._PERSON).digest()


@pytest.mark.parametrize("what, plain", [
    ("больше заявленного", struct.pack(">Q", 10) + zlib.compress(b"\x00" * 1000)),
    ("меньше заявленного", struct.pack(">Q", 5000) + zlib.compress(b"\x00" * 1000)),
    ("бомба мимо предела", struct.pack(">Q", seal.MAX_DB + 1) + zlib.compress(b"\x00" * 1000)),
    ("не zlib", struct.pack(">Q", 10) + b"DUMMY-not-zlib"),
    ("хвост после потока", struct.pack(">Q", 3) + zlib.compress(b"abc") + b"tail"),
    ("оборвана", b"\x00\x00"),
])
def test_a_binary_part_that_does_not_match_its_size_or_is_damaged_is_refused(keys, what, plain):
    """Подменить двоичную часть может только тот, у кого K, — но распаковка в хранилище
    гостя идёт без предела только по заявленному размеру: ни байта сверх, ни недобора."""
    _m, k = keys
    with pytest.raises(seal.PackageError):
        seal.open_blob(_forged_blob(keys, plain), k).write_to(io.BytesIO())


def test_a_package_without_the_base_part_says_so(keys):
    _m, k = keys
    with pytest.raises(seal.PackageError, match="нет части db"):
        seal.open_blob(_blob(keys, blobs=None), k)


def test_a_part_name_twice_is_not_built(keys):
    m, k = keys
    with pytest.raises(seal.PackageError, match="повторяется"):
        seal.seal({"db": {}}, k=k, m=m, pkg_gen=1, m_gen=1, blobs={"db": b"x"})


def test_a_package_of_the_previous_format_is_refused_without_reading_it(keys):
    """Формат 1 (база hex внутри JSON) снят целиком: такой пакет — «другой формат»,
    а не попытка прочесть части по прежней раскладке."""
    m, k = keys
    blob = bytearray(_blob(keys))
    blob[4] = 1
    body = bytes(blob[:-32])
    import hashlib
    old = body + hashlib.blake2b(body, digest_size=32, person=seal._PERSON).digest()
    with pytest.raises(seal.PackageError, match="другой формат"):
        seal.header(old)
    with pytest.raises(seal.PackageError, match="другой формат"):
        seal.open_parts(old, k)
    snap = bytearray(seal.snapshot_seal(k, {"pkg_gen": 1}, DB))
    snap[4] = 1
    assert seal.snapshot_db_size(bytes(snap)) == 0
    with pytest.raises(seal.PackageError, match="другой формат"):
        seal.snapshot_open(k, bytes(snap))
    assert seal.FORMAT == 2
