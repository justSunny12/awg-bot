"""Экран «🛰 Шлюзы» и слоты — что нажатия меняют в слотах, настройках, ключах
линка и выданных файлах: переключение в обе стороны, «⭐ При старте», пинг,
подсети, подпись, добавление второго слота с токеном, снятие резервного и
активного, «⚙️ Параметры» циклами, «🔀 VPN-транзит», «↔️ Связь подсетей» (часть
VPN-транзита) в обоих состояниях, уведомления о расхождении. Сами экраны (список, карточки,
вопросы, итоги, всплывашки) — в эталоне tests/screens/admin.txt (adm.rt.*,
adm.gw.*, adm.main.gw*)."""
from __future__ import annotations

import base64
import os

import pytest

from awgbot.bot.callbacks import GwMarkCB, GwSlotCB, SetCB
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
    # токен команды подключения — из конфигов линка, которых в тестах нет
    monkeypatch.setattr(services, "_gwjoin_token", lambda slot, jid, **k: f"AWGJ1.DUMMY{slot.id}")
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


def _code_ok(services, slot: int = 0) -> dict:
    """Код с экрана шлюза верный — как ввод в чат бота сервера: строка слота
    и устройство по виду сопряжения, смена ключей после кода начата."""
    st = services.gwjoin(slot) if slot else services.gwjoin_all()[0]
    services._gwjoin_live_set(int(st["slot"]), {"code": "K7F2QX", "kp": bytes(32), "sid": "s1"})
    res = services.gwjoin_code(int(st["slot"]), st["id"], "k7f-2qx")
    assert res["result"] == "ok", res
    return res["st"]


def _join_msgs(msg) -> list[str]:
    return [s[1] for s in msg.sent if s[0] == "answer" and "--join AWGJ1." in (s[1] or "")]


def _settled(services, slots_n=2):
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()


def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


async def test_the_card_pings_lazily_and_the_button_pings_anew(services, slots, fake_bot):
    """Пинг меряется лениво — при первом открытии карточки, дальше из кэша, а
    кнопка «📡 Пинг» меряет заново: без кэша каждое открытие карточки ходило
    бы в линк. Подпись слота в списке и в заголовке карточки — эталоны
    adm.rt.two.label и adm.gw.card.label; ответ пинга резерва —
    adm.gw.ping.standby."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    assert services.pings["n"] == 0, "список пингует — должен лениво только карточка"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert services.pings["n"] == 1, "первое открытие карточки не измерило пинг"
    # второе открытие — из кэша
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert services.pings["n"] == 1, "второе открытие снова пошло в линк"
    # кнопка пинга меряет заново — и именно этот слот
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert services.pings["n"] == 2, "кнопка «📡 Пинг» не измерила заново"


async def test_card_never_has_more_than_eight_buttons(services, slots, fake_bot):
    """Карточка слота — не больше восьми кнопок в самом полном виде: резерв с
    VPN-транзитом (у него и «Сделать активным», и «❓ Роутер»)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(2, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert _rows(markup) == [["▶️ Сделать активным"], ["📡 Пинг"],
                             ["✅ VPN-транзит", "🗺 Подсети"], ["🛜 Роутер", "✏️ Изменить"], ["⬅️ Назад"]]
    assert sum(len(r) for r in markup.inline_keyboard) <= 8


async def test_preferred_toggle_moves_and_clears(services, slots, fake_bot):
    """«⭐ При старте»: у ☑️ — перенести на этот слот (с прежнего снимается),
    у ✅ — снять вовсе — иначе холодный старт выберет не тот шлюз. Кнопка
    снова ☑️ и всплывашка «снят» — эталон adm.gw.pref.clear."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.db.gateway(2).preferred == 1 and services.db.gateway(1).preferred == 0, \
        "звезда не переехала на слот 2"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.preferred_gateway() is None, "повторное нажатие не сняло предпочтительного"


async def test_rename_from_the_edit_screen_renames_the_device_and_closes_the_input(services, slots, fake_bot):
    """«✏️ Имя» — имя устройства-шлюза: ввод переименовывает устройство и
    закрывает диалог (иначе следующее сообщение снова уйдёт в имя).
    Приглашение — эталон adm.gw.name."""
    from awgbot.bot.handlers.admin import devices as dh
    _, pi, _ = slots
    _slot1(services, pi)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    services.db.nav_touch(ADMIN, nav.message_id)
    await sh.gw_slot_name(cb, GwSlotCB(action="name", slot=1), services, st)
    msg = _amsg(fake_bot, "NAS")
    await dh.device_edit_name_apply(msg, services, st)
    assert services.db.get_device(pi.id).name == "NAS"
    assert await st.get_state() is None, "ввод имени не закрыт"


async def test_manual_switch_both_ways_with_confirmation(services, slots, fake_bot):
    """«▶️ Переключить на Pi2» с экрана «Шлюзы»: вопрос ничего не
    переключает, подтверждение переключает; обратно — из карточки бывшего
    активного; активный — без диалога и без переключения. «Отмена» и итог со
    списка — снова «Шлюзы», «уже идёт» — эталоны adm.gw.switch_ask.list,
    adm.gw.switch_yes.list, adm.gw.switch_ask.active."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2, val="l"), services)
    assert services.active_gateway().id == 1, "вопрос ничего не переключил"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=2, val="l"), services)
    assert services.active_gateway().id == 2, "трафик не переключён"
    # обратно — из карточки первого, который теперь в резерве
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=1), services)
    assert services.active_gateway().id == 1
    # активный — «уже идёт», без диалога
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=1), services)
    assert services.active_gateway().id == 1, "нажатие на активный что-то переключило"


