"""
rsvzone.py — обёртка install/reserve-zone-setup.sh (гостевая зона резерва) и
пути хранилища гостя: как infra/resolver.py — скрипт делает, модуль зовёт и
читает состояние.

Состояние зоны (`/run/awg-bot/rsv/state.json`, без секретов) пишет скрипт;
бот читает его файервол хоста (порты клонов — `ports`, первый ещё и в `port`;
порт вебхука, пересылка хвоста по всем портам клонов), счётчики трафика
гостя (сумма по клонам из `ifaces`) и карточки. Клонов два, когда у основы
есть интерфейс окна переезда профилей: его конфиг процесс открытия кладёт в
хранилище сам, `take(extra=…)` передаёт скрипту только имя и адрес. Хранилище гостя (`/run/awg-bot-guest`, tmpfs в памяти) заполняет
процесс открытия (tools/rsvopen.py) — здесь только имена файлов, которые
знают обе стороны: ключ пакета, настройки вебхука, пул адресов Telegram,
отметка «право отвечать в Telegram — у гостя».

Пакеты зоны (`ZONE_TOOLS`: dnsmasq, conntrack) скрипт не ставит — их ставит
`install_tools` (apt-get, без вопросов); нет apt-get — ZoneError с тем, что
поставить руками.

Секретное в аргументы не кладётся никогда: конфиг клона — через stdin
скрипта, ключи — только в файлах хранилища (0600).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from awgbot.core import config

ZONE = os.environ.get("RSV_ZONE", "awg-guest")
RUN_DIR = Path(os.environ.get("RSV_RUN", "/run/awg-bot-guest"))
VAR_DIR = Path(os.environ.get("RSV_VAR", "/var/lib/awg-bot-guest"))
STATE_DIR = Path(os.environ.get("RSV_STATE_DIR", "/run/awg-bot/rsv"))
STATE_PATH = STATE_DIR / "state.json"
GUEST_SOCK = STATE_DIR / "guest.sock"           # локальный канал бот резерва ↔ бот основы на резерве
OPEN_STATUS = STATE_DIR / "open.status"         # ход процесса открытия (без секретов)
REP_PATH = VAR_DIR / "rep.bin"                  # шифрованный снимок реплики под K
K_FILE = RUN_DIR / "k"                          # ключ пакета в хранилище гостя (hex, 0600)
WEBHOOK_FILE = RUN_DIR / "webhook.json"         # порт, путь, секрет, сертификат вебхука
TG_POOL_FILE = RUN_DIR / "tg_pool.json"         # пул адресов Telegram для слушателя
TG_TAKE_FILE = RUN_DIR / "tg_take"              # отметка: ставить вебхук (факт записи)


def links_dir(data_dir=None) -> Path:
    """Стороны линков шлюзов основы — по текущему каталогу данных (не фиксируется
    при импорте: тесты и CLI подменяют DATA_DIR позже; вызывающий может передать
    свой config.DATA_DIR); окружение RSV_LINKS_DIR — переопределение."""
    return Path(os.environ.get("RSV_LINKS_DIR") or (Path(data_dir or config.DATA_DIR) / "reserve" / "links"))
GUEST_UNIT = "awg-bot-guest.service"
PHASES = ("idle", "taking", "serving", "relay")


ZONE_TOOLS = ("dnsmasq", "conntrack")            # пакеты, без которых зона не полна (резолвер зоны, сброс хвоста)


def install_tools(names: list[str], timeout: int = 900) -> str:
    """Поставить недостающие пакеты зоны (Debian/Ubuntu: apt-get, без вопросов).
    Нет apt-get — ZoneError с тем, что поставить руками."""
    import shutil
    names = [n for n in names if n in ZONE_TOOLS]
    if not names:
        return ""
    if not shutil.which("apt-get"):
        raise ZoneError("нет apt-get — поставь руками: " + " ".join(names))
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        upd = subprocess.run(["apt-get", "-q", "update"], capture_output=True, timeout=300, env=env)   # списки могли устареть
        proc = subprocess.run(["apt-get", "-q", "-y", "--no-install-recommends", "install", *names],
                              capture_output=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise ZoneError(f"apt-get не запустился: {e}") from e
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
        head = "списки пакетов не обновились (apt-get update) · " if upd.returncode != 0 else ""
        raise ZoneError("apt-get: " + head + (" · ".join(t.strip() for t in tail) or f"код {proc.returncode}"))
    return " ".join(names)


class ZoneError(RuntimeError):
    """Скрипт зоны отказал; текст — его последние строки."""


def _script() -> Path:
    return config.BASE_DIR / "install" / "reserve-zone-setup.sh"


def _zone_env() -> dict:
    """Подсети veth зоны, пул NETMAP и таблица РФ-доступа — из настроек, чтобы скрипт и бот не
    разошлись, если умолчания сменили."""
    # пути — те же константы, что у бота: скрипт не должен угадывать их по своим умолчаниям
    paths = {"RSV_ZONE": ZONE, "RSV_RUN": str(RUN_DIR), "RSV_VAR": str(VAR_DIR), "RSV_STATE_DIR": str(STATE_DIR),
             "RSV_LINKS_DIR": str(links_dir())}
    try:
        from awgbot.core import settings
        return {**paths, "ZONE_CIDR": str(settings.get("app.reserve.zone_cidr", "169.254.77.0/30") or "169.254.77.0/30"),
                "ZONE_RF_CIDR": str(settings.get("app.reserve.zone_rf_cidr", "169.254.77.4/30") or "169.254.77.4/30"),
                "ZONE_POOL": str(settings.get("app.reserve.zone_pool", "169.254.96.0/19") or "169.254.96.0/19"),
                "RF_TABLE": str(int(config.ROUTING_TABLE))}
    except Exception:                                     # noqa: BLE001 — настройки недоступны (CLI без conf)
        return paths


def run(mode: str, *, env: dict | None = None, stdin: str | None = None, timeout: int = 240) -> str:
    """Один режим скрипта (`--shell`, `--take`, …). Ненулевой код — ZoneError
    с хвостом вывода; `--status` и `--plan` возвращают текст как есть."""
    args = ["bash", str(_script()), f"--{mode.lstrip('-')}"]
    full_env = {**os.environ, **_zone_env(), **{k: str(v) for k, v in (env or {}).items()}}
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, env=full_env,
                              input=(stdin.encode() if stdin is not None else None))
    except (OSError, subprocess.SubprocessError) as e:
        raise ZoneError(f"скрипт зоны не запустился: {e}") from e
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        tail = "\n".join(out.splitlines()[-4:]) if out else f"код {proc.returncode}"
        raise ZoneError(tail)
    return out


def _kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def plan() -> dict:
    """`--plan`: store, cake, netns, awg, webhook_port, zone — без root."""
    try:
        return _kv(run("plan", timeout=30))
    except ZoneError as e:
        return {"error": str(e)}


def status() -> dict:
    try:
        return _kv(run("status", timeout=30))
    except ZoneError as e:
        return {"error": str(e)}


def state(path: Path | None = None) -> dict:
    """Состояние зоны из state.json; нет файла — пусто (phase=idle). Путь берётся в
    момент вызова (не при импорте) — подмена STATE_PATH в тестах доходит сюда."""
    try:
        data = json.loads((path or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def phase() -> str:
    p = str(state().get("phase") or "idle")
    return p if p in PHASES else "idle"


def zone_addr(cidr: str = "") -> str:
    """Адрес зоны в veth (второй хост /30): сюда DNAT вебхука."""
    import ipaddress
    cidr = cidr or str(state().get("zone_cidr") or "169.254.77.0/30")
    try:
        hosts = list(ipaddress.ip_network(cidr, strict=False).hosts())
        return str(hosts[1]) if len(hosts) >= 2 else ""
    except ValueError:
        return ""


def shell() -> str:
    """Зона-оболочка: пространство, таблица, стороны линков шлюзов основы из
    reserve/links/; порт гостевого канала (routing.link_channel_port) — во вход таблицы зоны."""
    from awgbot.core import settings
    port = int(settings.get_int("app.routing.link_channel_port", 8787) or 8787)
    return run("shell", env={"GUEST_CHANNEL_PORT": str(port)})


def store() -> str:
    """Смонтировать хранилище гостя (до записи в него)."""
    return run("store", timeout=60)


def take(conf_text: str, *, iface: str, addr: str, dns: list[str], upstreams: list[str], admin: list[str],
         channel_port: int = 0, extra: list[tuple[str, str]] | None = None) -> str:
    """Клон из конфига (stdin), veth, dnsmasq и юниты гостя. extra — (имя, cidr)
    дополнительных интерфейсов основы (окно переезда профилей): их конфиги уже
    лежат в хранилище (`RUN_DIR/awg/<имя>.conf`), скрипт поднимает их следом."""
    env = {"GUEST_IF": iface, "GUEST_ADDR": addr, "GUEST_DNS": " ".join(dns), "GUEST_UPSTREAMS": " ".join(upstreams),
           "GUEST_ADMIN": " ".join(admin), "GUEST_EXTRA": " ".join(f"{n}={a}" for n, a in (extra or []))}
    if channel_port:
        env["GUEST_CHANNEL_PORT"] = str(int(channel_port))
    return run("take", env=env, stdin=conf_text)


def serve(webhook_port: int) -> str:
    return run("serve", env={"WEBHOOK_PORT": str(int(webhook_port or 0) or "")})


def relay(to: str) -> str:
    return run("relay", env={"RELAY_TO": to})


def relay_flush() -> str:
    """Записи conntrack по портам клонов — прочь, когда DNAT хвоста уже в таблице
    хоста (иначе поток, пришедший между relay и перерисовкой, завёл бы запись без NAT)."""
    return run("relay-flush", timeout=60)


def teardown() -> str:
    return run("teardown")


def wipe() -> str:
    return run("wipe")


def guest_unit_active() -> bool:
    try:
        proc = subprocess.run(["systemctl", "is-active", GUEST_UNIT], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_private(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    """Файл хранилища гостя: 0600 с первого байта (создание под umask, затем
    chmod — окно, в котором файл читаем шире)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())                              # rep.bin и пакет переживают крах хоста
    except BaseException:
        os.unlink(str(tmp))
        raise
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def open_status_write(step: str, **fields) -> None:
    """Ход процесса открытия для бота резерва: шаг, время, причина отказа."""
    from awgbot.util import timeutil
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"step": step, "at": timeutil.now_iso(), **{k: str(v) for k, v in fields.items()}}
        tmp = OPEN_STATUS.with_name(OPEN_STATUS.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(OPEN_STATUS))
    except OSError:
        pass


def open_status() -> dict:
    return read_json(OPEN_STATUS)
