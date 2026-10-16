"""Снимки админа: главная, ссылки /start <payload>, сообщения вне меню."""
from __future__ import annotations

from awgbot.bot.callbacks import ClientCB, Menu, NoteCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (BR, GB, IPHONE, KS, PHONE, _base, _mig_running, _people,
                                              _quiet, _two_expiring, _upd_found, _upd_waits)
from tests.screens.shots_admin_common_gw import NASPI, _gw_online, _routing, _slots


def _claim(services, mp):
    """Пересланное сообщение агента: подпись проверена, устройство назначено."""
    who = _slots(1)(services, mp)
    dev = services.db.get_device(NASPI)
    gw = services.db.gateways()[0]
    services.gateway_claim = lambda text: {"status": "marked", "device": dev, "gateway": gw}
    return who


def _claim_already(services, mp):
    who = _slots(1)(services, mp)
    dev = services.db.get_device(NASPI)
    gw = services.db.gateways()[0]
    services.gateway_claim = lambda text: {"status": "already", "device": dev, "gateway": gw}
    return who


def _claim_bad(services, mp):
    who = _slots(1)(services, mp)

    def bad(text):
        raise ValueError("подпись не сходится — сообщение не от этого шлюза или ключ линка другой")
    services.gateway_claim = bad
    return who


CLAIM = "✅ Шлюз настроен. Перешли это сообщение основному боту:\nGW1:RFVNTVk.RFVNTVk"


MAIN = [
    # ── 1.1 главная и ссылки с неё ──────────────────────────────────────────
    Shot("adm.main.quiet", role="admin", start="", data=_quiet, title="главная, никого нет"),
    Shot("adm.main", role="admin", start="", data=_people, title="главная с профилями, трафиком и онлайн"),
    Shot("adm.main.refresh", role="admin", press=[Menu(action="refresh")], data=_people,
         title="«🔄 Обновить» прежнего сообщения"),
    Shot("adm.main.refresh.link", role="admin", start="refresh", data=_people,
         title="ссылка «обновлено в … 🔄» — /start refresh: команда стёрта, главная на месте"),
    Shot("adm.online", role="admin", press=[Menu(action="online")], data=_people, title="онлайн"),
    Shot("adm.online.empty", role="admin", press=[Menu(action="online")], data=_quiet, title="онлайн, никого"),
    Shot("adm.expiring", role="admin", press=[Menu(action="expiring")], data=_people, title="истекают"),
    Shot("adm.expiring.empty", role="admin", press=[Menu(action="expiring")], data=_quiet,
         title="истекают, никого"),
    Shot("adm.traffic", role="admin", press=[Menu(action="traffic")], data=_people, title="трафик по профилям"),
    Shot("adm.traffic.empty", role="admin", press=[Menu(action="traffic")], data=_quiet, title="трафик, пусто"),
    Shot("adm.unassigned", role="admin", press=[Menu(action="unassigned")], data=_people,
         title="без профиля"),
    Shot("adm.unassigned.empty", role="admin", press=[Menu(action="unassigned")], data=_quiet,
         title="без профиля, пусто"),
]


LINKS = [
    # ── 8. ссылки /start <payload> с главной, карточек и уведомлений ────────
    Shot("adm.link.online", role="admin", start="online", data=_people, title="/start online"),
    Shot("adm.link.expiring", role="admin", start="expiring", data=_people, title="/start expiring"),
    Shot("adm.link.unassigned", role="admin", start="unassigned", data=_people, title="/start unassigned"),
    Shot("adm.link.traffic", role="admin", start="traffic", data=_people, title="/start traffic"),
    Shot("adm.link.traffic_dev", role="admin", start=f"traffic-{KS}", data=_people,
         title="трафик профиля по устройствам"),
    Shot("adm.link.traffic_local", role="admin", start="traffic_local", data=_people,
         title="прежняя ссылка «РФ-доступ» — трафик"),
    Shot("adm.link.traffic_local_dev", role="admin", start=f"traffic_local-{KS}-t", data=_people,
         title="прежняя ссылка «РФ-доступ профиля»"),
    Shot("adm.link.extend", role="admin", start=f"extend-{BR}", data=_people,
         title="продление по ссылке из «Истекают»"),
    Shot("adm.link.cl", role="admin", start=f"cl-{KS}", data=_people, title="карточка профиля по ссылке"),
    Shot("adm.link.dev", role="admin", start=f"dev-{PHONE}", data=_people, title="карточка устройства по ссылке"),
    Shot("adm.link.cl.missing", role="admin", start="cl-99", data=_people, title="профиля нет — главная"),
    Shot("adm.link.upd", role="admin", start="upd", data=_upd_found, title="/start upd — обновления"),
    Shot("adm.link.gw", role="admin", start="gw-1", data=_slots(2), title="карточка слота с главной"),
    Shot("adm.link.gw.missing", role="admin", start="gw-9", data=_slots(1), title="слота нет"),
    Shot("adm.link.gwcfg", role="admin", start="gwcfg-1", data=_slots(1),
         title="«Перевыпусти» из уведомления до 3.3.1 — устаревшая ссылка, главная"),
    Shot("adm.link.migration", role="admin", start="migration", data=_mig_running, title="/start migration"),
    Shot("adm.link.migration_cl", role="admin", start=f"migration-{KS}", data=_mig_running,
         title="переезд профиля"),
    Shot("adm.link.unknown", role="admin", start="whatever", data=_people, title="чужая ссылка — главная"),
]


