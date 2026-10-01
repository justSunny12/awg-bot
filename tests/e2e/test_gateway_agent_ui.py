"""E2E: экраны агента шлюза в новой раскладке — панель (строки по состояниям,
предупреждения отдельными строками, «⬆️ Доступна vX», пять кнопок), здоровье
с железом и «🔧 Восстановить», корень настроек без «Обслуживания»,
мониторинг с порогом линка в минутах (хранится в секундах), бэкапы (цикл
канала, «день и час» одним вводом), обновления (проверка при открытии, цикл
расписания, «никогда» → «месяц» и при открытии, и при старте агента).

Цена ошибки: предупреждение о чужом слоте в хвосте панели — его не видят, а
линк лежит; порог линка, записанный минутами в секундный ключ, — монитор
кричит «линк мёртв» через 5 секунд тишины; «никогда» из старого конфига —
строка «⬆️ Доступна vX» на панели умирает навсегда; перезапуск без
подтверждения — РФ-доступ у всех рвётся от промаха пальцем.
"""
from __future__ import annotations

import types

import pytest

import awgbot.core.config as cfg
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.core import settings
from awgbot.domain.gateway import GatewayServices, GwCheck, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = cfg.ADMIN_ID


class _Svc(GatewayServices):
    """Агент без хоста: живой статус — подставной, пробы считаются."""

    def __init__(self, db):
        super().__init__(db)
        self.live = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi")
        self.probes = 0

    def status(self):
        self.probes += 1
        return self.live

    def invalidate_static(self):
        pass


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(linkclient, "enabled", lambda: False)
    d = Database(tmp_path / "gw.db"); d.init_schema()
    yield _Svc(d)
    d.close()


@pytest.fixture()
def store(monkeypatch):
    """Настройки в памяти: запись видна чтению, как у настоящего кэша."""
    data: dict = {}
    real_get, real_int, real_bool = settings.get, settings.get_int, settings.get_bool
    monkeypatch.setattr(settings, "set_value", lambda k, v: data.__setitem__(k, v) or [k])
    monkeypatch.setattr(settings, "get", lambda k, d=None: data[k] if k in data else real_get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(data[k]) if k in data else real_int(k, d))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(data[k]) if k in data else real_bool(k, d))
    return data


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _msg(bot, text):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


def _last_edit(nav):
    s = next(x for x in reversed(nav.sent) if x[0] == "edit_text")
    return s[1], s[2]


def _button(markup, label):
    return next(b for r in markup.inline_keyboard for b in r if b.text == label)


# ── панель ───────────────────────────────────────────────────────────────────

async def _panel(svc, fake_bot, st: GwStatus):
    """Панель «В меню» из снимка тика — ровно таким, каким его дали."""
    svc.cached_status = lambda max_age: st
    cb, nav = _acb(fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    return _last_edit(nav)


async def test_the_panel_names_the_role_by_the_channel(svc, fake_bot, monkeypatch):
    """Роль — по каналу: несёт трафик / в резерве; канала или связи нет —
    роль неизвестна, первая строка говорит о линке."""
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi", uptime_seconds=12 * 86400)
    text, _ = await _panel(svc, fake_bot, st)
    assert text.splitlines()[0] == "🛰 <b>naspi</b> · 🟢 линк поднят · 12 дн", text
    monkeypatch.setattr(linkclient, "enabled", lambda: True)
    monkeypatch.setattr(linkclient, "online", lambda: True)
    for role, head in (("active", "🟢 несёт трафик"), ("standby", "🟢 в резерве")):
        monkeypatch.setattr(linkclient, "role", lambda role=role: role)
        text, _ = await _panel(svc, fake_bot, st)
        assert text.splitlines()[:3] == [f"🛰 <b>naspi</b> · {head} · 12 дн", "", "📡 Линк до сервера AWG 🟢 40 с · 🔗 упр. канал 🟢"], text
    monkeypatch.setattr(linkclient, "online", lambda: False)
    text, _ = await _panel(svc, fake_bot, GwStatus(hostname="naspi"))
    assert text.splitlines()[0] == "🛰 <b>naspi</b> · 🔴 линк лежит", "связи нет — роль по линку"


@pytest.mark.parametrize("mark, online, warn", [
    ("unmarked", True, "⚠️ Шлюз в боте сервера AWG не назначен — запрос ушёл по упр. каналу"),
    ("unmarked", False, "⚠️ Шлюз в боте сервера AWG не назначен, упр. канала нет — перешли ему сообщение из отчёта"),
    ("foreign", True, "⚠️ Шлюз этого слота — другое устройство, линк лежит"),
    ("unconfirmed", False, "⚠️ Аплинк этого устройства не найден — шлюз не подтверждён"),
])
async def test_warnings_are_own_lines_at_the_top_not_in_the_tail(svc, fake_bot, monkeypatch, mark, online, warn):
    """Предупреждение — своей строкой сразу под именем и линком, выше SSH,
    железа и здоровья: в хвосте панели его не читают, а линк при чужом
    слоте лежит."""
    monkeypatch.setattr(linkclient, "online", lambda: online)
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi", cpu=12.0, mark_status=mark,
                  ssh={"port": 22, "owner": "", "filter": False, "allow": 0, "new_plumbing": True})
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert lines[3] == warn, lines
    ssh = next(i for i, ln in enumerate(lines) if ln.startswith("🛡 SSH"))
    assert lines.index(warn) < ssh < next(i for i, ln in enumerate(lines) if ln.startswith("📈")), lines


