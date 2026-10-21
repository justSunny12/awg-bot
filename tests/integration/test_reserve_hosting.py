"""Сторона резерва: «клиенты основы у меня» (domain/services/reserve/inbound.py) —
проверки `go`, процесс открытия, реплика гостю, `tg_taken`, возврат, пересылка
хвоста, срок пересылки, возврат по кнопке у резерва, а также учёт трафика клона
(traffic.py).

Зона (`infra.rsvzone`), `systemctl`, `systemd-run` и пространство имён клона
подставлены: тест решает, что отвечает процесс открытия и что осталось в зоне.

Цена ошибки: `go` с чужим поколением пакета или при занятой зоне — клон
поднят по устаревшим данным или поверх чужих клиентов; ключ пакета в argv
процесса открытия — его видно любому через `ps`; отказ открытия без
`teardown`/`wipe` — расшифрованные данные основы остаются в памяти резерва;
`wiped` без пересылки хвоста — клиенты, ещё не сменившие запись, остаются без
сервера на 15 минут; данные основы, не снятые после возврата, — чужие клиенты
лежат на резерве бессрочно.
"""
from __future__ import annotations

import json
import subprocess
import types

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import inbound, openproc
from awgbot.infra import nftguard, rsvzone
from awgbot.util import netns, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import PEER_IP, notices, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


class Zone:
    """Состояние зоны для теста: ход процесса открытия, вызовы, файлы хранилища гостя."""

    def __init__(self, tmp_path):
        self.status: dict = {}
        self.calls: list[tuple] = []
        self.fail_on: dict[str, str] = {}
        self.phase = "serving"
        self.iface = "awgrsv0"
        self.webhook_port = 8443
        self.rep = tmp_path / "var" / "rep.bin"
        self.take = tmp_path / "run" / "tg_take"
        self.webhook = tmp_path / "run" / "webhook.json"
        self.events: list[str] = []                       # общий порядок: монтирование хранилища и запуск процесса

    def store(self) -> str:
        """Хранилище гостя монтирует бот до запуска юнита открытия (не в z.calls:
        те сверяют ход зоны после открытия)."""
        self.events.append("store")
        if "store" in self.fail_on:
            raise rsvzone.ZoneError(self.fail_on["store"])
        return ""

    def _do(self, name, *a):
        self.calls.append((name, *a))
        if name in self.fail_on:
            raise rsvzone.ZoneError(self.fail_on[name])
        return ""

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class Spawn:
    """`subprocess` модуля inbound: systemd-run/Popen и systemctl."""

    SubprocessError = subprocess.SubprocessError
    DEVNULL = subprocess.DEVNULL
    PIPE = subprocess.PIPE

    def __init__(self, events: list[str] | None = None):
        self.popen: list[dict] = []
        self.runs: list[list] = []
        self.popen_error: OSError | None = None
        self.run_error: Exception | None = None
        self.rc: int | None = None                         # код завершения процесса открытия; None — идёт
        self.events = events if events is not None else []

    def Popen(self, cmd, **kw):                              # noqa: N802
        if self.popen_error:
            raise self.popen_error
        rec = {"cmd": list(cmd), "stdin": b"", "kw": kw}
        self.popen.append(rec)
        self.events.append("popen")
        outer = rec
        spawn = self

        class _In:
            def write(self, data):
                outer["stdin"] += data

            def close(self):
                outer["closed"] = True
        return types.SimpleNamespace(stdin=_In(), poll=lambda: spawn.rc)

    def run(self, cmd, **kw):
        if self.run_error:
            raise self.run_error
        self.runs.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")


