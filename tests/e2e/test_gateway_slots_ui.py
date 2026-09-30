"""Экран «🛰 Шлюзы» и слоты: ни одного, один и два слота, выключено; карточка
слота, «✏️ Изменить», переключение в обе стороны, «⭐ При старте», пинг,
подсети, подпись, добавление второго слота с токеном, снятие резервного и
активного; «⚙️ Параметры» циклами, «🔀 VPN-транзит», рецепт роутера
вкладками, «↔️ Связь подсетей» во всех состояниях."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot import texts
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
    monkeypatch.setattr(rt.probes, "ping_peer", _ping)
    monkeypatch.setattr(rt.probes, "link_peer_endpoint", lambda iface="": "198.51.100.7" if iface == "awglink2" else "203.0.113.10")
    monkeypatch.setattr(rt.marking, "switch_active", lambda iface: None)
    services.runs, services.probe, services.pings, services.token = runs, probe, pings, token
    return admin, services.db.get_device(pi.device_id), services.db.get_device(pi2.device_id)


def _slot1(services, dev):
    return services.db.gateway_add(dev.id, "awglink", 443, "10.99.99.0/30", slot_id=1)


def _slot2(services, dev):
    return services.db.gateway_add(dev.id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)


def _settled(services, slots_n=2):
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()


def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


async def test_section_with_one_slot_offers_a_standby(services, slots):
    """Один шлюз: его кнопка и «➕ Резерв» рядом; строка о том, чем грозит
    отсутствие резерва; автопереключения и связи подсетей нет — не с чем."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    _settled(services)
    text, markup = await sh._screen("rt", services)
    rows = _rows(markup)
    assert rows[0][0].endswith("NASPi") and rows[0][1] == "➕ Резерв", rows
    assert rows[1:] == [["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]], rows
    lines = text.split("\n")
    assert lines[0] == "🛰 <b>Шлюзы</b> · 🇷🇺 РФ-доступ 🟢", lines
    assert lines[1].endswith("NASPi — 🟢 Активен"), lines
    assert "Резерва нет: упадёт NASPi — РФ-сервисы станут открываться с зарубежного адреса" in lines
    assert "↔️" not in text and "Автопереключение" not in text
    assert "<blockquote" not in text and "⭐ —" not in text, "«подробнее» и сноска про ⭐ — только при двух"


async def test_section_with_two_slots_lists_them(services, slots):
    """Два шлюза: строки слотов с ролями, кнопки слотов (⭐ у
    предпочтительного), переключение на резерв, тумблеры — только тут."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    text, markup = await sh._screen("rt", services)
    rows = _rows(markup)
    assert rows == [["⭐ NASPi", "Pi2"], ["▶️ Переключить на Pi2"], ["✅ Автопереключение", "☑️ Связь подсетей"],
                    ["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]], rows
    lines = text.split("\n")
    assert lines[1] == "⭐ NASPi — 🟢 Активен" and lines[2] == "Pi2 — 🟢 Резерв", lines
    assert "↔️ Связь подсетей: выключена" in lines
    assert lines[-1] == "⭐ — предпочтительный при холодном старте"
    assert "Резерва нет" not in text and "«" not in lines[1] + lines[2], "имена без кавычек"


async def test_list_and_card_show_roles_preferred_and_ping_lazily(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дача")
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, labels = _screen(nav)
    assert labels[0] == "⭐ NASPi" and labels[1] == "Pi2", "статус — в тексте, не на кнопке"
    assert "\nPi2 (дача) — 🟢 Резерв" in text, "подпись в скобках, без адреса и кавычек"
    # карточка резерва: пинг измерен лениво при первом открытии
    assert services.pings["n"] == 0
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    text, labels = _screen(nav)
    lines = text.split("\n")
    assert services.pings["n"] == 1
    assert lines[0] == "🛰 <b>Pi2 (дача)</b> — 🟢 Резерв", lines
    assert lines[1].startswith("📡 <code>awglink2:8443</code> · ") and lines[1].endswith(" · 43 мс"), lines
    assert lines[2] == "🌐 <code>198.51.100.7</code>", lines
    assert "🗺 подсети не заданы · 🔀 VPN-транзит ☑️" in lines
    assert labels == ["▶️ Сделать активным", "📤 Конфигурация", "📡 Пинг", "☑️ VPN-транзит", "🗺 Подсети",
                      "✏️ Изменить", "⬅️ Назад"], labels
    # второе открытие — из кэша
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert services.pings["n"] == 1
    # кнопка пинга меряет заново
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert services.pings["n"] == 2 and cb.answers[0][0].startswith("Пинг с ") and cb.answers[0][0].endswith("43 мс")
    # карточка активного: без «Сделать активным», ⭐ в заголовке
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    text, labels = _screen(nav)
    assert text.startswith("⭐ <b>NASPi</b> — 🟢 Активен\n"), text
    assert "▶️ Сделать активным" not in labels and labels[0] == "📤 Конфигурация"
    assert "предпочтительный при холодном старте" in text, "под «подробнее»"


async def test_card_never_has_more_than_eight_buttons(services, slots, fake_bot):
    """Карточка слота — не больше восьми кнопок в самом полном виде: резерв с
    VPN-транзитом (у него и «Сделать активным», и «❓ Роутер»)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_home_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(2, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert _rows(markup) == [["▶️ Сделать активным"], ["📤 Конфигурация", "📡 Пинг"],
                             ["✅ VPN-транзит", "🗺 Подсети"], ["❓ Роутер", "✏️ Изменить"], ["⬅️ Назад"]]
    assert sum(len(r) for r in markup.inline_keyboard) <= 8


async def test_preferred_toggle_moves_and_clears(services, slots, fake_bot):
    """«⭐ При старте» живёт в «✏️ Изменить»: у ☑️ — перенести на этот слот,
    у ✅ — снять; экран перерисовывается на месте."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=2), services, FakeState())
    text, labels = _screen(nav)
    assert text == "✏️ <b>Pi2</b> — изменить" and "⭐ При старте: ☑️" in labels
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.db.gateway(2).preferred == 1 and services.db.gateway(1).preferred == 0
    text, labels = _screen(nav)
    assert text == "✏️ <b>Pi2</b> — изменить" and "⭐ При старте: ✅" in labels
    assert cb.answers[-1][0] == "Предпочтительный: Pi2"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.preferred_gateway() is None
    _, labels = _screen(nav)
    assert "⭐ При старте: ☑️" in labels and cb.answers[-1][0] == "Предпочтительный: снят"


async def test_edit_screen_offers_name_label_replace_and_remove(services, slots, fake_bot):
    """«✏️ Изменить»: имя устройства, подпись, «⭐ При старте» (только при
    двух слотах), замена и снятие; «Назад» — в карточку."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=1), services, FakeState())
    text, _ = _screen(nav)
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert text == "✏️ <b>NASPi</b> — изменить"
    assert _rows(markup) == [["✏️ Имя", "✏️ Подпись"], ["🔁 Заменить", "🛑 Снять"], ["⬅️ Назад"]], _rows(markup)
    datas = [b.callback_data for r in markup.inline_keyboard for b in r]
    assert datas == [GwSlotCB(action="name", slot=1).pack(), GwSlotCB(action="label", slot=1).pack(),
                     SetCB(sec="rt_gw", act="open", key="1").pack(), GwSlotCB(action="remove_ask", slot=1).pack(),
                     GwSlotCB(action="card", slot=1).pack()]
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=1), services, FakeState())
    _, labels = _screen(nav)
    assert labels[2] == "⭐ При старте: ✅", labels


async def test_rename_from_the_edit_screen_returns_to_the_edit_screen(services, slots, fake_bot):
    """«✏️ Имя» — имя устройства-шлюза: после ввода — снова «✏️ Изменить» с
    итогом первой строкой, а не карточка обычного устройства; приглашение и
    ввод убраны."""
    from awgbot.bot.handlers.admin import devices as dh
    _, pi, _ = slots
    _slot1(services, pi)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    services.db.nav_touch(ADMIN, nav.message_id)
    await sh.gw_slot_name(cb, GwSlotCB(action="name", slot=1), services, st)
    prompt, labels = _screen(nav)
    assert "NASPi" in prompt and labels == ["✖️ Отмена"], (prompt, labels)
    msg = _amsg(fake_bot, "NAS")
    await dh.device_edit_name_apply(msg, services, st)
    assert services.db.get_device(pi.id).name == "NAS"
    shown = [s[1] for s in msg.sent if s[0] in ("answer", "edit_text")]
    assert shown and shown[-1] == "✅ Имя устройства: NASPi → NAS\n\n✏️ <b>NAS</b> — изменить", shown
    assert await st.get_state() is None


async def test_manual_switch_both_ways_with_confirmation(services, slots, fake_bot):
    """«▶️ Переключить на Pi2» с экрана «Шлюзы» → подтверждение («Отмена»
    первой) → трафик на Pi2; обратно — «▶️ Сделать активным» в карточке
    бывшего активного. Лежащий резерв — предупреждение и «Всё равно»."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    _, markup = await sh._screen("rt", services)
    switch = next(b for r in markup.inline_keyboard for b in r if b.text == "▶️ Переключить на Pi2")
    assert GwSlotCB.unpack(switch.callback_data) == GwSlotCB(action="switch_ask", slot=2, val="l")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2, val="l"), services)
    text, labels = _screen(nav)
    confirm = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert confirm.inline_keyboard[0][0].callback_data == GwSlotCB(action="list").pack(), \
        "со списка — «Отмена» обратно в «Шлюзы»"
    assert confirm.inline_keyboard[0][1].callback_data == GwSlotCB(action="switch_yes", slot=2, val="l").pack()
    assert text == ("▶️ Переключить трафик на Pi2?\nРФ-сервисы у всех начнут выходить с адреса этой сети — "
                    "приложения могут попросить войти заново. NASPi останется в резерве, обратно бот сам не вернёт")
    assert labels == ["⬅️ Отмена", "▶️ Переключить"]
    assert services.active_gateway().id == 1, "вопрос ничего не переключил"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=2, val="l"), services)
    assert services.active_gateway().id == 2 and cb.answers[0][0] == "Трафик идёт через Pi2"
    text, _ = _screen(nav)
    assert text.startswith("🛰 <b>Шлюзы</b> · ") and "\nPi2 — " in text and "Активен" in text.split("\n")[2], \
        "со списка — обратно на «Шлюзы», Pi2 теперь активный"
    # обратно — из карточки первого, который теперь в резерве
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    _, labels = _screen(nav)
    assert labels[0] == "▶️ Сделать активным"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=1), services)
    confirm = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert confirm.inline_keyboard[0][0].callback_data == GwSlotCB(action="card", slot=1).pack(), \
        "из карточки — «Отмена» обратно в карточку"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=1), services)
    assert services.active_gateway().id == 1
    assert _screen(nav)[0].startswith("⭐ <b>NASPi</b> — "), "из карточки — карточка того, на кого переключили"
    # активный — «уже идёт», без диалога
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=1), services)
    assert not [s for s in nav.sent if s[0] == "edit_text"] and "уже идёт" in cb.answers[0][0]
    # лежащий резерв — предупреждение и «Всё равно»
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2), services)
    text, labels = _screen(nav)
    assert text.startswith("⚠️ Pi2 не отвечает ") and " мин — точно переключаем?" in text, text
    assert labels == ["⬅️ Отмена", "▶️ Всё равно"]


async def test_home_subnets_and_label_inputs(services, slots, fake_bot):
    """«🗺 Подсети»: приглашение с текущими; итог правки — первыми строками
    карточки (не отдельным сообщением); «✏️ Подпись» из «Изменить» — карточка
    с подписью в заголовке."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_home(cb, GwSlotCB(action="home", slot=1), services, st)
    text, labels = _screen(nav)
    assert text.startswith("🗺 <b>Подсети NASPi</b> · сейчас не заданы\n"), text
    assert labels == ["✖️ Отмена"]
    msg = _amsg(fake_bot, "192.168.1.0/24 мусор")
    await sh.gateway_home_received(msg, st, services)
    assert services.db.gateway(1).home_subnets == ["192.168.1.0/24"]
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1, f"итог правки — первыми строками карточки, а не отдельным сообщением: {answers}"
    assert answers[0].startswith("✅ Подсети NASPi: <code>192.168.1.0/24</code>\n⚠️ Не принято: <code>мусор</code> — "), answers
    assert "🗺 <code>192.168.1.0/24</code> · 🔀 VPN-транзит ☑️" in answers[0]
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    text, _ = _screen(nav)
    assert text == ("✏️ <b>Подпись Pi2</b> — место одним-двумя словами: «дача», «офис». До 20 символов, «—» — убрать")
    msg = _amsg(fake_bot, "дача")
    await sh.gateway_label_received(msg, st, services)
    assert services.db.gateway(2).label == "дача"
    assert await st.get_state() is None
    shown = [s[1] for s in msg.sent if s[0] in ("answer", "edit_text")]
    assert shown and shown[-1] == "✅ Подпись: — → дача\n\n✏️ <b>Pi2</b> — изменить", "назад в «✏️ Изменить»"
    # «—» — убрать подпись
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    msg = _amsg(fake_bot, "—")
    await sh.gateway_label_received(msg, st, services)
    assert services.db.gateway(2).label == ""
    assert [s[1] for s in msg.sent if s[0] in ("answer", "edit_text")][-1].startswith("✅ Подпись: дача → —")


async def test_failed_label_input_reasks_and_keeps_the_input_open(services, slots, fake_bot):
    """Отказ сервиса (слот исчез, пока приглашение висело): переспрос, ввод не
    закрыт, «Отмена» на приглашении работает — и ничего не убираем."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    services.db.gateway_delete(2)
    msg = _amsg(fake_bot, "дом 2")
    await sh.gateway_label_received(msg, st, services)
    assert any(s[0] == "answer" and s[1].startswith("⚠️") for s in msg.sent)
    assert await st.get_state() is not None, "ввод открыт — можно ответить ещё раз"
    assert not [r for r in fake_bot.records if r[0] == "delete_message"], "до успеха ничего не убираем"


async def test_add_second_slot_as_new_machine_asks_its_own_token(services, slots, fake_bot):
    """«➕ Новое устройство» в новый слот — сразу к выпуску, без подтверждения:
    у второго шлюза свой бот, токен спрашивается здесь один раз."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    text, markup = await sh._screen("rt_gw", services)
    assert text.startswith("🛰 <b>Резервный шлюз</b> — ") and _labels(markup) == [
        "📱 Из моих устройств", "➕ Новое устройство", "⬅️ Назад"], _labels(markup)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=0), services, st)
    text, labels = _screen(nav)
    assert text.startswith("🤖 <b>Токен бота шлюза 2</b> — создай бота у @BotFather"), "без промежуточного подтверждения"
    assert labels == ["✖️ Отмена"]
    msg = _amsg(fake_bot, "222222222:BB-second-token-value-long-enough")
    await sh.gateway_token_received(msg, st, services)
    assert services.token[2].startswith("222222222:")
    assert nav.message_id in [r[2] for r in fake_bot.records if r[0] == "delete_message"], \
        "приглашение ввести токен отслужило"
    gws = services.db.gateways()
    assert [g.id for g in gws] == [1, 2] and services.db.get_device(gws[1].device_id).name == "Шлюз 2"
    assert services.runs[-1][0] == "--apply" and services.runs[-1][1]["LINK_IF"] == "awglink2"
    docs = [s for s in msg.sent if s[0] == "document"]
    assert len(docs) == 1 and docs[0][1].startswith("🛰 Файл конфигурации шлюза\n"), docs
    assert "<b>«Шлюз 2»</b>" in docs[0][1], "подпись файла первого применения не называет новый шлюз"
    instr = next(s[1] for s in msg.sent if s[0] == "answer" and "--install" in s[1])
    assert "awg-gw-bundle-awglink2.sh" in instr


