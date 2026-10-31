"""Загородка бота лишённого кворума лидера (bot/middleware.py, bot/handlers/
fenced.py, bot/routers.py; концепт «кворум» §6.3, §7.2): пользователям, гостям
и новым по приглашению — Т-К9 (кнопка — всплывашка, сообщение — ответ не
чаще раза в минуту на человека), админу — только «🛟 Резерв» и его действия,
прочее — всплывашка Т-К10; админ второго сервера — как прежде; без ограждения
бот ведёт себя как раньше.

События идут через настоящий диспетчер роли (`make_dispatcher`) на заглушке
сессии Bot API из tests/screens/harness.py — в записи то, что ушло бы в
Telegram. «Лишён кворума» — отметка сервисов, которую ставит правило лидера
(`_quo_fenced`, её читает `reserve_quorum_fenced`).

Цена ошибки: пользователь лишённого кворума лидера жмёт кнопку и получает
отказ базы вместо понятной строки (или, хуже, бот пишет решение мимо
ограждения); админ не может открыть «🛟 Резерв», чтобы перевести
пользователей кнопкой; Т-К9 на каждое сообщение — флуд в чате человека;
загородка, не снятая после возврата кворума, — бот молчит, когда его все видят.
Загородка-перечень пропускала кнопки, которых в перечне не было: «🗑 Удалить»
снимал пир на хосте, а строку в базе под следованием уже не удалял (пир снят,
устройство в списке, спиннер); «🔁 Повторить» на уведомлении запускал догон
шлюзов, «↩️ Вернуть запись» переводил запись DNS на сервер без кворума — поэтому
загородка — белый список, и стоит она раньше кнопок уведомлений.
"""
from __future__ import annotations

import pytest

from awgbot.bot import callbacks as cbs
from awgbot.bot.callbacks import (AdminSelfCB, BlockCB, ClientCB, DelDeviceCB, GraceCB, GwSlotCB, HelpCB, Menu, NoteCB,
                                  PauseCB, PresetCB, ReassignCB, RsvCB, SetCB, UpdateCB)
from awgbot.bot.handlers import fenced
from awgbot.bot.middleware import AccessMiddleware
from awgbot.bot.routers import make_dispatcher
from awgbot.bot.texts.reserve import quorum as qt
from awgbot.core import access_cache, config
from tests.integration import _reserve_common as rc
from tests.screens import base, harness

pytestmark = pytest.mark.e2e

USER = 5000


def _fence(services, on: bool = True) -> None:
    services.__dict__["_quo_fenced"] = bool(on)


@pytest.fixture()
def world(services, make_active_client):
    access_cache.invalidate_all()
    make_active_client(name="Вера", tg_id=USER)
    yield services
    access_cache.invalidate_all()


async def _press(services, uid: int, cb, *, role: str = "client") -> list:
    dp = make_dispatcher(role, services, services.db, reattach=True)
    session = harness.StubSession(uid, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=uid, name="U", steps=[("press", cb)], services=services)
    return rec.calls


async def _say(services, uid: int, *texts: str) -> list:
    dp = make_dispatcher("client", services, services.db, reattach=True)
    session = harness.StubSession(uid, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=uid, name="U", steps=[("text", t) for t in texts], services=services)
    return rec.everything


def _toasts(calls) -> list[str]:
    return [c.head for c in calls if c.head.startswith("~")]


def _sends(calls) -> list[str]:
    return [c.body for c in calls if c.head.startswith("+ send")]


# ── пользователи ────────────────────────────────────────────────────────────

async def test_a_user_button_gets_the_t_k9_alert(world):
    _fence(world)
    calls = await _press(world, USER, Menu(action="main"))
    assert _toasts(calls) == [f"~! {qt.USER_FENCED}"], [c.head for c in calls]
    assert not _sends(calls) and not [c for c in calls if c.head.startswith("= edit")], "лишённый кворума нарисовал экран"


async def test_a_user_message_gets_one_t_k9_answer_per_minute(world):
    _fence(world)
    calls = await _say(world, USER, "привет", "ау")
    assert _sends(calls) == [qt.USER_FENCED], f"Т-К9 не один раз за минуту: {_sends(calls)}"


