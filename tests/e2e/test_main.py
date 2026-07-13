"""E2E: реконсиляция из вотчдога/старта (runtime.main.do_reconcile)."""
import pytest

from awgbot.runtime import main as m
from awgbot.infra import awg

pytestmark = pytest.mark.e2e


async def test_do_reconcile_sends_notifs_and_invalidates(services, fake_bot, monkeypatch):
    from awgbot.domain.services import Notification
    invalidated = {"n": 0}
    monkeypatch.setattr(awg, "invalidate_server_params",
                        lambda: invalidated.__setitem__("n", invalidated["n"] + 1))
    monkeypatch.setattr(services, "reconcile_peers", lambda: [Notification(1, "x")])
    await m.do_reconcile(services, fake_bot)
    assert invalidated["n"] == 1
    assert any(r[0] == "send_message" for r in fake_bot.records)


async def test_do_reconcile_swallows_errors(services, fake_bot, monkeypatch):
    monkeypatch.setattr(awg, "invalidate_server_params", lambda: None)
    monkeypatch.setattr(services, "reconcile_peers",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    await m.do_reconcile(services, fake_bot)                # не поднимает
