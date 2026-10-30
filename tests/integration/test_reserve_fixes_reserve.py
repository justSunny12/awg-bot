"""Правки стороны резерва после двух ревью: процесс открытия
(domain/services/reserve/inbound.py — open.status до запуска, песочница
юнита, остановка юнита при срыве и возврате, повтор реплики и `relay`),
гостевой канал и кандидат кворума (gwguest.py, quorum/candidate.py — обрыв
сессии как подозрение, `state` шлюзам в releasing, отказ кода при чужих
пользователях здесь, «забираю» при занятой зоне) и `st` по смене тела
(session.py, такт живости runtime/scheduler/reserve.py).

Зона, systemd-run/systemctl, часы и канал — подмены (как в test_reserve_hosting
и tests/integration/_reserve_vote_common).

Цена ошибки: прежний open.status, принятый за ход нового открытия, — бот
резерва шлёт `up` основе по чужому шагу; токен резерва в окружении процесса
открытия — бот основы в зоне читает его; юнит открытия, не остановленный при
`back`, — процесс дописывает хранилище гостя уже после стирания; обрыв
сессии шлюза, принятый за «шлюза нет», — окно аварийного кода со звуком у
двух админов по каждому переподключению; шлюзы, снявшие пересылку в releasing, — пользователи основы теряют
связь до конца возврата; «забираю» при занятой зоне — переезд поверх
чужих пользователей.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import openproc
from awgbot.infra import rsvzone
from awgbot.runtime import pairlink
from awgbot.runtime.scheduler import reserve as jobs
from awgbot.util import rsvkeys
from tests.conftest import FakeBot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_vote_common import (PEER_ADMIN, T_UP, Cand, box, code_window_outside, gw_sent,
                                                    notes, sent)
from tests.integration.test_reserve_hosting import _serving, _taking, host
from tests.integration.test_reserve_jobs import _Sched

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["box", "host"]

CODE = rsvkeys.code_encode(bytes(range(16)))
STOP = ["systemctl", "stop", f"{openproc.OPEN_UNIT}.service"]


@pytest.fixture()
def units(host, monkeypatch):
    """systemctl на месте: остановку юнита открытия видно в журнале запусков."""
    monkeypatch.setattr(openproc.shutil, "which",
                        lambda name: f"/usr/bin/{name}" if name in ("systemd-run", "systemctl") else None)
    return host


# ── процесс открытия: запуск ────────────────────────────────────────────────

def test_the_open_status_says_launching_before_the_process_starts(host):
    """Прежний open.status (fail или done от прошлого открытия) не должен
    сойти за ход этого: до запуска — launching, после — launched."""
    seen = []
    real = host.spawn.Popen

    def popen(cmd, **kw):
        seen.append(dict(host.zone.status))
        return real(cmd, **kw)
    host.spawn.Popen = popen
    host.zone.status = {"step": "done"}                       # след прошлого открытия
    assert host.svc.reserve_go_req_in(7, "button") == ""
    assert seen and seen[0].get("step") == "launching", f"на момент запуска open.status: {seen}"
    assert host.zone.status["step"] == "launched"


def test_the_open_process_runs_without_the_reserves_token_and_cannot_read_its_base(host, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "DUMMY-reserve-token")
    monkeypatch.setenv("ADMIN_ID", "1")
    _taking(host)
    run = host.spawn.popen[0]
    cmd = " ".join(run["cmd"])
    assert "DUMMY-reserve-token" not in cmd and "BOT_TOKEN" not in cmd
    env = run["kw"].get("env") or {}
    assert "BOT_TOKEN" not in env and "ADMIN_ID" not in env, "токен резерва в окружении процесса открытия"
    assert "--setenv=AWG_BOT_ENV=/dev/null" in run["cmd"], "процесс читает env резерва"
    for prop in ("ProtectHome=yes", "ProtectSystem=strict", "NoNewPrivileges=yes"):
        assert prop in run["cmd"], f"нет {prop}"
    rw = [c for c in run["cmd"] if c.startswith("ReadWritePaths=")]
    assert rw and "-/etc/systemd" in rw[0].split("=", 1)[1].split(), "скрипт зоны не запишет юниты гостя"
    # каталог данных резерва закрыт временной ФС целиком (база, её WAL, стороны линков, копии);
    # видны только пакет и журнал второй стороны (на чтение)
    data = str(config.DATA_DIR)
    assert f"TemporaryFileSystem={data}:ro" in run["cmd"], f"каталог данных резерва открыт процессу: {run['cmd']}"
    binds = [c for c in run["cmd"] if c.startswith("BindReadOnlyPaths=")]
    assert binds == [f"BindReadOnlyPaths=-{data}/reserve/guest.pkg -{data}/reserve/jrn"], \
        f"процессу привязано не только пакет и журнал: {binds}"
    hidden = [c for c in run["cmd"] if c.startswith("InaccessiblePaths=")]
    assert any("env" in c for c in hidden), f"env резерва доступен процессу: {hidden}"


def test_a_second_go_while_the_first_is_opening_is_refused(host):
    _taking(host)
    assert "зона занята" in host.svc.reserve_go_req_in(7, "button")
    assert len(host.spawn.popen) == 1, "второй процесс открытия поверх первого"


@pytest.mark.parametrize("state", ["away", "leaving", "returning"])
def test_go_is_refused_while_my_own_clients_are_not_here(host, state):
    host.svc.reserve_set_out(state)
    why = host.svc.reserve_go_req_in(7, "button")
    assert "мои пользователи не на этом сервере" in why and not host.spawn.popen


# ── процесс открытия: остановка юнита ───────────────────────────────────────

def test_a_failed_opening_stops_the_open_unit(units):
    _taking(units)
    units.zone.status = {"step": "fail", "reason": "доли не сошлись"}
    units.svc.reserve_in_tick()
    assert STOP in units.spawn.runs, "юнит открытия жив после срыва — допишет хранилище после стирания"


def test_back_req_during_the_opening_stops_the_open_unit_and_while_serving_does_not(units):
    _taking(units)
    units.svc.reserve_back_req_in()
    assert STOP in units.spawn.runs
    units.spawn.runs.clear()
    units.svc.reserve_set_in("idle")
    _serving(units)
    units.svc.reserve_back_req_in()
    assert STOP not in units.spawn.runs, "открытие давно кончилось — стоп юнита не нужен"


def test_back_during_the_opening_stops_the_open_unit(units):
    """`back` основы, пока резерв ещё открывает пакет (срыв переезда на шаге
    up): зона стирается — процесс открытия обязан быть остановлен, иначе он
    дописывает хранилище гостя и отметку вебхука уже после стирания."""
    _taking(units)
    units.svc.reserve_back_in()
    assert STOP in units.spawn.runs, f"юнит открытия не остановлен при back: {units.spawn.runs}"


# ── зона снята мимо бота ────────────────────────────────────────────────────

@pytest.mark.parametrize("phase", ["idle", "", "relay"])
def test_a_zone_taken_down_outside_the_bot_ends_the_hosting_with_one_notice(host, phase):
    """`awg-bot reserve teardown` или перезагрузка хоста: бот резерва не
    должен вечно считать себя «serving» — idle, файервол заново, админу итог,
    шлюзам `state{idle}`. Пакет и доля второй стороны остаются (С11): основа
    может быть всё ещё мертва, кворум откроет снова."""
    svc = host.svc
    seen = []
    svc.reserve_gw_state_actions = lambda force=False: seen.append(force) or [("gw", 1, "state", svc._state_body())]
    _serving(host)
    host.zone.phase = phase
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and svc.rsv_get("rsv_in").get("reason") == "зона снята"
    assert ("fw",) in acts and svc.rsv_get("rsv_pkg_in").get("gen") == 7, f"пакет стёрт — открыть снова нечем: {acts}"
    texts = [n.text for a in acts if a[0] == "notify" for n in a[1]]
    assert len(texts) == 1 and "зона снята" in texts[0] and "Beta" in texts[0], texts
    gw = [a for a in acts if a[:3] == ("gw", 1, "state")]
    assert seen == [True] and gw and gw[0][3]["state"] == "idle", f"шлюзы держат пересылку в снятую зону: {acts}"
    assert svc.reserve_in_tick() == [], "итог повторяется на каждом такте"


# ── возврат: повторы ────────────────────────────────────────────────────────

def test_a_repeated_back_req_after_the_replica_sends_the_last_replica_again(host):
    """Обрыв канала на частях released: основа просит снова — уходит та же
    реплика с диска, гость второй раз не останавливается."""
    svc = host.svc
    host.zone.rep.parent.mkdir(parents=True, exist_ok=True)
    host.zone.rep.write_bytes(b"\xca\xfe")
    _serving(host)
    svc.reserve_back_req_in()
    svc.reserve_guest_replica_in(b"\xca\xfe")
    assert svc.reserve_back_req_in() == [("send_released", {"gen": 7, "blob": "cafe"})]
    assert svc.reserve_in() == "releasing"


def test_a_back_req_while_releasing_without_a_replica_yet_waits(host):
    _serving(host)
    host.svc.reserve_back_req_in()
    assert host.svc.reserve_back_req_in() == [], "реплики ещё нет — отдавать нечего, гость уже останавливается"


# ── гостевой канал: обрыв сессии шлюза ──────────────────────────────────────

@pytest.fixture()
def hermetic(box, tmp_path, monkeypatch):
    """Ход процесса открытия — во временный каталог, не в /run хоста."""
    monkeypatch.setattr(rsvzone, "STATE_DIR", tmp_path / "rsv")
    monkeypatch.setattr(rsvzone, "OPEN_STATUS", tmp_path / "rsv" / "open.status")
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    return box


def _seen_by_one_gateway(box) -> Cand:
    """Свой «не вижу», шлюз 1 «Beta» видит основу, шлюз 2 молчит — окна кода нет."""
    box.peer_admin_opened()
    c = Cand(box)
    c.b.gone(2)
    c.own("uns")
    c.view(1, "see")
    c.tick()
    assert not box.s.reserve_code_window()["open"], "подготовка: окно кода при видящем шлюзе"
    return c




def test_the_code_is_refused_while_my_own_clients_are_at_the_other_server(hermetic):
    box = hermetic
    box.peer_admin_opened()
    code_window_outside(Cand(box))
    box.s.reserve_set_out("away")
    res = box.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and not box.launched, "код открыл пакет поверх отъезда моих пользователей"
    assert any("не на этом сервере" in n.text for n in notes(res.get("actions"))), res


def test_the_opening_by_the_code_writes_launching_before_the_launch(hermetic):
    box = hermetic
    seen = []
    real = box._launch
    box.s._rsvopen_launch = lambda payload: (seen.append(rsvzone.open_status().get("step")), real(payload))
    box.peer_admin_opened()
    code_window_outside(Cand(box))
    assert box.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    assert seen == ["launching"], f"open.status на момент запуска: {seen}"


# ── state шлюзам ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inn, want", [("taking", "serving"), ("serving", "serving"), ("releasing", "serving"),
                                       ("relay", "idle"), ("idle", "idle")])
def test_gateways_keep_serving_until_the_return_is_over(hermetic, inn, want):
    """В releasing пользователи основы ещё здесь: шлюзы, снявшие пересылку, оставили
    бы их без РФ-части до конца возврата."""
    box = hermetic
    box.hello(1)
    box.s.reserve_set_in(inn, gen=3)
    got = gw_sent(box.s.reserve_gw_state_actions(force=True), "state")
    assert got and got[0][1]["state"] == want, f"{inn}: {got}"


# ── «забираю» при занятой зоне ──────────────────────────────────────────────

def _majority(c) -> list[tuple]:
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    return c.tick() + c.run(T_UP + 5)


def test_the_take_is_refused_while_my_own_users_are_away_and_the_card_says_why(hermetic):
    c = Cand(hermetic)
    hermetic.s.reserve_set_out("away")
    acts = _majority(c)
    assert not gw_sent(acts, "qtake") and not sent(acts, "ask"), f"«забираю» при моих пользователях не здесь: {acts}"
    assert hermetic.s.rsv_get("rsv_view").get("auto_block") == "мои пользователи не на этом сервере"


def test_the_tail_relay_does_not_hold_the_zone_busy_for_the_take(hermetic):
    """Пересылка хвоста (relay) уступает новому переезду: зона свободна,
    большинство «нет» забирает, как при idle."""
    c = Cand(hermetic)
    hermetic.s.reserve_set_in("relay")
    acts = _majority(c)
    assert gw_sent(acts, "qtake") and sent(acts, "ask"), "relay держит зону занятой — пользователи основы без VPN-бота"


# ── st по смене ─────────────────────────────────────────────────────────────

def test_st_goes_again_only_when_its_body_changes(mv, monkeypatch):
    svc = mv.svc
    assert svc.reserve_st_if_changed() is not None, "первая отправка"
    assert svc.reserve_st_if_changed() is None, "то же тело — второй раз"
    monkeypatch.setattr(svc, "reserve_mem_free_mb", lambda: 12345)
    svc.reserve_guest_online_set(9)
    assert svc.reserve_st_if_changed() is None, "память и число на связи — не повод для st"
    svc.reserve_set_out("away")
    body = svc.reserve_st_if_changed()
    assert body is not None and body["out"] == "away", "смена направления не дошла до второй стороны"
    svc.reserve_st_sent_reset()
    assert svc.reserve_st_if_changed() is not None, "неудачная отправка — отпечаток не сброшен"


def test_st_sent_by_the_session_is_not_sent_again_by_the_tick(mv):
    """Сессия в начале шлёт st сама (reserve_st_body_sent) — такт живости
    дубль того же тела не шлёт; новое открытие сессии отпечаток не сбрасывает."""
    svc = mv.svc
    svc.reserve_session_opened({})
    body = svc.reserve_st_body_sent()
    assert body["out"] == "home"
    assert svc.reserve_st_if_changed() is None, "такт повторил st, отправленный сессией"
    svc.reserve_session_opened({})
    assert svc.reserve_st_if_changed() is None
    svc.reserve_set_out("away")
    assert svc.reserve_st_if_changed() is not None


async def test_the_liveness_job_sends_st_on_a_change_and_again_after_a_failed_send(mv, monkeypatch):
    svc = mv.svc
    box = {"ok": True}
    got: list = []

    async def send(kind, body=None):
        got.append(kind)
        return box["ok"]
    monkeypatch.setattr(pairlink, "send", send)
    monkeypatch.setattr(pairlink, "online", lambda: True)
    monkeypatch.setattr(svc, "reserve_liveness_tick", lambda now=None: (None, []))
    monkeypatch.setattr(svc, "reserve_gwguest_tick", lambda: [])
    monkeypatch.setattr(svc, "quorum_tick", lambda: [])          # кворум — своим тактом, здесь только st
    monkeypatch.setattr(svc, "quorum_runtime_plan", lambda: {"responder": None, "probes": {}})
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, svc, FakeBot())
    fn = sched.jobs["rsv_liveness"][0]
    await fn()
    await fn()
    assert got == ["st"], f"st без смены тела: {got}"
    box["ok"] = False
    svc.reserve_set_out("away")
    await fn()
    box["ok"] = True
    await fn()
    assert got == ["st", "st", "st"], f"st после неудачной отправки не повторён: {got}"


async def test_the_liveness_job_sends_no_st_without_the_pair_channel(mv, monkeypatch):
    got: list = []

    async def send(kind, body=None):
        got.append(kind)
        return True
    monkeypatch.setattr(pairlink, "send", send)
    monkeypatch.setattr(pairlink, "online", lambda: False)
    monkeypatch.setattr(mv.svc, "reserve_liveness_tick", lambda now=None: (None, []))
    monkeypatch.setattr(mv.svc, "reserve_gwguest_tick", lambda: [])
    monkeypatch.setattr(mv.svc, "quorum_tick", lambda: [])
    monkeypatch.setattr(mv.svc, "quorum_runtime_plan", lambda: {"responder": None, "probes": {}})
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, mv.svc, FakeBot())
    await sched.jobs["rsv_liveness"][0]()
    assert got == []


# ── замена устройства шлюза ─────────────────────────────────────────────────

def test_replacing_the_gateway_device_starts_a_new_generation(mv, monkeypatch):
    """У прежнего устройства шлюза осталась доля ключа переезда: замена —
    новое поколение M и код админу, иначе прежнее устройство вместе с долей
    резерва собирает ключ."""
    svc = mv.svc
    monkeypatch.setattr(svc.db, "gateway_update", lambda *a, **kw: None)
    monkeypatch.setattr(svc, "_backup_event", lambda *a: None, raising=False)
    monkeypatch.setattr(svc, "gwlink_forget", lambda *a: None, raising=False)
    m_gen = int(svc.reserve_keys()["m_gen"])
    svc.gateway_device_replace(1, 42)
    assert int(svc.reserve_keys()["m_gen"]) == m_gen + 1, "поколение после замены устройства шлюза прежнее"
    assert svc.reserve_code_notify_pending(), "новый код админу не запланирован"
