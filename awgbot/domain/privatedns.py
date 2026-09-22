"""
privatedns.py — свой DNS-резолвер клиентов: состояние, решение админа и
связка с переездом профилей. Подмешивается в Services.

Адрес резолвера — собственный адрес сервера в туннеле, <подсеть>.1; на нём
слушает dnsmasq (install/awg-resolver-setup.sh). Свежая установка получает его
в оба поля DNS сразу. Живой установке с публичным адресом бот предлагает
перейти: адрес DNS вшит в каждую выданную ссылку, поэтому переход идёт через
переезд профилей — «сейчас», «при следующем переезде» или «не нужно».
Решение хранится в server_state (private_dns_decision); переезд читает его на
включении рычага, финал стирает.
"""
from __future__ import annotations

import logging

from awgbot.core import config, settings
from awgbot.infra import resolver

log = logging.getLogger(__name__)

DECISION_KEY = "private_dns_decision"       # "" | pending | dismissed
_ALERT_KEY = "resolver_alerted"             # "1" — об отказе доложено


class PrivateDnsMixin:

    # ── состояние ────────────────────────────────────────────────────────────

    @staticmethod
    def _dns_fields() -> tuple[str, str]:
        return (str(settings.get("app.client_config.dns1", config.DNS1) or "").strip(),
                str(settings.get("app.client_config.dns2", config.DNS2) or "").strip())

    @staticmethod
    def private_dns_target(prefix: str = "") -> str:
        """Адрес резолвера для подсети (по умолчанию — текущей)."""
        return resolver.resolver_addr(prefix or str(settings.get(
            "app.network.subnet_prefix", config.SUBNET_PREFIX) or ""))

    def private_dns_decision(self) -> str:
        return self.db.get_state(DECISION_KEY) or ""

    def set_private_dns_decision(self, value: str) -> None:
        if value not in ("", "pending", "dismissed"):
            raise ValueError(value)
        self.db.set_state(DECISION_KEY, value)

    def private_dns_info(self) -> dict:
        """Для экрана сервера и инфобокса.

        mode: private — оба поля на этом хосте (резолвер стоит);
              public  — хотя бы одно поле публичное, можно перейти;
              n/a     — docker-режим: сервер на .0, приватного адреса нет.
        """
        dns1, dns2 = self._dns_fields()
        target = self.private_dns_target()
        if config.AWG_RUNTIME != "host":
            mode = "n/a"
        elif resolver.is_private(dns1) and resolver.is_private(dns2):
            mode = "private"
        else:
            mode = "public"
        return {"dns1": dns1, "dns2": dns2, "target": target, "mode": mode,
                "decision": self.private_dns_decision(),
                "installed": resolver.installed()}

    def private_dns_offer_due(self) -> bool:
        """Показывать инфобокс при старте: адрес публичный, решения ещё нет."""
        info = self.private_dns_info()
        return info["mode"] == "public" and not info["decision"]

    def private_dns_for_migration(self) -> bool:
        """Двойники переезда получают свой резолвер: адрес уже приватный (он
        умрёт со старым интерфейсом — новый обязателен) либо админ решил
        перейти («сейчас» и «при следующем переезде» оба пишут pending)."""
        info = self.private_dns_info()
        return info["mode"] == "private" or info["decision"] == "pending"

    # ── связка с переездом ───────────────────────────────────────────────────

    def private_dns_on_migration_start(self, new_prefix: str) -> str:
        """Перед рождением двойников: резолвер слушает <новая подсеть>.1, адрес
        записан в app.docker.migration_dns. Возвращает адрес или "" (не нужно).
        ResolverError — наверх: рождать двойников с мёртвым DNS нельзя."""
        if not self.private_dns_for_migration():
            return ""
        addr = resolver.resolver_addr(new_prefix)
        resolver.add(addr)
        settings.set_value("app.docker.migration_dns", addr)
        log.info("переезд: резолвер клиентов слушает %s", addr)
        return addr

    def private_dns_on_promote(self, old_prefix: str) -> str:
        """Финал: dns1/dns2 = адрес двойников, старый адрес снят с резолвера,
        решение стёрто. Возвращает новый адрес или "" (переезд без DNS)."""
        addr = str(settings.get("app.docker.migration_dns", "") or "").strip()
        if not addr:
            return ""
        settings.set_value("app.client_config.dns1", addr)
        settings.set_value("app.client_config.dns2", addr)
        settings.set_value("app.docker.migration_dns", "")
        self.set_private_dns_decision("")
        old = resolver.resolver_addr(old_prefix)
        if old and old != addr:
            try:
                resolver.remove(old)
            except resolver.ResolverError as e:
                log.warning("финал переезда: старый адрес резолвера %s не снят: %s", old, e)
        return addr

    def private_dns_on_cancel(self) -> None:
        """Отмена переезда: адрес двойников с резолвера снимается, ключ
        очищается; решение админа остаётся — следующий переезд его исполнит."""
        addr = str(settings.get("app.docker.migration_dns", "") or "").strip()
        if not addr:
            return
        settings.set_value("app.docker.migration_dns", "")
        try:
            resolver.remove(addr)
        except resolver.ResolverError as e:
            log.warning("отмена переезда: адрес резолвера %s не снят: %s", addr, e)

    # ── здоровье резолвера (тик монитора) ────────────────────────────────────

    def resolver_ensure_dropin(self) -> None:
        """Override юнита dnsmasq — к версии из поставки (см. resolver.ensure_dropin).
        Резолвера нет — скрипт сам ничего не делает."""
        if not resolver.installed():
            return
        try:
            out = resolver.ensure_dropin()
        except resolver.ResolverError as e:
            log.warning("резолвер: override юнита не обновлён: %s", e)
            return
        if out.strip():
            log.info("резолвер: %s", out.strip().splitlines()[-1])

    def resolver_health_tick(self) -> list:
        """Приватный DNS у клиентов — значит резолвер обязан отвечать. Проба
        UDP-запросом с хоста; отказ — одно уведомление, восстановление — одно."""
        from awgbot.domain.services import Notification
        info = self.private_dns_info()
        if info["mode"] != "private":
            return []
        addrs = [a for a in {info["dns1"], info["dns2"]} if a]
        alive = all(resolver.probe(a) for a in addrs)
        said = self.db.get_state(_ALERT_KEY) == "1"
        if alive and said:
            self.db.set_state(_ALERT_KEY, "0")
            return [Notification(config.ADMIN_ID, "🟢 Резолвер клиентов снова отвечает "
                                 f"({', '.join(addrs)}).")]
        if not alive and not said:
            self.db.set_state(_ALERT_KEY, "1")
            active = resolver.service_active()
            hint = ("юнит dnsmasq не активен — journalctl -u dnsmasq -e"
                    if active is False else "юнит жив, но на адресе не отвечает — awg-bot resolver status")
            return [Notification(config.ADMIN_ID,
                                 "🔴 <b>Резолвер клиентов не отвечает</b> "
                                 f"({', '.join(addrs)}): у людей нет DNS. {hint}.",
                                 force_sound=True, critical=True)]
        return []
