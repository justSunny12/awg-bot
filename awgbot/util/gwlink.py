"""
gwlink.py — конверт канала ВПС ↔ шлюз внутри линка (концепт «канал линка», §10.2).

Канал живёт ВНУТРИ туннеля AWG: шифрование даёт он, и здесь его нет. Подпись
нужна для другого — чтобы сообщение слота 1 нельзя было выдать за сообщение
слота 2, и чтобы клиент туннеля, как-то дотянувшийся до порта, не выдал себя за
шлюз. Ключ выводится из приватного ключа линка, который есть у обеих сторон:
ВПС держит его в gw-<линк>.conf, шлюз — в своём конфиге линка.

Ключ подписи канала ОТДЕЛЬНЫЙ от ключа gwsign (тот подписывает claim, который
человек пересылает руками). Разделение доменов стоит одной строки и закрывает
целый класс путаницы: пересланный claim нельзя скормить каналу как сообщение, а
перехваченное сообщение канала — выдать за claim.

Формат — строка на сообщение, чтобы поток разбирался без длин и состояний:
    GL1:<base64url(JSON)>.<base64url(HMAC-SHA256[:20])>\\n
JSON: {"t": <вид>, "seq": <номер в сессии>, "ts": <unix>, …поля вида…}

ДОБИВКА. В теле есть поле "_" из пробелов: длина сообщения доводится до
кратности (512 для snap и settings, 256 для delta). Внутри туннеля наблюдателю
не видно содержимое, но видны длины, и «всегда ровно 317 байт» — такая же
подпись события, как ровный период. Добивка лежит ВНУТРИ подписанного тела:
снаружи её не срезать, не сломав подпись.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from awgbot.util import bundlecrypt

PREFIX = "GL1:"
PROTO = 1
# Больше этого — рвём сессию, не пытаясь разобрать: единственный источник на том
# конце наш же агент, и мегабайтная строка означает либо поломку, либо попытку
# засадить нам память. Для списков этапа 3 будет свой предел и чанки.
MAX_LINE = 256 * 1024
# Окно повтора. Семь суток gwsign нужны человеческой пересылке; здесь обе
# стороны живые и в одной сети, и широкое окно только помогало бы повторщику.
MAX_SKEW_SECONDS = 300
PAD_SNAP = 512
PAD_DELTA = 256


class ProtocolError(ValueError):
    """Сообщение не разобрано: подпись, окно времени, порядок, размер."""


def channel_key(link_privkey_b64: str) -> bytes:
    return hashlib.sha256(b"awgbot-gwlink-v1"
                          + bundlecrypt.derive_key(link_privkey_b64)).digest()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64len(n: int) -> int:
    """Длина base64url без '=' для n байт — считаем, а не кодируем: добивка
    подбирается в несколько проходов, и каждый проход кодировать накладно."""
    return (n + 2) // 3 * 4 - (3 - n % 3) % 3 if n else 0


def pack(key: bytes, kind: str, body: dict | None = None, *,
         seq: int = 0, pad: int = 0, now: float | None = None) -> bytes:
    """Одно сообщение строкой, готовое к отправке (с переводом строки)."""
    payload = dict(body or {})
    payload.update(t=kind, seq=int(seq), ts=int(time.time() if now is None else now))

    def _encode(filler: int) -> tuple[bytes, int]:
        p = {**payload, "_": " " * filler} if filler else payload
        raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()
        return raw, len(PREFIX) + _b64len(len(raw)) + 1 + _b64len(20) + 1

    raw, total = _encode(0)
    if pad > 1 and total % pad:
        # Байт добивки удлиняет base64 на один-два символа, поэтому в точку
        # попадаем не расчётом, а коротким доводом от прикидки: стартуем чуть
        # ниже нужного и шагаем по байту. Обычно это два-три прохода.
        # Минус запас на само поле "_": без него прикидка перескакивает границу,
        # и короткое сообщение добивалось бы до следующей кратности.
        start = max(0, (-total % pad) * 3 // 4 - 12)
        for filler in range(start, start + 4 * pad):
            raw, total = _encode(filler)
            if total % pad == 0:
                break
    mac = hmac.new(key, raw, hashlib.sha256).digest()[:20]
    return (PREFIX + _b64u(raw) + "." + _b64u(mac) + "\n").encode()


def unpack(key: bytes, line: bytes | str, *, now: float | None = None,
           last_seq: int | None = None) -> dict:
    """Разобрать и проверить строку. ProtocolError — чужая подпись, окно
    времени, повтор или мусор. Возвращает тело с "t", "seq", "ts"."""
    text = line.decode(errors="replace") if isinstance(line, bytes) else line
    text = text.strip()
    if len(text) > MAX_LINE:
        raise ProtocolError("сообщение длиннее предела")
    if not text.startswith(PREFIX) or "." not in text:
        raise ProtocolError("не сообщение канала")
    head, _, tail = text[len(PREFIX):].partition(".")
    try:
        raw = _unb64u(head)
        mac = _unb64u(tail)
    except (ValueError, TypeError) as e:
        raise ProtocolError("сообщение повреждено") from e
    want = hmac.new(key, raw, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(mac, want):
        raise ProtocolError("подпись не сходится — сообщение не от этого шлюза")
    try:
        data = json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError("сообщение повреждено") from e
    if not isinstance(data, dict) or not isinstance(data.get("t"), str):
        raise ProtocolError("сообщение без вида")
    ts = int(data.get("ts") or 0)
    now = time.time() if now is None else now
    if abs(now - ts) > MAX_SKEW_SECONDS:
        raise ProtocolError("сообщение вне окна времени — повтор или часы разошлись")
    seq = int(data.get("seq") or 0)
    if last_seq is not None and seq <= last_seq:
        raise ProtocolError("порядок сообщений нарушен — повтор")
    data.pop("_", None)
    return data
