"""«✅ Готов быть резервом» и пакеты гостевой зоны (handlers/settings/reserve/card.py
ready_toggle → reserve_tools_ensure): включение готовности запускает установку
недостающего в фоне и говорит об этом во всплывашке.

Цена ошибки: всплывашка молчит об установке — админ не знает, что сервер ещё
не готов принять пользователей второго сервера; установка при выключении — бот
ставит пакеты, которые уже не нужны; установка в потоке нажатия — кнопка
«висит» минутами, пока apt-get качает пакеты.
"""
from __future__ import annotations

import threading

import pytest

from awgbot.bot.handlers.settings.reserve import card as card_h
from awgbot.core import settings
from awgbot.domain.services.reserve import invite as invitemod
from awgbot.infra import rsvzone
from tests.conftest import FakeState
from tests.e2e import test_reserve_ui as _ui
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.e2e
rsv = rc.rsv
ui = _ui.ui


@pytest.fixture()
def apt(monkeypatch):
    """План зоны без dnsmasq и conntrack; apt-get — журнал вызовов (успех)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(invitemod.ReserveInviteMixin, "reserve_tools_missing",
                        lambda self: [] if calls else ["dnsmasq", "conntrack"])
    monkeypatch.setattr(rsvzone, "install_tools", lambda names, timeout=900: calls.append(list(names)) or " ".join(names))
    yield calls
    _join()


def _join() -> None:
    for t in threading.enumerate():
        if t.name == "rsv-tools":
            t.join(10)


async def test_turning_readiness_on_starts_the_install_and_says_so(services, ui, fake_bot, apt):
    settings.set_value("app.reserve.ready", False)
    cb = _ui._cb(_ui._nav(fake_bot), fake_bot)
    await card_h.ready_toggle(cb, services, FakeState())
    assert cb.answers[-1][0] == "Готовность включена · ставлю dnsmasq, conntrack", cb.answers
    _join()
    assert apt == [["dnsmasq", "conntrack"]], "включение готовности не запустило установку"
    assert settings.get_bool("app.reserve.ready", False) is True


async def test_with_the_tools_in_place_the_toast_is_plain(services, ui, fake_bot, apt, monkeypatch):
    monkeypatch.setattr(invitemod.ReserveInviteMixin, "reserve_tools_missing", lambda self: [])
    settings.set_value("app.reserve.ready", False)
    cb = _ui._cb(_ui._nav(fake_bot), fake_bot)
    await card_h.ready_toggle(cb, services, FakeState())
    assert cb.answers[-1][0] == "Готовность включена", cb.answers
    _join()
    assert apt == []


async def test_turning_readiness_off_without_a_pair_installs_nothing(services, ui, fake_bot, apt):
    assert settings.get_bool("app.reserve.ready", False) is True
    cb = _ui._cb(_ui._nav(fake_bot), fake_bot)
    await card_h.ready_toggle(cb, services, FakeState())
    assert cb.answers[-1][0] == "Готовность выключена", cb.answers
    _join()
    assert apt == [], "выключение готовности поставило пакеты"
