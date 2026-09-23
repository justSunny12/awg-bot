"""Ссылки в текстах про шлюзы: строка «Бот шлюза» (чат бота слота по его
username) и имена шлюзов в строке РФ-доступа шапки админа — deep-link
«/start gw-<слот>» в карточку слота."""
from __future__ import annotations

import re

from awgbot.bot import texts

BOT = "awg_test_bot"


def _hrefs(line: str) -> list[tuple[str, str]]:
    """(href, подпись) всех ссылок строки — по порядку."""
    return re.findall(r'<a href="([^"]+)">([^<]*)</a>', line)


def _card(slot: int) -> str:
    return f"https://t.me/{BOT}?start={texts.GW_CARD_PAYLOAD}-{slot}"


# ── «Бот шлюза: …» ───────────────────────────────────────────────────────────

def test_agent_bot_line_links_to_the_bot_chat_by_username():
    line = texts.agent_bot_line({"agent_bot": {"username": "naspi_gw_bot", "name": "Шлюз квартиры"}})
    assert line == 'Бот шлюза: <a href="https://t.me/naspi_gw_bot">Шлюз квартиры</a>', \
        "ссылка — на диалог с ботом, без команд"


def test_agent_bot_line_escapes_the_profile_name():
    """Имя профиля бота задаёт человек в BotFather: «<» или «&» без
    экранирования Telegram отвергает — и не приходит вся карточка."""
    line = texts.agent_bot_line({"agent_bot": {"username": "x_bot", "name": "<b>R&D</b>"}})
    assert "<b>R&D</b>" not in line and "&lt;b&gt;R&amp;D&lt;/b&gt;" in line, line


def test_agent_bot_line_without_username_is_empty():
    """Бота ещё не спросили (нет токена, Telegram молчал) — строки нет вовсе,
    а не «Бот шлюза: » с пустой ссылкой."""
    assert texts.agent_bot_line({"agent_bot": {}}) == ""
    assert texts.agent_bot_line({"agent_bot": {"username": "", "name": "Имя"}}) == ""
    assert texts.agent_bot_line({}) == ""
    assert texts.agent_bot_line(None) == ""


def test_agent_bot_line_falls_back_to_username_for_the_label():
    line = texts.agent_bot_line({"agent_bot": {"username": "naspi_gw_bot", "name": ""}})
    assert line == 'Бот шлюза: <a href="https://t.me/naspi_gw_bot">naspi_gw_bot</a>'


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────

def _info(ok=True, active="NASPi", active_slot=1, standby=()):
    return {"ok": ok, "active": active, "active_slot": active_slot, "standby": list(standby)}


def test_working_line_links_the_active_gateway_to_its_card():
    line = texts.routing_admin_status_line(_info(), BOT)
    assert line == f'🇷🇺 РФ-доступ: 🟢 работает (<a href="{_card(1)}">NASPi</a>)', line


def _plain(line: str) -> str:
    return re.sub(r"</?a[^>]*>", "", line)


def test_standby_word_links_to_the_standby_slot_not_the_active_one():
    """Слово «резерв» ведёт в карточку РЕЗЕРВА (ссылка в карточку активного
    здесь показала бы не тот шлюз); состояние — жив / не отвечает /
    проверяется — остаётся текстом, и строка без разметки читается как
    прежде."""
    for state, tail, dot in (("alive", "жив", "🟢"), ("dead", "не отвечает", "🟠"),
                             ("unknown", "проверяется", "🟢")):
        line = texts.routing_admin_status_line(
            _info(standby=[{"name": "Pi2", "slot": 2, "state": state}]), BOT)
        assert _hrefs(line) == [(_card(1), "NASPi"), (_card(2), "резерв")], (state, line)
        assert _plain(line) == f"🇷🇺 РФ-доступ: {dot} работает (NASPi), резерв {tail}", (state, line)


def test_active_on_slot_two_links_to_slot_two():
    """После переключения активен слот 2 — ссылка имени ведёт в его карточку,
    резерв — в карточку слота 1."""
    line = texts.routing_admin_status_line(
        _info(active="Pi2", active_slot=2, standby=[{"name": "NASPi", "slot": 1, "state": "alive"}]), BOT)
    assert _hrefs(line) == [(_card(2), "Pi2"), (_card(1), "резерв")], line


def test_off_line_links_every_named_gateway():
    """«выключен» — имена тех, кто не отвечает, тоже ссылки: именно туда и
    идут разбираться."""
    both = texts.routing_admin_status_line(
        _info(ok=False, standby=[{"name": "Pi2", "slot": 2, "state": "dead"}]), BOT)
    assert both.startswith("🇷🇺 РФ-доступ: 🔴 выключен — ") and both.endswith(" не отвечают"), both
    assert _hrefs(both) == [(_card(1), "NASPi"), (_card(2), "Pi2")], both
    one = texts.routing_admin_status_line(
        _info(ok=False, standby=[{"name": "Pi2", "slot": 2, "state": "alive"}]), BOT)
    assert one == (f'🇷🇺 РФ-доступ: 🔴 выключен, <a href="{_card(1)}">NASPi</a> не отвечает, '
                   f'<a href="{_card(2)}">резерв</a> жив'), one


def test_link_labels_are_escaped():
    """Имя устройства задаёт человек: внутри ссылки оно экранируется так же,
    как без неё."""
    line = texts.routing_admin_status_line(_info(active="<Pi & co>"), BOT)
    assert _hrefs(line) == [(_card(1), "&lt;Pi &amp; co&gt;")], line


def test_without_username_the_line_stays_plain():
    """username бота ещё не известен (старт без getMe) — прежний чистый текст,
    без обрывков разметки."""
    assert texts.routing_admin_status_line(
        _info(standby=[{"name": "Pi2", "slot": 2, "state": "alive"}])) == \
        "🇷🇺 РФ-доступ: 🟢 работает (NASPi), резерв жив"
    assert texts.routing_admin_status_line(
        _info(ok=False, standby=[{"name": "Pi2", "slot": 2, "state": "dead"}]), "") == \
        "🇷🇺 РФ-доступ: 🔴 выключен — NASPi, Pi2 не отвечают"


def test_without_slot_numbers_there_is_nothing_to_link():
    """Сведения без номера слота (слот 0, старая форма словаря) — чистый
    текст: ссылка «gw-0» открыла бы «не найдено»."""
    line = texts.routing_admin_status_line(
        {"ok": True, "active": "NASPi", "standby": [{"name": "Pi2", "state": "alive"}]}, BOT)
    assert line == "🇷🇺 РФ-доступ: 🟢 работает (NASPi), резерв жив", line


def test_admin_panel_passes_the_bot_username_to_the_routing_line():
    out = texts.admin_panel({"ok": True}, bot_username=BOT,
                            routing_info=_info(standby=[{"name": "Pi2", "slot": 2, "state": "alive"}]))
    assert f'<a href="{_card(1)}">NASPi</a>' in out and f'<a href="{_card(2)}">резерв</a> жив' in out, out