OUTSIDE = [
    # ── 1.7 вне меню ────────────────────────────────────────────────────────
    Shot("adm.claim", role="admin", text=CLAIM, data=_claim,
         title="пересланное сообщение агента — назначено, конфигурацию везёт канал, файла нет"),
    Shot("adm.claim.already", role="admin", text=CLAIM, data=_claim_already, title="этот шлюз уже назначен"),
    Shot("adm.claim.bad", role="admin", text=CLAIM, data=_claim_bad, title="подпись не сошлась"),
    Shot("adm.note.extend", role="admin", press=[NoteCB(kind="extend", ref=BR)], data=_people,
         title="«Продлить» на уведомлении об истечении"),
    Shot("adm.note.unassigned", role="admin", press=[NoteCB(kind="unassigned")], data=_people,
         title="кнопка уведомления о пире без профиля"),
]


def _rt_disabled_quiet(services, mp):
    """Только админ, РФ-доступ развёрнут, но выключен, шлюзов нет."""
    who = _base(services)
    _routing(services, mp, enabled=False)
    return who


def _nobody_online(services):
    """Никто не на связи, но есть истекающий профиль и пир без профиля;
    сервер работает 12 дней 4 часа."""
    who = _people(services)
    for d in (PHONE, IPHONE):
        services.db.update_device_fields(d, last_handshake=0)
    services.db.set_state("container_started_at", "2026-09-03T05:00:00Z")
    return who


def _update_and_migration(services, mp):
    """Идёт переезд, и проверка нашла версию; тег — без «v»."""
    who = _mig_running(services, mp)
    services.update_available_tag = lambda: "1.3.0"
    return who


def _down(services, mp):
    """Сервер не отвечает, шлюз РФ-доступа — тоже."""
    who = _slots(1)(services, mp)
    services.db.set_state("server_ok_view", "0")
    services.routing_admin_status = lambda: {"ok": False, "active": "NASPi", "active_slot": 1, "standby": []}
    return who


# ── шаг 5: ветки, которые держали e2e — главная ─────────────────────────────
BRANCHES = [
    Shot("adm.main.rt_disabled", role="admin", start="", data=_rt_disabled_quiet,
         title="главная: РФ-доступ выключен, шлюзов нет"),
    Shot("adm.main.nobody_online", role="admin", start="", data=_nobody_online,
         title="главная: онлайн 0 без ссылки рядом с «Истекают» и «Без профиля»; аптайм в днях и часах"),
    Shot("adm.main.update", role="admin", start="", data=_update_and_migration,
         title="главная: доступна версия (тег без «v»), переезд — последней строкой"),
    Shot("adm.main.upd_wait", role="admin", start="", data=_upd_waits("1.2.3"),
         title="главная: допустимой версии нет, v1.3.0 ждёт шлюз — строка ожидания"),
    Shot("adm.main.upd_wait.many", role="admin", start="", data=_upd_waits("1.2.3", "1.2.0", "1.2.1", "1.2.2"),
         title="главная: ждёт четыре шлюза — три и «и ещё 1»"),
    Shot("adm.main.down", role="admin", start="", data=_down,
         title="главная: сервер не отвечает, шлюз РФ-доступа недоступен"),
    Shot("adm.link.dev.missing", role="admin", start="dev-99", data=_people,
         title="устройства нет — главная"),
    Shot("adm.online.gw", role="admin", press=[Menu(action="online")], data=_gw_online,
         title="онлайн: шлюз вверху с пометкой"),
    Shot("adm.expiring.two", role="admin", press=[Menu(action="expiring")], data=_two_expiring,
         title="истекают двое: ближайший сверху"),
]


# ── шаг 5: главная — «В меню» и строка РФ под трафиком ──────────────────────

def _rf_home(*, people: bool = False, enabled: bool = True, rx: int = 0, error: str = "",
             username: bool = True):
    """Главная со строкой РФ: функция развёрнута и включена/выключена; rx —
    РФ-итог месяца (иначе — как у построителя); error — учёт сломан (как
    его оставил опрос); username — имя бота известно."""
    def build(services, mp):
        who = _people(services) if people else _base(services)
        _routing(services, mp, enabled=enabled)
        if rx:
            services.db.set_state("rf_month_rx", str(rx))
            services.db.set_state("rf_month_tx", "0")
        if error:
            services.db.set_state("rf_acct_error", error)
        if not username:
            services.bot_username = ""
        return who
    return build


MENU = [
    Shot("adm.main.menu", role="admin", press=[Menu(action="main")], data=_people,
         title="«⬅️ В меню» — главная поверх экрана"),
    Shot("adm.cl.delete.one", role="admin", press=[ClientCB(action="delete", client_id=BR)], data=_people,
         title="удалить профиль с одним устройством?"),
    Shot("adm.main.rf_zero", role="admin", start="", data=_rf_home(),
         title="главная: функция включена — «0 ГБ» РФ под нулевым трафиком, без ссылок"),
    Shot("adm.main.rf_kept", role="admin", start="", data=_rf_home(people=True, enabled=False),
         title="главная: функция выключена, а РФ за месяц был — строка остаётся"),
    Shot("adm.main.rf_broken", role="admin", start="", data=_rf_home(
         people=True, error="nft не найден — поставь пакет nftables"),
         title="главная: учёт РФ сломан — пометка без текста ошибки ядра"),
    Shot("adm.main.rf_only", role="admin", start="", data=_rf_home(rx=GB),
         title="главная: трафика нет, а РФ есть — ссылок нет"),
    Shot("adm.main.no_username", role="admin", start="", data=_rf_home(people=True, username=False),
         title="главная: имя бота неизвестно — подписи без ссылок"),
]
