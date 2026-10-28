"""params.py — часть пакета awg (см. __init__): живые параметры сервера с кэшем по
интерфейсу, занятые адреса, автодетект топологии, генерация ключей."""
from __future__ import annotations

import subprocess
import threading
import time as _time
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError
from .parsers import _OBFUSCATION_KEYS


# ─────────────────────────────────────────────────────────────────────────────
# Чтение живых параметров сервера
# ─────────────────────────────────────────────────────────────────────────────

_SEP = "\n---AWGBOT-SEP---\n"          # разделитель для пакетного cat (в файлах не встречается)

# Кэш серверных параметров: обфускация/pubkey/psk/порт фактически статичны
# (меняются только при переустановке сервера). TTL — предохранитель, основная
# инвалидация — от вотчдога при внешнем изменении awg0.conf.
_params_lock = threading.Lock()
# Кэш серверных параметров — ПО ИНТЕРФЕЙСУ. Одиночного кэша хватало, пока
# интерфейс был один; на время переезда их два, и общий кэш отдавал бы параметры
# старого сервера для конфигов нового: чужой порт, чужой pubkey, чужая
# обфускация. Отказ при этом молчаливый — превью выглядит нормально, а не
# подключается никто.
_params_cache: dict[str, dict] = {}
_params_cached_at: dict[str, float] = {}
PARAMS_TTL_SECONDS = 600


def iface_of(iface: Optional[str] = None) -> str:
    """Имя интерфейса: пустое/None — интерфейс по умолчанию. Единая точка
    разрешения для всего кода, чтобы конвенция «пустое = дефолт» жила в одном
    месте, а не размножалась по вызовам."""
    return iface or config.AWG_INTERFACE


def conf_path(iface: Optional[str] = None) -> str:
    """Путь к conf интерфейса. Дефолтный берём из config.CONF_PATH — он может
    быть переопределён иначе, чем по шаблону."""
    name = pkg.iface_of(iface)
    return config.CONF_PATH if name == config.AWG_INTERFACE \
        else f"{config.AWG_DIR}/{name}.conf"


def invalidate_server_params(iface: Optional[str] = None) -> None:
    """Сброс кэша (зовёт вотчдог при внешнем изменении файлов). Без аргумента —
    все интерфейсы: вотчдог знает, что файл менялся, но не всегда какой."""
    with _params_lock:
        if iface is None:
            _params_cache.clear()
            _params_cached_at.clear()
        else:
            name = pkg.iface_of(iface)
            _params_cache.pop(name, None)
            _params_cached_at.pop(name, None)


def read_server_params(force: bool = False, iface: Optional[str] = None) -> dict:
    """Всё, что нужно генератору конфигов: обфускация + ListenPort, серверный
    pubkey, общий psk. Читается ЖИВЫМ с сервера, но кэшируется ПО ИНТЕРФЕЙСУ:
    один exec на TTL/инвалидацию вместо нескольких на каждую генерацию.

    Интерфейс обязателен по смыслу, хоть и необязателен по сигнатуре: параметры
    у каждого свои, и выдать конфиг нового пира со старым портом и старым
    серверным ключом — значит выдать нерабочий конфиг, который выглядит рабочим.

    Публичный ключ ВЫВОДИТСЯ из приватного, а не читается из файла рядом.
    Раньше его брали из wireguard_server_public_key.key — файла, который
    живёт своей жизнью. Файл и конфиг могут разойтись: смена
    серверного ключа без правки файла разослала бы всем конфиги с ЧУЖИМ
    публичным ключом, и заметили бы это только при первом переподключении.
    Вывод из PrivateKey исключает расхождение по построению."""
    name = pkg.iface_of(iface)
    path = pkg.conf_path(name)
    with _params_lock:
        cached = _params_cache.get(name)
        if (not force and cached is not None
                and _time.time() - _params_cached_at.get(name, 0.0) < PARAMS_TTL_SECONDS):
            return dict(cached)

    # Один exec на оба файла (вместо двух cat). PSK общий на сервер, а не на
    # интерфейс: пиры создаёт бот, и ключ у них один.
    script = (f'cat "{path}"; printf \'%s\' \'{_SEP}\'; '
              f'cat "{config.PSK_PATH}"')
    out = pkg._exec_sh(script).stdout.decode(errors="replace")
    parts = out.split(_SEP)
    if len(parts) != 2:
        raise AwgError("Не удалось прочитать серверные параметры (формат ответа)")
    conf, psk = parts
    priv = pkg._extract_param(conf, "PrivateKey")
    if not priv:
        raise AwgError(f"В {path} нет PrivateKey — публичный ключ не вывести")
    params = pkg.parse_interface_params(conf)
    result = {
        "obfuscation": {k: params.get(k, "") for k in _OBFUSCATION_KEYS},
        "listen_port": int(params.get("ListenPort", config.SERVER_PORT)),
        "server_pubkey": pkg.pubkey_of(priv),
        "psk": psk.strip(),
    }
    with _params_lock:
        _params_cache[name] = dict(result)
        _params_cached_at[name] = _time.time()
    return result


