"""Unit: генерация QR (util/qrgen) — блоб из vpn-ссылки, кадр серии, итоговый GIF."""
import base64
import struct

import pytest

from awgbot.util import qrgen

pytestmark = pytest.mark.unit


def _make_vpn(raw: bytes) -> str:
    return "vpn://" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_vpn_to_blob_roundtrips():
    raw = b"\x00\x01compressed-config\xff"
    assert qrgen._vpn_to_blob(_make_vpn(raw)) == raw


def test_chunk_frame_structure():
    frame = qrgen._chunk_frame(2, 1, b"abc")
    buf = base64.urlsafe_b64decode(frame + "=" * (-len(frame) % 4))
    magic, count, index, length = struct.unpack(">hBBI", buf[:8])
    assert count == 2 and index == 1 and length == 3
    assert buf[8:] == b"abc"


def test_vpn_link_to_qr_gif_is_animated_gif():
    gif = qrgen.vpn_link_to_qr_gif(_make_vpn(b"x" * 200))
    assert isinstance(gif, bytes)
    assert gif[:6] in (b"GIF87a", b"GIF89a")               # валидная сигнатура GIF


def test_qr_gif_deterministic_for_same_link():
    link = _make_vpn(b"same-payload-bytes")
    assert qrgen.vpn_link_to_qr_gif(link) == qrgen.vpn_link_to_qr_gif(link)
