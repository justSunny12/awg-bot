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

ПОВТОР (proto 2). Защита от повтора — нонсами сессии, а не часами: при hello
клиент называет свой нонс (поле `nonce`), сервер отвечает первым сообщением со
своим тем же полем.
Дальше каждая сторона подписывает сообщение с нонсом ПОЛУЧАТЕЛЯ (HMAC над
нонс + тело): сообщение прошлой сессии не сходится по построению, а порядок
внутри сессии держит `seq`. Сам hello подписан без нонса и повторяем — он не
несёт ничего, кроме открытия сессии, и любое следующее сообщение повторщика
подпись не пройдёт. Часы хоста в проверку не входят вовсе: малина без RTC,
поднявшаяся раньше NTP, раньше молча теряла канал ровно тогда, когда он и
должен был сказать «часы разошлись». `ts` остаётся справочным — по нему ВПС
показывает расхождение часов, но не решает ничего.

ДОБИВКА. В теле есть поле "_" из пробелов: длина сообщения доводится до
кратности (512 для snap и settings, 256 для delta). Внутри туннеля наблюдателю
не видно содержимое, но видны длины, и «всегда ровно 317 байт» — такая же
подпись события, как ровный период. Добивка лежит ВНУТРИ подписанного тела:
снаружи её не срезать, не сломав подпись.

Здесь же — общие правила обеих сторон, которые раньше жили копиями и
расходились: порт по умолчанию (DEFAULT_PORT), адреса сторон в /30 линка
(link_hosts), отпечаток фидов (feeds_hash), таблица ключей бандла и их
человеческих имён (BUNDLE_KEYS, KEY_HUMAN, snap_field — поле снимка).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from awgbot.util import bundlecrypt

PREFIX = "GL1:"
PROTO = 2
NONCE_BYTES = 16
# Больше этого — рвём сессию, не пытаясь разобрать: единственный источник на том
# конце наш же агент, и мегабайтная строка означает либо поломку, либо попытку
# засадить нам память. Предел один на обоих концах — и у приёмника строки, и у
# разбора ниже. Большое тело (фиды локальной сети) режется на части — CHUNK.
MAX_LINE = 256 * 1024
PAD_SNAP = 512
PAD_DELTA = 256
# Большое тело (фиды локальной сети) режется на части этого размера: строка
# с конвертом обязана влезть в MAX_LINE на приёмной стороне, иначе сессия
# рвётся, переподключается, получает тот же фид — и так по кругу.
CHUNK = 128 * 1024
MAX_CHUNKS = 64


class ProtocolError(ValueError):
    """Сообщение не разобрано: подпись (в том числе чужой сессии), порядок, размер."""


def channel_key(link_privkey_b64: str) -> bytes:
    return hashlib.sha256(b"awgbot-gwlink-v1"
                          + bundlecrypt.derive_key(link_privkey_b64)).digest()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_nonce() -> bytes:
    import secrets
    return secrets.token_bytes(NONCE_BYTES)


def nonce_b64(nonce: bytes) -> str:
    return _b64u(nonce)


def nonce_from(text) -> bytes:
    """Нонс из поля сообщения; не той длины или мусор — ProtocolError."""
    try:
        raw = _unb64u(str(text or ""))
    except (ValueError, TypeError) as e:
        raise ProtocolError("нонс сессии повреждён") from e
    if len(raw) != NONCE_BYTES:
        raise ProtocolError("нонс сессии не той длины")
    return raw


def _b64len(n: int) -> int:
    """Длина base64url без '=' для n байт — считаем, а не кодируем: добивка
    подбирается в несколько проходов, и каждый проход кодировать накладно."""
    return (n + 2) // 3 * 4 - (3 - n % 3) % 3 if n else 0


def pack(key: bytes, kind: str, body: dict | None = None, *,
         seq: int = 0, pad: int = 0, now: float | None = None, nonce: bytes = b"") -> bytes:
    """Одно сообщение строкой, готовое к отправке (с переводом строки).
    nonce — нонс ПОЛУЧАТЕЛЯ; пусто только у hello."""
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
    mac = hmac.new(key, nonce + raw, hashlib.sha256).digest()[:20]
    return (PREFIX + _b64u(raw) + "." + _b64u(mac) + "\n").encode()


def unpack(key: bytes, line: bytes | str, *, now: float | None = None,
           last_seq: int | None = None, nonce: bytes = b"") -> dict:
    """Разобрать и проверить строку. ProtocolError — чужая подпись (в том
    числе чужой сессии — нонс не тот), повтор по seq или мусор. nonce — СВОЙ
    нонс, которым отправитель обязан был подписать; пусто — только для hello.
    now — не используется с proto 2 (окна времени нет), оставлен ради прежних
    вызовов. Возвращает тело с "t", "seq", "ts"."""
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
    want = hmac.new(key, nonce + raw, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(mac, want):
        raise ProtocolError("подпись не сходится — не этот шлюз или чужая сессия")
    try:
        data = json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError("сообщение повреждено") from e
    if not isinstance(data, dict) or not isinstance(data.get("t"), str):
        raise ProtocolError("сообщение без вида")
    try:
        int(data.get("ts") or 0)
        seq = int(data.get("seq") or 0)
    except (TypeError, ValueError, OverflowError) as e:
        raise ProtocolError("сообщение повреждено") from e
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
        else:                                    # HOME_SUBNETS
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


# ── общие правила обеих сторон ───────────────────────────────────────────────
# Одно место на то, что раньше жило в трёх-шести копиях и расходилось: порт по
# умолчанию, адреса сторон в /30, отпечаток фидов, таблица ключей конфигурации.

DEFAULT_PORT = 8787

# Все ключи, которые бандл везёт в юнит обвязки и которые сверяет снимок;
# поле снимка — ключ в нижнем регистре (`bundle.<ключ>`).
BUNDLE_KEYS = SETTINGS_KEYS + BUNDLE_ONLY_KEYS
KEY_HUMAN = {"ADMIN_IPS": "устройства админа", "HOME_SUBNETS": "локальные подсети",
             "LAN_MODE": "режим «за шлюзом — без VPN»", "RESOLVER": "резолвер",
             "PEER_HOME_NETS": "подсети за другими шлюзами"}


def snap_field(key: str) -> str:
    return key.lower()


def link_hosts(cidr: str) -> tuple[str, str]:
    """(адрес ВПС, адрес шлюза) в /30 линка: первый и второй хост — ровно так их
    раздаёт скрипт линка. Пусто — подсеть не /30 или битая."""
    import ipaddress
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return "", ""
    if net.version != 4 or net.prefixlen != 30:
        return "", ""
    hosts = list(net.hosts())
    return str(hosts[0]), str(hosts[1])


def feeds_hash(domains: str, nets: str) -> str:
    """Отпечаток фидов локальной сети — одно правило на ВПС и на шлюзе:
    разойдись оно, шлюз отвергал бы каждую доставку и качал бы фиды сам."""
    return hashlib.sha256((domains + "\n--\n" + nets).encode()).hexdigest()
