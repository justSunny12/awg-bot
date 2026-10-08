"""Обвязка эталонов сама по себе: заглушка сессии совместима с aiogram,
проверки лимитов и HTML ловят то, что должны, сериализация держит формат,
общее правило упаковки колбэков агента укладывается в 64 байта.

Цена ошибки: проверка, которая молчит на битой разметке, ставит печать
«проверено» на экране, который Bot API отвергнет («can't parse entities») —
человек нажал кнопку и не получил ничего. Упаковка длиннее 64 байт — кнопка
не создаётся вовсе, весь экран падает на отправке.
"""
from __future__ import annotations

import pytest
from aiogram.methods import GetChat
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from awgbot.bot.callbacks import GwCB, SetCB
from awgbot.bot.texts.fmt import details
from tests.screens import catalog, harness
from tests.screens.harness import Call, Record

pytestmark = pytest.mark.screens


def _kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def _rec(*calls):
    return Record(action="> press x", calls=list(calls))


# ── HTML ─────────────────────────────────────────────────────────────────────

def test_project_markup_is_accepted():
    """Разметка, которой пишут экраны проекта, — без замечаний: иначе каждый
    снимок красный, и проверку отключат."""
    text = ("⚙️ <b>Настройки</b> · v1.2.3\n<i>курсив</i> <code>10.8.1.2</code> &lt;5 мин &amp; ещё\n"
            '<a href="https://t.me/test_bot?start=sub">💳 Подписка</a>\n'
            + details("Подробнее <b>жирно</b>") + "\n<tg-spoiler>DUMMY</tg-spoiler> <s>x</s> <u>y</u>"
            + '\n<pre>a</pre> <code class="language-sh">ls</code> &#8212; &#x2014;')
    assert harness.html_problems(text) == []


@pytest.mark.parametrize("text, what", [
    ("Порог <5 мин", "неэкранированный «<»"),
    ("RAM <b>80%</b> & диск", "неэкранированный «&»"),
    ("<div>x</div>", "тег <div>"),
    ("<b>жирно", "незакрытые"),
    ("<b><i>x</b></i>", "без пары"),
    ("x</b>", "без пары"),
    ('<a href="https://example.com" onclick="y">x</a>', "атрибут onclick"),
    ("<blockquote expandable>x</blockquote foo>", "атрибуты у закрывающего"),
])
def test_broken_markup_is_caught(text, what):
    """Заведомо битая разметка — замечание с указанием, что именно не так."""
    found = harness.html_problems(text)
    assert any(what in p for p in found), f"{text!r}: {found}"


def test_unescaped_text_on_a_screen_fails_the_snapshot_checks():
    """Имя профиля «<script>» вставлено в экран без экранирования — снимок
    обязан покраснеть, а не уйти в эталон."""
    rec = _rec(Call("= edit #1", "👤 <b>Профиль</b> Вася <3"))
    found = harness.problems("adm.x", rec, {})
    assert found and "неэкранированный «<»" in found[0], found


# ── лимиты Telegram и правила раскладки ──────────────────────────────────────

def test_limits_are_checked_on_every_call():
    """Каждый лимит § «Ограничения Telegram» ловится на своём вызове."""
    long_cb = "set:" + "я" * 31                                   # 35 знаков, но 66 байт
    rec = _rec(
        Call("+ send #2", "x" * 4097),
        Call("+ document #3", "y" * 1025, caption=True),
        Call("~ " + "z" * 201, toast="z" * 201),
        Call("~ <b>ok</b>", toast="<b>ok</b>"),
        Call("= edit #1", "ok", markup=_kb([("a", long_cb)])),
        Call("= edit #1", "ok", markup=_kb(*[[(str(i), f"m:{i}")] for i in range(11)])),
        Call("= edit #1", "ok", markup=_kb([("🔔 Уведомления по событиям", "m:a"), ("⬅️ Назад", "m:b")])),
        Call("= edit #1", '<a href="https://t.me/test_bot?start=dev.7">x</a>'),
    )
    found = "\n".join(harness.problems("adm.x", rec, {}))
    for what in ("длина 4097 вне 1…4096", "длина 1025 вне 1…1024", "всплывашка 201 > 200",
                 "разметка во всплывашке", "66 байт вне 1…64", "рядов 11 > 10",
                 "подпись «🔔 Уведомления по событиям»", "payload /start 'dev.7'"):
        assert what in found, f"не поймано: {what}\n{found}"


def test_text_length_is_counted_after_markup_is_parsed():
    """4096 — после разбора разметки: теги и сущности знаков не занимают.
    Иначе длинный, но законный экран с разметкой считался бы нарушением."""
    body = "<b>" + "x" * 4090 + "</b>&amp;&lt;"                     # 4092 видимых знака
    assert harness.problems("adm.x", _rec(Call("= edit #1", body)), {}) == []


def test_label_exception_allows_only_the_named_label_on_the_named_shot():
    """Исключение макета — точечное: та же подпись на другом снимке и другая
    длинная подпись на том же снимке остаются нарушениями."""
    rec = _rec(Call("= edit #1", "ok", markup=_kb([("☑️ Аварии на e-mail", "m:a"), ("⬅️ Назад", "m:b")]),
                    ), Call("= edit #1", "ok", markup=_kb([("☑️ Другая длинная кнопка", "m:a"),
                                                           ("⬅️ Назад", "m:b")])))
    exc = {"gw.set.notify": {"☑️ Аварии на e-mail"}}
    found = harness.problems("gw.set.notify", rec, exc)
    assert len(found) == 1 and "Другая длинная" in found[0], found
    assert len(harness.problems("gw.set.mon", rec, exc)) == 2, "исключение протекло на чужой снимок"
    assert harness.long_labels(rec) == {"☑️ Аварии на e-mail", "☑️ Другая длинная кнопка"}