def read_occupied_ips(iface: Optional[str] = None) -> set[str]:
    """Занятые IP из живого conf (для аллокатора). У каждого интерфейса своя
    подсеть, поэтому и занятость своя."""
    return pkg.parse_occupied_ips(pkg.read_file(pkg.conf_path(iface)))


def detect_topology() -> dict:
    """Автодетект топологии из ЖИВОГО конфига интерфейса — для установщика,
    чтобы не спрашивать у пользователя то, что уже настроено в сервисе.

    Возвращает {"listen_port": int|None, "subnet_prefix": str|None,
    "subnet_cidr": str|None}. Любое поле None, если не удалось прочитать
    (конфига нет / ключа нет / бот ставится ДО awg) — тогда установщик
    спросит вручную.
    """
    out: dict = {"listen_port": None, "subnet_prefix": None, "subnet_cidr": None}
    try:
        # Не через _exec: установщик зовёт это ДО финализации конфига
        cp = subprocess.run(
            ["cat", config.CONF_PATH], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if cp.returncode != 0:
            return out
        conf = cp.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return out

    port = pkg._extract_param(conf, "ListenPort")
    if port and port.isdigit():
        out["listen_port"] = int(port)

    # Address = 10.8.1.0/24 (серверный адрес интерфейса) → префикс + CIDR.
    from awgbot.util import probenet
    # может быть список v4,v6; адрес блока зондов кворума (util/probenet) — не подсеть пользователей
    first = probenet.primary_addr(probenet.iface_addrs(conf))
    if first:
        ippart = first.split("/")[0]
        octs = ippart.split(".")
        if len(octs) == 4 and all(o.isdigit() for o in octs):
            out["subnet_prefix"] = ".".join(octs[:3])
            out["subnet_cidr"] = f"{'.'.join(octs[:3])}.0/24"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Генерация ключей
# ─────────────────────────────────────────────────────────────────────────────

def gen_keypair() -> tuple[str, str]:
    """Генерирует пару (private, public) ОДНИМ exec (пайп в sh).
    Приватный ключ не касается диска и не светится в аргументах процесса."""
    script = ("priv=$(awg genkey); pub=$(printf '%s' \"$priv\" | awg pubkey); "
              "printf '%s\\n%s\\n' \"$priv\" \"$pub\"")
    out = pkg._exec_sh(script).stdout.decode().strip().splitlines()
    if len(out) != 2:
        raise AwgError("Генерация ключей вернула неожиданный вывод")
    priv, pub = out[0].strip(), out[1].strip()
    pkg._validate_key(pub)
    return priv, pub


def pubkey_of(private_key: str) -> str:
    """priv → pub."""
    return pkg._exec_i(
        ["awg", "pubkey"], input_data=(private_key.strip() + "\n").encode()
    ).stdout.decode().strip()
