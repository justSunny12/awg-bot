"""Раздел «⬆️ Обновления» после отката, когда цель ушла вперёд: ход до
v1.2.0 откатился, доступна v1.3.0 — строка отката говорит «Доступна новее —
v1.3.0, обновление пойдёт на неё», кнопка обычная «⬆️ Обновить до v1.3.0»,
«🔁 Повторить» нет; цель та же — «🔁 Повторить» и ничего лишнего. Кнопка
«🔁 Повторить» на итоге прошлого хода при сменившейся цели — всплывашка
«Цель обновления изменилась…», хода нет.

Цена ошибки: человек жмёт «Повторить» ради v1.2.0 — и набор уходит на
v1.3.0, которую он не выбирал.
"""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import fleet as fleet_flow
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from awgbot.infra import updates
from tests.conftest import FakeCallback, FakeMessage
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.e2e

ROLE = {"requires": "1.0.0"}
V_BASE, V_OLD, V_NEW = "v1.1.0", "v1.2.0", "v1.3.0"
NEWER = "Доступна новее"


def _rolled_back(fleet, monkeypatch, *, newer: bool):
    """Сервер и шлюз на v1.1.0, ход до v1.2.0 откатился; newer — после этого
    на GitHub вышла v1.3.0 (раздел перечитывает список при открытии)."""
    fleet.server("1.1.0")
    releases = [rel(V_BASE, main=ROLE, gw=ROLE), rel(V_OLD, main=ROLE, gw=ROLE)]
    fleet.releases(*releases)
    fleet.slot(1, version="1.1.0")
    assert fleet.start() == "", "сценарий собран не так: ход не начат"
    fleet.finish_server(fleet.tick(), "1.2.0")
    out = fleet.ack(1, "failed", error="сеть")
    fleet.s.fleet_server_act(out)
    fleet.server("1.1.0")
    fleet.tick()
    assert fleet.run()["state"] == "rolled_back", fleet.run()
    if newer:
        releases.append(rel(V_NEW, main=ROLE, gw=ROLE))
        fleet.releases(*releases)                      # проверка по расписанию уже сохранила список
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(releases))
    return fleet


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def _cb(fake_bot) -> FakeCallback:
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    return FakeCallback(data="", message=msg, user_id=config.ADMIN_ID, bot=fake_bot)


async def test_the_section_offers_the_newer_target_instead_of_a_retry(fleet, monkeypatch):
    _rolled_back(fleet, monkeypatch, newer=True)
    text, markup = await sh._screen("upd", fleet.s)
    assert ("⚠️ Обновление до v1.2.0 отменено — сервер AWG вернулся на v1.1.0: сеть. "
            "Доступна новее — v1.3.0, обновление пойдёт на неё") in text, text
    labels = _labels(markup)
    assert "⬆️ Обновить до v1.3.0" in labels, labels
    assert "🔁 Повторить" not in labels, f"«Повторить» при сменившейся цели повёл бы на v1.3.0: {labels}"


async def test_the_section_keeps_the_retry_while_the_target_is_the_same(fleet, monkeypatch):
    _rolled_back(fleet, monkeypatch, newer=False)
    text, markup = await sh._screen("upd", fleet.s)
    assert "⚠️ Обновление до v1.2.0 отменено — сервер AWG вернулся на v1.1.0: сеть" in text, text
    assert NEWER not in text, f"фраза о новой цели при той же цели: {text}"
    labels = _labels(markup)
    assert "🔁 Повторить" in labels, labels
    assert not any(label.startswith("⬆️ Обновить до") for label in labels), \
        f"при той же цели рядом с «Повторить» лишняя кнопка обновления: {labels}"


async def test_retry_on_the_final_message_refuses_when_the_target_moved_on(fleet, monkeypatch, fake_bot):
    _rolled_back(fleet, monkeypatch, newer=True)
    before = fleet.run()
    cb = _cb(fake_bot)
    await fleet_flow.retry(cb, fleet.s)
    assert cb.answers[-1] == (texts.UPDATE_TARGET_CHANGED_TOAST, True), cb.answers
    assert fleet.run() == before, "итог откатившегося хода тронут отказом"
    assert [r for r in fake_bot.records if r[0] == "send_message"] == [], "живое сообщение хода при отказе"
