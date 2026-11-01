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

Вывод режима при коде 0 никто не пересказывает (процесс открытия — тоже):
строки «ВНИМАНИЕ» скрипта (NETMAP не встал, клон окна не поднялся, нет
conntrack) идут в журнал бота предупреждением здесь, остальное — отладкой.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from awgbot.core import config
from awgbot.util import fsatomic, kvfile

log = logging.getLogger(__name__)

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
HOOK_FOREIGN_FILE = RUN_DIR / "hook_foreign"    # отметка гостя: вебхук не здесь — Telegram у основы (переживает рестарт юнита)


def links_dir(data_dir=None) -> Path:
    """Стороны линков шлюзов основы — по текущему каталогу данных (не фиксируется
    при импорте: тесты и CLI подменяют DATA_DIR позже; вызывающий может передать
    свой config.DATA_DIR); окружение RSV_LINKS_DIR — переопределение."""
    return Path(os.environ.get("RSV_LINKS_DIR") or (Path(data_dir or config.DATA_DIR) / "reserve" / "links"))
GUEST_UNIT = "awg-bot-guest.service"
PHASES = ("idle", "taking", "serving", "relay")

_MB = 1024 * 1024
OPEN_MEM_FLOOR_MB = 192          # процесс открытия: прежний потолок — нижняя граница
GUEST_MEM_FLOOR_MB = 160         # гость: прежний потолок — нижняя граница (скрипт зоны ниже не ставит)
STORE_FLOOR_MB = 32              # хранилище гостя: прежний размер tmpfs — нижняя граница (скрипт зоны ниже не ставит)
STORE_CEIL_MB = 4096             # и верх: больше — заведомо мусор (база бота столько не весит)
PKG_MEM_WARN_BYTES = 64 * _MB    # база основы (без её размера — пакет) больше — карточка резерва предупреждает о памяти


