"""
awg.py — единственный слой взаимодействия с контейнером AmneziaWG.

ВСЕ вызовы docker exec живут здесь и больше нигде. Каждая операция проверена
руками на Этапе 1 (разведка). Модуль делится на:
  • чистые парсеры (parse_*) — без контейнера, тестируются против реальных выводов;
  • функции, дёргающие контейнер (read_*, add_peer, block_ip, show_dump, ...).

Крипто-материал (обфускация, серверный pubkey, psk, ListenPort) читается ЖИВЫМ
из файлов контейнера — не хардкодится, чтобы переустановка сервера не ломала
конфиги молча.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time as _time
from contextlib import contextmanager
from typing import Optional

from awgbot.core import config
from awgbot.util import timeutil

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


def _exec(cont_args: list[str], **kw) -> subprocess.CompletedProcess:
    """docker exec <container> <cont_args...>"""
    return _run(["docker", "exec", config.CONTAINER, *cont_args], **kw)


def _exec_i(cont_args: list[str], input_data: bytes, **kw) -> subprocess.CompletedProcess:
    """docker exec -i <container> <cont_args...> — с подачей stdin."""
    return _run(
        ["docker", "exec", "-i", config.CONTAINER, *cont_args],
        input_data=input_data, **kw,
    )


def _exec_sh(script: str, **kw) -> subprocess.CompletedProcess:
    """docker exec <container> sh -c '<script>' — для пайпов/редиректов.
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
    _exec_i(["sh", "-c", f"cat > {path}"], input_data=content.encode())


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
    Источник занятых IP для аллокатора (учитывает app-устройства из приложения)."""
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
_params_cache: Optional[dict] = None
_params_cached_at: float = 0.0
PARAMS_TTL_SECONDS = 600


def invalidate_server_params() -> None:
    """Сброс кэша (зовёт вотчдог при внешнем изменении файлов)."""
    global _params_cache
    with _params_lock:
        _params_cache = None


def read_server_params(force: bool = False) -> dict:
    """Всё, что нужно генератору конфигов: обфускация + ListenPort (из awg0.conf),
    серверный pubkey, общий psk. Читается ЖИВЫМ из контейнера, но кэшируется:
    один docker exec на TTL/инвалидацию вместо трёх на каждую генерацию."""
    global _params_cache, _params_cached_at
    with _params_lock:
        if (not force and _params_cache is not None
                and _time.time() - _params_cached_at < PARAMS_TTL_SECONDS):
            return dict(_params_cache)

    # Один exec на три файла (вместо трёх cat)
    script = (f"cat {config.CONF_PATH}; printf '%s' '{_SEP}'; "
              f"cat {config.SERVER_PUBKEY_PATH}; printf '%s' '{_SEP}'; "
              f"cat {config.PSK_PATH}")
    out = _exec_sh(script).stdout.decode(errors="replace")
    parts = out.split(_SEP)
    if len(parts) != 3:
        raise AwgError("Не удалось прочитать серверные параметры (формат ответа)")
    conf, pubkey, psk = parts
    params = parse_interface_params(conf)
    result = {
        "obfuscation": {k: params.get(k, "") for k in _OBFUSCATION_KEYS},
        "listen_port": int(params.get("ListenPort", config.SERVER_PORT)),
        "server_pubkey": pubkey.strip(),
        "psk": psk.strip(),
    }
    with _params_lock:
        _params_cache = dict(result)
        _params_cached_at = _time.time()
    return result


def read_occupied_ips() -> set[str]:
    """Занятые IP из живого awg0.conf (для аллокатора)."""
    return parse_occupied_ips(read_file(config.CONF_PATH))


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
        cp = subprocess.run(
            ["docker", "exec", cont, "cat", config.CONF_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
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
    """priv → pub (для валидации при реставрации app-устройства)."""
    return _exec_i(
        ["awg", "pubkey"], input_data=(private_key.strip() + "\n").encode()
    ).stdout.decode().strip()


# ─────────────────────────────────────────────────────────────────────────────
# Применение конфига (syncconf, без рестарта — не рвёт активных)
# ─────────────────────────────────────────────────────────────────────────────

def apply_config() -> None:
    """awg syncconf awg0 <(awg-quick strip awg0.conf) — на живую.
    Через временный файл (не process substitution) → работает в любом shell.
    Предупреждение 'world accessible' от strip идёт в stderr и безвредно."""
    script = (
        f"awg-quick strip {config.CONF_PATH} > /tmp/awg_strip.conf 2>/dev/null && "
        f"awg syncconf {config.AWG_INTERFACE} /tmp/awg_strip.conf; "
        f"rc=$?; rm -f /tmp/awg_strip.conf; exit $rc"
    )
    _exec_sh(script)


def _backup_conf() -> None:
    _exec(["cp", config.CONF_PATH, config.CONF_BAK_PATH])


def _restore_conf() -> None:
    _exec(["cp", config.CONF_BAK_PATH, config.CONF_PATH])


# ─────────────────────────────────────────────────────────────────────────────
# Добавление / удаление пира (awg0.conf + syncconf, с откатом на .bak)
# ─────────────────────────────────────────────────────────────────────────────

def add_peer(public_key: str, psk: str, ip: str) -> None:
    """Добавляет [Peer] в awg0.conf и применяет. Идемпотентно (если pubkey уже
    есть — не дублирует). При ошибке применения восстанавливает .bak и поднимает
    AwgError — контейнер остаётся консистентным, откат БД делает services."""
    _validate_key(public_key)
    _validate_key(psk)
    _validate_ip(ip)

    with mutation_lock:
        conf = read_file(config.CONF_PATH)
        header, peers = _split_conf(conf)
        if any(p["pubkey"] == public_key for p in peers):
            return                                # уже есть — нечего делать
        peers.append(_peer_block(public_key, psk, ip))
        new_conf = _build_conf(header, peers)

        with writing():
            _backup_conf()
            write_file(config.CONF_PATH, new_conf)
            try:
                apply_config()
            except AwgError:
                _restore_conf()
                apply_config()                    # вернуть демон к прежнему состоянию
                raise


def remove_peer(public_key: str) -> None:
    """Убирает [Peer] с данным pubkey и применяет. Идемпотентно. Откат как в add_peer."""
    _validate_key(public_key)
    with mutation_lock:
        conf = read_file(config.CONF_PATH)
        header, peers = _split_conf(conf)
        new_peers = [p for p in peers if p["pubkey"] != public_key]
        if len(new_peers) == len(peers):
            return                                # не было — нечего делать
        new_conf = _build_conf(header, new_peers)

        with writing():
            _backup_conf()
            write_file(config.CONF_PATH, new_conf)
            try:
                apply_config()
            except AwgError:
                _restore_conf()
                apply_config()
                raise


# ─────────────────────────────────────────────────────────────────────────────
# clientsTable — нативная запись для приложения Amnezia (некритично для VPN)
# ─────────────────────────────────────────────────────────────────────────────

def read_clients_table() -> list[dict]:
    raw = read_file(config.CLIENTS_TABLE_PATH).strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def clientstable_upsert(public_key: str, name: str) -> None:
    """Добавляет/обновляет запись устройства в clientsTable в формате приложения
    (clientId=pubkey, clientName=name, creationDate в стиле Amnezia/UTC+3)."""
    table = read_clients_table()
    for entry in table:
        if entry.get("clientId") == public_key:
            entry.setdefault("userData", {})["clientName"] = name
            break
    else:
        table.append({
            "clientId": public_key,
            "userData": {
                "clientName": name,
                "creationDate": timeutil.amnezia_date(),
            },
        })
    _write_table(table)


def _write_table(table: list) -> None:
    with mutation_lock, writing():
        write_file(config.CLIENTS_TABLE_PATH,
                   json.dumps(table, indent=4, ensure_ascii=False) + "\n")


def clientstable_remove(public_key: str) -> None:
    table = read_clients_table()
    new_table = [e for e in table if e.get("clientId") != public_key]
    if len(new_table) != len(table):
        _write_table(new_table)


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
# Статистика
# ─────────────────────────────────────────────────────────────────────────────

def show_dump() -> list[dict]:
    """awg show awg0 dump → распарсенный список пиров."""
    out = _exec(["awg", "show", config.AWG_INTERFACE, "dump"]).stdout.decode(errors="replace")
    return parse_dump(out)


# ─────────────────────────────────────────────────────────────────────────────
# Статус контейнера / мониторинг
# ─────────────────────────────────────────────────────────────────────────────

def _inspect(fmt: str) -> str:
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
    _run(["docker", "restart", config.CONTAINER], timeout=60)


__all__ = [
    "AwgError", "ContainerDown", "writing", "is_writing", "last_self_write",
    "mutation_lock", "invalidate_server_params",
    "read_file", "write_file",
    "parse_interface_params", "parse_occupied_ips", "parse_dump",
    "read_server_params", "read_occupied_ips", "detect_topology",
    "gen_keypair", "pubkey_of",
    "apply_config", "add_peer", "remove_peer",
    "read_clients_table", "clientstable_upsert", "clientstable_remove",
    "block_ip", "unblock_ip", "is_blocked",
    "show_dump",
    "container_running", "container_pid", "container_started_at",
    "awg_responding", "restart_container",
]
