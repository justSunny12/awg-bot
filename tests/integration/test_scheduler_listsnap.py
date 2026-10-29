"""Такт снимков списков после полуночи и догон на старте (runtime/scheduler/listsnap,
lists/msgs/heal.nightly) у планировщиков основного бота, агента шлюза и бота
основы на резерве: задание и его расписание; что такт сделал со снимками, с
сообщениями и сколько запросов ушло в Telegram.

Цена ошибки: задания нет у одного из ботов — ссылки его сообщений умирают
ссылками, «Скрыть» висит дольше 48 часов и перестаёт удалять; такт без
джиттера — запрос в Telegram ровно в 00:01 каждую ночь, маячок; не
обязательный — проспанный такт выброшен, и правка ждёт сутки; без догона на
старте — бот, лежавший в полночь, сутки показывает мёртвые ссылки; правка не
истёкшего снимка — живая ссылка становится текстом; запрос, когда делать
нечего, — лишний трафик каждую ночь; «Скрыть», снятая с моложе суток, —
уведомление нельзя убрать из чата; снятые вместе с ней «⏱ Продлить» или
«⬅️ В меню» — пропало действие; две правки одного сообщения вместо одной —
вторая вернула бы снятую кнопку или ссылку; темп выше одной правки в секунду
на чат или 20 в секунду на бота — флуд-контроль Telegram; ожидание 429
дольше минуты или повторы после второго 429 — такт застревает в одном чате.
"""
from __future__ import annotations

import datetime as _dt

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from awgbot.bot import keyboards as kb
from awgbot.bot.lists.msgs import heal
from awgbot.core import config
from awgbot.runtime.scheduler import listsnap as sl
from tests.conftest import restore_settings
from tests.integration._listmsg import CHAT, OTHER, HOUR, Rig, rows

pytestmark = pytest.mark.integration

EXTEND = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]]))
MENU = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👥 Пользователи", callback_data="m:users")]])
EXP_T0 = _dt.datetime(2026, 11, 27, 0, 0, 0, tzinfo=config.TZ)      # срок снимка, созданного в T0
MIN = 60


@pytest.fixture()
def rig(services, monkeypatch):
    return Rig(services, monkeypatch)


def _field(trigger: CronTrigger, name: str) -> str:
    return str(next(f for f in trigger.fields if f.name == name))


# ── задание у трёх планировщиков ─────────────────────────────────────────────

def _main(services, bot, db):
    from awgbot.runtime.scheduler import setup_scheduler
    return setup_scheduler(services, bot, db, watcher=None)


def _guest(services, bot, db):
    from awgbot.runtime.scheduler import setup_guest_scheduler
    return setup_guest_scheduler(services, bot, db)


def _gateway(services, bot, db, monkeypatch, tmp_path):
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from awgbot.runtime.scheduler import setup_gateway_scheduler
    monkeypatch.setattr(config, "ROLE", "gateway")
    gdb = Database(str(tmp_path / "gw.db"))
    gdb.init_schema()
    return setup_gateway_scheduler(GatewayServices(gdb), bot), gdb


@pytest.mark.parametrize("who", ["main", "gateway", "guest"])
async def test_every_bot_has_the_nightly_tick_after_midnight_with_jitter_and_a_startup_catchup(
        who, rig, services, db, monkeypatch, tmp_path):
    gdb = None
    if who == "main":
        sched = _main(services, rig.bot, db)
    elif who == "guest":
        sched = _guest(services, rig.bot, db)
    else:
        sched, gdb = _gateway(services, rig.bot, db, monkeypatch, tmp_path)
    try:
        night, start = sched.get_job("listsnap_nightly"), sched.get_job("listsnap_startup")
        assert night is not None, f"{who}: такта после полуночи нет — ссылки умирают ссылками, «Скрыть» висит"
        assert start is not None, f"{who}: догона на старте нет — проспанная полночь ждёт сутки"
        t = night.trigger
        assert isinstance(t, CronTrigger) and (_field(t, "hour"), _field(t, "minute")) == ("0", "1"), t
        assert t.jitter == sl.LISTSNAP_JITTER == 300, f"{who}: джиттер такта {t.jitter} — маячок в 00:01"
        assert str(t.timezone) == str(config.TZ), f"{who}: такт не по поясу показа: {t.timezone}"
        for job in (night, start):
            assert job.misfire_grace_time is None and job.coalesce is True, \
                f"{who}: {job.id} не обязательное — опоздавший такт выброшен"
        assert isinstance(start.trigger, DateTrigger)
    finally:
        if sched.running:
            sched.shutdown(wait=False)
        restore_settings()
        if gdb is not None:
            gdb.close()