async def test_a_confirmed_gateway_has_no_warning_line(svc, fake_bot):
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi", mark_status="confirmed")
    text, _ = await _panel(svc, fake_bot, st)
    assert "⚠️" not in text and "⬆️" not in text, text


async def test_the_panel_says_a_new_version_is_available_from_the_last_check(svc, fake_bot):
    """«⬆️ Доступна vX» — по тегу последней проверки, без похода в сеть при
    каждом открытии панели; тег без «v» — с «v»; пусто — строки нет."""
    svc.db.set_state("update_available_tag", "3.2.0")
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi")
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert lines[3] == "<b>⬆️ Доступна v3.2.0</b>", "строка новой версии — жирным, после шапки, пустой строки и линка: " + str(lines)
    svc.db.set_state("update_available_tag", "")
    text, _ = await _panel(svc, fake_bot, st)
    assert "Доступна" not in text


async def test_start_and_menu_show_the_new_version_line_too(svc, fake_bot, monkeypatch):
    """Строка «⬆️ Доступна» — не только на «Обновить»: /start и «В меню»
    рисуют ту же панель из снимка."""
    svc.db.set_state("update_available_tag", "v3.2.0")
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    msg = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert "<b>⬆️ Доступна v3.2.0</b>" in msg.sent[-1][1].splitlines(), msg.sent[-1][1]
    cb, nav = _acb(fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    assert "<b>⬆️ Доступна v3.2.0</b>" in _last_edit(nav)[0].splitlines()


async def test_the_panel_has_five_buttons_with_transit_and_four_without(svc, fake_bot):
    """[🩺 Здоровье] [🔧 Восстановить] / [🔀 VPN-транзит] / [🔄 Обновить]
    [⚙️ Настройки]; без VPN-транзита — ни кнопки, ни строк «🔀», «📋», «🗂»."""
    lan = {"iface": "eth0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "domains": 41200, "nets": 12,
           "updated_at": "", "own_vpn": 5, "own_ru": 1, "lan_pkts": 1234567,
           "svc": {"active": True, "own": ["a"], "peer": ["b", "c"], "ever": True}}
    text, markup = await _panel(svc, fake_bot, GwStatus(link_up=True, handshake_age=40.0, lan=lan))
    assert _rows(markup) == [["🩺 Здоровье", "🔧 Восстановить"], ["🔀 VPN-транзит"],
                             ["🔄 Обновить", "⚙️ Настройки"]]
    lines = text.splitlines()
    # числа — с разрядами, как в макете
    assert "🔀 VPN-транзит 🟢 · 1 234 567 пакетов с роутера" in lines, lines
    assert "📋 Списки: 41 200 доменов, 12 подсетей · свои: 5 в туннель, 1 напрямую" in lines, lines
    assert "🗂 SMB: свои — 1, извне — 2" in lines, lines
    assert [GwCB.unpack(b.callback_data).action for r in markup.inline_keyboard for b in r] == \
        ["health", "reassert", "lan", "refresh", "settings"]
    text, markup = await _panel(svc, fake_bot, GwStatus(link_up=True, handshake_age=40.0))
    assert _rows(markup) == [["🩺 Здоровье", "🔧 Восстановить"], ["🔄 Обновить", "⚙️ Настройки"]]
    assert not any(ln.startswith(("🔀", "📋", "🗂")) for ln in text.splitlines()), text


async def test_no_own_domains_means_no_own_tail_on_the_panel_but_a_line_on_the_screen(svc, fake_bot):
    """Своих доменов нет — хвост «· свои: пусто» в строке «📋 Списки» панели
    не рисуется (нули не выводим); на экране «🔀 VPN-транзит» строка «Свои
    списки: пусто» остаётся — там человек их и заводит."""
    lan = {"iface": "eth0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "domains": 41200, "nets": 12,
           "updated_at": "", "own_vpn": 0, "own_ru": 0, "lan_pkts": 5}
    st = GwStatus(link_up=True, handshake_age=40.0, lan=lan)
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert "📋 Списки: 41 200 доменов, 12 подсетей" in lines, lines
    assert not any("свои" in ln for ln in lines), lines
    assert "Свои списки: пусто" in texts.gateway_transit_text(st, []).splitlines()


async def test_the_smb_line_is_on_the_panel_only_with_subnet_link(svc, fake_bot):
    """«🗂» — только при включённой связи подсетей (блок svc активен)."""
    lan = {"iface": "eth0", "addr": "192.168.1.10", "domains": 1, "nets": 1, "lan_pkts": 1,
           "svc": {"active": False, "own": ["a"], "peer": ["b"], "ever": True}}
    text, _ = await _panel(svc, fake_bot, GwStatus(link_up=True, handshake_age=40.0, lan=lan))
    assert "🗂" not in text, text


async def test_the_health_summary_and_traffic_share_one_line(svc, fake_bot):
    """«🩺 Здоровье ✅ · 📊 …» при трафике; проблемы — числом и именами;
    нулевой трафик не выводится."""
    st = GwStatus(link_up=True, handshake_age=40.0, month_rx=1024 ** 3, month_tx=0,
                  checks=[GwCheck("MASQUERADE", False, "нет"), GwCheck("линк", True)])
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert lines[-2].startswith("🩺 Здоровье 🔴 проблем: 1 — MASQUERADE · 📊 1 ГБ"), lines
    assert lines[-1].startswith("<i>обновлено ") and lines[-1].endswith("</i>"), lines


# ── здоровье и восстановление ────────────────────────────────────────────────

async def test_health_is_live_and_offers_recovery_and_the_menu(svc, fake_bot):
    """«🩺 Здоровье» — живьём, по строкам, с железом и модулем; проблемы есть —
    подсказка, что сделает «🔧 Восстановить»; кнопки — восстановить и в меню."""
    svc.live = GwStatus(link_up=True, hostname="naspi", ram_free_mb=1234, disk_free_gb=98.0, smart="ОК",
                        throttled={"now": [], "ever": []}, module_version="1.0.20250915",
                        srcversion="1a2b3c4d5e", kernels_total=3,
                        checks=[GwCheck("линк", True, "хендшейк 40 с"), GwCheck("обвязка", False, "нет цепочки")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1 and cb.answers[0][0] == "Проверяю…"
    text, markup = _last_edit(nav)
    assert text.splitlines() == [
        "🩺 <b>Здоровье naspi</b> 🔴 проблем: 1",
        "✅ линк — хендшейк 40 с",
        "🔴 обвязка — нет цепочки",
        "RAM свободно 1 234 МБ · диск свободно 98 ГБ, SMART ОК · питание ОК",
        "Модуль awg 1.0.20250915 · srcversion 1a2b3c4d… · ядер 3",
        "«🔧 Восстановить» переставит правила и переподнимет линк"], text
    assert _rows(markup) == [["🔧 Восстановить", "⬅️ В меню"]]
    assert [GwCB.unpack(b.callback_data).action for r in markup.inline_keyboard for b in r] == ["reassert", "panel"]
    svc.live.checks = [GwCheck("линк", True)]
    await gh.gw_health(cb, svc)
    text, _ = _last_edit(nav)
    assert text.startswith("🩺 <b>Здоровье naspi</b> ✅ проблем нет") and "Восстановить" not in text, text


async def test_health_details_from_the_host_are_escaped(svc, fake_bot):
    svc.live = GwStatus(checks=[GwCheck("<b>x</b>", False, "rc=1 & <i>")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    assert "🔴 &lt;b&gt;x&lt;/b&gt; — rc=1 &amp; &lt;i&gt;" in _last_edit(nav)[0].splitlines()


async def test_recovery_asks_first_with_cancel_first_and_cancel_returns_to_the_panel(svc, fake_bot, monkeypatch):
    """«🔧 Восстановить» сам ничего не трогает: цена одной строкой, «Отмена»
    первой и назад — на панель; подтверждение — реассерт и итог
    «Восстановление: готово» отдельным сообщением."""
    calls = []
    monkeypatch.setattr(svc, "reassert", lambda: calls.append(1) or (True, ""))

    async def _poke(services):
        return None
    monkeypatch.setattr(linkclient, "poke", _poke)
    _carrying(svc)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action="reassert"), svc)
    text, markup = _last_edit(nav)
    assert text == ("🔧 Восстановить шлюз?\nЮнит переставит правила (маскарад, изоляция, метка) и "
                    "переподнимет линк — РФ-доступ прервётся на секунды"), text
    assert _rows(markup) == [["⬅️ Отмена", "🔧 Восстановить"]]
    assert [GwCB.unpack(b.callback_data).action for r in markup.inline_keyboard for b in r] == ["panel", "reassert!"]
    assert calls == []
    await gh.gw_execute(cb, GwCB(action="reassert!"), svc)
    assert calls == [1]
    assert any(k == "edit_text" and t.startswith("✅ <b>Восстановление: готово</b>") for k, t, _ in nav.sent), nav.sent


def _carrying(svc, role: str = "active", link_up: bool = True) -> None:
    """Сервер сказал роль, последний тик монитора видел линк живым или нет."""
    svc.set_link_role(role == "active", standby=False, name="NASPi")
    svc.cached_status = lambda max_age: GwStatus(link_up=link_up, handshake_age=40.0 if link_up else None,
                                                 hostname="naspi")


@pytest.mark.parametrize("action, text", [
    ("restart", "🔁 Перезапустить AWG? Линк опустится и поднимется — РФ-доступ у всех прервётся на секунды"),
    ("botrestart", "🔁 Перезапустить бота? Вернётся через несколько секунд; без влияния на пользователей"),
])
async def test_restarts_ask_first_and_cancel_returns_to_settings(svc, fake_bot, action, text):
    """Перезапуски — из корня настроек, «Отмена» первой и назад — туда же."""
    _carrying(svc)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action=action), svc)
    shown, markup = _last_edit(nav)
    assert shown == text
    assert _rows(markup) == [["⬅️ Отмена", "🔁 Перезапустить"]]
    assert [GwCB.unpack(b.callback_data).action for r in markup.inline_keyboard for b in r] == \
        ["settings", f"{action}!"]


@pytest.mark.parametrize("role, link_up", [("standby", True), ("active", False)])
@pytest.mark.parametrize("action, text", [
    ("restart", "🔁 Перезапустить AWG? Линк опустится и поднимется"),
    ("reassert", "🔧 Восстановить шлюз?\nЮнит переставит правила (маскарад, изоляция, метка) и "
                 "переподнимет линк"),
    ("botrestart", "🔁 Перезапустить бота? Вернётся через несколько секунд; без влияния на пользователей"),
])
async def test_a_gateway_not_carrying_traffic_is_not_threatened_with_rf_access_loss(
        svc, fake_bot, role, link_up, action, text):
    """Резерв или шлюз с мёртвым линком трафик клиентов не несёт: «РФ-доступ
    у всех прервётся» у него — ложная тревога, из-за которой человек
    откладывает нужный перезапуск. Подтверждение то же, без этого хвоста."""
    _carrying(svc, role=role, link_up=link_up)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action=action), svc)
    shown, markup = _last_edit(nav)
    assert shown == text, shown
    assert "РФ-доступ" not in shown
    assert _rows(markup)[0][0] == "⬅️ Отмена"


