"""
Экраны канала до шлюза: строка «🔗 Упр. канал …» в карточке слота и
предупреждения открытыми строками под ней — вместе со сверкой выданного с
установленным. Спрашивать шлюз по кнопке (свежий снимок, диагностика) карточка
больше не умеет — показывает то, что шлюз прислал сам. Карточка слота во всех
состояниях канала (жив, лежит с возрастом снимка, расхождения, отказ шлюза,
обвязка, поколение, связь подсетей, часы, выход наружу, подсети соседей) и
диалоги тумблеров при живом и лежащем канале — в эталоне
tests/screens/admin.txt (adm.gw.card.ch.*, adm.gw.lan_*.ch.*,
adm.gw.peer_*.ch.*), строка канала в панели агента — в gateway.txt; здесь —
что меняется в слоте и настройках, напоминания о перевыпуске и строки канала
на входе функций.

Экрана «Конфигурация шлюза» перед выпуском файла больше нет (вычитка 3.1.0):
файл выпускается из карточки сразу. Всё, что человеку нужно для решения,
переехало в карточку — пункты расхождения при лежащем канале (с возрастом
снимка), обвязка старого образца или не развёрнутая, подсети соседей. Чего
экран показывал сверх этого — «совпадает с тем, что выдаст этот файл»,
строка «Контракт линка», подсказки «файл для них не нужен» / «дождись
канала» — не показывается больше нигде, и тесты на это сняты: карточка
говорит то же короче («✅ актуальна», «⏳ уходят каналом»).

Всё, что рисуется здесь, приехало с чужой машины: экран обязан говорить
«последнее известное, N назад», когда канал лежит, молчать, когда шлюз ещё
ничего не сообщал. Цена ошибки — человек чинит несуществующее расхождение или, наоборот,
уверен, что конфигурация доехала.
"""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _screen(nav):
    s = next(x for x in reversed(nav.sent) if x[0] == "edit_text")
    labels = [b.text for row in s[2].inline_keyboard for b in row] if s[2] else []
    return s[1], labels


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Слот шлюза с включённой маршрутизацией и пустым каналом."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_link_privkey", lambda g=None: PRIV)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")   # возраст хендшейка снимается
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: True if k in ("app.routing.enabled",
                                                         "app.routing.failover.enabled") else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "routing_link_ok", lambda: True)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "ok")
    pings = []
    monkeypatch.setattr(services, "gateway_ping", lambda slot_id: pings.append(slot_id) or 61)
    services.pings = pings
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    return services.db.gateway(1)


def _installed(services) -> dict:
    """Что стоит на шлюзе, когда всё доехало: ровно те значения, из которых ВПС
    собирает бандл."""
    addrs = " ".join(sorted(set(services.db.admin_device_addresses(ADMIN))))
    return {"lan_mode": "1", "home_subnets": "192.168.68.0/24", "resolver": "10.9.1.1",
            "peer_home_nets": "", "admin_ips": addrs}


def _snap(services, *, bundle=None, online=True, **kw):
    """Положить снимок так, как его принял бы канал, и при желании открыть сессию."""
    snap = {"bundle": bundle if bundle is not None else _installed(services),
            "link_contract": "1", "plumbing_gen": "new", "mark_status": "confirmed",
            "agent_version": "3.1.0", "awg_generation": 1, "egress_ok": True,
            "boot_id": "b" * 36, "ts": "2026-09-22T20:00:00+03:00", "rev": 1}
    snap.update(kw)
    services.gwlink_snapshot_in(1, snap, 1, True)
    if online:
        services.gwlink_session_opened(1, "3.1.0", 1)
    return snap


async def _card(services, fake_bot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    return _screen(nav)


# ── карточка слота ───────────────────────────────────────────────────────────

async def test_a_live_channel_does_not_replace_the_link_ping(services, slot, fake_bot):
    """Канал жив, а пинг в карточке меряется по линку, как и без канала. Сама
    карточка с живым каналом (одна строка, выход наружу под «подробнее», без
    кнопок запроса к шлюзу) — эталон adm.gw.card.ch.live."""
    _snap(services)
    await _card(services, fake_bot)
    assert services.pings == [1], (
        "пинг меряет путь ядро ↔ ядро, отклик канала — занятость процесса агента; "
        "подменить первое вторым значило бы врать в карточке")


# ── напоминания о перевыпуске ────────────────────────────────────────────────

def test_a_matching_installed_configuration_silences_the_reissue_reminder(services, slot):
    """Раньше ВПС сравнивал своё со своим — что выдал тогда с тем, что выдал бы
    сейчас, — и напоминал о перевыпуске даже тогда, когда человек уже применил
    файл другим путём. С каналом напоминание перестаёт быть догадкой: на шлюзе
    стоит то же самое — молчим."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_SSH_KEY, 1), "")
    assert services.gw_bundle_drift_notes(), "без снимка напоминание работает как раньше"

    _snap(services)
    assert services.gw_bundle_drift_notes() == [], (
        "на шлюзе стоит ровно выдаваемое, а бот всё равно гонит перевыпускать")


def test_a_drifted_installed_configuration_still_asks_for_a_reissue(services, slot):
    """Обратная сторона: снимок есть, установленное не совпадает, а канал молчит
    дольше суток — ждать его больше нечего. Возвращается прежний путь: одно
    тихое напоминание перевыпустить файл, и не больше одного на расхождение."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"}, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_seen_1", "2026-01-01T00:00:00+03:00")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text
    assert services.gw_bundle_drift_notes() == [], "напоминание повторилось на то же расхождение"


