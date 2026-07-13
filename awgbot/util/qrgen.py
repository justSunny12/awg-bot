"""
qrgen.py — генерация QR-кода для импорта в AmneziaVPN (анимированный GIF-серией).

Формат серии выведен из исходников amnezia-client (exportController.cpp,
generateQrCodeImageSeries + defs.h). Каждый кадр серии:

    base64url( magic:qint16_BE ++ count:quint8 ++ index:quint8 ++ QByteArray(data) )

где QByteArray сериализуется Qt-потоком как [quint32_BE длина ++ сами байты], а
data — это «compressedConfig»: qCompress-блоб (4 байта BE длины несжатого + zlib),
т.е. ровно то, что лежит внутри vpn://-ссылки после base64url-декода. magic=1984.

Amnezia в родном экспорте бьёт по k=850 (даёт неравные кадры). Мы делим payload
РОВНО пополам на 2 кадра — приёмник склеивает чанки по index простой
конкатенацией, размер чанка ему не важен, а равные кадры дают равномерную (и
вдвое меньшую, чем одиночный QR) плотность. Проверено живым импортом. Бонус:
двухкадровый QR нельзя снять одним скриншотом.

Кадры собираются в анимированный GIF (1 сек/кадр, бесконечный цикл), который
Telegram автоплеит как фото — получателю не нужно открывать файл.
"""
from __future__ import annotations

import base64
import io
import math
import struct

import qrcode
from PIL import Image

QR_MAGIC_CODE = 1984          # amnezia::qrMagicCode (qint16, BigEndian)
FRAME_DURATION_MS = 1000      # 1 секунда на кадр
FRAMES = 2                    # делим payload на столько равных частей
_QR_BOX = 12                  # размер модуля в пикселях (крупнее — легче скан)
_QR_BORDER = 4                # тихая зона (модулей)


def _vpn_to_blob(vpn_link: str) -> bytes:
    """vpn://... → сырой compressedConfig (qCompress-блоб), который чанкует Amnezia.
    Это байты сразу после base64url-декода ссылки (без разбора JSON)."""
    link = vpn_link.strip()
    if link.startswith("vpn://"):
        link = link[len("vpn://"):]
    return base64.urlsafe_b64decode(link + "=" * (-len(link) % 4))


def _chunk_frame(count: int, index: int, data: bytes) -> str:
    """Один кадр серии в формате Amnezia → base64url-строка для QR.
    Повторяет QDataStream: magic(int16 BE) + count(u8) + index(u8)
    + QByteArray(data)=[uint32 BE длина + байты]."""
    buf = (struct.pack(">h", QR_MAGIC_CODE)
           + struct.pack(">B", count)
           + struct.pack(">B", index)
           + struct.pack(">I", len(data))
           + data)
    return base64.urlsafe_b64encode(buf).rstrip(b"=").decode()


def _qr_image(payload: str) -> Image.Image:
    """QR-картинка одного кадра (Ecc L, как в экспорте Amnezia)."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                       border=_QR_BORDER, box_size=_QR_BOX)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def vpn_link_to_qr_gif(vpn_link: str) -> bytes:
    """vpn://-ссылка → анимированный GIF (2 равных кадра серии Amnezia).
    Возвращает готовые байты GIF для отправки как фото."""
    blob = _vpn_to_blob(vpn_link)
    part = math.ceil(len(blob) / FRAMES)
    images = []
    for index in range(FRAMES):
        piece = blob[index * part:(index + 1) * part]
        images.append(_qr_image(_chunk_frame(FRAMES, index, piece)))
    # выровнять кадры под общий размер (у равных частей совпадает, но подстрахуемся)
    side = max(im.size[0] for im in images)
    norm = []
    for im in images:
        if im.size[0] != side:
            canvas = Image.new("RGB", (side, side), "white")
            off = (side - im.size[0]) // 2
            canvas.paste(im, (off, off))
            im = canvas
        norm.append(im)
    out = io.BytesIO()
    norm[0].save(out, format="GIF", save_all=True, append_images=norm[1:],
                 duration=FRAME_DURATION_MS, loop=0, disposal=2)
    return out.getvalue()


__all__ = ["vpn_link_to_qr_gif"]