@pytest.fixture()
def host(mv, tmp_path, monkeypatch):
    """Резерв: пакет основы принят, зона и процессы подставные."""
    svc = mv.svc
    z = Zone(tmp_path)
    monkeypatch.setattr(rsvzone, "REP_PATH", z.rep)
    monkeypatch.setattr(rsvzone, "TG_TAKE_FILE", z.take)
    monkeypatch.setattr(rsvzone, "WEBHOOK_FILE", z.webhook)
    monkeypatch.setattr(rsvzone, "plan", lambda: {"webhook_port": str(z.webhook_port)})
    monkeypatch.setattr(rsvzone, "open_status", lambda: dict(z.status))
    monkeypatch.setattr(rsvzone, "open_status_write", lambda step, **f: z.status.update(step=step, **f))
    monkeypatch.setattr(rsvzone, "state", lambda *a: {"phase": z.phase, "iface": z.iface})
    monkeypatch.setattr(rsvzone, "serve", lambda port: z._do("serve", port))
    monkeypatch.setattr(rsvzone, "teardown", lambda: z._do("teardown"))
    monkeypatch.setattr(rsvzone, "wipe", lambda: z._do("wipe"))
    monkeypatch.setattr(rsvzone, "relay", lambda to: z._do("relay", to))
    monkeypatch.setattr(rsvzone, "store", z.store)
    monkeypatch.setattr(nftguard, "tg_pool", lambda: ["149.154.160.0/20", "91.108.4.0/22"])
    monkeypatch.setattr(nftguard, "relay_packets", lambda: None)       # счётчик хвоста — не из таблицы хоста прогона
    spawn = Spawn(z.events)
    monkeypatch.setattr(inbound, "subprocess", spawn)                  # systemctl гостя при реплике
    monkeypatch.setattr(openproc, "subprocess", spawn)                 # systemd-run / Popen процесса открытия
    monkeypatch.setattr(openproc.shutil, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    z.webhook.parent.mkdir(parents=True, exist_ok=True)
    z.webhook.write_text(json.dumps({"port": 8443}))
    svc.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "share": "DUMMY-share"})   # пакет и доля — направление «ко мне» открыто
    guard = tmp_path / "reserve"
    guard.mkdir(exist_ok=True)
    (guard / "guest.pkg").write_bytes(b"DUMMY-package")
    mv.zone, mv.spawn = z, spawn
    return mv


K_HEX = "ab" * 32


def _taking(host, kind="button"):
    assert host.svc.reserve_go_in(K_HEX, 7, kind) == ""


def _serving(host, kind="button", **fields):
    host.svc.reserve_set_in("serving", kind=kind, gen=7, taken_at=timeutil.now_iso(), tg_taken=True, served=True,
                            open_started=timeutil.now_iso(), **fields)


# ── go: проверки и запуск процесса открытия ─────────────────────────────────

def test_go_with_a_foreign_package_generation_is_refused_with_the_reason(host):
    svc = host.svc
    why = svc.reserve_go_in(K_HEX, 8, "button")
    assert "7" in why and "8" in why and svc.reserve_in() == "idle" and not host.spawn.popen
    svc.rsv_put("rsv_pkg_in", None)
    assert "нет" in svc.reserve_go_in(K_HEX, 8, "button"), "пакета нет вовсе — клонировать нечего"


@pytest.mark.parametrize("prep,reason", [
    (lambda h: h.svc.reserve_set_in("serving"), "зона занята"),
    (lambda h: settings.set_value("app.reserve.ready", False), "выключена"),
    (lambda h: h.svc.reserve_st_in(rm.peer_st(ready=False)), "не готов"),
])
def test_go_is_refused_when_the_zone_is_busy_or_the_reserve_is_not_ready(host, prep, reason):
    prep(host)
    why = host.svc.reserve_go_in(K_HEX, 7, "button")
    assert reason in why, why
    assert not host.spawn.popen, "процесс открытия запущен несмотря на отказ"


def test_go_with_a_key_that_is_not_hex_is_refused(host):
    assert "hex" in host.svc.reserve_go_in("zz", 7, "button") and host.svc.reserve_in() == "idle"