def test_the_startup_catchup_comes_30_to_120_seconds_after_start_and_varies(rig):
    seen = set()
    for _ in range(25):
        sched = AsyncIOScheduler(timezone=config.TZ)
        sl.add_listsnap_nightly(sched, rig.services, rig.bot)
        at = sched.get_job("listsnap_startup").trigger.run_date
        d = (at - rig.clock.t).total_seconds()
        assert 30 <= d <= 120, f"догон через {d} с — вне 30–120"
        seen.add(round(d, 3))
    assert len(seen) > 1, "догон всегда в одну и ту же секунду после старта — маячок"


async def test_the_scheduled_job_runs_the_tick_with_its_bot_and_services(rig):
    """Задание планировщика — это такт: истёкший снимок после его вызова — отметка, ссылка — текст."""
    key = rig.snap()
    m = await rig.bot.send_message(CHAT, "Итог " + rig.link(key, new=True))
    sched = AsyncIOScheduler(timezone=config.TZ)
    sl.add_listsnap_nightly(sched, rig.services, rig.bot)
    rig.clock.t = EXP_T0 + _dt.timedelta(minutes=3)
    await sched.get_job("listsnap_startup").func()
    assert rig.raw(key).get("gone") == 1 and "list-snap-" not in rig.text_of(m.message_id), \
        "догон на старте после проспанной полуночи ничего не поправил"


async def test_a_failing_tick_is_a_log_line_not_a_crash(rig, monkeypatch, caplog):
    import logging

    async def boom(bot, services):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(heal, "nightly", boom)
    sched = AsyncIOScheduler(timezone=config.TZ)
    sl.add_listsnap_nightly(sched, rig.services, rig.bot)
    with caplog.at_level(logging.WARNING):
        await sched.get_job("listsnap_nightly").func()
    assert "lists: такт не прошёл" in caplog.text


# ── такт: снимки ─────────────────────────────────────────────────────────────

async def test_expired_snapshots_become_marks_and_their_messages_are_edited_others_untouched(rig):
    old = rig.snap()
    a = await rig.bot.send_message(CHAT, "Итог " + rig.link(old, "и ещё 8", new=True))
    rig.clock.advance(24 * HOUR)
    young = rig.snap()
    b = await rig.bot.send_message(CHAT, "Отчёт " + rig.link(young, "и ещё 8", new=True))
    rig.clock.t = EXP_T0 + _dt.timedelta(minutes=2)
    out = await heal.nightly(rig.bot, rig.services)
    assert rig.raw(old).get("gone") == 1, "истёкший снимок не стал отметкой"
    assert rig.text_of(a.message_id) == "Итог и ещё 8", f"ссылка истёкшего не стала текстом: {rig.text_of(a.message_id)}"
    assert rig.edit_calls(b.message_id) == [], "сообщение с неистёкшим снимком поправлено"
    assert not rig.raw(young).get("gone") and rig.rec(b.message_id)["keys"] == [young]
    assert rig.rec(a.message_id) is None, "запись без причин осталась"
    assert out["expired"] == 1 and out["links"] == 1, out