async def test_lan_subnets_and_label_inputs(services, slots, fake_bot):
    """«🗺 Подсети»: принятое сохраняется, мусор — нет; «✏️ Подпись»
    сохраняется, «—» её убирает. Итоги («⚠️ Не принято: …» первыми строками
    карточки, «✅ Подпись: дача → —») — эталоны adm.gw.lan_nets.partial и
    adm.gw.label.clear."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan(cb, GwSlotCB(action="lan_nets", slot=1), services, st)
    msg = _amsg(fake_bot, "192.168.1.0/24 мусор")
    await sh.gateway_lan_received(msg, st, services)
    assert services.db.gateway(1).lan_subnets == ["192.168.1.0/24"], "мусор записан в подсети или принятое потеряно"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    msg = _amsg(fake_bot, "дача")
    await sh.gateway_label_received(msg, st, services)
    assert services.db.gateway(2).label == "дача"
    assert await st.get_state() is None
    # «—» — убрать подпись
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    msg = _amsg(fake_bot, "—")
    await sh.gateway_label_received(msg, st, services)
    assert services.db.gateway(2).label == "", "«—» не убрало подпись"


async def test_failed_label_input_reasks_and_keeps_the_input_open(services, slots, fake_bot):
    """Слот исчез, пока приглашение висело: отвечать больше некуда — ввод
    закрыт, «⚠️ Такого шлюза нет» с выходом «⬅️ В меню», не тупик (эталон
    adm.gw.label.gone)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    services.db.gateway_delete(2)
    msg = _amsg(fake_bot, "подпись 2")
    await sh.gateway_label_received(msg, st, services)
    assert await st.get_state() is None, "ввод остался открытым, а слота уже нет"
    shown = [s for s in msg.sent if s[0] == "answer"][-1]
    assert shown[1] == "⚠️ Такого шлюза нет" and [b.text for r in shown[2].inline_keyboard for b in r] == ["⬅️ В меню"]