async def test_replacing_a_machine_still_asks_first(services, slots, fake_bot):
    """Замена машины в занятом слоте — с подтверждением («Отмена» первой):
    прежняя машина потеряет линк."""
    _, pi, _ = slots
    _slot1(services, pi)
    text, markup = await sh._screen("rt_gw", services, "1")
    assert text.startswith("🔁 Заменить устройство NASPi? Ключи линка сменятся"), text
    back = markup.inline_keyboard[-1][0]
    assert back.text == "⬅️ Назад" and back.callback_data == GwSlotCB(action="edit", slot=1).pack()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=1), services, FakeState())
    text, labels = _screen(nav)
    assert text.startswith("➕ Новое устройство «Шлюз» в твоём профиле") and labels == ["⬅️ Отмена", "🔁 Заменить"]
    assert services.runs == [], "вопрос ничего не выпустил"


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
    text, labels = _screen(nav)
    assert text == ("🛰 Pi2 станет шлюзом? Выйдет из лимитов; удалить, заблокировать, выдать ссылку будет "
                    "нельзя. Ключи линка — новые, файл первого применения выпущу сразу"), text
    assert labels == ["⬅️ Отмена", "🛰 Назначить"]
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == 2
    assert any(s[0] == "document" and s[1].startswith("🛰 Файл конфигурации шлюза\n")
               and "<b>«Pi2»</b>" in s[1] for s in nav.sent), nav.sent


