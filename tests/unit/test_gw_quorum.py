"""Кворум резерва на стороне агента шлюза (domain/gateway/reserve/quorum.py;
концепт «кворум» §2.5, §3.2, §4.7, §6.5): раздел `quorum` документа →
quorum.json, интерфейсы зондов через infra/quoprobe, пробы и взгляд
голосующего, `qview` резерву (гостевой канал) и своему серверу (канал
конфигурации), эпоха из `qstate`, строка панели Т-К17, поле `quo` снимка.

Хост — `_Host`: скрипт зондов моделью (какие зонды стоят, какие конфиги ушли в
`--up`, сколько раз `--wipe`); модельные часы — аргумент `now` такта и подмена
`time` модуля.

Цена ошибки: quorum.json не 0600 — закрытые ключи зондов читаемы шире;
новый документ стирает взгляды — после каждого документа шлюз «ещё не
смотрел»; зонд к клону стоит без переезда — лишний туннель к чужому серверу;
`--up` на каждом такте — пересоздание интерфейсов и рваные сессии; `qview`
без смены взгляда — шум в обоих каналах; эпоха из `qstate` не доходит до
проб — лидер не считает «да» этого шлюза.
"""
from __future__ import annotations

import json
import os
import stat
import types

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import quorum as qmod
from awgbot.infra import quoprobe
from awgbot.infra.db import Database
from awgbot.util.quorum.probe import Info, Probe, Reply

pytestmark = pytest.mark.unit

H = "[Interface]\nAddress = 169.254.88.5/32\nPrivateKey = DUMMY\n\n[Peer]\nAllowedIPs = 169.254.88.1/32\nEndpoint = 203.0.113.10:43125\n"
R = H.replace("169.254.88.1/32", "169.254.88.2/32").replace("203.0.113.10", "198.51.100.7")


