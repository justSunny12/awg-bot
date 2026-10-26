"""Бот основы на резерве (гостевой режим, config.GUEST): роутеры, загородка
хостовых действий, словарь роли, главный экран и «⚙️ Настройки», конфигурация
старого устройства, отказы хостовых сервисов, перезапуск клона.

Прогон идёт через настоящий диспетчер роли `guest` (AccessMiddleware, фильтры,
разбор упакованных колбэков) на заглушке сессии Bot API: что бот после этого
сказал Telegram — то и проверяется.

Цена ошибки: кнопка шлюзов, обновлений, бэкапов или «Сервиса», дошедшая до
хендлера в боте на резерве, действует на хост резерва с данными основы
(обновляет чужой сервер, ставит чужие слоты, шлёт чужие бэкапы); хендлер
админа раньше загородки — то же через другую дверь; роутер админа второго
сервера в боте основы — чужой человек получает экран пары; `awg-quick` в
песочнице клона — клон не поднимется, VPN клиентов основы встанет на резерве
в самый момент аварии.
"""
from __future__ import annotations

import datetime

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User

from awgbot.bot import roles
from awgbot.bot.callbacks import GwJoinCB, GwMarkCB, GwSlotCB, RsvCB, SetCB, UpdateCB
from awgbot.bot.routers import make_dispatcher, routers_for
from awgbot.core import config
from awgbot.domain.services.reserve import guest as guest_svc
from awgbot.domain.services.types import ServiceError
from tests.e2e.test_client_entry_points import _Session

pytestmark = pytest.mark.e2e


@pytest.fixture()
def guest_mode(monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)


# ── словарь роли и роутеры ───────────────────────────────────────────────────

def test_the_role_dictionary_follows_the_guest_flag(monkeypatch):
    assert roles.current() is roles.MAIN
    monkeypatch.setattr(config, "GUEST", True)
    assert roles.current() is roles.GUEST
    monkeypatch.setattr(config, "ROLE", "gateway")
    assert roles.current() is roles.GATEWAY, "агент шлюза не бывает гостем"


def test_the_guest_dictionary_has_only_the_reserve_card_in_settings_and_no_host_blocks():
    g = roles.GUEST
    assert g.settings_root == ("rsv",) and g.subsections == () and g.root_labels == {"rsv": "🛟 Резерв"}
    assert (g.has.fleet, g.has.migration, g.has.gw_emergency) == (False, False, False)
    assert g.root_text, "корень настроек без своего текста покажет список разделов основы"
    assert roles.MAIN.root_text == "" and roles.MAIN.has.fleet, "словарь основы не должен измениться"