def test_the_bundle_question_puts_cancel_first():
    assert _rows(kb.gateway_bundle_kb()) == [["⬅️ Отмена", "📦 Применить"]]


# ── настройки ────────────────────────────────────────────────────────────────

async def test_settings_root_is_the_version_and_two_columns(svc, fake_bot, monkeypatch):
    """Корень — заголовок с версией и кнопки в два столбца; старое
    «Обслуживание» из сообщений 3.1.0 открывает тот же корень."""
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.1.0")
    cb, nav = _acb(fake_bot)
    await gh.gw_settings(cb, svc, FakeState())
    text, markup = _last_edit(nav)
    assert text == "⚙️ <b>Настройки</b> · v3.1.0"
    assert _rows(markup) == [["🔔 Уведомления", "✉️ E-mail"], ["🛡 SSH-доступ", "🩺 Мониторинг"],
                             ["💾 Бэкапы", "⬆️ Обновления"], ["🔁 Перезапуск AWG", "🔁 Перезапуск бота"],
                             ["⬅️ В меню"]]
    cb, nav = _acb(fake_bot)
    await gh.gw_maint(cb, svc, FakeState())
    assert _last_edit(nav)[0] == "⚙️ <b>Настройки</b> · v3.1.0"


async def test_notify_section_text_follows_the_values(svc, fake_bot, store):
    store.update({"quiet_hours.quiet_hours_enabled": True, "quiet_hours.quiet_hours_start": 20,
                  "quiet_hours.quiet_hours_end": 7, "resource_alerts.enabled": True,
                  "resource_alerts.thresholds_percent.cpu": 80, "resource_alerts.thresholds_percent.ram": 80,
                  "resource_alerts.thresholds_percent.disk": 80, "app.gateway.temp_alert_c": 75})
    cb, nav = _acb(fake_bot)
    await gh.gw_section(cb, GwCB(action="notify"), svc, FakeState())
    text, _ = _last_edit(nav)
    assert text.splitlines()[:3] == ["🔔 <b>Уведомления</b>", "Тихие часы 20:00–07:00 МСК — без звука, кроме аварий",
                                     "Алерты хоста: CPU 80% · RAM 80% · диск 80% · 75 °C"], text
    assert "Аварии на e-mail — только когда Telegram недоступен" in text
    store.update({"quiet_hours.quiet_hours_enabled": False, "resource_alerts.enabled": False})
    await gh.gw_section(cb, GwCB(action="notify"), svc, FakeState())
    text, _ = _last_edit(nav)
    assert text.splitlines()[1:3] == ["Тихие часы выключены — уведомления со звуком круглые сутки",
                                      "Алерты хоста выключены"], text


