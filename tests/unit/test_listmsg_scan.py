"""Разборы lists/msgs/scan: ключи снимков в отправленном HTML, снятие ссылки
умершего снимка подписью, клавиатура записи — в JSON и обратно, ряд «Скрыть»
прочь.

Цена ошибки: ключ, не узнанный в ссылке с меткой «-n», — снимок уведомления
не привязан, ссылка умрёт с отметкой «больше не хранится», хотя её можно было
снять; чужая ссылка (`cl-4-n`, `list-gwnets-2`), принятая за ключ, — правка
тактом трогает ссылки на профили; `strip`, снявший обе ссылки вместо одной, —
в отчёте рассылки пропадает живой список недоставленных; подпись с `&lt;`,
распакованная в `<`, — Telegram отвергнет правку как битую разметку, и
ссылка останется мёртвой; `without_hide`, снявший ряд «⬅️ В меню» или
«⏱ Продлить», — у человека пропадает действие, которое через сутки ещё
осмысленно; одиночная «Скрыть», оставленная пустой клавиатурой, — правка
получит отказ Telegram.
"""
from __future__ import annotations

import html as _html
import json
import re

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

from awgbot.bot import keyboards as kb
from awgbot.bot import links
from awgbot.bot.lists.msgs import scan

pytestmark = pytest.mark.unit

K1, K2 = "abc123def4", "zz9988yy77"


def _a(payload: str, label: str) -> str:
    return f'<a href="https://t.me/test_bot?start={payload}">{label}</a>'


def _visible(text: str) -> str:
    """Видимый текст, как его покажет Telegram: теги прочь, сущности распакованы."""
    return _html.unescape(re.sub(r"<[^>]+>", "", text))


def _kb_json(*rows: list[tuple[str, str]]) -> str:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in row]
                                                 for row in rows]).model_dump_json(exclude_none=True)


def _rows(kb_json: str) -> list[list[tuple[str, str]]]:
    return [[(b["text"], b.get("callback_data")) for b in row] for row in json.loads(kb_json)["inline_keyboard"]]


# отчёт рассылки: ссылки на профили, два хвоста-снимка (с меткой следа «-n»)
REPORT = links.mark_new(
    "✅ Разослано: " + _a("cl-4", "Боря") + ", " + _a("cl-3", "Ксюша") + " " + _a(f"list-snap-{K1}", "и ещё 20")
    + " — доставлено: 2 адресата\n⚠️ Не доставлено: " + _a("cl-6", "Подписчик &lt;00&gt;") + " "
    + _a(f"list-snap-{K2}", "и ещё 27") + " — бот заблокирован")


# ── keys ─────────────────────────────────────────────────────────────────────

def test_keys_finds_both_snapshot_links_with_the_new_mark_in_order():
    assert "-n" in REPORT and f"list-snap-{K1}-n" in REPORT, "образец без метки следа — тест не о том"
    assert scan.keys(REPORT) == [K1, K2], "ключи снимков отчёта не узнаны (метка «-n» сбила разбор?)"


def test_profile_and_gateway_list_links_are_not_snapshot_keys():
    """`cl-4-n` и `list-gwnets-2` — ссылки на экраны, не на снимки: их такт не трогает."""
    text = _a("cl-4-n", "Боря") + " " + _a("list-gwnets-2", "подсети") + " " + _a("list-snap-short", "x")
    assert scan.keys(text) == [], f"чужая ссылка принята за ключ снимка: {scan.keys(text)}"


def test_keys_of_empty_text_and_repeated_link():
    assert scan.keys("") == [] and scan.keys(None) == []
    twice = _a(f"list-snap-{K1}", "и ещё 3") + " " + _a(f"list-snap-{K1}-n", "и ещё 3")
    assert scan.keys(twice) == [K1], "один снимок дважды — один ключ"


# ── strip ────────────────────────────────────────────────────────────────────

