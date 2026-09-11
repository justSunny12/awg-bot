"""Пачка оптимизаций: навигация одной транзакцией, кэш доступа, пакетное
удаление, структурное сравнение правил, параллельная рассылка, снимок панели."""
from __future__ import annotations

import asyncio

import pytest

from awgbot.core import access_cache
from awgbot.infra import awg


# ── nav_touch: одна транзакция вместо трёх хопов ─────────────────────────────

def test_nav_touch_sets_active_and_history_and_returns_previous(services):
    db = services.db
    assert db.nav_touch(7, 100) is None
    assert db.get_nav_message_id(7) == 100
    assert db.nav_touch(7, 101) == 100
    assert db.get_nav_message_id(7) == 101
    assert db.nav_touch(7, 101) == 101, "повтор того же id — история не дублируется"
    assert db.pop_nav_history(7) == [100, 101]


def test_nav_touch_history_is_capped(services):
    db = services.db
    for mid in range(1, 40):
        db.nav_touch(9, mid)
    ids = db.pop_nav_history(9)
    assert len(ids) == db._NAV_HISTORY_CAP and ids[-1] == 39


# ── кэш «кто это» в middleware: TTL + сброс любой записью ───────────────────

def test_access_cache_hit_then_invalidated_by_db_commit(services):
    access_cache.invalidate_all()
    access_cache.put(42, "client", "obj")
    assert access_cache.get(42) == ("client", "obj", None)
    services.db.set_state("perf_probe", "1")          # любая транзакция сбрасывает кэш
    assert access_cache.get(42) is None


def test_access_cache_expires(monkeypatch):
    access_cache.invalidate_all()
    t = [1000.0]
    monkeypatch.setattr(access_cache.time, "monotonic", lambda: t[0])
    access_cache.put(1, "invited", "c", "d")
    assert access_cache.get(1) == ("invited", "c", "d")
    t[0] += access_cache.TTL_SECONDS + 1
    assert access_cache.get(1) is None


async def test_middleware_uses_cache_for_client(services, monkeypatch):
    from awgbot.bot.middleware import AccessMiddleware
    from tests.conftest import FakeMessage
    access_cache.invalidate_all()
    created = services.create_client("Кэш", 3, "month")
    services.activate_client(created.invite_code, 555)
    c = services.db.get_client(created.client_id)
    mw = AccessMiddleware(services.db)
    calls = []
    orig = services.db.get_client_by_tg
    monkeypatch.setattr(services.db, "get_client_by_tg", lambda uid: (calls.append(uid), orig(uid))[1])
    seen = []

    async def handler(event, data):
        seen.append((data["role"], data["client"].id))
    msg = FakeMessage(chat_id=555, user_id=555, text="hi")
    await mw(handler, msg, {"event_from_user": msg.from_user})
    await mw(handler, msg, {"event_from_user": msg.from_user})
    assert seen == [("client", c.id)] * 2
    assert calls == [555], "второй апдейт — из кэша, без БД"


# ── пакетное удаление с откатом на поштучное ─────────────────────────────────

async def test_delete_many_batches_and_falls_back(fake_bot):
    from awgbot.bot.handlers.common import delete_many
    await delete_many(fake_bot, 1, [10, 11, 12])
    assert ("delete_messages", 1, [10, 11, 12]) in fake_bot.records

    class Bot(type(fake_bot)):
        async def delete_messages(self, chat_id, message_ids, **kw):
            raise RuntimeError("one id is stale")
    b = Bot()
    await delete_many(b, 1, [10, 11])
    assert [r for r in b.records if r[0] == "delete_message"] == [
        ("delete_message", 1, 10), ("delete_message", 1, 11)]


async def test_delete_many_empty_is_noop(fake_bot):
    from awgbot.bot.handlers.common import delete_many
    await delete_many(fake_bot, 1, [])
    assert fake_bot.records == []


# ── ssh_reconcile: правила равны структурно, а не строкой ────────────────────

def test_rules_equal_ignores_option_order_but_not_rule_order():
    cur = ["-A AWG_SSH -s 1.2.3.4/32 -p tcp -m tcp --dport 22 -j ACCEPT",
           "-A AWG_SSH -p tcp -m tcp --dport 22 -j DROP"]
    des = ["-A AWG_SSH -p tcp -m tcp -s 1.2.3.4/32 --dport 22 -j ACCEPT",
           "-A AWG_SSH -p tcp -m tcp --dport 22 -j DROP"]
    assert awg._rules_equal(cur, des)
    assert not awg._rules_equal(list(reversed(cur)), des)
    assert not awg._rules_equal(cur[:1], des)


