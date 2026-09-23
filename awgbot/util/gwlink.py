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
# засадить нам память. Предел один на обоих концах — и у приёмника строки, и у
# разбора ниже. Фиды локальной сети (этап 3) идут одним сжатым сообщением и
# весят килобайты — до предела далеко.
MAX_LINE = 256 * 1024
# Окно повтора. Семь суток gwsign нужны человеческой пересылке; здесь обе
# стороны живые и в одной сети, и широкое окно только помогало бы повторщику.
MAX_SKEW_SECONDS = 300
PAD_SNAP = 512
PAD_DELTA = 256
# Большое тело (фиды локальной сети) режется на части этого размера: строка
# с конвертом обязана влезть в MAX_LINE на приёмной стороне, иначе сессия
# рвётся, переподключается, получает тот же фид — и так по кругу.
CHUNK = 128 * 1024
MAX_CHUNKS = 64


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


# ── настройки по каналу (этап 2) ─────────────────────────────────────────────
#
# Канал возит ДАННЫЕ из закрытого списка, никогда код и никогда секреты. Ключи —
# ровно те, что бандл сегодня везёт в юнит обвязки переменными окружения, и ни
# одного больше. Чего здесь нет и не будет: конфиг аплинка (внутри приватный
# ключ), ключи линка, токен агента, почта, фраза шифрования, тело скрипта,
# поставка — всё это только бандлом, который переносит человек.
# PEER_HOME_NETS здесь НЕТ, и это не забыто: на шлюзе подсети соседей живут не
# только в юните, но и в AllowedIPs пира ВПС в конфиге линка — от них awg
# принимает пакеты и ставит маршруты. Конфиг линка правит только бандл, и
# доставка одного юнита дала бы зелёную галочку при неработающем доступе.
SETTINGS_KEYS = ("ADMIN_IPS", "HOME_SUBNETS", "LAN_MODE", "RESOLVER")
BUNDLE_ONLY_KEYS = ("PEER_HOME_NETS",)
_MAX_LIST = 64


def _ipv4(tok: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(tok).version == 4
    except ValueError:
        return False


def _cidr4(tok: str) -> bool:
    import ipaddress
    if "/" not in tok:
        return False
    try:
        return ipaddress.ip_network(tok, strict=False).version == 4
    except ValueError:
        return False


def _norm(val) -> str:
    return " ".join(str(val if val is not None else "").split())


def validate_settings(raw: dict) -> dict:
    """Проверить присланные настройки по типу каждого значения.

    Неизвестный ключ игнорируется (новый ВПС, старый агент). Значение не
    прошло проверку — отвергается ВСЁ сообщение: половина новых настроек хуже
    старых целиком. Проверка строже, чем нужно для работы: в значениях могут
    быть только цифры, точки, косые и пробелы — ни кавычек, ни переводов строк,
    то есть их нечем вывести ни в юнит, ни в оболочку.
    """
    if not isinstance(raw, dict):
        raise ProtocolError("настройки — не словарь")
    out: dict[str, str] = {}
    for key in SETTINGS_KEYS:
        if key not in raw:
            raise ProtocolError(f"в настройках нет {key}")
        val = _norm(raw[key])
        toks = val.split()
        if len(toks) > _MAX_LIST:
            raise ProtocolError(f"{key}: слишком много значений")
        if key == "LAN_MODE":
            ok = val in ("0", "1")
        elif key == "RESOLVER":
            ok = val == "" or _ipv4(val)
        elif key == "ADMIN_IPS":
            ok = all(_ipv4(t) for t in toks)
        else:                                    # HOME_SUBNETS, PEER_HOME_NETS
            ok = all(_cidr4(t) for t in toks)
        if not ok:
            raise ProtocolError(f"{key}: недопустимое значение")
        out[key] = val
    return out


def settings_hash(values: dict) -> str:
    """Отпечаток набора настроек: ключи по порядку, значения нормализованы.
    Хэш содержимого, а не счётчик поколений: ВПС, восстановленный из копии, не
    откатит счётчик назад и не решит, что малина «уже применила более новое»."""
    text = "".join(f"{k}={_norm(values.get(k, ''))}\n" for k in SETTINGS_KEYS)
    return hashlib.sha256(text.encode()).hexdigest()