async def test_monitoring_link_threshold_is_minutes_on_screen_and_seconds_in_the_config(svc, fake_bot, store):
    """«⏳ Линк: 5 мин» → приглашение в минутах с границами 1–1440 → «7»
    пишет 420 секунд; итог и раздел — в минутах."""
    store["app.gateway.handshake_max_age"] = 300
    cb, nav = _acb(fake_bot)
    await gh.gw_section(cb, GwCB(action="mon"), svc, FakeState())
    text, markup = _last_edit(nav)
    assert "линк молчит дольше 5 мин" in text, text
    btn = _button(markup, "⏳ Линк: 5 мин")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB.unpack(btn.callback_data), svc, st)
    assert _last_edit(nav)[0] == "✏️ <b>Линк молчит дольше</b> · сейчас 5 мин · 1–1440", _last_edit(nav)[0]
    msg = _msg(fake_bot, "7")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 420, "ввод в минутах записан не секундами"
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1, answers
    lines = answers[0][1].split("\n")
    assert lines[0] == "✅ Линк молчит дольше: 5 → 7 мин", lines
    assert "линк молчит дольше 7 мин" in answers[0][1]
    assert "⏳ Линк: 7 мин" in [b for r in _rows(answers[0][2]) for b in r]
    assert await st.get_state() is None


