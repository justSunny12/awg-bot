"""Роль админа второго сервера (§ роль peer_admin): шаг AccessMiddleware,
место роутера в диспетчере, экран Т-РН1 и сторож «роль ничего не пишет».

Роль получает ровно один аккаунт — `reserve_peer.peer_admin_id`, пришедший
при сопряжении под ключом сопряжения; шаг стоит после админа (совпавший
ADMIN_ID — админ) и раньше разбора пользователей; без пары шага нет;
незнакомцы — как прежде (/start и /code — активация, прочее — молчание).

Цена ошибки: роль у чужого аккаунта или у прежнего после смены админа
второй стороны — посторонний читает состояние этого сервера; роль поверх
своего админа — админ теряет свой бот; обработчик роли, пишущий в базу, —
админ другого сервера меняет данные этого.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from aiogram.types import Chat, Message, User

from awgbot.bot.middleware import AccessMiddleware
from awgbot.core import access_cache, config
from tests.conftest import FakeCallback, FakeMessage
from tests.integration import _reserve_common as rc
from tests.integration._reserve_common import PEER_ADMIN

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
rsv = rc.rsv


async def _role(db, uid: int, text: str = "привет"):
    """Роль, которую middleware даёт апдейту uid (None — молчание)."""
    seen = {}

    async def handler(event, data):
        seen.update(data)
        return "HANDLED"
    user = User.model_construct(id=uid, is_bot=False, first_name="U")
    msg = Message.model_construct(message_id=1, text=text, chat=Chat.model_construct(id=uid, type="private"))
    out = await AccessMiddleware(db)(handler, msg, {"event_from_user": user})
    return seen.get("role") if out == "HANDLED" else None


@pytest.fixture(autouse=True)
def _fresh_cache():
    access_cache.invalidate_all()
    yield
    access_cache.invalidate_all()


# ── шаг middleware ───────────────────────────────────────────────────────────

async def test_without_a_pair_the_peer_admin_is_a_stranger(db):
    assert await _role(db, PEER_ADMIN) is None
    assert await _role(db, PEER_ADMIN, "/start") == "activation"


async def test_the_bound_account_gets_the_role_and_nobody_else(services, rsv):
    rc.pair_row(services)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"
    assert await _role(services.db, PEER_ADMIN + 1) is None, "чужой аккаунт получил ответ"
    assert await _role(services.db, PEER_ADMIN + 1, "/start CabcABC12345") == "activation"
    assert await _role(services.db, PEER_ADMIN + 1, "/code CabcABC12345") == "activation"


async def test_the_same_admin_id_on_both_sides_stays_the_admin(services, rsv):
    rc.pair_row(services, admin=ADMIN)
    assert await _role(services.db, ADMIN) == "admin", "свой админ получил роль второго сервера"


async def test_the_role_comes_before_the_client_profile(services, rsv, make_active_client):
    """Тот же аккаунт и пользователь этого сервера: отвечает роль (шаг раньше
    пользователей) — меню пользователя ему здесь не показать."""
    make_active_client(name="Совпал", tg_id=PEER_ADMIN)
    rc.pair_row(services)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"


async def test_a_new_admin_id_moves_the_role_at_once(services, rsv):
    rc.pair_as_inviter(services, rsv)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"
    st = services.reserve_st_body()
    services.reserve_st_in({**st, "name": "Beta", "self": rc.HOST_B, "admin_id": 555})
    assert await _role(services.db, 555) == "peer_admin"
    assert await _role(services.db, PEER_ADMIN) is None, "прежний аккаунт держит роль до конца кэша"


async def test_unpairing_takes_the_role_away_at_once(services, rsv):
    rc.pair_as_inviter(services, rsv)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"
    services.reserve_unpair()
    assert await _role(services.db, PEER_ADMIN) is None, "роль пережила удаление пары"
    assert await _role(services.db, PEER_ADMIN, "/start") == "activation"


# ── диспетчер ────────────────────────────────────────────────────────────────

def test_the_role_router_stands_before_admin_settings_and_clients():
    from awgbot.bot.handlers import admin, client, peeradmin, settings
    from awgbot.bot.routers import routers_for
    order = routers_for("client")
    i = order.index(peeradmin.router)
    assert i < order.index(admin.router) < order.index(settings.router) < order.index(client.router)


def test_every_role_handler_is_behind_the_role_filter():
    """Сообщения роли — только роли; кнопки экрана роли — роли или общему
    админу обоих серверов (экран роли — раздел его «🛟 Резерв»)."""
    from awgbot.bot.filters import PeerAdminFilter, RoleFilter
    from awgbot.bot.handlers import peeradmin
    roles = [f.callback.roles for f in peeradmin.router.message._handler.filters if isinstance(f.callback, RoleFilter)]
    assert roles == [{"peer_admin"}], f"сообщения роли без фильтра роли: {roles}"
    cb = [f.callback for f in peeradmin.router.callback_query._handler.filters]
    assert len(cb) == 1 and isinstance(cb[0], PeerAdminFilter), f"кнопки роли без фильтра роли: {cb}"


# только чтение сведений о паре; reserve_peer_admin_opened — отметка «админ
# второго сервера открыл бот» (без неё бот не знает, можно ли ему писать)
# reserve_peer_admin_client — чтение: пользователь ли он здесь («👤 Меню пользователя»)
_ALLOWED = {"reserve_peer_admin_summary", "reserve_peer_admin_opened", "reserve_peer_admin_client"}


def test_the_role_router_writes_nothing():
    """Сторож по вызовам: модуль роли обращается к сервисам только через
    разрешённые чтения и отметку «открыл бот»; ни `db.*`, ни `set*`/`put`/
    `update`/`delete`/`clear` где бы то ни было. Новый вызов — пересмотр."""
    from awgbot.bot.handlers import peeradmin
    tree = ast.parse(inspect.getsource(peeradmin))
    used, bad = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "services":
                used.add(node.attr)
            if node.attr == "db" or any(w in node.attr.lower() for w in ("set_", "_set", "put", "update", "delete",
                                                                          "clear", "unpair", "invite")):
                bad.append(node.attr)
    assert not bad, f"модуль роли трогает запись: {bad}"
    assert used <= _ALLOWED, f"модуль роли зовёт сервисы вне перечня: {used - _ALLOWED}"


# ── экран Т-РН1 ──────────────────────────────────────────────────────────────

async def test_start_shows_the_role_screen_and_marks_the_bot_opened(services, rsv, fake_bot, monkeypatch):
    from awgbot.bot.handlers import peeradmin
    rc.pair_as_inviter(services, rsv)
    monkeypatch.setattr(services, "server_ok", lambda: True)
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, services)
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "Ты — админ «Beta»" in text and "управлять «Alpha» отсюда нельзя" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["🔄 Обновить", "Скрыть"], "в этапе пары кнопки аварийного кода быть не должно (только в Н4)"
    assert services.rsv_get("rsv_peer_admin")["opened"] is True


async def test_refresh_redraws_in_place(services, rsv, fake_bot, monkeypatch):
    from awgbot.bot.handlers import peeradmin
    rc.pair_row(services)
    monkeypatch.setattr(services, "server_ok", lambda: True)
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.refresh(cb, services)
    assert [s[0] for s in nav.sent] == ["edit_text"] and cb.answers[-1][0] == "Обновлено"
