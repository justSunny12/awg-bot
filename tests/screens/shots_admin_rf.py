"""Снимки админа: РФ-доступ — свой и профиля; трафик списком и строка РФ в карточках."""
from __future__ import annotations

from awgbot.bot.callbacks import ClientCB, DeviceCB, Menu, RoutingCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (ADM, BR, GB, IPHONE, KS, LAPTOP, PHONE, _base, _client,
                                              _ks_many, _people, _quiet, _tag)
from tests.screens.shots_admin_common_gw import NASPI, _routing, _slots


def _ks_sites(services):
    """У Ксюши три своих сайта в РФ-доступе."""
    who = _people(services)
    services.routing_add_domains(KS, "kinopoisk.ru ozon.ru gosuslugi.ru")
    return who


RF = [
    # ── 1.3 РФ-доступ: свой и профиля ───────────────────────────────────────
    Shot("adm.rf.own", role="admin", press=[RoutingCB(action="panel", ref=ADM)], data=_people,
         title="свой РФ-доступ"),
    Shot("adm.rf.own.empty", role="admin", press=[RoutingCB(action="panel", ref=ADM)], data=_quiet,
         title="свой РФ-доступ без устройств"),
    Shot("adm.rf.client", role="admin", press=[RoutingCB(action="panel", ref=KS)], data=_people,
         title="РФ-доступ профиля из карточки"),
    Shot("adm.rf.denied", role="admin", press=[RoutingCB(action="panel", ref=BR)], data=_people,
         title="профилю РФ-доступ не выдан"),
    Shot("adm.rf.dev", role="admin", press=[RoutingCB(action="dev", ref=PHONE)], data=_people,
         title="отметить устройство — ряд «✅ Применить»"),
    Shot("adm.rf.dev.apply", role="admin", press=[RoutingCB(action="dev", ref=PHONE), RoutingCB(action="apply", ref=KS)],
         data=_people, title="отметка применена — всплывашка по имени, ряд снова «⬅️ Назад»"),
    Shot("adm.rf.dev.revert", role="admin",
         press=[RoutingCB(action="dev", ref=PHONE), RoutingCB(action="dev", ref=PHONE)], data=_people,
         title="отметку вернули — «✅ Применить» ушла"),
    Shot("adm.rf.all", role="admin", press=[RoutingCB(action="panel", ref=KS), RoutingCB(action="all", ref=KS)],
         data=_people, title="«✅ Все устройства» при всех включённых — снимает отметки, не применяет"),
    Shot("adm.rf.all.apply", role="admin",
         press=[RoutingCB(action="panel", ref=KS), RoutingCB(action="all", ref=KS), RoutingCB(action="apply", ref=KS)],
         data=_people, title="снятые отметки применены — «РФ-доступ: выключен N»"),
    Shot("adm.rf.apply.stale", role="admin", press=[RoutingCB(action="apply", ref=KS)], data=_people,
         title="«✅ Применить» без отметок (кнопка из старого сообщения) — отказ"),
    Shot("adm.rf.sites.empty", role="admin", press=[RoutingCB(action="sites", ref=KS)], data=_people,
         title="сайты, пусто"),
    Shot("adm.rf.add", role="admin", press=[RoutingCB(action="add", ref=KS, tag="panel")], data=_people,
         title="добавить сайты: приглашение"),
    Shot("adm.rf.add.done", role="admin", press=[RoutingCB(action="add", ref=KS, tag="panel")],
         text="kinopoisk.ru\nhttps://www.ozon.ru/catalog\nне домен", data=_people,
         title="добавить сайты: отчёт и раздел"),
    Shot("adm.rf.sites", role="admin", press=[RoutingCB(action="sites", ref=KS)], data=_ks_sites,
         title="сайты списком"),
    Shot("adm.rf.del", role="admin", press=[RoutingCB(action="del", ref=KS, idx=1, tag=_tag("kinopoisk.ru"))],
         data=_ks_sites, title="убрать сайт"),
    Shot("adm.rf.clear", role="admin", press=[RoutingCB(action="clear", ref=KS)], data=_ks_sites,
         title="очистить список?"),
    Shot("adm.rf.clear.yes", role="admin", press=[RoutingCB(action="clear_yes", ref=KS)], data=_ks_sites,
         title="список очищен"),
]