async def test_add_second_slot_as_new_machine_asks_its_own_token(services, slots, fake_bot):
    """«➕ Новое устройство» в новый слот — сразу к выпуску, без подтверждения:
    у второго шлюза свой бот, токен спрашивается здесь один раз и уедет
    каналом именно второму слоту. Дальше — команда подключения второго слота
    (линк на ключах токена), строка слота и устройство «Шлюз 2» — только
    после верного кода. Приглашение «Токен бота шлюза 2» и сообщение
    подключения — эталоны adm.rt.new_ask.standby и adm.join.*."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=0), services, st)
    assert services.runs == [] and [g.id for g in services.db.gateways()] == [1], "до токена что-то выпущено"
    msg = _amsg(fake_bot, "222222222:BB-second-token-value-long-enough")
    await sh.gateway_token_received(msg, st, services)
    assert services.token[2].startswith("222222222:")
    assert nav.message_id in [r[2] for r in fake_bot.records if r[0] == "delete_message"], \
        "приглашение ввести токен отслужило"
    assert [g.id for g in services.db.gateways()] == [1], "строка слота до кода"
    assert services.runs[-1][0] == "--join-stage" and services.runs[-1][1]["LINK_IF"] == "awglink2"
    assert "JOIN_PREV" not in services.runs[-1][1], "новому слоту прежних ключей нет"
    assert len(_join_msgs(msg)) == 1 and not [s for s in msg.sent if s[0] == "document"], msg.sent
    _code_ok(services, 2)
    gws = services.db.gateways()
    assert [g.id for g in gws] == [1, 2] and services.db.get_device(gws[1].device_id).name == "Шлюз 2"


async def test_replacing_a_machine_still_asks_first(services, slots, fake_bot):
    """Замена машины в занятом слоте — сначала вопрос: прежняя машина потеряет
    линк, поэтому сам вопрос ничего не выпускает."""
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=1), services, FakeState())
    assert services.runs == [], "вопрос ничего не выпустил"


async def test_add_second_slot_from_my_devices(services, slots, fake_bot):
    """Второй слот из «моих устройств» при известном токене: команда
    подключения второго слота, после верного кода машина — во втором слоте.
    Список без назначенного и вопрос «станет резервным» — эталоны
    adm.rt.pick_list.standby, adm.rt.pick.standby."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = "222222222:BB-second-token-value-long-enough"
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi2.id, slot=0), services)
    assert services.db.gateway_by_device(pi2.id) is None, "вопрос «станет шлюзом?» уже назначил"
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id) is None, "назначено до кода"
    assert len(_join_msgs(nav)) == 1, f"команда подключения не выдана: {nav.sent}"
    _code_ok(services, 2)
    assert services.db.gateway_by_device(pi2.id).id == 2


async def test_remove_standby_and_active(services, slots, fake_bot, monkeypatch):
    """Снять резерв — линк откатывается; снять активный — трафик на второй и
    смена ключей; последний шлюз снимается целиком. Вопрос старой кнопкой и
    итог «РФ-доступ выключен до назначения нового» — эталоны
    adm.gw.remove_ask.old и adm.gw.remove_yes.last."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    assert [g.id for g in services.db.gateways()] == [1] and services.runs[-1][0] == "--rollback"
    # снова два, убираем активный — трафик на второй
    _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=1), services)
    assert services.active_gateway().id == 2 and services.runs[-1][0] == "--rekey"
    # последний — вопрос ничего не снимает, подтверждение снимает
    cb, nav = _acb(fake_bot)
    await sh.gateway_remove_ask(cb, GwMarkCB(action="remove_ask", device_id=pi2.id), services)
    assert [g.id for g in services.db.gateways()] == [2], "вопрос о снятии уже снял шлюз"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=2), services)
    assert services.db.gateways() == [], "последний шлюз не снят"


async def test_params_screen_cycles_probe_window_threshold_and_lists(services, slots, fake_bot, monkeypatch):
    """«⚙️ Параметры»: такт, окно, порог и период списков — циклами по кругу,
    значение пишется в настройки. Экран после цикла, всплывашка без ключа
    настройки и порог, пересчитанный от окна, — эталоны adm.rt.params.cycle*."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.delattr(services, "_rt_window_size")      # фикстура прибила окно — здесь оно из настроек
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    real_int, real_get = settings.get_int, settings.get
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: store.get(k, real_int(k, d)))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, real_get(k, d)))
    steps = [("app.routing.failover.window_samples", [20, 5, 10]),
             ("app.routing.probe_seconds", [45, 60, 30]),
             ("app.routing.failover.min_availability", [75, 25, 50]),
             ("app.routing.lists_refresh_hours", [12, 24, 6])]
    for key, values in steps:
        for v in values:
            cb, nav = _acb(fake_bot)
            await sh.cycle(cb, SetCB(sec="rt_params", act="cycle", key=key), services)
            assert store[key] == v, (key, store.get(key))