class _Clock:
    def __init__(self, t: float = 5000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


class _Host:
    def __init__(self, monkeypatch):
        self.standing: dict[str, str] = {}
        self.ups: list[tuple[str, str]] = []
        self.downs: list[str] = []
        self.wipes = 0
        self.fail_up = ""
        monkeypatch.setattr(quoprobe, "standing", lambda: sorted(self.standing))
        monkeypatch.setattr(quoprobe, "up", self._up)
        monkeypatch.setattr(quoprobe, "down", self._down)
        monkeypatch.setattr(quoprobe, "wipe", self._wipe)

    def _up(self, name, conf):
        if self.fail_up:
            raise quoprobe.QuoError(self.fail_up)
        same = self.standing.get(name) == conf
        self.standing[name] = conf
        self.ups.append((name, conf))
        return "up: same" if same else "up: ok"

    def _down(self, name):
        self.standing.pop(name, None)
        self.downs.append(name)
        return "down: ok"

    def _wipe(self):
        self.standing.clear()
        self.wipes += 1
        return "wipe: ok"


@pytest.fixture()
def host(monkeypatch):
    return _Host(monkeypatch)


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(qmod, "time", types.SimpleNamespace(monotonic=c.monotonic))
    return c


@pytest.fixture()
def gw(tmp_path, host, clock):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE = tmp_path / "reserve.json"
    svc._QUO_STORE = tmp_path / "quorum.json"
    svc.set_reserve = lambda **kw: (tmp_path / "reserve.json").write_text(
        json.dumps({"slot": 1, "name": "Alpha", "state": "idle", **kw}), encoding="utf-8")
    svc.set_reserve()
    return svc


def _sec(**over) -> dict:
    sec = {"l": "a", "g": 4, "members": ["a", "b", "a1", "a2"], "t": 2, "port": 8789, "e": 12,
           "probes": {"h": H, "r": R}, "point": {}}
    sec.update(over)
    return sec


def _store(gw) -> dict:
    return json.loads(gw._QUO_STORE.read_text(encoding="utf-8"))


# ── раздел и файл ───────────────────────────────────────────────────────────

def test_the_section_lands_in_a_private_file_and_raises_the_probe_to_the_server(gw, host):
    res = gw.quorum_apply(_sec())
    p = gw._QUO_STORE
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, "quorum.json с ключами зондов не 0600"
    st = _store(gw)
    assert (st["l"], st["g"], st["members"], st["t"], st["port"], st["e"]) == ("a", 4, ["a", "b", "a1", "a2"], 2, 8789, 12)
    assert host.ups == [("awgqah", H)], "зонд к серверу не поднят (или поднят зонд к клону без переезда)"
    assert res["changed"] and "включено" in res["note"]


def test_the_same_section_again_changes_nothing_and_says_nothing(gw, host):
    gw.quorum_apply(_sec())
    res = gw.quorum_apply(_sec())
    assert not res["changed"] and res["note"] == "", res


def test_a_new_document_keeps_the_agents_own_marks(gw):
    gw.quorum_apply(_sec())
    cur = _store(gw)
    cur["views"] = {"awgqah": {"w": "see", "since": "2026-11-02T14:00:00"}}
    cur["pledges"] = {"a": {"e": 12}}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    res = gw.quorum_apply(_sec(g=5))
    st = _store(gw)
    assert res["changed"] and st["g"] == 5
    assert st["views"] == cur["views"] and st["pledges"] == cur["pledges"], "новый документ стёр взгляды и обязательства"


def test_without_the_section_the_agent_wipes_the_probes_and_the_file(gw, host):
    gw.quorum_apply(_sec())
    res = gw.quorum_apply(None)
    assert res["changed"] and "снято" in res["note"]
    assert host.wipes == 1 and not gw._QUO_STORE.exists(), "без раздела зонды или файл с ключами остались"
    assert gw.quorum_apply(None) == {"changed": False, "note": ""}, "повторное снятие что-то делает"
    assert host.wipes == 1


def test_a_failing_wipe_still_removes_the_file(gw, host, monkeypatch):
    gw.quorum_apply(_sec())
    monkeypatch.setattr(quoprobe, "wipe", lambda: (_ for _ in ()).throw(quoprobe.QuoError("нет пространства")))
    gw.quorum_apply(None)
    assert not gw._QUO_STORE.exists()


# ── интерфейсы зондов ───────────────────────────────────────────────────────

def test_the_probe_to_the_clone_stands_only_while_the_users_are_at_the_reserve(gw):
    gw.quorum_apply(_sec())
    assert set(gw.quorum_probe_ifaces()) == {"awgqah"}
    gw.set_reserve(state="away")
    want = gw.quorum_probe_ifaces()
    assert set(want) == {"awgqah", "awgqar"}, "в отъезде основы зонда к клону нет"
    assert (want["awgqar"]["resp"], want["awgqar"]["node_addr"], want["awgqar"]["port"]) == ("169.254.88.2", "169.254.88.5", 8789)
    gw.set_reserve(state="idle", reserve_state="serving")
    assert set(gw.quorum_probe_ifaces()) == {"awgqah", "awgqar"}, "резерв обслуживает, а зонда к клону нет"


def test_without_the_probe_to_the_server_there_are_no_probes_at_all(gw):
    gw.quorum_apply(_sec())
    cur = _store(gw)
    cur["probes"] = {"r": R}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    gw.set_reserve(state="away")
    assert gw.quorum_probe_ifaces() == {} and not gw.quorum_present()


def test_probes_apply_takes_down_what_is_not_wanted(gw, host):
    host.standing = {"awgqar": R, "awgqbh": "старый"}
    gw.quorum_apply(_sec())
    assert sorted(host.downs) == ["awgqar", "awgqbh"], "лишние зонды не сняты"
    assert set(host.standing) == {"awgqah"}


def test_probes_are_not_reapplied_every_tick(gw, host, clock):
    gw.quorum_apply(_sec())
    n = len(host.ups)
    for _ in range(10):
        clock.t += 5
        gw.quorum_tick(clock.t)
    assert len(host.ups) == n, "--up зонда на каждом такте без смены желаемого"
    clock.t += qmod.APPLY_EVERY_S + 1
    gw.quorum_tick(clock.t)
    assert len(host.ups) == n + 1, "зонд не перепроверен и через APPLY_EVERY_S"


def test_a_change_of_wanted_probes_applies_at_once(gw, host, clock):
    gw.quorum_apply(_sec())
    gw.set_reserve(state="away")
    clock.t += 5
    gw.quorum_tick(clock.t)
    assert ("awgqar", R) in host.ups, "переезд основы — зонд к клону не поднят на ближайшем такте"


def test_a_failing_up_does_not_break_the_tick(gw, host, clock):
    host.fail_up = "интерфейс не поднялся"
    gw.quorum_apply(_sec())
    clock.t += 5
    assert isinstance(gw.quorum_tick(clock.t), list)


# ── пробы и взгляд ──────────────────────────────────────────────────────────

def _probes(actions) -> list[tuple]:
    return [a for a in actions if a[0] == "probe"]


def _next_probe(gw, clock, acts: list | None = None) -> Probe:
    """Такт за тактом (по 1 с), пока правило не скажет «пора пробу»: интервал — с джиттером."""
    for _ in range(40):
        got = gw.quorum_tick(clock.t)
        if acts is not None:
            acts += got
        probes = _probes(got)
        if probes:
            return Probe.parse(probes[0][2])
        clock.t += 1
    raise AssertionError("за 40 с проба не ушла")


def _reply(gw, p: Probe, *, e: int = 12, y: int = 2, v: int = 3, r: str = "lead", info: Info | None = None) -> list[tuple]:
    data = Reply(p.l, p.g, e, r, p.s, y, v, info or Info(hs=7, flow=True, at="2026-11-02T14:07:10")).encode()
    return gw.quorum_reply_in("awgqah", data)


def test_the_first_tick_sends_a_probe_to_the_responder(gw, clock):
    gw.quorum_apply(_sec())
    acts = gw.quorum_tick(clock.t)
    probes = _probes(acts)
    assert len(probes) == 1 and probes[0][1] == "awgqah" and probes[0][3] == ("169.254.88.1", 8789), acts
    p = Probe.parse(probes[0][2])
    assert (p.l, p.g, p.n, p.e, p.w) == ("a", 4, "a1", 12, "new"), p


def test_without_a_slot_there_is_no_vote(gw, clock, tmp_path):
    (tmp_path / "reserve.json").write_text(json.dumps({"name": "Alpha"}), encoding="utf-8")
    gw.quorum_apply(_sec())
    assert gw.quorum_tick(clock.t) == []


def test_without_the_section_the_tick_is_empty(gw, clock):
    assert gw.quorum_tick(clock.t) == []


def test_two_replies_make_a_see_and_it_goes_to_the_reserve_the_server_and_the_snapshot_once(gw, clock):
    gw.quorum_apply(_sec())
    p1 = _next_probe(gw, clock)
    assert _reply(gw, p1) == [], "«вижу» из «ещё не смотрел» по одному ответу"
    clock.t += 1
    p2 = _next_probe(gw, clock)
    acts = _reply(gw, p2)
    kinds = [a[:2] for a in acts]
    assert kinds == [("guest", "qview"), ("link", "qview"), ("push",)], acts
    body = acts[0][2]
    assert (body["l"], body["g"], body["w"], body["e"]) == ("a", 4, "see", 12) and body["i"]["hs"] == 7
    assert acts[1][2] == body, "серверу ушёл не тот взгляд, что резерву"
    assert _store(gw)["views"]["awgqah"]["w"] == "see", "взгляд не записан — после перезапуска панель пуста"
    clock.t += 1
    p3 = _next_probe(gw, clock)
    assert p3.w == "see"
    assert _reply(gw, p3) == [], "qview без смены взгляда"


def test_silence_turns_into_uns_and_that_is_told_once(gw, clock):
    gw.quorum_apply(_sec())
    seen = []
    for _ in range(30):
        seen += gw.quorum_tick(clock.t)
        clock.t += 5
    views = [a for a in seen if a[:2] == ("guest", "qview")]
    assert [a[2]["w"] for a in views] == ["uns"], f"молчание ответчика — не один «не вижу»: {views}"
    assert "i" not in views[0][2], "справка к «не вижу» — от чужого ответа"


def test_a_lost_reply_is_sus_and_not_told(gw, clock):
    gw.quorum_apply(_sec())
    for _ in range(2):
        _reply(gw, _next_probe(gw, clock))
        clock.t += 1
    acts: list = []
    _next_probe(gw, clock, acts)                 # на эту пробу ответа нет
    for _ in range(3):
        clock.t += 2
        acts += gw.quorum_tick(clock.t)
    assert not [a for a in acts if a[0] in ("guest", "link")], "пропуск одного ответа ушёл голосом"
    assert _store(gw)["views"]["awgqah"]["w"] == "sus"


def test_a_reply_to_another_probe_or_garbage_is_ignored(gw, clock):
    gw.quorum_apply(_sec())
    p = Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2])
    other = Probe(p.l, p.g, p.n, p.e, p.w, p.s + 100)
    assert _reply(gw, other) == [] and _reply(gw, other) == []
    assert gw.quorum_reply_in("awgqah", b"\xff garbage") == []
    assert gw.quorum_reply_in("awgqzz", b"{}") == [], "ответ на чужой зонд"


