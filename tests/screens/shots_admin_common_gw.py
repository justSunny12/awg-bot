"""Общие построители снимков админа для РФ-доступа и шлюзов: обвязка
линка, слоты, канал до шлюза, документ конфигурации.

Сеть шлюза в снимках не трогается: события канала линка кладутся через
services.gwlink_* так, как их принял бы канал; ключи и токены — заглушки.
"""
from __future__ import annotations

import datetime as _dt

from awgbot.bot.callbacks import GwSlotCB
from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import ADM, IPHONE, _iso, _online, _people


# ── построители РФ-доступа и шлюзов ─────────────────────────────────────────

NASPI, PI4 = 7, 8                       # устройства админа под слоты (после _people)
PRIV = "RERERERERERERERERERERERERERERERERERERERERERE="   # ключ линка — заглушка
TOKEN = "1234567890:DUMMYDUMMYDUMMYDUMMYDUMMY"


def _routing(services, mp, *, enabled: bool = True, awake: bool = True) -> None:
    """Обвязка РФ-доступа развёрнута (awake — интерфейс линка прочитан при
    старте) и функция включена или выключена; сеть линка — заглушки."""
    from awgbot.core import settings
    from awgbot.infra import routing as rt
    mp.setattr(config, "ROUTING_ENABLED", awake)
    services.routing_provisioned = lambda: True
    # выключатель — настоящая настройка снимка (фейк маршрутизации держит его
    # константой): кнопки «Включить» и «Выключить» меняют экран, как в бою
    settings.set_value("app.routing.enabled", enabled)
    orig = settings.get_bool
    mp.setattr(settings, "get_bool", lambda k, d=None: (
        str(settings.get(k, d)).lower() in ("1", "true", "yes") if k == "app.routing.enabled" else orig(k, d)))
    mp.setattr(rt.probes, "ping_peer", lambda iface="", **k: 43 if iface == "awglink2" else 61)
    mp.setattr(rt.probes, "link_peer_endpoint",
           lambda iface="": "198.51.100.7" if iface == "awglink2" else "203.0.113.10")
    mp.setattr(rt.marking, "switch_active", lambda iface: None)
    services.routing_status = lambda: (True, "ок")
    services.routing_link_ok = lambda: True
    services._link_privkey = lambda gw=None: PRIV
    services._run_link_script = lambda mode, env=None: None
    services.gw_bundle_encrypted = lambda slot=None: (b"DUMMY-ENC", "awg-gw-bundle.enc")
    services.gw_bundle_plain = lambda slot=None: (b"DUMMY-PLAIN", "awg-gw-bundle.sh")
    import time as _time
    services.routing_update_lists = lambda force=False: (services.db.set_state(services._RT_LISTS_KEY, str(int(_time.time()))), 12345)[1]
    services.routing_provision = lambda: "dnsmasq: ok\nlink: awglink up"
    tokens: dict[int, str] = {}
    services.gw_bot_token = lambda slot=None: tokens.get(int(slot or 1), "")
    services.set_gw_bot_token = lambda t, slot=None: tokens.__setitem__(int(slot or 1), t)
    services.forget_gw_bot_token = lambda slot=None: tokens.pop(int(slot or 1), None)
    services._shot_tokens = tokens


