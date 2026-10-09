"""Экран «🛰 Шлюзы» и слоты — что нажатия меняют в слотах, настройках, ключах
линка и выданных файлах: переключение в обе стороны, «⭐ При старте», пинг,
подсети, подпись, добавление второго слота с токеном, снятие резервного и
активного, «⚙️ Параметры» циклами, «🔀 VPN-транзит», «↔️ Связь подсетей» во
всех состояниях, напоминания о перевыпуске. Сами экраны (список, карточки,
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
    services.gateway_set_home_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(2, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert _rows(markup) == [["▶️ Сделать активным"], ["📤 Конфигурация", "📡 Пинг"],
                             ["✅ VPN-транзит", "🗺 Подсети"], ["❓ Роутер", "✏️ Изменить"], ["⬅️ Назад"]]
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


async def test_home_subnets_and_label_inputs(services, slots, fake_bot):
    """«🗺 Подсети»: принятое сохраняется, мусор — нет; «✏️ Подпись»
    сохраняется, «—» её убирает. Итоги («⚠️ Не принято: …» первыми строками
    карточки, «✅ Подпись: дача → —») — эталоны adm.gw.home.partial и
    adm.gw.label.clear."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_home(cb, GwSlotCB(action="home", slot=1), services, st)
    msg = _amsg(fake_bot, "192.168.1.0/24 мусор")
    await sh.gateway_home_received(msg, st, services)
    assert services.db.gateway(1).home_subnets == ["192.168.1.0/24"], "мусор записан в подсети или принятое потеряно"
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
    """Отказ сервиса (слот исчез, пока приглашение висело): ввод не закрыт —
    можно ответить ещё раз — и ничего не убираем. Ответ «⚠️ …» — эталон
    adm.gw.label.gone."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    services.db.gateway_delete(2)
    msg = _amsg(fake_bot, "дом 2")
    await sh.gateway_label_received(msg, st, services)
    assert await st.get_state() is not None, "ввод открыт — можно ответить ещё раз"
    assert not [r for r in fake_bot.records if r[0] == "delete_message"], "до успеха ничего не убираем"


async def test_add_second_slot_as_new_machine_asks_its_own_token(services, slots, fake_bot):
    """«➕ Новое устройство» в новый слот — сразу к выпуску, без подтверждения:
    у второго шлюза свой бот, токен спрашивается здесь один раз и уезжает
    в файл именно второго слота. Приглашение «Токен бота шлюза 2», файл
    «Шлюз 2» и скрипт второго слота — эталоны adm.rt.new_ask.standby и
    adm.rt.token.done.standby."""
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
    gws = services.db.gateways()
    assert [g.id for g in gws] == [1, 2] and services.db.get_device(gws[1].device_id).name == "Шлюз 2"
    assert services.runs[-1][0] == "--apply" and services.runs[-1][1]["LINK_IF"] == "awglink2"
    assert len([s for s in msg.sent if s[0] == "document"]) == 1, "файл первого применения не выдан"


async def test_replacing_a_machine_still_asks_first(services, slots, fake_bot):
    """Замена машины в занятом слоте — сначала вопрос: прежняя машина потеряет
    линк, поэтому сам вопрос ничего не выпускает."""
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=1), services, FakeState())
    assert services.runs == [], "вопрос ничего не выпустил"


async def test_add_second_slot_from_my_devices(services, slots, fake_bot):
    """Второй слот из «моих устройств» при известном токене: машина
    назначена во второй слот, файл первого применения выдан. Список без
    назначенного, вопрос «станет резервным» и подпись файла — эталоны
    adm.rt.pick_list.standby, adm.rt.pick.standby, adm.rt.mark_yes.standby."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[2] = "222222222:BB-second-token-value-long-enough"
    cb, nav = _acb(fake_bot)
    await sh.gateway_pick(cb, GwMarkCB(action="pick", device_id=pi2.id, slot=0), services)
    assert services.db.gateway_by_device(pi2.id) is None, "вопрос «станет шлюзом?» уже назначил"
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == 2
    assert len([s for s in nav.sent if s[0] == "document"]) == 1, f"файл первого применения не выдан: {nav.sent}"


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
    assert text.startswith("🔴 Выключить РФ-доступ для всех?") and labels == ["⬅️ Отмена", "🔴 Выключить"]
    markup = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert markup.inline_keyboard[0][0].callback_data == SetCB(sec="rt_params", act="open").pack()


# ── строка РФ-доступа в шапке админа ─────────────────────────────────────────
# Шапка админа во всех состояниях активного и резерва (жив, проверяется, не
# отвечает, оба лежат, активен слот 2, подпись слота) — эталоны adm.main.gw*.

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