def test_qstate_raises_the_epoch_the_probes_carry(gw, clock):
    gw.quorum_apply(_sec())
    gw.quorum_tick(clock.t)
    assert gw.reserve_guest_in("qstate", {"l": "a", "e": 15, "holder": "self", "r": "lead"}) == []
    clock.t += 30
    p = Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2])
    assert p.e == 15, "эпоха из qstate не дошла до пробы — лидер не считает «да» шлюза"
    gw.reserve_guest_in("qstate", {"l": "a", "e": 3, "holder": "self", "r": "lead"})
    clock.t += 30
    assert Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2]).e == 15, "эпоха пошла назад"


def test_a_new_document_epoch_reaches_the_running_voter(gw, clock):
    gw.quorum_apply(_sec())
    gw.quorum_tick(clock.t)
    gw.quorum_apply(_sec(e=20))
    clock.t += 30
    assert Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2]).e == 20


def test_a_reply_with_a_greater_epoch_is_adopted(gw, clock):
    gw.quorum_apply(_sec())
    p = Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2])
    _reply(gw, p, e=30)
    clock.t += 30
    assert Probe.parse(_probes(gw.quorum_tick(clock.t))[0][2]).e == 30


# ── панель и снимок ─────────────────────────────────────────────────────────

def test_the_panel_view_follows_the_last_reply(gw, clock):
    gw.quorum_apply(_sec())
    gw.quorum_tick(clock.t)
    assert gw.quorum_panel_view()["w"] == "new"
    clock.t += 1
    for _ in range(2):
        _reply(gw, _next_probe(gw, clock), y=2, v=3)
        clock.t += 1
    view = gw.quorum_panel_view()
    assert (view["name"], view["w"], view["y"], view["v"]) == ("Alpha", "see", 2, 3), view


