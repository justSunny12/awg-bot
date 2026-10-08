"""Постоянная проба рельс общих разделов: временный раздел-образец
«🕒 Расписание» (один тумблер, один ввод, ключи app.schedule.* — только для
пробы) подсовывается в реестр sections и во временные словари обеих ролей и
гоняется через настоящий диспетчер роли — без единой строки обработчиков.

Проверяется ровно обещание рельс: новый общий раздел — это модуль плюс ID в
корне роли. Корень показывает кнопку, open рисует экран, тумблер пишет conf и
перерисовывает, ввод открывает приглашение с «✖️ Отмена», число пишется и
возвращает раздел с итогом первой строкой, отмена возвращает раздел; ни одна
кнопка не падает в обработчик устаревшей.

Цена ошибки: рельсы сломались — следующий раздел снова требует правок в
диспетчерах, таблицах границ и обработчиках обеих ролей, а пропуск одной из
них у человека выглядит как «Кнопка устарела» или «Эта настройка недоступна»
на только что выпущенной кнопке.
"""
from __future__ import annotations

import dataclasses
import itertools
import types

import pytest
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import roles, sections, ui
from awgbot.bot.callbacks import CancelCB, GwCB, SetCB
from awgbot.bot.handlers.stale import STALE_BUTTON
from awgbot.bot.sections._kb import back_button
from awgbot.core import settings
from tests.conftest import restore_settings
from tests.screens import harness
from tests.screens.base import Shot

ID = "schedule"
ON, HOUR = "app.schedule.enabled", "app.schedule.hour"


def _module() -> types.ModuleType:
    """Раздел-образец по рецепту §«Новый общий раздел»: только объявления."""
    m = types.ModuleType("awgbot.bot.sections.schedule")
    m.ID, m.LABEL, m.BACK = ID, "🕒 Расписание", "root"
    m.KEYS = (ON, HOUR)
    m.BOUNDS = {HOUR: (0, 23, "Час запуска", "ч")}
    m.DEFAULTS = {HOUR: 4}
    m.CYCLES = {}
    m.ACTIONS = {}

    def text(br) -> str:
        on = settings.get_bool(ON, True)
        hour = settings.get_int(HOUR, m.DEFAULTS[HOUR])
        return ui.screen(m.LABEL, f"{ui.tick(on)} {'вкл' if on else 'выкл'}",
                         lines=[f"Запуск в {hour:02d}:00 по часам {br.host_gen}" if on else None])

    def keyboard(br):
        on = settings.get_bool(ON, True)
        kb = InlineKeyboardBuilder()
        kb.button(text=f"{ui.tick(on)} Расписание", callback_data=br.cb.pack(ID, "toggle", ON))
        if on:
            kb.button(text=f"✏️ {settings.get_int(HOUR, m.DEFAULTS[HOUR]):02d}:00",
                      callback_data=br.cb.pack(ID, "edit", HOUR))
        kb.adjust(2)
        kb.row(back_button(br))
        return kb.as_markup()

    async def screen(br, services, key: str = ""):
        return text(br), keyboard(br)

    m.text, m.keyboard, m.screen = text, keyboard, screen
    return m


@pytest.fixture()
def rails(monkeypatch):
    """Раздел — в MODULES/SECTIONS, ID — в корнях временных словарей обеих
    ролей; после теста всё возвращается (monkeypatch) и conf — в копию."""
    mod = _module()
    monkeypatch.setattr(sections, "MODULES", sections.MODULES + (mod,))
    monkeypatch.setitem(sections.SECTIONS, ID, mod)
    for name in ("MAIN", "GATEWAY"):
        br = getattr(roles, name)
        monkeypatch.setattr(roles, name, dataclasses.replace(br, settings_root=br.settings_root + (ID,)))
    yield mod
    restore_settings()


ROLES = {"admin": "MAIN", "gateway": "GATEWAY"}


def _br(role):
    return getattr(roles, ROLES[role])


_N = itertools.count(1)


async def _take(tmp_path, role, *steps, conf=None):
    """Один прогон через диспетчер роли: свежие conf и БД (своя папка на прогон)."""
    shot = Shot(f"{'adm' if role == 'admin' else 'gw'}.rails{next(_N)}", role=role, steps=steps,
                conf=conf or {})
    return await harness.take(shot, tmp_path)


def _screens(rec):
    """Экраны записи по порядку: (текст, [подписи кнопок], [колбэки])."""
    out = []
    for c in rec.calls:
        if c.body is None or c.caption:
            continue
        rows = c.markup.inline_keyboard if c.markup is not None else []
        out.append((c.body, [b.text for r in rows for b in r], [b.callback_data for r in rows for b in r]))
    return out


def _stale(rec):
    return [c.head for c in rec.everything if STALE_BUTTON in (c.toast or "") or STALE_BUTTON in (c.body or "")]


def _root(role):
    return SetCB(sec="root") if role == "admin" else GwCB(action="settings")


@pytest.mark.parametrize("role", ["admin", "gateway"])
async def test_the_root_shows_the_new_section_and_its_button_opens_it(rails, tmp_path, role):
    """Корень рисуется из словаря роли: ID в settings_root — кнопка есть;
    нажатие на неё — экран раздела, а не «Кнопка устарела»."""
    rec = await _take(tmp_path, role, ("press", _root(role)))
    text, labels, datas = _screens(rec)[-1]
    assert "🕒 Расписание" in labels, labels
    btn = datas[labels.index("🕒 Расписание")]
    rec = await _take(tmp_path, role, ("press", _root(role)), ("press", btn))
    assert not _stale(rec), _stale(rec)
    text, labels, _ = _screens(rec)[-1]
    assert text.split("\n")[0] == "🕒 <b>Расписание</b> ✅ вкл", text
    assert f"по часам {_br(role).host_gen}" in text, "слова роли не подставились"
    assert labels == ["✅ Расписание", "✏️ 04:00", "⬅️ Назад"], labels


