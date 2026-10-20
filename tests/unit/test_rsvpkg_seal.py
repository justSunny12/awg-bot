"""Упаковка пакета переезда (util/rsvpkg/seal.py): заголовок с `K` под `M`,
части под `K`, конверт DNS под `M`, отпечаток, добивка до ступени 64 КБ,
потолок 16 МБ; шифрованный снимок бота основы на резерве под `K`.

Цена ошибки: заголовок, который не открывается без ключей, — резерв не
может сверить поколение и отпечаток с тем, что сказала основа, и шлёт
`pkg_bad` на каждый целый пакет; размер не ступенями — по размеру пакета
видно, сколько у основы клиентов; чужой `M` или порча байта, прошедшие
молча, — клон из мусора в аварию; пакет больше потолка, собранный молча, —
канал пары, который никогда его не довезёт.
"""
from __future__ import annotations

import os

import pytest

from awgbot.util import rsvkeys
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.unit

PARTS = {"vpn": {"ifaces": [{"name": "awg0", "port": 51820}]}, "bot": {"db": "00ff", "env": {"ADMIN_ID": 1}}}
DNS = {"provider": "cloudflare", "name": "vpn.example.net", "token": "DUMMY-cf", "self": "198.51.100.7"}


@pytest.fixture()
def keys():
    return rsvkeys.new_master(), seal.new_k()


def _blob(keys, **kw):
    m, k = keys
    return seal.seal(kw.pop("parts", PARTS), k=k, m=m, pkg_gen=kw.pop("pkg_gen", 7), m_gen=kw.pop("m_gen", 3),
                     dns=kw.pop("dns", DNS))


def test_the_package_opens_back_with_m_and_k(keys):
    m, k = keys
    blob = _blob(keys)
    assert seal.open_k(blob, m) == k
    assert seal.open_parts(blob, k) == PARTS
    assert seal.open_dns(blob, m) == DNS


def test_the_header_opens_without_any_key(keys):
    blob = _blob(keys)
    h = seal.header(blob)
    assert (h.pkg_gen, h.m_gen, h.size, h.has_dns) == (7, 3, len(blob), True)
    assert h.digest == seal.digest(blob) and len(h.digest) == 64
    assert seal.header(_blob(keys, dns=None)).has_dns is False


def test_the_size_is_a_multiple_of_64_kib_whatever_the_content(keys):
    sizes = set()
    for n in (0, 1, 1000, 70_000, 200_000):
        blob = _blob(keys, parts={"bot": {"db": os.urandom(n).hex()}})
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
        seal.seal({"bot": {"db": os.urandom(17 * 1024 * 1024).hex()}}, k=k, m=m, pkg_gen=1, m_gen=1)


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
    blob = seal.snapshot_seal(k, {"clients": 3})
    assert seal.snapshot_open(k, blob) == {"clients": 3}
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(seal.new_k(), blob)
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(k, blob[:-1] + bytes([blob[-1] ^ 1]))