async def test_remove_standby_and_active(services, slots, fake_bot, monkeypatch):
    """Снять резерв и активный — из «✏️ Изменить», с подтверждением; итог —
    первой строкой экрана «Шлюзы»; «Отмена» — назад в «Изменить»."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_ask(cb, GwSlotCB(action="remove_ask", slot=2), services)
    text, labels = _screen(nav)
    assert text == ("🛑 Pi2 — больше не резерв?\nЛинк снимется; трафик пойдёт через NASPi, резерва не будет. "
                    "Устройство станет обычным"), text
    assert labels == ["⬅️ Отмена", "🛑 Снять"]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert markup.inline_keyboard[0][0].callback_data == GwSlotCB(action="edit", slot=2).pack()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    assert [g.id for g in services.db.gateways()] == [1] and services.runs[-1][0] == "--rollback"
    text, labels = _screen(nav)
    assert text.startswith("🛑 Pi2 больше не шлюз · трафик идёт через NASPi, резерва нет\n\n🛰 <b>Шлюзы</b> · "), text
    assert "➕ Резерв" in labels
    # снова два, убираем активный — трафик на второй
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_ask(cb, GwSlotCB(action="remove_ask", slot=1), services)
    text, _ = _screen(nav)
    assert text.startswith("🛑 NASPi — больше не шлюз?\nТрафик сразу перейдёт на Pi2 — адрес сменится"), text
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=1), services)
    assert services.active_gateway().id == 2 and services.runs[-1][0] == "--rekey"
    # устройство-шлюз из «Моих устройств» открывает карточку своего слота
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi2.id), services, FakeState())
    text, labels = _screen(nav)
    assert "Pi2</b> — " in text.split("\n")[0] and "Активен" in text.split("\n")[0], text
    assert "📡 <code>awglink2:8443</code>" in text and "✏️ Изменить" in labels, labels
    # последний — «РФ-доступ выключится»
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_ask(cb, GwMarkCB(action="remove_ask", device_id=pi2.id), services)
    text, _ = _screen(nav)
    assert "РФ-доступ выключится до назначения нового" in text
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    text, labels = _screen(nav)
    assert text.startswith("🛑 Pi2 больше не шлюз · РФ-доступ выключен до назначения нового\n\n"), text
    assert "🛰 Назначить" in labels


async def test_bundle_button_and_action_are_per_slot(services, slots, fake_bot):
    """«📤 Конфигурация» в карточке слота 2 выпускает файл именно слота 2 —
    сразу, без промежуточного экрана: карточка гаснет, файл с подписью."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дача")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    datas = {b.text: b.callback_data for row in markup.inline_keyboard for b in row}
    assert datas.get("📤 Конфигурация") == GwSlotCB(action="bundle", slot=2).pack(), datas
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_bundle(cb, GwSlotCB(action="bundle", slot=2), services)
    assert not [s for s in nav.sent if s[0] == "edit_text"], "перед файлом снова промежуточный экран"
    assert ("edit_reply_markup", ADMIN) in fake_bot.records or any(s[0] == "edit_reply_markup" for s in nav.sent), \
        "карточка над файлом не погасла"
    docs = [s[1] for s in nav.sent if s[0] == "document"]
    assert len(docs) == 1 and docs[0].startswith("📤 Конфигурация шлюза <b>«Pi2» (дача)</b>."), docs