async def test_params_turn_off_asks_first_and_returns_to_params(services, slots, fake_bot, monkeypatch):
    """«🔴 Выключить РФ-доступ» — только через подтверждение; «Отмена» — назад
    в «Параметры», а не в корень."""
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.toggle(cb, SetCB(sec="rt", act="toggle", key="app.routing.enabled"), services)
    text, labels = _screen(nav)
    assert text.startswith("☑️ Выключить РФ-доступ для всех?") and labels == ["⬅️ Отмена", "☑️ Выключить"]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert markup.inline_keyboard[0][0].callback_data == SetCB(sec="rt_params", act="open").pack()


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────
# Шапка админа во всех состояниях активного и резерва (жив, проверяется, не
# отвечает, оба лежат, активен слот 2, подпись слота) — эталоны adm.main.gw*.

async def test_client_status_line_has_two_states_only():
    from awgbot.bot import texts
    assert texts.routing_status_line(True) == "🇷🇺 РФ-доступ: 🟢 работает"
    assert texts.routing_status_line(False) == "🇷🇺 РФ-доступ: 🔴 временно недоступен"


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

async def test_lan_mode_toggles_and_the_router_button_follows_it(services, slots, fake_bot, monkeypatch):
    """«Включить» пишет режим в слот, «Выключить» снимает. «❓ Роутер» в
    карточке — только пока режим включён (рецепт для выключенного транзита
    сломал бы сеть): эталоны adm.gw.lan_yes и adm.gw.lan_yes.off."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda gw: "")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1, "режим не включён"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


def _snap_installed(services, sid: int, online: bool = True) -> None:
    """Снимок слота, когда на шлюзе стоит ровно выдаваемое сейчас, и сессия канала."""
    from awgbot.util import gwlink
    env = services.gwlink_issued_env(services.db.gateway(sid))
    services.gwlink_snapshot_in(sid, {"bundle": {gwlink.snap_field(k): v for k, v in env.items()},
                                      "agent_version": "3.3.0", "rev": 1}, 1, True)
    if online:
        services.gwlink_session_opened(sid, "3.3.0", 2, ["cfg1", "confirm1", "token1"])


async def test_lan_mode_travels_in_the_bundle_and_the_channel_without_a_notification(
        services, slots, monkeypatch):
    """LAN_MODE, LAN_SUBNETS и RESOLVER едут в окружении обвязки — и каналом:
    смена подсетей или режима при живом канале уведомления не даёт, канал
    довезёт сам. Шлюз отверг текущий документ — одно уведомление, режим
    назван словами, а не «без VPN»."""
    from awgbot.domain import gwcfg
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    env = services._lan_env(services.db.gateway(1))
    assert env["LAN_MODE"] == "0" and env["LAN_SUBNETS"] == "192.168.68.0/24" and env["RESOLVER"] == ""
    services.gateway_set_lan_mode(1, True)
    env = services._lan_env(services.db.gateway(1))
    assert env["LAN_MODE"] == "1" and env["RESOLVER"] == "10.9.1.1"
    _snap_installed(services, 1)
    assert services.gw_bundle_drift_notes() == []
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    assert services.gw_bundle_drift_notes() == [], "подсети едут каналом, а бот шлёт уведомление"
    services.gateway_set_lan_mode(1, False)
    assert services.gw_bundle_drift_notes() == [], "режим едет каналом, а бот шлёт уведомление"
    monkeypatch.setattr(services, "gwcfg_link_conf", lambda gw, env: "")
    doc = services.gwcfg_doc(services.db.gateway(1))
    services.gwcfg_ack_in(1, gwcfg.ack_build(doc["gen"], doc["hash"], failed={"env": "exit 1"}))
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1, [n.text for n in notes]
    text = notes[0].text
    assert "VPN-транзит: у сервера выключен, на шлюзе включён" in text and "резолвер" in text, text
    assert "без VPN" not in text
    assert services.gw_bundle_drift_notes() == [], "одно уведомление на расхождение"


# ── связь подсетей ───────

def _peer_conf(monkeypatch):
    store = {"app.routing.enabled": True, "app.routing.failover.enabled": True,
             "app.routing.peer_nets.enabled": False}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(store.get(k, d)))
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    return store


def test_peer_nets_are_prefixes_of_the_other_transit_slots(services, slots, monkeypatch):
    """Подсети соседей слота — префиксы в туннеле других слотов с VPN-транзитом
    (связь подсетей — его часть); без транзита — пусто. От подсетей соседей и
    от мёртвого ключа прежнего тумблера не зависят; одинаковые подсети за
    двумя шлюзами — не пересечение."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    services.gateway_set_lan_subnets(2, "192.168.68.0/24")
    info = services.gateway_peer_nets_info()
    assert services.gateway_peer_nets(1) == [] and info == {"state": "few"}
    services.db.gateway_update(1, lan_mode=1)
    info = services.gateway_peer_nets_info()
    assert services.gateway_peer_nets(1) == [] and info == {"state": "few"}
    services.db.gateway_update(2, lan_mode=1)
    for dead in (False, True):
        store["app.routing.peer_nets.enabled"] = dead
        assert services.gateway_peer_nets(1) == ["10.241.0.0/16"] and services.gateway_peer_nets(2) == ["10.240.0.0/16"]
        info = services.gateway_peer_nets_info()
        assert info["state"] == "ok" and [n for _, _, n in info["pairs_named"]] == [["10.240.1.0/24"], ["10.241.68.0/24"]]
    res = services.gateway_set_lan_subnets(2, "192.168.1.0/24")
    assert res["kept"] == ["192.168.1.0/24"] and "conflict" not in res and "peer_others" not in res
    assert services.gateway_peer_nets(1) == ["10.241.0.0/16"], "подсети соседа префиксу не указ"
    assert [n for _, _, n in services.gateway_peer_nets_info()["pairs_named"]] == [["10.240.1.0/24"], ["10.241.1.0/24"]]
    env = services._lan_env(services.db.gateway(1))
    assert env["PEER_LAN_NETS"] == "10.241.0.0/16"
    import pytest as _pt
    with _pt.raises(Exception, match="сначала выключи"):
        services.gateway_set_lan_subnets(2, "")             # при включённом режиме без VPN подсети не убрать


