"""E2E: продление подписки через UI (FSM карточки клиента).

extend_period_chosen: при наличии остатка спрашивает про его сохранение и
запоминает выбор периода в FSM; «Бессрочно» — сразу без вопроса.
extend_keep_answer: применяет продление с/без сохранения остатка.
"""
import pytest

from awgbot.bot.handlers import admin as admin_h
from awgbot.bot.callbacks import ConfirmCB, PeriodCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


async def test_extend_chosen_with_remainder_asks_keep(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7200, period_kind="year")  # остаток ~год
    before_end = services.db.get_client(client.id).period_end
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="month", ctx="extend", ref=client.id), services, state)
    # спросили про остаток и запомнили период в FSM, период пока не менялся
    data = await state.get_data()
    assert data["extend_kind"] == "month" and data["extend_client"] == client.id
    assert services.db.get_client(client.id).period_end == before_end


async def test_extend_keep_yes_preserves_remainder(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7201, period_kind="year")
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()
    await state.update_data(extend_kind="month", extend_client=client.id)
    await admin_h.extend_keep_answer(
        cb, ConfirmCB(action="keep", ref=client.id, yes=True), services, state)
    fresh = services.db.get_client(client.id)
    assert fresh.period_kind == "month"
    assert fresh.status == "active"
    assert await state.get_data() == {}                     # FSM очищен


async def test_extend_never_is_immediate(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7202, period_kind="year")
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="never", ctx="extend", ref=client.id), services, state)
    fresh = services.db.get_client(client.id)
    assert fresh.period_kind == "never"
    assert fresh.period_end is None                         # бессрочная — без вопроса об остатке
    assert await state.get_data() == {}                     # в FSM ничего не откладывали


async def test_extend_keep_answer_without_state_aborts(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7203, period_kind="year")
    before = services.db.get_client(client.id).period_kind
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()                                     # пустой FSM (диалог прерван)
    await admin_h.extend_keep_answer(
        cb, ConfirmCB(action="keep", ref=client.id, yes=True), services, state)
    assert cb.answers and cb.answers[-1][1] is True         # алерт «начни заново»
    assert services.db.get_client(client.id).period_kind == before   # ничего не изменилось


async def test_extend_from_expired_reactivates(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7204, period_kind="year")
    # эмулируем истёкшую подписку
    from awgbot.core.blocks import ClientBlock
    services.db.update_client_fields(client.id, status="expired")
    services._client_set_block(client.id, ClientBlock.EXPIRY)
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()
    await state.update_data(extend_kind="month", extend_client=client.id)
    await admin_h.extend_keep_answer(
        cb, ConfirmCB(action="keep", ref=client.id, yes=False), services, state)
    fresh = services.db.get_client(client.id)
    assert fresh.status == "active"
    from awgbot.core.blocks import ClientBlock as CB
    assert int(fresh.block_reason) & int(CB.EXPIRY) == 0     # причина EXPIRY снята
