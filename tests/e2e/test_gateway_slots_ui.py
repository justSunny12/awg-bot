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


async def test_list_and_card_show_the_label_and_ping_lazily(services, slots, fake_bot):
    """Подпись слота — в скобках и в строке списка, и в заголовке карточки
    (по ней различают две малины); пинг меряется лениво — при первом
    открытии карточки, дальше из кэша, а кнопка «📡 Пинг» меряет заново:
    без кэша каждое открытие карточки ходило бы в линк."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дача")
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, _ = _screen(nav)
    assert "\nPi2 (дача) — 🟢 Резерв" in text, "подпись в скобках, без адреса и кавычек"
    assert services.pings["n"] == 0, "список пингует — должен лениво только карточка"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    text, _ = _screen(nav)
    assert services.pings["n"] == 1, "первое открытие карточки не измерило пинг"
    assert text.split("\n")[0] == "🛰 <b>Pi2 (дача)</b> — 🟢 Резерв", text
    # второе открытие — из кэша
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert services.pings["n"] == 1, "второе открытие снова пошло в линк"
    # кнопка пинга меряет заново — и именно этот слот
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert services.pings["n"] == 2 and cb.answers[0][0].endswith("43 мс"), cb.answers


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
    у ✅ — снять вовсе; после снятия кнопка снова ☑️, всплывашка говорит
    «снят» — иначе холодный старт выберет не тот шлюз, а человек не узнает."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.db.gateway(2).preferred == 1 and services.db.gateway(1).preferred == 0, \
        "звезда не переехала на слот 2"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert services.preferred_gateway() is None, "повторное нажатие не сняло предпочтительного"
    _, labels = _screen(nav)
    assert "⭐ При старте: ☑️" in labels and cb.answers[-1][0] == "Предпочтительный: снят", (labels, cb.answers)


async def test_rename_from_the_edit_screen_renames_the_device_and_closes_the_input(services, slots, fake_bot):
    """«✏️ Имя» — имя устройства-шлюза: приглашение называет слот, ввод
    переименовывает устройство и закрывает диалог (иначе следующее сообщение
    снова уйдёт в имя)."""
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
    assert await st.get_state() is None, "ввод имени не закрыт"


async def test_manual_switch_both_ways_with_confirmation(services, slots, fake_bot):
    """«▶️ Переключить на Pi2» с экрана «Шлюзы»: вопрос ничего не
    переключает, «Отмена» и итог возвращают в «Шлюзы» (откуда пришли), а не
    в карточку; обратно — из карточки бывшего активного; активный —
    «уже идёт», без диалога."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=2, val="l"), services)
    confirm = next(s[2] for s in reversed(nav.sent) if s[0] == "edit_text")
    assert confirm.inline_keyboard[0][0].callback_data == GwSlotCB(action="list").pack(), \
        "со списка — «Отмена» обратно в «Шлюзы»"
    assert confirm.inline_keyboard[0][1].callback_data == GwSlotCB(action="switch_yes", slot=2, val="l").pack()
    assert services.active_gateway().id == 1, "вопрос ничего не переключил"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=2, val="l"), services)
    assert services.active_gateway().id == 2, "трафик не переключён"
    text, _ = _screen(nav)
    assert text.startswith("🛰 <b>Шлюзы</b> · ") and "\nPi2 — " in text and "Активен" in text.split("\n")[2], \
        "со списка — обратно на «Шлюзы», Pi2 теперь активный"
    # обратно — из карточки первого, который теперь в резерве
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_yes(cb, GwSlotCB(action="switch_yes", slot=1), services)
    assert services.active_gateway().id == 1
    # активный — «уже идёт», без диалога
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_switch_ask(cb, GwSlotCB(action="switch_ask", slot=1), services)
    assert not [s for s in nav.sent if s[0] == "edit_text"] and "уже идёт" in cb.answers[0][0]