async def test_lan_mode_toggles_and_the_router_button_follows_it(services, slots, fake_bot, monkeypatch):
    """«Включить» пишет режим в слот, «Выключить» снимает. «❓ Роутер» в
    карточке — только пока режим включён (рецепт для выключенного транзита
    сломал бы сеть): эталоны adm.gw.lan_yes и adm.gw.lan_yes.off."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda gw: "")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1, "режим не включён"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0, "режим не выключен"


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
    assert res["conflict"] is None and res["peer_others"] == ["NASPi"], "кому перевыпускать"
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
    """Включение пишет настройку, выключение — снимает. Рабочая связь парами
    «имя (подпись): подсеть» в «Шлюзах», «↔️ Связь подсетей ✅» в карточке,
    вопрос и всплывашка выключения, карточка без строки связи — эталоны
    adm.rt.peers.ok, adm.gw.card.peers, adm.gw.peer_ask.off,
    adm.gw.peer_yes.off, adm.gw.card.own.pending."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes", val="1"), services)
    assert store["app.routing.peer_nets.enabled"] is True, "связь подсетей не включена"
    # выключение: вопрос ничего не меняет, подтверждение — выключает
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    assert store["app.routing.peer_nets.enabled"] is True, "вопрос о выключении уже выключил"
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes"), services)
    assert store["app.routing.peer_nets.enabled"] is False, "связь подсетей не выключена"


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


@pytest.mark.parametrize("state, enabled", [
    ("off", False), ("no_lan", True), ("no_nets", True), ("overlap", True), ("ok", True)])
async def test_peer_nets_info_follows_the_slots_in_every_state(services, slots, monkeypatch, state, enabled):
    """Состояние связи подсетей — из слотов: тумблер, VPN-транзит, подсети,
    пересечение. Строка «Шлюзов» в каждом из пяти состояний — эталоны
    adm.rt.two (выключена) и adm.rt.peers.*."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    store["app.routing.peer_nets.enabled"] = enabled
    _peer_line_state(services, state)
    info = services.gateway_peer_nets_info()
    assert info["state"] == state, info


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
    """«Выбрать все» по правилу массового выбора — ☑️ выдаёт всем, ✅
    снимает со всех; каждый, кому выдали, получает уведомление один раз.
    Экран и всплывашки — эталоны adm.rt.users.all и adm.rt.users.all.off."""
    a = make_active_client(name="Аня", tg_id=4101)
    b = make_active_client(name="Боря", tg_id=4102)
    before = len([r for r in fake_bot.records if r[0] == "send_message"])
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services)
    assert services.db.get_client(a.id).routing_allowed and services.db.get_client(b.id).routing_allowed
    notified = [r for r in fake_bot.records if r[0] == "send_message"][before:]
    assert sorted(r[1] for r in notified) == [4101, 4102], "уведомление — каждому, по одному"
    cb, nav = _acb(fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow_all"), services)
    assert not services.db.get_client(a.id).routing_allowed and not services.db.get_client(b.id).routing_allowed


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
    assert len(cb.answers) == 1, f"колбэку ответили не один раз: {cb.answers}"


async def test_transit_confirmation_carries_the_target_state(services, slots, fake_bot, monkeypatch):
    """«Включить/Выключить» без цели брало «не текущее»: двойное нажатие
    выключало только что включённое. Цель едет в кнопке; повтор той же
    кнопки при уже включённом режиме ничего не переключает («Уже включено» —
    эталон adm.gw.lan_yes.again)."""
    _, pi, _pi2 = slots
    _slot1(services, pi)
    calls = []
    monkeypatch.setattr(services, "gateway_set_lan_mode", lambda slot, on: calls.append((slot, on)) or {})
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)]
    # второе нажатие той же кнопки (цель «включить», а режим уже включён) — ничего не переключает
    services.db.gateway_update(1, lan_mode=1)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1, val="1"), services)
    assert calls == [(1, True)], f"повтор кнопки переключил режим ещё раз: {calls}"


async def test_no_free_slot_is_an_alert_not_a_spinning_button(services, slots, fake_bot, monkeypatch):
    """gateway_next_slot бросает ServiceError (слоты заняты, gateways_max поднят
    без портов/подсетей): раньше никто не ловил — кнопка крутилась, экрана не было.
    Отказ всплывашкой — эталон adm.rt.new_ask.full; диалог ввода не открыт."""
    from awgbot.domain.services import ServiceError
    _, pi, pi2 = slots
    _slot1(services, pi)
    monkeypatch.setattr(services, "gateway_next_slot", lambda: (_ for _ in ()).throw(ServiceError("свободных слотов нет")))
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
