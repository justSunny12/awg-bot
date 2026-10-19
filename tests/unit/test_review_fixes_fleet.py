"""Набор обновлений после перепроверки выпуска: пересечения с заменой
устройства, сопряжением и сменой ключей линка.

- команда набора (gwupd) — с устройством слота: замена устройства её снимает,
  новое устройство чужой команды не наследует;
- откат хода не ждёт без срока шлюз на связи, который на upd_revert не
  отвечает (команда не о его установке), — через полчаса revert_failed;
- выпуск команды подключения или замены во время хода — отказ «идёт
  обновление набора»;
- ход не начинается, пока идёт смена ключей линка или сопряжение (как и они
  ждут ход), — отказ «links» и всплывашка кнопки;
- повторы догона (tries, next_at, final) сбрасываются, когда цель сменилась.

Цена ошибки: ROLLING_BACK навсегда (откат ждёт ответа, которого не будет);
новое устройство слота, получившее «удержание» и «пропал» прежнего; агент,
перезапущенный ходом посреди переключения ключей, — сожжённая попытка; шлюз,
который после восстановления сервера на другой выпуск больше не догоняет.
"""
from __future__ import annotations

import pytest

from awgbot.domain.fleet import order as fleet_order
from awgbot.domain.services.gwjoin.issue import JOIN_FLEET_BUSY
from awgbot.domain.services.types import ServiceError
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

BOTH = {"requires": "3.4.0"}


def _two_running(fleet):
    """Сервер и два шлюза, активный — слот 1 (очередь: 2, 1); ступень
    сервера пройдена, шлюз 2 обновился."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    assert fleet.start() == ""
    fleet.finish_server(fleet.tick(), "3.5.0")
    fleet.gw_done(2, "3.5.0")


# ── cross-1: откат не ждёт без срока шлюз на связи ──────────────────────────

def test_a_gateway_online_that_never_answers_the_revert_fails_it_after_half_an_hour(fleet):
    """Шлюз 2 на связи получил upd_revert и молчит (устройство слота сменили
    посреди хода — агент этой команды не знает): полчаса ход ждёт, затем
    revert_failed и откат идёт дальше — до итога rollback_failed."""
    _two_running(fleet)
    out = fleet.ack(1, "failed", error="сеть")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2]
    fleet.now_shift(fleet_order.GW_STEP_MAX_S - 60)
    out = fleet.tick()
    assert fleet.st(2)["state"] == "reverting" and out.server == "", "срок ещё не вышел, а откат пошёл дальше"
    fleet.now_shift(fleet_order.GW_STEP_MAX_S + 1)
    out = fleet.tick()
    assert fleet.st(2)["state"] == "revert_failed", "шлюз на связи без ответа держит откат без срока"
    assert "не ответил на откат за 30 минут" in fleet.st(2)["error"]
    assert out.server == "revert", "после неудачи шлюза откат не дошёл до сервера"
    fleet.s.fleet_server_act(out)
    fleet.server("3.4.0")
    fleet.tick()
    run = fleet.run()
    assert run["state"] == "rollback_failed" and run["revert_failed"] == [2], run


# ── cross-1: команда набора — с устройством слота ───────────────────────────

def test_replacing_the_device_forgets_the_fleet_command_of_that_slot_only(fleet):
    """gwlink_forget (замена устройства, «Настроить заново») снимает команду
    набора слота: id, удержание, «пропал», исчерпанные повторы прежнего
    устройства новому не достаются; у соседнего слота команда остаётся."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    old = {"id": "a1b2c3d4", "state": "lost", "hold": True, "run": "r1", "final": True, "tries": 3}
    fleet.s.fleet_slot_set(1, dict(old))
    fleet.s.fleet_slot_set(2, dict(old))
    fleet.s.gwlink_forget(1)
    assert fleet.st(1) == {}, "команда прежнего устройства пережила замену"
    assert fleet.st(2) == old, "забыта команда соседнего слота"


# ── cross-1: подключение и замена во время хода ─────────────────────────────

def test_no_join_command_is_issued_while_the_fleet_runs(fleet):
    """Ход идёт (или откатывается) — команда подключения и замены не выдаётся
    ни на шаге выбора слота, ни на выпуске; после итога хода — выдаётся."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    assert fleet.start() == ""
    for call in (lambda: fleet.s.gwjoin_peek_slot(), lambda: fleet.s.gwjoin_issue(slot_id=1)):
        with pytest.raises(ServiceError) as e:
            call()
        assert str(e.value) == JOIN_FLEET_BUSY
    assert fleet.s.gwjoin(1) == {} and fleet.s.gwjoin_all() == [], "запись сопряжения заведена посреди хода"
    fleet.s.fleet_run_set({**fleet.run(), "state": "rolling_back"})
    with pytest.raises(ServiceError):
        fleet.s.gwjoin_peek_slot()
    fleet.s.fleet_run_set({**fleet.run(), "state": "rolled_back"})
    assert fleet.s.gwjoin_peek_slot() == 2, "после итога хода подключение по-прежнему закрыто"


# ── вне оси: ход ждёт смену ключей и сопряжение ─────────────────────────────

@pytest.mark.parametrize("busy", ["rekey", "join", "closing"])
def test_the_fleet_does_not_start_during_a_link_rekey_or_a_pairing(fleet, busy):
    """Смена ключей линка (ручная или ради I1) или сопряжение, в том числе
    возврат после него (closing), — ход не начинается: отказ «links»; конец
    смены — ход идёт."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    if busy == "rekey":
        fleet.s._rekey_save(1, {"id": "a1b2c3d4e5f6", "state": "staged"})
    else:
        fleet.s._gwjoin_save(2, {"id": "0123456789abcdef", "slot": 2, "kind": "new",
                                 "state": "waiting" if busy == "join" else "closing"})
    assert fleet.start() == "links"
    assert fleet.run() == {} and fleet.applied == [], "ход начат посреди смены ключей"
    fleet.s._rekey_save(1, {})
    fleet.s._gwjoin_save(2, {})
    assert fleet.start() == "" and fleet.run()["state"] == "running"