async def test_params_screen_cycles_probe_window_threshold_and_lists(services, slots, fake_bot, monkeypatch):
    """«⚙️ Параметры» с экрана «Шлюзы»: такт, окно, порог и период списков —
    циклами по кругу, значение пишется в настройки, экран перерисовывается,
    всплывашка называет новое значение; «⬇️ Обновить списки» — числом записей."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.delattr(services, "_rt_window_size")      # фикстура прибила окно — здесь оно из настроек
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    real_int, real_get = settings.get_int, settings.get
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: store.get(k, real_int(k, d)))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, real_get(k, d)))
    _, markup = await sh._screen("rt", services)
    params = next(b for r in markup.inline_keyboard for b in r if b.text == "⚙️ Параметры")
    assert SetCB.unpack(params.callback_data) == SetCB(sec="rt_params", act="open")
    text, markup = await sh._screen("rt_params", services)
    assert _rows(markup) == [["⏱ Такт: 30 с", "🪟 Окно: 10"], ["📉 Порог: 50%", "🔄 Списки: 6 ч"],
                             ["⬇️ Обновить списки"], ["🔴 Выключить РФ-доступ"], ["⬅️ Назад"]], _rows(markup)
    assert text.split("\n")[1] == "Проверка живости: такт 30 с · окно 10 · порог 50% (5 неудач из 10)", text
    steps = [("app.routing.failover.window_samples", [20, 5, 10], "🪟 Окно: {}", "Окно: {}"),
             ("app.routing.probe_seconds", [45, 60, 30], "⏱ Такт: {} с", "Такт: {} с"),
             ("app.routing.failover.min_availability", [75, 25, 50], "📉 Порог: {}%", "Порог: {}%"),
             ("app.routing.lists_refresh_hours", [12, 24, 6], "🔄 Списки: {} ч", "Списки: раз в {} ч")]
    for key, values, label, toast in steps:
        for v in values:
            cb, nav = _acb(fake_bot)
            await sh.cycle(cb, SetCB(sec="rt_params", act="cycle", key=key), services)
            assert store[key] == v, (key, store.get(key))
            _, labels = _screen(nav)
            assert label.format(v) in labels, (key, labels)
            assert cb.answers[-1][0] == toast.format(v), f"всплывашка цикла: {cb.answers[-1]}"
            assert cb.answers[-1][0].count(":") == 1 and "app." not in cb.answers[-1][0], "ключ настройки в тексте"
    store["app.routing.failover.window_samples"] = 20
    text, _ = await sh._screen("rt_params", services)
    assert "окно 20 · порог 50% (10 неудач из 20)" in text
    monkeypatch.setattr(services, "routing_update_lists", lambda force=False: 41200)
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="lists_refresh"), services)
    assert cb.answers == [("Обновляю списки…", False)], cb.answers
    text = _screen(nav)[0]
    assert text.startswith("✅ Списки обновлены: 41 200 записей\n\n⚙️ <b>Параметры РФ-доступа</b>"), text


async def test_params_turn_off_asks_first_and_returns_to_params(services, slots, fake_bot, monkeypatch):
    """«🔴 Выключить РФ-доступ» — только через подтверждение; «Отмена» — назад
    в «Параметры», а не в корень."""
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.toggle(cb, SetCB(sec="rt", act="toggle", key="app.routing.enabled"), services)
    text, labels = _screen(nav)
    assert text.startswith("🔴 Выключить РФ-доступ для всех?") and labels == ["⬅️ Отмена", "🔴 Выключить"]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert markup.inline_keyboard[0][0].callback_data == SetCB(sec="rt_params", act="open").pack()


async def test_single_gateway_card_has_no_preferred_mark(services, slots, fake_bot):
    """С единственным шлюзом выбирать предпочтительного не из чего: ни «⭐»
    на кнопке и в заголовке карточки, ни строки «предпочтительный при холодном
    старте», ни «⭐ При старте» в «Изменить»; со вторым слотом — появляются."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    _, markup = await sh._screen("rt", services)
    assert markup.inline_keyboard[0][0].text == "NASPi", "⭐ у единственного шлюза"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    text, labels = _screen(nav)
    assert text.startswith("🛰 <b>NASPi</b> — "), text
    assert "предпочтительн" not in text.lower(), text
    assert labels[-2:] == ["✏️ Изменить", "⬅️ Назад"]
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=1), services, FakeState())
    _, labels = _screen(nav)
    assert any(l.startswith("⭐ При старте") for l in labels)


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────