async def test_the_gateways_screen_has_no_peer_button_and_shows_the_line(services, slots, monkeypatch):
    """Тумблера «Связь подсетей» больше нет: на экране «Шлюзы» — инфобокс.
    Меньше двух слотов с VPN-транзитом — когда появится; два — пары в адресах в
    туннеле (эталоны adm.rt.two, adm.rt.peers.*)."""
    _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    services.gateway_set_lan_subnets(2, "192.168.68.0/24")
    text, markup = await sh.gateways_screen(services)
    assert not [b for row in _rows(markup) for b in row if "Связь подсетей" in b]
    assert "🔗 Связь подсетей появится, когда VPN-транзит будет включён хотя бы у двух шлюзов" in text, text
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    text, markup = await sh.gateways_screen(services)
    assert ("🔗 Связь подсетей: NASPi: <code>10.240.1.0/24</code> ↔️ Pi2: <code>10.241.68.0/24</code>"
            in text), text
    assert not [b for row in _rows(markup) for b in row if "Связь подсетей" in b]


@pytest.mark.parametrize("transit, state", [((0, 0), "few"), ((1, 0), "few"), ((0, 1), "few"), ((1, 1), "ok")])
async def test_peer_nets_info_follows_the_transit_of_the_slots(services, slots, monkeypatch, transit, state):
    """Состояний связи подсетей два: слотов с VPN-транзитом меньше двух — few,
    два и больше — ok. «Выключена», «нет VPN-транзита у…», «пересекаются» —
    сняты вместе с тумблером."""
    _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24"); services.gateway_set_lan_subnets(2, "192.168.1.0/25")
    services.db.gateway_update(1, lan_mode=transit[0]); services.db.gateway_update(2, lan_mode=transit[1])
    assert services.gateway_peer_nets_info()["state"] == state


async def test_turning_transit_on_redraws_the_firewall_with_the_transit_links(services, slots, fake_awg,
                                                                            monkeypatch):
    """Форвардинг линк ↔ линк на ВПС — между линками слотов с VPN-транзитом:
    включили или выключили транзит — таблица файервола перерисована сразу, с
    новым списком (и в форме с фильтром, и в NAT-only — сверка одна)."""
    from awgbot.infra import nftguard
    _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_lan_subnets(1, "192.168.1.0/24"); services.gateway_set_lan_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(1, lan_mode=1)
    for enabled in (True, False):
        monkeypatch.setattr(nftguard, "enabled", lambda e=enabled: e)
        before = fake_awg.fw_calls
        services.gateway_set_lan_mode(2, True)
        assert fake_awg.fw_calls == before + 1 and fake_awg.fw_transit == ["awglink", "awglink2"]
        services.gateway_set_lan_mode(2, False)
        assert fake_awg.fw_calls == before + 2 and fake_awg.fw_transit == ["awglink"]


