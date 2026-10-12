"""E2E: «⚙️ Настройки → 💳 Подписки → 📈 Бонус» — ввод правила бонуса при
исчерпании в трёх форматах и «0», переспрос на мусор, строка экрана и
подпись кнопки при каждом виде, запись в conf и чтение обратно.

Цена ошибки: ввод «10% 50», записанный не той строкой, при следующем чтении
conf превращается в 100 ГБ — админ видит одно, профили получают другое;
переспрос, закрывший ввод, оставляет человека без приглашения; кнопка с
прежним «N ГБ» врёт о процентном правиле. Приглашение, переспрос и экран
целиком сверяют эталоны adm.set.subs.bonus*; здесь — запись, шаги и итог.
"""
from __future__ import annotations

import shutil

import pytest

from awgbot.bot import keyboards as kb, texts
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from awgbot.domain.services.traffic import bonus
from tests import conftest
from tests.conftest import FakeCallback, FakeMessage, FakeState, restore_settings

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
KEY = "limits.traffic_bonus_gb"


@pytest.fixture()
def conf(tmp_path):
    """Своя копия conf: запись настоящая (YAML), соседи по процессу её не видят."""
    for f in conftest._CONF_COPY.glob("*.yaml"):
        shutil.copy2(f, tmp_path / f.name)
    settings.init(tmp_path)
    yield tmp_path
    restore_settings()


def _reread(path):
    """Перечитать conf с диска — как после перезапуска бота."""
    settings.init(path)
    return bonus.current(settings)


async def _open_input(services, bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=bot)
    st = FakeState()
    await sh.edit_value(cb, SetCB(sec="subs", act="edit", key=KEY), st, services)
    return nav, st


async def _type(services, bot, st, text):
    msg = FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)
    await sh.receive_value(msg, st, services)
    return msg


def _subs_button():
    return next(b.text for row in kb.settings_subs().inline_keyboard for b in row
                if b.callback_data == SetCB(sec="subs", act="edit", key=KEY).pack())


async def test_edit_button_opens_the_bonus_prompt_not_a_stale_alert(services, fake_bot, conf):
    """Ключ бонуса убран из числовых границ — кнопка всё равно открывает свой
    ввод с текущим значением, а не «Кнопка устарела»."""
    nav, st = await _open_input(services, fake_bot)
    assert await st.get_state() == "SettingsInput:value", "ввод не открыт"
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and "Бонус при исчерпании" in shown[-1][1] and "сейчас 100 ГБ" in shown[-1][1], nav.sent


@pytest.mark.parametrize("typed, stored, screen, button, note", [
    ("50", 50, "50 ГБ", "50 ГБ", "✅ Бонус при исчерпании: 100 ГБ → 50 ГБ"),
    ("10%", "10%", "10% лимита", "10%", "✅ Бонус при исчерпании: 100 ГБ → 10% лимита"),
    (" 10%  50 ", "10% 50", "10% лимита, не более 50 ГБ", "10%, ≤ 50 ГБ",
     "✅ Бонус при исчерпании: 100 ГБ → 10% лимита, не более 50 ГБ"),
    ("0", 0, "выкл", "выкл", "✅ Бонус при исчерпании: 100 ГБ → выкл"),
])
async def test_each_format_is_saved_and_shown(services, fake_bot, conf, typed, stored, screen, button, note):
    """Ввод → значение в conf (переживает перечитывание с диска), итог первой
    строкой раздела, строка «Бонус при исчерпании: …» и кнопка — по новому
    правилу; ввод закрыт."""
    _, st = await _open_input(services, fake_bot)
    msg = await _type(services, fake_bot, st, typed)
    assert await st.get_state() is None, "ввод не закрыт после верного значения"
    assert settings.get(KEY) == stored, f"в conf {settings.get(KEY)!r} вместо {stored!r}"
    menu = [s for s in msg.sent if s[0] == "answer"]
    assert menu, msg.sent
    text, markup = menu[-1][1], menu[-1][2]
    assert text.splitlines()[0] == note, text.splitlines()[0]
    assert f"Бонус при исчерпании: {screen}" in text.splitlines(), text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert f"📈 Бонус: {button}" in labels, labels
    assert _reread(conf) == bonus.parse(typed), "после перечитывания conf правило другое"


@pytest.mark.parametrize("typed", ["abc", "10 ГБ", "101%", "10% 0", "100001", "", "50 10"])
async def test_garbage_is_asked_again_and_nothing_is_written(services, fake_bot, conf, typed):
    """Мусор — переспрос BONUS_BAD, ввод открыт, в conf прежнее."""
    _, st = await _open_input(services, fake_bot)
    msg = await _type(services, fake_bot, st, typed)
    assert ("answer", texts.BONUS_BAD, None) in msg.sent, msg.sent
    assert await st.get_state() == "SettingsInput:value", "ввод закрыт после переспроса"
    assert settings.get(KEY) == 100, "мусор записан в conf"


async def test_garbage_then_a_valid_value_still_saves(services, fake_bot, conf):
    """После переспроса тот же ввод принимает верное значение."""
    _, st = await _open_input(services, fake_bot)
    await _type(services, fake_bot, st, "abc")
    await _type(services, fake_bot, st, "10%")
    assert settings.get(KEY) == "10%" and await st.get_state() is None


@pytest.mark.parametrize("value, line, button", [
    (100, "Бонус при исчерпании: 100 ГБ", "📈 Бонус: 100 ГБ"),
    (0, "Бонус при исчерпании: выкл", "📈 Бонус: выкл"),
    ("10%", "Бонус при исчерпании: 10% лимита", "📈 Бонус: 10%"),
    ("10% 50", "Бонус при исчерпании: 10% лимита, не более 50 ГБ", "📈 Бонус: 10%, ≤ 50 ГБ"),
    ("испорчено", "Бонус при исчерпании: 100 ГБ", "📈 Бонус: 100 ГБ"),
])
def test_subs_screen_line_and_button_per_kind(conf, value, line, button):
    """Строка экрана и подпись кнопки — из одного правила; испорченное
    значение показано тем, что реально действует (100 ГБ)."""
    settings.set_value(KEY, value)
    lines = texts.settings_subs_text().splitlines()
    assert line in lines, lines
    assert "Отсрочка: 14 дн." in lines, "отсрочка — своей строкой"
    assert _subs_button() == button