def test_ssh_reconcile_skips_rebuild_when_only_option_order_differs(monkeypatch):
    """iptables-nft печатает опции в своём порядке — цепочку не пересобираем."""
    from awgbot.core import config
    calls = []

    class P:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out

    iface, port, chain = config.AWG_INTERFACE, config.SSH_PORT, awg._SSH_CHAIN
    # реальный вывод `iptables -S`: -p tcp ДО -d и -i, как печатает iptables-nft
    listed = [f"-A {chain} -s 10.0.0.2/32 -d 10.0.0.1/32 -p tcp -i {iface} -m tcp --dport {port} -j ACCEPT",
              f"-A {chain} -d 10.0.0.1/32 -p tcp -i {iface} -m tcp --dport {port} -j DROP"]

    def fake_exec(args, check=True, **kw):
        calls.append(args)
        if args[:2] == ["iptables", "-S"] and args[2] == chain:
            return P(0, ("-N " + chain + "\n" + "\n".join(listed) + "\n").encode())
        if args[:2] == ["iptables", "-S"]:                      # hook-цепочка: джамп первым
            return P(0, f"-A {args[2]} -j {chain}\n".encode())
        return P(1)                                              # -C stale → нет
    monkeypatch.setattr(awg, "_exec", fake_exec)
    monkeypatch.setattr(awg, "gated_ifaces", lambda: [iface])
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "", raising=False)
    awg.ssh_reconcile(["10.0.0.2"], ["10.0.0.1"])
    mutating = [a for a in calls if any(f in a for f in ("-F", "-A", "-I", "-N", "-D"))]
    assert mutating == [], mutating


# ── рассылка: параллельно, с окном и пейсингом ──────────────────────────────

async def test_broadcast_runs_in_parallel_window(monkeypatch):
    from awgbot.bot import notifier
    inflight = [0]; peak = [0]

    async def fake_send(bot, tg_id, text, photos):
        inflight[0] += 1; peak[0] = max(peak[0], inflight[0])
        await asyncio.sleep(0.01)
        inflight[0] -= 1
        if tg_id == 3:
            raise RuntimeError("blocked")
    monkeypatch.setattr(notifier, "send_announcement", fake_send)
    monkeypatch.setattr(notifier, "_BATCH_PACING_SECONDS", 0)
    ok, failed = await notifier.broadcast(object(), list(range(1, 13)), "t", [])
    assert (ok, failed) == (11, 1)
    assert 1 < peak[0] <= notifier._PARALLEL


async def test_send_notifications_reads_quiet_hours_once(monkeypatch):
    from awgbot.bot import notifier
    from types import SimpleNamespace as N
    reads = []
    monkeypatch.setattr(notifier, "_silent_now", lambda force: (reads.append(force), False)[1])
    sent = []

    async def fake_send(bot, tg_id, text, markup, silent, critical=False):
        sent.append(tg_id)
    monkeypatch.setattr(notifier, "_send", fake_send)
    monkeypatch.setattr(notifier, "_BATCH_PACING_SECONDS", 0)
    await notifier.send_notifications(object(), [N(tg_id=i, text="x") for i in range(1, 8)])
    assert sorted(sent) == list(range(1, 8))
    assert reads == [False], "тихие часы посчитаны один раз на батч"


# ── панель: один снимок, одним хопом ─────────────────────────────────────────

def test_admin_panel_snapshot_carries_everything_the_panel_needs(services):
    snap = services.admin_panel_snapshot()
    for key in ("st", "ac", "routing_ok", "mig", "expiring", "unassigned",
                "has_dev", "rt_visible", "rt_on"):
        assert key in snap
    assert "traffic_rx" in snap["st"]


def test_client_card_data_none_for_missing(services):
    assert services.client_card_data(999999) is None
    c = services.create_client("Карта", 3, "month")
    d = services.client_card_data(c.client_id)
    assert d["client"].id == c.client_id and d["devices"] == [] and d["online"] is False
    assert d["progress"] is None
