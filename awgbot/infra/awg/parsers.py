"""parsers.py — часть пакета awg (см. __init__): чистые парсеры awg0.conf и dump,
разбор и сборка conf по блокам пиров."""
from __future__ import annotations

import re
from typing import Optional

from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде


# ─────────────────────────────────────────────────────────────────────────────
# ЧИСТЫЕ ПАРСЕРЫ (тестируются без контейнера)
# ─────────────────────────────────────────────────────────────────────────────

# Параметры, нужные для генерации клиентского конфига (обфускация).
_OBFUSCATION_KEYS = [
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
    # Остальные ключи протокола — по списку из исходников приложения 5.0.1.5
    # (configKeys.h, awgProtocolKeys). Читаем ВСЕ, а не только нужные сегодня:
    # прочитанное пустым в конфиг не попадает, зато задай их однажды на сервере —
    # и клиенты получат их без правки кода. Не прочитать нужный означало бы
    # выдать конфиг, который молча не подключается.
    "HeaderProtectionKey", "ContentPaddingAddition",
    "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
    "KeepaliveTimeout", "MaxHandshakeAttempts",
    "RandomTrailers", "DisableCookies",
]


def _extract_param(conf_text: str, key: str) -> Optional[str]:
    """Значение `key = ...` из [Interface]. Учитывает закомментированные строки
    (I1-I5 в awg0.conf закомментированы, но их значение нужно для клиента).
    Возвращает строку значения (может быть пустой) или None, если ключа нет."""
    pattern = re.compile(rf"^[ \t]*#?[ \t]*{re.escape(key)}[ \t]*=[ \t]*(.*)$", re.MULTILINE)
    m = pattern.search(conf_text)
    if m is None:
        return None
    return m.group(1).strip()


def parse_interface_params(conf_text: str) -> dict:
    """Из [Interface] извлекает обфускацию (Jc..I5) + ListenPort.
    I2-I5 обычно пустые — сохраняем как ''. Отсутствующие Jc.. → пропускаем."""
    params: dict[str, str] = {}
    for key in _OBFUSCATION_KEYS:
        val = pkg._extract_param(conf_text, key)
        if val is not None:
            params[key] = val
    port = pkg._extract_param(conf_text, "ListenPort")
    if port:
        params["ListenPort"] = port
    return params


def parse_occupied_ips(conf_text: str) -> set[str]:
    """Все AllowedIPs пиров из awg0.conf → множество адресов без маски.
    Источник занятых IP для аллокатора (учитывает пиры, которых ещё нет в БД)."""
    ips: set[str] = set()
    for m in re.finditer(r"^\s*AllowedIPs\s*=\s*([\d.]+)/\d+", conf_text, re.MULTILINE):
        ips.add(m.group(1))
    return ips


def parse_dump(dump_text: str) -> list[dict]:
    """`awg show awg0 dump` → список пиров.

    Первая строка — интерфейс, пропускаем. Строки пиров разделены табами.
    Форматы (проверено в разведке):
      полный:  pub psk endpoint allowed_ips handshake(unix) rx tx keepalive
      краткий: pub psk (none)   allowed_ips keepalive          (не подключался)
    Различаем по тому, число ли в поле [4] (handshake) — устойчиво к длине строки.
    """
    peers: list[dict] = []
    lines = [ln for ln in dump_text.splitlines() if ln.strip()]
    for ln in lines[1:]:                      # [0] — интерфейс
        f = ln.split("\t")
        if len(f) < 4:
            continue
        pub = f[0]
        endpoint = f[2] if f[2] != "(none)" else None
        allowed = f[3]
        ip = allowed.split("/")[0] if allowed else None
        if len(f) >= 7 and f[4].isdigit():    # полный формат: есть handshake+rx+tx
            hs = int(f[4]) or None
            rx = int(f[5]) if f[5].isdigit() else 0
            tx = int(f[6]) if f[6].isdigit() else 0
        else:                                  # краткий: не подключался
            hs, rx, tx = None, 0, 0
        peers.append({
            "public_key": pub,
            "endpoint": endpoint,
            "allowed_ips": allowed,
            "address": ip,
            "last_handshake": hs,
            "rx": rx,
            "tx": tx,
        })
    return peers


def _split_conf(text: str) -> tuple[str, list[dict]]:
    """Разбивает awg0.conf на (header, peers).
    header — весь [Interface] вербатим (включая закомментированные I1-I5).
    peers — список {pubkey, lines[]} по каждому [Peer]-блоку.
    Пустые строки внутри/после блоков отбрасываются (нормализация)."""
    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip() == "[Peer]"), len(lines))
    header = "\n".join(lines[:idx]).rstrip()
    peers: list[dict] = []
    cur: Optional[dict] = None
    for l in lines[idx:]:
        s = l.strip()
        if s == "[Peer]":
            if cur is not None:
                peers.append(cur)
            cur = {"pubkey": None, "lines": ["[Peer]"]}
        elif cur is not None:
            if s == "":
                continue
            cur["lines"].append(l.rstrip())
            if s.startswith("PublicKey"):
                cur["pubkey"] = s.split("=", 1)[1].strip()
    if cur is not None:
        peers.append(cur)
    return header, peers


def _build_conf(header: str, peers: list[dict]) -> str:
    """Собирает awg0.conf обратно: header + по одному пустому разделителю между
    блоками + финальный перевод строки. Никаких тройных пустот в хвосте."""
    if not peers:
        return header.rstrip() + "\n"
    blocks = ["\n".join(p["lines"]) for p in peers]
    return header.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"


def _peer_block(pubkey: str, psk: str, ip: str) -> dict:
    return {
        "pubkey": pubkey,
        "lines": [
            "[Peer]",
            f"PublicKey = {pubkey}",
            f"PresharedKey = {psk}",
            f"AllowedIPs = {ip}/32",
        ],
    }
