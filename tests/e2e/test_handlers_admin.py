"""E2E: admin-хендлеры (прямой вызов с фейками) — панель, клиенты, создание, удаление."""
import pytest

from awgbot.bot.handlers import admin as admin_h
from awgbot.bot.callbacks import ClientCB, ConfirmCB, PeriodCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(services, bot, data=""):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(data=data, message=nav, user_id=ADMIN, bot=bot), nav


# ── панель ───────────────────────────────────────────────────────────────────
async def test_admin_start_shows_panel(services, fake_bot):
    services.ensure_admin_client()
    msg = FakeMessage(text="/start", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_h.admin_start(msg, services, FakeState())
    assert any(s[0] == "answer" for s in msg.sent)
    assert services.db.get_nav_message_id(ADMIN) is not None


# ── список / карточка клиента ────────────────────────────────────────────────
async def test_clients_list_with_client(services, make_active_client, fake_bot):
    make_active_client(name="Ося", tg_id=7000)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.clients_list(cb, services)
    assert any(s[0] == "edit_text" and "Профили" in s[1] for s in nav.sent)
    assert cb.answers


async def test_clients_list_shows_admin_profile(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.clients_list(cb, services)          # админ-профиль теперь виден
    assert any("Профили" in s[1] for s in nav.sent if s[0] == "edit_text")


async def test_client_open_card(services, make_active_client, fake_bot):
    client = make_active_client(name="Ким", tg_id=7001)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_open(cb, ClientCB(action="open", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── создание клиента (FSM: имя → лимит → трафик → период) ─────────────────────
async def test_create_client_full_fsm(services, fake_bot):
    services.ensure_admin_client()
    state = FakeState()
    m = lambda text: FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)

    await admin_h.add_client_name(m("Новичок"), services, state)
    assert (await state.get_data())["name"] == "Новичок"
    await admin_h.add_client_limit(m("3"), services, state)
    assert (await state.get_data())["limit"] == 3
    await admin_h.add_client_traffic(m("100"), services, state)
    assert (await state.get_data())["traffic_gb"] == 100

    # выбор периода → создание клиента
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, state)
    names = [c.name for c in services.db.list_clients(include_service=False)]
    assert "Новичок" in names
    # выданы приветствие + шаблон приглашения со ссылкой на бота
    assert any("t.me/test_bot" in s[1] for s in nav.sent if s[0] == "answer")


async def test_create_client_bad_limit_reprompts(services, fake_bot):
    state = FakeState()
    await state.update_data(name="X")
    msg = FakeMessage(text="не число", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_h.add_client_limit(msg, services, state)
    assert "limit" not in await state.get_data()      # не принято
    assert any("число" in s[1].lower() for s in msg.sent)


async def test_create_client_period_stale_dialog(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _admin_cb(services, fake_bot)
    # пустой state (диалог устарел) → алерт, не падаем
    await admin_h.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, FakeState())
    assert cb.answers and cb.answers[0][1] is True    # show_alert


# ── регенерация инвайта / удаление ───────────────────────────────────────────
async def test_regen_invite(services, fake_bot):
    created = services.create_client("Пенд", 1, "year")
    old = created.invite_code
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.regen_invite(cb, ClientCB(action="regen_invite", client_id=created.client_id), services)
    new_code = services.db.get_client(created.client_id).invite_code
    assert new_code != old
    assert any("t.me/test_bot" in s[1] for s in nav.sent if s[0] == "answer")


async def test_client_delete_apply(services, make_active_client, fake_bot):
    client = make_active_client(name="НаУдаление", tg_id=7002)
    services.add_device(client.id, "d")
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_delete_apply(
        cb, ConfirmCB(action="del_client", ref=client.id, yes=True), services)
    assert services.db.get_client(client.id) is None
    assert any("удал" in s[1].lower() for s in nav.sent if s[0] == "edit_text")


async def test_client_delete_cancel_shows_card(services, make_active_client, fake_bot):
    client = make_active_client(name="Остаётся", tg_id=7003)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_delete_apply(
        cb, ConfirmCB(action="del_client", ref=client.id, yes=False), services)
    assert services.db.get_client(client.id) is not None   # не удалён
    assert cb.answers


def test_period_choices_has_cancel_both_contexts():
    """Баг-фикс: диалог выбора срока не тупик — есть кнопка отмены."""
    from awgbot.bot import keyboards as kb
    ext = [b.text for r in kb.period_choices("extend", ref=7).inline_keyboard for b in r]
    cre = [b.text for r in kb.period_choices("create").inline_keyboard for b in r]
    assert any("Отмена" in t for t in ext)
    assert any("Отмена" in t for t in cre)
    # extend-отмена ведёт к карточке клиента, create — в меню
    ext_cb = [b.callback_data for r in kb.period_choices("extend", ref=7).inline_keyboard
              for b in r if "Отмена" in b.text][0]
    assert ext_cb == "c:open:7"