def test_a_live_channel_silences_the_reissue_reminder_it_will_deliver_itself(services, slot):
    """Канал жив — он довезёт расхождение сам. Напоминание «перевыпусти» тогда
    шум: человек перевыпустит, а канал довёз бы то же самое."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_SSH_KEY, 1), "")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"})
    assert services.gw_bundle_drift_notes() == []


def test_a_briefly_silent_channel_still_covers_the_delivery(services, slot):
    """Канал лёг пару часов назад (ребут малины, линк моргнул) — он поднимется и
    довезёт. Напоминать о перевыпуске после каждого моргания линка — шум."""
    import datetime
    from awgbot.util import timeutil
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    _snap(services, bundle={**_installed(services), "home_subnets": "192.168.1.0/24"}, online=False)
    services.gwlink_session_closed(1)
    services.db.set_state("gwlink_seen_1", timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=2)))
    assert services.gw_bundle_drift_notes() == [], "канал молчит два часа, а бот уже гонит перевыпускать"
    services.db.set_state("gwlink_seen_1", timeutil.to_iso(timeutil.now() - datetime.timedelta(hours=25)))
    assert len(services.gw_bundle_drift_notes()) == 1, "канал молчит сутки — напоминание обязано вернуться"


def test_a_channel_that_never_came_up_does_not_silence_the_reminder(services, slot):
    """Канал не поднимался ни разу (старый агент, выключен бандлом) — доставлять
    нечем, и напоминание работает как до канала."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1), "lan=0;nets=;resolver=;peer=")
    assert len(services.gw_bundle_drift_notes()) == 1


# ── этап 4: тумблеры при живом канале ────────────────────────────────────────

async def test_the_lan_mode_turns_off_with_a_live_channel(
        services, slot, fake_bot):
    """Канал доставит смену режима сам — гнать человека перевыпускать файл значит
    заставить его делать руками то, что уже едет (вопрос и всплывашка —
    эталоны adm.gw.lan_ask.ch.live, adm.gw.lan_yes.ch.live). Режим снят."""
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1, "вопрос уже выключил режим"
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


async def test_the_lan_mode_turns_off_without_a_channel(
        services, slot, fake_bot):
    """Канала нет — режим всё равно снимается, а вопрос и всплывашка говорят
    о перевыпуске (эталоны adm.gw.lan_ask.ch.dead, adm.gw.lan_yes.ch.dead)."""
    _snap(services, online=False)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


def _peer_store(monkeypatch, on=False):
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": on}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    return store


async def test_the_peer_toggle_turns_on_with_a_live_channel(
        services, slot, fake_bot, monkeypatch):
    """Подсети соседей живут на шлюзе ещё и в конфиге линка, а его везёт только
    файл. Пообещай диалог «применят сами» — человек не перевыпустит, и доступ
    между подсетями не заработает при зелёном канале (вопрос и всплывашка —
    эталоны adm.gw.peer_ask.ch.live, adm.gw.peer_yes.ch.live)."""
    store = _peer_store(monkeypatch)
    _snap(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    assert store["app.routing.peer_nets.enabled"] is False, "вопрос уже включил связь"
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes"), services)
    assert store["app.routing.peer_nets.enabled"] is True, "связь подсетей не включена"


def _neighbour(services, monkeypatch):
    """Второй шлюз с режимом без VPN и включённый доступ между подсетями: у
    слота 1 появляются подсети соседа, которых на нём ещё нет."""
    _peer_store(monkeypatch, on=True)
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gateway_set_home_subnets(2, "192.168.70.0/24")
    services.gateway_set_lan_mode(2, True)
    assert services.gateway_peer_nets(1) == ["192.168.70.0/24"], "сценарий собран не так"


def test_neighbour_subnets_are_not_sent_over_the_channel(services, slot, monkeypatch):
    """Включили доступ между подсетями, канал жив, конфигурацию не перевыпустили.
    Канал подсети соседей не везёт (они живут и в конфиге линка) — доставлять
    ему нечего; карточка поэтому не обещает «уходят каналом» (эталоны
    adm.gw.card.ch.neighbour.*)."""
    _neighbour(services, monkeypatch)
    _snap(services)                                     # на шлюзе подсетей соседей ещё нет
    assert services.gwlink_settings_due(services.db.gateway(1)) is None, (
        "подсети соседей ушли бы каналом, а их везёт только файл")


def test_a_live_channel_does_not_silence_the_reminder_about_neighbour_subnets(
        services, slot, monkeypatch):
    """Напоминание перевыпустить файл молчит, пока канал довезёт всё сам. Подсети
    соседей он не довезёт — значит напоминание о них обязано прийти, один раз."""
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(services.db.gateway(1)))
    _snap(services)
    _neighbour(services, monkeypatch)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text, "канал жив — и о подсетях соседей молчок"
    assert services.gw_bundle_drift_notes() == [], "одно напоминание на расхождение"


