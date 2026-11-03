"""Обязательство «нет» снова видящего голосующего (domain/services/reserve/quorum/
pledges.py; концепт «кворум» §3.4.3, §3.5.1): шлюз сказал кандидату «не вижу»,
потом снова «вижу», а кандидат ничего не решает — `qrel{e}` ему сразу, одно на
обязательство; пока идёт отсчёт, решение или исполнение — обязательства держатся,
как прежде.

Мир — резерв «Alpha» (сторона `a`) кандидатом о линии `b` «Beta» (`Cand` из
tests/integration/_reserve_vote_common.py) и настоящий агент шлюза 1 линии `b`
(domain/gateway/reserve/quorum.py) на файлах во временном каталоге; лидер — машина
`util/quorum/leader` под эпохой 7: пробы шлюза идут в неё, её ответы — шлюзу. Слова
переносит тест: `qview` шлюза — кандидату по гостевому каналу, `qrel`/`qtake`/`qask`
кандидата — шлюзу. Часы одни (модельные) у кандидата и агента.

Цена ошибки: без снятия — ложное «не вижу» при включении голосования (зонд шлюза
спросил раньше первого ответа лидера) или короткий сбой связи одного шлюза держит
`held` до 10 минут, и живой лидер с голосом меньше может лишиться кворума зря;
снятие посреди решения — `qrel{e}` откатывает у шлюза поднятую по `qtake` эпоху
e + 1 к e, и прежний лидер набирает «да» при живом новом держателе (два писателя).
"""
from __future__ import annotations

import json
import types

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import quorum as quomod
from awgbot.infra import quoprobe
from awgbot.infra.db import Database
from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.probe import Probe

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import ASK, LINE, MEMBERS, T_UP, Cand, box, gw_sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

E = 7
NAME = f"awgq{LINE}h"
CONF = ("[Interface]\nAddress = 169.254.88.6/32\nPrivateKey = DUMMY\n\n"
        "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 169.254.88.2/32\nEndpoint = 198.51.100.7:51820\n")
PLEDGE_S = 600


class _World:
    """Кандидат `c`, агент шлюза 1 линии `b` и её лидер; такт агента — 1 с, такт кандидата — 5 с."""

    def __init__(self, box, tmp_path, monkeypatch, members: list[str]):
        self.c = Cand(box, members=members)
        self.clock = box.clock
        monkeypatch.setattr(quomod, "time", types.SimpleNamespace(monotonic=self.clock.monotonic))
        ups: list[str] = []
        monkeypatch.setattr(quoprobe, "up", lambda name, conf, port=0: ups.append(name) or "up: ok")
        monkeypatch.setattr(quoprobe, "down", lambda name: "down: ok")
        monkeypatch.setattr(quoprobe, "standing", lambda: list(ups))
        monkeypatch.setattr(quoprobe, "wipe", lambda: "wipe: ok")
        d = tmp_path / "gw"
        d.mkdir()
        (d / "reserve.json").write_text(json.dumps({"slot": 1, "name": "Alpha", "state": "idle"}), encoding="utf-8")
        (d / "quorum.json").write_text(json.dumps({
            "l": LINE, "g": 1, "members": list(members), "t": 2, "port": 8789, "probes": {"h": CONF},
            "point": {"x": 2, "y": "cd" * 16, "q_gen": 4}, "e": E}), encoding="utf-8")
        db = Database(d / "gw.db")
        db.init_schema()
        self.gw = GatewayServices(db)
        self.gw._RSV_STORE = d / "reserve.json"
        self.gw._QUO_STORE = d / "quorum.json"
        voters = tuple(n for n in members if n != LINE)
        self.ld = Leader(l=LINE, v=len(voters), epoch=E, candidate="a")
        self.ld.set_members(voters, len(voters), 1, self.clock.t)
        self.words: list[tuple] = []                      # слова кандидата шлюзу 1: (момент, вид, тело)
        self.probes: list[tuple[float, Probe]] = []

    @property
    def v(self):
        return self.gw._gw_qvoters[NAME]

    def run(self, seconds: int, *, answer: bool) -> None:
        """Секунды модельного времени; answer — лидер отвечает на пробы шлюза."""
        for _ in range(int(seconds)):
            self.clock.t += 1
            now = self.clock.t
            acts = self.gw.quorum_tick(now)
            for a in list(acts):
                if a[0] == "probe":
                    p = Probe.parse(a[2])
                    self.probes.append((now, p))
                    self.ld.probe_in(p, now)
                    if answer:
                        acts += self.gw.quorum_reply_in(NAME, self.ld.reply_for(p, now).encode())
            for a in acts:
                if a[0] == "guest" and a[1] == "qview":
                    self.c.s.reserve_guest_in(1, "qview", a[2])
            if int(now) % 5 == 0:
                for a in self.c.tick():
                    if a and a[0] == "gw" and a[1] == 1:
                        self.words.append((now, a[2], dict(a[3])))
                        self.gw.quorum_guest_in(a[2], dict(a[3]))

    def said(self, kind: str) -> list[tuple]:
        return [(t, b) for t, k, b in self.words if k == kind]

    def yes(self) -> bool:
        """«Да» шлюза 1 в счёте лидера сейчас."""
        return "b1" in self.ld.yes_nodes(self.clock.t)