@pytest.mark.parametrize("role", ["admin", "gateway"])
async def test_the_toggle_writes_conf_and_redraws_the_section(rails, tmp_path, role):
    br = _br(role)
    rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", br.cb.pack(ID, "toggle", ON)))
    assert not _stale(rec), _stale(rec)
    assert settings.get_bool(ON, True) is False, "тумблер не записал conf"
    text, labels, _ = _screens(rec)[-1]
    assert text.split("\n")[0] == "🕒 <b>Расписание</b> ☑️ выкл" and labels == ["☑️ Расписание", "⬅️ Назад"], \
        (text, labels)


@pytest.mark.parametrize("role", ["admin", "gateway"])
async def test_the_input_asks_with_cancel_writes_the_number_and_puts_the_result_on_top(rails, tmp_path, role):
    """Ввод: приглашение на месте раздела с «✖️ Отмена» (реестр экранов:
    set_schedule), «5» пишет ключ, раздел возвращается с итогом первой
    строкой; переспрос на значение вне границ модуля. Ключа в conf нет —
    «сейчас» и «было» в итоге берутся из DEFAULTS модуля."""
    br = _br(role)
    conf: dict = {}
    rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", br.cb.pack(ID, "edit", HOUR)),
                      conf=conf)
    assert not _stale(rec), _stale(rec)
    heads = [c.head for c in rec.calls]
    assert not any(h.startswith("~!") for h in heads), f"ввод отказан: {heads}"
    text, labels, datas = _screens(rec)[-1]
    assert text.startswith("✏️ <b>Час запуска</b> · сейчас 4 ч · 0–23"), text
    assert labels == ["✖️ Отмена"] and CancelCB.unpack(datas[0]) == CancelCB(kind=f"set_{ID}", ref=0), datas
    rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", br.cb.pack(ID, "edit", HOUR)),
                      ("text", "24"), conf=conf)
    assert settings.get(HOUR) is None, "значение вне границ записано"
    assert any("0–23" in (c.body or "") for c in rec.calls), [c.head for c in rec.calls]
    rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", br.cb.pack(ID, "edit", HOUR)),
                      ("text", "5"), conf=conf)
    assert settings.get_int(HOUR, 4) == 5, "ввод не записал ключ"
    text, labels, _ = _screens(rec)[-1]
    assert text.startswith("✅ Час запуска: 4 → 5 ч\n\n🕒 <b>Расписание</b> ✅ вкл"), text
    assert "✏️ 05:00" in labels, labels


@pytest.mark.parametrize("role", ["admin", "gateway"])
async def test_cancel_under_the_prompt_brings_the_section_back(rails, tmp_path, role):
    br = _br(role)
    rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", br.cb.pack(ID, "edit", HOUR)),
                      ("press", CancelCB(kind=f"set_{ID}", ref=0)))
    assert not _stale(rec), _stale(rec)
    asked = [c for c in rec.everything if (c.body or "").startswith("✏️ <b>Час запуска</b>")]
    assert asked, "приглашения не было — отменять нечего: " + str([c.head for c in rec.everything])
    text, labels, _ = _screens(rec)[-1]
    assert text.split("\n")[0] == "🕒 <b>Расписание</b> ✅ вкл", text
    assert settings.get(HOUR) is None


@pytest.mark.parametrize("role", ["admin", "gateway"])
async def test_no_button_of_the_new_section_falls_into_the_stale_handler(rails, tmp_path, role):
    """Каждая кнопка раздела (в обоих состояниях тумблера) разбирается
    диспетчером раздела: «Назад» — корень, остальные — свой раздел."""
    br = _br(role)
    for conf in ({ON: True}, {ON: False}):
        rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), conf=conf)
        _, _, datas = _screens(rec)[-1]
        for data in datas:
            rec = await _take(tmp_path, role, ("press", br.cb.pack(ID)), ("press", data), conf=conf)
            assert not _stale(rec), f"{data}: ушла в устаревшую — {_stale(rec)}"
            assert rec.calls, f"{data}: бот промолчал"


def test_the_agent_packs_new_sections_by_the_common_rule_and_finds_them_back(rails):
    """Агент: раздел без строки в таблице имён упаковывается общим правилом
    «раздел/действие», ключевые действия — по имени действия; разбор
    возвращает раздел — по правилу или по KEYS модуля."""
    br = roles.GATEWAY
    assert br.cb.pack(ID).pack() == "gw:schedule/open:"
    assert sections.resolve(br, "gw:schedule/open:") == (ID, "open", "", "")
    assert sections.resolve(br, "gw:schedule/edit:app.schedule.hour") == (ID, "edit", HOUR, "")
    assert sections.resolve(br, br.cb.pack(ID, "edit", HOUR).pack()) == (ID, "edit", HOUR, "")
    assert sections.resolve(br, br.cb.pack(ID, "toggle", ON).pack()) == (ID, "toggle", ON, "")
    # без ID в корне роли раздел чужой: колбэк не наш — уйдёт в устаревшую
    plain = dataclasses.replace(br, settings_root=tuple(s for s in br.settings_root if s != ID))
    assert sections.resolve(plain, "gw:schedule/open:") is None