async def test_a_stranger_with_an_invite_gets_t_k9_not_the_activation(world):
    _fence(world)
    calls = await _say(world, 8888, "/start CabcABC12345")
    assert _sends(calls) == [qt.USER_FENCED], _sends(calls)


async def test_the_answer_comes_again_after_a_minute(world):
    """Окно «раз в минуту» — на человека; минута прошла — ответ снова."""
    _fence(world)
    mw = AccessMiddleware(world.db, fenced=lambda: True)
    session = harness.StubSession(USER, base.NOW, base.PEERS)
    bot = harness.make_bot(session)
    from aiogram.types import User

    async def handler(event, data):
        return "HANDLED"

    for n in range(3):
        msg = harness.message_update(n + 1, session, USER, "U", "привет", n + 1).message.as_(bot)
        assert await mw(handler, msg, {"event_from_user": User(id=USER, is_bot=False, first_name="U")}) is None
        if n == 1:
            mw._fenced_told[USER] -= 61                     # прошла минута
    assert [c.body for c in session.calls] == [qt.USER_FENCED, qt.USER_FENCED], \
        "после минуты Т-К9 не пришёл снова (или пришёл на каждое сообщение)"


async def test_without_the_fence_a_user_gets_the_menu(world):
    _fence(world, False)
    calls = await _press(world, USER, Menu(action="main"))
    assert qt.USER_FENCED not in " ".join(_toasts(calls)), "без ограждения пользователю — Т-К9"
    assert [c for c in calls if c.head.startswith(("= edit", "+ send"))], "без ограждения меню не нарисовано"


async def test_the_fence_lifted_lets_the_user_in_again(world):
    _fence(world)
    await _press(world, USER, Menu(action="main"))
    _fence(world, False)
    calls = await _press(world, USER, Menu(action="main"))
    assert not _toasts(calls) or qt.USER_FENCED not in _toasts(calls)[0], "кворум вернулся, а Т-К9 остался"


# ── админ ───────────────────────────────────────────────────────────────────

OUTSIDE = [SetCB(sec="gw"), Menu(action="clients"), ClientCB(action="open", client_id=1), GwSlotCB(action="list"),
           DelDeviceCB(device_id=1, stage="confirm"), BlockCB(target="dev", action="block", ref=1, kind="silent"),
           PauseCB(action="pick", ref=1, days=7), PresetCB(kind="devlimit", ref=1, val=5),
           ReassignCB(device_id=1, client_id=1), AdminSelfCB(action="add"), GraceCB(action="take", ref=1),
           HelpCB(platform="apple"), NoteCB(kind="gw_retry", ref=1), NoteCB(kind="rsv_record_back"),
           NoteCB(kind="extend", ref=1), NoteCB(kind="unassigned"), "zz:old:button"]


@pytest.mark.parametrize("cb", OUTSIDE, ids=lambda c: c if isinstance(c, str) else c.pack())
async def test_the_admin_outside_the_reserve_section_gets_the_t_k10_alert(world, cb):
    _fence(world)
    calls = await _press(world, config.ADMIN_ID, cb)
    assert _toasts(calls) == [f"~! {qt.ADMIN_FENCED}"], [c.head for c in calls]
    assert not [c for c in calls if c.head.startswith(("= edit", "+ send", "= markup"))], \
        "за загородкой нарисован экран или сняты кнопки уведомления"


async def test_the_notification_buttons_do_not_reach_the_gateways_or_the_record(world, monkeypatch):
    """«🔁 Повторить» и «↩️ Вернуть запись» лишённого кворума: ни сброса
    расписания догона и такта набора, ни смены записи DNS."""
    hits = []
    monkeypatch.setattr(type(world), "fleet_gw_retry", lambda self, slot: hits.append(("retry", slot)) or "")
    monkeypatch.setattr(type(world), "reserve_dns_switch", lambda self, ip: hits.append(("dns", ip)))
    from awgbot.runtime.linkserver import upd as fleet_upd

    async def tick(services):
        hits.append(("tick",))
    monkeypatch.setattr(fleet_upd, "fleet_run_tick", tick)
    _fence(world)
    for cb in (NoteCB(kind="gw_retry", ref=1), NoteCB(kind="rsv_record_back")):
        calls = await _press(world, config.ADMIN_ID, cb)
        assert _toasts(calls) == [f"~! {qt.ADMIN_FENCED}"], [c.head for c in calls]
    assert hits == [], f"кнопка уведомления прошла загородку: {hits}"