def test_a_guest_installation_must_be_a_main_bot(monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(config, "ROLE", "gateway")
    with pytest.raises(RuntimeError, match="Гостевой"):
        config.validate()


def test_guest_routers_keep_the_client_ones_drop_the_peer_admin_and_put_the_fence_before_the_admin():
    guest = [r.name for r in routers_for("guest")]
    main = [r.name for r in routers_for("client")]
    assert "peeradmin" not in guest and "peeradmin" in main, "экран админа второго сервера — не для бота основы"
    assert guest.index("guest") < guest.index("admin"), f"загородка после админских хендлеров: {guest}"
    assert guest.index("guest") < guest.index("settings")
    assert guest.index("sections") < guest.index("guest"), "общие разделы стоят раньше загородки: их фильтр ролевой"
    assert guest[-1] == "stale" and guest[:3] == main[:3]
    # роль админа второго сервера целиком — экран и ввод аварийного кода — у бота основы на резерве не живёт
    assert set(main) - set(guest) == {"peeradmin", "peercode"}, (
        f"у гостя лишилось не только роль админа второго сервера: {set(main) ^ set(guest)}")
    assert "peercode" not in guest, "ввод аварийного кода в боте основы на резерве"
    assert {"client", "friend", "routing", "guide"} <= set(guest), "клиентские роутеры целиком"


# ── сквозь диспетчер ─────────────────────────────────────────────────────────

class _Run:
    def __init__(self, services):
        self.dp = make_dispatcher("guest", services, services.db, reattach=True)
        self.session = _Session()
        self.bot = Bot("42:DUMMY", session=self.session)
        self.uid = config.ADMIN_ID
        self.n = 0

    def _msg(self, **extra):
        self.n += 1
        return Message(message_id=100 + self.n, date=datetime.datetime.now(), chat=Chat(id=self.uid, type="private"),
                       **extra)

    async def press(self, cbdata) -> list:
        self.session.calls.clear()
        data = cbdata if isinstance(cbdata, str) else cbdata.pack()
        upd = Update(update_id=self.n + 1, callback_query=CallbackQuery(
            id=f"q{self.n}", chat_instance="ci", **{"from": User(id=self.uid, is_bot=False, first_name="A")},
            message=self._msg(text="меню"), data=data))
        await self.dp.feed_update(self.bot, upd)
        return list(self.session.calls)

    async def send_document(self) -> list:
        self.session.calls.clear()
        doc = Document(file_id="DUMMY", file_unique_id="DUMMY", file_name="backup.tar")
        upd = Update(update_id=self.n + 1, message=self._msg(document=doc, from_user=User(id=self.uid, is_bot=False, first_name="A")))
        await self.dp.feed_update(self.bot, upd)
        return list(self.session.calls)


@pytest.fixture()
def run(services, guest_mode):
    return _Run(services)


def _kinds(calls) -> list[str]:
    return [type(c).__name__ for c in calls]


HOST_ACTIONS = [
    SetCB(sec="srv"), SetCB(sec="fw"), SetCB(sec="backup"), SetCB(sec="svc"), SetCB(sec="upd"), SetCB(sec="rt"),
    SetCB(sec="mig"), SetCB(sec="email"), SetCB(sec="subs"), SetCB(sec="mon"), SetCB(sec="notify"),
    SetCB(sec="svc", act="do", key="restart_awg"),
    RsvCB(action="invite"), RsvCB(action="accept"), RsvCB(action="unpair"), RsvCB(action="unpair_yes"),
    RsvCB(action="ready"), RsvCB(action="emerg"), RsvCB(action="new_code"), RsvCB(action="code"),
    UpdateCB(action="install", tag="v9.9.9"), UpdateCB(action="go", tag="v9.9.9"),
    GwJoinCB(action="code"), GwMarkCB(action="new_ask"), GwMarkCB(action="mark_yes", device_id=1, slot=1),
    GwSlotCB(action="list"), GwSlotCB(action="remove_yes", slot=1), GwSlotCB(action="switch_yes", slot=1),
]


@pytest.mark.parametrize("cb", HOST_ACTIONS, ids=lambda c: c.pack())
async def test_every_host_action_gets_the_unavailable_toast_and_changes_nothing(run, cb):
    calls = await run.press(cb)
    assert _kinds(calls) == ["AnswerCallbackQuery"], f"кроме всплывашки бот сделал: {_kinds(calls)}"
    assert calls[0].text == guest_svc.GUEST_REFUSED and calls[0].show_alert is True


async def test_the_reserve_card_and_the_settings_root_are_open(run, services):
    for cb in (SetCB(sec="root"), SetCB(sec="rsv"), RsvCB(action="refresh")):
        calls = await run.press(cb)
        edits = [c for c in calls if type(c).__name__ == "EditMessageText"]
        assert edits and "Резерв" in edits[0].text, f"{cb.pack()}: карточка не показана: {_kinds(calls)}"
        assert not any(getattr(c, "show_alert", False) for c in calls)


async def test_a_file_from_the_admin_is_refused_not_taken_for_a_restore(run):
    calls = await run.send_document()
    assert _kinds(calls) == ["SendMessage"] and calls[0].text == guest_svc.GUEST_REFUSED, _kinds(calls)


async def test_the_refusal_does_not_touch_the_admins_row_of_clients_menu(run, services):
    """Клиентские кнопки админа (профили) живут: загородка берёт только хостовое."""
    calls = await run.press("m:clients")
    texts = [getattr(c, "text", "") or "" for c in calls]
    assert guest_svc.GUEST_REFUSED not in texts, "список профилей на резерве закрыт загородкой"
    assert "EditMessageText" in _kinds(calls)


# ── словарь, экраны, клавиатуры ──────────────────────────────────────────────

async def test_the_settings_root_on_the_reserve_is_the_reserve_card_without_other_sections(services, guest_mode):
    from awgbot.bot import sections
    services.guest_state_in({"name": "Бета", "since": "2026-10-21T03:12:00+05:00", "reason": "dead",
                             "primary": {"status": "dead", "since": "2026-10-21T03:10:00+05:00"}, "own_name": "Альфа"})
    text, markup = await sections.root.screen(roles.GUEST, services)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "Бета" in text and "Альфа" in text and "несёт нагрузку" in text
    assert not any(w in " ".join(labels) for w in ("Сервер", "SSH", "Подписки", "Бэкапы", "Обновления", "Сервис")), labels
    assert any("Резерв" in x for x in labels), labels


def test_the_admin_main_keyboard_has_no_gateways_button_on_the_reserve():
    from awgbot.bot.keyboards.admin import admin_main
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]    # noqa: E731
    assert any("Шлюзы" in x for x in labels(admin_main()))
    guest = labels(admin_main(guest=True))
    assert not any("Шлюзы" in x for x in guest) and any("Настройки" in x for x in guest) and any("Профили" in x for x in guest)