async def test_home_subnets_and_label_inputs(services, slots, fake_bot):
    """«🗺 Подсети»: принятое сохраняется, непринятое названо в итоге (иначе
    человек думает, что мусор тоже записан); «✏️ Подпись» сохраняется, «—»
    её убирает и итог говорит «→ —»."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_home(cb, GwSlotCB(action="home", slot=1), services, st)
    msg = _amsg(fake_bot, "192.168.1.0/24 мусор")
    await sh.gateway_home_received(msg, st, services)
    assert services.db.gateway(1).home_subnets == ["192.168.1.0/24"]
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1, f"итог правки — первыми строками карточки, а не отдельным сообщением: {answers}"
    assert answers[0].startswith("✅ Подсети NASPi: <code>192.168.1.0/24</code>\n⚠️ Не принято: <code>мусор</code> — "), answers
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
    у второго шлюза свой бот, токен спрашивается здесь один раз и уезжает
    в файл именно второго слота."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=0), services, st)
    text, _ = _screen(nav)
    assert text.startswith("🤖 <b>Токен бота шлюза 2</b> — создай бота у @BotFather"), "без промежуточного подтверждения"
    msg = _amsg(fake_bot, "222222222:BB-second-token-value-long-enough")
    await sh.gateway_token_received(msg, st, services)
    assert services.token[2].startswith("222222222:")
    assert nav.message_id in [r[2] for r in fake_bot.records if r[0] == "delete_message"], \
        "приглашение ввести токен отслужило"
    gws = services.db.gateways()
    assert [g.id for g in gws] == [1, 2] and services.db.get_device(gws[1].device_id).name == "Шлюз 2"
    assert services.runs[-1][0] == "--apply" and services.runs[-1][1]["LINK_IF"] == "awglink2"
    docs = [s for s in msg.sent if s[0] == "document"]
    assert len(docs) == 1 and "<b>Шлюз 2</b>" in docs[0][1], "подпись файла первого применения не называет новый шлюз"
    instr = next(s[1] for s in msg.sent if s[0] == "answer" and "--install" in s[1])
    assert "awg-gw-bundle-awglink2.sh" in instr


async def test_replacing_a_machine_still_asks_first(services, slots, fake_bot):
    """Замена машины в занятом слоте — сначала вопрос: прежняя машина потеряет
    линк, поэтому сам вопрос ничего не выпускает."""
    _, pi, _ = slots
    _slot1(services, pi)
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=1), services, FakeState())
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
    assert text == ("🛰 Pi2 станет шлюзом?\nВыйдет из лимитов; удалить, заблокировать, выдать ссылку будет "
                    "нельзя. Ключи линка — новые, файл первого применения выпущу сразу\n"
                    "Станет резервным: трафик пойдёт через него, только если основной не отвечает"), text
    assert labels == ["⬅️ Отмена", "🛰 Назначить"]
    cb, nav = _acb(fake_bot)
    await sh.gateway_mark_yes(cb, GwMarkCB(action="mark_yes", device_id=pi2.id, slot=0), services, FakeState())
    assert services.db.gateway_by_device(pi2.id).id == 2
    assert any(s[0] == "document" and s[1].startswith("🛰 Файл конфигурации шлюза\n")
               and "<b>Pi2</b>" in s[1] for s in nav.sent), nav.sent


async def test_remove_standby_and_active(services, slots, fake_bot, monkeypatch):
    """Снять резерв — линк откатывается; снять активный — трафик на второй и
    смена ключей; последний шлюз снятый старой кнопкой (по устройству) —
    вопрос и итог говорят, что РФ-доступ выключится до назначения нового."""
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


async def test_bundle_action_is_per_slot(services, slots, fake_bot):
    """«📤 Конфигурация» слота 2 выпускает файл именно слота 2 — подпись
    называет его с подписью слота: перепутанный файл увёл бы чужой ключ
    линка."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дача")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_bundle(cb, GwSlotCB(action="bundle", slot=2), services)
    docs = [s[1] for s in nav.sent if s[0] == "document"]
    assert len(docs) == 1 and docs[0].startswith("📤 <b>Конфигурация шлюза Pi2 (дача)</b>"), docs