async def test_deleting_a_device_while_fenced_leaves_the_peer_and_the_row(world, fake_awg):
    """Ровно то, что было: пир снимался на хосте, строка под следованием
    оставалась. Теперь до хоста кнопка не доходит."""
    client = world.db.get_client_by_tg(USER)
    dev = world.add_device(client.id, "Телефон")
    peers = dict(fake_awg.peers)
    _fence(world)
    calls = await _press(world, config.ADMIN_ID, DelDeviceCB(device_id=dev.device_id, stage="confirm"))
    assert _toasts(calls) == [f"~! {qt.ADMIN_FENCED}"], [c.head for c in calls]
    assert world.db.get_device(dev.device_id) is not None, "устройство удалено за загородкой"
    assert fake_awg.peers == peers, "пир снят на хосте за загородкой"


def _sample(cls):
    """Экземпляр колбэка с заполненными обязательными полями."""
    vals = {}
    for name, f in cls.model_fields.items():
        if f.is_required():
            vals[name] = 1 if f.annotation is int else "x"
    return cls(**vals)


_ALL_CB = sorted((c for c in vars(cbs).values()
                  if isinstance(c, type) and issubclass(c, cbs.CallbackData) and c is not cbs.CallbackData),
                 key=lambda c: c.__name__)


@pytest.mark.parametrize("cls", _ALL_CB, ids=lambda c: c.__name__)
def test_every_button_class_is_closed_unless_it_is_on_the_white_list(cls):
    """Новый класс кнопок закрыт сам: открыты только раздел «🛟 Резерв», экран
    админа второго сервера и служебные (листание, «весь список», «Скрыть»,
    «✖️ Отмена»); из настроек — корень и «🛟 Резерв», из меню — главная."""
    if cls in fenced.FREE:
        assert fenced.allowed(_sample(cls).pack())
        return
    assert not fenced.allowed(_sample(cls).pack()), f"{cls.__name__} открыт лишённому кворума"
    if cls is SetCB:
        assert fenced.allowed(SetCB(sec="rsv", act="do", key="x").pack()) and fenced.allowed(SetCB(sec="root").pack())
    if cls is Menu:
        assert fenced.allowed(Menu(action="main").pack()) and fenced.allowed(Menu(action="refresh").pack())


async def test_the_reserve_card_buttons_pass_the_fence(world):
    """Действия карточки резерва (RsvCB) — к своим обработчикам, не Т-К10."""
    _fence(world)
    calls = await _press(world, config.ADMIN_ID, RsvCB(action="refresh"))
    assert f"~! {qt.ADMIN_FENCED}" not in _toasts(calls), [c.head for c in calls]


@pytest.mark.parametrize("cb", [SetCB(sec="svc"), SetCB(sec="backup"), SetCB(sec="notify"),
                                UpdateCB(action="install")])
async def test_the_shared_sections_are_fenced_too(world, cb):
    """Концепт «кворум» §6.3: админу лишённого кворума — только «🛟 Резерв»;
    «🔧 Сервис» (перезапуск, ключи линка), «💾 Бэкапы», «⬆️ Обновления» —
    действия на хосте и в базе, их тоже закрывает Т-К10. Общие разделы стоят
    в диспетчере раньше загородки."""
    _fence(world)
    calls = await _press(world, config.ADMIN_ID, cb)
    assert _toasts(calls) == [f"~! {qt.ADMIN_FENCED}"], f"{cb.pack()} прошёл загородку: {[c.head for c in calls]}"


