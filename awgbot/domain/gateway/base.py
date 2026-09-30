"""base.py — общее для миксинов агента: замок применения, запуск команд с
таймаутом, дата-классы проверки и сводки.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field, fields

from awgbot.util import timeutil

log = logging.getLogger("awgbot.gateway")

# Notification переиспользуем клиентский: notifier один на обе роли.


import threading  # noqa: E402

# Бандл из чата и настройки из канала применяются одним и тем же скриптом через
# тот же юнит — вперемешку они переписывали бы юнит друг другу посреди прогона.
# Прочие рестарты юнита (тик, «Восстановить», SSH) идут через reassert_guarded:
# под этим замком без ожидания, отказ словами. Между процессами (ручной запуск,
# юнит, systemd-run) прогоны разводит flock самого скрипта обвязки.
_APPLY_LOCK = threading.Lock()


TIMEOUT_MARK = "не уложилось в"


def _run(argv: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    """subprocess.run с таймаутом, который не бросает: отказ по времени — тот
    же отказ (код 124, причина в stderr), иначе исключение из тика или хендлера
    роняло бы их молча."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, b"", f"{TIMEOUT_MARK} {timeout} с".encode())


def _bundle_argv(path: str) -> list[str]:
    """Скрипт применения — под systemd-run вне cgroup агента: таймаут Python
    убивает только sh, а apt под ним работал бы сиротой в нашем cgroup, и
    рестарт агента (обновление) убил бы его посреди dpkg. Нет systemd-run —
    как раньше."""
    import shutil
    if shutil.which("systemd-run"):
        return ["systemd-run", "--wait", "--collect", "--pipe", "--quiet",
                "--unit", f"awg-gw-apply-{int(time.time())}", "sh", path, "--apply"]
    return ["sh", path, "--apply"]


def _out(proc) -> str:
    return proc.stdout.decode(errors="replace")


@dataclass
class GwCheck:
    """Одна проверка доктора: имя, вердикт, деталь. ok=None — «нечем проверить»,
    и это ТРЕТЬЕ состояние, а не успех: спутать «не смог посмотреть» с «всё
    хорошо» — способ прозевать отказ ровно там, где смотреть перестали."""
    name: str
    ok: bool | None
    detail: str = ""
    group: str = ""                          # "lan" — локальная сеть без VPN: свой стрик, не критично


@dataclass
class GwStatus:
    """Снимок шлюза для панели. Сериализуется в state (gw_status) целиком:
    панель по /start рисуется из снимка последнего тика, а не гоняет пробы."""
    link_up: bool = False
    handshake_age: float | None = None      # секунд; None — хендшейка нет
    rx: int = 0                             # счётчики линка с момента подъёма
    tx: int = 0
    checks: list[GwCheck] = field(default_factory=list)   # монитор здоровья
    temp: float | None = None
    throttled: dict | None = None
    cpu: float | None = None
    ram: float | None = None
    ram_free_mb: int | None = None
    disk: float | None = None
    disk_free_gb: float | None = None
    smart: str | None = None                # "OK" / "FAIL" / None — не смотрели
    uptime_seconds: int | None = None
    hostname: str = ""
    server_name: str = ""                   # имя ВПС для «Линк до …»
    module_version: str = ""
    srcversion: str = ""
    kernels_missing: list[str] = field(default_factory=list)
    kernels_total: int = 0
    month_rx: int = 0                       # потребление линка за календарный месяц
    month_tx: int = 0
    egress_ms: float | None = None          # выход наружу через канал квартиры, мс последнего замера
    egress_ok: bool | None = None           # он же вердиктом: улики или зонд
    egress_src: str = ""                    # чем доказан: трафик | проба | кэш
    tg_missing: list[str] = field(default_factory=list)   # диапазоны Telegram без маркировки
    mark_status: str = ""                   # шлюзовое устройство: confirmed|unmarked|foreign|unconfirmed
    lan: dict = field(default_factory=dict) # локальная сеть без VPN: пусто — выключена
    ssh: dict = field(default_factory=dict) # порт (факт), владелец, фильтр снаружи, адреса — для панели
    ts: str = ""                            # когда снят (ISO); пусто — живой

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "GwStatus":
        d = json.loads(raw)
        d["checks"] = [GwCheck(**c) for c in d.get("checks", [])]
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def age_seconds(self) -> float | None:
        if not self.ts:
            return None
        return max(0.0, (timeutil.now() - timeutil.parse_iso(self.ts)).total_seconds())





def pathlib_read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
