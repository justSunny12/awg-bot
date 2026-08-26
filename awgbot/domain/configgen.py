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
# Порядок и написание сверены с ИСХОДНИКАМИ приложения 5.0.1.5 —
# client/core/utils/constants/configKeys.h, функция awgProtocolKeys(). Не с
# образцом конфига: образец показывает один случай, а список в коде — все.
_OBF_ORDER = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
              "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
              "HeaderProtectionKey", "ContentPaddingAddition",
              "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
              "KeepaliveTimeout", "MaxHandshakeAttempts",
              "RandomTrailers", "DisableCookies"]

# Ключи, которых у прежнего сервера нет вовсе. Приложение печатает их ТОЛЬКО
# непустыми (AwgClientConfig::toJson — `if (!x.isEmpty())` на каждом), и мы
# делаем так же: пустое значит «сервер их не задаёт».
#
# Отсюда главное свойство правки — инертность: пока серверный конфиг прежний,
# выдача побайтово та же, что была, и выкатывать её можно задолго до переезда.
# Появится ключ на сервере — поедет обеим формам сразу. Разъехаться им нельзя:
# половина людей импортирует ссылкой, половина файлом.
#
# DisableCookies сюда ВХОДИТ, хотя он односторонний и клиенту сам по себе не
# нужен. Довод не в пользе, а в точности: приложение кладёт его в клиентский
# конфиг, а формат обязан воспроизводить эталон. Вреда от него нет — клиент
# cookie-ответы и так почти не шлёт.
#
# I2–I5 остаются в списке обязательных: наш образец печатал их пустыми, и мы
# печатаем. Приложение 5.0.1.5 их при пустоте опускает, но читатель
# (fromJson → toString) отсутствующий ключ отдаёт пустой строкой, так что обе
# формы для него тождественны, и менять выдачу ради косметики незачем.
_OBF_OPTIONAL = {"HeaderProtectionKey", "ContentPaddingAddition",
                 "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
                 "KeepaliveTimeout", "MaxHandshakeAttempts",
                 "RandomTrailers", "DisableCookies"}


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
    """Текст клиентского конфига.

    include_mtu выбирает ВАРИАНТ, а не одну строку: True — отдельный .conf для
    файлового импорта, False — встроенный в vpn:// (точный эталон приложения).
    Отсюда два отличия, и оба следуют из варианта: строка MTU и обращение с
    пустыми обфускейт-полями.

    ПУСТЫЕ ОБФУСКЕЙТ-ПОЛЯ печатаются только во встроенный конфиг. Эталон
    приложения содержит `I2 = ` … `I5 = ` пустыми, телефон это глотает — а
    `awg setconf` на любом линуксовом клиенте падает с «Line unrecognized» и не
    поднимает интерфейс вовсе. То есть выданный ботом .conf не годился ни для
    роутера, ни для второго сервера, ни для шлюза. Отсутствие ключа и пустое
    значение для awg значат одно и то же, поэтому пропуск ничего не меняет по
    смыслу. Во встроенный конфиг они по-прежнему идут: формат vpn:// заморожен
    и обязан воспроизводить эталон байт в байт.
    """
    lines = [
        "[Interface]",
        f"Address = {address}/32",
        f"DNS = {config.DNS1}, {config.DNS2}",
        f"PrivateKey = {private_key}",
    ]
    if include_mtu:
        lines.append(f"MTU = {config.MTU}")
    for k in _OBF_ORDER:
        v = obf.get(k, "")
        empty = not str(v).strip()
        if empty and (include_mtu or k in _OBF_OPTIONAL):
            continue
        lines.append(f"{k} = {v}")
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

def _obf_fields(obf: dict) -> dict:
    """Обфускейт-поля для JSON-структуры vpn://.

    Ключи 3.1 с пустым значением ПРОПУСКАЕМ. Структура заморожена и обязана
    воспроизводить эталон приложения: пока сервер их не задаёт, лишняя пара
    `"RandomTrailers": ""` меняла бы ссылку у всех, ничего при этом не давая.
    Прочие пустые (I2–I5) эталон печатает, и их мы печатаем тоже.
    """
    return {k: obf.get(k, "") for k in _OBF_ORDER
            if not (k in _OBF_OPTIONAL and not str(obf.get(k, "")).strip())}


def _build_last_config(
    private_key: str, public_key: str, address: str,
    obf: dict, server_pubkey: str, psk: str, host: str, port: int,
    embedded_conf: str,
) -> str:
    lc = {
        **_obf_fields(obf),
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


def _subnet_of(address: str) -> str:
    """«10.9.1.2» → «10.9.1.0». Сервер держит нулевой адрес (квирк докерной
    Amnezia), так что это и есть его адрес в сети устройства."""
    return address.rsplit(".", 1)[0] + ".0"


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
        **_obf_fields(obf),
        "last_config": last_config,
        "port": str(port),
        "protocol_version": "2",
        # Подсеть берётся ИЗ АДРЕСА устройства, а не из глобальной настройки:
        # интерфейсов может быть два, и у второго своя подсеть. Глобальное
        # значение назвало бы переехавшему устройству чужую сеть — и назвало бы
        # навсегда, поле вморожено в выданную ссылку. Для старой подсети выдача
        # при этом побайтово прежняя: адрес оттуда даёт ровно её.
        "subnet_address": _subnet_of(address),
        "transport_proto": "udp",
    }
    # APP_CONTAINER, а НЕ CONTAINER: это идентификатор протокола для приложения,
    # а не имя docker-контейнера на сервере. Совпадают они исторически, и после
    # переезда на хост docker-имя исчезнет — но зашитое в выданные ссылки
    # значение обязано остаться прежним, иначе приложение перестанет опознавать
    # профили у всех разом.
    return {
        "containers": [{"awg": awg_block, "container": config.APP_CONTAINER}],
        "defaultContainer": config.APP_CONTAINER,
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


__all__ = [
    "encode_vpn", "decode_vpn", "generate",
]