@pytest.mark.parametrize("cb", [SetCB(sec="rsv"), SetCB(sec="root"), Menu(action="main")])
async def test_the_admin_reaches_the_reserve_section_and_the_main_menu(world, cb):
    _fence(world)
    calls = await _press(world, config.ADMIN_ID, cb)
    assert f"~! {qt.ADMIN_FENCED}" not in _toasts(calls), f"{cb.pack()} перехвачен загородкой"
    assert qt.USER_FENCED not in " ".join(_toasts(calls)), "админу — строка пользователей"
    assert [c for c in calls if c.head.startswith(("= edit", "+ send"))], f"{cb.pack()}: экран не нарисован"


async def test_without_the_fence_the_admin_router_is_transparent(world):
    _fence(world, False)
    calls = await _press(world, config.ADMIN_ID, SetCB(sec="gw"))
    assert f"~! {qt.ADMIN_FENCED}" not in _toasts(calls), "без ограждения админ получил Т-К10"
    assert [c for c in calls if c.head.startswith(("= edit", "+ send"))], "без ограждения раздел не открылся"


async def test_a_document_from_the_fenced_admin_is_answered_with_t_k10(world):
    _fence(world)
    dp = make_dispatcher("client", world, world.db, reattach=True)
    session = harness.StubSession(config.ADMIN_ID, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=config.ADMIN_ID, name="A", steps=[("file", "backup.tar", b"DUMMY")],
                            services=world)
    assert _sends(rec.calls) == [qt.ADMIN_FENCED], [c.head for c in rec.calls]


# ── админ второго сервера ───────────────────────────────────────────────────

async def test_the_peer_admin_is_not_fenced(world):
    rc.pair_row(world)
    access_cache.invalidate_all()
    _fence(world)
    mw = AccessMiddleware(world.db, fenced=lambda: True)
    seen = {}

    async def handler(event, data):
        seen.update(data)
        return "HANDLED"

    from aiogram.types import User
    session = harness.StubSession(rc.PEER_ADMIN, base.NOW, base.PEERS)
    msg = harness.message_update(1, session, rc.PEER_ADMIN, "P", "/start", 1).message.as_(harness.make_bot(session))
    out = await mw(handler, msg, {"event_from_user": User(id=rc.PEER_ADMIN, is_bot=False, first_name="P")})
    assert out == "HANDLED" and seen.get("role") == "peer_admin", "админ второго сервера попал под загородку"
    assert not session.calls


async def test_the_admin_passes_the_middleware_while_fenced(world):
    mw = AccessMiddleware(world.db, fenced=lambda: True)
    from aiogram.types import User
    session = harness.StubSession(config.ADMIN_ID, base.NOW, base.PEERS)
    msg = harness.message_update(1, session, config.ADMIN_ID, "A", "/start", 1).message.as_(harness.make_bot(session))

    async def handler(event, data):
        return data.get("role")

    assert await mw(handler, msg, {"event_from_user": User(id=config.ADMIN_ID, is_bot=False, first_name="A")}) == "admin"


async def test_a_broken_fence_predicate_lets_people_in(world):
    """Сбой чтения отметки не должен запирать бот: молчание хуже, чем решение,
    которое и так остановит база."""
    def boom():
        raise RuntimeError("DUMMY")
    mw = AccessMiddleware(world.db, fenced=boom)
    from aiogram.types import User
    session = harness.StubSession(USER, base.NOW, base.PEERS)
    msg = harness.message_update(1, session, USER, "U", "привет", 1).message.as_(harness.make_bot(session))

    async def handler(event, data):
        return data.get("role")

    assert await mw(handler, msg, {"event_from_user": User(id=USER, is_bot=False, first_name="U")}) == "client"


# ── бот основы на резерве ───────────────────────────────────────────────────

async def test_the_fenced_guest_leader_refuses_users_with_t_k9(world, monkeypatch):
    """Концепт «кворум» §6.3 — лишённый кворума лидер-гость (бот «Alpha» на
    «Beta») тоже лидер своей линии: его пользователям — Т-К9, как у основы."""
    monkeypatch.setattr(config, "GUEST", True)
    _fence(world)
    calls = await _press(world, USER, Menu(action="main"), role="guest")
    assert _toasts(calls) == [f"~! {qt.USER_FENCED}"], \
        f"пользователь лишённого кворума гостя прошёл загородку: {[c.head for c in calls]}"