@pytest.fixture()
def world(box, tmp_path, monkeypatch):
    return _World(box, tmp_path, monkeypatch, MEMBERS)


def test_a_false_no_at_enabling_no_longer_holds_the_yes_until_the_pledge_term(world):
    """Голосование включилось: зонд шлюза спрашивает раньше первого ответа лидера,
    молчание дольше `no_s` — «не вижу» кандидату и обязательство; лидер начал отвечать —
    «вижу». Кандидат ничего не решает (свой взгляд «вижу», одно «нет» из трёх) — `qrel{7}`
    сразу, пробы снова «вижу», «да» шлюза у лидера в счёте за секунды, а не через 10 минут;
    второго `qrel` на то же обязательство нет."""
    w = world
    w.c.own("see")
    w.run(1, answer=False)
    t0 = w.clock.t
    w.run(w.v.timing.no_s + 5, answer=False)
    assert w.v.view == "uns" and w.v.pledge is not None, "подготовка: «не вижу» с обязательством не наступило"
    assert not w.said("qrel")
    w.run(60, answer=True)
    assert w.v.view == "see"
    rel = w.said("qrel")
    assert [b for _, b in rel] == [{"l": LINE, "e": E}], f"qrel снова видящему шлюзу: {w.words}"
    assert w.v.pledge is None and w.v.epoch == E, "qrel снял не обязательство или сдвинул эпоху"
    assert w.yes(), f"«да» шлюза 1 не в счёте лидера: {w.ld.seen}"
    assert rel[0][0] - t0 < 120 < PLEDGE_S, f"обязательство снято только через {rel[0][0] - t0:.0f} с"
    w.run(120, answer=True)
    assert len(w.said("qrel")) == 1, f"qrel на одно обязательство — не один: {w.said('qrel')}"
    assert all(p.w == "see" for t, p in w.probes if t > rel[0][0] + 1), "после qrel пробы лидеру всё ещё held"


def test_without_the_release_rule_the_pledge_would_hold(world, monkeypatch):
    """Сторож теста выше: без правила (подмена) те же события держат `held` до срока —
    тест выше проверяет именно снятие, а не что-то другое."""
    w = world
    monkeypatch.setattr(w.c.s, "_quorum_seen_again", lambda c: [])
    w.c.own("see")
    w.run(1, answer=False)
    w.run(w.v.timing.no_s + 5, answer=False)
    w.run(120, answer=True)
    assert w.v.view == "see" and w.v.pledge is not None and not w.yes()
    assert [p.w for _, p in w.probes][-1] == "held"


