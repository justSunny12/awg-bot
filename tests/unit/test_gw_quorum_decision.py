"""Агент шлюза — голосующий кворума на этапе решения
(domain/gateway/reserve/quorum.py, runtime/linkclient/dispatch.py; концепт
«кворум» §3.4.3, §3.5.1, §3.6, §3.7.12): обязательство «нет» (отдал
`qview{uns}` — пробы основе идут `held`; держится в quorum.json и переживает
перезапуск; снимают `qrel{e}`, `qtake{e}` и срок), опрос `qask` (взгляд сразу
или после свежих проб), `qneed` → `qshare` только при своём «не вижу» и том же
поколении точки, `qpause` — «не вижу» не раньше срока, `qstate` — эпоха; слова
своего сервера по каналу конфигурации — в тот же разбор.

Хост — подмены: интерфейсы зондов (infra/quoprobe) — журнал поднятых, файлы
quorum.json и reserve.json — во временном каталоге; пробы и ответы — настоящие
датаграммы `Probe`/`Reply`; часы — модельные (подмена time модуля).

Цена ошибки: обязательство, потерянное при перезапуске агента, — его пробы
снова «вижу» под старой эпохой, и прежний лидер возвращается к записи при
живом новом держателе (два писателя); обязательство, не снятое `qrel`, — живой
лидер лишается голоса на 10 минут; точка, отданная при видимой основе или
чужого поколения, — кандидат собирает ключ без аварии или не собирает его
вовсе; пауза, не удержавшая «не вижу», — переезд всех пользователей на время
перезапуска бота.
"""
from __future__ import annotations

import json
import types

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import quorum as quomod
from awgbot.infra import quoprobe
from awgbot.infra.db import Database
from awgbot.util import timeutil
from awgbot.util.quorum.probe import Info, Probe, Reply

pytestmark = pytest.mark.unit

NAME = "awgqah"
CONF = ("[Interface]\nAddress = 169.254.88.5/32\nPrivateKey = DUMMY\n\n"
        "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 169.254.88.1/32\nEndpoint = 203.0.113.10:51820\n")
POINT_Y = "cd" * 16
Q_GEN = 4
E = 7


class _Clock:
    def __init__(self):
        self.t = 20_000.0

    def monotonic(self) -> float:
        return self.t


