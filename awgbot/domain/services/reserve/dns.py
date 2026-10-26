"""
dns.py — часть пакета reserve (см. __init__): запись DNS адреса для пользователей —
арбитр VPN. Настройка провайдера (`rsv_dns`: provider, name, token, user,
server), чтение и смена записи через API, факт — у авторитетного сервера
зоны, надзор за записью (указывает не сюда, а переезда не было — Т-РУ16),
собственный адрес сервера.

Провайдер «вручную» — запись меняет человек: переезд кнопкой ждёт смены,
аварийный переезд невозможен (направление «не готово» строкой).
"""
from __future__ import annotations

import ipaddress
import random
import time

from awgbot.core import config, settings
from awgbot.util import dnsapi, livestep, timeutil

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

PROVIDERS: tuple[str, ...] = ("manual", "cloudflare", "duckdns", "dyndns2", "digitalplat")
PROVIDER_LABELS = {"manual": "вручную", "cloudflare": "Cloudflare", "duckdns": "DuckDNS", "dyndns2": "DynDNS2",
                   "digitalplat": "DigitalPlat"}
RECORD_WATCH_SECONDS = 120
TTL_WARN_S = 300                  # TTL записи выше — предупреждение: клиенты переходят медленно
_OWN_IP_TTL = 600


