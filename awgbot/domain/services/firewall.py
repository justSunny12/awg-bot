"""
firewall.py — файервол из чата (README §6b).
"""
from __future__ import annotations

import logging
import os
import sys

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import awg
from awgbot.domain.services.types import ServiceError


log = logging.getLogger("awgbot.services")


class FirewallMixin:
    # ── файервол из чата (README §6b) ────────────────────────────────────────
    # Раньше единственным интерфейсом был CLI, и «подтверди вход» требовало
    # второго SSH-сеанса. Из чата это честнее: Telegram доступен независимо от
    # того, заперли вы себе SSH или нет, а таймер отката страхует ровно от
    # этого случая.

    def firewall_screen(self) -> dict:
        from awgbot.infra import nftguard
        st = nftguard.status(self.db.admin_device_addresses(config.ADMIN_ID))
        spec = st["spec"]
        return {"enabled": st["enabled"], "present": st["present"],
                "rollback": st["rollback"], "ufw": st["ufw"],
                "ssh_port": spec.ssh_port, "allow": list(spec.ssh_allow4) + list(spec.ssh_allow6),
                "raw_allow": list(settings.get("app.firewall.ssh_allow", []) or []),
                "unresolved": list(spec.unresolved), "admin_ips": list(spec.tunnel_admin4),
                "nat": spec.nat}

    # ── порт SSH (кнопка «Изменить порт») ────────────────────────────────────
    # Порт живёт в conf (его фильтрует таблица) и в sshd; из чата меняются оба.
    # Порядок: сначала conf — хук on_change пересобирает таблицу, и к моменту
    # рестарта sshd новый порт уже открыт; sshd не принял — conf возвращаем.
    # Таймера отката нет намеренно: текущие SSH-сеансы рестарт не рвёт, а
    # вернуть порт можно этой же кнопкой — чат от SSH не зависит.

    def ssh_port_busy(self, port: int) -> str:
        """Кто слушает порт; пусто — свободен. Свой текущий порт — тоже
        «занят»: sshd на нём и стоит."""
        from awgbot.infra import sshd
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        try:
            return sshd.port_busy(int(port))
        except sshd.SshdError as e:
            raise ServiceError(str(e))

    def ssh_port_change(self, port: int) -> int:
        """Перевести sshd и фильтр на порт. Возвращает прежний порт."""
        from awgbot.infra import sshd
        port = int(port)
        if not sshd.valid_port(port):
            raise ServiceError("порт — число от 1 до 65535")
        old = int(settings.get_int("app.network.ssh_port", config.SSH_PORT))
        if port == old:
            raise ServiceError(f"порт {port} и так текущий")
        busy = self.ssh_port_busy(port)
        if busy:
            raise ServiceError(f"порт {port} занят: {busy}")
        settings.set_value("app.network.ssh_port", port)
        try:
            for line in sshd.set_port(port):
                log.info("sshd: %s", line)
        except sshd.SshdError as e:
            settings.set_value("app.network.ssh_port", old)
            raise ServiceError(str(e))
        return old

    def firewall_allow_add(self, raw: str) -> list[str]:
        """Добавить адреса в вайтлист SSH. Добавление запереть не может —
        применяем без таймера отката."""
        from awgbot.infra import nftguard
        entries: list[str] = []
        for tok in str(raw).replace(",", " ").split():
            try:
                nftguard.classify(tok)
            except ValueError as e:
                raise ServiceError(f"«{tok}» не адрес, не подсеть и не имя: {e}")
            if tok not in entries:
                entries.append(tok)
        if not entries:
            raise ServiceError("пусто: жду адрес, подсеть или имя")
        cur = list(settings.get("app.firewall.ssh_allow", []) or [])
        cur += [e for e in entries if e not in cur]
        settings.set_value("app.firewall.ssh_allow", cur)
        if nftguard.enabled():
            self.reconcile_ssh_access()
        return cur

    def firewall_allow_remove(self, entry: str) -> list[str]:
        """Убрать адрес. Это МОЖЕТ запереть — применяем с таймером отката."""
        cur = [v for v in (settings.get("app.firewall.ssh_allow", []) or []) if v != entry]
        settings.set_value("app.firewall.ssh_allow", cur)
        from awgbot.infra import nftguard
        if nftguard.enabled():
            self._firewall_apply(rollback=True)
        return cur

    _FW_ROLLBACK_SECONDS = 300

    def _firewall_apply(self, rollback: bool) -> None:
        from awgbot.infra import nftguard
        spec = nftguard.build_spec(self.db.admin_device_addresses(config.ADMIN_ID))
        for line in nftguard.ensure_persistence():
            log.info("firewall: %s", line)
        nftguard.apply_text(nftguard.render(spec))
        if not rollback:
            return
        env = {k: str(v) for k, v in (("AWG_BOT_CONF_DIR", config.CONF_DIR),
                                      ("AWG_BOT_DATA_DIR", config.DATA_DIR),
                                      ("AWG_BOT_ENV", os.environ.get("AWG_BOT_ENV", "")))
               if v}
        cmd = ["-p", f"WorkingDirectory={config.BASE_DIR}", sys.executable,
               "-m", "tools.firewall", "rollback"]
        nftguard.arm_rollback(self._FW_ROLLBACK_SECONDS, cmd, env)

    def firewall_enable(self) -> int:
        """Включить фильтр с таймером отката. Возвращает секунды на проверку.

        Пустой вайтлист не запрещаем: SSH останется открыт всем адресам, и это
        осознанный выбор (ключи никто не отменял) — а вот молча включить фильтр,
        который никого не пускает, было бы ловушкой."""
        from awgbot.infra import nftguard
        settings.set_value("app.firewall.enabled", True)
        try:
            self._firewall_apply(rollback=True)
        except nftguard.GuardError as e:
            settings.set_value("app.firewall.enabled", False)
            raise ServiceError(str(e))
        return self._FW_ROLLBACK_SECONDS

    def firewall_confirm(self) -> bool:
        from awgbot.infra import nftguard
        return nftguard.disarm_rollback()

    def firewall_disable(self) -> list[str]:
        """Снять фильтр. NAT клиентов таблица держит дальше: «выключить
        файервол» не обещает «оставить клиентов без интернета»."""
        from awgbot.infra import nftguard
        nftguard.disarm_rollback()
        done = nftguard.remove()
        settings.set_value("app.firewall.enabled", False)
        return done

    def retire_legacy_ssh_gate(self) -> None:
        """Разово снять прежние ворота (цепочка AWGBOT_SSH + PostUp-страж) —
        но ТОЛЬКО когда новая таблица уже держит SSH. Иначе снятие открыло бы
        SSH из туннеля всем пирам до включения файервола."""
        from awgbot.infra import nftguard
        if not nftguard.enabled() or self.db.get_state("legacy_ssh_gate_removed"):
            return
        try:
            if awg.remove_legacy_ssh_gate():
                log.info("firewall: старые ворота SSH (AWGBOT_SSH, PostUp) сняты")
        except awg.AwgError as e:
            log.warning("firewall: старые ворота не сняты: %s", e)
            return
        self.db.set_state("legacy_ssh_gate_removed", "1")
