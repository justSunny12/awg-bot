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
имени сервера, ни портов пользователей: всё это едет по каналу пары после верного
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

Кадр (версия, base64url с префиксом и суммой, выпуск, хост, Jc…H4) — общий с
токеном подключения шлюза, util/tokenwire.py.
"""
from __future__ import annotations

import base64
import ipaddress
import struct
from dataclasses import dataclass

from awgbot.util import tokenwire as tw
from awgbot.util.tokenwire import FORMAT, TokenError

PREFIX = "AWGP1."
_DAMAGED = "приглашение повреждено"
_FIXED = 1 + 4 + 8 + 4 + 1 + 2 + 4 + 32 + 32 + tw.OBF_LEN + 1   # без хоста и суммы


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
        return tw.obfuscation(self)


def encode(tok: PairToken) -> str:
    """Приглашение строкой `AWGP1.<base64url без =>`."""
    if not tw.host_ok(tok.host):
        raise TokenError("собственный адрес не укладывается в приглашение")
    if len(tok.inviter_pub) != 32 or len(tok.peer_priv) != 32:
        raise TokenError("ключ линка не 32 байта")
    pid = bytes.fromhex(tok.pair_id)
    if len(pid) != 8:
        raise TokenError("id приглашения — 8 байт")
    body = (struct.pack(">B", FORMAT) + tw.version_bytes(tok.version, "приглашение") + pid
            + struct.pack(">I", int(tok.expires)) + tw.pack_host(tok.host)
            + struct.pack(">H", int(tok.pair_port))
            + ipaddress.IPv4Address(tok.peer_addr).packed + tok.inviter_pub + tok.peer_priv
            + tw.pack_obf(tok.jc, tok.jmin, tok.jmax, tok.s, tok.h)
            + struct.pack(">B", int(tok.awg_gen)))
    return tw.wrap(PREFIX, body)


def decode(text: str) -> PairToken:
    """Разобрать строку приглашения; не наше, повреждено, другой версии — TokenError."""
    body = tw.unwrap(PREFIX, text, min_body=_FIXED + 1, not_ours="это не приглашение в пару", damaged=_DAMAGED,
                     newer="приглашение другого формата — обнови бот")
    try:
        version = tw.version_str(body[1:5])
        pair_id = body[5:13].hex()
        (expires,) = struct.unpack(">I", body[13:17])
        host, p = tw.unpack_host(body, 17)
        (pair_port,) = struct.unpack(">H", body[p:p + 2]); p += 2
        peer_addr = str(ipaddress.IPv4Address(body[p:p + 4])); p += 4
        inviter_pub, peer_priv = body[p:p + 32], body[p + 32:p + 64]; p += 64
        jc, jmin, jmax, s4, hs, p = tw.unpack_obf(body, p)
        awg_gen = body[p]; p += 1
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as e:
        raise TokenError(_DAMAGED) from e
    if p != len(body) or len(inviter_pub) != 32 or len(peer_priv) != 32 or not tw.host_ok(host):
        raise TokenError(_DAMAGED)
    return PairToken(version=version, pair_id=pair_id, expires=expires, host=host, pair_port=pair_port,
                     peer_addr=peer_addr, inviter_pub=inviter_pub, peer_priv=peer_priv,
                     jc=jc, jmin=jmin, jmax=jmax, s=s4, h=hs, awg_gen=awg_gen)


def find(text: str) -> str:
    """Приглашение из текста (пересланное сообщение целиком): первая строка
    вида `AWGP1.…` или пусто."""
    return tw.find(PREFIX, text)