def test_go_launches_the_open_process_with_the_key_only_in_stdin(host):
    svc = host.svc
    _taking(host)
    assert svc.reserve_in() == "taking"
    run = host.spawn.popen[0]
    assert run["cmd"][0] == "systemd-run" and "--pipe" in run["cmd"] and f"--unit={openproc.OPEN_UNIT}" in run["cmd"]
    assert "tools.rsvopen" in run["cmd"]
    assert K_HEX not in " ".join(run["cmd"]), "ключ пакета в argv — виден любому через ps"
    payload = json.loads(run["stdin"])
    assert payload["k"] == K_HEX and payload["webhook_port"] == 8443 and payload["tg_pool"] == ["149.154.160.0/20", "91.108.4.0/22"]
    assert payload["set_record"] is False, "при переезде кнопкой запись меняет основа, а не резерв"
    assert host.zone.status["step"] == "launched"
    assert svc.rsv_get("rsv_in")["kind"] == "button" and svc.rsv_get("rsv_in")["gen"] == 7


def test_go_for_an_emergency_kind_lets_the_open_process_change_the_record(host):
    _taking(host, "dead")
    payload = json.loads(host.spawn.popen[0]["stdin"])
    assert payload["set_record"] is True and payload["target_ip"] == rm.OWN_IP


def test_a_failed_launch_frees_the_zone_and_reports_the_reason(host):
    host.spawn.popen_error = OSError("нет python")
    why = host.svc.reserve_go_in(K_HEX, 7, "button")
    assert "не запустился" in why and "нет python" in why
    assert host.svc.reserve_in() == "idle", "зона осталась занятой отказавшим запуском"


# ── процесс открытия: ход ───────────────────────────────────────────────────

def test_up_goes_to_the_primary_once_when_the_clone_is_up(host):
    svc = host.svc
    _taking(host)
    assert svc.reserve_in_tick() == [], "процесс ещё запускается"
    host.zone.status = {"step": "clone_up"}
    acts = svc.reserve_in_tick()
    assert sent(acts) == ["up"] and ("fw",) in acts
    assert svc.reserve_in_tick() == [], "up ушёл второй раз"


def test_done_turns_the_zone_into_the_serving_mode_once(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "done"}
    acts = svc.reserve_in_tick()
    assert host.zone.calls == [("serve", 8443)] and sent(acts) == ["up"]
    svc.reserve_in_tick()
    assert host.zone.names().count("serve") == 1, "serve повторяется на каждом тике"


def test_a_refused_serve_ends_the_hosting_with_cleanup(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "done"}
    host.zone.fail_on["serve"] = "порт вебхука занят"
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle"
    assert host.zone.names()[-2:] == ["teardown", "wipe"], "после отказа в зоне остались данные основы"
    down = [a for a in acts if a[:2] == ("send", "down")]
    assert down and "порт вебхука занят" in down[0][2]["reason"]
    assert any("принять не удалось" in n.text for n in notices(acts))


def test_a_failed_open_process_ends_the_hosting_with_cleanup_and_down(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "fail", "reason": "доли не сошлись"}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and host.zone.names() == ["teardown", "wipe"]
    assert [a[2]["reason"] for a in acts if a[:2] == ("send", "down")] == ["доли не сошлись"]
    assert ("fw",) in acts and "доли не сошлись" in notices(acts)[0].text
    assert svc.reserve_in_tick() == [], "после отказа такт молчит"


def test_the_open_process_that_hangs_for_35_minutes_is_cut_off(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "clone_up"}
    host.clock.advance(inbound.OPEN_WAIT_S - 5)
    assert svc.reserve_in() == "taking" and "down" not in sent(svc.reserve_in_tick())
    host.clock.advance(10)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and "down" in sent(acts) and host.zone.names()[-2:] == ["teardown", "wipe"]


def test_a_finished_open_process_is_not_cut_off_by_the_timeout(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "done"}
    svc.reserve_in_tick()
    host.clock.advance(inbound.OPEN_WAIT_S + 60)
    svc.reserve_in_tick()
    assert svc.reserve_in() == "taking", "ждём вебхук гостя; зона уже обслуживает"


# ── реплика, право отвечать ─────────────────────────────────────────────────