async def test_nothing_to_do_means_not_a_single_telegram_request(rig):
    key = rig.snap()
    await rig.bot.send_message(CHAT, "Итог " + rig.link(key, new=True), reply_markup=kb.hide_only())
    before = len(rig.session.calls)
    rig.clock.advance(23 * HOUR)                       # «Скрыть» ещё рано, снимок жив
    out = await heal.nightly(rig.bot, rig.services)
    assert rig.session.calls[before:] == [], f"такт без дела сходил в Telegram: {rig.heads()[before:]}"
    assert out == {"expired": 0, "links": 0, "hide": 0, "later": 0}, out


async def test_an_empty_base_makes_no_request_either(rig):
    await heal.nightly(rig.bot, rig.services)
    assert rig.session.calls == [], rig.heads()


# ── такт: «Скрыть» на сутки ──────────────────────────────────────────────────

async def _sent_ago(rig, seconds: float, text: str, markup, chat: int = CHAT):
    rig.session.date = int((rig.clock.t - _dt.timedelta(seconds=seconds)).timestamp())
    return (await rig.bot.send_message(chat, text, reply_markup=markup)).message_id


async def test_hide_older_than_23h30_is_taken_off_younger_stays_action_buttons_remain(rig):
    lone = await _sent_ago(rig, 23 * HOUR + 30 * MIN, "✅ Готово", kb.hide_only())
    ext = await _sent_ago(rig, 30 * HOUR, "⏱ Подписка истекает", EXTEND)
    young = await _sent_ago(rig, 23 * HOUR + 29 * MIN, "✅ Тоже готово", kb.hide_only())
    await heal.nightly(rig.bot, rig.services)
    assert [c.head for c in rig.edit_calls(lone)] == [f"- markup off #{lone}"], \
        f"одиночная «Скрыть» не снята целиком: {[c.head for c in rig.edit_calls(lone)]}"
    got = rig.edit_calls(ext)
    assert len(got) == 1 and rows(got[0].markup) == [[("⏱ Продлить", "ext:take:5")]], \
        f"снят не только ряд «Скрыть»: {[(c.head, rows(c.markup)) for c in got]}"
    assert rig.edit_calls(young) == [], "«Скрыть» моложе 23 ч 30 мин снята — уведомление нельзя убрать"
    assert rig.rec(lone) is None and rig.rec(ext) is None, "запись со снятой «Скрыть» осталась"
    assert rig.rec(young) is not None
    assert all(not h.startswith("= edit") for h in rig.heads()), "только кнопки — а правился текст"


async def test_a_message_needing_both_link_and_hide_gets_one_text_edit_with_the_new_keyboard(rig):
    key = rig.snap()
    a = await _sent_ago(rig, 0, "⚠️ Не перенесены " + rig.link(key, "и ещё 4", new=True), EXTEND)
    rig.clock.t = EXP_T0 + _dt.timedelta(minutes=4)
    await heal.nightly(rig.bot, rig.services)
    got = rig.edit_calls(a)
    assert [c.head for c in got] == [f"= edit #{a}"], f"не одна правка текста: {[c.head for c in got]}"
    assert rows(got[0].markup) == [[("⏱ Продлить", "ext:take:5")]], \
        "правка текста вернула «Скрыть» или сняла кнопку действия"
    assert got[0].body == "⚠️ Не перенесены и ещё 4"
    assert rig.rec(a) is None


async def test_a_hundred_recipients_get_one_edit_each_at_the_bot_pace(rig, monkeypatch):
    """Объявление у ста получателей: по правке в каждом чате, старты не чаще общего темпа.
    Темп ускорен (200/с вместо 20/с), чтобы тест шёл полсекунды; правило то же."""
    monkeypatch.setattr(heal, "BOT_RATE", 200)
    chats = [7_000_000 + i for i in range(100)]
    for c in chats:
        await _sent_ago(rig, 25 * HOUR, "📢 Плановые работы", kb.hide_only(), chat=c)
    await heal.nightly(rig.bot, rig.services)
    per_chat: dict[int, int] = {}
    for _t, _name, chat, _mid in rig.session.edits:
        per_chat[chat] = per_chat.get(chat, 0) + 1
    assert per_chat == {c: 1 for c in chats}, f"не по одной правке в чате: {len(per_chat)} чатов"
    times = sorted(t for t, *_ in rig.session.edits)
    span = times[-1] - times[0]
    assert span >= 99 / 200 * 0.8, f"сто правок за {span:.3f} с — общий темп бота не держится"
    assert rig.records() == []