def test_the_panel_view_after_a_restart_comes_from_the_file(gw, clock, tmp_path):
    gw.quorum_apply(_sec())
    cur = _store(gw)
    cur["views"] = {"awgqah": {"w": "uns", "since": "2026-11-02T14:05:00"}}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    view = gw.quorum_panel_view()
    assert (view["w"], view["since"]) == ("uns", "2026-11-02T14:05:00")


def test_the_panel_view_says_whether_the_reserve_is_online(gw):
    gw.quorum_apply(_sec())
    assert gw.quorum_panel_view()["reserve_online"] is False
    gw._quo_reserve_online = True
    assert gw.quorum_panel_view()["reserve_online"] is True


def test_without_the_section_there_is_no_panel_view_and_no_snapshot(gw):
    assert gw.quorum_panel_view() == {} and gw.quorum_snapshot() is None


def test_the_snapshot_carries_the_view_generation_and_epoch(gw, clock):
    gw.quorum_apply(_sec())
    gw.quorum_tick(clock.t)
    snap = gw.quorum_snapshot()
    assert snap == {"w": "new", "since": "", "g": 4, "e": 12, "q": 0}, snap
    from awgbot.domain import gwsnapshot
    assert gwsnapshot.sanitize({"quo": snap})["quo"] == snap, "снимок агента не проходит чистку сервера"


def test_the_cfg_prints_of_the_snapshot_carry_the_quorum_print(gw):
    """Отпечаток раздела по стоящему — тот же, что сервер считает по желаемому:
    иначе документ шлётся по кругу или отпечатки снимка не считаются вовсе."""
    from awgbot.domain.gwcfg import prints
    gw.quorum_apply(_sec())
    fp = gw.cfg_fingerprints()
    assert fp["quorum"] and fp["quorum"] == prints.quorum_print(_store(gw)), fp
