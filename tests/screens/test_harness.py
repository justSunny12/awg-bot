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
    await bot.answer_callback_query("cq1", text="Сохранено", show_alert=True)
    await bot.answer_callback_query("cq2")
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


# ── шаги снимка ──────────────────────────────────────────────────────────────

def test_shorthand_fields_unfold_into_steps_in_the_usual_order():
    """press/start/text/call — сокращение: /start первым, нажатия, ввод,
    событие последним. Явные steps — как есть; вместе с сокращениями —
    ошибка каталога, а не молча выбранный порядок."""
    from tests.screens.base import Shot

    async def ev(services, bot):
        pass
    shot = Shot("cl.x", role="client", start="sub", press=["m:a", "m:b"], text="5", call=("уведомление", ev))
    assert shot.all_steps() == (("start", "sub"), ("press", "m:a"), ("press", "m:b"), ("text", "5"),
                                ("call", ev, "уведомление"))
    steps = (("press", "m:a"), ("text", "5"), ("press", "m:b"), ("start", ""))
    assert Shot("cl.y", role="client", steps=steps).all_steps() == steps
    with pytest.raises(AssertionError, match="steps и сокращения"):
        Shot("cl.z", role="client", steps=steps, press=["m:a"])


@pytest.mark.parametrize("step, what", [
    (("tap", "m:a"), "шаг не понят"),
    (("press",), "неверное число полей"),
    (("file", "a.enc", "не байты"), "байты"),
    (("call", "не функция"), "не функция"),
])
def test_malformed_steps_are_refused(step, what):
    """Опечатка в шаге каталога — внятная ошибка на месте, а не снимок
    «ничего не произошло» в эталоне."""
    with pytest.raises(AssertionError, match=what):
        harness.check_step(step)


async def test_text_with_markup_reaches_the_handler_as_html():
    """Ввод с разметкой (entities) — обработчик видит html_text, как от
    живого клиента Telegram; строка действия показывает ввод в HTML."""
    from aiogram import Dispatcher, Router
    from aiogram.types import Message

    router = Router()

    @router.message()
    async def echo(message: Message):
        await message.answer(message.html_text)
    dp = Dispatcher()
    dp.include_router(router)
    session = harness.StubSession(42, catalog.NOW)
    rec = await harness.run(dp, session, uid=42, name="U", steps=[
        ("text", "Привет всем", [{"type": "bold", "offset": 0, "length": 6}])])
    assert rec.action == "> text «<b>Привет</b> всем»", rec.action
    assert [(c.head, c.body) for c in rec.calls] == [("+ send #2", "<b>Привет</b> всем")]


# ── память заглушки: чаты, удалённые, повторные ответы ───────────────────────

async def test_calls_to_another_chat_are_labelled_and_numbered_in_that_chat():
    """Уведомление другому человеку — с адресатом в заголовке и своей
    нумерацией; живое меню чата снимка от него не сдвигается."""
    session = harness.StubSession(1000, catalog.NOW, {1: "админ"})
    bot = harness.make_bot(session)
    await bot.send_message(1, "x", reply_markup=_kb([("Скрыть", "hd")]))
    await bot.send_message(77, "y")
    await bot.edit_message_reply_markup(chat_id=1, message_id=1, reply_markup=None)
    await bot.send_message(1000, "z")
    assert [c.head for c in session.calls] == ["+ send #1 → чат админ", "+ send #1 → чат tg 77",
                                               "- markup off #1 → чат админ", "+ send #2"]
    assert session.nav == 1, "кнопки в чужом чате — не живое меню чата снимка"


