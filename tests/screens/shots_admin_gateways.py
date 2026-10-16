"""Снимки админа: «🛰 Шлюзы» — слоты, назначение, параметры, снятие, автопереключение."""
from __future__ import annotations

import base64
import json

from awgbot.bot.callbacks import DeviceCB, GwMarkCB, GwSlotCB, SetCB
from awgbot.core import config

from tests.screens.base import NOW, Shot
from tests.screens.shots_admin_common import BR, IPHONE, MAC, _base, _fw, _people, _raise
from tests.screens.shots_admin_common_gw import (LAN2, NASPI, PI4, TOKEN, TOKEN2, _active1, _card,
                                                 _drop_slot2, _gw, _label, _routing, _slots)


def _rt_off(services, mp):
    """РФ-доступ развёрнут, но выключен."""
    who = _people(services)
    _routing(services, mp, enabled=False)
    return who


def _rt_asleep(services, mp):
    """Обвязка развёрнута, бот ещё не перезапущен — функция спит."""
    who = _people(services)
    _routing(services, mp, awake=False)
    return who


def _rt_none(services, mp):
    """Включено, шлюз ещё не назначен; у админа есть устройства-кандидаты."""
    who = _people(services)
    _routing(services, mp)
    return who


def _rt_none_bare(services, mp):
    """Включено, кандидатов нет: у админа ни одного устройства."""
    who = _base(services)
    _routing(services, mp)
    return who


def _rt_unavailable(services):
    """РФ-доступ админу не виден: функция недоступна."""
    who = _people(services)
    services.routing_available = lambda: False
    return who