def test_the_replica_goes_to_the_guest_storage_with_the_guest_stopped_and_then_started_to_take_telegram(host):
    """Гость останавливается ДО записи реплики и запускается после: живой
    гость пишет свой снимок раз в 30 с и иначе перезаписал бы финальную
    реплику основы старым состоянием."""
    svc = host.svc
    _taking(host)
    assert svc.reserve_released_store(b"DUMMY-replica") == []
    assert host.zone.rep.read_bytes() == b"DUMMY-replica"
    assert (host.zone.rep.stat().st_mode & 0o777) == 0o600, "реплика с данными клиентов читается другими"
    assert host.zone.take.read_text() == "1", "отметка «ставь вебхук» не поставлена"
    assert host.spawn.runs == [["systemctl", "stop", rsvzone.GUEST_UNIT], ["systemctl", "start", rsvzone.GUEST_UNIT]], \
        "гость не остановлен до записи реплики — его снимок может затереть финальную"
    assert svc.rsv_get("rsv_in")["replica_at"]


def test_a_replica_outside_taking_is_ignored(host):
    host.svc.reserve_released_store(b"DUMMY-replica")
    assert not host.zone.rep.exists() and not host.zone.take.exists() and not host.spawn.runs


def test_a_failed_guest_restart_ends_the_hosting_and_does_not_mark_the_replica_as_delivered(host):
    """Гость остановлен до записи реплики, и поднять его не вышло — ждать
    нечего: срыв приёма (down основе с причиной, зона снята), реплика не
    считается доставленной."""
    _taking(host)
    host.spawn.run_error = subprocess.TimeoutExpired("systemctl", 60)
    acts = host.svc.reserve_released_store(b"DUMMY-replica")
    down = [a[2]["reason"] for a in acts if a[:2] == ("send", "down")]
    assert down and "реплика не положена боту основы" in down[0], acts
    assert host.svc.reserve_in() == "idle" and host.zone.names()[-2:] == ["teardown", "wipe"]
    assert "replica_at" not in host.svc.rsv_get("rsv_in")


def test_tg_taken_from_the_guest_starts_serving_and_notifies_once(host):
    svc = host.svc
    _taking(host)
    acts = svc.reserve_guest_tg_taken()
    assert svc.reserve_in() == "serving" and sent(acts) == ["taken"] and ("fw",) in acts
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1 and "перевёл к тебе клиентов и бот" in texts[0] and not texts[0].endswith(".")
    assert svc.reserve_guest_tg_taken() == [], "повтор слова гостя — повторное «taken» и уведомление"


def test_tg_taken_without_a_hosting_changes_nothing(host):
    assert host.svc.reserve_guest_tg_taken() == [] and host.svc.reserve_in() == "idle"


def test_the_emergency_hosting_notice_names_the_reason(host):
    svc = host.svc
    _taking(host, "dead")
    text = notices(svc.reserve_guest_tg_taken())[0].text
    assert "не отвечает" in text and "Beta" in text


# ── возврат ─────────────────────────────────────────────────────────────────

def test_back_req_stops_the_guest_and_waits_for_the_replica(host):
    svc = host.svc
    _serving(host)
    assert svc.reserve_back_req_in() == [("guest", "stop", {})]
    assert svc.reserve_in() == "releasing" and svc.reserve_busy()


def test_back_req_when_nothing_is_hosted_answers_released_without_a_replica(host):
    """Зоны нет (перезагрузка резерва, teardown мимо бота): основа не должна
    просить вечно — released без реплики, зона остаётся idle."""
    assert host.svc.reserve_back_req_in() == [("send_released", {"gen": 7, "blob": ""})]
    assert host.svc.reserve_in() == "idle"


@pytest.mark.parametrize("state", ["idle", "relay"])
def test_back_req_without_a_zone_gives_the_snapshot_from_disk(host, state):
    """rep.bin (под K основы) на диске — его и отдать, основа сольёт его."""
    host.svc.reserve_set_in(state)
    host.zone.rep.parent.mkdir(parents=True, exist_ok=True)
    host.zone.rep.write_bytes(b"DUMMY-replica")
    assert host.svc.reserve_back_req_in() == [("send_released", {"gen": 7, "blob": b"DUMMY-replica".hex()})]
    assert host.svc.reserve_in() == state, "просьба основы сдвинула фазу зоны"