@pytest.mark.parametrize("raw", ["0", "1441", "abc", "5.5", ""])
async def test_monitoring_link_threshold_out_of_bounds_is_asked_again(svc, fake_bot, store, raw):
    """Ввод вне 1–1440 минут — переспрос, ничего не записано, ввод открыт:
    «0» записался бы порогом в 0 секунд — линк мёртв всегда."""
    st = FakeState()
    await st.set_state("SettingsInput:value")
    await st.update_data(key="app.gateway.handshake_max_age", sec="mon")
    msg = _msg(fake_bot, raw)
    await gh.gw_receive_value(msg, st, svc)
    assert "app.gateway.handshake_max_age" not in store
    assert [s[1] for s in msg.sent if s[0] == "answer"] == ["⚠️ Нужно целое число 1–1440 мин"]
    assert await st.get_state() is not None


async def test_link_threshold_absent_in_the_config_reads_as_the_default_five_minutes(svc, fake_bot, store):
    """Ключа в конфиге агента нет (секция закомментирована) — монитор живёт с
    300 с по умолчанию: и приглашение, и итог говорят «5 мин», а не «—»."""
    assert settings.get("app.gateway.handshake_max_age") is None, "в образце конфига ключ появился — тест устарел"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.handshake_max_age"), svc, st)
    assert _last_edit(nav)[0] == "✏️ <b>Линк молчит дольше</b> · сейчас 5 мин · 1–1440"
    msg = _msg(fake_bot, "10")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 600
    first = next(s[1] for s in msg.sent if s[0] == "answer").split("\n")[0]
    assert first == "✅ Линк молчит дольше: 5 → 10 мин", first


def test_the_mon_keyboard_rounds_odd_seconds_up_and_never_to_zero(store):
    """Секунды из старого конфига не кратны минуте — подпись в целых минутах
    вверх (90 с — «2 мин»: алерт не должен казаться раньше, чем есть),
    меньше минуты — «1 мин», а не «0 мин»; текст раздела — то же число."""
    store["app.gateway.handshake_max_age"] = 90
    assert "⏳ Линк: 2 мин" in [b for r in _rows(kb.gateway_mon_kb()) for b in r]
    assert "линк молчит дольше 2 мин" in texts.gw_settings_mon_text()
    store["app.gateway.handshake_max_age"] = 30
    assert "⏳ Линк: 1 мин" in [b for r in _rows(kb.gateway_mon_kb()) for b in r]


# ── 💾 бэкапы ────────────────────────────────────────────────────────────────

async def test_backup_section_is_the_main_bot_layout(svc, fake_bot, store):
    store.update({"app.scheduler.backup_enabled": True, "app.scheduler.backup_day": 1,
                  "app.scheduler.backup_hour": 12, "app.scheduler.backup_channel": "telegram"})
    cb, nav = _acb(fake_bot)
    await gh.gw_section(cb, GwCB(action="backup"), svc, FakeState())
    text, markup = _last_edit(nav)
    assert text.splitlines()[:2] == ["💾 <b>Бэкапы</b> ✅ вкл · 🔓 без шифрования",
                                     "Каждое 1-е число в 12:00 → в этот чат"], text
    assert _rows(markup) == [["✅ Автобэкапы", "🔐 Шифрование"], ["📨 Куда: Telegram", "✏️ 1-е, 12:00"],
                             ["💾 Сделать сейчас"], ["⬅️ Назад"]]
    assert GwCB.unpack(markup.inline_keyboard[-1][0].callback_data).action == "settings"


async def test_backup_channel_cycle_checks_mailbox_and_encryption(svc, fake_bot, store):
    """«📨 Куда» — цикл Telegram ↔ E-mail: без ящика — экран «почта не
    настроена», без шифрования — alert; прошло — записано, раздел
    перерисован, всплывашка с новым значением; обратно — Telegram."""
    key = "app.scheduler.backup_channel"
    store.update({"app.scheduler.backup_enabled": True, key: "telegram"})
    cb, nav = _acb(fake_bot)
    await gh.gw_section(cb, GwCB(action="backup"), svc, FakeState())
    cyc = GwCB.unpack(_button(_last_edit(nav)[1], "📨 Куда: Telegram").callback_data)
    assert cyc == GwCB(action="cyc", val=key)
    cb, nav = _acb(fake_bot)
    await gh.gw_cycle(cb, cyc, svc)
    assert store[key] == "telegram" and _last_edit(nav)[0] == texts.EMAIL_NOT_CONFIGURED
    svc.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    cb, nav = _acb(fake_bot)
    await gh.gw_cycle(cb, cyc, svc)
    # у агента в копии ключи линка и туннеля, а не устройств — текст роли
    assert store[key] == "telegram" and cb.answers == [(texts.backup_needs_encryption(gateway=True), True)], \
        cb.answers
    svc.backup_set_passphrase("correct horse battery")
    cb, nav = _acb(fake_bot)
    await gh.gw_cycle(cb, cyc, svc)
    assert store[key] == "email" and cb.answers[-1] == ("Куда: E-mail", False), cb.answers
    assert "📨 Куда: E-mail" in [b for r in _rows(_last_edit(nav)[1]) for b in r]
    cb, nav = _acb(fake_bot)
    await gh.gw_cycle(cb, cyc, svc)
    assert store[key] == "telegram" and cb.answers[-1] == ("Куда: Telegram", False)


