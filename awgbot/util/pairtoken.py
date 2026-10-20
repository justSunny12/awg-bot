"""
pairtoken.py — приглашение в пару серверов (`AWGP1.<base64url>`), основной бот.

Пригласивший выпускает строку в своём боте и пересылает её человеком админу
второго сервера; тот вставляет её в «📥 Принять приглашение». По образцу
токена подключения шлюза (`util/jointoken.py`): в приглашении только то, без
чего не поднять линк пары под карантином — выпуск пригласившего (ранний отказ
по совместимости), id приглашения, срок, собственный адрес и порт линка пары
пригласившего, адрес принимающей стороны в /30, открытый ключ стороны
пригласившего, закрытый ключ принимающей стороны (сгенерирован под
приглашение), базовая обфускация линка, поколение AWG. Ни PSK, ни I1, ни
имени сервера, ни портов клиентов: всё это едет по каналу пары после верного
кода. Без кода приглашение бесполезно.

Двоичная запись (big-endian):

     1  версия формата (1)
     4  выпуск пригласившего: X, Y, Z, P
     8  id приглашения
     4  срок действия, unix
     1+ собственный адрес пригласившего: длина и ≤ 63 байт [A-Za-z0-9.-]
     2  порт линка пары у пригласившего
     4  адрес принимающей стороны в /30 линка пары
    32  открытый ключ стороны пригласившего    32  закрытый ключ принимающей стороны
     3  Jc, Jmin, Jmax                          4  S1–S4
    32  H1–H4 — нижняя и верхняя граница диапазона, uint32
     1  поколение AWG пригласившего
     4  контрольная сумма: BLAKE2b (4 байта) от всего выше
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import struct
from dataclasses import dataclass

PREFIX = "AWGP1."
FORMAT = 1
HOST_MAX = 63
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,63}\Z")
_SUM = 4
_FIXED = 1 + 4 + 8 + 4 + 1 + 2 + 4 + 32 + 32 + 3 + 4 + 32 + 1   # без хоста и суммы


class TokenError(ValueError):
    """Приглашение не разобралось: не наше, повреждено, другой версии формата."""


@dataclass(frozen=True)
class PairToken:
    version: str          # выпуск пригласившего, «4.0.0» или «4.0.0.1»
    pair_id: str          # 16 hex
    expires: int          # unix
    host: str             # собственный адрес пригласившего
    pair_port: int        # порт линка пары у пригласившего
    peer_addr: str        # адрес принимающей стороны в /30
    inviter_pub: bytes    # 32 байта
    peer_priv: bytes      # 32 байта
    jc: int
    jmin: int
    jmax: int
    s: tuple              # S1–S4
    h: tuple              # ((lo, hi) × 4)
    awg_gen: int

    @property
    def inviter_pub_b64(self) -> str:
        return base64.b64encode(self.inviter_pub).decode()

    @property
    def peer_priv_b64(self) -> str:
        return base64.b64encode(self.peer_priv).decode()

    @property
    def cidr(self) -> str:
        return str(ipaddress.ip_network(f"{self.peer_addr}/30", strict=False))

    @property
    def inviter_addr(self) -> str:
        """Адрес пригласившего — вторая адресуемая в /30 (у принявшего — первая)."""
        net = ipaddress.ip_network(f"{self.peer_addr}/30", strict=False)
        hosts = list(net.hosts())
        return str(hosts[1] if str(hosts[0]) == self.peer_addr else hosts[0])

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
        raise TokenError(f"выпуск {version!r} не укладывается в приглашение")
    parts += [0] * (4 - len(parts))
    return bytes(parts)


def _version_str(raw: bytes) -> str:
    x, y, z, p = raw
    return f"{x}.{y}.{z}" + (f".{p}" if p else "")


def _sum(body: bytes) -> bytes:
    return hashlib.blake2b(body, digest_size=_SUM).digest()


def encode(tok: PairToken) -> str:
    """Приглашение строкой `AWGP1.<base64url без =>`."""
    if not _HOST_RE.match(tok.host):
        raise TokenError("собственный адрес не укладывается в приглашение")
    host = tok.host.encode("ascii")
    if len(tok.inviter_pub) != 32 or len(tok.peer_priv) != 32:
        raise TokenError("ключ линка не 32 байта")
    pid = bytes.fromhex(tok.pair_id)
    if len(pid) != 8:
        raise TokenError("id приглашения — 8 байт")
    body = (struct.pack(">B", FORMAT) + _version_bytes(tok.version) + pid
            + struct.pack(">I", int(tok.expires)) + struct.pack(">B", len(host)) + host
            + struct.pack(">H", int(tok.pair_port))
            + ipaddress.IPv4Address(tok.peer_addr).packed + tok.inviter_pub + tok.peer_priv
            + struct.pack(">BBB", tok.jc, tok.jmin, tok.jmax) + struct.pack(">BBBB", *tok.s)
            + b"".join(struct.pack(">II", lo, hi) for lo, hi in tok.h)
            + struct.pack(">B", int(tok.awg_gen)))
    raw = body + _sum(body)
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(text: str) -> PairToken:
    """Разобрать строку приглашения; не наше, повреждено, другой версии — TokenError."""
    s = str(text or "").strip()
    if not s.startswith(PREFIX):
        raise TokenError("это не приглашение в пару")
    b64 = s[len(PREFIX):]
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,400}", b64):
        raise TokenError("приглашение повреждено")
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except (ValueError, TypeError) as e:
        raise TokenError("приглашение повреждено") from e
    if len(raw) < _FIXED + 1 + _SUM:
        raise TokenError("приглашение повреждено")
    body, got = raw[:-_SUM], raw[-_SUM:]
    if not hmac.compare_digest(_sum(body), got):
        raise TokenError("приглашение повреждено")
    if body[0] != FORMAT:
        raise TokenError("приглашение другого формата — обнови бот")
    try:
        version = _version_str(body[1:5])
        pair_id = body[5:13].hex()
        (expires,) = struct.unpack(">I", body[13:17])
        hlen = body[17]
        host = body[18:18 + hlen].decode("ascii")
        p = 18 + hlen
        (pair_port,) = struct.unpack(">H", body[p:p + 2]); p += 2
        peer_addr = str(ipaddress.IPv4Address(body[p:p + 4])); p += 4
        inviter_pub, peer_priv = body[p:p + 32], body[p + 32:p + 64]; p += 64
        jc, jmin, jmax = body[p:p + 3]; p += 3
        s4 = tuple(body[p:p + 4]); p += 4
        hs = tuple(struct.unpack(">II", body[p + 8 * i:p + 8 * i + 8]) for i in range(4)); p += 32
        awg_gen = body[p]; p += 1
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as e:
        raise TokenError("приглашение повреждено") from e
    if p != len(body) or len(inviter_pub) != 32 or len(peer_priv) != 32 or not _HOST_RE.match(host):
        raise TokenError("приглашение повреждено")
    return PairToken(version=version, pair_id=pair_id, expires=expires, host=host, pair_port=pair_port,
                     peer_addr=peer_addr, inviter_pub=inviter_pub, peer_priv=peer_priv,
                     jc=jc, jmin=jmin, jmax=jmax, s=s4, h=hs, awg_gen=awg_gen)


def find(text: str) -> str:
    """Приглашение из текста (пересланное сообщение целиком): первая строка
    вида `AWGP1.…` или пусто."""
    m = re.search(r"AWGP1\.[A-Za-z0-9_-]{40,400}", str(text or ""))
    return m.group(0) if m else ""
