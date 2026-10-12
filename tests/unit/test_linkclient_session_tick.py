"""
Клиент канала на шлюзе: снимок сразу после операции из чата, тик и
внеочередной обзор (отказ шага — в журнал), fill после own_ack фоном.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from awgbot.core import config
from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, SN, _Agent, _live_client

pytestmark = pytest.mark.unit

conf, gateway_role = _c.conf, _c.gateway_role


# ── снимок сразу после операции из чата ─────────────────────────────────────

async def test_poke_sends_the_change_right_away(gateway_role, conf):
    """Человек применил бандл или мастер восстановления и смотрит на карточку
    слота на ВПС. Ждать тика монитора (минуты) — значит показывать ему старое."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)
    agent.snap["bundle"] = {"lan_mode": "0"}
    await linkclient.poke(agent)
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    assert [m["t"] for m in msgs] == ["snap", "delta"]
    assert msgs[-1]["bundle"] == {"lan_mode": "0"}
    await linkclient.poke(agent)
    assert len(client._writer.written) == 2, "повторный poke без изменений что-то отправил"


async def test_poke_without_the_channel_is_quiet(monkeypatch, tmp_path):
    """Конфига линка нет (или это не шлюз) — операция из чата не должна
    ни падать, ни поднимать клиента."""
    monkeypatch.setattr(linkclient, "_client", None)
    monkeypatch.setattr(config, "ROLE", "client")
    await linkclient.poke(_Agent())
    assert linkclient._client is None
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "нет-такого.conf"))
    await linkclient.poke(_Agent())
    assert linkclient._client is None


async def test_poke_survives_a_dead_socket(gateway_role, conf):
    """Сессия умерла между тиками: итог операции в чате агента важнее снимка на
    ВПС — исключение из poke уронило бы хендлер после успешного применения."""
    agent = _Agent()
    client = _live_client(agent)
    await client.push(full=True)

    def broken(data):
        raise ConnectionResetError("линк упал")
    client._writer.write = broken
    agent.snap["egress_ok"] = False
    await linkclient.poke(agent)                     # не бросает


async def test_the_settings_answer_carries_the_outcome_and_no_fingerprint(gateway_role, conf):
    """Ответ на настройки — только итог применения. Что доставка завершена,
    сервер видит по снимку, который уходит следом: отпечаток в ответе был
    вторым источником правды, расходившимся со снимком после отката."""
    agent = _Agent()
    agent.apply_link_settings = lambda values: {"ok": True, "changed": ["HOME_SUBNETS"], "error": ""}
    agent.link_settings_note = lambda result: "применены"
    client = _live_client(agent)
    await client.push(full=True)
    agent.snap["bundle"] = {"home_subnets": "192.168.70.0/24"}
    await client._apply_settings({"HOME_SUBNETS": "192.168.70.0/24"})
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    ack = next(m for m in msgs if m["t"] == "ack")
    assert set(ack) - {"t", "seq", "ts"} == {"ok", "changed", "error"}, f"в ответе лишнее: {sorted(ack)}"
    assert ack["ok"] is True and ack["changed"] == ["HOME_SUBNETS"]
    assert msgs[-1]["t"] == "delta" and msgs[-1]["bundle"] == {"home_subnets": "192.168.70.0/24"}, (
        "снимок с новыми значениями не ушёл следом за ответом")


# ── тик и внеочередной обзор: отказ шага — в журнал, не молча ───────────────

async def test_a_failing_tick_step_is_logged_and_the_next_step_still_runs(gateway_role, conf, caplog):
    """Повтор записей SMB упал (исключение в агенте): сверка своих списков на
    том же тике всё равно идёт, а отказ виден в журнале — раньше его глушили
    молча, и «записи не применяются» нечем было объяснить."""
    import logging
    agent = _Agent()
    reconciled: list[int] = []

    def boom():
        raise RuntimeError("помощник упал")
    agent.services_retry = boom
    agent.own_reconcile = lambda: reconciled.append(1) or False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: None
    _live_client(agent)
    with caplog.at_level(logging.WARNING, logger="awgbot"):
        await linkclient.on_tick(agent)
    assert reconciled == [1], "после упавшего шага сверка своих списков не пошла"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("retry_peer_services" in m and "помощник упал" in m for m in warns), warns


async def test_a_failing_smb_scan_on_poke_is_logged(gateway_role, conf, caplog):
    import logging
    agent = _Agent()

    def boom():
        raise OSError("avahi-browse: нет прав")
    agent.services_scan = boom
    _live_client(agent)
    with caplog.at_level(logging.WARNING, logger="awgbot"):
        await linkclient.poke(agent)                  # не бросает
    assert any("нет прав" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING), \
        [r.getMessage() for r in caplog.records]