def test_strip_of_one_of_two_links_keeps_the_other_alive_and_the_visible_text_the_same():
    out = scan.strip(REPORT, [K1])
    assert scan.keys(out) == [K2], f"снята не та ссылка или обе: {scan.keys(out)}"
    assert "и ещё 20 — доставлено" in out, "подпись умершей ссылки не осталась текстом"
    assert _visible(out) == _visible(REPORT), "видимый текст сообщения изменился от снятия ссылки"
    assert len(out) < len(REPORT), "HTML не стал короче — ссылка не снята"
    assert out.count('href="https://t.me/test_bot?start=cl-') == 3, "ссылки на профили задеты"


def test_strip_keeps_an_escaped_label_escaped():
    """Подпись `&lt;…&gt;` остаётся сущностью: распакованная — битая разметка, Telegram правку отвергнет."""
    text = _a(f"list-snap-{K1}-n", "и ещё &lt;3&gt;")
    out = scan.strip(text, {K1})
    assert out == "и ещё &lt;3&gt;", out


def test_strip_twice_changes_nothing_more_and_nothing_dead_changes_nothing():
    once = scan.strip(REPORT, [K1, K2])
    assert scan.keys(once) == []
    assert scan.strip(once, [K1, K2]) == once, "повтор снятия изменил текст"
    assert scan.strip(REPORT, []) == REPORT and scan.strip(REPORT, ["nosuchkey0"]) == REPORT
    assert scan.strip("", [K1]) == "" and scan.strip(None, [K1]) == ""


# ── клавиатура ───────────────────────────────────────────────────────────────

HIDE = kb.hide_only().model_dump_json(exclude_none=True)


def test_without_hide_of_a_lone_hide_is_no_keyboard_at_all():
    assert scan.without_hide(HIDE) == "", "одиночная «Скрыть» — после снятия кнопок нет"
    assert scan.markup_from(scan.without_hide(HIDE)) is None, "пустая клавиатура ушла бы в правку объектом"


def test_without_hide_keeps_the_action_row_above():
    extend = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]])).model_dump_json(exclude_none=True)
    assert _rows(scan.without_hide(extend)) == [[("⏱ Продлить", "ext:take:5")]], "кнопка действия снята вместе со «Скрыть»"


def test_without_hide_keeps_the_menu_row_and_a_neighbour_in_the_same_row():
    menu = _kb_json([("⬅️ В меню", "upd:menu")], [("Скрыть", "hd")])
    assert _rows(scan.without_hide(menu)) == [[("⬅️ В меню", "upd:menu")]]
    mixed = _kb_json([("🔁 Повторить", "upd:retry"), ("Скрыть", "hd")])
    assert _rows(scan.without_hide(mixed)) == [[("🔁 Повторить", "upd:retry")]], "соседняя кнопка ряда пропала"
    no_hide = _kb_json([("👥 Пользователи", "m:users")])
    assert _rows(scan.without_hide(no_hide)) == [[("👥 Пользователи", "m:users")]]


def test_without_hide_of_empty_or_garbage_is_empty():
    assert scan.without_hide("") == "" and scan.without_hide(None) == "" and scan.without_hide("{нечитаемо") == ""


def test_kb_json_round_trips_and_ignores_non_inline_keyboards():
    markup = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]]))
    back = scan.markup_from(scan.kb_json(markup))
    assert back == markup, "клавиатура записи не та, что ушла: правка вернула бы другие кнопки"
    reply = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="меню")]])
    assert scan.kb_json(reply) == "" and scan.kb_json(None) == "", "reply-клавиатура записана как инлайн"
    assert scan.kb_json(InlineKeyboardMarkup(inline_keyboard=[])) == ""
    assert scan.markup_from("") is None and scan.markup_from("[]") is None and scan.markup_from("{x") is None


def test_has_hide_and_has_action():
    assert scan.has_hide(HIDE) and not scan.has_action(HIDE)
    menu = _kb_json([("⬅️ В меню", "upd:menu")])
    assert scan.has_action(menu) and not scan.has_hide(menu)
    assert not scan.has_hide("") and not scan.has_action("")
