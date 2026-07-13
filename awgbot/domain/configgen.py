"""
configgen.py — генерация клиентских конфигов и разбор vpn://.

Формат vpn:// доказан живым подключением телефона на Этапе 1:
    vpn:// + urlsafe_b64( [4 байта big-endian длины несжатого JSON] + zlib(JSON) )

Встроенный в vpn:// конфиг воспроизводит эталон приложения точно (без строки MTU —
приложение берёт mtu из JSON-поля). Отдельный .conf (файловый импорт) включает
MTU = 1376, т.к. там JSON-поля нет.

Обфускация/pubkey/psk/порт приходят ЖИВЫМИ из awg.read_server_params().
"""

from __future__ import annotations

import base64
import json
import zlib

from awgbot.core import config

# Порядок обфускейт-параметров, как в клиентском [Interface] (эталон приложения).
# Тот же набор из 16 ключей извлекается из сервера в awg._OBFUSCATION_KEYS — при
# правке синхронизировать оба (общего источника нет намеренно: здесь важен ПОРЯДОК
# вывода в конфиг, там — множество извлекаемых из сервера ключей).
_OBF_ORDER = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
              "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5"]


# ─────────────────────────────────────────────────────────────────────────────
# Построение клиентского .conf
# ─────────────────────────────────────────────────────────────────────────────

