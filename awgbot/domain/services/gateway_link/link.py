"""link.py — скрипт линка и сборка файла конфигурации шлюза."""

from __future__ import annotations

import os
from awgbot.core import config
from awgbot.core import settings
from awgbot.domain.services.selfhost import own_host
from awgbot.domain.services.types import Notification, ServiceError
from .common import log


class LinkScriptMixin:
    """Скрипт линка и сборка файла конфигурации шлюза."""
    # ── скрипт линка ─────────────────────────────────────────────────────────
    def _link_script(self) -> str:
        return str(config.BASE_DIR / "install" / "routing-link-setup.sh")

    def _run_link_script(self, mode: str, env: dict | None = None) -> None:
        import subprocess
        try:
            proc = subprocess.run(["sh", self._link_script(), mode], capture_output=True,
                                  timeout=120, env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            raise ServiceError(f"скрипт линка ({mode}) не уложился в 120 с")
        if proc.returncode != 0:
            # причину скрипт пишет в stdout строкой «ОШИБКА: …» (say), в stderr — сбои команд
            out = proc.stdout.decode(errors="replace")
            why = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("ОШИБКА")), "")
            raise ServiceError(f"скрипт линка ({mode}) не отработал: "
                               + (why or proc.stderr.decode(errors="replace").strip())[-200:])

    _LEGACY_LINK_UNIT = "/etc/systemd/system/awg-link.service"
    _LINK_UNIT_TEMPLATE = "/etc/systemd/system/awg-link@.service"

    def _link_units_stale(self) -> bool:
        """Старый юнит ещё есть, либо шаблон поставлен прежней версией (ExecStop
        абсолютным путём к awg-quick, из-за чего перезапуск не опускал линк)."""
        if os.path.exists(self._LEGACY_LINK_UNIT):
            return True
        try:
            with open(self._LINK_UNIT_TEMPLATE, encoding="utf-8") as f:
                return "--down" not in f.read()
        except OSError:
            return False

    def gateway_units_migrate(self) -> bool:
        """Юнит первого линка — на шаблон awg-link@, а шаблон прежней версии — на текущий. Сам по себе переезд
        случился бы на первом ребуте ВПС (реассерт зовёт юнит); ждать его
        незачем — зовём --reassert явно один раз. Возвращает, был ли переезд."""
        if not config.ROUTING_GW_INTERFACE or not self._link_units_stale():
            return False
        first = self.gateway_first_slot()
        env = self._slot_env(first) if first is not None else {"LINK_IF": config.ROUTING_GW_INTERFACE}
        self._run_link_script("--reassert", env)
        return True

    _SYS_NET = "/sys/class/net"

    def gateway_links_adopt(self) -> list[str]:
        """Слоты с конфигом линка, чей интерфейс не поднят (бэкап восстановлен
        на пустом сервере, --adopt в restore не прошёл): --adopt — шаблон,
        экземпляр, подъём; идемпотентен, повторяется на каждом старте, пока
        линк не встанет. Возвращает принятые линки."""
        done = []
        for g in self.db.gateways():
            if not os.path.exists(os.path.join(config.AWG_DIR, f"{g.link_if}.conf")):
                continue
            if os.path.isdir(os.path.join(self._SYS_NET, g.link_if)):
                continue
            try:
                self._run_link_script("--adopt", self._slot_env(g))
                done.append(g.link_if)
            except ServiceError as e:
                log.warning("линк %s не принят: %s", g.link_if, e)
        return done

    def gateway_sync_link_ports(self) -> list[Notification]:
        """Порт линка слота — по живому конфигу на ВПС. Порт меняют руками
        (ListenPort в awglink.conf, перезапуск линка): конфиг линка ведёт бот,
        ручная смена порта не поддерживается — слот держит согласованный порт
        (его знают шлюз и документ конфигурации), просим вернуть его (по разу
        на каждый ручной порт; вернули — молча)."""
        from awgbot.infra.db.schema import _link_conf_params
        if not config.ROUTING_GW_INTERFACE:
            return []
        notes = []
        for g in self.db.gateways():
            if not os.path.exists(os.path.join(config.AWG_DIR, f"{g.link_if}.conf")):
                continue
            port, _cidr = _link_conf_params(g.link_if)
            hand_key = self._gw_slot_key("gw_link_port_hand", g.id)
            if port == g.link_port:
                if self.db.get_state(hand_key):
                    self.db.set_state(hand_key, "")       # порт вернули — молча
                continue
            if (self.db.get_state(hand_key) or "") == str(port):
                continue
            self.db.set_state(hand_key, str(port))
            log.info("gateway: порт линка слота %s изменён руками на %s (согласован %s)", g.id, port, g.link_port)
            notes.append(Notification(
                config.ADMIN_ID,
                f"🛰 {self._gw_link(g)}: порт линка изменён руками на <code>{port}</code> "
                f"(был <code>{g.link_port}</code>) — шлюз о нём не узнает, линк не поднимется. "
                "Конфиг линка ведёт бот, ручная правка не поддерживается: "
                f"верни <code>ListenPort = {g.link_port}</code> в <code>{g.link_if}.conf</code>"))
        return notes

    def _gw_firewall_refresh(self, strict: bool = False) -> None:
        """Порт нового линка (и снятие старого) и карантин сопряжения — в
        таблицу хоста сразу, а не при следующей плановой перерисовке: иначе
        второй шлюз не достучится до ВПС до неё. Фильтр выключен — таблица в
        форме NAT-only, карантин рисуется и в ней (reconcile). strict — отказ
        поднимается (выпуск команды подключения без карантина не идёт)."""
        try:
            from awgbot.infra import nftguard
            if nftguard.enabled():
                self._firewall_apply(rollback=False)
            else:
                nftguard.reconcile(self.db.admin_device_addresses(config.ADMIN_ID),
                                   channel_extra=self.gwcfg_channel_ports()[1:],
                                   transit_links=self.gateway_transit_links())
        except Exception as e:                            # noqa: BLE001
            log.warning("gateway: файервол хоста не перерисован: %s", e)
            if strict:
                raise ServiceError(f"таблица файервола сервера не перерисована: {e}") from None

    @staticmethod
    def _slot_env(gw) -> dict:
        """Окружение скрипта линка для слота: имя интерфейса, порт, /30, хост
        Endpoint из настроек (network.self_host, иначе server_host), если задан (без него скрипт
        берёт первый глобальный адрес интерфейса — за 1:1 NAT это приватный), и
        keepalive линка."""
        env = {"LINK_IF": gw.link_if, "LINK_PORT": str(gw.link_port), "LINK_CIDR": gw.link_cidr}
        # живое значение, как у ссылок пользователей: после «✏️ Домен» файл шлюза
        # не должен нести прежний хост до рестарта бота; собственный адрес
        # (network.self_host) главнее адреса для пользователей
        host = own_host()
        if host:
            env["ENDPOINT_HOST"] = host
        # keepalive линка — тем же значением, что у пиров устройств: бот читает
        # YAML целиком, а скрипт линка — только awk-ом по строке в двойных
        # кавычках, и `keepalive_seconds: 25` без них у него пропадал бы в «25-35»
        env["LINK_KEEPALIVE"] = str(settings.get("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS))
        return env


    # ── ключ линка ───────────────────────────────────────────────────────────

    def _link_privkey(self, gw=None) -> str:
        from awgbot.util import bundlecrypt
        link_if = gw.link_if if gw is not None else (config.ROUTING_GW_INTERFACE or "awglink")
        try:
            with open(f"/root/gw-{link_if}.conf", encoding="utf-8") as f:
                return bundlecrypt.read_privkey(f.read())
        except (OSError, ValueError) as e:
            raise ServiceError(f"ключ линка не прочитан: {e}")