GATEWAYS = [
    # ── 1.6 шлюзы и РФ-доступ в настройках (вход — «🛰 Шлюзы» с главной) ────
    Shot("adm.rt.provision", role="admin", press=[SetCB(sec="rt")], data=_people,
         title="РФ-доступ не развёрнут"),
    Shot("adm.rt.provision.do", role="admin", press=[SetCB(sec="rt", act="do", key="provision")],
         data=_rt_none, title="развернуть: итог"),
    Shot("adm.rt.asleep", role="admin", press=[SetCB(sec="rt")], data=_rt_asleep,
         title="обвязка есть, функция спит до перезапуска"),
    Shot("adm.rt.off", role="admin", press=[SetCB(sec="rt")], data=_rt_off, title="РФ-доступ выключен"),
    Shot("adm.rt.empty", role="admin", press=[SetCB(sec="rt")], data=_rt_none, title="шлюз не назначен"),
    Shot("adm.rt.one", role="admin", press=[SetCB(sec="rt")], data=_slots(1), title="один шлюз"),
    Shot("adm.rt.two", role="admin", press=[SetCB(sec="rt")], data=_slots(2), title="два шлюза, резерв жив"),
    Shot("adm.rt.two.down", role="admin", press=[SetCB(sec="rt")], data=_slots(2, standby="down"),
         title="два шлюза, резерв ещё проверяется (🟡) — переключение на него предлагается"),
    Shot("adm.rt.gw", role="admin", press=[SetCB(sec="rt_gw")], data=_rt_none, title="назначить: выбор машины"),
    Shot("adm.rt.gw.standby", role="admin", press=[GwSlotCB(action="add")], data=_slots(1),
         title="резервный шлюз: выбор машины"),
    Shot("adm.rt.gw.full", role="admin", press=[SetCB(sec="rt_gw")], data=_slots(2),
         title="слоты заняты"),
    Shot("adm.rt.gw.replace", role="admin", press=[SetCB(sec="rt_gw", key="1")], data=_slots(1),
         title="заменить машину слота"),
    Shot("adm.rt.pick_list", role="admin", press=[GwMarkCB(action="pick_list")], data=_rt_none,
         title="из моих устройств"),
    Shot("adm.rt.pick_list.empty", role="admin", press=[GwMarkCB(action="pick_list")], data=_rt_none_bare,
         title="из моих устройств: пусто"),
    Shot("adm.rt.pick", role="admin", press=[GwMarkCB(action="pick", device_id=MAC)], data=_rt_none,
         title="назначить шлюзом?"),
    Shot("adm.rt.pick.replace", role="admin", press=[GwMarkCB(action="pick", device_id=PI4, slot=1)],
         data=_slots(1), title="заменить машину на эту?"),
    Shot("adm.rt.mark_yes", role="admin", press=[GwMarkCB(action="mark_yes", device_id=MAC)], data=_rt_none,
         title="токен бота шлюза: приглашение"),
    Shot("adm.rt.new_ask.replace", role="admin", press=[GwMarkCB(action="new_ask", slot=1)], data=_slots(1),
         title="новое устройство вместо прежнего?"),
    Shot("adm.rt.token.done", role="admin", press=[GwMarkCB(action="new_ask")], text=TOKEN, data=_rt_none,
         title="токен принят: инструкция и файл первого применения"),
    Shot("adm.rt.users", role="admin", press=[SetCB(sec="rt_users")], data=_slots(1), title="кому доступен"),
    Shot("adm.rt.users.allow", role="admin", press=[SetCB(sec="rt", act="do", key="allow", val=str(BR))],
         data=_slots(1), title="отметить профиль — ряд «✅ Применить»"),
    Shot("adm.rt.users.allow.apply", role="admin",
         press=[SetCB(sec="rt", act="do", key="allow", val=str(BR)), SetCB(sec="rt", act="do", key="allow_apply")],
         data=_slots(1), title="отметка применена — разрешено профилю, всплывашка по имени"),
    Shot("adm.rt.users.apply.stale", role="admin", press=[SetCB(sec="rt", act="do", key="allow_apply")],
         data=_slots(1), title="«✅ Применить» без отметок — отказ"),
    Shot("adm.rt.params", role="admin", press=[SetCB(sec="rt_params")], data=_slots(1), title="параметры"),
    Shot("adm.rt.params.cycle", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.probe_seconds")], data=_slots(1),
         title="такт зонда циклом"),
    Shot("adm.rt.params.lists", role="admin", press=[SetCB(sec="rt", act="do", key="lists_refresh")],
         data=_slots(1), title="списки обновлены"),
    Shot("adm.rt.disable", role="admin",
         press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")], data=_slots(1),
         title="выключить РФ-доступ для всех?"),
    Shot("adm.rt.disable.yes", role="admin", press=[SetCB(sec="rt", act="do", key="off!")], data=_slots(1),
         title="РФ-доступ выключен"),
    Shot("adm.rt.enable", role="admin", press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")],
         data=_rt_off, title="включить РФ-доступ"),
    Shot("adm.gw.card", role="admin", press=[GwSlotCB(action="card", slot=1)], data=_slots(1),
         title="карточка слота"),
    Shot("adm.gw.card.standby", role="admin", press=[GwSlotCB(action="card", slot=2)], data=_slots(2),
         title="карточка резервного"),
    Shot("adm.gw.card.lan", role="admin", press=[GwSlotCB(action="card", slot=1)], data=_slots(1, lan=True),
         title="карточка: VPN-транзит включён"),
    Shot("adm.gw.card.missing", role="admin", press=[GwSlotCB(action="card", slot=9)], data=_slots(1),
         title="слота нет"),
    Shot("adm.gw.dev", role="admin", press=[DeviceCB(action="open", device_id=NASPI)], data=_slots(1),
         title="устройство-шлюз — карточка слота"),
    Shot("adm.gw.edit", role="admin", press=[GwSlotCB(action="edit", slot=1)], data=_slots(1),
         title="«✏️ Изменить» слота"),
    Shot("adm.gw.edit.two", role="admin", press=[GwSlotCB(action="edit", slot=2)], data=_slots(2),
         title="«✏️ Изменить» при двух слотах"),
    Shot("adm.gw.pref", role="admin", press=[GwSlotCB(action="pref", slot=2)], data=_slots(2),
         title="предпочтительный при старте"),
    Shot("adm.gw.ping", role="admin", press=[GwSlotCB(action="ping", slot=1)], data=_slots(1), title="пинг"),
    Shot("adm.gw.switch_ask", role="admin", press=[GwSlotCB(action="switch_ask", slot=2)], data=_slots(2),
         title="переключить трафик?"),
    Shot("adm.gw.switch_ask.down", role="admin", press=[GwSlotCB(action="switch_ask", slot=2)],
         data=_slots(2, standby="down"), title="переключить на лежащий?"),
    Shot("adm.gw.switch_yes", role="admin", press=[GwSlotCB(action="switch_yes", slot=2)], data=_slots(2),
         title="трафик переключён"),
    Shot("adm.gw.lan_ask.nonet", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)], data=_slots(1),
         title="VPN-транзит без локальной подсети"),
    Shot("adm.gw.lan_ask", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_slots(1, subnets=True), title="включить VPN-транзит?"),
    Shot("adm.gw.lan_ask.off", role="admin", press=[GwSlotCB(action="lan_ask", slot=1)],
         data=_slots(1, lan=True), title="выключить VPN-транзит?"),
    Shot("adm.gw.lan_yes", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="1")],
         data=_slots(1, subnets=True), title="VPN-транзит включён"),
    Shot("adm.gw.router", role="admin", press=[GwSlotCB(action="router", slot=1)], data=_slots(1, lan=True),
         title="рецепт роутера: MikroTik"),
    Shot("adm.gw.router.ow", role="admin", press=[GwSlotCB(action="router", slot=1, val="ow")],
         data=_slots(1, lan=True), title="рецепт роутера: OpenWrt"),
    Shot("adm.gw.bundle", role="admin", press=[GwSlotCB(action="bundle", slot=1)], data=_slots(1),
         title="конфигурация шлюза файлом"),
    Shot("adm.gw.bundle.card", role="admin",
         press=[GwSlotCB(action="bundle", slot=1), SetCB(sec="rt", act="do", key="bundle_cancel", val="1")],
         data=_slots(1), title="«🛰 В карточку» под файлом"),
    Shot("adm.gw.bundle.home", role="admin",
         press=[GwSlotCB(action="bundle", slot=1), SetCB(sec="rt", act="do", key="bundle_home", val="1")],
         data=_slots(1), title="«⬅️ На главную» под файлом"),
    Shot("adm.gw.lan_nets", role="admin", press=[GwSlotCB(action="lan_nets", slot=1)], data=_slots(1),
         title="локальные подсети: приглашение"),
    Shot("adm.gw.lan_nets.done", role="admin", press=[GwSlotCB(action="lan_nets", slot=1)], text="192.168.1.0/24",
         data=_slots(1), title="локальные подсети: итог"),
    # кнопка «🗺 Подсети» в сообщении, оставшемся в чате с версии до 3.3.0:
    # прежнее имя действия ведёт в тот же ввод (один выпуск)
    Shot("adm.gw.lan_nets.legacy", role="admin", press=[GwSlotCB(action="home", slot=1)], data=_slots(1),
         title="локальные подсети: прежний колбэк из старого сообщения"),
    Shot("adm.gw.label", role="admin", press=[GwSlotCB(action="label", slot=1)], data=_slots(1),
         title="подпись: приглашение"),
    Shot("adm.gw.label.done", role="admin", press=[GwSlotCB(action="label", slot=1)], text="дача",
         data=_slots(1), title="подпись: итог"),
    Shot("adm.gw.name.done", role="admin", press=[GwSlotCB(action="name", slot=1)], text="NAS", data=_slots(1),
         title="имя устройства слота: итог"),
    Shot("adm.gw.remove_ask", role="admin", press=[GwSlotCB(action="remove_ask", slot=1)], data=_slots(1),
         title="снять единственный шлюз?"),
    Shot("adm.gw.remove_ask.active", role="admin", press=[GwSlotCB(action="remove_ask", slot=1)],
         data=_slots(2), title="снять активный при живом резерве?"),
    Shot("adm.gw.remove_ask.standby", role="admin", press=[GwSlotCB(action="remove_ask", slot=2)],
         data=_slots(2), title="снять резервный?"),
    Shot("adm.gw.remove_yes", role="admin", press=[GwSlotCB(action="remove_yes", slot=2)], data=_slots(2),
         title="резервный снят"),
    Shot("adm.gw.failover", role="admin", press=[GwSlotCB(action="failover")],
         data=_slots(2, failover=False), title="автопереключение снова включено"),
    Shot("adm.main.gw", role="admin", start="", data=_slots(2), title="главная со шлюзами"),
    Shot("adm.main.rt_off", role="admin", start="", data=_rt_unavailable,
         title="главная: РФ-доступ админу не виден"),
]


