"""Набор обновлений на стороне шлюза: команда `upd` → проверки агента →
`accepted` → `done` после выхода на связь новой версией; отказ каждой
проверки — `refused` с причиной; цепочка ступеней одной командой; ответ
переживает перезапуск агента; занят сменой ключей — ждёт факта; новее
выпуска набора — понижение без цепочки, ниже COMPAT_FLOOR — отказ; агент без
`upd1` команд не получает; откат набора — локально или понижением с GitHub.
Сторона сервера вне хода: после восстановления на V ниже или ручного
понижения сервера шлюзы идут следом по одному; шлюз сверх набора
возвращается по hello; инвариант «умер сервер».

Агент — настоящий GatewayServices на временной базе и настоящий клиент
канала; ВПС на том конце — подставной (tests/unit/_linkclient_session_common),
GitHub — подставной список выпусков.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway import upd as gw_upd
from awgbot.domain.selfupdate import marks
from awgbot.infra import awglock, updates
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from awgbot.runtime.linkclient import upd as lc_upd
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._fleet_common import rel
from tests.unit._linkclient_session_common import KEY, _Conn, _reply

pytestmark = pytest.mark.integration

conf, vps, gateway_role = _c.conf, _c.vps, _c.gateway_role
BOTH = {"requires": "4.0.0"}
RELEASES = [rel("v4.0.0", both=True), rel("v4.0.1", both=True),
            rel("v4.1.0", main=BOTH, gw={"requires": "4.0.1", "needs_main": "4.1.0"})]


class _Agent(GatewayServices):
    """Настоящий агент; сеть, апдейтер и откат на месте — записываются."""

    def __init__(self, db):
        super().__init__(db)
        self.applied: list = []
        self.hold_ids: list = []
        self.github = list(RELEASES)
        self.github_calls = 0

    def apply_update(self, release, *, hold="") -> None:
        self.applied.append((release.tag, bool(hold)))
        self.hold_ids.append(hold)

    # канал клиента спрашивает у агента всё это — у агента без обвязки пусто
    def gw_snapshot(self) -> dict:
        return {"agent_version": config.INSTALLED_VERSION}

    def lan_feeds_applied_hash(self) -> str:
        return ""

    def services_applied_hash(self) -> str:
        return ""

    def own_applied_hash(self) -> str:
        return ""

    def keep_hello(self) -> dict:
        return {}


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gw.db")
    monkeypatch.setattr(config, "INSTALLED_VERSION", "4.0.0")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    d = Database(str(tmp_path / "gw.db"))
    d.init_schema()
    a = _Agent(d)
    a.set_link_role(True, server="4.1.0")

    def lr(**_):
        a.github_calls += 1
        return list(a.github)
    monkeypatch.setattr(updates, "list_releases", lr)
    monkeypatch.setattr(lc_upd, "PAUSE_S", (0, 0))
    yield a
    d.close()


def _restart(monkeypatch, version: str | None = None) -> None:
    """Новый процесс агента (после апдейтера): другая метка процесса."""
    if version is not None:
        monkeypatch.setattr(config, "INSTALLED_VERSION", version)
    monkeypatch.setattr(gw_upd, "_PROC", gw_upd.secrets.token_hex(4))


def _cmd(target: str, *, rid: str = "a1b2c3d4", hold: bool = True, digest: str | None = None) -> dict:
    r = next((x for x in RELEASES if x.tag == target), None)
    dg = digest if digest is not None else (f"sha256:{r.sha256}" if r else "")
    return {"t": "upd", "id": rid, "target": target, "digest": dg, "why": "fleet", "hold": hold}


async def _run(agent, msg: dict) -> list[dict]:
    """Команда через разбор клиента канала (без сессии): что ушло бы серверу."""
    client = linkclient.LinkClient(agent)
    sent: list[dict] = []

    async def send(kind, body=None, pad=0):
        sent.append({"t": kind, **(body or {})})
        return True
    client._send = send
    client._writer = object()
    await client._dispatch(msg)
    task = client.__dict__.get("_upd_task")
    if task is not None:
        await asyncio.wait_for(task, 5)
    return sent


# ── проверки ────────────────────────────────────────────────────────────────

async def test_a_command_is_checked_accepted_and_applied_with_hold(agent):
    sent = await _run(agent, _cmd("v4.0.1"))
    assert [s["state"] for s in sent] == ["accepted"] and sent[0]["step"] == "v4.0.1"
    assert agent.applied == [("v4.0.1", True)], "шаг набора поставлен без удержания"
    assert agent.upd_cmd()["state"] == "applying"
    assert agent.upd_panel() == {"state": "updating", "target": "v4.0.1"}


@pytest.mark.parametrize("case,why", [
    ("not_release", "агент — сборка не из выпуска (v4.9.9.1)"),
    ("below_floor", "v3.9.9 ниже версии совместимости v4.0.0"),
    ("missing", "выпуска v4.2.0 нет на GitHub"),
    ("barrier", "v4.0.2 за пропущенным выпуском v4.0.1"),
    ("digest", "digest v4.0.1 у GitHub не совпал с названным сервером AWG"),
    ("generation", "v4.0.1 несёт AmneziaWG поколения 2, у шлюза — 1"),
    ("needs_main", "v4.1.0 нужен сервер AWG новее (сейчас v4.0.1)"),
])
async def test_every_failed_check_is_refused_with_its_reason(agent, monkeypatch, case, why):
    target, digest = "v4.0.1", None
    if case == "not_release":
        monkeypatch.setattr(config, "INSTALLED_VERSION", "4.9.9.1")
    elif case == "below_floor":
        target = "v3.9.9"
    elif case == "missing":
        target, digest = "v4.2.0", ""
    elif case == "barrier":
        broken = updates.Release(tag="v4.0.1", version=(4, 0, 1), body="", asset_url="u", sha256="b" * 64,
                                 meta=None, meta_bad="битые метаданные")
        agent.github = [RELEASES[0], broken, rel("v4.0.2", both=True)]
        target, digest = "v4.0.2", f"sha256:{rel('v4.0.2', both=True).sha256}"
    elif case == "digest":
        digest = "sha256:" + "0" * 64
    elif case == "generation":
        agent.github = [RELEASES[0], rel("v4.0.1", both=True, gen=2)]
    elif case == "needs_main":
        agent.set_link_role(True, server="4.0.1")
        target = "v4.1.0"
        monkeypatch.setattr(config, "INSTALLED_VERSION", "4.0.1")
    sent = await _run(agent, _cmd(target, digest=digest))
    assert [s["state"] for s in sent] == ["refused"], sent
    assert sent[0]["error"] == why
    assert agent.applied == []


async def test_two_steps_in_one_command_follow_the_chain_of_minimums(agent, monkeypatch):
    """v4.1.0 требует 4.0.1: первая ступень — v4.0.1, после перезапуска — v4.1.0,
    та же команда; done — только на цели."""
    sent = await _run(agent, _cmd("v4.1.0"))
    assert sent[0]["step"] == "v4.0.1" and agent.applied == [("v4.0.1", True)]
    assert agent.hold_ids == ["a1b2c3d4"], "удержание без id команды"
    _restart(monkeypatch, "4.0.1")                                 # новый процесс
    assert agent.upd_resume() == "next"
    client = linkclient.LinkClient(agent)
    got: list = []

    async def send(kind, body=None, pad=0):
        got.append({"t": kind, **(body or {})})
        return True
    client._send, client._writer = send, object()
    await client._upd_run()
    assert agent.applied[-1] == ("v4.1.0", True) and got[-1]["step"] == "v4.1.0"
    _restart(monkeypatch, "4.1.0")
    assert agent.upd_resume() == "done"
    assert agent.upd_ack_pending()["state"] == "done"


async def test_the_answer_waits_for_the_guard_and_survives_a_restart(agent, monkeypatch, tmp_path):
    """После перезапуска «done» уходит первой сессией — и только когда сторож
    снят (удержание прежнего уже на месте)."""
    await _run(agent, _cmd("v4.0.1"))
    _restart(monkeypatch, "4.0.1")
    (tmp_path / "update-guard").write_text("VERSION=4.0.1\n", encoding="utf-8")
    monkeypatch.setattr(lc_upd, "SETTLE_POLL_S", 0.01)
    client = linkclient.LinkClient(agent)
    got: list = []

    async def send(kind, body=None, pad=0):
        got.append({"t": kind, **(body or {})})
        return True
    client._send, client._writer = send, object()
    task = asyncio.get_running_loop().create_task(client._upd_resume())
    await asyncio.sleep(0.05)
    assert got == [], "«done» раньше, чем сторож снят"
    (tmp_path / "update-guard").unlink()
    await asyncio.wait_for(task, 2)
    assert [g["state"] for g in got] == ["done"] and agent.upd_ack_pending() == {}


async def test_a_guard_rollback_is_reported_as_rolled_back(agent, tmp_path, monkeypatch):
    await _run(agent, _cmd("v4.0.1"))
    _restart(monkeypatch)
    (tmp_path / "update-rolled-back").write_text(json.dumps({"version": "4.0.1", "prev": "4.0.0"}), encoding="utf-8")
    notes: list = []

    async def notify(text, *, silent=False):
        notes.append((text, silent))
    monkeypatch.setattr(linkclient, "_notify", notify)
    client = linkclient.LinkClient(agent)
    got: list = []

    async def send(kind, body=None, pad=0):
        got.append({"t": kind, **(body or {})})
        return True
    client._send, client._writer = send, object()
    await client._upd_resume()
    assert got[-1]["state"] == "rolled_back" and got[-1]["error"] == "обновлённый агент не вышел на связь за 10 минут"
    assert notes and notes[0][1] is True
    assert notes[0][0].startswith("⚠️ Обновление до v4.0.1 не удалось — агент вернулся на v4.0.0: "
                                  "обновлённый агент не вышел на связь за 10 минут. Подробности — в чате")
    assert agent.upd_panel()["state"] == "failed"


async def test_a_busy_agent_waits_for_the_fact_and_then_refuses(agent, monkeypatch):
    monkeypatch.setattr(agent, "rekey_state", lambda: {"state": "switched"})
    monkeypatch.setattr(lc_upd, "BUSY_POLL_S", 0)
    monkeypatch.setattr(lc_upd, "svc_busy_wait", lambda: 0)
    sent = await _run(agent, _cmd("v4.0.1"))
    assert sent[-1]["state"] == "refused" and sent[-1]["error"] == "занят: идёт смена ключей линка"
    # смена ключей закончилась вовремя — команда идёт
    state = {"n": 0}

    def rekey():
        state["n"] += 1
        return {"state": "switched"} if state["n"] < 3 else {}
    monkeypatch.setattr(agent, "rekey_state", rekey)
    monkeypatch.setattr(lc_upd, "svc_busy_wait", lambda: 600)
    sent = await _run(agent, _cmd("v4.0.1", rid="b1b2c3d4"))
    assert sent[-1]["state"] == "accepted" and state["n"] == 3


async def test_an_agent_newer_than_the_fleet_goes_down_without_a_chain(agent, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "4.1.0")
    sent = await _run(agent, _cmd("v4.0.0", hold=False))
    assert sent[0]["state"] == "accepted" and sent[0]["step"] == "v4.0.0"
    assert agent.applied == [("v4.0.0", False)]
    agent._upd_save({})                                            # та команда доведена
    sent = await _run(agent, _cmd("v3.9.9", rid="c1c2c3c4", hold=False, digest=""))
    assert sent[0]["state"] == "refused" and "ниже версии совместимости" in sent[0]["error"]


async def test_the_same_command_again_gets_the_current_answer(agent):
    await _run(agent, _cmd("v4.0.1"))
    sent = await _run(agent, _cmd("v4.0.1"))
    assert [s["state"] for s in sent] == ["accepted"] and agent.applied == [("v4.0.1", True)]
    sent = await _run(agent, _cmd("v4.0.1", rid="d1d2d3d4"))
    assert sent[0]["state"] == "refused" and sent[0]["error"].startswith("занят: идёт обновление")


async def test_revert_is_local_when_the_old_one_is_held(agent, monkeypatch):
    await _run(agent, _cmd("v4.0.1"))
    _restart(monkeypatch, "4.0.1")
    agent.upd_resume()
    spawned: list = []
    import awgbot.infra.detach as detach
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, **k: spawned.append(argv[1:]))
    monkeypatch.setattr(marks, "revert_check", lambda: 0)
    calls = agent.github_calls
    sent = await _run(agent, {"t": "upd_revert", "id": "a1b2c3d4", "to": "v4.0.0"})
    assert spawned == [["__update_revert"]] and sent == [] and agent.github_calls == calls, "откат пошёл на GitHub"
    _restart(monkeypatch, "4.0.0")                                   # новый (прежний) процесс
    assert agent.upd_resume() == "reverted"
    assert agent.upd_ack_pending()["state"] == "reverted"


async def test_revert_without_the_old_one_goes_down_from_github(agent, monkeypatch):
    await _run(agent, _cmd("v4.0.1"))
    _restart(monkeypatch, "4.0.1")
    agent.upd_resume()
    monkeypatch.setattr(marks, "revert_check", lambda: 2)
    sent = await _run(agent, {"t": "upd_revert", "id": "a1b2c3d4", "to": "v4.0.0"})
    assert agent.applied[-1] == ("v4.0.0", False) and sent == []
    _restart(monkeypatch, "4.0.0")
    assert agent.upd_resume() == "reverted"


async def test_commit_drops_the_held_old_one(agent, monkeypatch):
    await _run(agent, _cmd("v4.0.1"))
    spawned: list = []
    import awgbot.infra.detach as detach
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, **k: spawned.append(argv[1:]))
    await _run(agent, {"t": "upd_commit", "id": "ffffffff"})
    assert spawned == [], "чужая фиксация исполнена"
    await _run(agent, {"t": "upd_commit", "id": "a1b2c3d4"})
    assert spawned == [["__update_commit"]]


async def test_a_reconnect_while_the_updater_works_is_not_a_rollback(agent, monkeypatch, tmp_path):
    """Сессия переподключилась в окне «скачивание → сборка → остановка» (тот
    же процесс): ложного rolled_back нет; апдейтер умер, не доведя, — failed."""
    await _run(agent, _cmd("v4.0.1"))
    (tmp_path / "update-running").write_text("999999\n", encoding="utf-8")
    assert agent.upd_resume() == "" and agent.upd_cmd()["state"] == "applying", "ложный откат набора"
    cmd = agent.upd_cmd()
    cmd["applied_at"] = "2020-01-01T00:00:00+00:00"                # прошло больше 90 секунд, апдейтера нет
    agent._upd_save(cmd)
    assert agent.upd_resume() == "failed"
    assert agent.upd_ack_pending()["state"] == "failed"


async def test_a_repeated_revert_does_not_start_a_second_one(agent, monkeypatch):
    await _run(agent, _cmd("v4.0.1"))
    _restart(monkeypatch, "4.0.1")
    agent.upd_resume()
    spawned: list = []
    import awgbot.infra.detach as detach
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, **k: spawned.append(argv[1:]))
    monkeypatch.setattr(marks, "revert_check", lambda: 0)
    await _run(agent, {"t": "upd_revert", "id": "a1b2c3d4", "to": "v4.0.0"})
    sent = await _run(agent, {"t": "upd_revert", "id": "a1b2c3d4", "to": "v4.0.0"})
    assert spawned == [["__update_revert"]], "второй __update_revert тем же id"
    assert sent and sent[0]["t"] == "upd_ack"


def test_the_agent_tells_the_server_it_takes_the_command():
    from awgbot.runtime.linkclient.common import CAPS
    assert "upd1" in CAPS


def test_the_applied_note_of_the_agent_is_one_silent_line(agent):
    agent._upd_save({"id": "a1b2c3d4", "target": "v4.0.1", "state": "applying", "step": "v4.0.1"})
    note = agent.update_applied_note("v4.0.1", True)
    assert note.text == "✅ Агент обновлён до v4.0.1 по команде сервера AWG" and note.silent
    agent._upd_save({"id": "a1b2c3d4", "target": "v4.1.0", "state": "applying", "step": "v4.0.1"})
    assert agent.update_applied_note("v4.0.1", True) is None, "промежуточная ступень сказала «обновлён»"
    agent._upd_save({})
    from awgbot.domain.selfupdate.core import DEFAULT_NOTE
    assert agent.update_applied_note("v4.0.1", True) is DEFAULT_NOTE, "обновление руками потеряло обычный итог"


# ── живой канал: команда, ответ, перезапуск ────────────────────────────────

async def test_a_command_over_the_wire_and_its_answer_after_the_restart(agent, vps, gateway_role, monkeypatch):
    """Подставной ВПС шлёт upd после role; агент отвечает accepted той же
    сессией; новая сессия после «перезапуска» несёт done первым делом после hello."""
    conn = _Conn([_reply(KEY, "role", {"active": True, "server": "4.1.0"}),
                  _reply(KEY, "upd", {k: v for k, v in _cmd("v4.0.1").items() if k != "t"}, seq=2, first=False)],
                 hang=True)
    vps.sessions.append(conn)
    linkclient.ensure(agent)
    for _ in range(200):
        if agent.applied:
            break
        await asyncio.sleep(0.01)
    assert agent.applied == [("v4.0.1", True)]
    kinds = [gwlink.unpack(KEY, line, nonce=_c.SN).get("t") for line in conn.written[1:]]
    assert "upd_ack" in kinds
    conn.close()


# ── сервер ведёт шлюзы вслед за собой вне хода: по одному ──────────────────

from tests.unit import _fleet_common as _fc       # noqa: E402

fleet = _fc.fleet
DOWN_RELEASES = [rel("v3.4.0", both=True), rel("v3.4.1", both=True), rel("v3.4.2", both=True)]


def _two_gateways_above(fleet, version="3.4.2"):
    fleet.releases(*DOWN_RELEASES)
    fleet.slot(1, version)
    fleet.slot(2, version)
    fleet.active(1)
    fleet.s.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.2", "state": "done", "steps": [], "prev": "v3.4.1"})


@pytest.mark.parametrize("how", ["restore", "manual"])
def test_the_server_went_down_and_the_gateways_follow_one_at_a_time(fleet, how):
    """Восстановление на V ниже (или ручной `awg-bot update` вниз): выпуск
    набора — версия сервера; шлюзы выше получают `upd` до версии роли в V,
    по одному, без цепочки и без удержания."""
    _two_gateways_above(fleet)
    fleet.server("3.4.1")
    if how == "restore":
        fleet.s.fleet_after_restore("3.4.1")
        assert fleet.run() == {}, "ход прежнего выпуска пережил восстановление"
    out = fleet.tick()
    sent = fleet.sent(out)
    assert len(sent) == 1, "шлюзы пошли вниз не по одному"
    first, body = sent[0]
    assert body["target"] == "v3.4.1" and body["why"] == "follow" and body["hold"] is False
    assert fleet.sent(fleet.tick()) == [], "второй шлюз пошёл, пока первый посреди команды"
    out = fleet.gw_done(first, "3.4.1")
    assert [n.text for n in out.notes] and all(n.silent for n in out.notes)
    second = fleet.sent(out) + fleet.sent(fleet.tick())     # ответ первого — такт: следующий сразу
    assert [s for s, _ in second] == [3 - first] and second[0][1]["target"] == "v3.4.1"


def test_a_gateway_updated_by_hand_above_the_fleet_is_brought_back_on_hello(fleet):
    fleet.releases(*DOWN_RELEASES)
    fleet.server("3.4.1")
    fleet.slot(1, "3.4.1")
    assert fleet.sent(fleet.tick()) == []
    out = fleet.hello(1, "3.4.2")                       # ручной `update` на шлюзе — вышел на связь новее набора
    sent = fleet.sent(out)
    assert sent and sent[0][1]["target"] == "v3.4.1" and sent[0][1]["why"] == "follow"


def test_a_gateway_gone_mid_command_does_not_hold_the_others(fleet):
    _two_gateways_above(fleet)
    fleet.server("3.4.1")
    first = fleet.sent(fleet.tick())[0][0]
    fleet.offline(first)
    sent = fleet.sent(fleet.tick())
    assert [s for s, _ in sent] == [3 - first], "пропавший посреди команды держит остальных"


def test_dead_server_invariant(fleet, monkeypatch):
    """Умер сервер: новый — новейшей версии, бэкап снят на V ниже. Переход —
    вниз на V (выпуск с GitHub, digest метки); после него сервер на V, а
    шлюзы — на версии своей роли в V, даже если сами были новее."""
    from awgbot.domain.fleet import restore as fr
    v341 = rel("v3.4.1", main=BOTH)                    # V: выпуск только для сервера — агент в нём v3.4.0
    releases = [rel("v3.4.0", both=True), v341, rel("v3.4.2", both=True)]
    fleet.releases(*releases)
    fleet.server("3.4.2")
    monkeypatch.setattr(updates, "list_releases", lambda **kw: list(releases))
    plan = fr.plan_release({"version": "3.4.1", "digest": f"sha256:{v341.sha256}"})
    assert plan["kind"] == fr.DOWN and not plan["error"] and plan["release"].tag == "v3.4.1"
    fleet.slot(1, "3.4.2")                              # шлюз нового сервера — новейшей версии
    fleet.server("3.4.1")                               # restore --release довёл переход
    fleet.s.fleet_after_restore("3.4.1")
    assert fleet.s.fleet_release() == "v3.4.1"
    sent = fleet.sent(fleet.tick())
    assert sent and sent[0][1]["target"] == "v3.4.0", "шлюз не на версии роли в выпуске бэкапа"