async def test_the_smb_list_goes_on_every_change_and_never_without_one(gateway_role, conf):
    """Обзор нашёл изменение — список уходит тут же; не нашёл — ни байта.
    Повторно тот же список отправляется только при переподключении (hello)."""
    agent = _Agent()
    changes = iter([True, False, True])
    agent.services_scan = lambda: next(changes)
    agent.services_local = lambda: [{"t": "_smb._tcp", "n": "nas", "h": "nas", "p": 445, "a": "192.168.1.10"}]
    client = _live_client(agent)
    for _ in range(3):
        await linkclient.services_changed(agent)
    msgs = [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
    assert [m["t"] for m in msgs] == ["svc", "svc"], [m["t"] for m in msgs]
    assert msgs[0]["items"] == agent.services_local()


async def test_reconcile_never_runs_in_the_middle_of_applying_the_canon(gateway_role, conf):
    """Сверка файлов посреди `sync` сочла бы строки канона своими правками и
    отправила бы их серверу: применение канона и сверка идут по очереди."""
    import threading
    agent = _Agent()
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def apply(msg):
        order.append("apply:start"); started.set()
        release.wait(5)
        order.append("apply:end")
        return {"ok": True, "hash": "ab", "n": 0, "error": ""}
    agent.apply_own_lists = apply
    agent.own_reconcile = lambda: order.append("reconcile") or False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: None
    client = _live_client(agent)
    t_apply = asyncio.create_task(client._apply_own({"hash": "ab"}))
    await asyncio.to_thread(started.wait, 5)
    t_tick = asyncio.create_task(client.own_tick())
    await asyncio.sleep(0.1)
    assert order == ["apply:start"], f"сверка пошла посреди применения канона: {order}"
    release.set()
    await asyncio.wait_for(asyncio.gather(t_apply, t_tick), 5)
    assert order == ["apply:start", "apply:end", "reconcile"], order


# ── fill после own_ack: наполнение набора — фоном ───────────────────────────

def _kinds_sent(client) -> list[str]:
    return [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in client._writer.written]


def _fill_agent(fill_result=(True, "набор lan_vpn4: адреса 3 доменов взяты в обработку")):
    """Агент, у которого канон применился с новыми «в туннель»; fill ждёт
    `release` (dig по сотням доменов) и записывает, что и когда звали."""
    import threading
    agent = _Agent()
    agent.release = threading.Event()
    agent.started = threading.Event()
    agent.fills: list[str] = []
    agent.fill_result = fill_result
    agent.apply_own_lists = lambda msg: {"ok": True, "hash": msg.get("hash", "ab"), "n": 1, "error": "",
                                         "fill": f"/tmp/fill-{msg.get('hash', 'ab')}"}

    def own_fill(path):
        agent.fills.append(f"start:{path}")
        agent.started.set()
        agent.release.wait(5)
        agent.fills.append(f"end:{path}")
        if isinstance(agent.fill_result, Exception):
            raise agent.fill_result
        return agent.fill_result
    agent.own_fill = own_fill
    return agent


async def test_own_ack_goes_before_the_fill_is_done(gateway_role, conf):
    """Сервер видит «применены» сразу после sync, а не через полчаса dig:
    канал не ждёт наполнения набора."""
    agent = _fill_agent()
    client = _live_client(agent)
    await asyncio.wait_for(client._apply_own({"hash": "ab"}), 2)
    assert "own_ack" in _kinds_sent(client), "own_ack ждёт fill"
    await asyncio.to_thread(agent.started.wait, 5)
    assert agent.fills == ["start:/tmp/fill-ab"], agent.fills
    assert client._fill_task is not None and not client._fill_task.done()
    agent.release.set()
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-ab", "end:/tmp/fill-ab"]


async def test_a_failed_fill_is_logged_and_changes_nothing_else(gateway_role, conf, caplog):
    """fill отказал (dig упал, скрипт без fill) — предупреждение в журнал;
    итог канона уже ушёл «применены», повторного ответа нет."""
    import logging
    agent = _fill_agent((False, "usage: awg-lan-domain.sh add|ru|del"))
    agent.release.set()
    client = _live_client(agent)
    with caplog.at_level(logging.INFO, logger="awgbot"):
        await client._apply_own({"hash": "ab"})
        await asyncio.wait_for(client._fill_task, 5)
    assert _kinds_sent(client) == ["own_ack"], _kinds_sent(client)
    ack = gwlink.unpack(KEY, client._writer.written[0], nonce=SN)
    assert ack["ok"] is True and "fill" not in ack, f"итог fill или путь уехали серверу: {ack}"
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("набор не пополнен" in m and "usage" in m for m in warns), warns


async def test_a_successful_fill_is_logged_as_info(gateway_role, conf, caplog):
    import logging
    agent = _fill_agent()
    agent.release.set()
    client = _live_client(agent)
    with caplog.at_level(logging.INFO, logger="awgbot"):
        await client._apply_own({"hash": "ab"})
        await asyncio.wait_for(client._fill_task, 5)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert "свои списки: набор lan_vpn4: адреса 3 доменов взяты в обработку" in infos, infos
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "удачный fill — с предупреждением"


async def test_a_fill_that_raises_does_not_break_the_next_one(gateway_role, conf):
    """Исключение в fill (скрипт пропал) не должно остановить следующий fill:
    второй ждёт первого и идёт своим ходом."""
    agent = _fill_agent(RuntimeError("скрипта нет"))
    agent.release.set()
    client = _live_client(agent)
    await client._apply_own({"hash": "a1"})
    first = client._fill_task
    agent.fill_result = (True, "ok")
    await client._apply_own({"hash": "a2"})
    await asyncio.wait_for(client._fill_task, 5)
    assert first.done()
    assert agent.fills[-1] == "end:/tmp/fill-a2", agent.fills


async def test_two_canons_in_a_row_fill_one_after_the_other(gateway_role, conf):
    """Два канона подряд: файл для fill один и тот же — второй fill начинается
    только после первого, иначе два dig-прохода читали бы полупереписанный файл."""
    agent = _fill_agent()
    client = _live_client(agent)
    await client._apply_own({"hash": "a1"})
    await asyncio.to_thread(agent.started.wait, 5)
    await client._apply_own({"hash": "a2"})
    assert _kinds_sent(client) == ["own_ack", "own_ack"], "второй ответ ждал первый fill"
    await asyncio.sleep(0.2)
    assert agent.fills == ["start:/tmp/fill-a1"], f"второй fill пошёл, не дождавшись первого: {agent.fills}"
    agent.release.set()
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-a1", "end:/tmp/fill-a1", "start:/tmp/fill-a2", "end:/tmp/fill-a2"]


async def test_no_fill_key_means_no_task(gateway_role, conf):
    agent = _fill_agent()
    agent.apply_own_lists = lambda msg: {"ok": True, "hash": "ab", "n": 1, "error": ""}
    client = _live_client(agent)
    await client._apply_own({"hash": "ab"})
    assert client._fill_task is None and agent.fills == []
    assert _kinds_sent(client) == ["own_ack"]


async def test_a_skipped_or_failed_canon_starts_no_fill(gateway_role, conf):
    """Канон пропущен по правилам сверки — ответа нет, и fill тоже."""
    agent = _fill_agent()
    agent.apply_own_lists = lambda msg: {"ok": False, "skipped": "pending", "hash": "ab", "n": 0, "error": ""}
    agent.own_pending_events = lambda: ("r", [])
    client = _live_client(agent)
    await client._apply_own({"hash": "ab"})
    assert client._fill_task is None and agent.fills == []


async def test_the_deferred_canon_on_a_tick_also_fills(gateway_role, conf):
    """Отложенный канон (обвязка обновилась) применяется на тике — новые «в
    туннель» так же уходят в fill после ответа."""
    agent = _fill_agent()
    agent.release.set()
    agent.own_reconcile = lambda: False
    agent.own_unsent = lambda: False
    agent.own_retry = lambda: {"ok": True, "hash": "cd", "n": 1, "error": "", "fill": "/tmp/fill-cd"}
    client = _live_client(agent)
    await client.own_tick()
    assert _kinds_sent(client) == ["own_ack"]
    assert client._fill_task is not None
    await asyncio.wait_for(client._fill_task, 5)
    assert agent.fills == ["start:/tmp/fill-cd", "end:/tmp/fill-cd"]


async def test_stop_lets_go_of_a_running_fill(gateway_role, conf):
    """Агент останавливается посреди fill: остановка не ждёт dig (поток не
    прервать) и не падает; задача-обёртка снята."""
    agent = _fill_agent()
    client = _live_client(agent)
    client._task = None
    await client._apply_own({"hash": "ab"})
    await asyncio.to_thread(agent.started.wait, 5)
    t0 = time.monotonic()
    await asyncio.wait_for(client.stop(), 2)
    assert time.monotonic() - t0 < 1, "stop ждал fill"
    assert client._fill_task is None
    agent.release.set()