# ── строки канала на входе функций: экранирование и пределы ──────────────────

def test_the_neighbour_subnets_line_escapes_what_the_gateway_sent():
    """Имена подсетей приехали с чужой машины: разметка в них ломает сообщение
    целиком, и карточка не рисуется вовсе."""
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": ["<b>1.2.3.0/24</b>", "&x"]}}
    out = _channel_block(ch, True)
    assert "<b>1.2.3.0/24</b>" not in out
    assert "<code>&lt;b&gt;1.2.3.0/24&lt;/b&gt;</code>, <code>&amp;x</code>" in out


def test_the_neighbour_subnets_line_names_at_most_eight_and_survives_an_empty_list():
    many = [f"192.168.{i}.0/24" for i in range(12)]
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True,
          "peer_nets": {"ok": False, "missing": many}}
    out = _channel_block(ch, True)
    assert "192.168.7.0/24" in out and "192.168.8.0/24" not in out, "список не ограничен восемью"
    ch["peer_nets"] = {"ok": False, "missing": ["1" * 40]}
    assert "1" * 18 in _channel_block(ch, True) and "1" * 19 not in _channel_block(ch, True), \
        "элемент списка с малины не ограничен"
    ch["peer_nets"] = {"ok": False, "missing": []}
    assert "⚠️ Связь подсетей: на шлюзе нет подсетей — " in _channel_block(ch, True), (
        "пустой список отказа — строка без предмета")


@pytest.mark.parametrize("skew, side", [(600, "спешат"), (-3600, "отстают")])
def test_the_clock_line_warns_about_the_clock_not_about_the_channel(skew, side):
    """Канал от часов больше не зависит: строка, обещающая, что «канал
    перестанет принимать сообщения», послала бы человека чинить то, что не
    сломается, и промолчала бы о том, что сломается — TLS и расписания."""
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": skew}
    out = _channel_block(ch, True)
    line = next((x for x in out.splitlines() if x.startswith("⏱ Часы шлюза")), "")
    assert line, f"расхождение {skew} с не показано"
    assert f"{side} на {abs(skew) // 60} мин" in line
    assert "синхронизацию" in line, "не сказано, что делать"
    assert "перестанет принимать" not in line and "канал" not in line, (
        f"строка по-прежнему пугает отказом канала: {line}")


def test_a_small_clock_drift_draws_nothing():
    ch = {"ever": True, "online": True, "has_snap": True, "has_bundle": True, "clock_skew": 119}
    assert "Часы шлюза" not in _channel_block(ch, True), "дрожь в пару минут подана как проблема"


def _channel_block(ch, server_ok):
    """Строка канала и предупреждения одним блоком — как их склеивает карточка."""
    from awgbot.bot.texts.routing import channel_lines
    head, warns, _egress = channel_lines(ch, server_ok)
    return "\n".join([head] + warns) if head else ""


def test_the_egress_line_names_the_snapshot_age_when_the_channel_is_down():
    """Шлюз мёртв вторую неделю, а карточка хранит последний снимок: «выход
    наружу» без оговорки читался как живой статус."""
    from awgbot.bot.texts.routing import channel_lines
    ch = {"ever": True, "online": False, "seen": "", "age": 13 * 86400, "has_snap": True, "has_bundle": True,
          "egress_gw": True}
    _, _, egress = channel_lines(ch, False)
    assert egress.endswith("(по снимку 13 дн назад)"), egress
    ch["online"] = True
    _, _, egress = channel_lines(ch, False)
    assert "по снимку" not in egress, egress
