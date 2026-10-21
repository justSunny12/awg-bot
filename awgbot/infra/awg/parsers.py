"""parsers.py — часть пакета awg (см. __init__): чистые парсеры awg0.conf и dump,
разбор и сборка conf по блокам пиров."""
from __future__ import annotations

import re

from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде
from awgbot.util import awgconf


# ─────────────────────────────────────────────────────────────────────────────
# ЧИСТЫЕ ПАРСЕРЫ (тестируются без сервера)
# ─────────────────────────────────────────────────────────────────────────────

# чистый разбор — в util/awgconf (им пользуется и часть VPN пакета переезда);
# здесь — прежние имена и обёртки с подменой через pkg для тестов
_OBFUSCATION_KEYS = awgconf.OBFUSCATION_KEYS
_extract_param = awgconf.extract_param


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


_split_conf = awgconf.split_conf


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
