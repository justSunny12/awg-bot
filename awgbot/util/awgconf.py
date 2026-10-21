"""
awgconf.py — чистый разбор текста конфига AmneziaWG (без сервера и subprocess):
ключи обфускации, параметр из [Interface], разбиение на заголовок и пиры.
Нижний слой: им пользуются и infra/awg (с подменой в тестах через pkg), и
util/rsvpkg (часть VPN пакета переезда).
"""
from __future__ import annotations

import re
from typing import Optional

# Параметры, нужные для генерации клиентского конфига (обфускация).
OBFUSCATION_KEYS = [
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


def extract_param(conf_text: str, key: str) -> Optional[str]:
    """Значение `key = ...` из [Interface]. Учитывает закомментированные строки
    (I1-I5 в конфиге клиентского интерфейса — awg1 на хосте после переезда —
    закомментированы, но их значение нужно для клиента).
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
    for key in OBFUSCATION_KEYS:
        val = extract_param(conf_text, key)
        if val is not None:
            params[key] = val
    port = extract_param(conf_text, "ListenPort")
    if port:
        params["ListenPort"] = port
    return params


def split_conf(text: str) -> tuple[str, list[dict]]:
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