async def test_an_unknown_cycle_key_is_refused(svc, fake_bot, store):
    cb, _ = _acb(fake_bot)
    await gh.gw_cycle(cb, GwCB(action="cyc", val="app.gateway.monitor_minutes"), svc)
    assert cb.answers == [("Кнопка устарела — открой раздел заново", True)]
    assert "app.gateway.monitor_minutes" not in store


async def test_backup_day_and_hour_in_one_input(svc, fake_bot, store):
    """«✏️ 1-е, 12:00» → одно приглашение «день и час»; «5 9» — записано,
    итог первой строкой раздела, кнопка — новая; «31 12» — переспрос."""
    store.update({"app.scheduler.backup_enabled": True, "app.scheduler.backup_day": 1,
                  "app.scheduler.backup_hour": 12})
    cb, nav = _acb(fake_bot)
    await gh.gw_section(cb, GwCB(action="backup"), svc, FakeState())
    when = GwCB.unpack(_button(_last_edit(nav)[1], "✏️ 1-е, 12:00").callback_data)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, when, svc, st)
    assert _last_edit(nav)[0] == ("✏️ <b>День и час автобэкапа</b> · сейчас 1-го в 12:00 · пришли два числа: "
                                  "<code>1 12</code>")
    bad = _msg(fake_bot, "31 12")
    await gh.gw_receive_value(bad, st, svc)
    assert [s[1] for s in bad.sent if s[0] == "answer"] == [texts.BACKUP_WHEN_BAD]
    assert store["app.scheduler.backup_day"] == 1
    msg = _msg(fake_bot, "5 9")
    await gh.gw_receive_value(msg, st, svc)
    assert (store["app.scheduler.backup_day"], store["app.scheduler.backup_hour"]) == (5, 9)
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1 and answers[0][1].split("\n")[0] == "✅ Автобэкап: 1-е, 12:00 → 5-е, 09:00"
    assert "✏️ 5-е, 09:00" in [b for r in _rows(answers[0][2]) for b in r]
    assert answers[0][1].split("\n", 2)[2].startswith("💾 <b>Бэкапы"), "итог — не в разделе бэкапов"


# ── ⬆️ обновления ────────────────────────────────────────────────────────────

@pytest.fixture()
def upd(svc, monkeypatch, store):
    """Проверка обновлений без сети: счётчик походов к списку релизов."""
    from awgbot.infra import updates
    scene = {"next": types.SimpleNamespace(tag="v3.2.1", body="- пункт", title=""),
             "fail": False, "calls": 0}

    def _next(max_generation=None):
        scene["calls"] += 1
        if scene["fail"]:
            raise updates.UpdateError("GitHub не ответил")
        return scene["next"]
    monkeypatch.setattr(updates, "next_release", _next)
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.2.0")
    store["updates.poll_schedule"] = "day"
    return scene


async def test_opening_updates_checks_right_away(svc, fake_bot, upd):
    """Проверка — при открытии раздела: «Проверяю…», найденная цель — в шапке
    и кнопкой «⬆️ Обновить до vX»; тег ложится туда, откуда строку
    «⬆️ Доступна vX» читает панель."""
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    assert cb.answers[0][0] == "Проверяю…" and upd["calls"] == 1
    text, markup = _last_edit(nav)
    assert text.split("\n")[0] == "⬆️ <b>Обновления</b> · v3.2.0 → v3.2.1", text
    assert _rows(markup) == [["⬆️ Обновить до v3.2.1"], ["✅ Уведомлять", "📅 Проверка: день"], ["⬅️ Назад"]]
    assert svc.update_available_tag() == "v3.2.1"
    upd["next"] = None
    await gh.gw_updates_screen(cb, svc)
    text, markup = _last_edit(nav)
    assert text == "⬆️ <b>Обновления</b> · v3.2.0 🟢 актуальна" and _rows(markup)[0] == ["✅ Уведомлять", "📅 Проверка: день"]


async def test_a_failed_check_says_so_and_keeps_the_found_version(svc, fake_bot, upd):
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    upd["fail"] = True
    await gh.gw_updates_screen(cb, svc)
    assert _last_edit(nav)[0] == "⬆️ <b>Обновления</b> · v3.2.0 ⚪ проверка не удалась", _last_edit(nav)[0]
    assert svc.update_available_tag() == "v3.2.1", "сбой проверки стёр найденную версию"


async def test_notify_toggle_and_schedule_cycle_answer_at_once_without_the_network(svc, fake_bot, upd, store):
    """Тумблер и цикл «📅 Проверка» рисуют раздел по сохранённому тегу — без
    похода к списку релизов на каждое нажатие; «никогда» в цикле нет."""
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    calls = upd["calls"]
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_toggle(cb, svc)
    assert svc.updates_muted() and cb.answers[0][0] == "Уведомления выключены"
    text, markup = _last_edit(nav)
    assert text.split("\n")[0] == "⬆️ <b>Обновления</b> · v3.2.0 → v3.2.1" and _rows(markup)[1][0] == "☑️ Уведомлять"
    assert _rows(markup)[0] == ["⬆️ Обновить до v3.2.1"], "кнопка обновления пропала без сети"
    cyc = GwCB.unpack(_button(markup, "📅 Проверка: день").callback_data)
    assert cyc == GwCB(action="cyc", val="updates.poll_schedule")
    seen = []
    for word in ("неделя", "месяц", "день", "неделя"):
        cb, nav = _acb(fake_bot)
        await gh.gw_cycle(cb, cyc, svc)
        seen.append(store["updates.poll_schedule"])
        assert cb.answers[0][0] == f"Проверка: {word}"
        assert _rows(_last_edit(nav)[1])[1][1] == f"📅 Проверка: {word}"
    assert seen == ["week", "month", "day", "week"]
    assert upd["calls"] == calls, "тумблер или цикл сходили в сеть"