def test_the_guest_replica_goes_to_the_primary_as_released(host):
    svc = host.svc
    _serving(host)
    svc.reserve_back_req_in()
    acts = svc.reserve_guest_replica_in(b"\x01\x02")
    assert acts == [("send_released", {"gen": 7, "blob": "0102"})]
    assert svc.reserve_in_tick() == [], "реплика ушла — ждём back"


def test_a_replica_while_serving_is_not_sent(host):
    _serving(host)
    assert host.svc.reserve_guest_replica_in(b"\x01") == []


def test_without_a_guest_replica_the_snapshot_from_disk_goes_after_the_wait(host):
    svc = host.svc
    host.zone.rep.parent.mkdir(parents=True, exist_ok=True)
    host.zone.rep.write_bytes(b"\xaa\xbb")
    _serving(host)
    svc.reserve_back_req_in()
    host.clock.advance(inbound.REPLICA_WAIT_S - 1)
    assert svc.reserve_in_tick() == []
    host.clock.advance(2)
    assert svc.reserve_in_tick() == [("send_released", {"gen": 7, "blob": "aabb"})]
    assert svc.reserve_in_tick() == [], "released отправлен один раз"


def test_without_any_replica_an_empty_released_still_goes_so_the_primary_is_not_left_waiting(host):
    svc = host.svc
    _serving(host)
    svc.reserve_back_req_in()
    host.clock.advance(inbound.REPLICA_WAIT_S + 1)
    assert svc.reserve_in_tick() == [("send_released", {"gen": 7, "blob": ""})]


def _releasing(host, kind="button"):
    _serving(host, kind)
    host.svc.reserve_back_req_in()


def test_back_forwards_the_tail_wipes_the_storage_and_confirms_in_that_order(host):
    svc = host.svc
    _releasing(host)
    acts = svc.reserve_back_in()
    assert host.zone.calls == [("relay", f"{PEER_IP}:45871"), ("wipe",)], "хвост — сначала, снятие — потом"
    kinds = [a[1] for a in acts if a[0] == "send"]
    assert kinds == ["relay", "wiped"], kinds
    relay = [a for a in acts if a[:2] == ("send", "relay")][0][2]
    assert relay["until"] > timeutil.now_iso()
    assert svc.reserve_in() == "relay" and svc.rsv_get("rsv_in")["relay_to"] == f"{PEER_IP}:45871"
    assert not (host.zone.rep.parent.parent / "reserve" / "guest.pkg").exists(), "пакет основы остался на резерве"
    assert svc.rsv_get("rsv_pkg_in") == {}
    text = notices(acts)[0].text
    assert "вернулись к нему" in text and "Beta" in text


def test_back_without_a_known_address_tears_the_zone_down_instead_of_forwarding(host):
    svc = host.svc
    _releasing(host)
    host.svc.rsv_update("rsv_peer_state", self_host="", ports=[])
    host.svc.db.reserve_peer_update(host="")
    svc.reserve_back_in()
    assert host.zone.calls[0] == ("teardown",) and host.zone.names()[-1] == "wipe"


def test_a_failed_forward_does_not_leave_the_primarys_data_on_the_reserve(host):
    svc = host.svc
    _releasing(host)
    host.zone.fail_on["relay"] = "nft: ошибка"
    acts = svc.reserve_back_in()
    assert "wipe" in host.zone.names() and sent(acts)[-1] == "wiped"


def test_back_after_an_emergency_hosting_drops_the_share_but_after_a_button_keeps_it(host):
    svc = host.svc
    svc.rsv_put("rsv_shares", {"x": "DUMMY"})
    _releasing(host, "button")
    svc.reserve_back_in()
    assert svc.rsv_get("rsv_shares") == {"x": "DUMMY"}, "после переезда кнопкой доля резерва прежняя"
    svc.reserve_set_in("idle")
    _releasing(host, "dead")
    svc.reserve_back_in()
    assert svc.rsv_get("rsv_shares") == {}, "после аварийного переезда прямая скомпрометирована — доля прочь"


