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
from awgbot.bot.handlers import settingscore as core
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
        "host": "vpn.example.org", "name": "Сервер 1", "dns": "10.8.1.1", "mtu": 1376,
        "keepalive": "25-35", "iface": "awg0", "port": 51820, "port_conf": 51820,
        "subnet": "10.8.1.0/24", "kernel": "3.1.20260812", "generation": 1,
        "migration_blocked": ""})
    text, markup = await sh._screen("srv", services)
    assert "vpn.example.org" in text and "Сервер 1" in text and "3.1.20260812" in text
    assert "поколение 1" in text
    assert "новым" in text and "переездом профилей" in text, "цена правки названа"
    labels = _labels(markup)
    assert "✏️ Доменное имя" in labels and "✏️ DNS клиентов" in labels and "✏️ MTU" in labels
    assert "✏️ Имя сервера" in labels
    assert "🚚 Сменить порт или подсеть" in labels
    assert not any(l.startswith("✏️") and "порт" in l.lower() for l in labels), \
        "порт не правится как обычная настройка"


async def test_server_screen_says_when_there_is_no_domain(services, fake_bot, monkeypatch):
    """Доменного имени может не быть вовсе — в ссылки тогда уезжает IP. Пустая
    строка выглядела бы как потерянное значение."""
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "203.0.113.10", "name": "Сервер 1", "dns": "10.8.1.1", "mtu": 1376,
        "keepalive": "25-35", "iface": "awg0", "port": 45871, "port_conf": 45871,
        "subnet": "10.8.1.0/24", "kernel": "", "generation": 1, "migration_blocked": ""})
    text, _ = await sh._screen("srv", services)
    assert "Доменное имя: не задано" in text and "203.0.113.10" in text


async def test_server_screen_flags_a_port_mismatch(services, fake_bot, monkeypatch):
    """Экран, говорящий одно, пока ссылки несут другое, хуже отсутствующего."""
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "vpn.example.org", "name": "X", "dns": "10.8.1.1", "mtu": 1376,
        "keepalive": "25", "iface": "awg0", "port": 45871, "port_conf": 51820,
        "subnet": "10.8.1.0/24", "kernel": "", "generation": 1, "migration_blocked": ""})
    text, _ = await sh._screen("srv", services)
    assert "⚠️" in text and "45871" in text and "51820" in text


@pytest.mark.parametrize("raw, ok", [
    ("203.0.113.10", True), ("vpn.example.org", True),
    ("не адрес", False), ("", False), ("10.8.1.300", False),
])
def test_server_host_is_validated_before_it_reaches_a_link(raw, ok):
    valid, err = core._validate_server_value("app.network.server_host", raw)
    assert valid is ok
    assert valid or err


def test_dns_accepts_one_or_two_addresses():
    v = core._validate_server_value
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
    assert "Доступ по SSH" in text, "заголовок раздела не обновлён"
    assert "таймером" in text, "перед включением про таймер сказать надо"
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

async def test_enabled_screen_does_not_repeat_the_timer_promise(services, fake_bot, monkeypatch):
    """На включённом фильтре таймер — прошедшее время: строка только занимает
    место в экране, который и без того длинный."""
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(enabled=True))
    text, _ = await sh._screen("fw", services)
    assert "🟢 фильтр включён" in text
    assert "таймером" not in text and "NAT клиентов" not in text

# ── переезд из раздела «Сервер AWG» ─────────────────────────────────────────

async def test_server_screen_hides_the_migration_button_while_one_runs(services, fake_bot, monkeypatch):
    """Запрет симметричный: пока идёт переезд по поколению, своего затевать
    нельзя — и раздел обязан объяснить почему, а не просто спрятать кнопку."""
    monkeypatch.setattr(services, "server_screen", lambda: {
        "host": "vpn.example.org", "name": "X", "dns": "1.1.1.1", "mtu": 1376,
        "keepalive": "25", "iface": "awg0", "port": 45871, "port_conf": 45871,
        "subnet": "10.8.1.0/24", "kernel": "", "generation": 1,
        "migration_blocked": "идёт переезд на поколение 2: он начат сменой ядра"})
    text, markup = await sh._screen("srv", services)
    assert "нельзя" in text and "поколение 2" in text
    labels = _labels(markup)
    assert not any("Сменить порт" in l for l in labels)


