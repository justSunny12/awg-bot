"""smbtunnel.py — «SMB в туннеле»: имена SMB-серверов локальных сетей шлюзов для устройств админа через резолвер админа."""
from __future__ import annotations

import hashlib
import time

from awgbot.core import config, settings
from awgbot.domain import gwservices
from awgbot.domain.services.types import Notification, ServiceError

from .common import log

SETTING = "app.routing.smb_tunnel"
_APPLIED_KEY = "smb_tunnel_applied"        # отпечаток поставленного файла записей
_ALERT_KEY = "admin_dns_alert"             # «резолвер админа не отвечает» уже сказано
_FAIL_KEY = "admin_dns_failed_once"        # прошлый тик резолвер админа молчал, тревоги ещё не было
_FAIL_PAUSE_S = 10 * 60                    # файл записей не встал — тот же файл не пробуется каждый такт


class SmbTunnelMixin:
    """Агенты шлюзов присылают каналом SMB-серверы своих сетей (svc); при
    включённом тумблере сервер собирает из них файл записей DNS-SD под
    awg.internal для резолвера админа — той же функцией и тем же построчным
    белым списком, что шлюз-получатель у связи подсетей. Резолвер админа
    отвечает только устройствам админа (nftguard: :53 его адреса — набору
    admin4), их новые конфиги получают его DNS (configgen.allowed_ips_for).
    Обратная зона — подсети пользователей: по ней Mac находит домен обзора.
    Адреса записей — адреса в туннеле сети слота (lan_items_twinned):
    настоящие через туннель не открываются, а одинаковые за разными шлюзами
    так видны оба."""

    # ── доступность и тумблер ────────────────────────────────────────────────

    @staticmethod
    def smb_tunnel_on() -> bool:
        return bool(settings.get(SETTING, False))

    @staticmethod
    def smb_tunnel_unavailable() -> str:
        """Почему тумблер нельзя включить; пусто — можно. Нужен свой резолвер
        пользователей, поднятый резолвер админа и фильтр снаружи: только файервол
        закрывает имена от прочих пользователей."""
        from awgbot.infra import admindns, nftguard, resolver
        if not resolver.installed():
            return "Нужен свой резолвер: ⚙️ → 🖥 Сервер AWG → «🔒 Свой резолвер»"
        if not admindns.serving():
            return "Резолвер админа не поднят — на сервере: awg-bot resolver admin status"
        if not nftguard.enabled():
            return "Нужен фильтр снаружи: ⚙️ → 🛡 SSH-доступ → «☑️ Фильтр снаружи» — без него имена увидят все пользователи"
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
        """SMB всех слотов на связи (не молчащих дольше суток), после чистки по
        подсетям слота — в адресах в туннеле его сети: одинаковые адреса за
        разными шлюзами видны оба."""
        items: list[dict] = []
        for gw in self.db.gateways():
            if not self._gwlink_seen_recent(gw.id, self._SVC_NEIGHBOUR_STALE_S):
                continue
            own = gwservices.clean(self.gwlink_services(gw.id), gw.lan_subnets, gwservices.MAX_OWN)
            items += self.lan_items_twinned(gw, own)
        return gwservices.clean(items, ["0.0.0.0/0"], gwservices.MAX_PEER)

    def smb_tunnel_text(self) -> str:
        """Файл записей резолвера админа; тумблер выключен, фильтр снаружи
        выключен (без него имена увидели бы все пользователи) или записей нет — пусто.
        Домены обзора — в обратных зонах подсетей туннеля и каждого адреса
        устройства админа (/32)."""
        from awgbot.infra import nftguard
        if not self.smb_tunnel_on() or not nftguard.enabled():
            return ""
        items = self.smb_tunnel_items()
        if not items:
            return ""
        # домен обзора Mac ищет в обратной зоне своего адреса с маской: клиент
        # AmneziaVPN на macOS ставит туннелю /32 — зона подсети клиентов его не
        # находит, нужна зона каждого адреса устройства админа
        own = list(nftguard.tunnel_nets()) + [f"{a}/32" for a in self._gw_ssh_allow()]
        return gwservices.render_dnsmasq(items, own,
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
        fail = self.__dict__.setdefault("_smb_apply_fail", {})
        if not force and fail.get("digest") == digest and time.monotonic() - float(fail.get("at") or 0) < _FAIL_PAUSE_S:
            return False                                  # тот же файл только что не встал — пауза, не каждый такт
        try:
            admindns.apply_records(text)
        except admindns.AdminDnsError as e:
            if fail.get("text") != str(e):
                log.warning("SMB в туннеле: записи не поставлены: %s", e)   # один раз на текст отказа
            fail.update(digest=digest, text=str(e), at=time.monotonic())
            return False
        fail.clear()
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
        """Старт бота: резолвер админа — на хосте со своим резолвером пользователей;
        переезд адреса резолвера пользователей тянет server=. Затем — файл записей
        (тумблер мог быть включён до перезапуска)."""
        from awgbot.infra import admindns, nftguard, resolver
        if not resolver.installed():
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

    def _admin_dns_fail_set(self, value: str) -> None:
        """Отметка «молчал прошлый тик» — записью только при смене значения."""
        if (self.db.get_state(_FAIL_KEY) or "0") != value:
            self.db.set_state(_FAIL_KEY, value)

    def admin_dns_health_tick(self) -> list[Notification]:
        """Резолвер админа поставлен — обязан отвечать: от него DNS всех
        устройств админа с конфигом, выданным после его установки, тумблер тут
        ни при чём. Обычным уведомлением, не критическим: ломает DNS только
        админу. Тревога — только если молчал и прошлый тик. Отметка «сказано» —
        по факту доставки. Заодно — файл записей
        (слоты, замолчавшие дольше суток, выпадают; фильтр выключили — снимается)."""
        from awgbot.domain.alerts import armer
        from awgbot.infra import admindns
        if not admindns.installed():
            self._admin_dns_fail_set("0")            # снят и поставлен заново — счёт молчания с нуля
            return []
        self.smb_tunnel_sync()
        alive = admindns.probe()
        said = self.db.get_state(_ALERT_KEY) == "1"
        if alive:
            self._admin_dns_fail_set("0")
        elif not said and self.db.get_state(_FAIL_KEY) != "1":
            # первый тик отказа — ждём второго, как у резолвера клиентов
            self._admin_dns_fail_set("1")
            log.warning("резолвер админа не ответил — проверю на следующем тике")
            return []
        if alive and said:
            return [Notification(config.ADMIN_ID, "✅ Резолвер админа снова отвечает",
                                 on_sent=armer(self.db, _ALERT_KEY, "0"))]
        if not alive and not said:
            return [Notification(config.ADMIN_ID, f"🔴 Резолвер админа <code>{admindns.addr()}</code> не отвечает — "
                                                  "у устройств админа нет DNS и имён SMB в туннеле",
                                 on_sent=armer(self.db, _ALERT_KEY, "1"))]
        return []