def test_a_repeated_back_repeats_relay_and_wiped_but_wipes_nothing_again(host):
    """Основа повторяет `back`, пока не получит `relay`: если ответ потерялся
    в обрыве, повтор должен снова дать relay с тем же сроком и wiped, но
    зона второй раз не стирается."""
    svc = host.svc
    _releasing(host)
    first = svc.reserve_back_in()
    until = [a for a in first if a[:2] == ("send", "relay")][0][2]["until"]
    again = svc.reserve_back_in()
    assert again == [("send", "relay", {"until": until}), ("send", "wiped", {})], again
    assert host.zone.names().count("wipe") == 1, "повтор back стёр зону ещё раз"


def test_the_relay_stops_after_its_term(host):
    svc = host.svc
    svc.reserve_set_in("relay", relay_to="x:1", relay_at=timeutil.now_iso(),
                       relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), 3600)))
    settings.set_value("app.reserve.relay_idle_hours", 0)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "relay"
    svc.rsv_update("rsv_in", relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), -5)))
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and host.zone.names() == ["teardown"] and ("fw",) in acts
    assert sent(acts) == ["st"], "второй сервер должен узнать, что зона свободна"


def test_the_relay_stops_early_when_the_quiet_hours_have_passed(host):
    svc = host.svc
    settings.set_value("app.reserve.relay_idle_hours", 2)
    svc.reserve_set_in("relay", relay_to="x:1", relay_at=timeutil.to_iso(timeutil.shift(timeutil.now(), -3 * 3600)),
                       relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), 86400)))
    svc.reserve_in_tick()
    assert svc.reserve_in() == "idle"


def test_a_broken_relay_term_does_not_keep_the_forward_forever(host):
    svc = host.svc
    svc.reserve_set_in("relay", relay_to="x:1", relay_at="", relay_until="не дата")
    svc.reserve_in_tick()
    assert svc.reserve_in() == "idle"


# ── возврат по кнопке у резерва ─────────────────────────────────────────────

def test_the_evict_check_names_each_obstacle(host):
    svc = host.svc
    assert "здесь нет" in svc.reserve_evict_check()
    _serving(host)
    assert svc.reserve_evict_check() == ""
    svc.rsv_update("rsv_peer_state", session=False)
    assert "не на связи" in svc.reserve_evict_check()
    svc.rsv_update("rsv_peer_state", session=True)
    svc.reserve_set_in("releasing")
    assert svc.reserve_evict_check() == "возврат уже идёт"


def test_the_return_button_at_the_reserve_asks_the_primary_via_evict_only_when_allowed(host):
    svc = host.svc
    assert svc.reserve_guest_back_req() == []
    _serving(host)
    assert svc.reserve_guest_back_req() == [("send", "evict", {})]
    svc.reserve_set_in("releasing")
    assert svc.reserve_guest_back_req() == [], "возврат уже идёт — вторая просьба не нужна"


def test_the_in_tick_without_a_pair_does_nothing(services, rsv):
    assert services.reserve_in_tick() == []


# ── учёт трафика клона ──────────────────────────────────────────────────────

@pytest.fixture()
def counters(host, monkeypatch):
    box = types.SimpleNamespace(out="", code=0, error=None, calls=0)

    def run_in(path, argv, timeout=10, input_data=None, check=False):
        box.calls += 1
        if box.error:
            raise box.error
        return subprocess.CompletedProcess(argv, box.code, box.out.encode(), b"")
    monkeypatch.setattr(netns, "run_in", run_in)
    host.counters = box
    return host


def test_the_clone_counters_are_summed_into_the_month_total_while_serving(counters):
    svc = counters.svc
    _serving(counters)
    counters.counters.out = "pubA\t100\t200\npubB\t50\t60\n"
    svc.reserve_traffic_in_tick()
    counters.counters.out = "pubA\t400\t300\npubB\t50\t60\n"
    svc.reserve_traffic_in_tick()
    acc = svc.rsv_get("rsv_traffic")["in"]
    assert (acc["rx"], acc["tx"]) == (450, 360), acc
    assert counters.counters.calls == 2