async def test_prepare_screen_names_the_cohort_and_the_cost(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "migration_prepare_data", lambda want_port=0: {
        "iface": "awg0", "port": 45871, "subnet": "10.8.1.0/24",
        "clients": 3, "devices": 7, "want_port": want_port, "blocked": ""})
    text, markup = await sh._screen("mig_prep", services)
    assert "45871" in text and "10.8.1.0/24" in text
    assert "7" in text and "3" in text, "размер когорты не назван"
    assert "отмена безопасна" in text.lower() and "финал" in text.lower()
    labels = _labels(markup)
    assert "🚚 Поднять второй интерфейс" in labels and "✏️ Задать порт" in labels


async def test_prepare_runs_and_restarts(services, fake_bot, monkeypatch):
    calls: list = []
    monkeypatch.setattr(services, "migration_prepare",
                        lambda port=None: calls.append(port) or
                        {"iface": "awg1", "subnet": "10.9.1.0/24", "port": "443"})
    monkeypatch.setattr(services, "set_restart_wait", lambda c, m: calls.append("wait"))
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="mig_prep", act="do", key="go", val="443"), services)
    assert calls == [443, "wait", "restart"]
    said = [s[1] for s in nav.sent if s[0] == "answer"]
    assert said and "awg1" in said[-1] and "Начать переезд" in said[-1]


async def test_prepare_failure_does_not_restart(services, fake_bot, monkeypatch):
    calls: list = []

    def boom(port=None):
        raise RuntimeError("порт 443 занят")
    monkeypatch.setattr(services, "migration_prepare", boom)
    monkeypatch.setattr(services, "restart_bot", lambda: calls.append("restart"))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="mig_prep", act="do", key="go"), services)
    assert calls == []
    said = [s[1] for s in nav.sent if s[0] == "answer"]
    assert said and "занят" in said[-1] and "не тронут" in said[-1]


async def test_maintenance_mentions_migration_only_with_its_button(services, fake_bot, monkeypatch):
    """Без кнопки разговор о переезде — это рассказ о механизме, которого в
    этом экране не видно."""
    monkeypatch.setattr(services, "svc_screen_data",
                        lambda: {"state": "", "available": False, "progress": None, "orphans": 0})
    text, markup = await sh._screen("svc", services)
    assert "Переезд" not in text
    assert not any("переезд" in b.text.lower()
                   for row in markup.inline_keyboard for b in row)

    monkeypatch.setattr(services, "svc_screen_data",
                        lambda: {"state": "", "available": True, "progress": None, "orphans": 0})
    text, markup = await sh._screen("svc", services)
    assert "Переезд профилей" in text
    assert any("переезд" in b.text.lower() for row in markup.inline_keyboard for b in row)


# ── свой DNS-резолвер из раздела «Сервер AWG» ────────────────────────────────

def _srv(private_dns: dict, blocked: str = "") -> dict:
    return {"host": "vpn.example.org", "name": "Сервер 1", "dns": "1.1.1.1, 1.0.0.1",
            "mtu": 1376, "keepalive": "25-35", "iface": "awg0", "port": 51820,
            "port_conf": 51820, "subnet": "10.8.1.0/24", "kernel": "v3.1.20260906",
            "generation": 1, "migration_blocked": blocked, "private_dns": private_dns}