async def test_the_old_check_button_redraws_the_section_that_checks_itself(svc, fake_bot, upd):
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_check(cb, svc)
    assert _last_edit(nav)[0].split("\n")[0] == "⬆️ <b>Обновления</b> · v3.2.0 → v3.2.1"
    assert upd["calls"] == 1 and len(cb.answers) == 1


async def test_never_from_an_old_config_becomes_month_and_mute_on_open(svc, fake_bot, upd, store):
    """«никогда» из старого конфига: раздел при открытии ставит «месяц» и
    выключает уведомления — проверка идёт ради строки на панели."""
    store["updates.poll_schedule"] = "never"
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    assert store["updates.poll_schedule"] == "month" and svc.updates_muted()
    assert _rows(_last_edit(nav)[1])[1] == ["☑️ Уведомлять", "📅 Проверка: месяц"]
    assert upd["calls"] == 1, "проверка при «никогда» не пошла"


async def test_never_from_an_old_config_becomes_month_and_mute_at_agent_start(tmp_path, monkeypatch):
    """То же при старте агента — до первого открытия раздела: иначе до него
    периодическая проверка спит, и строка «⬆️ Доступна vX» молчит."""
    from awgbot.runtime import main as rt
    from tests.conftest import restore_settings
    (tmp_path / "updates.yaml").write_text('poll_schedule: "never"\npoll_hour: 10\npoll_minute: 0\n',
                                           encoding="utf-8")

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gw.db")
    monkeypatch.setattr(rt, "Bot", _no_bot)
    settings.init(tmp_path)
    try:
        with pytest.raises(_Stop):
            await rt.run_gateway()
        assert settings.get("updates.poll_schedule") == "month"
        assert "never" not in (tmp_path / "updates.yaml").read_text(encoding="utf-8")
        d = Database(tmp_path / "gw.db")
        try:
            assert GatewayServices(d).updates_muted(), "уведомления при «никогда» не выключены"
        finally:
            d.close()
    finally:
        restore_settings()


async def test_an_ordinary_schedule_is_left_alone_at_agent_start(tmp_path, monkeypatch):
    """«день» при старте не трогается и уведомления не глушатся — починка
    только для «никогда»."""
    from awgbot.runtime import main as rt
    from tests.conftest import restore_settings
    (tmp_path / "updates.yaml").write_text('poll_schedule: "day"\npoll_hour: 10\npoll_minute: 0\n',
                                           encoding="utf-8")

    class _Stop(Exception):
        pass

    def _no_bot(*a, **k):
        raise _Stop()
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "gw.db")
    monkeypatch.setattr(rt, "Bot", _no_bot)
    settings.init(tmp_path)
    try:
        with pytest.raises(_Stop):
            await rt.run_gateway()
        assert settings.get("updates.poll_schedule") == "day"
        d = Database(tmp_path / "gw.db")
        try:
            assert not GatewayServices(d).updates_muted()
        finally:
            d.close()
    finally:
        restore_settings()


# ── мелочи, которые люди видят каждый день ───────────────────────────────────

async def test_recovery_from_the_health_screen_cancels_back_to_health(svc, fake_bot, monkeypatch):
    """«🔧 Восстановить» со здоровья — «Отмена» возвращает на здоровье, а не
    на панель: человек читал проблемы и передумал — пусть видит их дальше."""
    svc.live = GwStatus(checks=[GwCheck("обвязка", False, "нет цепочки")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    go = GwCB.unpack(_button(_last_edit(nav)[1], "🔧 Восстановить").callback_data)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, go, svc)
    cancel = GwCB.unpack(_button(_last_edit(nav)[1], "⬅️ Отмена").callback_data)
    assert cancel.action == "health", cancel
    # с панели — на панель
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action="reassert"), svc)
    assert GwCB.unpack(_button(_last_edit(nav)[1], "⬅️ Отмена").callback_data).action == "panel"


async def test_an_unknown_router_tab_falls_back_to_mikrotik(svc, fake_bot, monkeypatch):
    """Вкладка из чужого или старого колбэка — рецепт MikroTik, а не пустой
    экран без отмеченной вкладки."""
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "naspi")
    monkeypatch.setattr(svc, "lan_router_params", lambda: ("192.168.1.0/24", "192.168.1.2", []))
    cb, nav = _acb(fake_bot)
    await gh.gw_transit_router(cb, GwCB(action="lan_router", val="zz"), svc)
    text, markup = _last_edit(nav)
    assert _rows(markup)[0] == ["✅ MikroTik", "OpenWrt"], _rows(markup)
    assert "/ip route add" in text, text