def test_a_real_majority_of_no_keeps_the_pledges_through_the_count_and_the_take(box, tmp_path, monkeypatch):
    """v = 4 (a, b1, b2, b3): свой «не вижу», b2 и b3 не видят — большинство держится и без b1.
    b1 сказал «нет» и снова видит — отсчёт идёт, `qrel` ему нет, пробы `held`; через T_up —
    `qtake{7}`: эпоха у шлюза 8, его пробы прежнему лидеру под 7 в счёт не идут; посреди
    исполнения (лидер на просьбу молчит — точки, окно кода) b1 снова «не вижу» и «вижу» —
    `qrel` нет: он откатил бы поднятую эпоху."""
    w = _World(box, tmp_path, monkeypatch, ["b", "a", "b1", "b2", "b3"])
    w.c.own("uns")
    w.c.view(2, "uns")
    w.c.view(3, "uns")
    w.run(1, answer=False)
    w.run(w.v.timing.no_s + 5, answer=False)
    assert w.v.pledge is not None
    t0 = w.c.s._qcand.no_since or 0.0
    assert w.c.s._qcand.no_since is not None, "подготовка: отсчёт большинства не идёт"
    w.run(40, answer=True)
    assert w.v.view == "see" and not w.said("qrel"), f"qrel посреди отсчёта: {w.words}"
    assert w.v.pledge is not None and not w.yes(), "обязательство снято посреди отсчёта"
    while not w.said("qtake"):
        assert w.clock.t < t0 + T_UP + 10, f"большинство «нет» T_up — «забираю» нет: {w.words}"
        w.run(1, answer=True)
    assert [b for _, b in w.said("qtake")] == [{"l": LINE, "e": E}], f"«забираю» шлюзу 1: {w.words}"
    assert w.v.epoch == E + 1 and not w.yes(), "после qtake «да» шлюза прежнему лидеру под старой эпохой"
    w.run(w.v.timing.no_s + 5, answer=False)
    assert w.v.view == "uns"
    w.run(ASK, answer=True)
    assert w.c.stage() in ("ask", "points", "code"), "подготовка: исполнение решения снято"
    assert w.v.view == "see" and not w.said("qrel"), f"qrel посреди исполнения: {w.words}"
    assert w.v.epoch == E + 1 and not w.yes(), "поднятая по qtake эпоха откатилась посреди исполнения"


# ── условия правила у кандидата (без агента шлюза) ──────────────────────────

@pytest.fixture()
def c(box):
    return Cand(box)


def _no_then_see(c, *, own: str = "see") -> list[tuple]:
    """Шлюз 1 сказал «нет» (одно из трёх — без отсчёта) и снова «вижу»: действия такта после «вижу»."""
    c.own(own)
    c.view(1, "uns")
    assert not gw_sent(c.tick(), "qrel")
    c.view(1, "see")
    return c.tick()


@pytest.mark.parametrize("own", ["see", "new"])
def test_a_gateway_seeing_again_gets_qrel_once_while_nothing_is_decided(c, own):
    """Свой взгляд «вижу» или ещё «не смотрел» (включение голосования) — решения нет:
    `qrel{7}` шлюзу 1 тем же тактом и больше ни разу."""
    acts = _no_then_see(c, own=own)
    assert gw_sent(acts, "qrel") == [(1, {"l": LINE, "e": E})], acts
    assert not gw_sent(c.run(120), "qrel"), "qrel на одно обязательство повторяется"
    assert c.s._qcand.pledged() == []


def test_a_new_no_after_the_release_gets_its_own_qrel(c):
    _no_then_see(c)
    c.view(1, "uns", since="2026-10-30T10:20:00+03:00")
    assert not gw_sent(c.tick(), "qrel"), "qrel тому, кто сейчас «не вижу»"
    c.view(1, "see", since="2026-10-30T10:21:00+03:00")
    assert gw_sent(c.tick(), "qrel") == [(1, {"l": LINE, "e": E})], "второе обязательство не снято"