async def test_public_dns_is_named_and_the_resolver_is_offered(services, monkeypatch):
    monkeypatch.setattr(services, "server_screen", lambda: _srv(
        {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1", "decision": ""}))
    text, markup = await sh._screen("srv", services)
    assert "публичный" in text and "🔒 Свой DNS-резолвер" in _labels(markup)

    monkeypatch.setattr(services, "server_screen", lambda: _srv(
        {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1",
         "decision": "pending"}))
    text, _ = await sh._screen("srv", services)
    assert "при следующем переезде станет свой" in text


async def test_private_dns_is_named_and_nothing_is_offered(services, monkeypatch):
    monkeypatch.setattr(services, "server_screen", lambda: _srv(
        {"mode": "private", "dns1": "10.8.1.1", "dns2": "10.8.1.1", "target": "10.8.1.1",
         "decision": ""}))
    text, markup = await sh._screen("srv", services)
    assert "свой резолвер на сервере" in text and "🔒 Свой DNS-резолвер" not in _labels(markup)


async def test_dns_screen_explains_and_offers_three_ways(services, monkeypatch):
    monkeypatch.setattr(services, "private_dns_info", lambda: {
        "target": "10.8.1.1", "mode": "public", "decision": "", "dns1": "1.1.1.1", "dns2": "1.0.0.1"})
    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "")
    text, markup = await sh._screen("dns", services)
    assert "10.8.1.1" in text and "DoH" in text and "переезд" in text.lower()
    labels = _labels(markup)
    assert "🚚 Переехать сейчас" in labels and "⏳ При следующем переезде" in labels \
        and "Не нужно" in labels and "⬅️ Назад" in labels

    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "идёт переезд")
    _, markup = await sh._screen("dns", services)
    assert "🚚 Переехать сейчас" not in _labels(markup), "переезд сейчас невозможен — кнопки нет"


@pytest.mark.parametrize("key, decision, expect", [
    ("later", "pending", "следующий переезд"),
    ("never", "dismissed", "публичный DNS"),
])
async def test_later_and_never_record_the_decision(services, fake_bot, monkeypatch, key, decision, expect):
    cb, nav = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key=key), services, FakeState())
    assert services.private_dns_decision() == decision
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and expect in shown[-1][1]
    assert any("Назад" in b.text for row in shown[-1][2].inline_keyboard for b in row)


async def test_now_records_pending_and_opens_the_migration_preparation(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "")
    monkeypatch.setattr(services, "migration_prepare_data", lambda want_port=0: {
        "iface": "awg0", "port": 45871, "subnet": "10.8.1.0/24", "clients": 1, "devices": 2,
        "want_port": want_port, "private_dns": True, "blocked": ""})
    cb, nav = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key="now"), services, FakeState())
    assert services.private_dns_decision() == "pending"
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and "Смена порта или подсети" in shown[-1][1]
    assert "свой резолвер" in shown[-1][1], "подготовка называет, что DNS станет своим"
    assert "🚚 Поднять второй интерфейс" in _labels(shown[-1][2])


async def test_now_while_a_migration_runs_explains_and_stays(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "migration_blocked_reason", lambda: "идёт переезд")
    monkeypatch.setattr(services, "server_screen", lambda: _srv(
        {"mode": "public", "dns1": "1.1.1.1", "dns2": "1.0.0.1", "target": "10.8.1.1",
         "decision": "pending"}, blocked="идёт переезд"))
    cb, nav = _acb(fake_bot)
    await sh.private_dns_action(cb, SetCB(sec="dns", act="do", key="now"), services, FakeState())
    assert services.private_dns_decision() == "pending", "решение записано — исполнит идущий/следующий переезд"
    assert cb.answers and cb.answers[-1][1] is True and "идёт переезд" in cb.answers[-1][0]


def test_dns_handler_is_registered_before_the_generic_do_action():
    order = [h.callback.__name__ for h in sh.router.callback_query.handlers]
    assert order.index("private_dns_action") < order.index("do_action")


# ── порт SSH ─────────────────────────────────────────────────────────────────