def _token2(services, mp):
    """Бот второго слота уже известен серверу — токен спрашивать не нужно."""
    services._shot_tokens[2] = TOKEN2


def _pref2(services, mp):
    services.db.gateway_set_preferred(2)


def _standby_fails(services, mp):
    """Окно замеров резерва — сплошные отказы."""
    for _ in range(10):
        services._rt_window_push(2, False)


def _link_down(services, mp):
    services.routing_link_ok = lambda: False


def _no_streak(services, mp):
    services.db.set_state("routing_gw_2_up_streak", "")


def _switched(services, mp):
    services.gateway_switch(2, manual=True)


def _long_dead(services, mp):
    _standby_fails(services, mp)
    services.db.set_state("routing_gw_2_down_since",
                          str(int(NOW.timestamp()) - 13 * 86400 - 4 * 3600))


def _lan_standby(services, mp):
    services.db.gateway_update(2, lan_subnets=[LAN2], lan_mode=1)


def _peer_no_lan(services, mp):
    services.db.gateway_update(2, lan_mode=0)


def _peer_space_off(services, mp):
    mp.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")


def _peer_same(services, mp):
    services.db.gateway_update(2, lan_subnets=["192.168.1.0/24"])


def _token_kept(services, mp):
    """Запись env не удалась: токен снятого слота остался."""
    services.forget_gw_bot_token = lambda slot=None: None


