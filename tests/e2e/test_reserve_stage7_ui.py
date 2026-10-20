"""Дополнения резерва в боте: «здесь ещё N, там M» в карточке основы и в
живом сообщении, строка «мало свободной памяти» (карточка основы и экран
роли), строка пула Telegram на экране роли, экран роли для общего админа
обоих серверов разделом его «🛟 Резерв» («🛰 Для «…»»), «👤 Меню клиента»
у админа второго сервера, который ещё и клиент этого (роль клиента на
полчаса сессии, middleware ведёт его как клиента).

Нажатия админа — через настоящий диспетчер роли (как в
test_reserve_move_ui.py); экран роли и шаг middleware — напрямую.

Цена ошибки: «здесь ещё N» без «там M» — админ основы не видит, что
клиенты уже у резерва, и ждёт; строка памяти при 0 (прежняя версия
резерва не шлёт поля) — ложная тревога о каждом старом резерве; кнопка
«🛰 Для …» у админа, который не админ второго, — чужой экран; «👤 Меню
клиента» для не клиента — меню чужого профиля; отметка клиента без срока
— экран роли не вернётся никогда.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import PeerCB, RsvCB
from awgbot.core import access_cache, config
from awgbot.domain.services.reserve import tgpool
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage
from tests.e2e.test_reserve_move_ui import _card, _edits, _labels, _live_text, ui
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_common import PEER_ADMIN

pytestmark = pytest.mark.e2e
rsv = rc.rsv
mv = rm.mv
ADMIN = config.ADMIN_ID
__all__ = ["ui"]


@pytest.fixture(autouse=True)
def _no_peer_client_mark():
    """Отметка «он сейчас клиент» — модульная: у каждого теста своя."""
    access_cache._peer_client.clear()
    access_cache.invalidate_all()
    yield
    access_cache._peer_client.clear()
    access_cache.invalidate_all()


def _plain(text: str) -> str:
    return text.replace("<b>", "").replace("</b>", "")


# ── карточка основы: здесь ещё N, там M ─────────────────────────────────────

async def test_the_away_card_says_how_many_are_still_here_and_how_many_are_there(ui, monkeypatch):
    rm.go_away(ui.svc)
    rm.st_in(ui.svc, online=12)
    monkeypatch.setattr(ui.svc, "_online_count", lambda: 7)
    text, _ = await _card(ui)
    assert "здесь ещё 7, там 12" in _plain(text), text


async def test_the_away_card_without_the_peers_count_says_only_here(ui, monkeypatch):
    """Прежний резерв поля `online` не шлёт — «там 0» не пишем."""
    rm.go_away(ui.svc)
    monkeypatch.setattr(ui.svc, "_online_count", lambda: 7)
    text, _ = await _card(ui)
    assert "здесь ещё 7" in _plain(text) and "там" not in _plain(text).split("здесь ещё 7")[1].split("\n")[0], text


async def test_the_away_card_with_nobody_anywhere_has_no_tail(ui, monkeypatch):
    rm.go_away(ui.svc)
    monkeypatch.setattr(ui.svc, "_online_count", lambda: 0)
    text, _ = await _card(ui)
    assert "здесь ещё" not in text and "там" not in text.split("Мои клиенты")[1].split("\n")[0]


async def test_here_and_there_are_not_shown_while_the_clients_are_home(ui, monkeypatch):
    rm.st_in(ui.svc, online=12)
    monkeypatch.setattr(ui.svc, "_online_count", lambda: 7)
    text, _ = await _card(ui)
    assert "здесь ещё" not in text and "там 12" not in text


def test_the_live_message_of_a_finished_move_names_both_counts(ui):
    rm.st_in(ui.svc, online=5)
    assert ui.svc.reserve_move_live()["there"] == 5, "число устройств у резерва не дошло до живого сообщения"
    away = _live_text(ui, "released", state="away", online=41)
    assert "здесь ещё 41, там 5" in away, away
    alone = _live_text(ui, "released", state="away", online=41, there=0)
    assert alone.endswith("здесь ещё 41"), alone


# ── мало свободной памяти ───────────────────────────────────────────────────

@pytest.mark.parametrize("mem, line", [(300, True), (399, True), (400, False), (2048, False), (0, False)])
async def test_the_low_memory_line_of_the_card_is_below_400_mb_only(ui, mem, line):
    rm.st_in(ui.svc, mem_free=mem)
    text, _ = await _card(ui)
    has = "мало свободной памяти" in text
    assert has is line, f"{mem} МБ: строка {'нет' if line else 'есть'}: {text}"
    if line:
        assert f"({mem} МБ)" in text


async def _role_screen(services, fake_bot, monkeypatch, **over) -> tuple[str, list[str]]:
    from awgbot.bot.handlers import peeradmin
    monkeypatch.setattr(services, "server_ok", lambda: True)
    for k, v in over.items():
        monkeypatch.setattr(services, k, v)
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, services)
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    return text, [b.text for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize("mem, line", [(250, True), (400, False), (0, False)])
async def test_the_role_screen_tells_the_peer_admin_about_low_memory(services, rsv, fake_bot, monkeypatch, mem, line):
    rc.pair_as_inviter(services, rsv)
    text, _ = await _role_screen(services, fake_bot, monkeypatch, reserve_mem_free_mb=lambda: mem)
    assert ("мало свободной памяти" in text) is line, text


# ── строка пула Telegram на экране роли ─────────────────────────────────────

async def test_the_role_screen_names_the_learned_networks_of_the_pool(services, rsv, fake_bot, monkeypatch):
    rc.pair_as_inviter(services, rsv)
    tgpool._write({"announced": {"62041": ["149.154.160.0/20"]}, "fetched_at": timeutil.now_iso(),
                   "learned": [{"prefix": "203.0.113.0/24"}, {"prefix": "198.51.100.0/24"}],
                   "pool": ["203.0.113.0/24", "198.51.100.0/24"]})
    text, _ = await _role_screen(services, fake_bot, monkeypatch)
    assert "🟢 пул адресов Telegram для вебхука: ядро и 2 выученные сети" in text, text
    assert "давно не обновлялись" not in text


async def test_the_role_screen_marks_old_announces(services, rsv, fake_bot, monkeypatch):
    rc.pair_as_inviter(services, rsv)
    old = timeutil.to_iso(timeutil.shift(timeutil.now(), -5 * 86400))
    tgpool._write({"announced": {"62041": ["149.154.160.0/20"]}, "fetched_at": old, "learned": [], "pool": []})
    text, _ = await _role_screen(services, fake_bot, monkeypatch)
    assert "⚠️ пул адресов Telegram для вебхука: ядро · анонсы давно не обновлялись" in text, text


# ── общий админ обоих серверов ──────────────────────────────────────────────

async def test_the_card_offers_the_role_screen_only_to_the_admin_of_both_servers(ui):
    _text, labels = await _card(ui)
    assert not [x for x in labels if x.startswith("🛰 Для")], "кнопка роли у админа, который не админ второго"
    ui.svc.db.reserve_peer_update(peer_admin_id=ADMIN)
    _text, labels = await _card(ui)
    assert "🛰 Для «Beta»" in labels, labels


async def test_the_role_screen_opens_inside_the_card_with_back_instead_of_hide(ui, monkeypatch):
    ui.svc.db.reserve_peer_update(peer_admin_id=ADMIN)
    monkeypatch.setattr(ui.svc, "server_ok", lambda: True)
    e = _edits(await ui.run.press(RsvCB(action="role")))
    assert e, "экран роли из карточки не открылся"
    assert "Ты — админ «Beta»" in e[0].text, e[0].text
    labels = _labels(e[0])
    assert "Скрыть" not in labels and any("Назад" in x or "⬅️" in x for x in labels), labels
    assert "👤 Меню клиента" not in labels


async def test_the_refresh_of_the_role_screen_works_for_the_admin_of_both(ui, monkeypatch):
    ui.svc.db.reserve_peer_update(peer_admin_id=ADMIN)
    monkeypatch.setattr(ui.svc, "server_ok", lambda: True)
    calls = await ui.run.press(PeerCB(action="refresh"))
    e = _edits(calls)
    assert e and "Ты — админ «Beta»" in e[0].text, "«🔄 Обновить» общего админа никто не взял"
    assert not [x for x in _labels(e[0]) if x == "Скрыть"], "обновлённый экран раздела потерял «назад»"


async def test_the_role_buttons_are_not_for_an_admin_who_is_not_the_peers_admin(ui, monkeypatch):
    monkeypatch.setattr(ui.svc, "server_ok", lambda: True)
    calls = await ui.run.press(PeerCB(action="refresh"))
    assert not [c for c in _edits(calls) if "Ты — админ" in c.text], "экран роли у админа, который не админ второго"


def test_the_code_is_accepted_from_the_admin_only_when_he_is_the_peers_admin(services, rsv):
    rc.pair_row(services)
    assert services.reserve_code_sender_ok(PEER_ADMIN) is True
    assert services.reserve_code_sender_ok(ADMIN) is False, "код от своего админа без общей роли"
    assert services.reserve_code_in(ADMIN, "DUMMY")["why"] == "closed"
    rc.pair_row(services, admin=ADMIN)
    assert services.reserve_self_peer_admin() is True
    assert services.reserve_code_sender_ok(ADMIN) is True
    assert services.reserve_code_sender_ok(PEER_ADMIN) is False, "прежний аккаунт роли принимается после смены"


# ── «👤 Меню клиента» ───────────────────────────────────────────────────────

async def _role(db, uid: int, text: str = "привет"):
    from tests.e2e.test_reserve_peer_admin import _role as role_of
    return await role_of(db, uid, text)


async def test_the_client_menu_button_is_only_for_a_peer_admin_who_is_a_client_here(services, rsv, fake_bot,
                                                                                   monkeypatch, make_active_client):
    rc.pair_as_inviter(services, rsv)
    _t, labels = await _role_screen(services, fake_bot, monkeypatch)
    assert "👤 Меню клиента" not in labels
    make_active_client(name="Совпал", tg_id=PEER_ADMIN)
    _t, labels = await _role_screen(services, fake_bot, monkeypatch)
    assert "👤 Меню клиента" in labels, labels


async def test_the_client_menu_makes_him_a_client_for_half_an_hour(services, rsv, fake_bot, monkeypatch,
                                                                   make_active_client):
    from awgbot.bot.handlers import peeradmin
    rc.pair_as_inviter(services, rsv)
    make_active_client(name="Совпал", tg_id=PEER_ADMIN)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.as_client(cb, services)
    assert not [a for a in cb.answers if a[1]], f"отказ клиенту: {cb.answers}"
    assert fake_bot.records or nav.sent, "меню клиента не показано"
    access_cache.invalidate_all()
    assert await _role(services.db, PEER_ADMIN) == "client", "middleware не ведёт его как клиента"
    # полчаса прошли — снова роль
    import time
    access_cache._peer_client[PEER_ADMIN] = time.monotonic() - 1
    access_cache.invalidate_all()
    assert await _role(services.db, PEER_ADMIN) == "peer_admin", "экран роли не вернулся после срока отметки"


def test_the_client_mark_lasts_thirty_minutes():
    import time
    access_cache.peer_client_on(PEER_ADMIN)
    left = access_cache._peer_client[PEER_ADMIN] - time.monotonic()
    assert 29 * 60 < left <= 30 * 60, left
    access_cache.peer_client_off(PEER_ADMIN)
    assert access_cache.peer_client_active(PEER_ADMIN) is False


async def test_the_client_menu_for_a_non_client_is_an_alert_and_no_mark(services, rsv, fake_bot):
    from awgbot.bot.handlers import peeradmin
    rc.pair_as_inviter(services, rsv)
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.as_client(cb, services)
    assert cb.answers and cb.answers[-1][1] is True and "не клиент" in cb.answers[-1][0]
    assert not access_cache.peer_client_active(PEER_ADMIN)
    assert await _role(services.db, PEER_ADMIN) == "peer_admin"


async def test_a_stranger_with_the_mark_is_still_a_stranger(services, rsv):
    """Отметка клиента касается только шага роли: чужой аккаунт она клиентом не делает."""
    rc.pair_row(services)
    access_cache.peer_client_on(PEER_ADMIN + 1)
    assert await _role(services.db, PEER_ADMIN + 1) is None


def test_the_admin_of_both_servers_is_not_offered_the_client_menu(services, rsv, make_active_client):
    rc.pair_row(services, admin=ADMIN)
    make_active_client(name="Админ", tg_id=ADMIN)
    assert services.reserve_peer_admin_client() is None and services.reserve_peer_admin_is_client() is False

