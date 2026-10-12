"""Снимки агента шлюза: панель, здоровье, события старта процесса, «Скрыть» на итоге."""
from __future__ import annotations

import copy

from awgbot.bot.handlers import gateway as gateway_handlers
from awgbot.bot.handlers import restore as restore_handlers
from awgbot.bot.callbacks import GwCB, HideCB
from awgbot.domain.gateway import GwCheck, GwStatus

from tests.screens.shots_gateway_common import _CHECKS_LAN, _CHECKS_OK, _TICK_LAN, _lan, _ssh_panel, _status, _status_lan
from tests.screens.shots_gateway_agent import _shot


_CHECKS_BAD = [
    GwCheck("линк", False, "хендшейка нет 14 мин"),
    GwCheck("таблица awg_gw_guard", False, "нет цепочки ssh_in"),
    GwCheck("ip_forward", True),
    GwCheck("маршрут к Telegram", None, "нечем проверить: нет dig"),
    GwCheck("выход наружу", False, "таймаут 5 с <проба>"),
    GwCheck("ядра", True, "модуль собран для 2 из 2"),
]

# Порядок — как в живом status(): обвязка (plumbing_checks: … «маскарад
# клиентов», сразу за ним «маскарад к роутеру»), маршруты, линк, ядра,
# локальная сеть, выход наружу.
_ROUTER_MASQ_CHECKS = [
    GwCheck("ip_forward", True),
    GwCheck("MASQUERADE/изоляция", True),
    GwCheck("цепочки таблицы", True),
    GwCheck("политика FORWARD", True),
    GwCheck("юнит реассерта", True),
    GwCheck("политика аплинка", True),
    GwCheck("маскарад в аплинк", True),
    GwCheck("маскарад клиентов", True),
    GwCheck("маскарад к роутеру", False, "правило к 192.168.1.1, а роутер теперь 192.168.1.254 — агент "
            "перевыставит обвязку", group="svc"),
    GwCheck("маршрут к Telegram", True),
    GwCheck("маршрут к GitHub", True),
    GwCheck("линк", True, "хендшейк 40 с назад"),
    GwCheck("ядра", True, "модуль собран для 2 из 2"),
    *_CHECKS_LAN,
    GwCheck("выход наружу", True, "35 мс через канал"),
]


def _status_bad(**kw) -> GwStatus:
    """Линк лежит, обвязка разошлась, питание проседает сейчас."""
    d = dict(link_up=False, handshake_age=None, checks=copy.deepcopy(_CHECKS_BAD),
             throttled={"now": ["недонапряжение"], "ever": ["недонапряжение", "троттлинг"]},
             cpu=97.0, temp=81.0, ram=88.0, disk=91.0, ram_free_mb=312, disk_free_gb=3.2,
             smart="FAIL", mark_status="foreign", ssh=_ssh_panel(sshd_down=True))
    d.update(kw)
    return _status(**d)


async def _first_panel(services, bot):
    await gateway_handlers.send_first_panel(bot, services)


async def _restart_panel(services, bot):
    await gateway_handlers.restore_panel_after_restart(bot, services)


async def _update_result(services, bot):
    from awgbot.runtime import main
    await main.report_update_result(bot, services)


async def _restore_result(services, bot):
    await restore_handlers.report_restore_result(bot, services)


async def _rebooted(services, bot):
    from awgbot.runtime import main
    await main._announce_reboot(bot, services.db, "агента")


_SMB_PEERS = ["naspi5", "backup", "Time Machine", "<b>x</b>", "media"]


