"""
jointoken.py — токен подключения шлюза (`AWGJ1.<base64url>`), обе роли.

Сервер выпускает его вместе с командой подключения (§2.4 концепта «единые
обновления»); шлюз (`awg-bot join`, установщик) разбирает. В токене — только
то, без чего не поднять линк слота и не поставить нужный выпуск: номер слота,
выпуск X.Y.Z.P, id сопряжения, срок, хост и порты сервера, адрес шлюза в /30,
открытый ключ стороны ВПС, закрытый ключ шлюзовой стороны (сгенерирован под
токен), базовая обфускация линка Jc…H4 — целиком. Ни PSK, ни I1, ни ADMIN_ID,
ни токена бота шлюза: всё это едет по каналу после верного кода.

Двоичная запись (big-endian):

    1  версия формата (1)
    1  номер слота
    4  выпуск: X, Y, Z, P (P = 0 — без хотфикса)
    8  id сопряжения
    4  срок действия, unix
    1+ хост сервера: длина и ≤ 63 байт [A-Za-z0-9.-]
    2  порт линка        2  порт канала
    4  адрес шлюза в /30 линка
   32  открытый ключ стороны ВПС     32  закрытый ключ шлюзовой стороны
    3  Jc, Jmin, Jmax                 4  S1–S4
   32  H1–H4 — нижняя и верхняя граница диапазона, uint32
    4  контрольная сумма: BLAKE2b (4 байта) от всего выше

Разбор установщиком на sh (версия, хост, порты) — по тем же смещениям
(install/awg-bot-install.sh), контрольную сумму сверяет `awg-bot join`.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import struct
from dataclasses import dataclass

PREFIX = "AWGJ1."
FORMAT = 1
HOST_MAX = 63
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,63}\Z")
_SUM = 4


class TokenError(ValueError):
    """Токен не разобрался: не наш, повреждён, другой версии формата."""


@dataclass(frozen=True)
class JoinToken:
    slot: int
    version: str          # «3.4.0» или «3.4.0.1»
    join_id: str          # 16 hex
    expires: int          # unix
    host: str
    link_port: int
    channel_port: int
    gw_addr: str          # адрес шлюза в /30 линка
    vps_pub: bytes        # 32 байта
    gw_priv: bytes        # 32 байта
    jc: int
    jmin: int
    jmax: int
    s: tuple              # S1–S4
    h: tuple              # ((lo, hi) × 4)

    # ── вид для конфигов ─────────────────────────────────────────────────────

    @property
    def vps_pub_b64(self) -> str:
        return base64.b64encode(self.vps_pub).decode()

    @property
    def gw_priv_b64(self) -> str:
        return base64.b64encode(self.gw_priv).decode()

    @property
    def vps_addr(self) -> str:
        """Адрес ВПС в /30: первый хост той же /30, что адрес шлюза."""
        net = ipaddress.ip_network(f"{self.gw_addr}/30", strict=False)
        return str(list(net.hosts())[0])

    @property
    def cidr(self) -> str:
        return str(ipaddress.ip_network(f"{self.gw_addr}/30", strict=False))

    def obfuscation(self) -> dict:
        """Jc…H4 строками — как их пишет скрипт линка."""
        out = {"Jc": str(self.jc), "Jmin": str(self.jmin), "Jmax": str(self.jmax)}
        for i, v in enumerate(self.s, 1):
            out[f"S{i}"] = str(v)
        for i, (lo, hi) in enumerate(self.h, 1):
            out[f"H{i}"] = f"{lo}-{hi}"
        return out


def _version_bytes(version: str) -> bytes:
    parts = [int(x) for x in str(version).lstrip("v").split(".")]
    if not 3 <= len(parts) <= 4 or any(not 0 <= p <= 255 for p in parts):
        raise TokenError(f"выпуск {version!r} не укладывается в токен")
    parts += [0] * (4 - len(parts))
    return bytes(parts)


def _version_str(raw: bytes) -> str:
    x, y, z, p = raw
    return f"{x}.{y}.{z}" + (f".{p}" if p else "")


def _sum(body: bytes) -> bytes:
    return hashlib.blake2b(body, digest_size=_SUM).digest()


def encode(tok: JoinToken) -> str:
    """Токен строкой `AWGJ1.<base64url без =>`."""
    host = tok.host.encode("ascii")
    if not _HOST_RE.match(tok.host):
        raise TokenError("хост сервера не укладывается в токен")
    if len(tok.vps_pub) != 32 or len(tok.gw_priv) != 32:
        raise TokenError("ключ линка не 32 байта")
    jid = bytes.fromhex(tok.join_id)
    if len(jid) != 8:
        raise TokenError("id сопряжения — 8 байт")
    body = (struct.pack(">BB", FORMAT, int(tok.slot)) + _version_bytes(tok.version) + jid
            + struct.pack(">I", int(tok.expires)) + struct.pack(">B", len(host)) + host
            + struct.pack(">HH", int(tok.link_port), int(tok.channel_port))
            + ipaddress.IPv4Address(tok.gw_addr).packed + tok.vps_pub + tok.gw_priv
            + struct.pack(">BBB", tok.jc, tok.jmin, tok.jmax) + struct.pack(">BBBB", *tok.s)
            + b"".join(struct.pack(">II", lo, hi) for lo, hi in tok.h))
    raw = body + _sum(body)
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(text: str) -> JoinToken:
    """Разобрать строку токена; не наш, повреждён, другой версии — TokenError."""
    s = str(text or "").strip()
    if not s.startswith(PREFIX):
        raise TokenError("это не команда подключения")
    b64 = s[len(PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,400}", b64):
        raise TokenError("команда подключения повреждена")
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except (ValueError, TypeError) as e:
        raise TokenError("команда подключения повреждена") from e
    if len(raw) < 2 + 4 + 8 + 4 + 1 + _SUM:
        raise TokenError("команда подключения повреждена")
    body, got = raw[:-_SUM], raw[-_SUM:]
    import hmac
    if not hmac.compare_digest(_sum(body), got):
        raise TokenError("команда подключения повреждена")
    if body[0] != FORMAT:
        raise TokenError("команда подключения другого формата — обнови установщик")
    try:
        slot = body[1]
        version = _version_str(body[2:6])
        join_id = body[6:14].hex()
        (expires,) = struct.unpack(">I", body[14:18])
        hlen = body[18]
        host = body[19:19 + hlen].decode("ascii")
        p = 19 + hlen
        link_port, channel_port = struct.unpack(">HH", body[p:p + 4]); p += 4
        gw_addr = str(ipaddress.IPv4Address(body[p:p + 4])); p += 4
        vps_pub, gw_priv = body[p:p + 32], body[p + 32:p + 64]; p += 64
        jc, jmin, jmax = body[p:p + 3]; p += 3
        s4 = tuple(body[p:p + 4]); p += 4
        hs = tuple(struct.unpack(">II", body[p + 8 * i:p + 8 * i + 8]) for i in range(4)); p += 32
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as e:
        raise TokenError("команда подключения повреждена") from e
    if p != len(body) or len(vps_pub) != 32 or len(gw_priv) != 32 or not _HOST_RE.match(host) or not slot:
        raise TokenError("команда подключения повреждена")
    return JoinToken(slot=slot, version=version, join_id=join_id, expires=expires, host=host,
                     link_port=link_port, channel_port=channel_port, gw_addr=gw_addr, vps_pub=vps_pub,
                     gw_priv=gw_priv, jc=jc, jmin=jmin, jmax=jmax, s=s4, h=hs)


def find(text: str) -> str:
    """Токен из текста (команда целиком, токен в кавычках): первая строка вида
    `AWGJ1.…` или пусто."""
    m = re.search(r"AWGJ1\.[A-Za-z0-9_-]{40,400}", str(text or ""))
    return m.group(0) if m else ""
