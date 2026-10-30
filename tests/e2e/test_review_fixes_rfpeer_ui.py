"""«Выключить» в диалоге Т-РТ4 тумблера «Мои шлюзы для резерва» (bot/handlers/settings/
reserve/card.py: rf_mine_off_yes) пересчитывает только согласие (`reserve_rfpeer_offer_now`):
слот «шлюзы «Beta»» и эпизод второй стороны — своим тактом, их уведомления — только оттуда.

Сервер — «Alpha» с двумя слотами и парой с «Beta» (tests/integration/_rfpeer_common.py),
хендлер зовётся напрямую.

Цена ошибки: обработчик, гоняющий весь такт транзита, съедает переход слота 29 —
его уведомление (Т-РТ8, Т-РТ7, Т-РТ11) возвращается обработчику и теряется: админ не узнаёт,
что РФ-доступ пользователей прекратился или вернулся.
"""
from __future__ import annotations

import pytest

from awgbot.bot.handlers.settings.reserve import card as card_h
from awgbot.core import config, settings
from awgbot.domain.notices import reserve as rn
from awgbot.domain.services.reserve.rfpeer.offer import why_words
from tests.conftest import FakeCallback, FakeMessage
from tests.integration import _rfpeer_common as rp

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
rf = rp.rf


@pytest.fixture()
def ui(rf, monkeypatch):
    from awgbot.util import dnsapi
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [])   # карточка не ходит в сеть за адресом
    return rf


def _cb(fake_bot) -> FakeCallback:
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot)


async def test_turn_off_does_not_run_the_transit_tick(ui, fake_bot, monkeypatch):
    rp.peer_rfp(ui, offer=True)
    rp.settle(ui)
    ticks: list[int] = []
    monkeypatch.setattr(ui, "reserve_rfpeer_tick", lambda: ticks.append(1) or [])
    await card_h.rf_mine_off_yes(_cb(fake_bot), ui)
    assert ticks == [], "«Выключить» прогнал весь такт транзита"
    assert (ui.rsv_get("rsv_rfpeer").get("offer") or {}).get("on") is False, "согласие не снято тем же нажатием"
    assert settings.get_bool("app.reserve.rf_via_mine", True) is False


async def test_turn_off_while_the_peer_slot_goes_keeps_its_notice_for_the_tick(ui, fake_bot):
    """Пользователи на слоте «шлюзы «Beta»», «Beta» только что отозвала согласие, и в эту
    же секунду админ жмёт «Выключить»: Т-РТ8 приходит следующим тактом, а не теряется."""
    rp.peer_rfp(ui, offer=True)
    rp.settle(ui)
    rp.own_down(ui)
    rp.ticks(ui, ui._rt_fail_need())
    assert ui.active_gateway().id == rp.VSLOT, "подготовка: слот 29 не активен"
    rp.peer_rfp(ui, offer=False, why="off")
    rp.fresh_handshake(ui)
    await card_h.rf_mine_off_yes(_cb(fake_bot), ui)
    red = rp.crit(rp.tick(ui))
    assert red == [rn.rf_transit_lost("Beta", why_words("Beta")["off"])], \
        f"«Выключить» съел переход слота — Т-РТ8 не пришло: {red}"