# ── шаг 5: трафик списком и строка РФ в карточках — ветки из e2e ────────────

def _traffic_world(*, enabled: bool = True, rf_total=None, outside: int = 0, gone: bool = False):
    """Профили для экранов трафика без шлюзов: «Молчун» без трафика, Ксюша
    (РФ разрешён) с 2 ГБ, у её телефона rf_total — РФ-часть; РФ-итог сервера
    — сумма устройств плюс outside байт; gone — у Ксюши был «Ноут» с 2 ГБ
    РФ, его удалили в середине месяца."""
    def build(services, mp):
        who = _base(services)
        _routing(services, mp, enabled=enabled)
        _client(services, "Молчун", 2011, devices=("Телефон",))
        ks = _client(services, "Ксюша", 2012, devices=("Телефон",), routing=True)
        phone = services.db.list_devices(ks)[0].id
        services.db.add_traffic_bulk([(phone, GB, GB)])
        rx, tx = rf_total or (0, 0)
        if rx or tx:
            services.db.rf_add_bulk([(phone, rx, tx)])
        if gone:
            dev = services.add_device(ks, "Ноут").device_id
            services.db.add_traffic_bulk([(dev, GB, GB)])
            services.db.rf_add_bulk([(dev, GB, GB)])
            services.remove_device(dev)
            rx, tx = rx + GB, tx + GB
        services.db.set_state("rf_month_rx", str(rx))
        services.db.set_state("rf_month_tx", str(tx + outside))
        return who
    return build


# профили матрицы строки РФ: (имя, РФ разрешён, РФ-часть за месяц)
_RF_MATRIX = (("Аня", True, 0), ("Боря", False, 0), ("Вера", False, 4), ("Гоша", True, 4))
RFM = {n: (3 + i, 1 + i) for i, (n, _a, _r) in enumerate(_RF_MATRIX)}      # имя → (профиль, устройство)