def _admin_line(services) -> str:
    from awgbot.bot import texts
    return texts.routing_admin_status_line(services.routing_admin_status())


def _push(services, slot: int, good: bool, n: int = 10) -> None:
    for _ in range(n):
        services._rt_window_push(slot, good)


async def test_admin_status_line_names_the_active_gateway_and_the_standby(services, slots, monkeypatch):
    """Пять случаев из ТЗ: один жив; два живы; резерв мёртв (🟠); один мёртв;
    оба мертвы — с именами, а не «сервер работает»."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🟢 работает · NASPi"
    services.db.gateway_update(1, label="дом 1")
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🟢 работает · NASPi (дом 1)"
    services.db.gateway_update(1, label="")

    _slot2(services, pi2)
    assert _admin_line(services).endswith("работает · NASPi · резерв проверяется"), "стрика ещё нет"
    services.db.set_state("routing_gw_2_up_streak", "3")
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🟢 работает · NASPi · резерв жив"
    _push(services, 2, False)
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🟠 работает · NASPi · резерв не отвечает"

    monkeypatch.setattr(services, "routing_link_ok", lambda: False)
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🔴 недоступен — NASPi, Pi2 не отвечают"
    services._rt_window_reset(2); services.db.set_state("routing_gw_2_up_streak", "3")
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🔴 недоступен, NASPi не отвечает, резерв жив"

    services.db.gateway_delete(2)
    assert _admin_line(services) == "🇷🇺 РФ-доступ: 🔴 недоступен, NASPi не отвечает"


async def test_client_status_line_has_two_states_only():
    from awgbot.bot import texts
    assert texts.routing_status_line(True) == "🇷🇺 РФ-доступ: 🟢 работает"
    assert texts.routing_status_line(False) == "🇷🇺 РФ-доступ: 🔴 не работает"


async def test_admin_panel_uses_the_detailed_line(services, slots, monkeypatch):
    _, pi, _ = slots
    _slot1(services, pi)
    monkeypatch.setattr(services, "server_status_cached", lambda: {"ok": True})
    snap = services.admin_panel_snapshot()
    assert snap["routing_info"] and snap["routing_info"]["active"] == "NASPi"
    from awgbot.bot import texts
    text = texts.admin_panel(snap["st"], snap["routing_ok"], routing_info=snap["routing_info"])
    assert "🇷🇺 РФ-доступ: 🟢 работает · NASPi" in text


# ── «🔀 VPN-транзит» ────────────────────────

async def test_lan_mode_needs_a_subnet_then_asks_and_toggles(services, slots, fake_bot, monkeypatch):
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    assert cb.answers == [("Сначала задай подсеть шлюза («🗺 Подсети»): без неё работать не будет", True)], \
        "без подсети — alert, не диалог"
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda gw: "")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    text, labels = _screen(nav)
    lines = text.split("\n")
    assert lines[0] == "🔀 <b>VPN-транзит на NASPi</b> — включить?", lines
    assert "⚠️ Шлюз станет точкой отказа: упадёт — подсеть без интернета, резерв не поможет" in lines
    assert any(l.startswith("⚠️ Свой резолвер не настроен") for l in lines), "без резолвера — предупреждение"
    assert lines[-1] == "Потребуется перевыпуск конфигурации шлюза", "канала нет — файлом"
    assert labels == ["⬅️ Отмена", "✅ Включить"]
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1
    text, labels = _screen(nav)
    assert "🗺 <code>192.168.68.0/24</code> · 🔀 VPN-транзит ✅" in text.split("\n"), text
    assert "✅ VPN-транзит" in labels and "❓ Роутер" in labels
    assert cb.answers[-1] == ("VPN-транзит включён: перевыпусти конфигурацию шлюза", True), cb.answers
    # выключение — с предупреждением про роутер
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda gw: "10.9.1.1")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    text, labels = _screen(nav)
    assert text.startswith("🔀 <b>VPN-транзит на NASPi</b> — выключить?\n⚠️ Сначала убери на роутере"), text
    assert labels == ["⬅️ Отмена", "☑️ Выключить"]
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0
    assert "❓ Роутер" not in _screen(nav)[1]


async def test_router_recipe_is_shown_in_tabs(services, slots, fake_bot):
    """«❓ Роутер» — вкладками: одна показана, активная с «✅»; переключение
    вкладки — та же кнопка с другим val; «Назад» — в карточку."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    services.db.gateway_update(1, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_router(cb, GwSlotCB(action="router", slot=1), services)
    text, labels = _screen(nav)
    assert text.startswith("❓ <b>Роутер для NASPi</b> · <code>192.168.68.0/24</code> · шлюз <code>АДРЕС_ШЛЮЗА</code>\n"), text
    assert "<b>MikroTik RouterOS 7</b>" in text and "<b>OpenWrt</b>" not in text
    assert labels == ["✅ MikroTik", "OpenWrt", "⬅️ Назад"]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        GwSlotCB(action="router", slot=1, val="mt").pack(), GwSlotCB(action="router", slot=1, val="ow").pack()]
    assert markup.inline_keyboard[1][0].callback_data == GwSlotCB(action="card", slot=1).pack()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_router(cb, GwSlotCB(action="router", slot=1, val="ow"), services)
    text, labels = _screen(nav)
    assert "<b>OpenWrt</b>" in text and "<b>MikroTik RouterOS 7</b>" not in text
    assert labels == ["MikroTik", "✅ OpenWrt", "⬅️ Назад"]
    assert "панели" not in text, "строки «адрес — в панели бота шлюза» больше нет"


