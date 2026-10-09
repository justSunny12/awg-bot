"""Ссылки в текстах про шлюзы: строка «Бот шлюза» (чат бота слота по его
username) и имена шлюзов в строке РФ-доступа шапки админа — deep-link
«/start gw-<слот>» в карточку слота. Формы обеих строк на экранах — в эталоне
tests/screens/admin.txt: подпись бота по username (adm.gw.card.bot.username),
шапка во всех состояниях резерва и активного (adm.main.gw.*); здесь —
экранирование и пустые значения на входе функций."""
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


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────

def _info(ok=True, active="NASPi", active_slot=1, standby=()):
    return {"ok": ok, "active": active, "active_slot": active_slot, "standby": list(standby)}


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
        "🇷🇺 РФ-доступ: 🟢 работает · NASPi · резерв жив"
    assert texts.routing_admin_status_line(
        _info(ok=False, standby=[{"name": "Pi2", "slot": 2, "state": "dead"}]), "") == \
        "🇷🇺 РФ-доступ: 🔴 недоступен — NASPi, Pi2 не отвечают"


def test_without_slot_numbers_there_is_nothing_to_link():
    """Сведения без номера слота (слот 0, старая форма словаря) — чистый
    текст: ссылка «gw-0» открыла бы «не найдено»."""
    line = texts.routing_admin_status_line(
        {"ok": True, "active": "NASPi", "standby": [{"name": "Pi2", "state": "alive"}]}, BOT)
    assert line == "🇷🇺 РФ-доступ: 🟢 работает · NASPi · резерв жив", line