async def test_edit_of_a_deleted_message_and_a_second_answer_are_recorded_not_raised():
    """Правка удалённого и второй ответ на тот же колбэк в Telegram молча
    проваливаются (человек не видит ни правки, ни второй всплывашки) — в
    записи это отдельные строки; снимок не падает, и в нарушения лимитов
    эти строки не идут (известные баги не красят каталог)."""
    session = harness.StubSession(42, catalog.NOW)
    bot = harness.make_bot(session)
    await bot.delete_message(42, 1)
    await bot.edit_message_text("новый", chat_id=42, message_id=1)
    await bot.edit_message_reply_markup(chat_id=42, message_id=1, reply_markup=None)
    await bot.answer_callback_query("cq1")
    await bot.answer_callback_query("cq1", text="Профиль не найден", show_alert=True)
    await bot.answer_callback_query("cq1")
    heads = [c.head for c in session.calls]
    assert heads == ["- delete #1", "! правка удалённого #1", "= edit #1", "! правка удалённого #1",
                     "- markup off #1", "~ (пустой ответ)", "~ (повторный ответ) Профиль не найден",
                     "~ (повторный ответ)"], heads
    rec = Record(action="> press x", calls=session.calls)
    assert harness.problems("adm.x", rec, {}) == []


def test_multiline_copy_text_stays_on_one_line():
    """Текст копирования с переводами строк печатается с «\\n» буквально:
    формат эталона построчный, иначе кнопка разъезжается на строки текста."""
    from aiogram.types import CopyTextButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="📋 Скопировать", copy_text=CopyTextButton(text="Привет!\nЖми ссылку\nhttps://t.me/test_bot"))]])
    assert harness.markup_lines(kb) == ["  [📋 Скопировать | copy:Привет!\\nЖми ссылку\\nhttps://t.me/test_bot]"]


# ── снимок целиком через настоящий диспетчер ─────────────────────────────────

@pytest.mark.parametrize("blob, answer", [
    (b"#!/bin/sh\n# awg-gw-bundle installer\n", "GW_FIRST_RUN_FILE"),
    (b"DUMMY", "GW_BUNDLE_NOT_OURS"),
])
async def test_file_step_hands_the_bytes_to_the_handler(blob, answer, tmp_path, frozen, fakes):
    """Шаг «файл»: документ с подписью уходит агенту, обработчик скачивает
    его (getFile и поток байтов) и решает по содержимому — разные байты,
    разный ответ. Иначе экраны «📦 Получена конфигурация» и восстановления
    из копии не снять."""
    from awgbot.bot import texts
    from tests.screens.base import Shot
    shot = Shot("gw.file", role="gateway", steps=[("file", "conf.bin", blob, "подпись")])
    rec = await harness.take(shot, tmp_path, fakes)
    assert rec.action == "> file conf.bin «подпись»", rec.action
    sent = [c for c in rec.calls if c.head.startswith("+ send")]
    assert sent and sent[-1].body == getattr(texts, answer), [(c.head, c.body) for c in rec.calls]


async def test_call_step_records_a_notification_without_a_human_action(tmp_path, frozen, fakes):
    """Шаг «событие»: функция зовётся с сервисами и ботом снимка, запись —
    что бот отправил, с адресатом; строка действия — имя события."""
    from awgbot.bot.notifier import notify_one
    from tests.screens.base import CLIENT_TG, Shot, owner

    async def ping(services, bot):
        assert services.bot_username == harness.BOT_USERNAME
        await notify_one(bot, CLIENT_TG, "🔔 <b>Тест</b>")
    rec = await harness.take(Shot("adm.ev", role="admin", data=lambda s: (owner(s), (1, "Админ"))[1],
                                  call=("notify_one", ping)), tmp_path, fakes)
    assert rec.action == "> call notify_one", rec.action
    assert [(c.head, c.body) for c in rec.calls] == [("+ send #1 → чат клиент", "🔔 <b>Тест</b>")]


