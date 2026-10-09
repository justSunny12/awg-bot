"""Unit: раскладка клавиатур — правила, общие для всех экранов.

- не больше десяти рядов кнопок на экране;
- в рядах по две (и по три) подписи не длиннее 18 знаков — длиннее
  Telegram режет их многоточием на телефоне;
- тумблеры — только ✅/☑️; кружки 🟢/🔴 — состоянию объектов (устройство
  онлайн), не настройкам;
- в подтверждениях первая кнопка — «⬅️ Отмена», она не красная;
- «🏠» нигде: «В меню» — «⬅️ В меню», локальная сеть — не «дом».

Правила живут в tests/screens/harness.problems и прогоняются по клавиатурам
всех снимков эталонов (tests/screens); здесь — точечные раскладки
(главная, карточки, списки) на образцовых данных и статика: клавиатуры
собираются только через ui.rows, хвосты «⬅️ Назад» / «⬅️ В меню» — только
из ui.back / ui.to_menu.
"""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from awgbot.bot import ui
from awgbot.bot.callbacks import (BlockCB, DelDeviceCB, DeviceCB, FriendCB, RoutingCB)
from awgbot.bot.keyboards import admin as kba
from awgbot.bot.keyboards import client as kbc
from awgbot.bot.keyboards import common as kbm


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


def _buttons(m):
    return [b for row in m.inline_keyboard for b in row]


def test_toggle_marks_are_ticks_for_both_roles():
    """Тумблеры обеих ролей берут значок из одного места: ✅ / ☑️."""
    assert (kbm._chk(True), kbm._chk(False)) == ("✅", "☑️")
    assert (kbm._tick(True), kbm._tick(False)) == ("✅", "☑️")


# ── статика: клавиатуры только из ui.rows ────────────────────────────────────

_BOT = pathlib.Path(kbm.__file__).parent.parent          # awgbot/bot


def _sources(skip_ui: bool = True):
    for path in sorted(_BOT.rglob("*.py")):
        if skip_ui and path.name == "ui.py":
            continue
        yield path, path.read_text(encoding="utf-8")


def test_keyboards_are_built_only_through_ui_rows():
    """InlineKeyboardBuilder в коде бота не используется: раскладка — рядами
    как есть (ui.rows), иначе правила раскладки проверялись бы в двух местах.
    Смотрим имена в коде, не текст: упоминание в докстринге — не использование."""
    found = set()
    for path, src in _sources(skip_ui=False):
        for node in ast.walk(ast.parse(src)):
            names = ([a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom))
                     else [node.id] if isinstance(node, ast.Name)
                     else [node.attr] if isinstance(node, ast.Attribute) else [])
            if any(n.rsplit(".", 1)[-1] == "InlineKeyboardBuilder" for n in names):
                found.add(str(path.relative_to(_BOT)))
    assert found == set(), sorted(found)


def test_tail_labels_come_only_from_ui():
    """«⬅️ Назад» и «⬅️ В меню» — только ui.back / ui.to_menu: литерал в
    модуле клавиатур — вторая копия подписи, которую правка в ui не тронет."""
    found = set()
    for path, src in _sources():
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.replace("\ufe0f", "") in (ui.BACK_LABEL.replace("\ufe0f", ""),
                                                        ui.MENU_LABEL.replace("\ufe0f", "")):
                    found.add((str(path.relative_to(_BOT)), node.value))
    assert found == set(), found


def test_no_house_icon_in_keyboard_literals():
    """«🏠» ищем и в исходнике: кнопка может появиться только в ветке, до
    которой эталоны не дошли."""
    found = set()
    for path, src in _sources():
        if "keyboards" not in path.parts and "sections" not in path.parts:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "🏠" in node.value:
                found.add((path.stem, node.value))
    assert found == set(), found


# ── главная и карточки клиента и гостя ───────────────────────────────────────

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


# ── главная, карточка профиля и списки администратора ───────────────────────

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


# ── ui.rows: правила сборки ──────────────────────────────────────────────────

def test_rows_drops_none_and_empty_rows_and_keeps_order():
    m = ui.rows([("a", "m:a"), None, ("b", "m:b")], None, [None], ("c", "m:c"),
                [("d", "m:d", "danger")], ui.btn("u", url="https://example.com"))
    assert [[b.text for b in r] for r in m.inline_keyboard] == [["a", "b"], ["c"], ["d"], ["u"]]
    assert m.inline_keyboard[2][0].style == "danger" and m.inline_keyboard[0][0].style is None
    assert m.inline_keyboard[3][0].url == "https://example.com"
    assert ui.grid([1, None, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert ui.back("m:x") == ("⬅️ Назад", "m:x") and ui.to_menu("m:main") == ("⬅️ В меню", "m:main")