def _slot7(services, mp):
    services.gateway_next_slot = lambda: (7, "awglink7", 9443, "10.99.99.24/30")


def _no_free_slot(services, mp):
    services.gateway_next_slot = _raise("service", "свободных слотов нет")


def _fresh_gw(services, mp):
    """Шлюз только что назначен: линк ещё ни разу не ответил."""
    services.routing_link_ok = lambda: False
    services._probe_slot = lambda g, active=False: "down"


def _allow_all(services, mp):
    for c in services.routing_grantable_clients():
        services.set_routing_allowed(c.id, True)


def _ceiling(services, mp):
    mp.setattr(config, "ROUTING_GATEWAYS_MAX", 1)


def _lists_aged(services, mp):
    """Списки: две записи из трёх источников, обновлены 2 ч назад, период 12 ч."""
    from awgbot.core import settings
    services._routing_read_cache = lambda name: ["a.ru", "b.ru"]
    services.db.set_state(services._RT_LISTS_KEY, str(int(NOW.timestamp()) - 7200))
    settings.set_value("app.routing.lists_refresh_hours", 12)
    mp.setattr(config, "ROUTING_LISTS_HOME_URLS", ["https://a.example", "https://b.example", "https://c.example"])


def _rt_none_token(services, mp):
    """Шлюза нет, токен бота первого слота уже известен."""
    who = _rt_none(services, mp)
    services._shot_tokens[1] = TOKEN
    return who


def _probe_down(services, mp):
    from awgbot.infra import routing as rt
    services.routing_probe = lambda: rt.PROBE_DOWN


_PIN = "RERERERERERERERERERERERERERERERERERERERERERE="   # ключ аплинка Ксюшиного телефона — заглушка
_CLAIM_PRIV = base64.b64encode(b"DUMMY-CLAIM-KEY-FOR-SCREENS-32B!").decode()   # ключ линка под claim — заглушка, 32 байта


def _iphone_key(services, mp):
    services.db.update_device_fields(IPHONE, public_key=_PIN)
    services._link_privkey = lambda gw=None: _CLAIM_PRIV


def _claim_key(services, mp):
    services._link_privkey = lambda gw=None: _CLAIM_PRIV


