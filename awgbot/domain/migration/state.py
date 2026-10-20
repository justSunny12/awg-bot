"""state.py — часть пакета migration (см. __init__): состояние рычага и подготовка
второго интерфейса под переезд."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from awgbot.core import config

from .common import log, _STATE_KEY, STATE_RUNNING, STATE_OFF, ServiceErrorMigration


class MigrationStateMixin:
    """Состояние рычага и подготовка второго интерфейса."""

    # ── состояние ────────────────────────────────────────────────────────────

    def migration_available(self) -> bool:
        """Рычаг вообще существует? Нужны ОБА ключа: интерфейс без подсети
        нечем адресовать, подсеть без интерфейса не к чему привязать."""
        return bool(config.MIGRATION_INTERFACE and config.MIGRATION_SUBNET_PREFIX)

    # ── подготовка переезда из интерфейса (docs/ROADMAP.md, п.8) ────────────
    # Прежде второй интерфейс заводили руками в app.yaml, и рычаг появлялся
    # только после этого. То есть о самой возможности человек узнавал, лишь
    # если уже знал.

    def migration_generation_driven(self) -> bool:
        """Идущий переезд затеян сменой поколения ядра? Такой нельзя ни
        подменить своим, ни догнать следующим обновлением: двойники рождены
        под конкретное поколение, и цель у переезда ровно одна."""
        from awgbot.infra import awglock
        return self.migration_running() and awglock.target_generation() > awglock.applied_generation()

    def migration_blocked_reason(self) -> str:
        """Почему нельзя затеять переезд по кнопке прямо сейчас. Пустая строка
        — можно."""
        from awgbot.infra import awglock
        if self.migration_generation_driven():
            return (f"идёт переезд на поколение {awglock.target_generation()}: "
                    "он начат сменой ядра и обязан завершиться первым")
        if self.migration_running():
            return "переезд уже идёт"
        if self.migration_available():
            return ("второй интерфейс уже поднят — начни переезд в «🔧 Сервис» "
                    "или отмени его")
        return ""

    _MIG_IF_CANDIDATES = tuple(f"awg{i}" for i in range(1, 10))

    def migration_prepare(self, port: Optional[int] = None,
                          subnet_prefix: str = "") -> dict:
        """Поднять второй интерфейс под переезд и записать ключи в app.yaml.

        Порт и подсеть — то, ради чего переезд и затевают; не названы — берём
        случайный высокий порт и первую свободную подсеть. Константы config
        читаются при старте, поэтому вызывающий обязан перезапустить бота.
        """
        import subprocess
        from awgbot.core import settings
        reason = self.migration_blocked_reason()
        if reason:
            raise ServiceErrorMigration(reason.capitalize())
        if port is not None and not (1 <= int(port) <= 65535):
            raise ServiceErrorMigration("порт — число от 1 до 65535")

        conf_dir = config.AWG_DIR
        busy_ifaces = {config.AWG_INTERFACE} | {
            p.stem for p in Path(conf_dir).glob("*.conf")} if Path(conf_dir).is_dir() else {
            config.AWG_INTERFACE}
        new_if = next((n for n in self._MIG_IF_CANDIDATES if n not in busy_ifaces), "")
        if not new_if:
            raise ServiceErrorMigration("свободного имени интерфейса не нашлось")
        new_prefix = subnet_prefix or self._free_subnet_prefix()
        if not new_prefix:
            raise ServiceErrorMigration("свободной подсети не нашлось")

        env = {**os.environ, "AWG_IF": new_if, "SUBNET_PREFIX": new_prefix,
               "AWG_QUICK_DIR": conf_dir}
        if port is not None:
            env["LISTEN_PORT"] = str(int(port))
        script = str(config.BASE_DIR / "install" / "awg-server-init.sh")
        try:
            proc = subprocess.run(["bash", script], capture_output=True, timeout=180, env=env)
        except (OSError, subprocess.SubprocessError) as e:
            raise ServiceErrorMigration(f"второй интерфейс не поднят: {e}")
        out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
        if proc.returncode != 0:
            raise ServiceErrorMigration("второй интерфейс не поднят:\n"
                                        + "\n".join(out.splitlines()[-5:]))
        got_port = ""
        for line in proc.stdout.decode(errors="replace").splitlines():
            if line.startswith("LISTEN_PORT="):
                got_port = line.split("=", 1)[1].strip()
        try:
            settings.set_value("app.awg.migration_interface", new_if)
            settings.set_value("app.awg.migration_subnet_prefix", new_prefix)
            # как делает установщик: на новом интерфейсе сервер на .1, пул с .2
            if int(config.IP_HOST_START) < 2:
                settings.set_value("app.network.ip_host_start", 2)
        except Exception as e:                            # noqa: BLE001
            raise ServiceErrorMigration(f"ключи переезда не записаны в app.yaml: {e}")
        log.info("переезд: поднят %s (%s.0/24, порт %s)", new_if, new_prefix, got_port)
        return {"iface": new_if, "subnet": f"{new_prefix}.0/24", "port": got_port}

    def _free_subnet_prefix(self) -> str:
        """Первые три октета подсети, которой нет ни в одном конфиге awg."""
        base = config.SUBNET_PREFIX or "10.8.1"
        parts = base.split(".")
        if len(parts) != 3 or not parts[1].isdigit():
            return ""
        used = ""
        try:
            for conf in Path(config.AWG_DIR).glob("*.conf"):
                used += conf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        for step in range(1, 10):
            cand = f"{parts[0]}.{int(parts[1]) + step}.{parts[2]}"
            if f"{cand}." not in used:
                return cand
        return ""

    def migration_state(self) -> str:
        if not self.migration_available():
            return STATE_OFF
        return self.db.get_state(_STATE_KEY) or STATE_OFF

    def migration_running(self) -> bool:
        return self.migration_state() == STATE_RUNNING
