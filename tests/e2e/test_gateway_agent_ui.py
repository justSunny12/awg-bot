"""E2E: агент шлюза — то, чего нет в эталонах экранов (tests/screens/gateway.txt):
ветки панели в редких состояниях (целые сутки аплинка, тег без «v», /start с
новой версией, без своих доменов, без связи подсетей), живая проба здоровья,
реассерт только после подтверждения, порог линка в секундах, «никогда» из
старого конфига при старте агента, обрезка длинных итогов и всплывашек.

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
from awgbot.bot import sections, texts
from awgbot.bot.roles import GATEWAY
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


# ── панель ───────────────────────────────────────────────────────────────────

async def _panel(svc, fake_bot, st: GwStatus):
    """Панель «В меню» из снимка тика — ровно таким, каким его дали."""
    svc.cached_status = lambda max_age: st
    cb, nav = _acb(fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    return _last_edit(nav)


async def test_the_panel_head_drops_zero_hours_and_a_missing_uptime(svc, fake_bot):
    """Ровно 12 суток аплинка — «12 дн», без «0 ч» (нули не выводим); аптайма
    в замере нет — шапка без хвоста «·», а не «· 0 мин»."""
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi", uptime_seconds=12 * 86400)
    text, _ = await _panel(svc, fake_bot, st)
    assert text.splitlines()[0] == "🛰 <b>naspi</b> 🟢 линк поднят · 12 дн", text
    text, _ = await _panel(svc, fake_bot, GwStatus(hostname="naspi"))
    assert text.splitlines()[0] == "🛰 <b>naspi</b> 🔴 линк лежит", text


async def test_a_tag_without_v_is_shown_with_v(svc, fake_bot):
    """Тег последней проверки записан без «v» — на панели всё равно «v3.2.0»,
    как в разделе обновлений и в журнале: разные написания одной версии
    читаются как две разные."""
    svc.db.set_state("update_available_tag", "3.2.0")
    st = GwStatus(link_up=True, handshake_age=40.0, hostname="naspi")
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert "<b>⬆️ Доступна v3.2.0</b>" in lines, lines


async def test_start_shows_the_new_version_line_too(svc, fake_bot, monkeypatch):
    """Строка «⬆️ Доступна» — не только на «В меню»: /start рисует ту же
    панель из снимка, иначе после перезапуска агента новость молчит."""
    svc.db.set_state("update_available_tag", "v3.2.0")
    monkeypatch.setattr(svc, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    msg = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await gh.gw_start(msg, svc, FakeState())
    assert "<b>⬆️ Доступна v3.2.0</b>" in msg.sent[-1][1].splitlines(), msg.sent[-1][1]


async def test_no_own_domains_means_no_own_tail_on_the_panel(svc, fake_bot):
    """Своих доменов нет — хвост «· свои: пусто» в строке «📋 Списки» панели
    не рисуется (нули не выводим)."""
    lan = {"iface": "eth0", "addr": "192.168.1.10", "resolver": "10.8.0.1", "domains": 41200, "nets": 12,
           "updated_at": "", "own_vpn": 0, "own_ru": 0, "lan_pkts": 5}
    st = GwStatus(link_up=True, handshake_age=40.0, lan=lan)
    lines = (await _panel(svc, fake_bot, st))[0].splitlines()
    assert "📋 Списки: 41 200 доменов, 12 подсетей" in lines, lines
    assert not any("свои" in ln for ln in lines), lines


async def test_the_smb_line_is_on_the_panel_only_with_subnet_link(svc, fake_bot):
    """«🗂» — только при включённой связи подсетей (блок svc активен)."""
    lan = {"iface": "eth0", "addr": "192.168.1.10", "domains": 1, "nets": 1, "lan_pkts": 1,
           "svc": {"active": False, "own": ["a"], "peer": ["b"], "ever": True}}
    text, _ = await _panel(svc, fake_bot, GwStatus(link_up=True, handshake_age=40.0, lan=lan))
    assert "🗂" not in text, text


# ── здоровье и восстановление ────────────────────────────────────────────────

async def test_health_is_a_live_probe_not_the_tick_snapshot(svc, fake_bot):
    """«🩺 Здоровье» — живой замер на каждое нажатие, а не снимок тика: человек
    жмёт его, когда что-то сломалось только что."""
    svc.cached_status = lambda max_age: pytest.fail("здоровье взято из снимка тика")
    svc.live = GwStatus(link_up=True, hostname="naspi",
                        checks=[GwCheck("линк", True, "хендшейк 40 с"), GwCheck("обвязка", False, "нет цепочки")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    assert svc.probes == 1, "живой замер не снят"
    await gh.gw_health(cb, svc)
    assert svc.probes == 2, "второе нажатие не сняло замер заново"
async def test_health_details_from_the_host_are_escaped(svc, fake_bot):
    """Имя проверки и подробность с хоста — с «<» и «&»: без экранирования
    Telegram отвергает сообщение, и человек не видит здоровья вовсе."""
    svc.live = GwStatus(checks=[GwCheck("<b>x</b>", False, "rc=1 & <i>")])
    cb, nav = _acb(fake_bot)
    await gh.gw_health(cb, svc)
    assert "🔴 &lt;b&gt;x&lt;/b&gt; — rc=1 &amp; &lt;i&gt;" in _last_edit(nav)[0].splitlines()


async def test_recovery_touches_nothing_until_confirmed(svc, fake_bot, monkeypatch):
    """«🔧 Восстановить» сам ничего не трогает — только вопрос; реассерт —
    после подтверждения и ровно один раз: промах пальцем не рвёт РФ-доступ."""
    calls = []
    monkeypatch.setattr(svc, "reassert", lambda: calls.append(1) or (True, ""))

    async def _poke(services):
        return None
    monkeypatch.setattr(linkclient, "poke", _poke)
    _carrying(svc)
    cb, nav = _acb(fake_bot)
    await gh.gw_confirm(cb, GwCB(action="reassert"), svc)
    assert calls == [], "реассерт до подтверждения"
    await gh.gw_execute(cb, GwCB(action="reassert!"), svc)
    assert calls == [1], calls


def _carrying(svc, role: str = "active", link_up: bool = True) -> None:
    """Сервер сказал роль, последний тик монитора видел линк живым или нет."""
    svc.set_link_role(role == "active", standby=False, name="NASPi")
    svc.cached_status = lambda max_age: GwStatus(link_up=link_up, handshake_age=40.0 if link_up else None,
                                                 hostname="naspi")


@pytest.mark.parametrize("role, link_up", [("standby", True), ("active", False)])
@pytest.mark.parametrize("action, text", [
    ("restart", "🔁 Перезапустить AWG?\nЛинк опустится и поднимется"),
    ("reassert", "🔧 Восстановить шлюз?\nЮнит переставит правила (маскарад, изоляция, метка) и "
                 "переподнимет линк"),
    ("botrestart", "🔁 Перезапустить бота?\nВернётся через несколько секунд; без влияния на пользователей"),
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


# ── настройки ────────────────────────────────────────────────────────────────

async def test_monitoring_link_threshold_is_minutes_on_screen_and_seconds_in_the_config(svc, fake_bot, store):
    """Ввод «7» в минутах пишет в секундный ключ 420 и закрывает ввод: записанные
    минутами 7 секунд — монитор кричит «линк мёртв» на каждой паузе."""
    store["app.gateway.handshake_max_age"] = 300
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.handshake_max_age"), svc, st)
    msg = _msg(fake_bot, "7")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 420, "ввод в минутах записан не секундами"
    assert len([s for s in msg.sent if s[0] == "answer"]) == 1, msg.sent
    assert await st.get_state() is None, "ввод остался открытым"


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


async def test_link_threshold_absent_in_the_config_is_written_in_seconds(svc, fake_bot, store):
    """Ключа в конфиге агента нет (секция закомментирована) — первый ввод
    «10» всё равно пишет секунды (600), а не минуты поверх умолчания."""
    assert settings.get("app.gateway.handshake_max_age") is None, "в образце конфига ключ появился — тест устарел"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await gh.gw_edit(cb, GwCB(action="edit", val="app.gateway.handshake_max_age"), svc, st)
    msg = _msg(fake_bot, "10")
    await gh.gw_receive_value(msg, st, svc)
    assert store["app.gateway.handshake_max_age"] == 600


def test_the_mon_keyboard_never_rounds_to_zero(store):
    """Порог меньше минуты из старого конфига — подпись «1 мин», а не «0 мин»:
    нулевой порог читается как «алерт на каждом замере»."""
    store["app.gateway.handshake_max_age"] = 30
    assert "⏳ Линк: 1 мин" in [b for r in _rows(sections.mon.keyboard(GATEWAY)) for b in r]


# ── 💾 бэкапы ────────────────────────────────────────────────────────────────

# ── ⬆️ обновления ────────────────────────────────────────────────────────────

@pytest.fixture()
def upd(svc, monkeypatch, store):
    """Проверка обновлений без сети: счётчик походов к списку релизов."""
    from awgbot.infra import updates
    # поколение ядра 0 — блокировка обновления (update_block_reason) не срабатывает
    scene = {"next": types.SimpleNamespace(tag="v3.2.1", body="- пункт", title="", awg_generation=lambda: 0),
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


async def test_a_failed_check_keeps_the_found_version(svc, fake_bot, upd):
    """Сбой проверки не стирает найденную раньше версию: иначе строка
    «⬆️ Доступна vX» на панели гаснет от каждого таймаута GitHub."""
    cb, nav = _acb(fake_bot)
    await gh.gw_updates_screen(cb, svc)
    upd["fail"] = True
    await gh.gw_updates_screen(cb, svc)
    assert upd["calls"] == 2, "проверка при открытии не сходила к списку релизов"
    assert svc.update_available_tag() == "v3.2.1", "сбой проверки стёр найденную версию"


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
    """«после 1 плохого замера», а не «1 плохих замеров»."""
    store.update({"app.monitoring.alert_streak": 1, "app.gateway.monitor_minutes": 3,
                  "app.gateway.handshake_max_age": 300, "app.gateway.link_alert_loud": False})
    text = texts.settings_mon_text(GATEWAY)
    assert "алерт после 1 плохого замера ·" in text, text


def test_many_added_addresses_are_capped_in_the_result():
    """Вставили два десятка адресов — итог перечисляет двенадцать и «и ещё K»,
    а не простыню, выталкивающую раздел за экран."""
    added = [f"198.51.100.{i}" for i in range(1, 16)]
    text = texts.gateway_ssh_allow_added(added)
    assert text.count("<code>") == 12 and text.endswith(" и ещё 3"), text


def test_free_disk_space_is_grouped_when_large():
    """Терабайтный диск — «1 234 ГБ» с разрядами, а не «1234.4»."""
    st = GwStatus(disk_free_gb=1234.4, smart="OK")
    assert "диск свободно 1 234 ГБ, SMART OK" in texts.gateway_health(st).splitlines()


@pytest.mark.parametrize("age, limit, line", [
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
    """Свежий снимок — «40 с назад», а не «0 мин назад»."""
    from awgbot.util import timeutil
    import datetime
    st = GwStatus(link_up=True, handshake_age=5.0,
                  ts=timeutil.to_iso(timeutil.now() - datetime.timedelta(seconds=40)))
    assert texts.gateway_panel(st).splitlines()[-1] in ("<i>обновлено 40 с назад</i>", "<i>обновлено 41 с назад</i>")