class ReserveDnsMixin:
    # ── настройка ───────────────────────────────────────────────────────────
    def reserve_dns(self) -> dict:
        d = self.rsv_get("rsv_dns")
        d.setdefault("provider", "manual")
        d["name"] = self.reserve_dns_name()
        return d

    def reserve_dns_name(self) -> str:
        host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip().strip(".")
        try:
            ipaddress.ip_address(host)
            return ""                                        # IP — записи нет
        except ValueError:
            return host

    def reserve_dns_provider(self) -> str:
        p = str(self.rsv_get("rsv_dns").get("provider") or "manual")
        return p if p in PROVIDERS else "manual"

    def reserve_dns_automatic(self) -> bool:
        """Запись меняется ботом: провайдер не «вручную» и токен задан."""
        d = self.rsv_get("rsv_dns")
        return self.reserve_dns_provider() != "manual" and bool(d.get("token"))

    def reserve_dns_set_provider(self, provider: str) -> dict:
        if provider not in PROVIDERS:
            raise ServiceError("Неизвестный провайдер")
        with _LOCK:
            fields = {"provider": provider, "checked_at": "", "current": "", "check_ok": None}
            if provider != self.reserve_dns_provider():
                fields.update(token="", user="", server="", token_at="")   # токен прежнего провайдера — не этому
            d = self.rsv_update("rsv_dns", **fields)
        self.reserve_pkg_mark()
        return d

    def reserve_dns_set_token(self, text: str) -> dict:
        """Токен из чата: Cloudflare и DuckDNS — токен, DigitalPlat — ключ
        dp_live_…; DynDNS2 — строка https://логин:пароль@сервер/nic/update."""
        text = str(text or "").strip()
        if not text or len(text) > 512 or any(ch.isspace() for ch in text):
            raise ServiceError("Это не похоже на токен: одна строка без пробелов")
        fields: dict = {"token": text, "token_at": timeutil.now_iso(), "checked_at": "", "check_ok": None}
        if self.reserve_dns_provider() == "dyndns2":
            from urllib.parse import urlsplit
            u = urlsplit(text)
            if u.scheme != "https" or not u.hostname or not u.username:
                raise ServiceError("Для DynDNS2 нужна строка вида https://логин:пароль@сервер/nic/update")
            # в server — только схема и хост[:порт]: путь /nic/update дописывает сам провайдер (dyndns2._url)
            fields.update(user=u.username or "", token=u.password or "",
                          server=f"{u.scheme}://{u.hostname}{(':' + str(u.port)) if u.port else ''}")
        with _LOCK:
            d = self.rsv_update("rsv_dns", **fields)
        self.reserve_pkg_mark()
        return d

    def reserve_dns_forget_token(self) -> None:
        with _LOCK:
            self.rsv_update("rsv_dns", token="", user="", server="", token_at="", checked_at="", check_ok=None)
        self.reserve_pkg_mark()

    # ── чтение и смена ──────────────────────────────────────────────────────
    def reserve_dns_check(self) -> dict:
        """«🧪 Проверить»: читает запись у провайдера (токен и права) и у
        авторитетного сервера. {ok, text, ip}."""
        name = self.reserve_dns_name()
        if not name:
            return {"ok": False, "text": "Адрес для пользователей — IP, записи DNS у него нет", "ip": ""}
        busy = self.reserve_busy() if hasattr(self, "reserve_busy") else ""
        if busy:
            # у DuckDNS и DynDNS2 проверка — это запись: посреди переезда она вернула бы прежний адрес
            return {"ok": False, "text": f"Сейчас {busy} — проверка подождёт", "ip": ""}
        d = self.reserve_dns()
        ip, auth, ttl, why = "", [], 0, ""
        try:
            auth, ttl = dnsapi.read_authoritative_ttl(name)
        except dnsapi.DnsError as e:
            why = str(e)
        if not auth:
            self.rsv_update("rsv_dns", checked_at=timeutil.now_iso(), check_ok=False, check_why=(why or "записи нет")[:160])
            return {"ok": False, "text": f"Запись не прочитана: {why or 'у авторитетного сервера записи нет'}", "ip": ""}
        if self.reserve_dns_provider() != "manual":
            try:
                ip = dnsapi.provider_for(d).check(d)
            except dnsapi.DnsError as e:
                self.rsv_update("rsv_dns", checked_at=timeutil.now_iso(), check_ok=False, check_why=str(e)[:160])
                return {"ok": False, "text": f"Токен не даёт править запись: {e}", "ip": auth[0]}
        cur = ip or auth[0]
        self.rsv_update("rsv_dns", checked_at=timeutil.now_iso(), check_ok=True, current=cur, check_why="",
                        addrs=list(auth), ttl=int(ttl))
        warn = ""
        if len(auth) > 1:
            warn = f" ⚠️ у записи {len(auth)} адреса: {', '.join(auth)} — оставь одну A-запись"
        elif ttl > TTL_WARN_S:
            warn = f" ⚠️ TTL {ttl} с — часть пользователей перейдёт на новый адрес не раньше чем через {ttl // 60} мин, лучше ≤ {TTL_WARN_S}"
        head = f"Запись {name} доступна для правки: сейчас {cur}" if ip else f"Запись {name}: сейчас {cur}"
        return {"ok": not warn, "text": head + warn, "ip": cur, "ttl": int(ttl), "addrs": list(auth)}

    def reserve_dns_switch(self, ip: str) -> None:
        """Сменить запись на `ip` через API; «вручную» и отказ — ServiceError."""
        name = self.reserve_dns_name()
        if not name:
            raise ServiceError("Адрес для пользователей — IP, записи нет")
        d = self.reserve_dns()
        try:
            dnsapi.provider_for(d).set(d, ip)
        except dnsapi.NotSupported:
            raise ServiceError("запись меняется вручную")
        except dnsapi.DnsError as e:
            raise ServiceError(str(e))
        self.rsv_update("rsv_dns", switched_to=ip, switched_at=timeutil.now_iso())

    def reserve_dns_points_to(self, ip: str) -> bool | None:
        """Факт у авторитетного сервера: True/False; None — не прочиталось."""
        name = self.reserve_dns_name()
        if not name or not ip:
            return None
        try:
            addrs, ttl = dnsapi.read_authoritative_ttl(name)
        except dnsapi.DnsError:
            return None
        self.rsv_update("rsv_dns", current=addrs[0] if addrs else "", checked_at=timeutil.now_iso(), addrs=list(addrs),
                        ttl=int(ttl))
        return addrs == [ip]                                  # вторая A-запись увела бы половину клиентов мимо

    def reserve_own_ip(self) -> str:
        """Собственный публичный адрес: `self_host` (или адрес для пользователей)
        через системный резолвер; IP как есть. Кэш 10 мин."""
        from ..selfhost import own_host
        host = own_host()
        if not host:
            return ""
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass
        cache = self.__dict__.get("_rsv_own_ip") or {}
        if cache.get("host") == host and time.monotonic() - cache.get("at", 0) < _OWN_IP_TTL and cache.get("ip"):
            return cache["ip"]
        addrs = dnsapi.read_system(host)
        ip = addrs[0] if addrs else str(cache.get("ip") or "")
        self.__dict__["_rsv_own_ip"] = {"host": host, "ip": ip, "at": time.monotonic()}
        return ip

    def reserve_peer_ip(self) -> str:
        """Адрес резерва для записи: его `self_host` из st или адрес пары."""
        peer = self.reserve_peer() or {}
        host = str(self.reserve_peer_state().get("self_host") or peer.get("host") or "").strip()
        if not host:
            return ""
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            addrs = dnsapi.read_system(host)
            return addrs[0] if addrs else ""

    # ── экран ────────────────────────────────────────────────────────────────
    def reserve_dns_screen_data(self) -> dict:
        d = self.reserve_dns()
        own = self.reserve_own_ip()
        cur = str(d.get("current") or "")
        return {"name": d["name"], "provider": self.reserve_dns_provider(),
                "provider_label": PROVIDER_LABELS.get(self.reserve_dns_provider(), "вручную"),
                "has_token": bool(d.get("token")), "current": cur, "own_ip": own, "points_here": bool(cur and cur == own),
                "checked_at": str(d.get("checked_at") or ""), "check_ok": d.get("check_ok"),
                "check_why": str(d.get("check_why") or ""), "ttl": int(d.get("ttl") or 0),
                "addrs": [str(a) for a in (d.get("addrs") or [])], "ttl_warn": TTL_WARN_S}

    # ── надзор ───────────────────────────────────────────────────────────────
    def reserve_record_watch_tick(self) -> list[Notification]:
        """Раз в 2 мин ± 40 %: запись указывает не сюда, а пользователи не у резерва —
        Т-РУ16 один раз на адрес; вернулась — отметка снята. Пока мои пользователи у
        резерва (переезд был) — молчим."""
        if not self.reserve_paired() or not self.reserve_dns_name():
            return []
        w = self.rsv_get("rsv_record_watch")
        now = time.monotonic()
        if not livestep.mono_due(w.get("mono"), now, RECORD_WATCH_SECONDS * random.uniform(0.6, 1.4)):
            return []
        self.rsv_update("rsv_record_watch", mono=now)
        if self.reserve_away() or self.reserve_out() != "home" or self.reserve_poll() == "away":
            return []                                         # право отвечать у вебхука резерва — переезд был
        own = self.reserve_own_ip()
        name = self.reserve_dns_name()
        from awgbot.domain.notices import reserve as rn
        try:
            addrs, ttl = dnsapi.read_authoritative_ttl(name)
        except dnsapi.DnsError:
            return []
        if not own:
            return []
        self.rsv_update("rsv_dns", current=addrs[0] if addrs else "", checked_at=timeutil.now_iso(), addrs=list(addrs),
                        ttl=int(ttl))
        if not addrs:                                         # записи нет вовсе — клиенты не резолвят имя
            if w.get("told") == "missing":
                return []
            self.rsv_update("rsv_record_watch", told="missing", told_at=timeutil.now_iso())
            log.warning("резерв: записи %s у авторитетного сервера нет", name)
            return [Notification(tg_id=int(config.ADMIN_ID), text=rn.record_missing(name), force_sound=True)]
        if addrs == [own]:
            if w.get("told"):
                self.rsv_update("rsv_record_watch", told="")
            return []
        if own in addrs:                                      # свой адрес среди нескольких: вторая запись чужая
            key = "multi:" + ",".join(addrs)
            if w.get("told") == key:
                return []
            self.rsv_update("rsv_record_watch", told=key, told_at=timeutil.now_iso())
            log.warning("резерв: у записи %s несколько адресов: %s", name, ", ".join(addrs))
            return [Notification(tg_id=int(config.ADMIN_ID), text=rn.record_multi(name, addrs), force_sound=True)]
        if w.get("told") == addrs[0]:
            return []
        self.rsv_update("rsv_record_watch", told=addrs[0], told_at=timeutil.now_iso())
        log.warning("резерв: запись %s указывает на %s, а не сюда — переезда не было", name, addrs[0])
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.record_foreign(name, addrs[0]),
                             force_sound=True, action=("rsv_record_back", 0))]

    def reserve_record_return(self) -> str:
        """«↩️ Вернуть запись»: на свой адрес; пусто — ок, иначе причина."""
        own = self.reserve_own_ip()
        if not own:
            return "свой адрес не определился"
        try:
            self.reserve_dns_switch(own)
        except ServiceError as e:
            return str(e)
        self.rsv_update("rsv_record_watch", told="")
        return ""
