"""Unit: раскладка клавиатур — правила, общие для всех экранов.

- не больше десяти рядов кнопок на экране;
- в рядах по две (и по три) подписи не длиннее 18 знаков — длиннее
  Telegram режет их многоточием на телефоне;
- тумблеры — только ✅/☑️; кружки 🟢/🔴 — состоянию объектов (устройство
  онлайн), не настройкам;
- в подтверждениях первая кнопка — «⬅️ Отмена», разрушительное действие —
  красным;
- «🏠» нигде: «В меню» — «⬅️ В меню», локальная сеть — не «дом».

Клиентские и гостевые билдеры проверяются на образцовых данных все до
одного (новый билдер без образца — красный тест). Экраны администратора и
агента переделываются следующими этапами: их нарушения — списком
исключений, он пустеет к последнему этапу; исключение, которое больше не
нужно, тоже красное.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardMarkup

from awgbot.bot.callbacks import (BlockCB, DelDeviceCB, DeviceCB, FriendCB, Menu, RoutingCB)
from awgbot.bot.keyboards import admin as kba
from awgbot.bot.keyboards import broadcast as kbb
from awgbot.bot.keyboards import client as kbc
from awgbot.bot.keyboards import common as kbm
from awgbot.bot.keyboards import gateway as kbg
from awgbot.bot.keyboards import routing as kbr
from awgbot.bot.keyboards import settings as kbs

pytestmark = pytest.mark.unit
G = 1024 ** 3
MAX_LABEL = 18


def _dev(i=1, name="iPhone", **kw):
    base = dict(id=i, name=name, block_reason=0, is_managed=True, is_gateway=0, is_lent=False,
                friend=None, friend_status=None, routing_on=1, last_handshake=None,
                holder_client_id=None, owner_name="Вася", owner_tg_name="", holder_name="Коля",
                holder_tg_name="", address=f"10.8.1.{i + 1}", private_key="k")
    base.update(kw)
    return SimpleNamespace(**base)


DEVS = [_dev(1, "iPhone"), _dev(2, "MacBook", routing_on=0),
        _dev(3, "Планшет", is_lent=True, holder_client_id=9)]
MANY = [_dev(i, f"Устройство {i}") for i in range(1, 15)]
HELD = [_dev(4, "Планшет", is_lent=True)]
BLOCKED = _dev(5, "Ноут", block_reason=int(__import__("awgbot.core.blocks", fromlist=["x"]).DeviceBlock.USER))
PENDING = _dev(6, "Другу", friend_status="pending", friend=SimpleNamespace(status="pending"))
UNMANAGED = _dev(7, "Пир", is_managed=False, private_key=None)

# билдер клиента/гостя → варианты разметки на образцовых данных
CLIENT = {
    "client_main": lambda: [kbc.client_main(has_devices=h, routing_visible=r, client_id=1)
                            for h in (True, False) for r in (True, False)],
    "guest_main": lambda: [kbc.guest_main(routing_visible=r, client_id=1, has_devices=h)
                           for h in (True, False) for r in (True, False)],
    "client_devices": lambda: [kbc.client_devices(DEVS, HELD), kbc.client_devices(MANY, HELD),
                               kbc.client_devices(MANY, page=1), kbc.client_devices([])],
    "guest_devices": lambda: [kbc.guest_devices(HELD), kbc.guest_devices(MANY)],
    "device_actions": lambda: [kbc.device_actions(d, is_admin=False, back_target="m:devices")
                               for d in (DEVS[0], BLOCKED, PENDING, UNMANAGED)],
    "lent_out_device_actions": lambda: [kbc.lent_out_device_actions(DEVS[2], "m:devices")],
    "held_device_actions": lambda: [kbc.held_device_actions(HELD[0], "m:devices"),
                                    kbc.held_device_actions(BLOCKED, "fr:list:0", cb_cls=FriendCB)],
    "block_device_confirm": lambda: [kbc.block_device_confirm(1), kbc.block_device_confirm(1, guest=True)],
    "confirm_delete_device": lambda: [kbc.confirm_delete_device(1), kbc.confirm_delete_device(1, guest=True)],
    "confirm_transfer": lambda: [kbc.confirm_transfer(1)],
    "unmanaged_device_dialog": lambda: [kbc.unmanaged_device_dialog(7)],
    "pick_device": lambda: [kbc.pick_device(DEVS, "gen_link"), kbc.pick_device(MANY, "gen_qr")],
    "guest_pick_device": lambda: [kbc.guest_pick_device(HELD * 2, "gen_file"),
                                  kbc.guest_pick_device(MANY, "gen_link")],
    "add_device_kb": lambda: [kbc.add_device_kb(for_friend=f) for f in (True, False)],
    "device_created_kb": lambda: [kbc.device_created_kb(1)],
    "invite_kb": lambda: [kbc.invite_kb("Твоё приглашение: https://t.me/b?start=F1", "https://t.me/b?start=F1")],
    "invite_menu": lambda: [kba.invite_menu("Привет! https://t.me/b?start=F1", "https://t.me/b?start=F1", 7)],
    "help_menu": lambda: [kbc.help_menu(), kbc.help_menu(is_initial=True), kbc.help_menu(guest=True)],
    "friend_finisher": lambda: [kbc.friend_finisher()],
    "guide_nav": lambda: [kbc.guide_nav("apple", s, 4, next_guide="connect_apple" if s == 4 else None,
                                        apple_connect_end=a, guest=g)
                          for s in (0, 2, 4) for a in (False, True) for g in (False, True)],
    "guide_connect_method": lambda: [kbc.guide_connect_method(1, "connect", guest=g) for g in (False, True)],
    "guide_connect_done": lambda: [kbc.guide_connect_done("connect_apple", 1, apple_end=a, guest=g)
                                   for a in (False, True) for g in (False, True)],
    "guide_connect_devices": lambda: [kbc.guide_connect_devices(DEVS, (2, 3)),
                                      kbc.guide_connect_devices(MANY, (14, 0)),
                                      kbc.guide_connect_devices(HELD, (1, 0), guest=True)],
    "added_by_admin": lambda: [kbc.added_by_admin(1)],
    "grace_offer": lambda: [kbc.grace_offer(1, 14)],
    "subscription_kb": lambda: [kbc.subscription_kb(1, paused_user=p, can_pause=c)
                                for p in (True, False) for c in (True, False)],
    "pause_kb": lambda: [kbc.pause_kb(1, n) for n in (1, 5, 7, 14, 28, 56)],
    # общие помощники, которыми пользуются экраны клиента
    "cancel_input": lambda: [kbm.cancel_input("dev", 1)],
    "confirm": lambda: [kbm.confirm(Menu(action="main"), "🗑 Удалить", Menu(action="main"))],
    "device_limit_kb": lambda: [kbm.device_limit_kb(1, lim, DeviceCB(action="open", device_id=1))
                                for lim in (0, 5 * G, 30 * G, 100 * G, 500 * G)],
    "to_menu": lambda: [kbm.to_menu()],
    "hide_only": lambda: [kbm.hide_only()],
    # РФ-доступ клиента и гостя
    "routing_panel": lambda: [kbr.routing_panel(1, d, enabled=e, total=len(d), n_domains=n,
                                                back_target="m:main")
                              for d, e in ((DEVS, 1), (DEVS[:2], 2), ([], 0), (MANY, 3))
                              for n in (0, 5)],
    "routing_sites": lambda: [kbr.routing_sites(1, doms) for doms in
                              ([], ["sber.ru", "kinopoisk.ru"], [f"site{i}.ru" for i in range(30)])],
    "routing_clear_confirm": lambda: [kbr.routing_clear_confirm(1)],
}

# не клавиатуры — помощники разметки, у них своих экранов нет
NOT_SCREENS = {"issue_row", "gen_kind", "issuable"}

# кружок — состояние объекта: устройство или профиль онлайн
_CIRCLE_OK_PREFIXES = ("d:open:", "d:gen_", "fr:open:", "fr:gen_", "c:open:")


def _buttons(m: InlineKeyboardMarkup):
    return [b for row in m.inline_keyboard for b in row]


def _violations(m: InlineKeyboardMarkup, name: str = "") -> set[str]:
    out = set()
    buttons = _buttons(m)
    if len(m.inline_keyboard) > kbm.MAX_ROWS:
        out.add("больше 10 рядов")
    for row in m.inline_keyboard:
        if len(row) >= 2 and any(len(b.text) > MAX_LABEL for b in row):
            out.add("длинная подпись в ряду")
    for b in buttons:
        if "🏠" in b.text:
            out.add("🏠")
        if b.text.startswith(("🟢", "🔴")) and not (b.callback_data or "").startswith(_CIRCLE_OK_PREFIXES):
            out.add("кружок вместо ✅/☑️")
    if "confirm" in name and buttons and buttons[0].text != "⬅️ Отмена":
        out.add("подтверждение не с «Отмены»")
    return out


@pytest.mark.parametrize("name", sorted(CLIENT))
def test_client_and_guest_keyboards_follow_the_layout_rules(name):
    for i, markup in enumerate(CLIENT[name]()):
        assert isinstance(markup, InlineKeyboardMarkup)
        bad = _violations(markup, name)
        rows = [[b.text for b in r] for r in markup.inline_keyboard]
        assert not bad, f"{name} [вариант {i}]: {sorted(bad)} — {rows}"


def test_every_client_keyboard_has_a_sample():
    """Новый билдер клиента без образца здесь — правило никто не проверит."""
    public = {n for n, f in inspect.getmembers(kbc, inspect.isfunction)
              if f.__module__ == kbc.__name__ and not n.startswith("_")}
    missing = public - NOT_SCREENS - set(CLIENT)
    assert not missing, f"нет образца: {sorted(missing)}"


def test_toggle_marks_are_ticks_for_both_roles():
    """Тумблеры обеих ролей берут значок из одного места: ✅ / ☑️."""
    assert (kbm._chk(True), kbm._chk(False)) == ("✅", "☑️")
    assert (kbm._tick(True), kbm._tick(False)) == ("✅", "☑️")


def test_the_rule_checker_itself_catches_each_rule():
    """Сторож проверки: каждое правило на заведомо плохой клавиатуре ловится."""
    from aiogram.types import InlineKeyboardButton as B
    def mk(*rows):
        return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
    cb = Menu(action="main").pack()
    assert _violations(mk(*[[B(text=str(i), callback_data=cb)] for i in range(11)])) == {"больше 10 рядов"}
    # десять рядов по две кнопки — двадцать кнопок, но правило про ряды
    assert _violations(mk(*[[B(text=str(i), callback_data=cb), B(text="x", callback_data=cb)]
                            for i in range(10)])) == set()
    assert _violations(mk([B(text="x" * 19, callback_data=cb), B(text="y", callback_data=cb)])) == \
        {"длинная подпись в ряду"}
    assert _violations(mk([B(text="🏠 В меню", callback_data=cb)])) == {"🏠"}
    assert _violations(mk([B(text="🟢 Уведомления", callback_data=cb)])) == {"кружок вместо ✅/☑️"}
    assert _violations(mk([B(text="🟢 iPhone", callback_data=DeviceCB(action="open", device_id=1).pack())])) == set()
    assert _violations(mk([B(text="🗑 Удалить", callback_data=cb), B(text="⬅️ Отмена", callback_data=cb)]),
                       "x_confirm") == {"подтверждение не с «Отмены»"}


# ── экраны администратора этапа 2: по правилам, без исключений ──────────────

def _cli(i=1, name="Ксюша", **kw):
    from awgbot.core.enums import ActivationStatus
    base = dict(id=i, name=name, tg_id=100 + i, block_reason=0,
                activation_status=ActivationStatus.ACTIVE, period_end=None, status="active",
                effective_period_end=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _card(n_devices, **kw):
    from awgbot.core.blocks import ClientBlock
    from awgbot.core.enums import ActivationStatus
    extra = {}
    if kw.pop("paused", False):
        extra["block_reason"] = int(ClientBlock.PAUSED)
    if kw.pop("pending", False):
        extra["activation_status"] = ActivationStatus.PENDING
    return kba.admin_client_actions(_cli(**extra), MANY[:n_devices], **kw)


CLIS = [_cli(i, f"Профиль {i}") for i in range(1, 15)]

ADMIN = {
    "admin_main": lambda: [kba.admin_main(gateways=g, routing_visible=r, self_client_id=1)
                           for g in (True, False) for r in (True, False)],
    "admin_client_actions": lambda: [_card(n, routing_visible=r, paused=p, pending=q)
                                     for n in (0, 1, 3, 5, 14) for r in (True, False)
                                     for p in (True, False) for q in (True, False)]
                                    + [kba.admin_client_actions(_cli(tg_id=1), DEVS, routing_visible=True)],
    "client_edit_kb": lambda: [kba.client_edit_kb(1)],
    "client_delete_confirm": lambda: [kba.client_delete_confirm(1)],
    "devs_limit_kb": lambda: [kba.devs_limit_kb(1)],
    "traffic_limit_kb": lambda: [kba.traffic_limit_kb(1)],
    "new_profile_devs_kb": lambda: [kba.new_profile_devs_kb()],
    "new_profile_traffic_kb": lambda: [kba.new_profile_traffic_kb()],
    "period_kb": lambda: [kba.period_kb("extend", 1, keep=k, has_remainder=h, min_days=m)
                          for k in (True, False) for h in (True, False) for m in (0, 7, 30)]
                         + [kba.period_kb("create")],
    "block_pause_kb": lambda: [kba.block_pause_kb(1)],
    "block_notify_kb": lambda: [kba.block_notify_kb(t, 1, d) for t in ("cli", "dev") for d in (-1, 0)],
    "expiring_kb": lambda: [kba.expiring_kb([(c, 3600) for c in CLIS[:n]]) for n in (0, 1, 2, 9, 14)]
                           + [kba.expiring_kb([(_cli(1, "Очень-очень длинное имя профиля"), 60)] * 2)],
    "admin_devices": lambda: [kba.admin_devices(d, can_add=a) for d in (DEVS, MANY, []) for a in (True, False)],
    "admin_client_device_list": lambda: [kba.admin_client_device_list(MANY, 1)],
    "unassigned_devices": lambda: [kba.unassigned_devices(MANY)],
    "reassign_targets": lambda: [kba.reassign_targets(1, CLIS)],
    "reassign_addslot": lambda: [kba.reassign_addslot(1, 2)],
    "traffic_devices_kb": lambda: [kba.traffic_devices_kb()],
    "admin_clients": lambda: [kba.admin_clients(CLIS, {1, 2}), kba.admin_clients([])],
    "broadcast_targets": lambda: [kbb.broadcast_targets(CLIS[:n], set(range(1, k + 1)), extend=e)
                                  for n in (0, 2, 14) for k in (0, 2) for e in (True, False)],
    "broadcast_days_kb": lambda: [kbb.broadcast_days_kb()],
    "broadcast_confirm": lambda: [kbb.broadcast_confirm()],
    "broadcast_cancel": lambda: [kbb.broadcast_cancel()],
}


@pytest.mark.parametrize("name", sorted(ADMIN))
def test_stage2_admin_keyboards_follow_the_layout_rules(name):
    """Главная, профили, карточки, продление, лимиты, блокировка, списки и
    объявление администратора — по общим правилам: ≤10 рядов, короткие
    подписи в рядах, ✅/☑️ у тумблеров, «Отмена» первой, без «🏠»."""
    for i, markup in enumerate(ADMIN[name]()):
        bad = _violations(markup, name)
        rows = [[b.text for b in r] for r in markup.inline_keyboard]
        assert not bad, f"{name} [вариант {i}]: {sorted(bad)} — {rows}"


def test_admin_main_is_eight_buttons_with_gateways_always():
    """Главная админа — восемь кнопок; «🛰 Шлюзы» — всегда: это единственный
    вход и к развёртыванию РФ-доступа, и к его включению — спрячь её, и
    функцию на новом сервере не найти."""
    full = [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Мои устройства", "🇷🇺 РФ-доступ"], ["👥 Профили", "➕ Профиль"],
            ["🛰 Шлюзы", "⚙️ Настройки"], ["📢 Объявление", "🔄 Обновить"]]
    rows = [[b.text for b in r] for r in kba.admin_main(gateways=True, routing_visible=True,
                                                       self_client_id=1).inline_keyboard]
    assert rows == full, rows
    rows = [[b.text for b in r] for r in kba.admin_main().inline_keyboard]
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Мои устройства"], ["👥 Профили", "➕ Профиль"],
                    ["🛰 Шлюзы", "⚙️ Настройки"], ["📢 Объявление", "🔄 Обновить"]], rows
    rows = [[b.text for b in r] for r in kba.admin_main(gateways=False, routing_visible=True,
                                                       self_client_id=1).inline_keyboard]
    assert rows == full, "параметр gateways больше ничего не прячет"


@pytest.mark.parametrize("n", [1, 3, 5, 6, 8, 14])
def test_profile_card_fits_ten_rows_with_every_conditional_row(n):
    """Карточка профиля при любом числе устройств — не больше десяти рядов и
    с условными рядами (пауза, приглашение, РФ-доступ) тоже. Устройства
    списком, пока влезают по рядам; не влезли — одной кнопкой «📱 Устройства:
    N». Сворачивать раньше, чем кончились ряды, — лишний экран на пути к
    устройству."""
    for paused in (False, True):
        for pending in (False, True):
            for routing in (False, True):
                m = _card(n, routing_visible=routing, paused=paused, pending=pending)
                rows = [[b.text for b in r] for r in m.inline_keyboard]
                labels = [t for r in rows for t in r]
                assert len(rows) <= kbm.MAX_ROWS, rows
                listed = [l for l in labels if l.startswith("⚪ Устройство")]
                if listed:
                    assert len(listed) == n, rows
                else:
                    assert f"📱 Устройства: {n}" in labels, rows
                    assert len(rows) - 1 + n > kbm.MAX_ROWS, f"свёрнуто, хотя {n} устройств влезали: {rows}"
                assert ("▶️ Снять паузу" in labels) is paused and ("🔁 Новое приглашение" in labels) is pending
                assert rows[-1] == ["➕ Устройство", "⬅️ Назад"], rows


def test_profile_card_rows():
    """[⏱ Продлить] [✏️ Изменить] / [🇷🇺 РФ-доступ] [🛑 Блок] / устройства /
    [➕ Устройство] [⬅️ Назад]; условные ряды — сверху."""
    rows = [[b.text for b in r] for r in _card(1, routing_visible=True).inline_keyboard]
    assert rows == [["⏱ Продлить", "✏️ Изменить"], ["🇷🇺 РФ-доступ", "🛑 Блок"],
                    ["⚪ Устройство 1"], ["➕ Устройство", "⬅️ Назад"]], rows
    rows = [[b.text for b in r] for r in _card(1, paused=True, pending=True).inline_keyboard]
    assert rows[:3] == [["▶️ Снять паузу"], ["🔁 Новое приглашение"], ["⏱ Продлить", "✏️ Изменить"]], rows
    assert rows[3] == ["🛑 Блок"], "без РФ-доступа «Блок» один в ряду"
    # счёт по рядам: пять устройств при всех условных рядах — ровно десять рядов
    rows = [[b.text for b in r] for r in _card(5, routing_visible=True, paused=True, pending=True).inline_keyboard]
    assert len(rows) == 10 and ["📱 Устройства: 5"] not in rows, rows
    # шестое уже не влезает — сворачиваются все
    rows = [[b.text for b in r] for r in _card(6, routing_visible=True, paused=True, pending=True).inline_keyboard]
    assert ["📱 Устройства: 6"] in rows and not any(r[0].startswith("⚪ Устройство") for r in rows), rows


@pytest.mark.parametrize("n, shown", [(0, 0), (1, 1), (17, 17), (18, 18), (19, 18), (40, 18)])
def test_expiring_list_takes_up_to_nine_rows_of_two(n, shown):
    """«Истекают»: по два профиля в ряд и ряд «В меню» — до восемнадцати
    профилей в десяти рядах; дальше — не больше, чтобы экран не перерос
    правило."""
    rows = kba.expiring_kb([(_cli(i + 1, f"Профиль {i + 1}"), 3600) for i in range(n)]).inline_keyboard
    names = [b.text for r in rows for b in r if b.text.startswith("⏱ ")]
    assert len(names) == shown, (n, len(names))
    assert len(rows) <= kbm.MAX_ROWS, len(rows)
    assert all(len(r) <= 2 for r in rows), [[b.text for b in r] for r in rows]


def test_edit_submenu_rows():
    rows = [[b.text for b in r] for r in kba.client_edit_kb(1).inline_keyboard]
    assert rows == [["✏️ Имя", "✏️ Период"], ["✏️ Лимит устр-в", "✏️ Трафик"],
                    ["🗑 Удалить профиль"], ["⬅️ Назад"]], rows


# ── экраны администратора и агента: исключения до своих этапов ───────────────

_ARGS = {"key": "restart", "sec": "mon", "configured": True, "has_secret": True, "muted": False,
         "back_sec": "mon", "st": {"enabled": True, "raw_allow": ["a.example"]},
         "action": "restart", "back": "panel", "items": [("ru", "a.ru"), ("vpn", "b.com")],
         "idx": 0, "kind": "ru", "dom": "a.ru", "val": "1.2.3.4", "label": "1.2.3.4", "slot": 1,
         "on": True, "healthy": True, "device_id": 3, "has_candidates": True, "enabled": True,
         "lists_every": 6, "unassigned_count": 0, "client_id": 5,
         "info": {"probe_seconds": 30, "window": 10, "threshold": 60, "failover": True, "availability": 50},
         # билдеры списков и пресетов — образцы под их параметры
         "client": _cli(1), "clients": [_cli(1), _cli(2)], "devices": DEVS, "target": "dev", "ref": 1,
         "ctx": "extend", "values": [10, 50, 100], "cancel_cb": "m:main", "selected": set(),
         "seconds": 60, "n": 8, "day": 1, "hour": 12, "c": _cli(1), "online_ids": set()}

# (модуль, билдер) → нарушения, которые терпим, и почему: «этап N» — до
# своего этапа; «макет …» — так нарисовано в утверждённом макете экрана
ADMIN_EXCEPTIONS = {
    ("settings", "migration_prepare_confirm"): ({"подтверждение не с «Отмены»"},
                                                "макет «Порт или подсеть»: действие первым, отмена рядом со «Свой порт»"),
    ("routing", "routing_disable_confirm"): ({"кружок вместо ✅/☑️"}, "макет: «🔴 Выключить» — действие, не тумблер"),
    ("routing", "routing_params_kb"): ({"кружок вместо ✅/☑️"},
                                       "макет «Параметры»: «🔴 Выключить РФ-доступ» — действие, не тумблер"),
}

_GW_STATE = {"gateway": SimpleNamespace(id=1, lan_mode=1), "device": SimpleNamespace(id=7, name="NASPi"),
             "active": True, "preferred": True}
_GW_STANDBY = {"gateway": SimpleNamespace(id=2, lan_mode=0), "device": SimpleNamespace(id=8, name="Pi4"),
               "active": False, "preferred": False}
_EXTRA = {("gateway", "gateway_panel_kb"): lambda: kbg.gateway_panel_kb(lan=True),
          ("routing", "gateway_card"): lambda: kbr.gateway_card(_GW_STANDBY, back_to_list=True),
          ("routing", "gateways_kb"): lambda: kbr.gateways_kb([_GW_STATE, _GW_STANDBY], peer_nets_on=False),
          ("routing", "gateway_edit_kb"): lambda: kbr.gateway_edit_kb(_GW_STATE, two_slots=True),
          ("routing", "routing_params_kb"): lambda: kbr.routing_params_kb(
              {"probe_seconds": 30, "window": 10, "availability": 50}, 6),
          ("settings", "migration_confirm"): lambda: kbs.migration_confirm("finish"),
          ("routing", "gateway_pick"): lambda: kbr.gateway_pick(
              [SimpleNamespace(id=7, name="NASPi", address="10.8.1.5")])}


def _admin_builders():
    """(модуль, имя, сборщик) для всех публичных функций модулей; без образца
    под сигнатуру сборщик поднимает LookupError — сторож ниже такое не
    пропускает молча."""
    for mod in (kba, kbs, kbg, kbb, kbr):
        short = mod.__name__.rsplit(".", 1)[-1]
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn.__module__ != mod.__name__ or name.startswith("_") or name in CLIENT:
                continue
            if (short, name) in _EXTRA:
                yield short, name, _EXTRA[(short, name)]
                continue
            req = [p.name for p in inspect.signature(fn).parameters.values()
                   if p.default is inspect.Parameter.empty
                   and p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
            missing = [r for r in req if r not in _ARGS]
            if missing:
                def _no_sample(missing=missing):
                    raise LookupError(f"нет образца для параметров {missing}")
                yield short, name, _no_sample
                continue
            yield short, name, (lambda fn=fn, req=req: fn(**{r: _ARGS[r] for r in req}))


def test_admin_and_agent_keyboards_break_the_rules_only_where_listed():
    seen = {}
    unsampled = {}
    for short, name, make in _admin_builders():
        try:
            markup = make()
        except (KeyError, TypeError, ValueError, LookupError) as e:
            unsampled[(short, name)] = f"{type(e).__name__}: {e}"
            continue
        if isinstance(markup, InlineKeyboardMarkup):
            seen[(short, name)] = _violations(markup, name)
    assert not unsampled, f"билдеры без образца — сторож их не проверяет: {unsampled}"
    unexpected = {k: v for k, v in seen.items()
                  if v and v != ADMIN_EXCEPTIONS.get(k, (set(), ""))[0]}
    assert not unexpected, f"нарушения вне списка исключений: {unexpected}"
    fixed = [k for k in ADMIN_EXCEPTIONS if k in seen and not seen[k]]
    assert not fixed, f"исключения больше не нужны — убери из списка: {fixed}"
    unchecked = [k for k in ADMIN_EXCEPTIONS if k not in seen]
    assert not unchecked, f"исключения для билдеров, которые не собрались: {unchecked}"


def test_no_house_icon_in_keyboard_literals_outside_the_listed_screens():
    """«🏠» ищем и в исходнике: кнопка может появиться только в ветке, до
    которой образцы не дошли."""
    root = pathlib.Path(kbm.__file__).parent
    found = set()
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "🏠" in node.value:
                found.add((path.stem, node.value))
    assert found == set(), found


def test_main_client_screen_is_four_rows_at_most():
    """Главная клиента — ряд выдачи, устройства, РФ-доступ с подпиской,
    помощь; без устройств — без ряда выдачи."""
    rows = [[b.text for b in r] for r in kbc.client_main(has_devices=True, routing_visible=True,
                                                        client_id=1).inline_keyboard]
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Устройства", "➕ Устройство"],
                    ["🇷🇺 РФ-доступ", "💳 Подписка"], ["❓ Как подключить"]], rows
    rows = [[b.text for b in r] for r in kbc.client_main(has_devices=False).inline_keyboard]
    assert rows == [["➕ Устройство"], ["💳 Подписка", "❓ Как подключить"]], rows
    rows = [[b.text for b in r] for r in kbc.client_main(has_devices=True, can_add=False).inline_keyboard]
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Устройства"],
                    ["💳 Подписка", "❓ Как подключить"]], "лимит исчерпан — без «➕ Устройство»"
    callbacks = [b.callback_data for r in kbc.client_main(has_devices=True, routing_visible=True,
                                                          client_id=7).inline_keyboard for b in r]
    assert RoutingCB(action="panel", ref=7).pack() in callbacks
    assert DeviceCB(action="add").pack() in callbacks


def test_guest_main_rows():
    rows = [[b.text for b in r] for r in kbc.guest_main(routing_visible=True, client_id=1).inline_keyboard]
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Устройства", "🇷🇺 РФ-доступ"], ["❓ Как подключить"]]
    rows = [[b.text for b in r] for r in kbc.guest_main(has_devices=False).inline_keyboard]
    assert rows == [["❓ Как подключить"]], "без устройств — только помощь"


def test_card_rows_follow_the_device_kind():
    """Карточки: своё — ряд выдачи, имя/лимит, другу/блок, удалить/назад;
    переданное — имя/лимит, удалить/назад; от друга — выдача, блок/удалить,
    назад; добавленное не ботом — без ряда выдачи и без «Другу»."""
    def rows(m):
        return [[b.text for b in r] for r in m.inline_keyboard]
    assert rows(kbc.device_actions(DEVS[0], is_admin=False, back_target="m:devices")) == [
        ["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["✏️ Имя", "✏️ Лимит"], ["👤 Другу", "🛑 Блок"],
        ["🗑 Удалить", "⬅️ Назад"]]
    assert rows(kbc.device_actions(PENDING, is_admin=False, back_target="x"))[2] == ["🔁 Приглашение", "🛑 Блок"]
    assert rows(kbc.device_actions(BLOCKED, is_admin=False, back_target="x"))[2] == ["👤 Другу", "✅ Разблок"]
    assert rows(kbc.device_actions(UNMANAGED, is_admin=False, back_target="x")) == [
        ["✏️ Имя", "✏️ Лимит"], ["🛑 Блок"], ["🗑 Удалить", "⬅️ Назад"]]
    assert rows(kbc.lent_out_device_actions(DEVS[2], "x")) == [["✏️ Имя", "✏️ Лимит"], ["🗑 Удалить", "⬅️ Назад"]]
    assert rows(kbc.held_device_actions(HELD[0], "x")) == [
        ["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["🛑 Блок", "🗑 Удалить"], ["⬅️ Назад"]]
    gen = [b.callback_data for b in _buttons(kbc.held_device_actions(HELD[0], "x", cb_cls=FriendCB))][:3]
    assert gen == [FriendCB(action=a, device_id=4).pack() for a in ("gen_link", "gen_qr", "gen_file")]
    deleted = [b.callback_data for b in _buttons(kbc.held_device_actions(HELD[0], "x"))]
    assert DelDeviceCB(device_id=4, stage="ask").pack() in deleted
    assert BlockCB(target="dev", action="menu_block", ref=4).pack() in deleted
