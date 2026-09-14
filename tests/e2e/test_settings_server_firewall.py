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


# ── раскладка корня настроек ─────────────────────────────────────────────────

def test_settings_root_order_and_names():
    """Порядок — от того, что трогают при настройке сервера, к тому, что
    трогают раз в полгода. Мониторинг и бэкапы уехали в «Обслуживание»: корень
    распух до десяти строк, а открывают их не ради настройки, а когда чинят."""
    from awgbot.bot import keyboards as kb
    rows = [b.text for row in kb.settings_root().inline_keyboard for b in row]
    assert rows == ["🖥 Сервер AWG", "🛡 Доступ по SSH", "🇷🇺 Условная маршрутизация",
                    "✉️ E-mail", "🔔 Уведомления", "💳 Параметры подписок",
                    "🔄 Обслуживание", "⬆️ Обновления бота", "⬅️ В меню"]


def test_maintenance_holds_monitoring_and_backups_first():
    from awgbot.bot import keyboards as kb
    rows = [b.text for row in kb.settings_svc().inline_keyboard for b in row]
    assert rows[:4] == ["📊 Мониторинг", "💾 Резервное копирование",
                        "🔄 Перезапустить AWG", "🔄 Перезапустить бота"]


def test_moved_sections_return_to_maintenance():
    """Выход из раздела обязан вести туда, откуда в него вошли, — иначе
    «Назад» выбрасывает в корень, и человек ищет, где он был."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot.callbacks import SetCB
    for markup in (kb.settings_mon(), kb.settings_backup()):
        assert markup.inline_keyboard[-1][0].callback_data == SetCB(sec="svc").pack()


# ── остальные ветки действий файервола ───────────────────────────────────────

async def test_removing_an_address_by_its_number(services, fake_bot, monkeypatch):
    """В кнопке номер записи, а не адрес: двоеточие IPv6 ломало упаковку. Номер
    обязан разрешаться в тот же адрес, что показан в списке."""
    removed: list = []
    monkeypatch.setattr(services, "firewall_screen",
                        lambda: _fw(raw_allow=["203.0.113.7", "2001:db8::1"]))
    monkeypatch.setattr(services, "firewall_allow_remove", lambda e: removed.append(e))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="del", val="1"), services)
    assert removed == ["2001:db8::1"]


async def test_stale_list_does_not_remove_a_neighbour(services, fake_bot, monkeypatch):
    """Список изменился с момента отрисовки — номер указывает уже на другого.
    Удалить соседа молча хуже, чем отказаться."""
    removed: list = []
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(raw_allow=["203.0.113.7"]))
    monkeypatch.setattr(services, "firewall_allow_remove", lambda e: removed.append(e))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="del", val="5"), services)
    assert removed == []
    assert any("изменился" in (a[0] or "") for a in cb.answers)


async def test_unknown_action_is_refused_and_screen_survives(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="чего-то-нет"), services)
    assert any("недоступно" in (a[0] or "") for a in cb.answers)


async def test_failure_inside_an_action_is_shown_not_swallowed(services, fake_bot, monkeypatch):
    """Отказ nft — это то, что человек обязан увидеть: правила не применились."""
    def boom():
        raise RuntimeError("nft: Operation not permitted")
    monkeypatch.setattr(services, "firewall_enable", boom)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="fw", act="do", key="on"), services)
    assert any("Не вышло" in (a[0] or "") for a in cb.answers)
    assert any(s[0] == "edit_text" for s in nav.sent), "экран не перерисован после отказа"


def test_every_edit_button_points_at_a_known_setting():
    """Опечатка в ключе кнопки превращает её в «Эта настройка недоступна», и
    раздел становится мёртвым. Сверяем ключи клавиатур со словарями текстов."""
    from awgbot.bot import keyboards as kb, texts
    from awgbot.bot.callbacks import SetCB
    known = set(texts.SETTINGS_TEXT) | set(texts.SETTINGS_BOUNDS)
    markups = [kb.settings_server(), kb.settings_firewall({"raw_allow": ["1.2.3.4"]}),
               kb.settings_mon(), kb.settings_subs(), kb.settings_notify()]
    checked = 0
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                if not b.callback_data.startswith("set:"):
                    continue
                cb = SetCB.unpack(b.callback_data)
                if cb.act != "edit":
                    continue
                checked += 1
                assert cb.key in known, f"кнопка «{b.text}» ведёт в несуществующий ключ {cb.key}"
    assert checked >= 8, "проверять оказалось нечего — тест устарел"