# ── сериализация ─────────────────────────────────────────────────────────────

def test_record_serializes_in_the_reference_format():
    """Формат эталона: заголовок, действие, вызовы в порядке, текст как ушёл,
    ряды кнопок с упаковкой или ссылкой, «красная» у разрушительной кнопки."""
    red = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Отмена", callback_data="m:main"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data="d:del:7", style="danger")],
        [InlineKeyboardButton(text="📖 Инструкция", url="https://example.com/x")]])
    rec = Record(action="> press d:ask:7", calls=[
        Call("- markup off #1"),
        Call("+ send #2", "Удалить <b>iPhone</b>?\nНазад не вернуть", markup=red),
        Call("~! Готово", toast="Готово"),
        Call("+ document #3", "conf", caption=True),
        Call("- delete #2"),
    ])
    assert harness.serialize("cl.dev.del", "подтверждение", rec) == (
        "### cl.dev.del — подтверждение\n"
        "> press d:ask:7\n"
        "- markup off #1\n"
        "+ send #2\n"
        "Удалить <b>iPhone</b>?\n"
        "Назад не вернуть\n"
        "  [⬅️ Отмена | m:main] [🗑 Удалить | d:del:7 | красная]\n"
        "  [📖 Инструкция | https://example.com/x]\n"
        "~! Готово\n"
        "+ document #3\n"
        "conf\n"
        "- delete #2\n")


# ── заглушка сессии против настоящего aiogram ────────────────────────────────

async def test_stub_session_answers_aiogram_methods_and_records_them():
    """Через настоящий Bot: ответы проходят разбор aiogram (Message с
    растущим номером, привязанный к боту), каждый вызов записан своим видом."""
    session = harness.StubSession(42, catalog.NOW)
    bot = harness.make_bot(session)
    me = await bot.get_me()
    assert me.username == harness.BOT_USERNAME
    m2 = await bot.send_message(42, "<b>a</b>", reply_markup=_kb([("x", "m:x")]))
    m3 = await bot.send_message(42, "b")
    assert (m2.message_id, m3.message_id) == (2, 3), "номера новых сообщений — с #2"
    await m2.edit_text("c")                         # объект ответа привязан к боту
    await bot.edit_message_reply_markup(chat_id=42, message_id=2, reply_markup=None)
    await bot.delete_message(42, 3)
    await bot.answer_callback_query("cq", text="Сохранено", show_alert=True)
    await bot.answer_callback_query("cq")
    assert [c.head for c in session.calls] == [
        "+ send #2", "+ send #3", "= edit #2", "- markup off #2", "- delete #3",
        "~! Сохранено", "~ (пустой ответ)"]
    assert session.calls[0].body == "<b>a</b>", "текст пишется HTML, как ушёл"
    assert session.nav == 2, "живое меню — последнее сообщение с кнопками"


async def test_stub_session_refuses_an_unknown_method_loudly():
    """Метод, которого обвязка не знает, — запись «? Имя» и ошибка: иначе
    новый вызов в коде получил бы молчаливый True, и эталон соврал бы."""
    session = harness.StubSession(42, catalog.NOW)
    bot = harness.make_bot(session)
    with pytest.raises(AssertionError, match="GetChat"):
        await bot(GetChat(chat_id=42))
    assert [c.head for c in session.calls] == ["? GetChat"]


# ── общее правило упаковки колбэков агента ───────────────────────────────────

_GW_SECTIONS = ("root", "notify", "email", "ssh", "mon", "backup", "svc", "upd")
_ACTS = ("open", "toggle", "edit", "cycle", "pick", "do")
_LONGEST_KEY = "app.monitoring.service_failure_alert_minutes"


def test_gateway_general_packing_rule_survives_aiogram():
    """GwCB(action="<раздел>/<действие>", val=<ключ>): «/» в значении aiogram
    принимает, точки в ключе тоже, обратный разбор даёт то же самое; образец
    из описания — 56 байт."""
    cb = GwCB(action="notify/toggle", val="resource_alerts.thresholds_percent.disk")
    packed = cb.pack()
    assert packed == "gw:notify/toggle:resource_alerts.thresholds_percent.disk"
    assert len(packed.encode()) == 56, len(packed.encode())
    assert GwCB.unpack(packed) == cb


def test_longest_general_packing_fits_64_bytes_for_both_roles():
    """Худший случай: самое длинное имя раздела и действия с самым длинным
    ключом мониторинга — в 64 байта у агента (GwCB) и у основного (SetCB).
    Не уложилось — кнопка не создаётся, и нужен запасной вариант с короткими
    кодами действий."""
    worst = max((GwCB(action=f"{s}/{a}", val=_LONGEST_KEY).pack() for s in _GW_SECTIONS for a in _ACTS),
                key=lambda p: len(p.encode()))
    assert len(worst.encode()) <= 64, (worst, len(worst.encode()))
    assert len(worst.encode()) == 61, "худший случай агента по описанию — 61 байт"
    assert GwCB.unpack(worst).val == _LONGEST_KEY
    main = SetCB(sec="mon", act="toggle", key=_LONGEST_KEY).pack()
    assert len(main.encode()) <= 64 and SetCB.unpack(main).key == _LONGEST_KEY, main