class _Agent:
    """Агент шлюза 1 линии `a`: такт, ответы основы, слова кандидата."""

    def __init__(self, tmp_path, monkeypatch, clock: _Clock):
        self.tmp = tmp_path
        self.clock = clock
        self.ups: list[str] = []
        monkeypatch.setattr(quomod, "time", types.SimpleNamespace(monotonic=clock.monotonic))
        monkeypatch.setattr(quoprobe, "up", lambda name, conf, port=0: self.ups.append(name) or "up: ok")
        monkeypatch.setattr(quoprobe, "down", lambda name: "down: ok")
        monkeypatch.setattr(quoprobe, "standing", lambda: list(self.ups))
        monkeypatch.setattr(quoprobe, "wipe", lambda: "wipe: ok")
        self.svc = self.new()

    def new(self) -> GatewayServices:
        """Свежий процесс агента на тех же файлах (перезапуск)."""
        d = Database(self.tmp / "gw.db")
        d.init_schema()
        svc = GatewayServices(d)
        svc._RSV_STORE = self.tmp / "reserve.json"
        svc._QUO_STORE = self.tmp / "quorum.json"
        return svc

    @property
    def store(self):
        return self.tmp / "quorum.json"

    def section(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def pledges(self) -> dict:
        return dict(self.section().get("pledges") or {})

    @property
    def v(self):
        return self.svc._gw_qvoters[NAME]

    def tick(self) -> list[tuple]:
        return self.svc.quorum_tick(self.clock.t)

    def run(self, seconds: float, *, answer: bool, step: float = 1.0, e: int | None = None) -> list[tuple]:
        """Такты по `step` секунд; answer — основа отвечает на каждую пробу."""
        acts: list[tuple] = []
        for _ in range(int(seconds // step)):
            self.clock.t += step
            out = self.tick()
            acts += out
            for a in out:
                if a[0] == "probe" and answer:
                    acts += self.reply(Probe.parse(a[2]).s, e=e)
        return acts

    def reply(self, s: int, e: int | None = None) -> list[tuple]:
        r = Reply("a", 1, E if e is None else e, "lead", s, 2, 3, Info(hs=4, flow=True, at="2026-10-30T10:00:00+03:00"))
        return self.svc.quorum_reply_in(NAME, r.encode())

    def probes(self, acts) -> list[Probe]:
        return [Probe.parse(a[2]) for a in acts if a[0] == "probe"]

    def word(self, kind: str, **body) -> list[tuple]:
        return self.svc.quorum_guest_in(kind, {"l": "a", **body})

    def see(self) -> None:
        self.run(60, answer=True)
        assert self.v.view == "see", "подготовка: основа не стала «вижу»"

    def uns(self) -> list[tuple]:
        acts = self.run(50, answer=False)
        assert self.v.view == "uns", "подготовка: «не вижу» не наступило"
        return acts


@pytest.fixture()
def clock():
    return _Clock()


@pytest.fixture()
def ag(tmp_path, monkeypatch, clock):
    (tmp_path / "reserve.json").write_text(json.dumps({"slot": 1, "name": "Alpha", "state": "idle"}), encoding="utf-8")
    (tmp_path / "quorum.json").write_text(json.dumps({
        "l": "a", "g": 1, "members": ["a", "b", "a1", "a2"], "t": 2, "port": 8789, "probes": {"h": CONF},
        "point": {"x": 2, "y": POINT_Y, "q_gen": Q_GEN}, "e": E}), encoding="utf-8")
    a = _Agent(tmp_path, monkeypatch, clock)
    a.tick()
    return a


def _kinds(acts, kind: str) -> list[dict]:
    return [a[2] for a in acts if a[0] in ("guest", "link") and a[1] == kind]


# ── обязательство «нет» ─────────────────────────────────────────────────────

def test_saying_uns_to_the_candidate_writes_a_pledge_and_probes_go_held(ag):
    ag.see()
    acts = ag.uns()
    views = _kinds(acts, "qview")
    assert views and all(v["w"] == "uns" for v in views), "«не вижу» кандидату не ушло"
    assert {a[0] for a in acts if a[0] in ("guest", "link")} == {"guest", "link"}, "qview — не резерву и своему серверу"
    assert ag.pledges().get(NAME, {}).get("e") == E, "обязательство «нет» не записано на диск"
    acts = ag.run(60, answer=True)
    assert ag.v.view == "see"
    ws = [p.w for p in ag.probes(acts)]
    assert ws and ws[-1] == "held", f"после «нет» пробы основе снова «вижу»: {ws}"


def test_qrel_of_my_epoch_lifts_the_pledge(ag):
    ag.see()
    ag.uns()
    ag.word("qrel", e=E + 1)
    assert NAME in ag.pledges(), "qrel чужой эпохи снял обязательство"
    ag.word("qrel", e=E)
    assert NAME not in ag.pledges(), "qrel своей эпохи не снял обязательство"
    acts = ag.run(60, answer=True)
    assert ag.probes(acts)[-1].w == "see", "после qrel пробы основе всё ещё held"


def test_qtake_moves_the_epoch_on_and_lifts_the_pledge(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    assert NAME not in ag.pledges() and ag.v.epoch == E + 1, "qtake{e}: эпоха не e + 1 или обязательство осталось"
    acts = ag.run(30, answer=True, e=E)
    assert all(p.e == E + 1 for p in ag.probes(acts)), "пробы прежнему лидеру идут со старой эпохой — он не узнает"


def test_the_pledge_survives_an_agent_restart(ag, clock):
    ag.see()
    ag.uns()
    ag.svc = ag.new()
    ag.tick()
    assert ag.v.pledge is not None and ag.v.pledge["e"] == E, "обязательство «нет» потеряно перезапуском агента"
    acts = ag.run(60, answer=True)
    assert ag.v.view == "see" and ag.probes(acts)[-1].w == "held", "после перезапуска пробы основе снова «вижу»"


def test_an_old_pledge_on_disk_is_dropped_at_restart(ag):
    sec = ag.section()
    sec["pledges"] = {NAME: {"e": E, "at": timeutil.to_iso(timeutil.shift(timeutil.now(), -601))}}
    ag.store.write_text(json.dumps(sec), encoding="utf-8")
    ag.svc = ag.new()
    ag.tick()
    assert ag.v.pledge is None and NAME not in ag.pledges(), "обязательство старше pledge_minutes пережило перезапуск"


def test_the_pledge_expires_by_its_term(ag):
    ag.see()
    ag.uns()
    at = ag.v.pledge["at"]
    ag.run(at + 595 - ag.clock.t, answer=False)
    assert NAME in ag.pledges(), "обязательство снято раньше срока"
    ag.run(10, answer=False)
    assert NAME not in ag.pledges() and ag.v.pledge is None, "срок обязательства вышел, а запись осталась"


# ── опрос ───────────────────────────────────────────────────────────────────

def test_qask_while_not_seeing_answers_at_once(ag):
    ag.uns()
    acts = ag.word("qask", g=1)
    assert [a[0] for a in acts] == ["guest", "link"] and all(a[1] == "qview" and a[2]["w"] == "uns" for a in acts), acts


def test_qask_with_a_fresh_reply_answers_see_with_the_traffic_note(ag):
    ag.see()
    acts = ag.word("qask", g=1)
    views = _kinds(acts, "qview")
    assert len(views) == 2 and views[0]["w"] == "see" and views[0].get("i", {}).get("hs") == 4, acts
    assert NAME not in ag.pledges(), "«вижу» записало обязательство"


def test_qask_with_an_old_reply_probes_first_and_answers_after_15_seconds(ag):
    ag.see()
    ag.v.last_reply = ag.clock.t - 30                    # последний ответ старше fresh_s
    ag.v.next_probe = ag.clock.t + 1000
    assert ag.word("qask", g=1) == [], "старый ответ — взгляд сразу, без свежих проб"
    acts = ag.run(14, answer=True)
    assert ag.probes(acts), "после qask со старым ответом проба не ушла сразу"
    assert not _kinds(acts, "qview"), "qview раньше 15 с"
    acts = ag.run(2, answer=True)
    views = _kinds(acts, "qview")
    assert views and views[0]["w"] == "see", f"после свежих проб qview не ушёл: {acts}"


def test_qask_without_the_section_line_is_ignored(ag):
    assert ag.svc.quorum_guest_in("qask", {"l": "b", "g": 1}) == [], "слово о чужой линии разобрано"


# ── точка ключа кворума ─────────────────────────────────────────────────────

def test_qneed_while_not_seeing_and_the_same_generation_gives_the_point(ag):
    ag.uns()
    acts = ag.word("qneed", q_gen=Q_GEN, since="2026-10-30T10:05:00+03:00")
    assert acts == [("guest", "qshare", {"l": "a", "q_gen": Q_GEN, "x": 2, "y": POINT_Y})], acts
    assert json.loads((ag.tmp / "reserve.json").read_text(encoding="utf-8")).get("share_given_at"), "отметка отданной точки не записана"


def test_qneed_while_seeing_is_refused(ag):
    ag.see()
    assert ag.word("qneed", q_gen=Q_GEN) == [], "точка отдана при видимой основе"
    assert not json.loads((ag.tmp / "reserve.json").read_text(encoding="utf-8")).get("share_given_at")


def test_qneed_of_another_generation_is_refused(ag):
    ag.uns()
    assert ag.word("qneed", q_gen=Q_GEN - 1) == [], "точка отдана под чужое поколение многочлена"


def test_the_share_mark_survives_a_new_document(ag):
    ag.uns()
    ag.word("qneed", q_gen=Q_GEN)
    mark = json.loads((ag.tmp / "reserve.json").read_text(encoding="utf-8"))["share_given_at"]
    sec = ag.section()
    ag.svc.quorum_apply({k: sec[k] for k in ("l", "g", "members", "t", "port", "probes", "point", "e")})
    assert json.loads((ag.tmp / "reserve.json").read_text(encoding="utf-8")).get("share_given_at") == mark, "новый раздел документа стёр отметку отданной точки"


# ── пауза и эпоха ───────────────────────────────────────────────────────────

def test_qpause_holds_uns_until_the_term(ag):
    ag.see()
    until = timeutil.to_iso(timeutil.shift(timeutil.now(), 120))
    ag.word("qpause", until=until)
    acts = ag.run(100, answer=False, step=5)
    assert ag.v.view != "uns" and not [v for v in _kinds(acts, "qview") if v["w"] == "uns"], "«не вижу» в паузе лидера"
    acts = ag.run(30, answer=False, step=5)
    assert ag.v.view == "uns", "после срока паузы «не вижу» не наступило"


def test_qpause_is_capped_at_five_minutes(ag):
    ag.see()
    ag.word("qpause", until=timeutil.to_iso(timeutil.shift(timeutil.now(), 3600)))
    ag.run(320, answer=False, step=5)
    assert ag.v.view == "uns", "пауза на час держит «не вижу» дольше 5 минут"


def test_qstate_raises_the_known_epoch(ag):
    ag.word("qstate", e=E + 3)
    assert ag.v.epoch == E + 3
    ag.word("qstate", e=E)
    assert ag.v.epoch == E + 3, "qstate с меньшей эпохой откатил известную"


# ── канал конфигурации ──────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["qask", "qtake", "qrel", "qneed", "qpause", "qstate"])
async def test_quorum_words_of_my_server_go_to_the_same_handler(kind, monkeypatch):
    """Свой сервер — кандидат своей линии в отъезде: его слова по каналу
    конфигурации — тот же разбор и исполнение действий, что у резерва."""
    from awgbot.runtime import guestlink
    from awgbot.runtime.linkclient.dispatch import DispatchMixin
    seen: list = []
    ran: list = []

    class _Svc:
        def quorum_guest_in(self, k, body):
            seen.append((k, body.get("l")))
            return [("guest", "qview", {"w": "uns"})]

    async def run_actions(services, actions):
        ran.append(list(actions))
    monkeypatch.setattr(guestlink, "run_actions", run_actions)

    class _C(DispatchMixin):
        pass
    c = _C()
    c.services = _Svc()
    await c._dispatch({"t": kind, "l": "a"})
    assert seen == [(kind, "a")], f"{kind} своего сервера не дошёл до разбора кворума"
    assert ran == [[("guest", "qview", {"w": "uns"})]], "действия разбора не исполнены"


# ── цифры лидера из раздела quorum ──────────────────────────────────────────

def test_the_agent_counts_with_the_leaders_timing_from_the_section(ag):
    """Решение 31.10.2026 (вариант 1): цифры кворума едут шлюзу в разделе quorum; без них —
    умолчания агента; смена цифр у лидера перенастраивает голосующего без пересоздания."""
    ag.run(3, answer=True)
    v = ag.v
    assert ag.svc.quorum_timing().probe_s == 15 and v.timing.probe_s == 15, "без цифр в разделе — не умолчания"
    sec = ag.section()
    sec["timing"] = {"probe_seconds": 10, "yes_window_seconds": 30, "no_seconds": 30, "rf_block_minutes": 12}
    ag.store.write_text(json.dumps(sec), encoding="utf-8")
    t = ag.svc.quorum_timing()
    assert (t.probe_s, t.yes_window_s, t.no_s, t.rf_block_s) == (10, 30, 30, 720), t
    ag.run(2, answer=True)
    assert ag.v is v and v.timing.probe_s == 10, "смена цифр лидера не дошла до голосующего или пересоздала его"