async def test_lan_mode_travels_in_the_bundle_and_reminds_on_change(services, slots, monkeypatch):
    """LAN_MODE, HOME_SUBNETS и RESOLVER едут в окружении сборки; смена любого
    из них после выпуска — напоминание о перевыпуске своим текстом, один раз."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    env, _ = services._gw_bundle_env(services.db.gateway(1))
    assert env["LAN_MODE"] == "0" and env["HOME_SUBNETS"] == "192.168.68.0/24" and env["RESOLVER"] == ""
    services.gateway_set_lan_mode(1, True)
    env, _ = services._gw_bundle_env(services.db.gateway(1))
    assert env["LAN_MODE"] == "1" and env["RESOLVER"] == "10.9.1.1"
    # снимок зависимостей — при сборке бандла
    services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, 1),
                          services._gw_bundle_deps(services.db.gateway(1)))
    assert services.gw_bundle_drift_notes() == []
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "локальные подсети" in notes[0].text and "неактуальна" in notes[0].text
    assert services.gw_bundle_drift_notes() == [], "один раз на расхождение"
    services.gateway_set_lan_mode(1, False)
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "VPN-транзит" in notes[0].text and "резолвер" in notes[0].text
    assert "без VPN" not in notes[0].text


# ── связь подсетей ───────

def _peer_conf(monkeypatch):
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": False}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    return store


def test_peer_nets_are_derived_only_between_lan_mode_slots(services, slots, monkeypatch):
    """Чужие подсети слота: тумблер, оба слота без VPN, без пересечений — иначе
    пусто. Пересечение — предупреждение при вводе, не отказ; исчезло — пара
    вернулась сама."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    services.gateway_set_home_subnets(2, "192.168.68.0/24")
    assert services.gateway_peer_nets(1) == [] and services.gateway_peer_nets_info()["state"] == "off"
    store["app.routing.peer_nets.enabled"] = True
    info = services.gateway_peer_nets_info()
    assert info["state"] == "no_lan" and len(info["who"]) == 2
    assert services.gateway_peer_nets(1) == [], "без режима без VPN ответы не найдут дорогу назад"
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    assert services.gateway_peer_nets(1) == ["192.168.68.0/24"] and services.gateway_peer_nets(2) == ["192.168.1.0/24"]
    info = services.gateway_peer_nets_info()
    assert info["state"] == "ok" and [n for _, n in info["pairs"]] == [["192.168.1.0/24"], ["192.168.68.0/24"]]
    # вложенность — тоже пересечение; ввод принят, пара выпала
    res = services.gateway_set_home_subnets(2, "192.168.0.0/16")
    assert res["conflict"] is not None and res["conflict"].id == 1
    assert services.gateway_peer_nets(1) == [] and services.gateway_peer_nets(2) == []
    assert services.gateway_peer_nets_info()["state"] == "overlap"
    res = services.gateway_set_home_subnets(2, "192.168.68.0/24")
    assert res["conflict"] is None and res["peer_others"] == ["«NASPi»"], "кому перевыпускать"
    assert services.gateway_peer_nets(1) == ["192.168.68.0/24"]
    env, _ = services._gw_bundle_env(services.db.gateway(1))
    assert env["PEER_HOME_NETS"] == "192.168.68.0/24"
    import pytest as _pt
    with _pt.raises(Exception, match="сначала выключи"):
        services.gateway_set_home_subnets(2, "")             # при включённом режиме без VPN подсети не убрать
    services.db.gateway_update(2, lan_mode=0)
    services.gateway_set_home_subnets(2, "")
    services.db.gateway_update(2, lan_mode=1)
    assert services.gateway_peer_nets_info()["state"] == "no_nets"


