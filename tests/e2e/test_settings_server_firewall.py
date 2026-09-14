"""
Разделы «🖥 Сервер» и «🛡 Файервол» в настройках (docs/ROADMAP.md, п.8).

Это то, что раньше спрашивал установщик и делал CLI. Правки уезжают в новые
ссылки, а включение фильтра может запереть SSH — оба экрана обязаны говорить
об этом прямо и проверять ввод до записи.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ── сервер ───────────────────────────────────────────────────────────────────

async def test_server_screen_shows_what_goes_into_new_links(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "203.0.113.10", "name": "Сервер 1", "dns": "10.8.1.1", "mtu": 1376,
        "keepalive": "25-35", "iface": "awg0", "port": 51820,
        "subnet": "10.8.1.0/24", "kernel": "3.1.20260812", "generation": 1})
    text, markup = await sh._screen("srv", services)
    assert "203.0.113.10" in text and "Сервер 1" in text and "3.1.20260812" in text
    assert "поколение 1" in text
    assert "новым" in text and "перевыпуск профилей" in text, "цена правки названа"
    labels = _labels(markup)
    assert "✏️ Адрес сервера" in labels and "✏️ DNS клиентов" in labels and "✏️ MTU" in labels
    assert not any("порт" in l.lower() for l in labels), "порт кнопкой не меняется"


@pytest.mark.parametrize("raw, ok", [
    ("203.0.113.10", True), ("vpn.example.org", True),
    ("не адрес", False), ("", False), ("10.8.1.300", False),
])
def test_server_host_is_validated_before_it_reaches_a_link(raw, ok):
    valid, err = sh._validate_server_value("app.network.server_host", raw)
    assert valid is ok
    assert valid or err


def test_dns_accepts_one_or_two_addresses():
    v = sh._validate_server_value
    assert v("app.client_config.dns1", "10.8.1.1")[0]
    assert v("app.client_config.dns1", "10.8.1.1, 1.1.1.1")[0]
    assert not v("app.client_config.dns1", "1.1.1.1 1.0.0.1 8.8.8.8")[0]
    assert not v("app.client_config.dns1", "резолвер")[0]


async def test_single_dns_is_written_to_both_fields(services, fake_bot, monkeypatch):
    """Публичный адрес вторым номером вернул бы утечку резолва мимо dnsmasq:
    стеки опрашивают список не строго по порядку."""
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "h", "name": "n", "dns": "10.8.1.1", "mtu": 1376, "keepalive": "25",
        "iface": "awg0", "port": 1, "subnet": "s", "kernel": "", "generation": 1})
    st = FakeState()
    await st.update_data(key="app.client_config.dns1", sec="srv")
    msg = FakeMessage(text="10.8.1.1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.receive_value(msg, st, services)
    assert written["app.client_config.dns1"] == "10.8.1.1"
    assert written["app.client_config.dns2"] == "10.8.1.1"


# ── файервол ─────────────────────────────────────────────────────────────────

def _fw(**kw):
    base = {"enabled": False, "present": True, "rollback": False, "ufw": False,
            "ssh_port": 22, "allow": [], "raw_allow": [], "unresolved": [],
            "admin_ips": ["10.8.1.2"], "nat": True}
    base.update(kw)
    return base


async def test_firewall_screen_offers_enable_and_lists_addresses(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "firewall_screen",
                        lambda: _fw(raw_allow=["203.0.113.7", "home.example.org"]))
    text, markup = await sh._screen("fw", services)
    assert "🔴 фильтр выключен" in text and "203.0.113.7" in text
    assert "NAT клиентов" in text, "выключение фильтра не обещает потерю интернета"
    labels = _labels(markup)
    assert "🟢 Включить фильтр" in labels and "➕ Добавить адрес" in labels
    assert "➖ 203.0.113.7" in labels and "➖ home.example.org" in labels


async def test_armed_screen_allows_only_confirm_or_rollback(services, fake_bot, monkeypatch):
    """Пока идёт обратный отсчёт, любое другое действие — способ забыть о нём."""
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(enabled=True, rollback=True))
    text, markup = await sh._screen("fw", services)
    assert "Идёт проверка входа" in text and "новое" in text
    labels = _labels(markup)
    assert labels[:2] == ["✅ Вход работает, подтверждаю", "↩️ Откатить сейчас"]
    assert not any("Включить" in l or "Добавить" in l for l in labels)


async def test_enable_arms_the_timer_and_confirm_disarms_it(services, fake_bot, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "firewall_enable", lambda: calls.append("on") or 300)
    monkeypatch.setattr(services, "firewall_confirm", lambda: calls.append("confirm") or True)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(enabled=True))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="on"), services)
    assert calls == ["on"]
    assert any("Проверь вход" in s[1] for s in nav.sent if s[0] == "answer")
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="confirm"), services)
    assert calls == ["on", "confirm"]
    assert any("Таймер снят" in s[1] for s in nav.sent if s[0] == "answer")


async def test_rollback_now_disarms_and_removes_the_filter(services, fake_bot, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "firewall_confirm", lambda: calls.append("disarm") or True)
    monkeypatch.setattr(services, "firewall_disable", lambda: calls.append("off") or ["снято"])
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="rollback"), services)
    assert calls == ["disarm", "off"], "сначала снять таймер, иначе он сработает по пустому"
    assert any("SSH снова открыт" in s[1] for s in nav.sent if s[0] == "answer")


async def test_adding_a_bad_address_is_refused_without_writing(services, fake_bot, monkeypatch):
    from awgbot.domain.services import ServiceError
    seen = []

    def add(raw):
        seen.append(raw)
        raise ServiceError("«мусор» не адрес, не подсеть и не имя")
    monkeypatch.setattr(services, "firewall_allow_add", add)
    st = FakeState()
    await st.update_data(key="app.firewall.ssh_allow", sec="fw")
    msg = FakeMessage(text="мусор", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.receive_value(msg, st, services)
    assert seen == ["мусор"]
    assert any("не адрес" in s[1] for s in msg.sent if s[0] == "answer")
    assert await st.get_data(), "ввод остаётся открытым — можно поправить, не начиная заново"