def test_a_restart_of_the_clone_does_not_lose_traffic(counters):
    svc = counters.svc
    _serving(counters)
    counters.counters.out = "pubA\t1000\t2000\n"
    svc.reserve_traffic_in_tick()
    counters.counters.out = "pubA\t10\t20\n"                  # счётчики обнулились — клон перезапущен
    svc.reserve_traffic_in_tick()
    acc = svc.rsv_get("rsv_traffic")["in"]
    assert (acc["rx"], acc["tx"]) == (1010, 2020)


def test_no_counters_are_read_when_nothing_is_hosted(counters):
    counters.svc.reserve_traffic_in_tick()
    assert counters.counters.calls == 0 and not counters.svc.rsv_get("rsv_traffic")


@pytest.mark.parametrize("fail", ["code", "oserror", "timeout", "noiface"])
def test_unreadable_counters_change_nothing(counters, fail, monkeypatch):
    svc = counters.svc
    _serving(counters)
    counters.counters.out = "pubA\t100\t200\n"
    svc.reserve_traffic_in_tick()
    before = svc.rsv_get("rsv_traffic")
    if fail == "code":
        counters.counters.code = 1
    elif fail == "oserror":
        counters.counters.error = OSError("нет nsenter")
    elif fail == "timeout":
        counters.counters.error = subprocess.TimeoutExpired("awg", 10)
    else:
        monkeypatch.setattr(rsvzone, "state", lambda *a: {"phase": "serving"})
    svc.reserve_traffic_in_tick()
    assert svc.rsv_get("rsv_traffic") == before


def test_a_new_month_archives_the_previous_one(counters):
    svc = counters.svc
    _serving(counters)
    svc.rsv_put("rsv_traffic", {"in": {"month": "2020-01", "rx": 5, "tx": 7, "last_rx": 5, "last_tx": 7}})
    counters.counters.out = "pubA\t1\t2\n"
    svc.reserve_traffic_in_tick()
    months = svc.db.reserve_traffic_months()
    assert [(m["month"], m["dir"], m["rx"], m["tx"]) for m in months] == [("2020-01", "in", 5, 7)]
    assert svc.rsv_get("rsv_traffic")["in"]["month"] == timeutil.now().strftime("%Y-%m")


def test_the_close_takes_a_last_measure_and_forgets_the_previous_counters(counters):
    svc = counters.svc
    _serving(counters)
    counters.counters.out = "pubA\t100\t200\n"
    svc.reserve_traffic_in_tick()
    counters.counters.out = "pubA\t150\t260\n"
    svc.reserve_traffic_in_close()
    acc = svc.rsv_get("rsv_traffic")["in"]
    assert (acc["rx"], acc["tx"]) == (150, 260) and acc["last_rx"] == 0 and acc["last_tx"] == 0


def test_out_traffic_accumulates_per_month_and_ignores_negative_deltas(mv):
    svc = mv.svc
    svc.reserve_traffic_out_add(10, 20)
    svc.reserve_traffic_out_add(5, -3)
    acc = svc.rsv_get("rsv_traffic")["out"]
    assert (acc["rx"], acc["tx"]) == (15, 20)



def test_the_package_opened_by_the_key_of_the_primary_is_said_as_k_in_st(host):
    """Переезд кнопкой — пакет открыт ключом основы: `in_by = k`, после
    возврата новое поколение ключа основе не нужно (его ждут только доли и код)."""
    _taking(host)
    assert host.svc.rsv_get("rsv_in").get("by") == "k"
    assert host.svc.reserve_st_body()["in_by"] == "k"


def test_the_zone_shell_is_ensured_before_the_opening_process_starts(host, monkeypatch):
    """После перезагрузки хоста резерва пространства зоны нет: --take в
    процессе открытия отказал бы — оболочку поднимают до запуска процесса."""
    order = []
    monkeypatch.setattr(host.svc, "reserve_zone_shell_ensure", lambda: order.append("shell") or True)
    real = host.svc._rsvopen_launch
    monkeypatch.setattr(host.svc, "_rsvopen_launch", lambda payload: order.append("open") or real(payload))
    _taking(host)
    assert order == ["shell", "open"], order