def test_a_dead_channel_gets_no_qrel_until_the_session_is_back(c):
    """Канал закрылся — слова нет; новая сессия забывает прежний взгляд и спрашивает заново
    (`qask`), обязательство кандидат помнит — свежее «вижу» снимает его."""
    c.own("see")
    c.view(1, "uns")
    c.tick()
    c.view(1, "see")
    c.b.gone(1)                                               # «вижу» пришло, и сессия тут же закрылась
    assert not gw_sent(c.tick() + c.run(20), "qrel"), "qrel по мёртвому каналу"
    assert gw_sent(c.b.hello(1), "qask"), "подготовка: новая сессия — взгляд шлюза не спрошен заново"
    assert not gw_sent(c.tick(), "qrel"), "qrel до свежего взгляда новой сессии"
    c.view(1, "see", since="2026-10-30T10:40:00+03:00")      # ответ на qask
    assert gw_sent(c.tick(), "qrel") == [(1, {"l": LINE, "e": E})], "после новой сессии обязательство не снято"


def test_a_gateway_still_not_seeing_gets_no_qrel(c):
    c.own("see")
    c.view(1, "uns")
    acts = c.tick() + c.run(120)
    assert not gw_sent(acts, "qrel"), "qrel шлюзу, который по-прежнему «не вижу»"


def test_the_epoch_of_the_gateways_see_must_match_the_known_one(c):
    """«Вижу» шлюза под эпохой 8 (поднял по `qtake` или опережает меня): `qrel{7}`
    обязательства под 8 не снимет, а поднятую 8 откатил бы — не шлю; узнал 8 фактом —
    `qrel{8}`."""
    c.own("see")
    c.view(1, "uns")
    c.tick()
    c.s.reserve_guest_in(1, "qview", {"l": LINE, "g": 1, "w": "see", "e": E + 1, "since": "2026-10-30T10:30:00+03:00"})
    assert not gw_sent(c.tick() + c.run(30), "qrel"), "qrel под эпохой, о которой шлюз не говорил"
    c.v.epoch_in(E + 1)
    assert gw_sent(c.tick(), "qrel") == [(1, {"l": LINE, "e": E + 1})]


def _state(c, kind: str, on: bool) -> None:
    s, cand = c.s, c.s._qcand
    if kind == "took":
        if on:
            s.__dict__["_quo_took"] = {"l": LINE, "e": E, "nodes": ["b2"]}
        else:
            s.__dict__.pop("_quo_took", None)
    elif kind == "exec":
        if on:
            s.__dict__["_quo_exec"] = {"stage": "ask", "at": c.b.clock.t, "e": E, "since": "", "points": {}, "need_at": 0.0,
                                       "own": "uns"}
        else:
            s.__dict__.pop("_quo_exec", None)
    elif kind == "decided":
        cand.decided = on
    elif kind in ("no_since", "rf_since"):
        setattr(cand, kind, c.b.clock.t if on else None)
    elif kind == "code":
        cw = dict(s.rsv_get("rsv_view").get("code") or {})
        cw["pending"] = on
        s.rsv_update("rsv_view", code=cw)
    elif kind == "zone":
        s.reserve_set_in("taking" if on else "idle")


@pytest.mark.parametrize("kind", ["took", "exec", "decided", "no_since", "rf_since", "code", "zone"])
def test_no_qrel_to_a_gateway_seeing_again_while_a_decision_or_execution_lives(c, kind):
    """Каждое условие порознь: отданный `qtake` (шлюз держит поднятую эпоху), исполнение,
    «забираю», отсчёт (короткий и случая 3), ввод кода, занятая зона — `qrel` нет и
    обязательство на месте; условие снято — тот же шлюз получает `qrel` один раз."""
    c.own("see")
    c.view(1, "uns")
    c.tick()
    c.view(1, "see")
    c.s._quorum_candidate_feed(c.s._qcand, c.b.clock.t)     # «вижу» в машину кандидата без такта
    _state(c, kind, True)
    assert c.s._quorum_seen_again(c.s._qcand) == [], f"{kind}: qrel посреди решения или исполнения"
    assert c.s._qcand.pledged() == ["b1"], f"{kind}: обязательство снято без слова"
    _state(c, kind, False)
    assert c.s._quorum_seen_again(c.s._qcand) == [("gw", 1, "qrel", {"l": LINE, "e": E})], kind
    assert c.s._quorum_seen_again(c.s._qcand) == [], f"{kind}: второй qrel на то же обязательство"
