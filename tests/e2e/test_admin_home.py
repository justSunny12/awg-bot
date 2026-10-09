"""E2E: главная администратора — шапка строками по условиям, восемь кнопок,
ссылки шапки и уведомлений (/start online|expiring|unassigned|upd|cl-|dev-|
gw-|extend-|traffic…), «⬆️ Доступна vX» из периодической проверки, списки
«Онлайн» и «Истекают».

Цена ошибки: строка «Истекают» или «Без профиля» пропала при непустом
списке — админ не узнает о чужом пире и о людях без доступа; строка висит при
пустом — шум; ссылка из уведомления не открывает экран — кнопок на
уведомлении больше нет, путь к объекту — только ссылка.

Экраны в типичных состояниях (главная, ссылки /start, «Онлайн», «Истекают»,
«Без профиля») сверяет эталон adm.main*, adm.link.*, adm.online*,
adm.expiring*, в том числе тихая главная с выключенным РФ-доступом, «Онлайн:
0» рядом со ссылками, версия и переезд, сервер и шлюз не отвечают, шлюз в
«Онлайн», двое истекающих; здесь — побочные эффекты, атомы форматтеров и
разбор ссылок.
"""
import datetime
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject

from awgbot.bot import texts
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers.admin.panel import parse_link
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
BOT = "awg_test_bot"


def _cmd(args):
    return CommandObject(prefix="/", command="start", args=args)


def _expiring(services, make_active_client, name, tg_id, days=2):
    c = make_active_client(name, tg_id=tg_id)
    now = timeutil.now()
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - datetime.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + datetime.timedelta(days=days)),
                                     period_kind="month")
    return services.db.get_client(c.id)


# ── шапка ────────────────────────────────────────────────────────────────────

async def test_periodic_check_records_the_found_version_even_when_muted(services, monkeypatch):
    """«⬆️ Доступна vX» на главной (снимок adm.main.update) берётся из ключа,
    который пишет периодическая проверка (update_scan), а не из тега
    уведомления: он пишется и при выключенных уведомлениях. Нечего ставить —
    ключ пуст, и строка уходит."""
    monkeypatch.setattr(services, "update_next", lambda: SimpleNamespace(tag="v3.2.0"))
    monkeypatch.setattr(services, "updates_muted", lambda: True)
    assert services.update_scan().tag == "v3.2.0"
    assert services.update_to_notify() is None, "уведомления выключены — уведомлять нечего"
    assert services.update_available_tag() == "v3.2.0", "проверка не записала найденную версию"

    monkeypatch.setattr(services, "update_next", lambda: None)
    services.update_scan()
    assert services.update_available_tag() == "", "обновились — строка должна уйти"


def test_release_url_points_to_the_tag():
    assert texts.release_url("v3.2.0") == "https://github.com/justSunny12/awg-bot/releases/tag/v3.2.0"


def test_home_routing_line_is_silent_without_gateways():
    """«🇷🇺 РФ-доступ: 🔴 недоступен» без единого шлюза — шум: сказать нечего,
    добавить шлюз можно в «🛰 Шлюзы» (шлюз, который не отвечает, — снимок
    adm.main.down)."""
    assert texts.routing_admin_status_line({"ok": False, "active": "", "standby": []}) == ""
    out = texts.admin_panel({"ok": True}, routing_info={"ok": False, "active": "", "standby": []})
    assert "РФ-доступ" not in out, out


# ── ссылки ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload, link", [
    ("online", ("online", 0)), ("expiring", ("expiring", 0)), ("unassigned", ("unassigned", 0)),
    ("upd", ("upd", 0)), ("traffic", ("traffic", 0)), ("traffic_local", ("traffic", 0)),
    ("traffic-7", ("traffic_dev", 7)), ("traffic_local-7", ("traffic_dev", 7)),
    ("traffic_local-7-t", ("traffic_dev", 7)), ("cl-12", ("cl", 12)), ("dev-5", ("dev", 5)),
    ("gw-2", ("gw", 2)), ("extend-3", ("extend", 3)),
    ("cl-", None), ("cl-abc", None), ("dev-5-x", None), ("Cabcdefghijk", None), ("", None),
])
def test_parse_link_maps_payloads_to_screens(payload, link):
    """Разбор ссылки шапки и уведомлений: известные виды — на свои экраны;
    мусор и коды приглашений (12 знаков с C/F) — не ссылка."""
    assert parse_link(payload) == link


async def test_link_extend_remembers_to_return_to_the_expiring_list(services, fake_bot, make_active_client):
    """«extend-<id>» (из списка истекающих прежнего образца): после продления —
    список истекающих, пока в нём кто-то есть (экран — снимок adm.link.extend)."""
    a = _expiring(services, make_active_client, "Аня", 3301)
    _expiring(services, make_active_client, "Боря", 3302, days=3)
    services.bot_username = BOT
    services.db.set_nav_message_id(ADMIN, None)
    state = FakeState()
    msg = FakeMessage(text=f"/start extend-{a.id}", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await ah.admin_start(msg, services, state, command=_cmd(f"extend-{a.id}"))
    assert [t for kind, t, _ in msg.sent if kind == "answer"], "по ссылке ничего не пришло"
    assert (await state.get_data()).get("return_to") == "expiring", \
        "после продления админ вернётся не в список истекающих"