async def test_edits_in_one_chat_wait_a_second_apart(rig):
    for i in range(3):
        await _sent_ago(rig, 25 * HOUR, f"✅ Готово {i}", kb.hide_only())
    await heal.nightly(rig.bot, rig.services)
    assert len(rig.session.edits) == 3
    # длинные паузы тест пропускает, время цикла при этом не идёт: k-я пауза — до старта
    # через k секунд после первой правки (а не ещё одна секунда от «сейчас»)
    waits = sorted(rig.vt.long())
    assert len(waits) == 2, f"правки чата без паузы между ними: {rig.vt.slept}"
    assert 0.8 <= waits[0] <= 1.0 and 1.8 <= waits[1] <= 2.0, f"правки чата не через секунду: {rig.vt.slept}"


# ── такт: 429 ────────────────────────────────────────────────────────────────

async def test_429_on_the_tick_waits_at_most_a_minute_and_goes_on(rig):
    a = await _sent_ago(rig, 25 * HOUR, "✅ Готово", kb.hide_only())
    rig.session.refuse("flood", mid=a, retry_after=900)
    await heal.nightly(rig.bot, rig.services)
    assert max(rig.vt.long()) <= 60, f"такт ждал 429 дольше минуты: {rig.vt.slept}"
    assert len([e for e in rig.session.edits if e[3] == a]) == 2 and rig.rec(a) is None, \
        "после ожидания правка не повторена"


async def test_429_on_the_tick_waits_retry_after_when_it_is_short(rig):
    a = await _sent_ago(rig, 25 * HOUR, "✅ Готово", kb.hide_only())
    rig.session.refuse("flood", mid=a, retry_after=3)
    await heal.nightly(rig.bot, rig.services)
    assert 3 in rig.vt.slept and 60 not in rig.vt.slept, \
        f"Telegram просил 3 с, такт ждал другое: {rig.vt.slept}"
    assert rig.rec(a) is None


async def test_a_second_429_leaves_the_rest_of_the_chat_for_later_and_other_chats_go_on(rig):
    mine = [await _sent_ago(rig, 25 * HOUR, f"✅ Готово {i}", kb.hide_only()) for i in range(3)]
    other = await _sent_ago(rig, 25 * HOUR, "✅ Другой чат", kb.hide_only(), chat=OTHER)
    rig.session.refuse("flood", chat=CHAT, retry_after=5, times=100)
    out = await heal.nightly(rig.bot, rig.services)
    tried = [e for e in rig.session.edits if e[2] == CHAT]
    assert len(tried) == 2, f"после второго 429 чат правился дальше: {len(tried)} запросов"
    assert all(rig.rec(m) is not None for m in mine), "отложенные записи сняты — догон их не найдёт"
    assert rig.rec(other, OTHER) is None, "другой чат не поправлен из-за флуд-контроля в этом"
    assert out["later"] == 3, out


# ── такт: уборка записей без дела ────────────────────────────────────────────

