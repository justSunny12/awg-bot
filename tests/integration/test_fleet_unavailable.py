"""Набор обновлений и недоступные шлюзы (Р21, Р22): недоступный никого не
держит и догоняет по hello сразу, без вопроса; неудачный догон набор не
откатывает — повторы, после третьей — строка ⚠️; вернувшийся посреди хода —
участник в конце очереди, его неудача — откат; вернувшийся после отката с
новой версией — upd_revert; пропавший посреди своего шага — через 15 минут
недоступный, ход идёт дальше.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.bot.texts import fleet as ft
from awgbot.util import timeutil
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.integration

BOTH = {"requires": "3.4.0"}


def _gw_release(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))


def test_an_unavailable_gateway_is_skipped_and_catches_up_on_hello(fleet):
    _gw_release(fleet)
    fleet.slot(1)
    fleet.slot(2, online=False)
    fleet.active(1)
    fleet.start()
    out = fleet.tick()
    assert [s for s, _ in fleet.sent(out)] == [1], "недоступный шлюз получил команду или держит ход"
    out = fleet.gw_done(1, "3.4.1")
    assert fleet.run()["state"] == "done", "набор ждёт недоступный шлюз"
    text = ft.fleet_live(fleet.s.fleet_run_plan())
    assert text.startswith("✅ <b>Обновлено до v3.4.1</b>: «Шлюз 1»")
    assert "🟡 «Шлюз 2» обновится, когда выйдет на связь" in text
    out = fleet.hello(2, "3.4.0")
    sent = fleet.sent(out)
    assert [s for s, _ in sent] == [2] and sent[0][1]["why"] == "catchup" and sent[0][1]["hold"] is False
    out = fleet.gw_done(2, "3.4.1")
    assert [n.text for n in out.notes] == ["✅ «Шлюз 2» вышел на связь и обновлён до v3.4.1"]
    assert out.notes[0].silent


def test_the_catch_up_starts_on_the_server_start_too(fleet):
    """Сервер поднялся, шлюз уже на связи и отстал — команда без вопроса."""
    _gw_release(fleet)
    fleet.slot(1, "3.4.0")
    fleet.s.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.1", "state": "done", "steps": [], "prev": "v3.4.0"})
    fleet.s.fleet_release_set("v3.4.1")             # его пишет фиксация хода
    out = fleet.s.fleet_on_start().merge(fleet.tick())
    sent = fleet.sent(out)
    assert [s for s, _ in sent] == [1] and sent[0][1]["why"] == "catchup"


def test_a_failed_catch_up_does_not_roll_back_and_retries_with_jitter(fleet, monkeypatch):
    import random
    _gw_release(fleet)
    fleet.slot(1, online=False)
    fleet.start()
    assert fleet.tick().send == [] and fleet.run()["state"] == "done"
    monkeypatch.setattr(random, "uniform", lambda lo, hi: hi)       # крайний джиттер +40 %
    out = fleet.hello(1, "3.4.0")
    delays = []
    for tries, want in ((1, 30 * 60), (2, 2 * 3600), (3, 6 * 3600)):
        before = timeutil.now()
        out = fleet.ack(1, "failed", error="GitHub не ответил")
        assert fleet.run()["state"] == "done", "неудачный догон откатил зафиксированный набор"
        st = fleet.st(1)
        at = timeutil.parse_iso(st["next_at"])
        delays.append(round((at - before).total_seconds() / want, 1))
        assert out.notes[0].text.endswith(
            "вышел на связь, но не обновился до v3.4.1: GitHub не ответил — повторю через "
            + {1: "30 минут", 2: "2 часа", 3: "6 часов"}[tries])
        assert fleet.tick().send == [], "повтор раньше срока"
        monkeypatch.setattr(timeutil, "now", lambda at=at: at + datetime.timedelta(seconds=1))
        out = fleet.tick()
        assert [s for s, _ in fleet.sent(out)] == [1], "повтор не пошёл в срок"
    assert delays == [1.4, 1.4, 1.4], "повторов не три (30 мин, 2 ч, 6 ч)"
    out = fleet.ack(1, "failed", error="GitHub не ответил")
    st = fleet.st(1)
    assert st["final"] is True
    assert [n.text for n in out.notes] == [
        "⚠️ «Шлюз 1» не обновился до v3.4.1: GitHub не ответил — повторов больше не будет, «🔁 Повторить» — "
        "в разделе обновлений"], "последняя неудача без строки (Т-У7б)"
    monkeypatch.setattr(timeutil, "now", lambda: datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))
    assert fleet.tick().send == [], "после третьей неудачи повторы не прекратились"
    plan = fleet.s.fleet_plan(None)
    assert "🛰 «Шлюз 1»: v3.4.0 · ⚠️ не обновился до v3.4.1 после выхода на связь: GitHub не ответил" \
        in ft.fleet_section(plan)
    assert fleet.s.fleet_retry() == "" and fleet.st(1)["final"] is False
    assert [s for s, _ in fleet.sent(fleet.tick())] == [1], "«🔁 Повторить» не вернул догон"


def test_a_gateway_back_mid_run_joins_the_end_of_the_queue_and_its_failure_rolls_back(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.slot(3, online=False)
    fleet.active(1)
    fleet.start()
    out = fleet.tick()
    assert [s for s, _ in fleet.sent(out)] == [2]
    fleet.hello(3, "3.4.0")                                     # вернулся посреди хода
    out = fleet.gw_done(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out)] == [3], "вернувшийся не в очереди хода"
    out = fleet.ack(3, "failed", error="место кончилось")
    assert fleet.run()["state"] == "rolling_back"
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2]


def test_a_gateway_back_after_the_rollback_with_the_new_version_is_reverted(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    fleet.tick()
    fleet.gw_done(2, "3.4.1")
    fleet.offline(2)                                            # обновился и пропал
    out = fleet.ack(1, "failed", error="сеть")
    assert fleet.run()["state"] == "rolled_back" and fleet.sent(out, "upd_revert") == []
    out = fleet.hello(2, "3.4.1")
    reverts = fleet.sent(out, "upd_revert")
    assert [s for s, _ in reverts] == [2] and reverts[0][1]["to"] == "v3.4.0"
    fleet.online(2, "3.4.0")
    fleet.ack(2, "reverted")
    assert fleet.st(2)["hold"] is False and fleet.tick().send == []


def test_a_gateway_gone_mid_step_is_unavailable_after_15_minutes(fleet, monkeypatch):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    fleet.tick()
    fleet.ack(2, "accepted")
    fleet.offline(2)
    assert fleet.tick().send == [], "ход пошёл дальше сразу после обрыва"
    later = timeutil.now() + datetime.timedelta(minutes=14)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    assert fleet.tick().send == []
    later2 = later + datetime.timedelta(minutes=2)
    monkeypatch.setattr(timeutil, "now", lambda: later2)
    out = fleet.tick()
    assert fleet.st(2)["state"] == "lost" and [s for s, _ in fleet.sent(out)] == [1]
    assert "🟡 «Шлюз 2» — пропал посреди обновления, доведётся, когда выйдет на связь" in \
        ft.fleet_live(fleet.s.fleet_run_plan())
    out = fleet.gw_done(1, "3.4.1")
    assert fleet.run()["state"] == "done"
    out = fleet.hello(2, "3.4.1")                               # обновился и вернулся
    assert [s for s, _ in fleet.sent(out, "upd_commit")] == [2], "удержание пропавшего не доведено фиксацией"


def test_a_resent_command_keeps_its_id(fleet):
    """Команда, которую шлюз не получил (сессия упала до ответа), уходит снова
    тем же id — агент её узнаёт и не начинает второй раз."""
    _gw_release(fleet)
    fleet.slot(1)
    fleet.start()
    first = fleet.sent(fleet.tick())[0][1]
    out = fleet.hello(1, "3.4.0")
    again = fleet.sent(out)
    assert again and again[0][1]["id"] == first["id"]


def test_a_commit_that_did_not_reach_the_socket_comes_again_on_hello(fleet):
    """Фиксация отмечается сданной только после записи в сокет: сессия упала на
    отправке — удержание у шлюза не висит вечно, следующая сессия получает
    upd_commit снова."""
    _gw_release(fleet)
    fleet.slot(1)
    fleet.start()
    fleet.tick()
    fleet.drop = {1}
    out = fleet.gw_done(1, "3.4.1")
    assert fleet.run()["state"] == "done" and [s for s, _ in fleet.sent(out, "upd_commit")] == [1]
    assert fleet.st(1)["hold"] is True and fleet.st(1)["pending"] == "upd_commit", "несданная фиксация сняла удержание"
    fleet.drop = set()
    out = fleet.hello(1, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_commit")] == [1]
    assert fleet.st(1)["hold"] is False and fleet.st(1)["pending"] == ""
    assert fleet.sent(fleet.hello(1, "3.4.1"), "upd_commit") == [], "сданная фиксация ушла второй раз"


def test_a_revert_that_did_not_reach_the_socket_comes_again_on_hello(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    fleet.tick()
    fleet.gw_done(2, "3.4.1")
    fleet.drop = {2}
    out = fleet.ack(1, "failed", error="сеть")                # откат в ходе: шлюзу 2 — upd_revert, не ушёл
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2]
    assert fleet.st(2)["state"] == "reverting" and fleet.st(2)["pending"] == "upd_revert"
    fleet.drop = set()
    out = fleet.hello(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2], "несданный откат не повторён новой сессией"
    assert fleet.st(2)["pending"] == ""
    fleet.online(2, "3.4.0")
    fleet.ack(2, "reverted")
    assert fleet.run()["state"] == "rolled_back"
    # откат вне хода (шлюз вернулся после отката новой версией) — тот же путь
    fleet.s.fleet_slot_set(2, {**fleet.st(2), "state": "done", "hold": True, "pending": ""})
    fleet.drop = {2}
    out = fleet.hello(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2] and fleet.st(2)["pending"] == "upd_revert"
    fleet.drop = set()
    out = fleet.hello(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2] and fleet.st(2)["pending"] == ""
