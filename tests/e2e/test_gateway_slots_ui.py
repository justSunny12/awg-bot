"""Экраны слотов шлюзов (docs/gateway-failover.md §6): раздел с одним и двумя
слотами, список, карточка, переключение в обе стороны, галочка
предпочтительного, пинг, подсети, подпись, добавление второго слота с токеном,
убрать резервный и активный."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot.callbacks import DeviceCB, GwMarkCB, GwSlotCB, SetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
PRIV = base64.b64encode(os.urandom(32)).decode()


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _screen(nav):
    return next((s[1], _labels(s[2])) for s in reversed(nav.sent) if s[0] == "edit_text")


@pytest.fixture()
def slots(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    runs = []
    monkeypatch.setattr(services, "_link_privkey", lambda gw=None: PRIV)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: runs.append((mode, dict(env or {}))))
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda slot=None: (b"ENC", f"b{slot}.enc"))
    monkeypatch.setattr(services, "gw_bundle_plain", lambda slot=None: (b"PLAIN", f"awg-gw-bundle{slot}.sh"))
    monkeypatch.setattr(settings, "set_value", lambda k, v: [k])
    token = {}
    monkeypatch.setattr(services, "gw_bot_token", lambda slot=None: token.get(slot or 1, ""))
    monkeypatch.setattr(services, "set_gw_bot_token", lambda t, slot=None: token.__setitem__(slot or 1, t))
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True if k in ("app.routing.enabled", "app.routing.failover.enabled") else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    monkeypatch.setattr(services, "routing_link_ok", lambda: True)
    probe = {1: "ok", 2: "ok"}
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: probe[g.id])
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 0)
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)
    pings = {"n": 0}

    def _ping(iface="", **k):
        pings["n"] += 1
        return 43 if iface == "awglink2" else 61
    from awgbot.infra import routing as rt
    monkeypatch.setattr(rt, "ping_peer", _ping)
    monkeypatch.setattr(rt, "link_peer_endpoint", lambda iface="": "198.51.100.7" if iface == "awglink2" else "203.0.113.10")
    monkeypatch.setattr(rt, "switch_active", lambda iface: None)
    services.runs, services.probe, services.pings, services.token = runs, probe, pings, token
    return admin, services.db.get_device(pi.device_id), services.db.get_device(pi2.device_id)


def _slot1(services, dev):
    return services.db.gateway_add(dev.id, "awglink", 443, "10.99.99.0/30", slot_id=1)


def _slot2(services, dev):
    return services.db.gateway_add(dev.id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)


async def test_section_with_one_slot_offers_a_standby(services, slots):
    _, pi, pi2 = slots
    _slot1(services, pi)
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "🛰 Шлюз: NASPi" in labels and "➕ Резервный шлюз" in labels
    assert "Резервного шлюза нет" in text and "🟢 работает" in text


async def test_section_with_two_slots_lists_them(services, slots):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    text, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert "🛰 Шлюзы: 2" in labels and "➕ Резервный шлюз" not in labels
    assert "«NASPi»" in text and "🟢 <b>[Активен]</b>" in text and "«Pi2»" in text and "🟢 <b>[Резерв]</b>" in text


async def test_list_and_card_show_roles_preferred_and_ping_lazily(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом 2")
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, labels = _screen(nav)
    assert labels[0] == "⭐ NASPi" and labels[1] == "Pi2", "статус — в инфобоксе, не на кнопке"
    assert "«NASPi»" in text and "🟢 <b>[Активен]</b>" in text and "🟢 <b>[Резерв]</b>" in text
    assert "➕ Добавить шлюз" not in labels, "потолок два слота"
    assert "🔁 Автопереключение: вкл" in labels
    assert "Предпочтительный при холодном старте: «NASPi»" in text and "дом 2" in text
    # карточка резерва: пинг измерен лениво при первом открытии, кнопка последней перед «Назад»
    assert services.pings["n"] == 0
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    text, labels = _screen(nav)
    assert services.pings["n"] == 1 and "Пинг с " in text and "43 мс" in text
    assert "↗️ Внешний IP: <code>198.51.100.7</code>" in text
    assert "<b>[Резерв]</b>" in text and "Трафик сейчас идёт" not in text, "лишних подсказок в карточке нет"
    assert labels[0] == "▶️ Переключить трафик сюда" and labels[1].startswith("☑️ Предпочтительный")
    assert labels[-2] == "📡 Пинг" and labels[-1] == "⬅️ Назад"
    # второе открытие — из кэша
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert services.pings["n"] == 1
    # кнопка пинга меряет заново
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert services.pings["n"] == 2 and cb.answers[0][0].startswith("Пинг с ") and cb.answers[0][0].endswith("43 мс")
    # карточка активного: без кнопки переключения, галочка стоит
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    text, labels = _screen(nav)
    assert "<b>[Активен]</b>" in text and "Переключить трафик сюда" not in labels
    assert labels[0].startswith("✅ Предпочтительный")


async def test_preferred_toggle_moves_and_clears(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.db.gateway(2).preferred == 1 and services.db.gateway(1).preferred == 0
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.preferred_gateway() is None
    _, labels = _screen(nav)
    assert labels[1].startswith("☑️ Предпочтительный")


async def test_manual_switch_both_ways_with_confirmation(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2), services)
    text, labels = _screen(nav)
    assert "Переключить трафик на «Pi2»?" in text and "▶️ Да, переключить" in labels and "⬅️ Отмена" in labels
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=2), services)
    assert services.active_gateway().id == 2 and "Pi2" in cb.answers[0][0]
    # обратно — из карточки первого, который теперь в резерве
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    _, labels = _screen(nav)
    assert labels[0] == "▶️ Переключить трафик сюда"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=1), services)
    assert services.active_gateway().id == 1
    # лежащий резерв — предупреждение и «Всё равно»
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2), services)
    text, labels = _screen(nav)
    assert "не отвечает уже" in text and "▶️ Всё равно переключить" in labels


async def test_home_subnets_and_label_inputs(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_home(cb, GwSlotCB(action="home", slot=1), services, st)
    text, labels = _screen(nav)
    assert "Домашние подсети «NASPi»" in text and labels == ["✖️ Отмена"]
    msg = _amsg(fake_bot, "192.168.1.0/24 мусор")
    await sh.gateway_home_received(msg, st, services)
    assert services.db.gateway(1).home_subnets == ["192.168.1.0/24"]
    report = next(s[1] for s in msg.sent if s[0] == "answer")
    assert "192.168.1.0/24" in report and "мусор" in report and "не похоже" in report
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    msg = _amsg(fake_bot, "дом 2")
    await sh.gateway_label_received(msg, st, services)
    assert services.db.gateway(2).label == "дом 2"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    text, _ = _screen(nav)
    assert "🏠 Домашние подсети: 192.168.1.0/24" in text


async def test_add_second_slot_as_new_machine_asks_its_own_token(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    text, markup = await sh._screen("rt_gw", services)
    assert "Резервный шлюз" in text and _labels(markup)[:2] == ["📱 Из моих устройств", "➕ Новое устройство"]
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, st)
    text, _ = _screen(nav)
    assert "Токен бота резервного шлюза" in text, "у второго шлюза свой бот"
    msg = _amsg(fake_bot, "222222222:BB-second-token-value-long-enough")
    await sh.gateway_token_received(msg, st, services)
    assert services.token[2].startswith("222222222:")
    gws = services.db.gateways()
    assert [g.id for g in gws] == [1, 2] and services.db.get_device(gws[1].device_id).name == "Шлюз 2"
    assert services.runs[-1][0] == "--apply" and services.runs[-1][1]["LINK_IF"] == "awglink2"
    docs = [s for s in msg.sent if s[0] == "document"]
    assert len(docs) == 1 and "первого применения" in docs[0][1]
    instr = next(s[1] for s in msg.sent if s[0] == "answer" and "--role gateway" in s[1])
    assert "awg-gw-bundle-awglink2.sh" in instr


async def test_add_second_slot_from_my_devices(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = "222222222:BB-second-token-value-long-enough"
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick_list(cb, GwMarkCB(action="pick_list", slot=0), services)
    _, labels = _screen(nav)
    assert any("Pi2" in l for l in labels) and not any("NASPi" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi2.id, slot=0), services)
    text, _ = _screen(nav)
    assert "Слот резервный" in text
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == 2
    assert any(s[0] == "document" and "первого применения" in s[1] for s in nav.sent)


async def test_remove_standby_and_active(services, slots, fake_bot, monkeypatch):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_ask(cb, GwSlotCB(action="remove_ask", slot=2), services)
    text, labels = _screen(nav)
    assert "перестанет быть резервным шлюзом" in text and "🛑 Да, снять шлюз" in labels
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    assert [g.id for g in services.db.gateways()] == [1] and services.runs[-1][0] == "--rollback"
    text, _ = _screen(nav)
    assert "линк слота снят" in text and "«NASPi»" in text
    # снова два, убираем активный — трафик на второй
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_ask(cb, GwSlotCB(action="remove_ask", slot=1), services)
    text, _ = _screen(nav)
    assert "перейдёт на «Pi2» прямо сейчас" in text
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=1), services)
    assert services.active_gateway().id == 2 and services.runs[-1][0] == "--rekey"
    # «Не шлюз?» из карточки устройства ведёт в то же подтверждение
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi2.id), services)
    text, labels = _screen(nav)
    assert "<b>[Активен]</b> — несёт трафик РФ-доступа" in text and "Пинг с " in text and "Внешний IP" in text
    assert "🛰 Карточка шлюза" in labels and labels[-2] == "📡 Пинг"
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_ask(cb, GwMarkCB(action="remove_ask", device_id=pi2.id), services)
    text, _ = _screen(nav)
    assert "условная маршрутизация выключится" in text


async def test_bundle_screen_and_action_are_per_slot(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом 2")
    text, markup = await sh._screen("rt_bundle", services, "2")
    assert "Конфигурация шлюза «Pi2» (дом 2)" in text
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert SetCB(sec="rt", act="do", key="bundle", val="2").pack() in datas
    assert GwSlotCB(action="card", slot=2).pack() in datas
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle", val="2"), services)
    docs = [s for s in nav.sent if s[0] == "document"]
    assert docs and docs[0][1] is not None


async def test_monitoring_screen_edits_probe_window_and_threshold(services, slots, fake_bot, monkeypatch):
    """⚙️ → Условная маршрутизация → «📡 Мониторинг и резервирование» последним
    перед «Назад»: такт зонда, окно и порог — пикерами, автопереключение —
    тумблером; значения горячие."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.delattr(services, "_rt_window_size")      # фикстура прибила окно — здесь оно из настроек
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    real_int = settings.get_int
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: store.get(k, real_int(k, d)))
    _, markup = await sh._screen("rt", services)
    labels = _labels(markup)
    assert labels[-2] == "📡 Мониторинг и резервирование" and labels[-1] == "⬅️ Назад"
    text, markup = await sh._screen("rt_mon", services)
    labels = _labels(markup)
    assert "такт 30 с" in " ".join(labels) and "🔘 такт 30 с" in labels and "🔘 окно 10" in labels and "🔘 порог 50 %" in labels
    assert "5 неуспешных из 10" in text and "≈ 5 мин" in text
    assert any(l.endswith("Автопереключение на резерв") for l in labels)
    cb, nav = _acb(fake_bot)
    await sh.pick(cb, SetCB(sec="rt", act="pick", key="window", val="20"), services)
    assert store["app.routing.failover.window_samples"] == 20
    text, labels = _screen(nav)
    assert "🔘 окно 20" in labels and "10 неуспешных из 20" in text and "≈ 10 мин" in text
    cb, nav = _acb(fake_bot)
    await sh.pick(cb, SetCB(sec="rt", act="pick", key="avail", val="75"), services)
    assert store["app.routing.failover.min_availability"] == 75
    text, _ = _screen(nav)
    assert "5 неуспешных из 20" in text
    cb, nav = _acb(fake_bot)
    await sh.pick(cb, SetCB(sec="rt", act="pick", key="probe", val="60"), services)
    assert store["app.routing.probe_seconds"] == 60
    text, _ = _screen(nav)
    assert "каждые 60 с" in text and "≈ 20 мин" in text
    cb, nav = _acb(fake_bot)
    await sh.pick(cb, SetCB(sec="rt", act="pick", key="probe", val="7"), services)
    assert cb.answers and "Нет такого варианта" in cb.answers[0][0]


async def test_single_gateway_card_has_no_preferred_toggle(services, slots, fake_bot):
    """С единственным шлюзом выбирать предпочтительного не из чего: ни кнопки,
    ни строки; со вторым слотом галочка появляется."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    text, labels = _screen(nav)
    assert not any("Предпочтительный" in l for l in labels) and "Предпочтительн" not in text
    assert labels[-1] == "⬅️ Назад" and labels[-2] == "📡 Пинг"
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    _, labels = _screen(nav)
    assert any(l.startswith("✅ Предпочтительный") for l in labels)