async def test_the_admin_main_screen_opens_with_the_reserve_line(services, guest_mode):
    from awgbot.bot.handlers.admin import panel
    services.guest_state_in({"name": "Бета", "since": "2026-10-21T03:12:00+05:00", "reason": "dead",
                             "primary": {"status": "dead"}, "own_name": "Альфа"})
    parts = await panel._panel_parts(services)
    text = parts[0] if isinstance(parts, tuple) else parts
    assert text.startswith("🛟 Работаю на резерве"), text[:80]
    assert "Бета" in text and "сервер AWG не отвечает" in text


def test_the_main_line_tells_where_the_return_lever_is_only_when_the_primary_is_alive_and_the_lever_is_a_button():
    from awgbot.bot.texts.reserve import guest as gt
    base = {"name": "Бета", "since": "2026-10-21T03:12:00+05:00", "reason": "dead"}
    assert "Вернуть пользователей" not in gt.main_line({**base, "primary": {"status": "dead"}, "return": "button"})
    assert "Вернуть пользователей" not in gt.main_line({**base, "primary": {"status": "alive"}, "return": "immediate"})
    assert "Вернуть пользователей" in gt.main_line({**base, "primary": {"status": "alive"}, "return": "button"})
    assert "Вернуть пользователей" not in gt.main_line({**base, "reason": "nogw", "primary": {"status": "alive"}, "return": "button"}), \
        "шлюзы не на связи — возвращать рано, РФ-доступ идёт через резерв"


def test_the_return_button_is_on_the_card_only_when_the_primary_said_it_is_waiting_for_the_button(services, guest_mode):
    """Кнопка — по слову самой основы (`primary.can_return`), а не по статусу связи и рычагу."""
    from awgbot.bot.keyboards.reserve import guest_card
    labels = lambda m: [b.text for row in m.inline_keyboard for b in row]    # noqa: E731
    assert not any("Вернуть" in x for x in labels(guest_card(can_return=False)))
    assert any("Вернуть" in x for x in labels(guest_card(can_return=True)))
    services.guest_state_in({"primary": {"status": "dead"}, "return": "button"})
    assert services.guest_can_return() is False
    services.guest_state_in({"primary": {"status": "alive", "can_return": False}, "return": "button"})
    assert services.guest_can_return() is False, "основа жива и рычаг «по кнопке», но она ещё не сказала «здорова»"
    services.guest_state_in({"primary": {"status": "alive", "healthy": True, "can_return": True}, "return": "button"})
    assert services.guest_can_return() is True