def _rf_matrix(*, enabled: bool, selfcheck: bool = True):
    """Строка «└ 🇷🇺 РФ-доступ» в карточках и списке по правилу «разрешён и
    функция включена, или за месяц было»: четыре профиля по одному
    устройству (_RF_MATRIX), у каждого 2 ГБ трафика; selfcheck — самопроверка
    обвязки проходит."""
    def build(services, mp):
        from awgbot.infra import routing as rt
        who = _base(services)
        _routing(services, mp, enabled=enabled)
        if not selfcheck:
            mp.setattr(rt.selfcheck, "available", lambda: False)
        total = 0
        for i, (name, allowed, rf_gb) in enumerate(_RF_MATRIX):
            cid = _client(services, name, 2020 + i, devices=("Телефон",), routing=allowed)
            dev = services.db.list_devices(cid)[0].id
            assert (cid, dev) == RFM[name]
            services.db.add_traffic_bulk([(dev, GB, GB)])
            if rf_gb:
                services.db.rf_add_bulk([(dev, GB, (rf_gb - 1) * GB)])
                total += rf_gb * GB
        services.db.set_state("rf_month_rx", str(total // 4))
        services.db.set_state("rf_month_tx", str(total - total // 4))
        return who
    return build


def _rf_gateway(services, mp):
    """Профиль админа: iPhone и устройство-шлюз NASPi, у обоих трафик и
    РФ-счётчики (у шлюза — как если бы байты на него всё-таки легли)."""
    who = _slots(1)(services, mp)
    services.db.add_traffic_bulk([(NASPI, 3 * GB, 3 * GB)])
    services.db.rf_add_bulk([(IPHONE, GB, GB), (NASPI, 5 * GB, 5 * GB)])
    services.db.set_state("rf_month_rx", str(GB // 2 + GB))
    services.db.set_state("rf_month_tx", str(GB // 10 + GB))
    return who


def _no_feature(services):
    """Сервер без функции РФ-доступа: у админа телефон с трафиком."""
    who = _base(services)
    dev = services.add_device(ADM, "phone").device_id
    services.db.add_traffic_bulk([(dev, GB, GB)])
    return who


_LIVE = [("start", "")]                 # главная — живое меню #2, ссылка правит его


TRAFFIC = [
    Shot("adm.traffic.no_rf", role="admin", press=[Menu(action="traffic")], data=_traffic_world(),
         title="трафик: РФ-итог 0 при живом трафике, профиль без трафика выпал"),
    Shot("adm.traffic.outside", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, 0), outside=GB), title="трафик: «Вне профилей» под РФ-итогом"),
    Shot("adm.traffic.outside.noise", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), outside=GB // 100 - 1),
         title="трафик: «Вне профилей» меньше 0.01 ГБ — шум, строки нет"),
    Shot("adm.traffic.outside.min", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), outside=GB // 100),
         title="трафик: «Вне профилей» ровно 0.01 ГБ"),
    Shot("adm.traffic.outside.deleted", role="admin", press=[Menu(action="traffic")],
         data=_traffic_world(rf_total=(GB, GB), gone=True),
         title="трафик: РФ удалённого устройства — во «Вне профилей»"),
    Shot("adm.traffic.old_back", role="admin", press=[Menu(action="traffic_local")], data=_people,
         title="«Назад» прежнего экрана РФ — дерево трафика"),
    Shot("adm.traffic.no_feature", role="admin", press=[Menu(action="traffic")], data=_no_feature,
         title="трафик: сервер без функции РФ-доступа"),
    Shot("adm.link.traffic_dev.idle", role="admin", start=f"traffic-{KS}", data=_ks_many,
         title="трафик профиля: устройства без трафика выпали, итог — только профиля"),
    Shot("adm.link.traffic_dev.gw", role="admin", start=f"traffic-{ADM}", data=_rf_gateway,
         title="трафик профиля со шлюзом: у шлюза нет РФ-ветки"),
    Shot("adm.link.traffic_dev.no_feature", role="admin", start=f"traffic-{ADM}", data=_no_feature,
         title="трафик профиля: сервер без функции РФ-доступа"),
    Shot("adm.link.traffic_dev.missing", role="admin", start="traffic-99", data=_people,
         title="трафик профиля, которого нет — главная"),
    Shot("adm.link.traffic_local_dev.short", role="admin", start=f"traffic_local-{KS}", data=_people,
         title="прежняя ссылка «traffic_local-<id>» без хвоста"),
    Shot("adm.link.traffic_dev.t", role="admin", start=f"traffic-{KS}-t", data=_people,
         title="ссылка «traffic-<id>-t»"),
    Shot("adm.link.traffic.live", role="admin", steps=_LIVE + [("start", "traffic")], data=_people,
         title="/start traffic при живой главной — правка меню, команда убрана"),
    Shot("adm.link.traffic_local.live", role="admin", steps=_LIVE + [("start", "traffic_local")],
         data=_people, title="прежняя /start traffic_local при живой главной — то же дерево"),
    Shot("adm.cl.admin", role="admin", press=[ClientCB(action="open", client_id=ADM)], data=_people,
         title="карточка своего профиля — главная"),
    Shot("adm.traffic.rf_on", role="admin", press=[Menu(action="traffic")], data=_rf_matrix(enabled=True),
         title="строки РФ в списке: функция включена"),
    Shot("adm.traffic.rf_off", role="admin", press=[Menu(action="traffic")], data=_rf_matrix(enabled=False),
         title="строки РФ в списке: функция выключена"),
]


RF_LINES = []

for _on, _names in ((True, ("Аня", "Боря", "Вера")), (False, ("Аня", "Гоша"))):
    for _n in _names:
        _cid, _dev = RFM[_n]
        _what = dict((n, (a, r)) for n, a, r in _RF_MATRIX)[_n]
        _desc = (f"функция {'включена' if _on else 'выключена'}, РФ {'разрешён' if _what[0] else 'не разрешён'}, "
                 f"за месяц {_what[1]} ГБ")
        RF_LINES += [
            Shot(f"adm.cl.rf.{'on' if _on else 'off'}.{_cid}", role="admin",
                 press=[ClientCB(action="open", client_id=_cid)], data=_rf_matrix(enabled=_on),
                 title=f"строка РФ в карточке профиля: {_desc}"),
            Shot(f"adm.dev.rf.{'on' if _on else 'off'}.{_dev}", role="admin",
                 press=[DeviceCB(action="open", device_id=_dev)], data=_rf_matrix(enabled=_on),
                 title=f"строка РФ в карточке устройства: {_desc}"),
        ]


SELFCHECK = [
    Shot("adm.cl.rf.selfcheck", role="admin", press=[ClientCB(action="open", client_id=RFM["Аня"][0])],
         data=_rf_matrix(enabled=True, selfcheck=False),
         title="строка РФ в карточке профиля: самопроверка обвязки не прошла — «0 ГБ» остаётся"),
    Shot("adm.dev.rf.selfcheck", role="admin", press=[DeviceCB(action="open", device_id=RFM["Аня"][1])],
         data=_rf_matrix(enabled=True, selfcheck=False),
         title="строка РФ в карточке устройства: самопроверка обвязки не прошла — «0 ГБ» остаётся"),
]


# ── шаг 5: РФ-доступ профиля — отказы старых кнопок и правка чужого списка ──

def _ks_sites_revoked(services):
    """У Ксюши три сайта, а разрешение на РФ-доступ у неё уже отозвали."""
    who = _ks_sites(services)
    services.set_routing_allowed(KS, False)
    return who


def _ks_laptop_off(services):
    """РФ-доступ Ксюши: «Ноутбук» выключен вручную, «Телефон» включён."""
    who = _people(services)
    services.set_routing_device(LAPTOP, False)
    return who


PROFILE = [
    Shot("adm.rf.all.denied", role="admin", press=[RoutingCB(action="all", ref=BR)], data=_people,
         title="«все» у профиля без разрешения — отказ"),
    Shot("adm.rf.del.stale_idx", role="admin",
         press=[RoutingCB(action="del", ref=KS, idx=9, tag=_tag("kinopoisk.ru"))], data=_ks_sites,
         title="убрать сайт: номер за концом списка — отказ"),
    Shot("adm.rf.del.stale_tag", role="admin", press=[RoutingCB(action="del", ref=KS, idx=1, tag="deadbeef")],
         data=_ks_sites, title="убрать сайт: метка не та — отказ, сосед цел"),
    Shot("adm.rf.del.revoked", role="admin",
         press=[RoutingCB(action="del", ref=KS, idx=1, tag=_tag("kinopoisk.ru"))], data=_ks_sites_revoked,
         title="убрать сайт после отзыва разрешения — отказ"),
    Shot("adm.rf.sites.add.done", role="admin", press=[RoutingCB(action="add", ref=KS)], text="z.ru",
         data=_ks_sites, title="добавить сайт из «Сайтов» профиля — снова «Сайты», «Назад» в его раздел"),
    Shot("adm.rf.devs", role="admin", press=[RoutingCB(action="devs", ref=KS)], data=_ks_laptop_off,
         title="старое действие «устройства» — раздел профиля, одно устройство выключено"),
    Shot("adm.rf.dev.last", role="admin", press=[RoutingCB(action="dev", ref=LAPTOP)], data=_ks_laptop_off,
         title="включили вручную последнее — «✅ Выбрать все»"),
]
