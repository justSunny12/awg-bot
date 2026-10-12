"""smbtunnel.py — «SMB в туннеле»: имена SMB-серверов локальных сетей шлюзов для устройств админа через резолвер админа."""
from __future__ import annotations

import hashlib

from awgbot.core import config, settings
from awgbot.domain import gwservices
from awgbot.domain.services.types import Notification, ServiceError

from .common import log

SETTING = "app.routing.smb_tunnel"
_APPLIED_KEY = "smb_tunnel_applied"        # отпечаток поставленного файла записей
_ALERT_KEY = "admin_dns_alert"             # «резолвер админа не отвечает» уже сказано


class SmbTunnelMixin:
    """Агенты шлюзов присылают каналом SMB-серверы своих сетей (svc); при
    включённом тумблере сервер собирает из них файл записей DNS-SD под
    awg.internal для резолвера админа — той же функцией и тем же построчным
    белым списком, что шлюз-получатель у связи подсетей. Резолвер админа
    отвечает только устройствам админа (nftguard: :53 его адреса — набору
    admin4), их новые конфиги получают его DNS (configgen.allowed_ips_for).
    Обратная зона — подсети клиентов: по ней Mac находит домен обзора."""

    # ── доступность и тумблер ────────────────────────────────────────────────

    @staticmethod
    def smb_tunnel_on() -> bool:
        return bool(settings.get(SETTING, False))

    @staticmethod
    def smb_tunnel_split() -> bool:
        """Конфиги сервера с раздельным туннелем: client_config.allowed_ips не
        покрывает весь IPv4 — настоящие адреса локальных сетей шлюзов мимо
        туннеля (имена при этом видны: /32 резолвера админа дописывается)."""
        import ipaddress
        for tok in str(config.CLIENT_ALLOWED_IPS).split(","):
            try:
                net = ipaddress.ip_network(tok.strip(), strict=False)
            except ValueError:
                continue
            if net.version == 4 and net.prefixlen == 0:
                return False
        return True

    @staticmethod
    def smb_tunnel_possible() -> bool:
        """Режим host — только там свой резолвер клиентов и резолвер админа;
        в docker тумблера на экране нет вовсе."""
        from awgbot.infra.awg import in_container
        return not in_container()

    @staticmethod
    def smb_tunnel_unavailable() -> str:
        """Почему тумблер нельзя включить; пусто — можно. Нужен свой резолвер
        клиентов, поднятый резолвер админа и фильтр снаружи: только файервол
        закрывает имена от прочих клиентов."""
        from awgbot.infra import admindns, nftguard, resolver
        from awgbot.infra.awg import in_container
        if in_container():
            return "SMB в туннеле — только при VPN прямо на хосте, без docker"
        if not resolver.installed():
            return "Нужен свой резолвер: ⚙️ → 🖥 Сервер AWG → «🔒 Свой резолвер»"
        if not admindns.serving():
            return "Резолвер админа не поднят — на сервере: awg-bot resolver admin status"
        if not nftguard.enabled():
            return "Нужен фильтр снаружи: ⚙️ → 🛡 SSH-доступ → «☑️ Фильтр снаружи» — без него имена увидят все клиенты"
        return ""

    def smb_tunnel_set(self, on: bool) -> bool:
        """Тумблер «SMB в туннеле»: включить можно только при доступности;
        файл записей — сразу (выключение снимает его). False — тумблер
        переставлен, а записи поставить или снять не вышло."""
        if on:
            why = self.smb_tunnel_unavailable()
            if why:
                raise ServiceError(why)
        settings.set_value(SETTING, bool(on))
        return self.smb_tunnel_sync(force=True)

    # ── файл записей ─────────────────────────────────────────────────────────

    def smb_tunnel_items(self) -> list[dict]:
        """SMB всех слотов на связи (не молчащих дольше суток), после чистки."""
        items: list[dict] = []
        for gw in self.db.gateways():
            if not self._gwlink_seen_recent(gw.id, self._SVC_NEIGHBOUR_STALE_S):
                continue
            items += gwservices.clean(self.gwlink_services(gw.id), gw.lan_subnets, gwservices.MAX_OWN)
        return gwservices.clean(items, ["0.0.0.0/0"], gwservices.MAX_PEER)

    def smb_tunnel_text(self) -> str:
        """Файл записей резолвера админа; тумблер выключен, фильтр снаружи
        выключен (без него имена увидели бы все клиенты) или записей нет — пусто."""
        from awgbot.infra import nftguard
        if not self.smb_tunnel_on() or not nftguard.enabled():
            return ""
        items = self.smb_tunnel_items()
        if not items:
            return ""
        return gwservices.render_dnsmasq(items, nftguard.tunnel_nets(),
                                         owner="# awg-bot (сервер AWG): SMB локальных сетей шлюзов для устройств админа.")

    def smb_tunnel_sync(self, force: bool = False) -> bool:
        """Поставить файл записей, если он изменился. Строки — тем же белым
        списком, что у шлюза (lines_ok); не прошли — файл снимается. Резолвера
        админа нет — ничего. True — поставлено (или нечего ставить)."""
        from awgbot.infra import admindns
        if not admindns.installed():
            return False
        text = self.smb_tunnel_text()
        if text and not gwservices.lines_ok(text):
            log.warning("SMB в туннеле: файл записей не прошёл построчную проверку — снимаю")
            text = ""
        digest = hashlib.sha256(text.encode()).hexdigest()[:16] if text else "-"
        if not force and (self.db.get_state(_APPLIED_KEY) or "") == digest:
            return True
        try:
            admindns.apply_records(text)
        except admindns.AdminDnsError as e:
            log.warning("SMB в туннеле: записи не поставлены: %s", e)
            return False
        self.db.set_state(_APPLIED_KEY, digest)
        log.info("SMB в туннеле: записей резолвера админа — %s", text.count("srv-host=") if text else 0)
        return True

    def smb_tunnel_slot(self, gw) -> bool:
        """SMB этого слота сейчас в резолвере админа: тумблер включён, фильтр
        снаружи включён, резолвер админа поставлен, слот на связи (не молчит
        дольше суток) — то же условие, что у файла записей."""
        from awgbot.infra import admindns, nftguard
        return (self.smb_tunnel_on() and nftguard.enabled() and admindns.installed()
                and self._gwlink_seen_recent(gw.id, self._SVC_NEIGHBOUR_STALE_S))

    # ── резолвер админа: старт и здоровье ────────────────────────────────────

    def admin_dns_ensure(self) -> None:
        """Старт бота: резолвер админа — на хосте со своим резолвером клиентов
        (режим host); переезд адреса резолвера клиентов тянет server=. Затем —
        файл записей (тумблер мог быть включён до перезапуска)."""
        from awgbot.infra import admindns, nftguard, resolver
        from awgbot.infra.awg import in_container
        if in_container() or not resolver.installed():
            return
        addrs = resolver.listen_addrs()
        was = admindns.installed()
        # перехват :53 обвязки маршрутизации — мимо адреса резолвера админа
        done = admindns.nat_exempt(nftguard.tunnel_nets())
        if done:
            log.info("резолвер админа: выведен из перехвата :53 (%s)", ", ".join(done))
        try:
            admindns.ensure(addrs[0] if addrs else "")
        except admindns.AdminDnsError as e:
            log.warning("резолвер админа не поставлен: %s", e)
            return
        if not was and admindns.installed():
            # правило «:53 резолвера админа — только устройствам админа» — в
            # таблицу сразу: до перерисовки его пускало бы общее «:53 из туннеля»
            self._gw_firewall_refresh()
        self.smb_tunnel_sync(force=True)

    def admin_dns_health_tick(self) -> list[Notification]:
        """Резолвер админа поставлен — обязан отвечать: от него DNS всех
        устройств админа с конфигом, выданным после его установки, тумблер тут
        ни при чём. Обычным уведомлением, не критическим: ломает DNS только
        админу. Отметка «сказано» — по факту доставки. Заодно — файл записей
        (слоты, замолчавшие дольше суток, выпадают; фильтр выключили — снимается)."""
        from awgbot.domain.alerts import armer
        from awgbot.infra import admindns
        if not admindns.installed():
            return []
        self.smb_tunnel_sync()
        alive = admindns.probe()
        said = self.db.get_state(_ALERT_KEY) == "1"
        if alive and said:
            return [Notification(config.ADMIN_ID, "✅ Резолвер админа снова отвечает",
                                 on_sent=armer(self.db, _ALERT_KEY, "0"))]
        if not alive and not said:
            return [Notification(config.ADMIN_ID, f"🔴 Резолвер админа <code>{admindns.addr()}</code> не отвечает — "
                                                  "у устройств админа нет DNS и имён SMB в туннеле",
                                 on_sent=armer(self.db, _ALERT_KEY, "1"))]
        return []