async def test_port_button_is_first_and_opens_the_prompt(services, fake_bot, monkeypatch):
    from awgbot.bot.states import SshPort
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    _, markup = await sh._screen("fw", services)
    assert _labels(markup)[0] == "🅿️ Изменить порт"
    cb, nav = _acb(fake_bot)
    st = FakeState()
    await sh.ssh_port_ask(cb, st, services)
    assert await st.get_state() == SshPort.value.state
    assert any("Порт SSH" in s[1] for s in nav.sent if s[0] == "edit_text"), nav.sent


async def test_busy_port_is_refused_with_a_way_back(services, fake_bot, monkeypatch):
    """Занятый порт — отказ с кнопкой «Назад» в раздел, а не переспрос: ввод
    закрыт, порт не менялся."""
    changed = []
    monkeypatch.setattr(services, "ssh_port_busy",
                        lambda p: 'tcp LISTEN 0.0.0.0:8443 users:(("nginx",pid=1,fd=6))')
    monkeypatch.setattr(services, "ssh_port_change", lambda p: changed.append(p) or 22)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="8443", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert not changed and await st.get_state() is None
    sent = [s for s in msg.sent if s[0] == "answer" and "Порт 8443 занят, необходимо выбрать другой" in s[1]]
    assert sent, msg.sent
    assert _labels(sent[0][2]) == ["⬅️ Назад"]
    assert SetCB.unpack(sent[0][2].inline_keyboard[0][0].callback_data).sec == "fw"


async def test_free_port_is_applied_and_the_section_is_redrawn(services, fake_bot, monkeypatch):
    changed = []
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: "")
    monkeypatch.setattr(services, "ssh_port_change", lambda p: changed.append(p) or 22)
    # экран отдаёт текущий порт: до смены 22, после — новый
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(ssh_port=changed[-1] if changed else 22))
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="2222", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert changed == [2222] and await st.get_state() is None
    texts_sent = [s[1] for s in msg.sent if s[0] == "answer"]
    assert any("22 → <b>2222</b>" in t for t in texts_sent)
    assert any("порт SSH 2222" in t for t in texts_sent), "раздел перерисован с новым портом"


async def test_bad_port_is_asked_again_and_refusal_from_sshd_is_shown(services, fake_bot, monkeypatch):
    from awgbot.domain.services import ServiceError
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: "")
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw())

    def boom(p):
        raise ServiceError("sshd -t: Bad configuration option")
    monkeypatch.setattr(services, "ssh_port_change", boom)
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="70000", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert await st.get_state() == "SshPort:value", "ввод открыт — можно поправить"
    assert any("от 1 до 65535" in s[1] for s in msg.sent if s[0] == "answer")
    msg = FakeMessage(text="2222", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert any("Порт не изменён" in s[1] and "Bad configuration" in s[1]
               for s in msg.sent if s[0] == "answer")


async def test_same_port_is_a_finisher_not_a_refusal(services, fake_bot, monkeypatch):
    """Текущий порт формально «занят» (им же sshd) — но человеку это не
    отказ: закрываем ввод финишером и показываем раздел, ничего не трогая."""
    touched = []
    monkeypatch.setattr(services, "ssh_port_busy", lambda p: touched.append(("busy", p)) or "x")
    monkeypatch.setattr(services, "ssh_port_change", lambda p: touched.append(("change", p)) or 22)
    monkeypatch.setattr(services, "firewall_screen", lambda: _fw(ssh_port=22))
    st = FakeState()
    await st.set_state("SshPort:value")
    msg = FakeMessage(text="22", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.ssh_port_received(msg, st, services)
    assert not touched and await st.get_state() is None
    texts_sent = [s[1] for s in msg.sent if s[0] == "answer"]
    assert any("не изменился" in t and "(22)" in t for t in texts_sent), texts_sent
    assert any("порт SSH 22" in t for t in texts_sent), "раздел показан после финишера"