def _slots(n: int = 1, *, standby: str = "alive", lan: bool = False, subnets: bool = False,
           token: bool = True, enabled: bool = True, failover: bool = True):
    """Шлюзы: n слотов на NASPi и Pi4 (активен первый); standby — резерв жив
    (alive) или лежит (down); subnets — у первого локальная подсеть, lan —
    ещё и VPN-транзит;
    token — бот шлюза известен (ссылка в карточке); failover —
    автопереключение на резерв."""
    def build(services, mp):
        from awgbot.core import settings
        who = _people(services)
        settings.set_value("app.routing.failover.enabled", failover)
        services.add_device(ADM, "NASPi")
        services.add_device(ADM, "Pi4")
        _routing(services, mp, enabled=enabled)
        if n >= 1:
            services.db.gateway_add(NASPI, "awglink", 443, "10.99.99.0/30", slot_id=1)
            if lan or subnets:
                services.db.gateway_update(1, lan_subnets=["192.168.1.0/24"], lan_mode=int(lan))
        if n >= 2:
            services.db.gateway_add(PI4, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
            if standby == "alive":
                services.db.set_state("routing_gw_2_up_streak", str(services._RT_UP_STREAK))
            else:
                services._probe_slot = lambda g, active=False: "ok" if g.id == 1 else "down"
                services.db.set_state("routing_gw_2_down_since", str(int(NOW.timestamp()) - 600))
        if token:
            for slot in range(1, n + 1):
                services.set_gw_bot_token(TOKEN, slot)
                services.set_gw_bot_identity(slot, "naspi_gw_bot" if slot == 1 else "pi4_gw_bot",
                                             "NASPi" if slot == 1 else "Pi4")
        return who
    return build


def _gw_online(services, mp):
    """Шлюз NASPi и Ксюшин телефон на связи."""
    who = _slots(1)(services, mp)
    _online(services, NASPI)
    services.db.update_device_fields(IPHONE, last_handshake=0)
    return who


# ── шаг 5: карточки слотов и «Шлюзы» — ветки, которые держали e2e ──────────
# Сцены — как в tests/e2e/test_gateway_*_ui.py: два слота с VPN-транзитом
# (у NASPi 192.168.1.0/24, у Pi4 192.168.68.0/24), события канала линка —
# через services.gwlink_* в построителе, как их принял бы канал.

LAN1, LAN2 = "192.168.1.0/24", "192.168.68.0/24"


def _gw(n: int = 2, *then, lan2: bool = False, **kw):
    """_slots(n, **kw) и поверх — lan2 (VPN-транзит и подсети у обоих
    слотов: их сети связаны, связь подсетей — часть транзита) и шаги
    then(services, mp) по порядку."""
    def build(services, mp):
        who = _slots(n, **kw)(services, mp)
        if lan2:
            services.db.gateway_update(1, lan_subnets=[LAN1], lan_mode=1)
            services.db.gateway_update(2, lan_subnets=[LAN2], lan_mode=1)
        for step in then:
            step(services, mp)
        return who
    return build


def _card(slot: int, data, title: str, sid: str):
    return Shot(sid, role="admin", press=[GwSlotCB(action="card", slot=slot)], data=data, title=title)


# ── шаг 5: «Шлюзы», слоты и назначение — ветки из e2e ───────────────────────

TOKEN2 = "2222222222:DUMMYDUMMYDUMMYDUMMYDUMMY"


def _label(slot: int, label: str):
    def step(services, mp):
        services.db.gateway_update(slot, label=label)
    return step


def _active1(services, mp):
    """Активный слот записан (холодный старт его записывает): «⭐ При старте»
    без записи двигало бы и «активного» на экране."""
    services.db.set_state(services._RT_ACTIVE_KEY, "1")


async def _drop_slot2(services, bot):
    services.db.gateway_delete(2)


# ── шаг 5: канал до шлюза в карточке слота — ветки из e2e ───────────────────
# Слот NASPi с VPN-транзитом, хендшейк линка свежий (сессия канала — «на
# связи»), резолвер слота — 10.9.1.1. Снимок кладётся так, как его принял бы
# канал; возраст снимка и часы шлюза — от NOW.

def _ch_base(services, mp):
    mp.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    services.gateway_resolver_addr = lambda g: "10.9.1.1" if g.lan_mode else ""


def _installed(services) -> dict:
    """Что стоит на шлюзе, когда всё доехало: ровно то, из чего ВПС соберёт файл."""
    addrs = " ".join(sorted(set(services.db.admin_device_addresses(config.ADMIN_ID))))
    return {"lan_mode": "1", "home_subnets": LAN1, "resolver": "10.9.1.1", "peer_home_nets": "", "admin_ips": addrs}


def _ch(bundle: dict | None = None, *, online: bool = True, closed: bool = False, age_h: float = 0,
        skew_min: int = 0, **kw):
    """Снимок слота 1: bundle — поверх установленного (None — всё доехало);
    online — сессия канала открыта; closed — закрыта после снимка; age_h —
    снимок принят столько часов назад; skew_min — часы шлюза спешат."""
    def step(services, mp):
        at = NOW - _dt.timedelta(hours=age_h)
        snap = {"bundle": {**_installed(services), **(bundle or {})}, "link_contract": "1", "plumbing_gen": "new",
                "mark_status": "confirmed", "agent_version": "3.1.0", "awg_generation": 1, "egress_ok": True,
                "boot_id": "b" * 36, "ts": _iso(at + _dt.timedelta(minutes=skew_min)), "rev": 1}
        snap.update(kw)
        services.gwlink_snapshot_in(1, snap, 1, True)
        services.db.set_state(services._gwlink_key(services._GWLINK_SNAP_AT_KEY, 1), _iso(at))
        if online:
            services.gwlink_session_opened(1, "3.1.0", 1)
        if closed:
            services.gwlink_session_closed(1)
    return step


def _chan(n: int = 1, *steps):
    return _gw(n, _ch_base, *steps, lan=True)
