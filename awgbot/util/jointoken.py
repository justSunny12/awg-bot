"""
jointoken.py — токен подключения шлюза (`AWGJ1.<base64url>`), обе роли.

Сервер выпускает его вместе с командой подключения; шлюз (`awg-bot join`,
установщик) разбирает. В токене — только
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

Разбор установщиком на bash (длина, версия, хост, id) — по тем же смещениям
(install/awg-bot-install.sh, join_parse); контрольную сумму сверяют и он
(`b2sum -l 32`, если есть в системе), и `awg-bot join`. Правка формата —
правка обоих разборов. Кадр (версия, base64url с префиксом и суммой, выпуск,
хост, Jc…H4) — общий с приглашением пары, util/tokenwire.py.
"""
from __future__ import annotations

import base64
import ipaddress
import struct
from dataclasses import dataclass

from awgbot.util import tokenwire as tw
from awgbot.util.tokenwire import FORMAT, TokenError

PREFIX = "AWGJ1."
_DAMAGED = "команда подключения повреждена"
_MIN_BODY = 2 + 4 + 8 + 4 + 1         # до хоста включая его длину


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
    def cidr(self) -> str:
        return str(ipaddress.ip_network(f"{self.gw_addr}/30", strict=False))

    def obfuscation(self) -> dict:
        """Jc…H4 строками — как их пишет скрипт линка."""
        return tw.obfuscation(self)


def encode(tok: JoinToken) -> str:
    """Токен строкой `AWGJ1.<base64url без =>`."""
    if not tw.host_ok(tok.host):
        raise TokenError("хост сервера не укладывается в токен")
    if len(tok.vps_pub) != 32 or len(tok.gw_priv) != 32:
        raise TokenError("ключ линка не 32 байта")
    jid = bytes.fromhex(tok.join_id)
    if len(jid) != 8:
        raise TokenError("id сопряжения — 8 байт")
    body = (struct.pack(">BB", FORMAT, int(tok.slot)) + tw.version_bytes(tok.version, "токен") + jid
            + struct.pack(">I", int(tok.expires)) + tw.pack_host(tok.host)
            + struct.pack(">HH", int(tok.link_port), int(tok.channel_port))
            + ipaddress.IPv4Address(tok.gw_addr).packed + tok.vps_pub + tok.gw_priv
            + tw.pack_obf(tok.jc, tok.jmin, tok.jmax, tok.s, tok.h))
    return tw.wrap(PREFIX, body)


def decode(text: str) -> JoinToken:
    """Разобрать строку токена; не наш, повреждён, другой версии — TokenError."""
    body = tw.unwrap(PREFIX, text, min_body=_MIN_BODY, not_ours="это не команда подключения", damaged=_DAMAGED,
                     newer="команда подключения другого формата — обнови установщик")
    try:
        slot = body[1]
        version = tw.version_str(body[2:6])
        join_id = body[6:14].hex()
        (expires,) = struct.unpack(">I", body[14:18])
        host, p = tw.unpack_host(body, 18)
        link_port, channel_port = struct.unpack(">HH", body[p:p + 4]); p += 4
        gw_addr = str(ipaddress.IPv4Address(body[p:p + 4])); p += 4
        vps_pub, gw_priv = body[p:p + 32], body[p + 32:p + 64]; p += 64
        jc, jmin, jmax, s4, hs, p = tw.unpack_obf(body, p)
    except (IndexError, struct.error, UnicodeDecodeError, ValueError) as e:
        raise TokenError(_DAMAGED) from e
    if p != len(body) or len(vps_pub) != 32 or len(gw_priv) != 32 or not tw.host_ok(host) or not slot:
        raise TokenError(_DAMAGED)
    return JoinToken(slot=slot, version=version, join_id=join_id, expires=expires, host=host,
                     link_port=link_port, channel_port=channel_port, gw_addr=gw_addr, vps_pub=vps_pub,
                     gw_priv=gw_priv, jc=jc, jmin=jmin, jmax=jmax, s=s4, h=hs)


def find(text: str) -> str:
    """Токен из текста (команда целиком, токен в кавычках): первая строка вида
    `AWGJ1.…` или пусто."""
    return tw.find(PREFIX, text)