PANEL = [
    # ── W00 панель ──────────────────────────────────────────────────────────
    _shot("gw.panel.start", start="", title="по /start: снимка тика нет — живой замер, без VPN-транзита"),
    _shot("gw.panel", press=[GwCB(action="panel")], tick=_TICK_LAN, update_tag="v1.2.4",
          title="«В меню»: снимок тика 2 мин назад, VPN-транзит, SMB, доступно обновление"),
    _shot("gw.panel.refresh", press=[GwCB(action="refresh")], tick=_TICK_LAN,
          live=_status_lan(month_rx=0), title="«🔄 Обновить» — живой замер поверх снимка тика"),
    _shot("gw.panel.channel.active", press=[GwCB(action="panel")], channel="active", standby=True,
          tick=_status_lan(), title="упр. канал на связи, шлюз несёт трафик"),
    _shot("gw.panel.channel.standby", press=[GwCB(action="panel")], channel="standby", standby=True,
          tick=_status(ssh=_ssh_panel(filter=True, allow=2)),
          title="упр. канал на связи, шлюз в резерве; фильтр SSH на 2 адреса"),
    _shot("gw.panel.channel.offline", press=[GwCB(action="panel")], channel="offline",
          tick=_status(mark_status="unmarked"),
          title="канал включён, но нет связи; шлюз не назначен — переслать сообщение"),
    _shot("gw.panel.unmarked.online", press=[GwCB(action="panel")], channel="active",
          tick=_status(mark_status="unmarked"), title="шлюз не назначен, запрос ушёл по каналу"),
    _shot("gw.panel.unconfirmed", press=[GwCB(action="panel")],
          tick=_status(mark_status="unconfirmed", ssh=_ssh_panel(owner="omv", new_plumbing=False)),
          title="аплинк не найден; SSH под OMV, обвязка старого образца"),
    _shot("gw.panel.broken", press=[GwCB(action="panel")], tick=_status_bad(),
          title="конфигурация расходится: чужой слот, линк лежит, проблемы, sshd не запущен"),
    _shot("gw.panel.own_pending", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(own={"active": True, "state": "no_link"},
                                    svc={"active": True, "own": [], "peer": [], "ever": False}),
                           checks=copy.deepcopy(_CHECKS_OK) + [
                               GwCheck("резолвер", False, "dnsmasq не отвечает", group="lan")]),
          title="VPN-транзит: резолвер упал, свои списки ждут синхронизации, SMB ещё не пришли"),
    _shot("gw.panel.own_failed", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(own={"active": True, "state": "failed"})),
          title="свои списки не применились — хвост со ссылкой на здоровье"),
    _shot("gw.panel.lan.empty", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(own_vpn=0, own_ru=0,
                                    svc={"active": True, "own": [], "peer": [], "ever": True})),
          title="своих доменов нет — без хвоста «свои»; лента SMB пришла пустой — «не найдены»"),
    _shot("gw.panel.smb_peers", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(svc={"active": True, "own": [], "peer": list(_SMB_PEERS), "ever": True})),
          title="SMB только от соседей (одно имя с разметкой) — счёт без нулевой части, имён на панели нет"),
    _shot("gw.panel.smb_off", press=[GwCB(action="panel")],
          tick=_status_lan(lan=_lan(svc={"active": False, "own": ["nas"], "peer": ["nas-2"], "ever": True})),
          title="связь подсетей не действует — строки 🗂 нет"),
    _shot("gw.panel.whole_days", press=[GwCB(action="panel")], tick=_status(uptime_seconds=12 * 86400),
          title="ровно 12 суток аплинка — «12 дн» без «0 ч»"),
    _shot("gw.panel.down", press=[GwCB(action="panel")],
          tick=_status(link_up=False, handshake_age=None, uptime_seconds=None),
          title="линк лежит, аптайма в замере нет — шапка без «·»"),
    _shot("gw.panel.upd_wait", press=[GwCB(action="panel")], tick=_status(), waiting=("v1.3.0", "1.3.0"),
          server_version="1.2.3", title="допустимой цели нет: v1.3.0 ждёт сервер AWG"),
    _shot("gw.panel.upd_wait.offline", press=[GwCB(action="panel")], tick=_status(),
          waiting=("v1.3.0", "1.3.0"), title="версия сервера неизвестна: ждёт сервер AWG — не на связи"),
    _shot("gw.panel.tag_without_v", press=[GwCB(action="panel")], tick=_status(), update_tag="1.2.4",
          title="тег последней проверки без «v» — на панели «v1.2.4»"),
    _shot("gw.panel.start.update", start="", tick=_status(), update_tag="v1.2.4",
          title="/start из снимка тика — строка «⬆️ Доступна» и здесь"),
    _shot("gw.panel.no_ssh", press=[GwCB(action="panel")], tick=_status(ssh={}),
          title="снимок агента без поля ssh — панель без строки SSH"),
    _shot("gw.panel.link_late", press=[GwCB(action="panel")], tick=_status(handshake_age=720.0), tick_age=40,
          title="хендшейк 12 мин назад при пороге 5 мин — 🟡; снимок 40 с назад"),
    _shot("gw.panel.link_late.raised", press=[GwCB(action="panel")], tick=_status(handshake_age=720.0),
          conf={"app.gateway.handshake_max_age": 900}, title="те же 12 мин при пороге 15 мин — 🟢"),
    _shot("gw.panel.link_hours", press=[GwCB(action="panel")], tick=_status(handshake_age=3 * 3600.0),
          title="хендшейк 3 ч назад — 🟡 «3 ч»"),

    # ── W01 здоровье ────────────────────────────────────────────────────────
    _shot("gw.health", press=[GwCB(action="health")],
          live=_status_lan(throttled={"now": [], "ever": ["недонапряжение"]}),
          title="всё в порядке, просадка питания с загрузки"),
    _shot("gw.health.broken", press=[GwCB(action="health")], live=_status_bad(),
          title="проблемы, «не проверено», питание сейчас"),
    _shot("gw.health.router_masq", press=[GwCB(action="health")], live=_status_lan(checks=copy.deepcopy(_ROUTER_MASQ_CHECKS)),
          title="адрес роутера сменился — маскарад соседей к роутеру разошёлся, починка сама "
                "(порядок проверок — как в живом замере)"),
    _shot("gw.health.no_host", press=[GwCB(action="health")], live=_status(hostname="", disk_free_gb=1234.4),
          title="имени хоста в замере нет — «Здоровье шлюза»; терабайтный диск — «1 234 ГБ»"),
    _shot("gw.health.escaped", press=[GwCB(action="health")],
          live=_status(checks=[GwCheck("<b>x</b>", False, "rc=1 & <i>")]),
          title="имя проверки и подробность с хоста экранированы"),
]


EVENTS = [
    # ── события старта процесса ─────────────────────────────────────────────
    _shot("gw.event.first_panel", call=("send_first_panel", _first_panel),
          title="первый запуск после установки — панель сама"),
    _shot("gw.event.restarted", call=("restore_panel_after_restart", _restart_panel), restart_wait=True,
          title="обещание «вернётся через несколько секунд» исполнено, панель следом"),
    _shot("gw.event.updated", call=("report_update_result", _update_result), update_pending="v1.2.3",
          update_wait=True, title="обновление применилось: «дождись» убрано, итог со списком изменений"),
    _shot("gw.event.update_failed", call=("report_update_result", _update_result), update_pending="v1.2.4",
          update_wait=True, title="обновление не применилось"),
    _shot("gw.event.restored", call=("report_restore_result", _restore_result),
          restore_done={"created_at": "2026-09-14T03:00:00+03:00"}, title="восстановлено из копии"),
    _shot("gw.event.rebooted", call=("_announce_reboot", _rebooted), title="хост перезагружен, агент запущен"),

    # ── общее: «Скрыть» на итоге ────────────────────────────────────────────
    _shot("gw.hide", press=[HideCB()], title="«Скрыть» убирает сообщение"),
]