def _claim_text(pub: str) -> str:
    """Токен claim, как его подписал бы агент, — с постоянными ts и nonce
    (gwsign.sign берёт случайный nonce, а эталону нужен один и тот же текст)."""
    import hashlib
    import hmac
    from awgbot.util import gwsign
    payload = json.dumps({"act": "claim", "pub": pub, "ts": int(NOW.timestamp()), "nonce": "00" * 8, "host": ""},
                         separators=(",", ":"), ensure_ascii=False).encode()
    mac = hmac.new(gwsign._key(_CLAIM_PRIV), payload, hashlib.sha256).digest()[:20]
    return gwsign.PREFIX + gwsign.b64u(payload) + "." + gwsign.b64u(mac)


_FW_UNRESOLVED = [f"h{i}.example" for i in range(15)]


def _rename(device_id: int, name: str):
    def step(services, mp):
        services.rename_device(device_id, name)
    return step


SLOTS = [
    # ── подпись слота, пинг резерва, «⭐ При старте» ───────────────────────
    Shot("adm.rt.two.label", role="admin", press=[SetCB(sec="rt")], data=_gw(2, _label(2, "дача")),
         title="«Шлюзы»: подпись слота в скобках"),
    Shot("adm.rt.two.label.long", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _label(2, "дача"), _rename(PI4, "Raspberry Pi 5 NAS"), _rename(NASPI, "Raspberry Pi 4 NAS")),
         title="«Шлюзы»: длинные имена — режется имя, подпись на кнопке остаётся"),
    _card(2, _gw(2, _label(2, "дача")), "карточка: подпись слота в заголовке", "adm.gw.card.label"),
    _card(2, _gw(2, _lan_standby), "карточка резерва с VPN-транзитом — самый полный набор кнопок",
          "adm.gw.card.standby.lan"),
    Shot("adm.gw.ping.standby", role="admin", press=[GwSlotCB(action="ping", slot=2)], data=_slots(2),
         title="пинг резерва — свой линк"),
    Shot("adm.gw.pref.clear", role="admin", press=[GwSlotCB(action="pref", slot=2)], data=_gw(2, _active1, _pref2),
         title="«⭐ При старте» на предпочтительном — снять"),
    Shot("adm.gw.name", role="admin", press=[GwSlotCB(action="name", slot=1)], data=_slots(1),
         title="имя устройства слота: приглашение"),
    # ── переключение со списка и активного ──────────────────────────────
    Shot("adm.gw.switch_ask.list", role="admin", press=[GwSlotCB(action="switch_ask", slot=2, val="l")],
         data=_slots(2), title="переключить со списка — «Отмена» обратно в «Шлюзы»"),
    Shot("adm.gw.switch_yes.list", role="admin", press=[GwSlotCB(action="switch_yes", slot=2, val="l")],
         data=_slots(2), title="переключено со списка — снова «Шлюзы»"),
    Shot("adm.gw.switch_ask.active", role="admin", press=[GwSlotCB(action="switch_ask", slot=1)],
         data=_slots(2), title="переключить на активный — «уже идёт», без вопроса"),
    # ── ввод подсетей и подписи ──────────────────────────────────────────
    Shot("adm.gw.lan_nets.partial", role="admin", press=[GwSlotCB(action="lan_nets", slot=1)],
         text="192.168.1.0/24 мусор", data=_slots(2), title="подсети: часть не принята — названа в итоге"),
    Shot("adm.gw.lan_nets.pairs", role="admin", press=[GwSlotCB(action="lan_nets", slot=1)],
         data=_slots(1, lan=True), title="подсети: приглашение с текущими парами «настоящая → в туннеле»"),
    Shot("adm.gw.lan_nets.refused", role="admin", press=[GwSlotCB(action="lan_nets", slot=1)],
         text="192.168.0.0/22 10.0.2.0/24 10.241.0.0/24 172.16.0.0/15", data=_slots(1),
         title="подсети: пары в итоге, отказы адресов в туннеле с причинами"),
    Shot("adm.gw.label.clear", role="admin", press=[GwSlotCB(action="label", slot=2)], text="—",
         data=_gw(2, _label(2, "дача")), title="подпись: «—» убирает"),
    Shot("adm.gw.label.gone", role="admin",
         steps=[("press", GwSlotCB(action="label", slot=2)), ("call", _drop_slot2, "слот снят"),
                ("text", "дача 2")], data=_slots(2), title="подпись: слот исчез, пока висело приглашение"),
    # ── второй слот: новая машина и из моих ──────────────────────────────
    Shot("adm.rt.new_ask", role="admin", press=[GwMarkCB(action="new_ask")], data=_rt_none,
         title="новая машина — сразу токен, без подтверждения"),
    Shot("adm.rt.new_ask.standby", role="admin", press=[GwMarkCB(action="new_ask", slot=0)], data=_slots(1),
         title="резерв новой машиной — токен бота шлюза 2"),
    Shot("adm.rt.new_ask.full", role="admin", press=[GwMarkCB(action="new_ask", slot=0)],
         data=_gw(1, _no_free_slot), title="свободных слотов нет — отказ всплывашкой"),
    Shot("adm.rt.new_yes.slot7", role="admin", press=[GwMarkCB(action="new_yes", slot=0)],
         data=_gw(1, _slot7), title="номер нового слота — от сервиса (7)"),
    Shot("adm.rt.new_yes.known", role="admin", press=[GwMarkCB(action="new_yes")],
         data=_rt_none_token, title="новая машина при известном токене — не спрашивает"),
    Shot("adm.rt.new_yes.standby", role="admin", press=[GwMarkCB(action="new_yes", slot=0)],
         data=_gw(1, _token2), title="резерв новой машиной при известном токене — файл «Шлюз 2»"),
    Shot("adm.rt.token.done.standby", role="admin", press=[GwMarkCB(action="new_ask", slot=0)], text=TOKEN2,
         data=_slots(1), title="токен второго слота принят — файл и скрипт слота 2"),
    Shot("adm.rt.pick_list.standby", role="admin", press=[GwMarkCB(action="pick_list", slot=0)],
         data=_slots(1), title="резерв из моих — без назначенного"),
    Shot("adm.rt.pick.standby", role="admin", press=[GwMarkCB(action="pick", device_id=PI4, slot=0)],
         data=_slots(1), title="резерв из моих: станет резервным?"),
    Shot("adm.rt.mark_yes.standby", role="admin", press=[GwMarkCB(action="mark_yes", device_id=PI4, slot=0)],
         data=_gw(1, _token2), title="резерв из моих при известном токене — инструкция и файл"),
    Shot("adm.rt.mark_yes.standby.menu", role="admin",
         press=[GwMarkCB(action="mark_yes", device_id=PI4, slot=0),
                SetCB(sec="rt", act="do", key="bundle_cancel", val="2")],
         data=_gw(1, _token2), title="«🛰 В карточку» под файлом первого применения — карточка слота 2"),
    # ── снятие ───────────────────────────────────────────────────────────
    Shot("adm.gw.remove_ask.old", role="admin", press=[GwMarkCB(action="remove_ask", device_id=NASPI)],
         data=_slots(1), title="снять последний шлюз старой кнопкой — РФ-доступ выключится"),
    Shot("adm.gw.remove_yes.last", role="admin", press=[GwSlotCB(action="remove_yes", slot=1)],
         data=_slots(1), title="последний шлюз снят — РФ-доступ выключен до назначения"),
    Shot("adm.gw.remove_yes.old", role="admin", press=[GwMarkCB(action="remove_yes")], data=_slots(1),
         title="снять единственный старой кнопкой устройства"),
    Shot("adm.gw.remove_yes.none", role="admin", press=[GwMarkCB(action="remove_yes")], data=_rt_none,
         title="снять, когда шлюза нет, — «и так не назначен»"),
    Shot("adm.gw.remove_yes.token_kept", role="admin", press=[GwSlotCB(action="remove_yes", slot=2)],
         data=_gw(2, _token_kept), title="резерв снят, токен его бота остался в env"),
    # ── конфигурация файлом ──────────────────────────────────────────────
    Shot("adm.gw.bundle.label", role="admin", press=[GwSlotCB(action="bundle", slot=2)],
         data=_gw(2, _label(2, "дача"), token=False), title="файл слота 2: подпись слота, бот неизвестен"),
    # ── параметры циклами ────────────────────────────────────────────────
    Shot("adm.rt.params.cycle.window", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.failover.window_samples")],
         data=_slots(1), title="окно циклом — порог пересчитан от окна"),
    Shot("adm.rt.params.cycle.threshold", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.failover.min_availability")],
         data=_slots(1), title="порог циклом"),
    Shot("adm.rt.params.cycle.lists", role="admin",
         press=[SetCB(sec="rt_params", act="cycle", key="app.routing.lists_refresh_hours")],
         data=_slots(1), title="период списков циклом"),
    Shot("adm.rt.params.lists_age", role="admin", press=[SetCB(sec="rt_params")], data=_gw(1, _lists_aged),
         title="параметры: списки — записи, источники, возраст, период"),
    Shot("adm.rt.lists.old", role="admin", press=[SetCB(sec="rt_lists")], data=_slots(1),
         title="старый подраздел «Списки» — «⚙️ Параметры»"),
    Shot("adm.rt.mon.old", role="admin", press=[SetCB(sec="rt_mon")], data=_slots(1),
         title="старый подраздел «Мониторинг» — «⚙️ Параметры»"),
    Shot("adm.rt.lists.off", role="admin", press=[SetCB(sec="rt_lists")], data=_rt_off,
         title="старый подраздел при выключенном РФ-доступе — раздел пуст"),
    Shot("adm.rt.bundle.old", role="admin", press=[SetCB(sec="rt_bundle", key="1")], data=_slots(1),
         title="старый экран перед выпуском файла — его больше нет"),
    # ── строка РФ-доступа на главной ─────────────────────────────────────
    Shot("adm.main.gw.label", role="admin", start="", data=_gw(1, _label(1, "офис")),
         title="главная: один шлюз с подписью"),
    Shot("adm.main.gw.checking", role="admin", start="", data=_gw(2, _no_streak),
         title="главная: резерв ещё проверяется"),
    Shot("adm.main.gw.standby_dead", role="admin", start="", data=_gw(2, _standby_fails),
         title="главная: резерв не отвечает (🟡)"),
    Shot("adm.main.gw.both_down", role="admin", start="", data=_gw(2, _standby_fails, _link_down),
         title="главная: оба шлюза не отвечают"),
    Shot("adm.main.gw.down.standby_alive", role="admin", start="", data=_gw(2, _link_down),
         title="главная: активный не отвечает, резерв жив"),
    Shot("adm.main.gw.slot2", role="admin", start="", data=_gw(2, _switched),
         title="главная: активен второй шлюз — ссылки в свои карточки"),
    # ── VPN-транзит ──────────────────────────────────────────────────────
    Shot("adm.gw.lan_yes.off", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="0")],
         data=_slots(1, lan=True), title="VPN-транзит выключен — «❓ Роутер» ушёл"),
    Shot("adm.gw.lan_yes.again", role="admin", press=[GwSlotCB(action="lan_yes", slot=1, val="1")],
         data=_slots(1, lan=True), title="повтор «Включить» при включённом — «Уже включено»"),
    # ── связь подсетей в «Шлюзах» ────────────────────────────────────────
    Shot("adm.rt.peers.ok", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _label(2, "дача"), lan2=True), title="связь подсетей работает — парами"),
    Shot("adm.rt.peers.few", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_no_lan, lan2=True), title="связь подсетей: VPN-транзит только у одного слота — появится со вторым"),
    Shot("adm.rt.peers.off", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_space_off, lan2=True), title="связь подсетей: адреса в туннеле выключены — к карточке"),
    Shot("adm.rt.peers.same", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _peer_same, lan2=True), title="связь подсетей: одинаковые подсети — разные адреса в туннеле"),
    # ── автопереключение ─────────────────────────────────────────────────
    Shot("adm.gw.failover.off", role="admin", press=[GwSlotCB(action="failover")], data=_slots(2),
         title="автопереключение выключено — тумблер ☑️ и последствия строкой"),
    Shot("adm.rt.mon.failover", role="admin",
         press=[SetCB(sec="rt_mon", act="toggle", key="app.routing.failover.enabled")], data=_slots(2),
         title="тумблер автопереключения из старого «Мониторинга» — «Шлюзы»"),
    Shot("adm.rt.two.down.long", role="admin", press=[SetCB(sec="rt")], data=_gw(2, _long_dead),
         title="резерв не отвечает вторую неделю — сколько; «▶️ Переключить на …» для 🔴 резерва не предлагается"),
    Shot("adm.rt.two.long_name", role="admin", press=[SetCB(sec="rt")],
         data=_gw(2, _rename(PI4, "Raspberry Pi 5 NAS")),
         title="«▶️ Переключить на …» — имя с обрезкой, как на кнопках слотов"),
    Shot("adm.rt.one.ceiling", role="admin", press=[SetCB(sec="rt")], data=_gw(1, _ceiling),
         title="один слот у потолка — без «➕ Резерв»"),
    Shot("adm.rt.one.fresh", role="admin", press=[SetCB(sec="rt")], data=_gw(1, _fresh_gw),
         title="только что назначенный шлюз — не зелёный"),
    Shot("adm.rt.enable.down", role="admin", press=[SetCB(sec="rt", act="toggle", key="app.routing.enabled")],
         data=_gw(1, _probe_down, enabled=False), title="включить РФ-доступ при лежащем шлюзе"),
    # ── «👥 Кому доступен»: выбрать все ──────────────────────────────────
    Shot("adm.rt.users.all", role="admin", press=[SetCB(sec="rt", act="do", key="allow_all")], data=_slots(1),
         title="«☑️ Все профили» — отмечены все, ряд «✅ Применить»"),
    Shot("adm.rt.users.all.apply", role="admin",
         press=[SetCB(sec="rt", act="do", key="allow_all"), SetCB(sec="rt", act="do", key="allow_apply")],
         data=_slots(1), title="применено — разрешено всем, уведомления адресатам"),
    Shot("adm.rt.users.all.off", role="admin", press=[SetCB(sec="rt", act="do", key="allow_all")],
         data=_gw(1, _allow_all), title="«✅ Все профили» при всех разрешённых — отметки сняты, не применено"),
    Shot("adm.rt.users.all.off.apply", role="admin",
         press=[SetCB(sec="rt", act="do", key="allow_all"), SetCB(sec="rt", act="do", key="allow_apply")],
         data=_gw(1, _allow_all), title="снятие отметок применено — запрещено всем"),
    # ── старые кнопки файла ──────────────────────────────────────────────
    Shot("adm.gw.bundle.old", role="admin", press=[SetCB(sec="rt", act="do", key="bundle")], data=_slots(1),
         title="«📤 Выпустить файл» прежней версии — тот же выпуск"),
    Shot("adm.gw.bundle.old.two", role="admin", press=[SetCB(sec="rt", act="do", key="bundle", val="2")],
         data=_slots(2), title="«📤 Выпустить файл» прежней версии для слота 2"),
    Shot("adm.gw.bundle.menu.old", role="admin", press=[SetCB(sec="rt", act="do", key="bundle_menu", val="2")],
         data=_slots(2), title="«В меню» под файлом до 3.1.0 — убирает сообщение, главная"),
    # ── пересланное сообщение агента ─────────────────────────────────────
    Shot("adm.claim.foreign", role="admin", text=_claim_text(_PIN), data=_gw(1, _iphone_key),
         title="claim другого устройства при назначенном шлюзе — не принято"),
    Shot("adm.claim.garbage", role="admin", text="GW1:abc.def", data=_gw(1, _claim_key), title="мусор вместо токена"),
    # ── файервол ВПС ─────────────────────────────────────────────────────
    Shot("adm.set.fw.unresolved", role="admin", press=[SetCB(sec="fw")],
         data=_fw(enabled=True, present=True, allow=[], raw_allow=_FW_UNRESOLVED, unresolved=_FW_UNRESOLVED,
                  admin_ips=["x"]), title="15 имён не резолвятся — блок предупреждений"),
]
