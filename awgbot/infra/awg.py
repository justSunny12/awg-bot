"""
awg.py — единственный слой взаимодействия с сервером AmneziaWG.

ВСЕ вызовы docker exec живут здесь и больше нигде. Более того, они собраны в
одной функции `_exec`: способ доступа к awg — контейнер или прямо хост —
переключается config.AWG_RUNTIME, а не разбросан по вызовам (docs/ROADMAP.md,
шаг 2). Всё, что портировать ещё не успели, закрыто `_docker_only` и падает
громко, а не деградирует молча.

Каждая операция проверена руками на Этапе 1 (разведка). Модуль делится на:
  • чистые парсеры (parse_*) — без контейнера, тестируются против реальных выводов;
  • функции, дёргающие контейнер (read_*, add_peer, block_ip, show_dump, ...).

Крипто-материал (обфускация, серверный pubkey, psk, ListenPort) читается ЖИВЫМ
из файлов контейнера — не хардкодится, чтобы переустановка сервера не ломала
конфиги молча.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time as _time
from contextlib import contextmanager
from typing import Optional

from awgbot.core import config

log = logging.getLogger("awgbot.awg")

# ─────────────────────────────────────────────────────────────────────────────
# Подавление самозаписи: пока бот сам правит файлы Amnezia, вотчдог игнорирует
# события (иначе реконсиляция сработала бы на нашу же запись). После выхода из
# контекста фиксируем метку времени — страховочная mtime-сетка вотчдога
# использует её, чтобы не среагировать на хвост нашей записи.
# ─────────────────────────────────────────────────────────────────────────────

_writing = threading.Event()
_last_self_write: float = 0.0

# Сериализация read-modify-write конфига: без него два параллельных
# add_peer/remove_peer теряют изменения друг друга (последняя запись побеждает).
mutation_lock = threading.RLock()


@contextmanager
def writing():
    """Контекст «бот сейчас пишет файлы» — вотчдог проверяет is_writing()."""
    global _last_self_write
    _writing.set()
    try:
        yield
    finally:
        _last_self_write = _time.time()
        _writing.clear()


def is_writing() -> bool:
    return _writing.is_set()


def last_self_write() -> float:
    """Unix-время окончания последней собственной записи (для mtime-сетки)."""
    return _last_self_write

# ─────────────────────────────────────────────────────────────────────────────
# Исключения
# ─────────────────────────────────────────────────────────────────────────────

class AwgError(Exception):
    """Общая ошибка взаимодействия с контейнером."""


class ContainerDown(AwgError):
    """Контейнер не запущен или недоступен."""


# ─────────────────────────────────────────────────────────────────────────────
# Низкоуровневый запуск docker exec
# ─────────────────────────────────────────────────────────────────────────────

def _run(
    args: list[str],
    input_data: Optional[bytes] = None,
    timeout: int = 15,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Запускает произвольную команду (list, без shell). Возвращает CompletedProcess.
    При check=True и ненулевом коде — AwgError с stderr."""
    try:
        proc = subprocess.run(
            args,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise AwgError(f"Команда не найдена: {args[0]} ({e})")
    except subprocess.TimeoutExpired:
        raise AwgError(f"Таймаут команды: {' '.join(args[:4])}...")
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise AwgError(f"Ошибка команды {' '.join(args[:5])}: {err}")
    return proc


def in_container() -> bool:
    """Ходим ли мы в контейнер, или команды идут прямо на хосте.

    Функция, а не константа модуля: значение замёрзло бы на импорте, и тест,
    подменяющий config.AWG_RUNTIME, проверял бы не то, что выполняется в бою.
    """
    return config.AWG_RUNTIME == "docker"


class HostModeUnsupported(RuntimeError):
    """Операция ещё не портирована на host-режим (docs/ROADMAP.md, шаг 2).

    НЕ наследник AwgError, и это намеренно. Половина вызывающих глотает AwgError
    и продолжает с пустым результатом: container_pid → None, container_running →
    False, host_ssh_targets → частичный список. Унаследуй заслон от AwgError — и
    он утонул бы ровно в тех except'ах, ради которых поставлен, вернув ту самую
    молчаливую деградацию. Для SSH-фильтра это была бы дыра без единого признака.
    """


def _docker_only(what: str) -> None:
    """Заслон для того, что в host-режиме ещё не портировано."""
    if not in_container():
        raise HostModeUnsupported(
            f"{what}: в host-режиме не поддерживается — перенос ещё не сделан "
            f"(docs/ROADMAP.md, шаг 2)")


def _exec(cont_args: list[str], **kw) -> subprocess.CompletedProcess:
    """Команда на сервере awg: `docker exec <container> ...` или прямо на хосте.

    Единственный вход внутрь — через неё ходят и блокировки, и правка конфига, и
    условная маршрутизация (infra/routing.py::_cont). Поэтому переезд с
    контейнера на хост здесь и переключается, а не в двух десятках мест.
    """
    if in_container():
        return _run(["docker", "exec", config.CONTAINER, *cont_args], **kw)
    return _run(list(cont_args), **kw)


def _exec_i(cont_args: list[str], input_data: bytes, **kw) -> subprocess.CompletedProcess:
    """То же с подачей stdin."""
    if in_container():
        return _run(
            ["docker", "exec", "-i", config.CONTAINER, *cont_args],
            input_data=input_data, **kw,
        )
    return _run(list(cont_args), input_data=input_data, **kw)


def _exec_sh(script: str, **kw) -> subprocess.CompletedProcess:
    """`sh -c '<script>'` — для пайпов и редиректов.
    script собирается ТОЛЬКО из констант конфига и валидированных значений."""
    return _exec(["sh", "-c", script], **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение / запись файлов контейнера (root в контейнере игнорирует права файлов)
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    return _exec(["cat", path]).stdout.decode(errors="replace")


def write_file(path: str, content: str) -> None:
    """Пишет файл целиком через stdin (без heredoc → без риска инъекций).
    `cat > file` усекает существующий файл, сохраняя inode и права."""
    # Путь в кавычках. Он приходит из констант конфига, но конфиг — это yaml,
    # который правят руками: пробел или точка с запятой в awg_dir превратились бы
    # в исполнение произвольной команды от root. Кавычки стоят ноль.
    _exec_i(["sh", "-c", f'cat > "{path}"'], input_data=content.encode())


# ─────────────────────────────────────────────────────────────────────────────
# Валидация значений, попадающих в shell/конфиг
# ─────────────────────────────────────────────────────────────────────────────

_RE_PUBKEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")          # base64 32 байта
_RE_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _validate_key(key: str) -> str:
    if not _RE_PUBKEY.match(key):
        raise AwgError(f"Некорректный ключ: {key!r}")
    return key


def _validate_ip(ip: str) -> str:
    if not _RE_IP.match(ip):
        raise AwgError(f"Некорректный IP: {ip!r}")
    octets = ip.split(".")
    if any(not (0 <= int(o) <= 255) for o in octets):
        raise AwgError(f"Некорректный IP: {ip!r}")
    return ip


def _is_ipv4(ip: str) -> bool:
    """Не-бросающая проверка IPv4 (для отсеивания мусора из docker/ip-вывода)."""
    try:
        _validate_ip(ip)
        return True
    except AwgError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# ЧИСТЫЕ ПАРСЕРЫ (тестируются без контейнера)
# ─────────────────────────────────────────────────────────────────────────────

# Параметры, нужные для генерации клиентского конфига (обфускация).
_OBFUSCATION_KEYS = [
    "Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
    "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
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
        val = _extract_param(conf_text, key)
        if val is not None:
            params[key] = val
    port = _extract_param(conf_text, "ListenPort")
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
    name = iface_of(iface)
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
            name = iface_of(iface)
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
    Раньше его брали из wireguard_server_public_key.key — артефакта контейнера
    Amnezia, который живёт своей жизнью. Файл и конфиг могут разойтись: смена
    серверного ключа без правки файла разослала бы всем конфиги с ЧУЖИМ
    публичным ключом, и заметили бы это только при первом переподключении.
    Вывод из PrivateKey исключает расхождение по построению."""
    name = iface_of(iface)
    path = conf_path(name)
    with _params_lock:
        cached = _params_cache.get(name)
        if (not force and cached is not None
                and _time.time() - _params_cached_at.get(name, 0.0) < PARAMS_TTL_SECONDS):
            return dict(cached)

    # Один exec на оба файла (вместо двух cat). PSK общий на сервер, а не на
    # интерфейс: пиры создаёт бот, и ключ у них один.
    script = (f'cat "{path}"; printf \'%s\' \'{_SEP}\'; '
              f'cat "{config.PSK_PATH}"')
    out = _exec_sh(script).stdout.decode(errors="replace")
    parts = out.split(_SEP)
    if len(parts) != 2:
        raise AwgError("Не удалось прочитать серверные параметры (формат ответа)")
    conf, psk = parts
    priv = _extract_param(conf, "PrivateKey")
    if not priv:
        raise AwgError(f"В {path} нет PrivateKey — публичный ключ не вывести")
    params = parse_interface_params(conf)
    result = {
        "obfuscation": {k: params.get(k, "") for k in _OBFUSCATION_KEYS},
        "listen_port": int(params.get("ListenPort", config.SERVER_PORT)),
        "server_pubkey": pubkey_of(priv),
        "psk": psk.strip(),
    }
    with _params_lock:
        _params_cache[name] = dict(result)
        _params_cached_at[name] = _time.time()
    return result


def read_occupied_ips(iface: Optional[str] = None) -> set[str]:
    """Занятые IP из живого conf (для аллокатора). У каждого интерфейса своя
    подсеть, поэтому и занятость своя."""
    return parse_occupied_ips(read_file(conf_path(iface)))


def detect_topology(container: str | None = None) -> dict:
    """Автодетект топологии из ЖИВОГО awg0.conf в контейнере — для установщика,
    чтобы не спрашивать у пользователя то, что уже настроено в сервисе.

    Возвращает {"listen_port": int|None, "subnet_prefix": str|None,
    "subnet_cidr": str|None}. Любое поле None, если не удалось прочитать
    (контейнер лёг / ключа нет / бот ставится ДО awg) — тогда установщик
    спросит вручную. `container` переопределяет config.CONTAINER (нужно на
    этапе установки, когда config ещё не финализирован).
    """
    out: dict = {"listen_port": None, "subnet_prefix": None, "subnet_cidr": None}
    try:
        cont = container or config.CONTAINER
        # Не через _exec: установщик зовёт это ДО финализации конфига и передаёт
        # имя контейнера параметром, которого в config ещё нет.
        argv = (["docker", "exec", cont, "cat", config.CONF_PATH]
                if in_container() else ["cat", config.CONF_PATH])
        cp = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if cp.returncode != 0:
            return out
        conf = cp.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return out

    port = _extract_param(conf, "ListenPort")
    if port and port.isdigit():
        out["listen_port"] = int(port)

    # Address = 10.8.1.0/24 (серверный адрес интерфейса) → префикс + CIDR.
    addr = _extract_param(conf, "Address")
    if addr:
        first = addr.split(",")[0].strip()          # может быть список v4,v6
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
    """Генерирует пару (private, public) ОДНИМ exec (пайп внутри контейнера).
    Приватный ключ не касается диска и не светится в аргументах процесса."""
    script = ("priv=$(awg genkey); pub=$(printf '%s' \"$priv\" | awg pubkey); "
              "printf '%s\\n%s\\n' \"$priv\" \"$pub\"")
    out = _exec_sh(script).stdout.decode().strip().splitlines()
    if len(out) != 2:
        raise AwgError("Генерация ключей вернула неожиданный вывод")
    priv, pub = out[0].strip(), out[1].strip()
    _validate_key(pub)
    return priv, pub


def pubkey_of(private_key: str) -> str:
    """priv → pub."""
    return _exec_i(
        ["awg", "pubkey"], input_data=(private_key.strip() + "\n").encode()
    ).stdout.decode().strip()


# ─────────────────────────────────────────────────────────────────────────────
# Применение конфига (syncconf, без рестарта — не рвёт активных)
# ─────────────────────────────────────────────────────────────────────────────

def apply_config(iface: Optional[str] = None) -> None:
    """awg syncconf <if> <(awg-quick strip <if>.conf) — на живую.
    Через временный файл (не process substitution) → работает в любом shell.
    Предупреждение 'world accessible' от strip идёт в stderr и безвредно.

    Файл кладём РЯДОМ С КОНФИГОМ и с маской 077, а не в /tmp с фиксированным
    именем. В нём приватный ключ сервера, и редирект создал бы его с обычной
    маской — читаемым всеми. Пока это был /tmp контейнера (свой mount namespace,
    кроме root никого), риска не было; после переезда на хост тот же код стал
    ронять ключ в общий /tmp на каждое добавление устройства. Предсказуемое имя
    там же открывает подмену симлинком: редирект от root записал бы куда указано.
    Каталог AWG_DIR — 700 и наш.
    """
    name = iface_of(iface)
    script = (
        f'umask 077; tmp=$(mktemp "{config.AWG_DIR}/.strip.XXXXXX") || exit 1; '
        f'awg-quick strip "{conf_path(name)}" > "$tmp" 2>/dev/null && '
        f'awg syncconf "{name}" "$tmp"; '
        'rc=$?; rm -f "$tmp"; exit $rc'
    )
    _exec_sh(script)


def _backup_conf(iface: Optional[str] = None) -> None:
    path = conf_path(iface)
    _exec(["cp", path, path + ".bak"])


def _restore_conf(iface: Optional[str] = None) -> None:
    path = conf_path(iface)
    _exec(["cp", path + ".bak", path])


# ─────────────────────────────────────────────────────────────────────────────
# Добавление / удаление пира (awg0.conf + syncconf, с откатом на .bak)
# ─────────────────────────────────────────────────────────────────────────────

def add_peer(public_key: str, psk: str, ip: str, iface: Optional[str] = None) -> None:
    """Добавляет [Peer] в conf интерфейса и применяет. Идемпотентно (если pubkey
    уже есть — не дублирует). При ошибке применения восстанавливает .bak и
    поднимает AwgError — сервер остаётся консистентным, откат БД делает services."""
    _validate_key(public_key)
    _validate_key(psk)
    _validate_ip(ip)
    path = conf_path(iface)

    with mutation_lock:
        conf = read_file(path)
        header, peers = _split_conf(conf)
        if any(p["pubkey"] == public_key for p in peers):
            return                                # уже есть — нечего делать
        peers.append(_peer_block(public_key, psk, ip))
        new_conf = _build_conf(header, peers)

        with writing():
            _backup_conf(iface)
            write_file(path, new_conf)
            try:
                apply_config(iface)
            except AwgError:
                _restore_conf(iface)
                apply_config(iface)               # вернуть демон к прежнему состоянию
                raise


def remove_peer(public_key: str, iface: Optional[str] = None) -> None:
    """Убирает [Peer] с данным pubkey и применяет. Идемпотентно. Откат как в
    add_peer.

    Интерфейс здесь не формальность: во время переезда старый пир живёт в conf
    СТАРОГО интерфейса, и удаление по дефолтному пути оставило бы его работать —
    призрачный доступ у устройства, которое человек считает удалённым."""
    _validate_key(public_key)
    path = conf_path(iface)
    with mutation_lock:
        conf = read_file(path)
        header, peers = _split_conf(conf)
        new_peers = [p for p in peers if p["pubkey"] != public_key]
        if len(new_peers) == len(peers):
            return                                # не было — нечего делать
        new_conf = _build_conf(header, new_peers)

        with writing():
            _backup_conf(iface)
            write_file(path, new_conf)
            try:
                apply_config(iface)
            except AwgError:
                _restore_conf(iface)
                apply_config(iface)
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Блокировка трафика пира (механика истечения) — iptables в контейнере
# ─────────────────────────────────────────────────────────────────────────────

def block_ip(ip: str) -> None:
    """DROP в НАЧАЛО FORWARD (перед широким ACCEPT). Идемпотентно."""
    _validate_ip(ip)
    if is_blocked(ip):
        return
    _exec(["iptables", "-I", "FORWARD", "1", "-s", f"{ip}/32", "-j", "DROP"])


def unblock_ip(ip: str) -> None:
    """Снимает DROP. Идемпотентно."""
    _validate_ip(ip)
    if not is_blocked(ip):
        return
    _exec(["iptables", "-D", "FORWARD", "-s", f"{ip}/32", "-j", "DROP"])


def is_blocked(ip: str) -> bool:
    """iptables -C FORWARD ... — код 0 = правило есть."""
    _validate_ip(ip)
    proc = _exec(["iptables", "-C", "FORWARD", "-s", f"{ip}/32", "-j", "DROP"],
                 check=False)
    return proc.returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# Пер-пирный SSH-доступ к хосту (только для админских пиров) — iptables.
#
# В контейнере хост за MASQUERADE видит всех пиров одним bridge-IP и различать их
# не может; единственное место, где исходный 10.8.1.x ещё настоящий, — FORWARD
# контейнера (до маскарадинга). На хосте маскарадинга между пиром и sshd нет, но
# меняется другое: цель становится локальным адресом, а такие пакеты идут в
# INPUT. Точку врезки поэтому выбирает _ssh_hook_chain, а не константа.
#
# Правила держим в отдельной цепочке AWGBOT_SSH: реассертится в тех же точках,
# что блокировки (старт/тик/рестарт), с дифф-скипом — пересборка только при
# фактическом изменении набора правил.
# ─────────────────────────────────────────────────────────────────────────────
_SSH_CHAIN = "AWGBOT_SSH"


def _ssh_hook_chain() -> str:
    """Встроенная цепочка, из которой прыгаем в AWGBOT_SSH.

    Точка врезки определяется НЕ вкусом, а маршрутом пакета. SSH-к-хосту из
    туннеля — это пакет на адрес самой машины, и netfilter отдаёт такие в INPUT;
    в FORWARD ходит только транзит. Внутри контейнера цель (шлюз docker-сети) для
    него чужая, пакет действительно транзитный — там FORWARD и нужен.

    Пока это было захардкожено в FORWARD, на хосте фильтр стоял целиком мимо
    потока: цепочка собиралась, выглядела правильной, счётчики не двигались
    никогда. Ни одного признака поломки — просто SSH-гейт, которого нет."""
    return "FORWARD" if in_container() else "INPUT"


def host_ssh_targets() -> list[str]:
    """Адреса ХОСТА, по которым до него дотягивается трафик из туннеля: шлюзы всех
    docker-сетей контейнера (bridge-стороны) + внешний egress-IP хоста (на случай
    hairpin: full-tunnel пир стучит по публичному IP → NAT → тот же хост). Их и
    гейтим. Хостовые команды (docker/ip) — через _run, не docker exec."""
    targets: list[str] = []

    def _add(v: str) -> None:
        v = v.strip()
        if v and v not in targets and _is_ipv4(v):
            targets.append(v)

    if in_container():
        try:
            out = _run(["docker", "inspect", config.CONTAINER, "-f",
                        "{{range .NetworkSettings.Networks}}{{.Gateway}}\n{{end}}"]
                       ).stdout.decode(errors="replace")
            for line in out.splitlines():
                _add(line)
        except AwgError:
            pass
    else:
        # На хосте docker-сетей нет: из туннеля хост виден по адресу самого
        # awg-интерфейса. Молчаливо пропустить его нельзя — список сузился бы до
        # одного egress-адреса, и SSH оказался бы открыт с туннельного адреса
        # хоста без единого признака поломки. Поэтому здесь check=True.
        out = _run(["ip", "-4", "-o", "addr", "show", "dev", config.AWG_INTERFACE]
                   ).stdout.decode(errors="replace")
        for m in re.finditer(r"\binet\s+([0-9.]+)", out):
            _add(m.group(1))
    try:
        out = _run(["ip", "route", "get", "1.1.1.1"]).stdout.decode(errors="replace")
        m = re.search(r"\bsrc\s+([0-9.]+)", out)
        if m:
            _add(m.group(1))
    except AwgError:
        pass
    return targets


def ssh_reconcile(admin_ips: list[str], targets: list[str]) -> None:
    """Идемпотентно привести пер-пирный SSH-фильтр (цепочка AWGBOT_SSH) к
    желаемому виду: для каждого target — ACCEPT с адресов админских устройств на
    SSH-порт, затем DROP всем остальным. Прочий трафик проходит цепочку насквозь.

    Дифф-скип: желаемый набор правил сравнивается с текущим (`iptables -S`), и
    если совпадает — ничего не трогаем. Реассерт идёт каждый монитор-тик
    (3 мин), пересобирать цепочку вслепую — шум из docker exec и лишнее окно
    «пустой цепочки» между flush и refill.

    Пустой targets → фильтр не накладываем (не смогли определить адреса хоста —
    безопаснее ничего не трогать, чем повесить неверный DROP).

    Пустой admin_ips → тоже не трогаем, и по той же причине, только цена выше.
    Желаемое состояние выродилось бы в одни DROP-и: SSH-к-хосту из туннеля
    закрыт всем, включая того, кто пришёл бы это чинить. Пустым список бывает
    в переходных состояниях — админ ещё без устройств, БД читается в момент
    пересоздания, — то есть ровно тогда, когда доступ нужнее всего. Охрана на
    targets тут стояла с самого начала, а на admin_ips её не было."""
    if not targets:
        return
    if not admin_ips:
        log.warning("ssh_reconcile: пустой список админских адресов — "
                    "фильтр не трогаем, иначе закрыли бы SSH всем")
        return
    port = str(config.SSH_PORT)
    iface = config.AWG_INTERFACE
    valid_ips = []
    for ip in admin_ips:
        try:
            _validate_ip(ip)
            valid_ips.append(ip)
        except AwgError:
            continue
    valid_targets = [t for t in targets if _is_ipv4(t)]

    # желаемое содержимое цепочки — в нотации `iptables -S` (как её печатает
    # iptables-nft: -s/-d с /32, протокол и до, и после -d)
    desired: list[str] = []
    for tgt in valid_targets:
        for ip in valid_ips:
            desired.append(f"-A {_SSH_CHAIN} -s {ip}/32 -d {tgt}/32 -i {iface} "
                           f"-p tcp -m tcp --dport {port} -j ACCEPT")
    for tgt in valid_targets:
        desired.append(f"-A {_SSH_CHAIN} -d {tgt}/32 -i {iface} "
                       f"-p tcp -m tcp --dport {port} -j DROP")

    # текущее состояние: -S <chain> (код ≠ 0 = цепочки нет)
    cur_proc = _exec(["iptables", "-S", _SSH_CHAIN], check=False)
    current = [ln.strip() for ln in
               cur_proc.stdout.decode(errors="replace").splitlines()
               if ln.startswith("-A ")] if cur_proc.returncode == 0 else None

    hook = _ssh_hook_chain()
    stale = "FORWARD" if hook == "INPUT" else "INPUT"
    jump_ok = _exec(["iptables", "-C", hook, "-j", _SSH_CHAIN],
                    check=False).returncode == 0
    stale_jump = _exec(["iptables", "-C", stale, "-j", _SSH_CHAIN],
                       check=False).returncode == 0
    if current == desired and jump_ok and not stale_jump:
        return                                       # состояние уже целевое

    # цепочка (может уже существовать — код 1, игнорируем)
    _exec(["iptables", "-N", _SSH_CHAIN], check=False)
    # джамп из начала hook-цепочки (перед широким ACCEPT подсети), если ещё нет
    if not jump_ok:
        _exec(["iptables", "-I", hook, "1", "-j", _SSH_CHAIN])
    # джамп из ПРЕЖНЕЙ точки врезки убираем: после смены режима он остаётся
    # висеть и показывает исправно выглядящую цепочку с нулевыми счётчиками —
    # ровно та картина, которая скрыла эту ошибку в прошлый раз.
    if stale_jump:
        _exec(["iptables", "-D", stale, "-j", _SSH_CHAIN], check=False)
    # пересобрать содержимое: ACCEPT-и админам, затем DROP-и всем
    _exec(["iptables", "-F", _SSH_CHAIN])
    for tgt in valid_targets:
        for ip in valid_ips:
            _exec(["iptables", "-A", _SSH_CHAIN, "-i", iface, "-s", f"{ip}/32",
                   "-d", tgt, "-p", "tcp", "--dport", port, "-j", "ACCEPT"])
    for tgt in valid_targets:
        _exec(["iptables", "-A", _SSH_CHAIN, "-i", iface,
               "-d", tgt, "-p", "tcp", "--dport", port, "-j", "DROP"])


# Fail-closed: пер-пирный фильтр держит бот, но между подъёмом awg0 и реассертом
# бота (до тика/если бот лежит) интерфейс уже принимает пиров, а цепочки ещё
# нет → широкий ACCEPT пускает всех на :22. Закрываем это на самом awg0:
# отдельная PostUp-строка ставит ГЛУХОЙ DROP на SSH-к-хосту в момент подъёма
# интерфейса (до бота). Врезка — в ту же цепочку, что и у бота (_ssh_hook_chain):
# разойдись они, страж стоял бы не там, где фильтр. ACCEPT'ы админам добавит бот на реассерте — то есть по
# умолчанию закрыто, открывается только для админских пиров.
#
# Почему это безопасно для подъёма интерфейса: awg-quick прерывает bringup, если
# команда PostUp вернула ненулевой код. Поэтому строка сконструирована так, что
# ВСЕГДА завершается 0 (все iptables — под `|| true`, финал — `; true`), а нет
# `ip`/`awk` → GW пустой → DROP просто не ставится, без ошибки. И `apply_config`
# (awg syncconf через `awg-quick strip`) PostUp вырезает — на правках пиров эта
# строка не исполняется, только при старте контейнера.
_SSH_FAILSAFE_MARK = _SSH_CHAIN            # наличие в header = строка уже вставлена


def _ssh_failsafe_postup() -> str:
    i = config.AWG_INTERFACE
    p = str(config.SSH_PORT)
    hook = _ssh_hook_chain()
    head = (f'iptables -N {_SSH_CHAIN} 2>/dev/null || true; '
            f'iptables -C {hook} -j {_SSH_CHAIN} 2>/dev/null || '
            f'iptables -I {hook} 1 -j {_SSH_CHAIN} 2>/dev/null || true; ')
    if in_container():
        # Внутри контейнера дефолтный шлюз — это адрес ХОСТА, каким контейнер
        # его видит. Он и есть цель, которую надо закрыть.
        return (
            'PostUp = GW="$(ip route 2>/dev/null | awk \'/^default/{print $3; exit}\')"; '
            + head +
            f'[ -n "$GW" ] && {{ iptables -C {_SSH_CHAIN} -i {i} -d "$GW" -p tcp '
            f'--dport {p} -j DROP 2>/dev/null || iptables -A {_SSH_CHAIN} -i {i} '
            f'-d "$GW" -p tcp --dport {p} -j DROP 2>/dev/null; }}; true'
        )
    # На хосте дефолтный шлюз — вышестоящий роутер провайдера, а вовсе не мы.
    # Тот же трюк здесь встал бы, вернул ноль, выглядел на месте — и не закрыл
    # бы ничего: fail-closed стал бы fail-open без единого признака. Поэтому
    # цель задаётся не адресом, а признаком: любой ЛОКАЛЬНЫЙ адрес этой машины.
    # Так покрываются и адрес awg-интерфейса, и egress, и всё, что появится
    # позже, — угадывать конкретный адрес не нужно.
    rule = (f'-i {i} -m addrtype --dst-type LOCAL -p tcp --dport {p} -j DROP')
    return (
        'PostUp = ' + head +
        f'iptables -C {_SSH_CHAIN} {rule} 2>/dev/null || '
        f'iptables -A {_SSH_CHAIN} {rule} 2>/dev/null || true; true'
    )


def ensure_ssh_failsafe() -> bool:
    """Идемпотентно привести fail-closed PostUp в [Interface] awg0.conf к нужному
    виду. Сервис НЕ перезапускаем: строка вступит в силу при следующем
    естественном старте. Если Amnezia перегенерит конфиг и строку сотрёт — бот
    вернёт её на реассерте. Возвращает True, если что-то изменил.

    Сверяем СОДЕРЖИМОЕ, а не наличие маркера. Раньше проверялось только «есть ли
    в header слово AWGBOT_SSH», и любая уже стоящая строка считалась годной. Это
    молча консервировало две вещи: старый ssh_port после его смены в настройках
    и контейнерную форму правила после переезда на хост, где она встаёт без
    ошибки и не закрывает ничего. Оба случая выглядели как исправная защита.
    """
    want = _ssh_failsafe_postup()
    with mutation_lock:
        conf = read_file(config.CONF_PATH)
        header, peers = _split_conf(conf)
        lines = header.splitlines()
        ours = [n for n, ln in enumerate(lines)
                if ln.lstrip().startswith("PostUp") and _SSH_FAILSAFE_MARK in ln]
        if ours and all(lines[n].strip() == want for n in ours) and len(ours) == 1:
            return False
        # выкидываем все свои прежние варианты и ставим один актуальный
        kept = [ln for n, ln in enumerate(lines) if n not in set(ours)]
        header = "\n".join(kept).rstrip() + "\n" + want
        new_conf = _build_conf(header, peers)
        with writing():
            _backup_conf()
            write_file(config.CONF_PATH, new_conf)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Статистика
# ─────────────────────────────────────────────────────────────────────────────

def show_dump(iface: Optional[str] = None) -> list[dict]:
    """awg show <if> dump → распарсенный список пиров.

    Единственное место, где интерфейс нужен по существу: счётчики и хендшейки
    живут в ядре по интерфейсам, и опрос одного не увидит пиров другого."""
    out = _exec(["awg", "show", iface_of(iface), "dump"]).stdout.decode(errors="replace")
    return parse_dump(out)


# ─────────────────────────────────────────────────────────────────────────────
# Статус контейнера / мониторинг
# ─────────────────────────────────────────────────────────────────────────────

def _inspect(fmt: str) -> str:
    _docker_only("docker inspect")
    return _run(
        ["docker", "inspect", "-f", fmt, config.CONTAINER]
    ).stdout.decode().strip()


def container_running() -> bool:
    try:
        return _inspect("{{.State.Running}}") == "true"
    except AwgError:
        return False


def container_pid() -> Optional[int]:
    """PID главного процесса контейнера на хосте (для inotify через /proc/<PID>/root).
    Меняется при рестарте контейнера."""
    try:
        pid = _inspect("{{.State.Pid}}")
        return int(pid) if pid and pid != "0" else None
    except (AwgError, ValueError):
        return None


def container_started_at() -> Optional[str]:
    """StartedAt контейнера (ISO). Для детекта рестарта → реконсиляция блокировок."""
    try:
        return _inspect("{{.State.StartedAt}}") or None
    except AwgError:
        return None


def awg_responding() -> bool:
    """awg show awg0 отвечает без ошибки = демон жив."""
    try:
        _exec(["awg", "show", config.AWG_INTERFACE], check=True)
        return True
    except AwgError:
        return False


def restart_container() -> None:
    """docker restart <config.CONTAINER> (ТЗ 9.3 — перезапуск сервиса разрешён).
    ВНИМАНИЕ: после этого iptables-DROP'ы слетают → нужна реконсиляция блокировок."""
    _docker_only("restart_container")
    _run(["docker", "restart", config.CONTAINER], timeout=60)


# ─────────────────────────────────────────────────────────────────────────────
# То же самое без оглядки на способ запуска
# ─────────────────────────────────────────────────────────────────────────────

def watch_root() -> Optional[str]:
    """Каталог с файлами awg, каким его видит бот — для inotify.

    В контейнере файлы лежат в чужом mount namespace, добраться до них можно
    только через /proc/<PID>/root, и PID меняется при каждом рестарте. На хосте
    это просто AWG_DIR, который никуда не переезжает, — поэтому и вотчдог
    привязывается к ПУТИ, а не к PID: путь есть в обоих режимах, PID только в
    одном.
    """
    if not in_container():
        return config.AWG_DIR
    pid = container_pid()
    return f"/proc/{pid}/root{config.AWG_DIR}" if pid else None


def _boot_time_iso() -> Optional[str]:
    """Время загрузки системы в формате StartedAt (UTC, суффикс Z)."""
    try:
        with open("/proc/stat", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime "):
                    ts = int(line.split()[1])
                    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts))
    except (OSError, ValueError, IndexError):
        return None
    return None


def service_started_at() -> Optional[str]:
    """Когда стартовало то, что держит наши правила iptables.

    В контейнере это StartedAt: его рестарт обнуляет и DROP'ы блокировок, и
    SSH-цепочку. На хосте — время загрузки системы, потому что наши цепочки
    живут в хостовых таблицах и переживают всё, кроме перезагрузки: ни
    `awg-quick down/up`, ни рестарт бота их не трогают.

    Сигнал разный, смысл один: «состояние, которое мы поддерживаем, обнулилось —
    переналожи». Взять на хосте момент подъёма интерфейса было бы неверно: он
    меняется чаще, чем состояние теряется, и гонял бы реконсиляцию впустую.
    """
    return container_started_at() if in_container() else _boot_time_iso()


def restart_server() -> None:
    """Перезапуск сервиса AmneziaWG (ТЗ 9.3).

    ВНИМАНИЕ: в docker-режиме после этого слетают iptables-правила внутри
    контейнера → нужна реконсиляция блокировок. На хосте они не слетают, но
    реконсиляция всё равно безвредна и делается в вызывающем.
    """
    if in_container():
        _run(["docker", "restart", config.CONTAINER], timeout=60)
        return
    # down может честно не сработать, если интерфейса нет, — это не ошибка,
    # ради которой стоит не поднимать его обратно.
    _run(["awg-quick", "down", config.AWG_INTERFACE], check=False, timeout=60)
    _run(["awg-quick", "up", config.AWG_INTERFACE], timeout=60)


__all__ = [
    "AwgError", "ContainerDown", "in_container",
    "writing", "is_writing", "last_self_write",
    "mutation_lock", "invalidate_server_params",
    "iface_of", "conf_path",
    "read_file", "write_file",
    "parse_interface_params", "parse_occupied_ips", "parse_dump",
    "read_server_params", "read_occupied_ips", "detect_topology",
    "gen_keypair", "pubkey_of",
    "apply_config", "add_peer", "remove_peer",
    "block_ip", "unblock_ip", "is_blocked",
    "show_dump",
    "container_running", "container_pid", "container_started_at",
    "awg_responding", "restart_container",
    "watch_root", "service_started_at", "restart_server",
]