def _conf_text(
    private_key: str,
    address: str,
    obf: dict,
    server_pubkey: str,
    psk: str,
    host: str,
    port: int,
    include_mtu: bool,
) -> str:
    """Текст клиентского конфига. include_mtu=True — для отдельного .conf,
    False — для встроенного в vpn:// (точный эталон приложения)."""
    lines = [
        "[Interface]",
        f"Address = {address}/32",
        f"DNS = {config.DNS1}, {config.DNS2}",
        f"PrivateKey = {private_key}",
    ]
    if include_mtu:
        lines.append(f"MTU = {config.MTU}")
    for k in _OBF_ORDER:
        lines.append(f"{k} = {obf.get(k, '')}")
    lines += [
        "",
        "[Peer]",
        f"PublicKey = {server_pubkey}",
        f"PresharedKey = {psk}",
        f"AllowedIPs = {config.CLIENT_ALLOWED_IPS}",
        f"Endpoint = {host}:{port}",
        f"PersistentKeepalive = {config.KEEPALIVE_SECONDS}",
        "",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Построение JSON и кодирование vpn://
# ─────────────────────────────────────────────────────────────────────────────

def _build_last_config(
    private_key: str, public_key: str, address: str,
    obf: dict, server_pubkey: str, psk: str, host: str, port: int,
    embedded_conf: str,
) -> str:
    lc = {
        **{k: obf.get(k, "") for k in _OBF_ORDER},
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "clientId": public_key,
        "client_ip": address,
        "client_priv_key": private_key,
        "client_pub_key": public_key,
        "config": embedded_conf,
        "hostName": host,
        "mtu": str(config.MTU),
        "persistent_keep_alive": str(config.KEEPALIVE_SECONDS),
        "port": port,                       # int (как в эталоне)
        "psk_key": psk,
        "server_pub_key": server_pubkey,
    }
    return json.dumps(lc, indent=4, ensure_ascii=False)


def _build_vpn_json(
    private_key: str, public_key: str, address: str,
    obf: dict, server_pubkey: str, psk: str, host: str, port: int,
) -> dict:
    embedded_conf = _conf_text(
        private_key, address, obf, server_pubkey, psk, host, port,
        include_mtu=False,
    )
    last_config = _build_last_config(
        private_key, public_key, address, obf, server_pubkey, psk,
        host, port, embedded_conf,
    )
    awg_block = {
        **{k: obf.get(k, "") for k in _OBF_ORDER},
        "last_config": last_config,
        "port": str(port),
        "protocol_version": "2",
        "subnet_address": f"{config.SUBNET_PREFIX}.0",
        "transport_proto": "udp",
    }
    return {
        "containers": [{"awg": awg_block, "container": config.CONTAINER}],
        "defaultContainer": config.CONTAINER,
        "description": config.SERVER_NAME,
        "dns1": config.DNS1,
        "dns2": config.DNS2,
        "hostName": host,
    }


def encode_vpn(obj: dict) -> str:
    """dict → строка vpn:// (Qt qCompress формат: 4 байта длины + zlib)."""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    # Уровень 8: для декодирования на стороне приложения уровень не важен (zlib
    # самоописателен), фиксируем компактный уровень ради стабильного размера.
    compressed = zlib.compress(payload, 8)
    blob = len(payload).to_bytes(4, "big") + compressed
    return "vpn://" + base64.urlsafe_b64encode(blob).decode().rstrip("=")


def decode_vpn(link: str) -> dict:
    """Строка vpn:// → dict. Поднимает ValueError на мусоре."""
    link = link.strip()
    if not link.startswith("vpn://"):
        raise ValueError("Строка не начинается с vpn://")
    s = link[len("vpn://"):]
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        return json.loads(zlib.decompress(raw[4:]))
    except Exception as e:
        raise ValueError(f"Не удалось разобрать vpn://: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Главная точка: генерация обоих деливераблов
# ─────────────────────────────────────────────────────────────────────────────

def generate(
    private_key: str,
    public_key: str,
    address: str,
    server_params: dict,
) -> dict:
    """Собирает клиентский конфиг устройства.

    address — IP без маски (например «10.8.1.4»).
    server_params — из awg.read_server_params():
        {obfuscation: {...}, listen_port: int, server_pubkey: str, psk: str}

    Возвращает {"conf": <.conf текст с MTU>, "vpn": <vpn:// строка>}.
    """
    obf = server_params["obfuscation"]
    port = server_params["listen_port"]
    spub = server_params["server_pubkey"]
    psk = server_params["psk"]
    host = config.SERVER_HOST

    conf_standalone = _conf_text(
        private_key, address, obf, spub, psk, host, port, include_mtu=True,
    )
    vpn_obj = _build_vpn_json(
        private_key, public_key, address, obf, spub, psk, host, port,
    )
    return {"conf": conf_standalone, "vpn": encode_vpn(vpn_obj)}


# ─────────────────────────────────────────────────────────────────────────────
# Разбор vpn:// для реставрации app-устройства (извлечение приватного ключа)
# ─────────────────────────────────────────────────────────────────────────────

def classify_vpn_link(link: str) -> dict:
    """Разбирает vpn:// и КЛАССИФИЦИРУЕТ его. Возвращает:
      {"kind": "client", "client_priv_key", "client_pub_key", "client_ip"}
        — обычная клиентская ссылка (в last_config есть приватный ключ);
      {"kind": "full_access", "host", "user"}
        — ссылка ПОЛНОГО ДОСТУПА к серверу (SSH-креды root@host, параметры
        awg-туннеля, обфускация), приватного ключа клиента в ней НЕТ. Приложение
        по ней заходит на хост по SSH и поднимает туннель, сам генерируя ключ.
    Поднимает ValueError только на настоящем мусоре."""
    obj = decode_vpn(link)
    containers = obj.get("containers")
    awg0 = containers[0].get("awg", {}) if isinstance(containers, list) and containers else {}

    # клиентская: есть last_config с приватным ключом
    if "last_config" in awg0:
        try:
            lc = json.loads(awg0["last_config"])
            priv, pub, ip = lc["client_priv_key"], lc["client_pub_key"], lc["client_ip"]
        except (KeyError, json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"vpn:// не содержит ожидаемых полей: {e}")
        if not priv or not pub:
            raise ValueError("В vpn:// нет приватного/публичного ключа")
        return {"kind": "client", "client_priv_key": priv,
                "client_pub_key": pub, "client_ip": ip}

    # full-access: серверные креды + awg-параметры, но без ключа клиента
    if any(k in obj for k in ("hostName", "userName", "password")):
        return {"kind": "full_access",
                "host": obj.get("hostName", ""), "user": obj.get("userName", "")}

    raise ValueError("vpn:// не распознан: нет ни конфига устройства, ни доступа к серверу")



__all__ = [
    "encode_vpn", "decode_vpn", "generate", "classify_vpn_link",
]