async def test_params_screen_cycles_probe_window_threshold_and_lists(services, slots, fake_bot, monkeypatch):
    """«⚙️ Параметры»: такт, окно, порог и период списков — циклами по кругу,
    значение пишется в настройки, экран перерисовывается, всплывашка называет
    новое значение без ключа настройки; порог в строке пересчитывается от
    окна."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    monkeypatch.delattr(services, "_rt_window_size")      # фикстура прибила окно — здесь оно из настроек
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    real_int, real_get = settings.get_int, settings.get
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: store.get(k, real_int(k, d)))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, real_get(k, d)))
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

async def test_lan_mode_toggles_and_the_router_button_follows_it(services, slots, fake_bot, monkeypatch):
    """«Включить» пишет режим в слот, «Выключить» снимает; «❓ Роутер» в
    карточке — только пока режим включён: рецепт для выключенного транзита
    сломал бы сеть."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gateway_set_home_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda gw: "")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 1
    assert "❓ Роутер" in _screen(nav)[1], "режим включён, а рецепта роутера нет"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_lan_ask(cb, GwSlotCB(action="lan_ask", slot=1), services)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=1), services)
    assert services.db.gateway(1).lan_mode == 0
    assert "❓ Роутер" not in _screen(nav)[1], "режим выключен, а рецепт роутера остался"


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
    """Включение пишет настройку; рабочая связь — парами «имя (подпись):
    подсеть» в «Шлюзах» и строкой «↔️ Связь подсетей ✅» в карточке под
    подсетью; выключение — со своим вопросом и всплывашкой о перевыпуске,
    после него строки о связи в карточке нет вовсе."""
    store = _peer_conf(monkeypatch)
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_peer_ask(cb, services)
    await sh.gw_slot_peer_yes(cb, GwSlotCB(action="peer_yes", val="1"), services)
    assert store["app.routing.peer_nets.enabled"] is True
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)
    services.gateway_set_home_subnets(1, "192.168.1.0/24"); services.gateway_set_home_subnets(2, "192.168.68.0/24")
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_list(cb, services, FakeState())
    text, _ = _screen(nav)
    assert "↔️ Связь подсетей: NASPi: <code>192.168.1.0/24</code> ↔️ Pi2: <code>192.168.68.0/24</code>" in text.split("\n"), text
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
    ("ok", True, "↔️ Связь подсетей: NASPi: <code>192.168.1.0/24</code> ↔️ Pi2: <code>192.168.68.0/24</code>"),
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
    """«Автопереключение» на экране «Шлюзы»: выключили — настройка записана,
    строка о последствиях на месте, всплывашка называет состояние; включили
    снова — записано."""
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
    assert store["app.routing.failover.enabled"] is True


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
    снимает со всех; каждый, кому выдали, получает уведомление один раз."""
    a = make_active_client(name="Аня", tg_id=4101)
    b = make_active_client(name="Боря", tg_id=4102)
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
    выключало только что включённое. Цель едет в кнопке; повтор той же
    кнопки при уже включённом режиме ничего не переключает."""
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


async def test_no_free_slot_is_an_alert_not_a_spinning_button(services, slots, fake_bot, monkeypatch):
    """gateway_next_slot бросает ServiceError (слоты заняты, gateways_max поднят
    без портов/подсетей): раньше никто не ловил — кнопка крутилась, экрана не было."""
    from awgbot.domain.services import ServiceError
    _, pi, pi2 = slots
    _slot1(services, pi)
    monkeypatch.setattr(services, "gateway_next_slot", lambda: (_ for _ in ()).throw(ServiceError("свободных слотов нет")))
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_ask(cb, GwMarkCB(action="new_ask", slot=0), services, st)
    assert cb.answers and cb.answers[-1] == ("свободных слотов нет", True), cb.answers
    assert await st.get_state() is None


async def test_a_long_dead_standby_shows_how_long_it_has_been_down(services, slots, fake_bot):
    """«не отвечает 5 мин» вторую неделю — ложь окна замеров: момент падения
    хранится в state, слот показывает «13 дн 4 ч», а оживление его стирает."""
    import time
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    since = services.db.get_state("routing_gw_2_down_since")
    assert since and since.isdigit(), "момент падения не сохранён"
    services.db.set_state("routing_gw_2_down_since", str(int(time.time()) - 13 * 86400 - 4 * 3600))
    text, _ = await sh._screen("rt", services)
    assert "Pi2 — 🔴 Резерв, не отвечает 13 дн 4 ч" in text, text
    services.probe[2] = "ok"
    for _ in range(services._rt_window_size()):           # окно замеров очистилось от отказов
        services.routing_liveness_tick()
    assert not services.db.get_state("routing_gw_2_down_since"), "ожил — момент падения должен стереться"
