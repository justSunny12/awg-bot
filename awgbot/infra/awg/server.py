"""server.py — часть пакета awg (см. __init__): статистика пиров, статус
сервиса, каталог для вотчдога, перезапуск сервера."""
from __future__ import annotations

import re
import time as _time
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError


# ─────────────────────────────────────────────────────────────────────────────
# Статистика
# ─────────────────────────────────────────────────────────────────────────────

def show_dump(iface: Optional[str] = None) -> list[dict]:
    """awg show <if> dump → распарсенный список пиров.

    Единственное место, где интерфейс нужен по существу: счётчики и хендшейки
    живут в ядре по интерфейсам, и опрос одного не увидит пиров другого."""
    out = pkg._exec(["awg", "show", pkg.iface_of(iface), "dump"]).stdout.decode(errors="replace")
    return pkg.parse_dump(out)


# ─────────────────────────────────────────────────────────────────────────────
# Статус сервиса / мониторинг
# ─────────────────────────────────────────────────────────────────────────────

def awg_responding() -> bool:
    """awg show awg0 отвечает без ошибки = демон жив."""
    try:
        pkg._exec(["awg", "show", config.AWG_INTERFACE], check=True)
        return True
    except AwgError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# То же самое без оглядки на способ запуска
# ─────────────────────────────────────────────────────────────────────────────

def watch_root() -> Optional[str]:
    """Каталог с файлами awg — для inotify: AWG_DIR хоста."""
    return config.AWG_DIR


def _boot_time_iso() -> Optional[str]:
    """Время загрузки системы — ISO в UTC с суффиксом Z."""
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
    """Когда стартовало то, что держит наши правила iptables: время загрузки
    системы — наши цепочки живут в таблицах хоста и переживают всё, кроме
    перезагрузки: ни `awg-quick down/up`, ни рестарт бота их не трогают.
    Смысл метки: «состояние, которое мы поддерживаем, обнулилось — переналожи».
    Момент подъёма интерфейса не годится: он меняется чаще, чем состояние
    теряется, и гонял бы реконсиляцию впустую.
    """
    return pkg._boot_time_iso()


def restart_server() -> None:
    """Перезапуск AmneziaWG: awg-quick down/up. Правила iptables при этом не
    слетают, но реконсиляция блокировок в вызывающем безвредна.

    В гостевом режиме (бот основы на резерве) интерфейс — клон в сетевом
    пространстве зоны без awg-quick: конфиг клона без хуков и Table, адрес и
    маршрут клону ставила зона; перезапуск — вниз, setconf, вверх."""
    if config.GUEST:
        iface = config.AWG_INTERFACE
        try:
            from awgbot.infra import rsvzone
            rsvzone.restart_clone()                        # скрипт зоны: все клоны, setconf из текущих конфигов
            return
        except Exception as e:                             # noqa: BLE001 — скрипта нет (стенд) — ниже руками
            pkg.log.info("гость: перезапуск клона скриптом зоны не прошёл (%s) — сам", e)
        # setconf не знает расширений awg-quick (Address, MTU, DNS, Table) — файл
        # без них кладёт рядом скрипт зоны; нет его — отфильтровать самим
        setconf = config.CONF_PATH[:-5] + ".setconf" if config.CONF_PATH.endswith(".conf") else config.CONF_PATH + ".setconf"
        try:
            with open(config.CONF_PATH, encoding="utf-8") as f:
                text = f.read()
            kept = [ln for ln in text.splitlines() if not re.match(r"^\s*(Address|MTU|DNS|Table)\s*=", ln)]
            pkg.write_file(setconf, "\n".join(kept) + "\n")   # всегда из текущего .conf: пиры за время обслуживания
        except OSError:
            setconf = config.CONF_PATH
        pkg._run(["ip", "link", "set", iface, "down"], check=False, timeout=30)
        pkg._run(["awg", "setconf", iface, setconf], timeout=60)
        pkg._run(["ip", "link", "set", iface, "up"], timeout=30)
        return
    # down может честно не сработать, если интерфейса нет, — это не ошибка,
    # ради которой стоит не поднимать его обратно.
    pkg._run(["awg-quick", "down", config.AWG_INTERFACE], check=False, timeout=60)
    pkg._run(["awg-quick", "up", config.AWG_INTERFACE], timeout=60)