async def test_the_tick_sweeps_records_idle_for_90_days_before_editing_and_says_so_once(rig, caplog):
    """Запись меню с итогом, ушедшего мимо гашения, — никто её не снимет: такт убирает
    такие старше 90 дней (строкой журнала, без запроса в Telegram), свежие не трогает;
    уборки нет — строки о ней нет."""
    import logging
    from awgbot.bot import screens
    old = (await rig.bot.send_message(CHAT, screens.with_note("📋 Свои сайты", "✅ Сайт добавлен"),
                                      reply_markup=MENU)).message_id
    rig.clock.advance(91 * 24 * HOUR)
    fresh = (await rig.bot.send_message(CHAT, screens.with_note("🗺 Подсети", "✅ Подсеть добавлена"),
                                        reply_markup=MENU)).message_id
    before = len(rig.session.calls)
    with caplog.at_level(logging.INFO):
        await heal.nightly(rig.bot, rig.services)
    assert rig.rec(old) is None, "запись без дела старше 90 дней пережила такт"
    assert rig.rec(fresh) is not None and rig.rec(fresh)["note"] == "✅ Подсеть добавлена", "свежая запись снята"
    assert rig.session.calls[before:] == [], f"уборка сходила в Telegram: {rig.heads()[before:]}"
    assert "lists: такт — снято записей сообщений без дела: 1" in caplog.text, caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await heal.nightly(rig.bot, rig.services)
    assert "снято записей сообщений без дела" not in caplog.text, "строка уборки, когда убирать нечего"
    assert "lists: такт — истекло снимков" in caplog.text



# ── такт: незнакомый отказ в одном чате ──────────────────────────────────────

@pytest.mark.parametrize("kind", ["teapot", "decode"])
async def test_an_unknown_refusal_in_one_chat_does_not_stop_the_tick(rig, monkeypatch, caplog, kind):
    """Ответ, которого правка не знает (418, страница вместо JSON), в одном чате: запись
    остаётся — правку повторит следующий такт, другой чат поправлен, строка такта есть."""
    import logging
    from tests.integration.test_list_msg_heal import _odd_refusal
    mine = await _sent_ago(rig, 25 * HOUR, "✅ Готово", kb.hide_only())
    other = await _sent_ago(rig, 25 * HOUR, "✅ Другой чат", kb.hide_only(), chat=OTHER)
    _odd_refusal(rig, monkeypatch, kind, chat=CHAT)
    with caplog.at_level(logging.INFO):
        out = await heal.nightly(rig.bot, rig.services)
    assert any(e[2] == CHAT and e[3] == mine for e in rig.session.edits), "сценарий не о том: правки не было"
    assert rig.rec(mine) is not None, f"{kind}: запись снята — «Скрыть» переживёт 48 часов"
    assert rig.rec(other, OTHER) is None, f"{kind}: другой чат не поправлен"
    assert out["later"] == 1 and out["hide"] == 1, out
    assert "lists: такт — истекло снимков 0, поправлено ссылок 0, снято «Скрыть» 1, отложено 1" in caplog.text, \
        caplog.text


# ── два такта разом ──────────────────────────────────────────────────────────

async def test_two_ticks_at_once_edit_a_message_once_and_count_it_once(rig, monkeypatch):
    """Догон на старте совпал с тактом после полуночи: оба прочли записи к правке, сообщение
    поправлено один раз, и в строках двух тактов оно посчитано один раз — иначе по
    журналу снято вдвое больше «Скрыть», чем было."""
    import asyncio
    import threading
    a = await _sent_ago(rig, 25 * HOUR, "✅ Готово", kb.hide_only())
    both = threading.Barrier(2, timeout=5)
    real_due = rig.db.list_msg_due
    reads = []

    def due(*args, **kwargs):
        out = real_due(*args, **kwargs)
        reads.append(1)
        if len(reads) <= 2:
            both.wait()                               # оба такта прочли до правки
        return out
    monkeypatch.setattr(rig.db, "list_msg_due", due)
    outs = await asyncio.gather(heal.nightly(rig.bot, rig.services), heal.nightly(rig.bot, rig.services))
    got = [e for e in rig.session.edits if e[3] == a]
    assert len(got) == 1, f"сообщение поправлено {len(got)} раза"
    assert sum(o["hide"] for o in outs) == 1, f"снятая «Скрыть» посчитана дважды: {outs}"
    assert rig.rec(a) is None