async def test_failover_toggle_lives_on_the_gateways_screen(services, slots, fake_bot, monkeypatch):
    """«Автопереключение» на экране «Шлюзы»: выключили — настройка записана;
    включили снова — записано. Тумблер ☑️, строка о последствиях и
    всплывашка — эталон adm.gw.failover.off."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_failover(cb, services)
    assert store["app.routing.failover.enabled"] is False, "автопереключение не выключено"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_failover(cb, services)
    assert store["app.routing.failover.enabled"] is True


async def test_the_old_failover_toggle_from_monitoring_lands_on_the_gateways_screen(
        services, slots, fake_bot, monkeypatch):
    """Тумблер «Автопереключение» из «Мониторинга» 3.1.0 в старом сообщении:
    переключает (экран «Шлюзы», где тумблер живёт теперь, — эталон
    adm.rt.mon.failover)."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.toggle(cb, SetCB(sec="rt_mon", act="toggle", key="app.routing.failover.enabled"), services)
    assert store["app.routing.failover.enabled"] is False, "старый тумблер ничего не переключил"


# ── «👥 Кому доступен» ───────────────────────────────────────────────────────

async def test_who_has_access_uses_select_all_both_ways(services, slots, fake_bot, make_active_client):
    """«Все профили» по правилу массового выбора — ☑️ отмечает всех, ✅
    снимает отметки со всех; применяет «✅ Применить»: каждый, кому выдали,
    получает уведомление один раз. Экран и всплывашки — эталоны
    adm.rt.users.all и adm.rt.users.all.off."""
    a = make_active_client(name="Аня", tg_id=4101)
    b = make_active_client(name="Боря", tg_id=4102)
    before = len([r for r in fake_bot.records if r[0] == "send_message"])
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services, st)
    assert not services.db.get_client(a.id).routing_allowed, "отметка применилась без «Применить»"
    assert [r for r in fake_bot.records if r[0] == "send_message"][before:] == []
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_apply"), services, st)
    assert services.db.get_client(a.id).routing_allowed and services.db.get_client(b.id).routing_allowed
    assert cb.answers[-1] == ("РФ-доступ разрешён 2 профилям", False), cb.answers
    notified = [r for r in fake_bot.records if r[0] == "send_message"][before:]
    assert sorted(r[1] for r in notified) == [4101, 4102], "уведомление — каждому, по одному"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services, st)
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_apply"), services, st)
    assert not services.db.get_client(a.id).routing_allowed and not services.db.get_client(b.id).routing_allowed
    # «Применить» без отметок своего экрана (кнопка из старого сообщения) — отказ
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_apply"), services, FakeState())
    assert cb.answers[-1] == ("Кнопка устарела — открой раздел заново", True), cb.answers


async def test_allow_all_reconciles_once_and_answers_first(services, slots, fake_bot, make_active_client, monkeypatch):
    """«Выбрать все» по одному профилю давал полную реконсиляцию и перезапуск
    dnsmasq на каждый (15 профилей — 15 сбросов DNS-кэша всем) и отвечал
    колбэку в конце — протухший колбэк, всплывашка терялась."""
    a = make_active_client("Аня", tg_id=4111, device_limit=1)
    b = make_active_client("Боря", tg_id=4112, device_limit=1)
    n = {"reconcile": 0}
    monkeypatch.setattr(services, "reconcile_routing", lambda: n.__setitem__("reconcile", n["reconcile"] + 1))
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services, st)
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_apply"), services, st)
    assert services.db.get_client(a.id).routing_allowed and services.db.get_client(b.id).routing_allowed
    assert n["reconcile"] == 1, f"реконсиляций на два профиля: {n['reconcile']}"
    assert len(cb.answers) == 1, f"колбэку ответили не один раз: {cb.answers}"


