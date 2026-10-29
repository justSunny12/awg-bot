"""
openproc.py — часть пакета reserve (см. __init__): запуск процесса открытия
пакета (tools/rsvopen) на резерве — выделено из inbound (там автомат приёма).

Процесс идёт временным юнитом systemd-run с песочницей (`rsvopen_sandbox_props`):
память, PrivateTmp, ProtectSystem=strict, каталог данных подменён пустым и виден
только пакет второй стороны, запись — хранилище гостя, его каталог на диске,
состояние зоны и /etc/systemd. Хранилище гостя монтирует бот до запуска (в
пространстве хоста), секреты идут только в stdin, хвост stderr — в
`rsvopen.err` для причины отказа без итога.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from awgbot.core import config, settings

from ..types import ServiceError
from ..gateway_link.common import log

OPEN_UNIT = "awg-bot-rsvopen"


def rsvopen_sandbox_props(base: str) -> list[str]:
    """Свойства systemd-run для процесса открытия — одним списком, чтобы стенд
    запускал его ровно так же. Хранилище гостя и каталоги из ReadWritePaths
    должны существовать до запуска (`_rsvopen_launch` их создаёт)."""
    from awgbot.infra import rsvzone
    pkg = str(config.DATA_DIR / "reserve" / "guest.pkg")
    jrn = str(config.DATA_DIR / "reserve" / "jrn")
    return ["-p", "MemoryMax=192M", "-p", "MemorySwapMax=0", "-p", "LimitCORE=0", "-p", "Nice=5",
            "-p", "PrivateTmp=yes", "-p", "ProtectHome=yes", "-p", "ProtectSystem=strict",
            "-p", "NoNewPrivileges=yes",
            "-p", f"InaccessiblePaths=-{config.ENV_PATH or '/etc/awg-bot/env'}",
            "-p", f"WorkingDirectory={base}",
            # скрипт зоны пишет юниты и настройки журнала гостя в /etc/systemd
            "-p", f"ReadWritePaths=-{rsvzone.RUN_DIR} -{rsvzone.VAR_DIR} -{rsvzone.STATE_DIR} -/etc/systemd -/run/netns",
            # из каталога данных резерва процессу виден только пакет второй стороны и её
            # журнал (сегменты событий и пачки счётчиков под её K) — на чтение: ни база
            # резерва (и её WAL), ни стороны линков, ни копии
            "-p", f"TemporaryFileSystem={config.DATA_DIR}:ro",
            "-p", f"BindReadOnlyPaths=-{pkg} -{jrn}"]




class ReserveOpenProcMixin:
    def _open_payload(self, kind: str) -> dict:
        from awgbot.infra import nftguard, rsvzone
        from ..selfhost import own_host
        plan = rsvzone.plan()
        try:
            port = int(plan.get("webhook_port") or 0)
        except ValueError:
            port = 0
        from awgbot.infra import quoprobe
        return {"set_record": kind != "button", "tg_take": False, "webhook_port": port, "self_host": own_host(),
                "target_ip": self.reserve_own_ip(), "tg_pool": list(nftguard.tg_pool()),
                "channel_port": int(settings.get_int("app.routing.link_channel_port", 8787) or 8787),
                "probe_port": int(quoprobe.port())}

    def _rsvopen_launch(self, payload: dict, keep_stdin: bool = False) -> None:
        """Временный юнит с песочницей (systemd-run); без systemd — просто процесс.
        Секреты — только в stdin. Хранилище гостя монтирует бот — в пространстве
        хоста, до запуска юнита: у юнита с файловой песочницей своё пространство
        монтирования (slave), и tmpfs, смонтированный внутри него, хосту и
        юнитам гостя не виден. Каталоги из ReadWritePaths создаются заранее и
        помечены «-» — несуществующий путь без «-» роняет юнит (код 226).
        `keep_stdin` — переезд кнопкой: вход уходит первой строкой, труба
        остаётся открытой до конверта с ключом пакета (`_rsvopen_send`)."""
        from awgbot.infra import rsvzone
        base = str(config.BASE_DIR)
        argv = [os.path.join(base, "venv", "bin", "python"), "-m", "tools.rsvopen"]
        if not os.path.exists(argv[0]):
            argv[0] = "python3"
        try:
            rsvzone.store()                                   # tmpfs — у хоста, юнит его видит
        except rsvzone.ZoneError as e:
            raise ServiceError(f"хранилище гостя не смонтировано: {e}")
        for d in (rsvzone.VAR_DIR, rsvzone.STATE_DIR):
            try:
                os.makedirs(d, mode=0o700, exist_ok=True)
            except OSError as e:
                raise ServiceError(f"каталог {d} не создан: {e}")
        # env резерва (его токен) процессу не нужен: AWG_BOT_ENV — пустой файл, а
        # /etc/awg-bot/env закрыт юниту; BOT_TOKEN/ADMIN_ID из окружения бота — вон
        env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "ADMIN_ID")}
        env.update({"AWG_BOT_CONF_DIR": str(config.CONF_DIR), "AWG_BOT_DATA_DIR": str(config.DATA_DIR),
                    "AWG_BOT_ENV": "/dev/null"})
        if shutil.which("systemd-run"):
            props = rsvopen_sandbox_props(base)
            setenv = [f"--setenv={k}={env[k]}" for k in ("AWG_BOT_CONF_DIR", "AWG_BOT_DATA_DIR", "AWG_BOT_ENV") if k in env]
            cmd = ["systemd-run", "--collect", "--quiet", "--pipe", f"--unit={OPEN_UNIT}", *props, *setenv, *argv]
        else:
            cmd = argv
        err_path = rsvzone.STATE_DIR / "rsvopen.err"          # хвост stderr — причина отказа без итога
        try:
            err_fd = os.open(str(err_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            err_fd = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_fd,
                                    cwd=base, env=env, close_fds=True)
            proc.stdin.write(json.dumps(payload).encode() + b"\n")
            if keep_stdin:
                proc.stdin.flush()
            else:
                proc.stdin.close()
        except OSError as e:
            raise ServiceError(f"процесс открытия не запустился: {e}")
        finally:
            if err_fd != subprocess.DEVNULL:
                os.close(err_fd)
        self.__dict__["_rsvopen_proc"] = proc
        self.__dict__["_rsvopen_stdin_open"] = bool(keep_stdin)
        rsvzone.open_status_write("launched")

    def _rsvopen_send(self, line: dict) -> str:
        """Вторая строка stdin процессу открытия (конверт с ключом пакета) и
        закрытие трубы. Пусто — передано; иначе причина."""
        proc = self.__dict__.get("_rsvopen_proc")
        if proc is None or not self.__dict__.get("_rsvopen_stdin_open"):
            return "процесс открытия не ждёт ключ"
        self.__dict__["_rsvopen_stdin_open"] = False
        try:
            proc.stdin.write(json.dumps(line).encode() + b"\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError) as e:
            log.info("резерв: ключ процессу открытия не передан: %s", e)   # текст ОС — в журнал, не в уведомление
            return "ключ процессу открытия не передан — процесс уже завершился"
        return ""

    def _rsvopen_exit(self) -> tuple[int | None, str]:
        """Код завершения процесса открытия (None — идёт или неизвестен после
        перезапуска бота) и хвост его stderr."""
        from awgbot.infra import rsvzone
        proc = self.__dict__.get("_rsvopen_proc")
        if proc is None:
            return None, ""
        rc = proc.poll()
        if rc is None:
            return None, ""
        self.__dict__["_rsvopen_proc"] = None
        try:
            tail = (rsvzone.STATE_DIR / "rsvopen.err").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        except (OSError, IndexError):
            tail = []
        return int(rc), " · ".join(t.strip() for t in tail if t.strip())[:200]

    @staticmethod
    def _rsvopen_stop() -> None:
        """Остановить временный юнит процесса открытия (срыв, `back` на шаге up);
        отказ — строка в журнал, не исключение."""
        if not shutil.which("systemctl"):
            return
        try:
            subprocess.run(["systemctl", "stop", f"{OPEN_UNIT}.service"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            log.info("резерв: юнит процесса открытия не остановлен: %s", e)