async def test_peer_nets_toggle_has_a_dialog_and_shows_state_in_the_list(services, slots, fake_bot, monkeypatch):
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, labels = _screen(nav)
    assert "↔️ Связь подсетей: выключена" in text.split("\n")
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert ["✅ Автопереключение", "☑️ Связь подсетей"] in _rows(markup), "тумблеры одним рядом"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    text, labels = _screen(nav)
    lines = text.split("\n")
    assert lines[0] == "↔️ <b>Связь подсетей</b> — включить?" and labels == ["⬅️ Отмена", "✅ Включить"]
    assert "только между шлюзами с включённым VPN-транзитом" in text and "\\\\имя.awg.internal" in text
    assert lines[-1].startswith("<blockquote") and "avahi-daemon" in lines[-1], "условие avahi — под «подробнее»"
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert markup.inline_keyboard[0][0].callback_data == GwSlotCB(action="list").pack(), "Отмена — в «Шлюзы»"
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes", val="1"), services)
    assert store["app.routing.peer_nets.enabled"] is True
    assert cb.answers[-1] == ("Подсети связаны: перевыпусти конфигурацию каждого шлюза", True)
    text, labels = _screen(nav)
    assert "✅ Связь подсетей" in labels
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    services.gateway_set_home_subnets(1, "192.168.1.0/24"); services.gateway_set_home_subnets(2, "192.168.68.0/24")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, _ = _screen(nav)
    assert "↔️ Связь подсетей: NASPi: <code>192.168.1.0/24</code> ↔ Pi2: <code>192.168.68.0/24</code>" in text.split("\n"), text
    services.db.gateway_update(2, label="дача")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    assert "Pi2 (дача): <code>192.168.68.0/24</code>" in _screen(nav)[0], "подпись в скобках, подсети после двоеточия"
    services.db.gateway_update(2, label="")
    # карточка: связь подсетей — одной строкой с галочкой, подсеть — строкой выше
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    lines = _screen(nav)[0].split("\n")
    i = lines.index("🗺 <code>192.168.68.0/24</code> · 🔀 VPN-транзит ✅")
    assert "↔️ Связь подсетей ✅" in lines[i + 1:], lines
    # выключение
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    assert _screen(nav)[0] == ("↔️ <b>Связь подсетей</b> — выключить?\nПодсети шлюзов перестанут видеть друг друга "
                               "сразу. Необходим перевыпуск конфигурации каждого шлюза")
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes"), services)
    assert store["app.routing.peer_nets.enabled"] is False
    assert cb.answers[-1] == ("Связь подсетей выключена: перевыпусти конфигурацию каждого шлюза", True)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert "↔️" not in _screen(nav)[0], "связь выключена — строки о ней в карточке нет вовсе"


def _peer_line_state(services, state):
    """Слоты под одно из пяти состояний связи подсетей."""
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    services.gateway_set_home_subnets(1, "192.168.1.0/24"); services.gateway_set_home_subnets(2, "192.168.68.0/24")
    if state == "no_lan":
        services.db.gateway_update(2, lan_mode=0)
    elif state == "no_nets":
        services.db.gateway_update(2, lan_mode=0)
        services.gateway_set_home_subnets(2, "-")
        services.db.gateway_update(2, lan_mode=1)
    elif state == "overlap":
        services.gateway_set_home_subnets(2, "192.168.1.0/25")


@pytest.mark.parametrize("state, enabled, line", [
    ("off", False, "↔️ Связь подсетей: выключена"),
    ("no_lan", True, "↔️ Связь подсетей не работает: у Pi2 выключен VPN-транзит — без него ответы не "
                     "найдут дорогу назад"),
    ("no_nets", True, "↔️ Связь подсетей не работает: у Pi2 не заданы подсети — «🗺 Подсети» в карточке шлюза"),
    ("overlap", True, "↔️ Связь подсетей не работает: подсети NASPi и Pi2 пересекаются ({nets}) — "
                      "смени подсеть одного из шлюзов"),
    ("ok", True, "↔️ Связь подсетей: NASPi: <code>192.168.1.0/24</code> ↔ Pi2: <code>192.168.68.0/24</code>"),
])
async def test_peer_nets_line_in_every_state(services, slots, monkeypatch, state, enabled, line):
    """Строка связи подсетей на экране «Шлюзы» — во всех пяти состояниях, в
    новых словах и с именами шлюзов так же, как в строках слотов (без
    кавычек): человек сверяет имена глазами."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    store["app.routing.peer_nets.enabled"] = enabled
    _peer_line_state(services, state)
    info = services.gateway_peer_nets_info()
    assert info["state"] == state
    line = line.format(nets=", ".join(f"<code>{n}</code>" for n in info.get("nets") or []))
    text, _ = await sh._screen("rt", services)
    assert line in text.split("\n"), text


async def test_failover_toggle_lives_on_the_gateways_screen(services, slots, fake_bot, monkeypatch):
    """«Автопереключение» — только на экране «Шлюзы»: выключили — строка о
    последствиях на месте, всплывашка называет состояние."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_failover(cb, services)
    assert store["app.routing.failover.enabled"] is False
    text, labels = _screen(nav)
    assert "☑️ Автопереключение" in labels
    assert ("⚠️ Автопереключение выключено: при падении активного шлюза РФ-доступ выключится, а не перейдёт "
            "на резерв") in text.split("\n")
    assert cb.answers[-1][0] == "Автопереключение выключено"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_failover(cb, services)
    assert store["app.routing.failover.enabled"] is True and "⚠️ Автопереключение" not in _screen(nav)[0]
    # в карточке слота тумблера нет
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    assert not any("Автопереключение" in l for l in _screen(nav)[1])