# ── конфигурация устройства без ключа ────────────────────────────────────────

def test_a_device_without_a_private_key_gets_the_come_back_later_text_on_the_reserve(services, make_active_client, guest_mode):
    client = make_active_client(name="Профиль", tg_id=9001)
    dev = services.add_device(client.id, "Телефон")
    services.db.update_device_fields(dev.device_id, private_key=None)
    with pytest.raises(ServiceError) as e:
        services.generate_config(dev.device_id)
    assert str(e.value) == guest_svc.DEVICE_CONFIG_LATER
    assert "когда сервер вернётся" in str(e.value) and "VPN на нём работает" in str(e.value)


def test_on_the_main_the_same_device_keeps_the_old_explanation(services, make_active_client):
    client = make_active_client(name="Профиль", tg_id=9001)
    dev = services.add_device(client.id, "Телефон")
    services.db.update_device_fields(dev.device_id, private_key=None)
    with pytest.raises(ServiceError) as e:
        services.generate_config(dev.device_id)
    assert "создавал не бот" in str(e.value)


# ── отказы хостовых сервисов ─────────────────────────────────────────────────

def test_every_host_only_name_exists_on_services_so_no_refusal_is_missing(services):
    assert services.guest_install_refusals() == [], "в HOST_ONLY имя, которого у Services нет, — отказ не встанет"
    assert len(set(guest_svc.HOST_ONLY)) == len(guest_svc.HOST_ONLY)


@pytest.mark.parametrize("name", guest_svc.HOST_ONLY)
def test_each_host_service_refuses_in_guest_mode_before_it_does_anything(services, name):
    services.guest_install_refusals()
    method = getattr(services, name)
    for args, kwargs in (((), {}), ((1, "x"), {"force": True})):
        with pytest.raises(ServiceError) as e:
            method(*args, **kwargs)
        assert str(e.value) == guest_svc.GUEST_REFUSED, name


def test_the_refusals_leave_the_clients_services_working(services, make_active_client):
    services.guest_install_refusals()
    client = make_active_client(name="Профиль", tg_id=9002)
    dev = services.add_device(client.id, "Телефон")
    assert dev.device_id and services.db.count_devices(client.id) == 1


def test_firewall_calls_do_not_reach_the_host_in_guest_mode(services, fake_awg):
    services.guest_install_refusals()
    for name in ("firewall_enable", "firewall_disable", "firewall_confirm"):
        with pytest.raises(ServiceError):
            getattr(services, name)()
    assert fake_awg.fw_calls == 0 and fake_awg.fw_applied == 0


# ── перезапуск клона ─────────────────────────────────────────────────────────

class _Cmds:
    def __init__(self, monkeypatch):
        from awgbot.infra import awg
        self.calls: list[list[str]] = []
        monkeypatch.setattr(awg, "_run", lambda cmd, **kw: self.calls.append(list(cmd)), raising=False)


def test_restarting_the_clone_in_the_zone_never_calls_awg_quick(monkeypatch):
    from awgbot.infra import awg
    cmds = _Cmds(monkeypatch)
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    awg.restart_server()
    assert not any(c[0] == "awg-quick" for c in cmds.calls), cmds.calls
    assert cmds.calls == [["ip", "link", "set", "awg0", "down"], ["awg", "setconf", "awg0", config.CONF_PATH],
                          ["ip", "link", "set", "awg0", "up"]], "вниз, конфиг, вверх — в этом порядке"


def test_restarting_on_the_main_still_goes_through_awg_quick(monkeypatch):
    from awgbot.infra import awg
    cmds = _Cmds(monkeypatch)
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    awg.restart_server()
    assert cmds.calls == [["awg-quick", "down", "awg0"], ["awg-quick", "up", "awg0"]]