def _mb_up(n: int) -> int:
    return -(-max(0, int(n)) // _MB)


def open_memory_mb(pkg_bytes: int, db_bytes: int = 0, rep_bytes: int = 0) -> int:
    """Потолок памяти процесса открытия (tools/rsvopen.py), не меньше прежних 192 МБ:
    128 МБ + 4 × (пакет + снимок `rep.bin`) + 2 × база.

    База — двоичная часть пакета (util/rsvpkg/seal, формат 2) и база снимка: в памяти
    она только сжатой, распаковка идёт потоком прямо в хранилище. Одновременно в памяти:
    файл пакета (и снимка) целиком, срез шифротекста двоичной части и её расшифровка
    (у NaCl — две копии) — до четырёх копий сжатого (пакет ≤ 16 МБ); база в хранилище
    (tmpfs числится в cgroup записавшего, 1 ×) и журнал SQLite поверх неё, когда
    процесс применяет журнал правок (до 1 ×). 128 МБ — интерпретатор с библиотеками,
    кэш SQLite журнала, части-JSON и прочее. `db_bytes` — размер базы основы (поле
    `db_size` слова `pkg`; снимок `rep.bin` новее пакета — больший из двух), 0 — не
    известен; `rep_bytes` — размер файла `rep.bin` (0 — снимка нет)."""
    return max(OPEN_MEM_FLOOR_MB, 128 + 4 * (_mb_up(pkg_bytes) + _mb_up(rep_bytes)) + 2 * _mb_up(db_bytes))


DB_SIZE_CEIL = STORE_CEIL_MB * _MB   # размер базы из слова `pkg` больше — заведомо мусор: не известен


def db_size_of(v) -> int:
    """Размер базы основы (несжатой) — из слова `pkg` или записи о пакете `rsv_pkg_in`;
    0 — не известен (пакет прежней основы, мусор)."""
    if isinstance(v, bool):
        return 0
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if 0 < n <= DB_SIZE_CEIL else 0


def store_mb(db_bytes: int) -> int:
    """Размер хранилища гостя (tmpfs `RUN_DIR`): до трёх копий базы сразу — `bot.db`,
    `bot.db.prev` (база, подменённая снимком `rep.bin` на старте гостя, остаётся рядом) и
    временная (`rsv-snap-*.db` — копия для снимка `rep.bin`; `.tmp` записи при подмене базы),
    плюс 16 МБ на WAL, списки РФ-доступа, конфиги и сертификат вебхука — 16 МБ + 3 × размер
    базы, не меньше прежних 32 и не больше 4096. `rep.bin` и журнал данных гостя — на диске
    (`VAR_DIR`), в хранилище их нет."""
    return min(STORE_CEIL_MB, max(STORE_FLOOR_MB, 16 + 3 * _mb_up(db_bytes)))


def guest_memory_mb(db_bytes: int) -> int:
    """Потолок памяти бота основы на резерве: процесс — 96 МБ и копия базы в памяти, плюс
    хранилище гостя целиком (`store_mb`): страницы tmpfs числятся в memory cgroup того, кто
    их записал, — всё, что гость пишет в хранилище (подмена базы, `.prev`, копия снимка, WAL),
    входит в MemoryMax его юнита и без свопа не вытесняется. 96 МБ + размер базы +
    `store_mb`, не меньше прежних 160."""
    return max(GUEST_MEM_FLOOR_MB, 96 + _mb_up(db_bytes) + store_mb(db_bytes))


def rep_sizes(path: Path | None = None) -> tuple[int, int]:
    """(размер файла `rep.bin`, размер его базы из открытого заголовка) — без ключа;
    снимка нет или не того формата — (0, 0)."""
    from awgbot.util.rsvpkg import seal
    p = path or REP_PATH
    try:
        with open(p, "rb") as f:
            head = f.read(64)
            size = os.fstat(f.fileno()).st_size
    except OSError:
        return 0, 0
    db = seal.snapshot_db_size(head)
    return (size, db) if db else (0, 0)


def guest_db_size(extra: int = 0) -> int:
    """Больший из размеров базы, с которой стартует гость: база в хранилище, база снимка
    `rep.bin` (гость подхватит его на старте) и `extra` (реплика, которую кладут сейчас)."""
    try:
        here = (RUN_DIR / "data" / "bot.db").stat().st_size
    except OSError:
        here = 0
    return max(int(here), rep_sizes()[1], int(extra or 0))


def guest_fit(extra: int = 0) -> int:
    """Перед стартом гостя ботом резерва: хранилище и потолок памяти юнита — по базе, с
    которой он стартует сейчас (`guest_db_size`), а не по той, что ушла в пакете:
    за обслуживание база растёт, а реплика при передаче бота и снимок `rep.bin` её
    несут. Хранилище перемонтируется размером `store_mb`, юнит — `--guest-mem` с
    `guest_memory_mb`. Сбой — строка в журнал, старт гостя не срывается (прежний
    потолок лучше, чем гость не поднят). Возвращает потолок, МБ (0 — не записан)."""
    db = guest_db_size(extra)
    mb = guest_memory_mb(db)
    try:
        store(store_mb(db))
        run("guest-mem", env={"GUEST_MEMORY_MB": str(mb)}, timeout=60)
    except ZoneError as e:
        log.warning("резерв: потолок памяти гостя по базе %d МБ не записан: %s", _mb_up(db), e)
        return 0
    log.info("резерв: потолок памяти гостя %d МБ (база %d МБ)", mb, _mb_up(db))
    return mb


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


DEFAULT_POOL = "169.254.96.0/19"                 # умолчание reserve.zone_pool — то же, что в conf/app.yaml и скрипте
_pool_told: set[str] = set()                     # битые значения пула, о которых журнал уже сказал


def _zone_pool() -> str:
    """Пул NETMAP из `reserve.zone_pool`: сеть IPv4 — как есть; битое значение (опечатка,
    IPv6) скрипту не передаётся — строка в журнал один раз на значение и умолчание:
    иначе план молча остался бы пустым, а причина «NETMAP не встал» — ложной."""
    import ipaddress
    from awgbot.core import settings
    raw = str(settings.get("app.reserve.zone_pool", DEFAULT_POOL) or DEFAULT_POOL).strip()
    try:
        net = ipaddress.ip_network(raw, strict=False)
        if net.version != 4:
            raise ValueError("не IPv4")
        return str(net)
    except ValueError as e:
        if raw not in _pool_told:
            _pool_told.add(raw)
            log.warning("резерв: reserve.zone_pool = %r не разобран (%s) — NETMAP подсетей клонов по умолчанию %s",
                        raw, e, DEFAULT_POOL)
        return DEFAULT_POOL


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
                "ZONE_TG_CIDR": str(settings.get("app.reserve.zone_tg_cidr", "169.254.77.8/30") or "169.254.77.8/30"),
                "ZONE_POOL": _zone_pool(),
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
    for line in out.splitlines():                        # код 0: предупреждения скрипта — в журнал, прочее — отладкой
        if line.startswith("ВНИМАНИЕ"):
            log.warning("резерв: зона --%s: %s", mode.lstrip("-"), line)
        elif line.strip():
            log.debug("резерв: зона --%s: %s", mode.lstrip("-"), line)
    return out


def plan() -> dict:
    """`--plan`: store, cake, netns, awg, webhook_port, zone — без root."""
    try:
        return kvfile.parse(run("plan", timeout=30))
    except ZoneError as e:
        return {"error": str(e)}


def status() -> dict:
    try:
        return kvfile.parse(run("status", timeout=30))
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


def store(size_mb: int = 0) -> str:
    """Смонтировать хранилище гостя (до записи в него); size_mb — его размер (`store_mb`):
    уже смонтированный tmpfs скрипт перемонтирует этим размером; 0 — умолчание скрипта
    (32 МБ), смонтированное не трогается."""
    return run("store", env={"GUEST_STORE_MB": str(int(size_mb))} if size_mb else None, timeout=60)


def take(conf_text: str, *, iface: str, addr: str, dns: list[str], upstreams: list[str], admin: list[str],
         channel_port: int = 0, extra: list[tuple[str, str]] | None = None, probe_addr: str = "",
         probe_port: int = 0, memory_mb: int = 0, store_size_mb: int = 0) -> str:
    """Клон из конфига (stdin), veth, dnsmasq и юниты гостя. extra — (имя, cidr)
    дополнительных интерфейсов основы (окно переезда профилей): их конфиги уже
    лежат в хранилище (`RUN_DIR/awg/<имя>.conf`), скрипт поднимает их следом.
    probe_addr — второй адрес клона (cidr) для ответчика кворума бота основы.
    memory_mb — потолок памяти юнита гостя (`guest_memory_mb`), store_size_mb — размер
    хранилища гостя (`store_mb`, скрипт перемонтирует tmpfs); 0 — умолчание скрипта."""
    env = {"GUEST_IF": iface, "GUEST_ADDR": addr, "GUEST_DNS": " ".join(dns), "GUEST_UPSTREAMS": " ".join(upstreams),
           "GUEST_ADMIN": " ".join(admin), "GUEST_EXTRA": " ".join(f"{n}={a}" for n, a in (extra or []))}
    if memory_mb:
        env["GUEST_MEMORY_MB"] = str(int(memory_mb))
    if store_size_mb:
        env["GUEST_STORE_MB"] = str(int(store_size_mb))
    if channel_port:
        env["GUEST_CHANNEL_PORT"] = str(int(channel_port))
    if probe_addr:
        import ipaddress
        env["GUEST_PROBE_ADDR"] = str(probe_addr)
        try:
            env["GUEST_PROBE_NET"] = str(ipaddress.ip_interface(probe_addr).network)
        except ValueError:
            env["GUEST_PROBE_NET"] = ""
        if probe_port:
            env["GUEST_PROBE_PORT"] = str(int(probe_port))
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
    """Файл хранилища гостя: 0600 с первого байта, атомарно, с fsync (util/fsatomic)."""
    fsatomic.write_atomic(path, data, mode)


def write_private_with(path: Path, fill, mode: int = 0o600):
    """То же, что write_private, но содержимое пишет `fill(f)` — потоком (база из пакета
    или снимка распаковывается прямо в файл, без копии в памяти). Возвращает то, что
    вернул `fill`; сбой — временный файл прочь, прежний на месте; rep.bin и пакет
    переживают крах хоста (fsync файла и каталога)."""
    return fsatomic.write_stream(path, fill, mode)


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