async def test_the_old_failover_toggle_from_monitoring_lands_on_the_gateways_screen(
        services, slots, fake_bot, monkeypatch):
    """Тумблер «Автопереключение» из «Мониторинга» 3.1.0 в старом сообщении:
    переключает и рисует экран «Шлюзы», где тумблер живёт теперь."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.toggle(cb, SetCB(sec="rt_mon", act="toggle", key="app.routing.failover.enabled"), services)
    assert store["app.routing.failover.enabled"] is False
    text, labels = _screen(nav)
    assert text.startswith("🛰 <b>Шлюзы</b> · ") and "☑️ Автопереключение" in labels
    assert cb.answers[-1][0] == "Автопереключение выключено"


async def test_gateways_screen_states_without_slots(services, fake_bot, monkeypatch):
    """Шлюзов нет: «Шлюз не назначен» и «🛰 Назначить»; функция выключена —
    «✅ Включить» и выход, разрешения сохранены."""
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    flags = {"app.routing.enabled": True}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: flags.get(k, d))
    monkeypatch.setattr(services, "routing_status", lambda: (True, ""))
    text, markup = await sh._screen("rt", services)
    assert text == "🛰 <b>Шлюзы</b> · 🇷🇺 РФ-доступ 🟢\nШлюз не назначен", text
    assert _rows(markup) == [["🛰 Назначить"], ["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]]
    flags["app.routing.enabled"] = False
    text, markup = await sh._screen("rt", services)
    assert text == "🛰 <b>Шлюзы</b> · 🇷🇺 РФ-доступ выключен · разрешения и списки сохранены"
    assert _rows(markup) == [["✅ Включить"], ["⬅️ В меню"]]
    assert SetCB.unpack(markup.inline_keyboard[0][0].callback_data) == SetCB(
        sec="rt", act="toggle", key="app.routing.enabled")


def test_peer_nets_change_reminds_about_reissue(services, slots, monkeypatch):
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    services.gateway_set_home_subnets(1, "192.168.1.0/24"); services.gateway_set_home_subnets(2, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "")
    for sid in (1, 2):
        services.db.set_state(services._gw_slot_key(services._GW_BUNDLE_DEPS_KEY, sid),
                              services._gw_bundle_deps(services.db.gateway(sid)))
    assert services.gw_bundle_drift_notes() == []
    store["app.routing.peer_nets.enabled"] = True
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 2 and all("локальные подсети других шлюзов" in n.text for n in notes), "обоим слотам, один раз"
    assert services.gw_bundle_drift_notes() == []


# ── «👥 Кому доступен» ───────────────────────────────────────────────────────

async def test_who_has_access_uses_select_all_both_ways(services, slots, fake_bot, make_active_client):
    """«👥 Кому доступен»: отметки профилей и «Выбрать все» по правилу
    массового выбора — ☑️ выдаёт всем, ✅ снимает со всех; «Назад» — в
    «Шлюзы». Каждый, кому выдали, получает уведомление один раз."""
    a = make_active_client(name="Аня", tg_id=4101)
    b = make_active_client(name="Боря", tg_id=4102)
    text, markup = await sh._screen("rt_users", services)
    assert text.startswith("👥 <b>Кому доступен РФ-доступ</b> (тебе — всегда)"), text
    rows = _rows(markup)
    assert ["☑️ Аня"] in rows and ["☑️ Боря"] in rows and rows[-2:] == [["☑️ Выбрать все"], ["⬅️ Назад"]], rows
    assert markup.inline_keyboard[-1][0].callback_data == SetCB(sec="rt").pack()
    before = len([r for r in fake_bot.records if r[0] == "send_message"])
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services)
    assert services.db.get_client(a.id).routing_allowed and services.db.get_client(b.id).routing_allowed
    assert cb.answers[-1][0] == "РФ-доступ разрешён всем"
    assert _screen(nav)[1][-2] == "✅ Выбрать все"
    notified = [r for r in fake_bot.records if r[0] == "send_message"][before:]
    assert sorted(r[1] for r in notified) == [4101, 4102], "уведомление — каждому, по одному"
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services)
    assert not services.db.get_client(a.id).routing_allowed and not services.db.get_client(b.id).routing_allowed
    assert cb.answers[-1][0] == "РФ-доступ не разрешён никому"


async def test_allow_all_reconciles_once_and_answers_first(services, slots, fake_bot, make_active_client, monkeypatch):
    """«Выбрать все» по одному профилю давал полную реконсиляцию и перезапуск
    dnsmasq на каждый (15 профилей — 15 сбросов DNS-кэша всем) и отвечал
    колбэку в конце — протухший колбэк, всплывашка терялась."""
    a = make_active_client("Аня", tg_id=4111, device_limit=1)
    b = make_active_client("Боря", tg_id=4112, device_limit=1)
    n = {"reconcile": 0}
    monkeypatch.setattr(services, "reconcile_routing", lambda: n.__setitem__("reconcile", n["reconcile"] + 1))
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services)
    assert services.db.get_client(a.id).routing_allowed and services.db.get_client(b.id).routing_allowed
    assert n["reconcile"] == 1, f"реконсиляций на два профиля: {n['reconcile']}"
    assert cb.answers == [("РФ-доступ разрешён всем", False)], cb.answers


async def test_transit_confirmation_carries_the_target_state(services, slots, fake_bot, monkeypatch):
    """«Включить/Выключить» без цели брало «не текущее»: двойное нажатие
    выключало только что включённое. Цель едет в кнопке; старая кнопка без
    цели (сообщение прежнего выпуска) — по-старому."""
    _, pi, _pi2 = slots
    _slot1(services, pi)
    calls = []
    monkeypatch.setattr(services, "gateway_set_lan_mode", lambda slot, on: calls.append((slot, on)) or {})
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    yes = _screen(nav)[1]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    packed = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert GwSlotCB(action="lan_yes", slot=1, val="1").pack() in packed, packed
    assert "Включить" in yes[-1] or "Включить" in " ".join(yes)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)]
    # второе нажатие той же кнопки (цель «включить», а режим уже включён) — ничего не переключает
    services.db.gateway_update(1, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)] and cb.answers[0][0] == "Уже включено"


async def test_the_new_slot_number_comes_from_the_service(services, slots, fake_bot, monkeypatch):
    """«Число слотов + 1» после снятия слота 1 при живом 2 давало токен слоту 3,
    а сервис заводит первый свободный — токен уезжал не тому слоту."""
    monkeypatch.setattr(services, "gateway_next_slot", lambda: (7, "awglink7", 9443, "10.99.99.24/30"))
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, st)
    assert _screen(nav)[0] == texts.gateway_ask_token(7)


async def test_removal_warns_when_the_bot_token_stayed_in_env(services, slots, fake_bot, monkeypatch):
    """Запись env не удалась — токен снятого устройства остался; итог снятия
    говорит об этом, иначе новый слот с тем же номером увезёт его в файл
    первого применения (два агента на одном токене)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    monkeypatch.setattr(services, "gw_bot_token", lambda slot_id=None: "222:BBB" if slot_id == 2 else "")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    text, _ = _screen(nav)
    assert text.startswith("🛑 Pi2 больше не шлюз · трафик идёт через NASPi, резерва нет\n"
                           + texts.GW_TOKEN_NOT_FORGOTTEN + "\n\n🛰 <b>Шлюзы</b> · "), text