async def test_transit_confirmation_carries_the_target_state(services, slots, fake_bot, monkeypatch):
    """«Включить/Выключить» без цели брало «не текущее»: двойное нажатие
    выключало только что включённое. Цель едет в кнопке; повтор той же
    кнопки при уже включённом режиме ничего не переключает («Уже включён» —
    эталон adm.gw.lan_yes.again)."""
    _, pi, _pi2 = slots
    _slot1(services, pi)
    calls = []
    monkeypatch.setattr(services, "gateway_set_lan_mode", lambda slot, on: calls.append((slot, on)) or {})
    services.gateway_set_lan_subnets(1, "192.168.1.0/24")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)]
    # второе нажатие той же кнопки (цель «включить», а режим уже включён) — ничего не переключает
    services.db.gateway_update(1, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)], f"повтор кнопки переключил режим ещё раз: {calls}"


async def test_no_free_slot_is_an_alert_not_a_spinning_button(services, slots, fake_bot, monkeypatch):
    """Следующий слот не выдаётся — ServiceError (слоты заняты, gateways_max поднят
    без портов/подсетей): раньше никто не ловил — кнопка крутилась, экрана не было.
    Отказ всплывашкой — эталон adm.rt.new_ask.full; диалог ввода не открыт."""
    from awgbot.domain.services import ServiceError
    _, pi, pi2 = slots
    _slot1(services, pi)
    monkeypatch.setattr(services, "_gwjoin_next_slot", lambda: (_ for _ in ()).throw(ServiceError("свободных слотов нет")))
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=0), services, st)
    assert cb.answers, "колбэк без ответа — кнопка крутится"
    assert await st.get_state() is None