@pytest.mark.parametrize("photo, heads", [
    (False, ["= edit #1"]),
    (True, ["- delete #1", "+ send #2"]),
])
async def test_photo_flag_puts_the_button_under_a_photo(photo, heads, tmp_path, frozen, fakes):
    """Под фото текстовый экран не правится, а пересоздаётся (гайд смотрит
    cb.message.photo): флаг photo снимает именно эту ветку."""
    from awgbot.bot.callbacks import GuideCB
    from tests.screens.base import Shot, owner
    shot = Shot("cl.g", role="client", press=[GuideCB(guide="connect", step=0)], data=owner, photo=photo)
    rec = await harness.take(shot, tmp_path, fakes)
    got = [c.head for c in rec.calls if not c.head.startswith("~")]
    assert got == heads, got


async def test_builder_gets_the_shot_monkeypatch_and_it_is_undone_after_the_shot(tmp_path, frozen, fakes):
    """Построитель с двумя аргументами получает monkeypatch снимка; подмена
    не доживает до следующего снимка. Старая сигнатура (services) и
    ключевые параметры после неё — по-прежнему с одним аргументом."""
    from awgbot.core import config
    from tests.screens.base import Shot
    before = config.SERVER_PORT
    seen = []

    def two(services, mp):
        mp.setattr(config, "SERVER_PORT", 1)
        seen.append(config.SERVER_PORT)
        return 1, "Админ"

    def one(services, *, status=True):
        seen.append(config.SERVER_PORT)
        return 1, "Админ"
    for i, data in enumerate((two, one)):
        await harness.take(Shot(f"adm.b{i}", role="admin", start="", data=data), tmp_path, fakes)
    assert seen == [1, before], seen
    assert config.SERVER_PORT == before


async def test_fakes_and_module_state_start_clean_on_every_shot(tmp_path, frozen, fakes, fake_awg):
    """Счётчик ключей фейкового awg, страницы листания, метки объявлений —
    в исходном виде на каждом снимке: иначе vpn:// и экраны зависели бы от
    места снимка в каталоге."""
    from awgbot.bot import paging
    from awgbot.bot.handlers.admin import broadcast
    from tests.screens.base import Shot
    seen = []

    def look(services):
        seen.append((fake_awg._n, dict(paging._pages), dict(broadcast._last_broadcast_at)))
        fake_awg._n += 5
        paging.remember(1, "devices", 0, 3)
        broadcast._last_broadcast_at[("x",)] = 1.0
        return 1, "Админ"
    for i in range(2):
        await harness.take(Shot(f"adm.r{i}", role="admin", start="", data=look), tmp_path, fakes)
    assert seen == [(0, {}, {}), (0, {}, {})], seen


# ── сторож полноты: запись вызовов построителей ──────────────────────────────

async def test_builder_calls_are_seen_in_worker_threads_and_only_inside_the_block():
    """Обработчики зовут построители и через asyncio.to_thread: вызов в
    потоке засчитывается; вызов до блока и после — нет; повторный вызов не
    ломает запись."""
    import asyncio
    from awgbot.bot.keyboards import admin as kba, common as kbm
    builders = {"common.hide_only": kbm.hide_only, "admin.admin_main": kba.admin_main,
                "common.to_menu": kbm.to_menu}
    kbm.to_menu()
    with harness.BuilderCalls(builders) as calls:
        await asyncio.to_thread(kbm.hide_only)
        kbm.hide_only()
    kba.admin_main()
    assert calls.seen == {"common.hide_only"}, calls.seen


def test_builder_list_counts_each_builder_once_and_skips_helpers():
    """Реэкспорт пакета keyboards не удваивает построитель; помощники (метки,
    теги, срез страницы) и кнопки-одиночки в список не входят."""
    b = harness.keyboard_builders()
    assert "settings.settings_firewall" in b and "gateway.gateway_panel_kb" in b
    assert not any(k.startswith("__init__") for k in b)
    for helper in ("common.entry_tag", "common.page_slice", "common.select_all_button", "common.reply_hide",
                   "gateway.link_minutes", "settings._cycle"):
        assert helper not in b, helper