async def test_the_button_says_why_the_fleet_waits(fleet, fake_bot, monkeypatch):
    """«⬆️ Обновить» при идущей смене ключей — всплывашка с причиной, хода нет."""
    from awgbot.bot.handlers import fleet as fleet_flow
    from awgbot.bot.texts import fleet as ft
    from awgbot.core import config
    from awgbot.infra import updates
    from tests.conftest import FakeCallback, FakeMessage
    releases = [rel("v3.4.0", both=True), rel("v3.5.0", both=True)]
    fleet.releases(*releases)
    fleet.slot(1)
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(releases))
    fleet.s._rekey_save(1, {"id": "a1b2c3d4e5f6", "state": "switched"})
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=fake_bot)
    await fleet_flow.start(cb, fleet.s, "v3.5.0")
    assert cb.answers[-1] == (ft.UPDATE_LINKS_BUSY_TOAST, True), cb.answers
    assert fleet.run() == {}


# ── cross-3: повторы догона — о своей цели ──────────────────────────────────

def test_exhausted_retries_do_not_stop_the_gateway_from_following_a_new_target(fleet):
    """Три неудачных догона до v3.4.1 — повторов больше нет; сервер перешёл на
    v3.4.2 (восстановление бэкапа) — у новой цели счёт с нуля, шлюз получает
    команду по следующему такту."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH), rel("v3.4.2", both=True))
    fleet.slot(1, online=False)
    fleet.s.fleet_release_set("v3.4.1")
    fleet.s.fleet_slot_set(1, {"id": "a1b2c3d4", "state": "failed", "target": "v3.4.1", "tries": 3,
                               "final": True, "next_at": "", "why": "catchup"})
    fleet.online(1, "3.4.0")
    assert fleet.tick().send == [], "сценарий собран не так: исчерпанный догон снова пошёл"
    fleet.server("3.4.2")
    fleet.s.fleet_after_restore("3.4.2")
    sent = fleet.sent(fleet.tick())
    assert [s for s, _ in sent] == [1], "исчерпанные повторы прежней цели держат шлюз на месте"
    st = fleet.st(1)
    assert st["target"] == "v3.4.2" and not st.get("final") and int(st.get("tries") or 0) == 0, st


def test_a_manual_rekey_is_refused_while_the_fleet_runs(fleet, monkeypatch):
    """Симметрия: «🔑 Сменить» и смена ради I1 посреди хода — отказ (ход
    перезапускает агента, смена сгорела бы), скрипт линка не тронут; смена по
    коду сопряжения (auto=join) — не ход набора, её не держит."""
    runs = []
    monkeypatch.setattr(fleet.s, "_rekey_run", lambda gw, mode, *a: runs.append(mode) or "скрипт подменён")
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    assert fleet.start() == ""
    for auto in ("", "i1"):
        with pytest.raises(ServiceError) as e:
            fleet.s.gateway_rekey_stage(1, auto=auto)
        assert str(e.value) == "Идёт обновление набора — смена ключей линка после его итога", auto
    assert runs == [], "скрипт линка запущен посреди хода"
    with pytest.raises(ServiceError) as e:
        fleet.s.gateway_rekey_stage(1, auto="join")
    assert runs == ["--rekey-stage"] and "сгенерированы" in str(e.value), "смену по коду держит ход набора"


def test_retry_waits_for_a_link_rekey_and_keeps_the_final_on_screen(fleet):
    """«🔁 Повторить» после отката во время смены ключей линка — отказ «links»,
    итог хода (rolled_back) остаётся на экране; конец смены — ход заново."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    assert fleet.start() == ""
    fleet.s.fleet_run_set({**fleet.run(), "state": "rolled_back"})
    fleet.s._rekey_save(1, {"id": "a1b2c3d4e5f6", "state": "staged"})
    assert fleet.s.fleet_retry() == "links"
    assert fleet.run()["state"] == "rolled_back", "итог хода закрыт, а новый ход не начат"
    fleet.s._rekey_save(1, {})
    assert fleet.s.fleet_retry() == "" and fleet.run()["state"] == "running"