async def test_a_dead_standby_remembers_when_it_went_down_and_forgets_on_recovery(services, slots, fake_bot):
    """«не отвечает 5 мин» вторую неделю — ложь окна замеров: момент падения
    хранится в state, а оживление его стирает («13 дн 4 ч» в «Шлюзах» —
    эталон adm.rt.two.down.long)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    since = services.db.get_state("routing_gw_2_down_since")
    assert since and since.isdigit(), "момент падения не сохранён"
    services.probe[2] = "ok"
    for _ in range(services._rt_window_size()):           # окно замеров очистилось от отказов
        services.routing_liveness_tick()
    assert not services.db.get_state("routing_gw_2_down_since"), "ожил — момент падения должен стереться"


# ── подсети шире /16 и кому подсети едут файлом ─────────────────────────────

def _shown(msg, bot) -> str:
    """Всё, что после ввода ушло в чат: меню могло встать новым сообщением
    или правкой прежнего."""
    out = [str(s[1]) for s in msg.sent]
    out += [str(r[2]) for r in bot.records if r[0] in ("send_message", "edit_message_text") and len(r) > 2]
    return "\n".join(out)


async def test_subnets_the_tunnel_cannot_carry_are_refused_at_input(services, slots, fake_bot):
    """На шлюз в туннеле приходится /16, служебные адреса сервера в туннеле —
    10.240.0.0/12, адреса в туннеле подсетей одного шлюза не пересекаются.
    Отказ — при вводе, с причиной; ровно /16 — принимается."""
    _, pi, _ = slots
    _slot1(services, pi)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan(cb, GwSlotCB(action="lan_nets", slot=1), services, st)
    msg = _amsg(fake_bot, "172.16.0.0/15 192.168.1.0/24 10.241.0.0/24 10.10.1.0/24")
    await sh.gateway_lan_received(msg, st, services)
    assert services.db.gateway(1).lan_subnets == ["192.168.1.0/24"], "подсеть, которую туннель не повезёт, записана"
    text = _shown(msg, fake_bot)
    assert "<code>172.16.0.0/15</code> — шире /16: в туннеле на шлюз приходится /16, сузь подсеть" in text, text
    assert ("<code>10.241.0.0/24</code> — в служебном пространстве сервера AWG (<code>10.240.0.0/12</code>): выбери "
            "другую подсеть") in text, text
    assert ("<code>10.10.1.0/24</code> — в туннеле пересеклась бы с <code>192.168.1.0/24</code> (у обеих "
            "<code>10.240.1.0/24</code>): адреса в туннеле подсетей одного шлюза не должны пересекаться") in text, text
    for raw, kept, why in (("172.18.0.0/16", ["172.18.0.0/16"], ""),
                           ("192.168.0.0/22 10.0.2.0/24", ["192.168.0.0/22"],
                            "<code>10.0.2.0/24</code> — в туннеле пересеклась бы с <code>192.168.0.0/22</code> "
                            "(<code>10.240.2.0/24</code> внутри <code>10.240.0.0/22</code>)"),
                           ("192.168.0.0/23 10.0.2.0/24", ["192.168.0.0/23", "10.0.2.0/24"], "")):
        st = FakeState()
        cb, nav = _acb(fake_bot)
        await sh.gw_slot_lan(cb, GwSlotCB(action="lan_nets", slot=1), services, st)
        msg = _amsg(fake_bot, raw)
        await sh.gateway_lan_received(msg, st, services)
        assert services.db.gateway(1).lan_subnets == kept, raw
        if why:
            assert why in _shown(msg, fake_bot), _shown(msg, fake_bot)


async def test_new_subnets_of_a_slot_go_to_nobody_else(services, slots, fake_bot, monkeypatch):
    """Подсети соседям больше не едут: снаружи шлюза сеть видна по префиксу его
    слота в туннеле. Итог правки не зовёт перевыпускать остальные шлюзы, их
    документ не меняется."""
    _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(1, lan_subnets=["192.168.1.0/24"], lan_mode=1)
    services.db.gateway_update(2, lan_subnets=["192.168.68.0/24"], lan_mode=1)
    services.gwlink_session_opened(2, "3.2.1", 2, [])
    before = services.gwcfg_env(services.db.gateway(2))
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan(cb, GwSlotCB(action="lan_nets", slot=1), services, st)
    msg = _amsg(fake_bot, "192.168.2.0/24")
    await sh.gateway_lan_received(msg, st, services)
    text = _shown(msg, fake_bot)
    assert "✅ Подсети NASPi: <code>192.168.2.0/24</code>" in text, text
    assert "остальным шлюзам" not in text and "перевыпуск" not in text, text
    assert services.gwcfg_env(services.db.gateway(2)) == before


async def test_more_than_64_subnets_are_refused_with_a_reason(services, slots, fake_bot):
    """Документ шлюза принимает не больше 64 подсетей — ввод на ВПС тот же
    предел: лишние не приняты и названы с причиной, первые 64 записаны."""
    _, pi, _ = slots
    _slot1(services, pi)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan(cb, GwSlotCB(action="lan_nets", slot=1), services, st)
    msg = _amsg(fake_bot, " ".join(f"172.16.{i}.0/24" for i in range(66)))
    await sh.gateway_lan_received(msg, st, services)
    assert len(services.db.gateway(1).lan_subnets) == 64
    assert "<code>172.16.64.0/24</code> — у шлюза не больше 64 подсетей" in _shown(msg, fake_bot)


async def test_the_lan_prompt_says_neighbours_only_when_the_networks_are_linked(services, slots, fake_bot):
    """«и из подсетей других шлюзов» и «сетям шлюзов с VPN-транзитом» — только
    при живой связи подсетей; без неё — только устройства админа. То же —
    пример перевода в карточке."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(1, lan_subnets=["192.168.1.0/24"], lan_mode=1)
    services.db.gateway_update(2, lan_subnets=["192.168.68.0/24"], lan_mode=0)
    from awgbot.bot import texts
    st = services.gateway_screen_state(1, lazy_ping=False)
    prompt, card = texts.gateway_lan_text(st), texts.gateway_card_text(st, st["states"])
    assert "Доступ через туннель — только твоим устройствам\n" in prompt + "\n"
    assert "через туннель сеть открывается по адресам в туннеле: 192.168.1.10 → 10.240.1.10" in card
    services.db.gateway_update(2, lan_mode=1)
    st = services.gateway_screen_state(1, lazy_ping=False)
    prompt, card = texts.gateway_lan_text(st), texts.gateway_card_text(st, st["states"])
    assert "Доступ через туннель — только твоим устройствам и подсетям других шлюзов" in prompt
    assert "через туннель и из подсетей других шлюзов сеть открывается" in card