async def test_a_huge_domain_input_result_still_fits_one_message(svc, fake_bot, monkeypatch):
    """Вставили сотни доменов — итог первыми строками экрана обрезан так,
    чтобы вместе с экраном влезть в одно сообщение (4096 видимых знаков), с
    хвостом «…и ещё N строк»; иначе Telegram отвергает сообщение и человек
    остаётся без экрана."""
    import html
    import re
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, lan={
        "iface": "eth0", "addr": "192.168.1.10", "domains": 1, "nets": 1, "lan_pkts": 1}))
    doms = [f"very-long-subdomain-name-{i:04d}.example-shop.com" for i in range(400)]
    monkeypatch.setattr(svc, "lan_own_lists", lambda: [("vpn", d) for d in doms[:10]])
    monkeypatch.setattr(svc, "lan_domains", lambda cmd, domains: (True, "\n".join(f"<code>{d}</code>: добавлен" for d in domains)))
    monkeypatch.setattr(svc, "own_active", lambda: False)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_transit_ask(cb, GwCB(action="lan_add"), svc, st)
    reply = _msg(fake_bot, " ".join(doms))
    await gh.gw_transit_domain_received(reply, st, svc)
    text = next(s[1] for s in reply.sent if s[0] == "answer")
    visible = html.unescape(re.sub(r"<[^>]+>", "", text))
    assert len(visible) <= 4096, len(visible)
    assert re.search(r"\n…и ещё \d+ строк", text), text[:300]
    assert "\n\n🔀 <b>VPN-транзит" in text, "экран под итогом потерялся"


@pytest.mark.parametrize("sync", ["online", "offline", "no_channel"])
def test_a_long_domain_does_not_eat_the_sync_tail_of_the_toast(sync):
    """Домен длиной в сотню знаков обрезается сам, а хвост синхронизации
    остаётся целиком: всплывашка — до 200 знаков, и Telegram режет хвост
    первым — как раз ту строку, ради которой её читают."""
    dom = "a" * 150 + ".example.com"
    toast = texts.gateway_transit_removed_toast(dom, sync=sync)
    assert len(toast) <= 200, len(toast)
    assert toast.endswith("\n" + texts.SYNC_TAILS[sync]), toast
    assert "Убран на всех шлюзах" not in toast, "первая строка «Убран на всех шлюзах» снята"
    assert texts.gateway_transit_removed_toast("sber.ru") == "sber.ru: убран"
    assert texts.gateway_transit_removed_toast("sber.ru", True, "") == "sber.ru: убран", (
        "shared больше ничего не добавляет — хвост решает только sync")


def test_the_monitoring_text_declines_the_streak(store):
    store.update({"app.monitoring.alert_streak": 1, "app.gateway.monitor_minutes": 3,
                  "app.gateway.handshake_max_age": 300, "app.gateway.link_alert_loud": False})
    text = texts.gw_settings_mon_text()
    assert "алерт после 1 плохого замера ·" in text and "по правилам тихих часов" in text, text
    store["app.monitoring.alert_streak"] = 5
    assert "алерт после 5 плохих замеров ·" in texts.gw_settings_mon_text()


def test_many_added_addresses_are_capped_in_the_result():
    """Вставили два десятка адресов — итог перечисляет двенадцать и «и ещё K»,
    а не простыню, выталкивающую раздел за экран."""
    added = [f"198.51.100.{i}" for i in range(1, 16)]
    text = texts.gateway_ssh_allow_added(added)
    assert text.count("<code>") == 12 and text.endswith(" и ещё 3"), text


def test_free_disk_space_is_tenths_when_small_and_grouped_when_large():
    st = GwStatus(disk_free_gb=0.4)
    assert "диск свободно 0.4 ГБ" in texts.gateway_health(st).splitlines()
    st = GwStatus(disk_free_gb=1234.4, smart="OK")
    assert "диск свободно 1 234 ГБ, SMART OK" in texts.gateway_health(st).splitlines()


@pytest.mark.parametrize("age, limit, line", [
    (40, 300, "📡 Линк до сервера AWG 🟢 40 с"),
    (720, 300, "📡 Линк до сервера AWG 🟡 12 мин"),
    (720, 900, "📡 Линк до сервера AWG 🟢 12 мин"),
    (3 * 3600, 300, "📡 Линк до сервера AWG 🟡 3 ч"),
])
def test_the_link_line_turns_yellow_by_the_configured_threshold(store, age, limit, line):
    """🟡 — по тому же порогу, по которому монитор шлёт алерт о молчании
    линка, а не по своим трём минутам: иначе панель желтеет раньше алерта или
    зеленеет, когда алерт уже пришёл."""
    store["app.gateway.handshake_max_age"] = limit
    assert texts.gateway_panel(GwStatus(link_up=True, handshake_age=float(age))).splitlines()[2] == line


def test_the_freshness_line_says_seconds_short(monkeypatch):
    from awgbot.util import timeutil
    import datetime
    st = GwStatus(link_up=True, handshake_age=5.0,
                  ts=timeutil.to_iso(timeutil.now() - datetime.timedelta(seconds=40)))
    assert texts.gateway_panel(st).splitlines()[-1] in ("<i>обновлено 40 с назад</i>", "<i>обновлено 41 с назад</i>")
