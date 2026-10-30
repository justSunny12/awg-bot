"""Правило лидера кворума на основе — ветви, которых нет в test_reserve_quorum_fence
(domain/services/reserve/quorum/leader.py, fence.py): старт без большинства дольше
T_down (Т-К7 один раз, и не снова после перезапуска), смена состава на лету,
отъезд (счёт Q2 без решений), все ветви снятия ограждения при выключенном
кворуме, отказы принятия чужой эпохи, принятая эпоха по `qstate` и после
перезапуска процесса — в away, следующий за второй стороной без Т-К7,
плановая пауза из синхронного кода, мусорный состав у бота основы на резерве.

Мир — основа «Alpha» в `home` в паре с «Beta» (tests/integration/_reserve_move.py),
своя линия `a`, состав a, b, a1, a2 (голосующих три). Включённость кворума и
причины выключения — подмены предикатов; пробы — настоящие датаграммы на ответчик;
часы правила лидера — модельные. «Перезапуск процесса» — сброс памяти сервисов
(всё, что держит процесс, а не диск).

Цена ошибки: лидер, стартовавший без большинства, молчит пользователям, а
админ об этом не знает; после перезапуска тот же эпизод снова шлёт Т-К7 —
шум; ограждение, снятое «кворум выключен» в ходе переезда или когда выключено
только у меня (ключи, состав), — второй писатель при живом решении второй
стороны; принятая эпоха, забытая при перезапуске, — основа возвращается к
записи при живом новом держателе.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve.quorum import fence as fence_mod
from awgbot.domain.services.reserve.quorum import leader as leader_mod
from awgbot.util import probenet, timeutil
from awgbot.util.quorum.probe import Info, Probe, Reply

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

MEMBERS = ["a", "b", "a1", "a2"]
W, T_DOWN, U = 40, 20, 30


class _Q:
    def __init__(self, svc, monkeypatch):
        self.s = svc
        self.t = 50_000.0
        self.seq = 0
        ns = types.SimpleNamespace(monotonic=lambda: self.t)
        monkeypatch.setattr(leader_mod, "time", ns)
        monkeypatch.setattr(fence_mod, "time", ns)
        self.active = True
        self.why: list[str] = ["no_gateways"]
        monkeypatch.setattr(svc, "reserve_quorum_active", lambda: self.active)
        monkeypatch.setattr(svc, "reserve_quorum_off_reasons", lambda: [] if self.active else list(self.why))
        monkeypatch.setattr(svc, "_quorum_info", lambda: Info(hs=3, flow=True, at=""))
        self.members(MEMBERS)

    def members(self, members, g: int = 1) -> None:
        self.s.rsv_update("rsv_quorum", active={"l": "a", "g": g, "members": list(members)},
                          mine={"l": "a", "g": g, "members": list(members)})

    def probe(self, node: str, w: str = "see", e: int | None = None, g: int = 1) -> Reply | None:
        self.seq += 1
        e = self.s.reserve_quorum_epoch() if e is None else e
        raw = self.s.reserve_quorum_probe_in(Probe("a", g, node, int(e), w, self.seq).encode(), probenet.node_addr("a", node))
        return Reply.parse(raw) if raw else None

    def tick(self) -> list[tuple]:
        return self.s.reserve_quorum_leader_tick(self.t)

    def run(self, seconds: float, yes=(), step: float = 5) -> list[tuple]:
        acts: list[tuple] = []
        for i in range(int(seconds // step)):
            self.t += step
            if i % 3 == 0:
                for n in yes:
                    self.probe(n)
            acts += self.tick()
        return acts

    def meta(self) -> tuple[str, str, int]:
        m = self.s.db.jrn_meta()
        return m["holder"], m["role"], int(m["epoch"])

    def restart(self) -> None:
        """Перезапуск процесса: память сервисов — заново, диск (`rsv_*`, журнал) — тот же."""
        for k in [k for k in self.s.__dict__ if k.startswith(("_quo", "_rsv_q", "_guest_q"))]:
            self.s.__dict__.pop(k, None)


def _notes(actions, word: str = "") -> list:
    return [n for a in actions or [] if a and a[0] == "notify" for n in a[1] if word in n.text]


@pytest.fixture()
def q(mv, monkeypatch):
    settings.set_value("app.reserve.quorum.act", True)
    return _Q(mv.svc, monkeypatch)


def _lead(q) -> None:
    q.tick()
    q.probe("b")
    q.probe("a1")
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), "подготовка: лидер не вернулся к записи после старта"


def _fence(q) -> None:
    _lead(q)
    q.t += W + 1
    q.tick()
    q.t += T_DOWN
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "подготовка: лидер не лишён кворума"


# ── старт без большинства ───────────────────────────────────────────────────

def test_a_start_without_a_majority_longer_than_t_down_tells_t_k7_once(q):
    """После перезапуска основы голосующих нет на связи: без права записи — молча
    первые T_down (ждём голосов), дальше — Т-К7 без звука, один раз на эпизод."""
    acts = q.tick()
    assert _notes(acts) == []
    acts = q.run(T_DOWN - 5)
    assert _notes(acts) == [], "Т-К7 раньше T_down после старта"
    acts = q.run(5)
    tk7 = _notes(acts, "без кворума")
    assert len(tk7) == 1 and tk7[0].silent, f"старт без большинства дольше T_down — нет Т-К7: {acts}"
    assert "видят 0 из 3" in tk7[0].text and not tk7[0].text.endswith("."), tk7[0].text
    assert _notes(q.run(600), "без кворума") == [], "Т-К7 стартового эпизода повторяется"
    assert q.s.reserve_quorum_count()["fenced_since"], "карточка не знает, с какого момента без кворума"


def test_after_a_restart_the_told_start_episode_is_not_told_again(q):
    q.tick()
    q.run(T_DOWN + 5)
    assert (q.s.rsv_get("rsv_qlog").get("fenced") or {}).get("told"), "подготовка: Т-К7 не отмечен на диске"
    q.restart()
    acts = q.tick() + q.run(T_DOWN + 30)
    assert _notes(acts, "без кворума") == [], "после перезапуска тот же эпизод снова дал Т-К7"
    assert q.meta()[:2] == ("self", "follower")


def test_the_first_majority_after_a_long_start_unfences_and_tells_t_k8(q):
    """Т-К7 уже ушёл — значит, возврат к записи объявляется Т-К8 (админ знает, что
    бот молчал)."""
    q.tick()
    q.run(T_DOWN + 5)
    acts = q.run(10, yes=("b", "a1"))
    assert q.meta()[:2] == ("self", "holder"), "большинство после долгого старта не вернуло запись"
    assert len(_notes(acts, "Кворум вернулся")) == 1, acts


# ── состав на лету ──────────────────────────────────────────────────────────

def test_a_composition_grown_while_leading_needs_the_new_majority(q):
    """Подключены шлюзы a3 и a4 (поколение 2): прежние два «да» — не половина из
    пяти. Окно прежнего поколения держит голоса, но не знаменатель."""
    _lead(q)
    q.members(["a", "b", "a1", "a2", "a3", "a4"], g=2)
    acts = q.run(W + T_DOWN + 10, yes=("a1", "a2"))         # без «да» кандидата — короткий T_down
    assert q.meta()[:2] == ("self", "follower"), "два «да» из пяти держат лидера после смены состава"
    assert len(_notes(acts, "без кворума")) == 1
    assert q.s.reserve_quorum_count()["v"] == 5


def test_a_composition_change_keeps_counting_the_old_generation_in_its_window(q):
    _lead(q)
    q.probe("a2")
    q.members(["a", "b", "a1", "a2", "a3"], g=2)
    q.tick()
    assert q.s.reserve_quorum_count()["y"] == 3, "голоса прежнего поколения сразу после смены выпали из счёта"
    q.run(T_DOWN + 10, yes=("b", "a1"))
    assert q.meta()[:2] == ("self", "holder"), "два «да» из четырёх — половина, лидер держится"


# ── отъезд: счёт Q2 без решений ─────────────────────────────────────────────

def test_while_away_the_home_responder_counts_but_never_fences(q, monkeypatch):
    _lead(q)
    monkeypatch.setattr(q.s, "reserve_quorum_leader_host", lambda: "b")
    monkeypatch.setattr(q.s, "reserve_out", lambda: "away")
    before = q.meta()
    acts = q.tick() + q.run(W + T_DOWN + 60)
    assert acts == [], f"в отъезде такт ответчика дома решает: {acts}"
    assert q.meta() == before, "в отъезде без голосов журнал тронут"
    r = q.probe("b", e=0)
    assert r is not None and r.r == "follow", "ответ дома в отъезде — не роль follow"


# ── снятие ограждения при выключенном кворуме ───────────────────────────────

@pytest.mark.parametrize("out", ["leaving", "away", "returning"])
def test_the_fence_holds_while_a_move_is_going_on(q, monkeypatch, out):
    _fence(q)
    q.active = False
    monkeypatch.setattr(q.s, "reserve_out", lambda: out)
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), f"«кворум выключен» в ходе {out} снял ограждение"


@pytest.mark.parametrize("why", [["keys"], ["not_acked"], ["timing:up_seconds 50 < 80"], ["keys", "not_acked"]])
def test_the_fence_holds_when_the_quorum_is_off_only_on_my_side(q, why):
    """Ключи, подтверждение состава, цифры — выключено только у меня: вторая
    сторона может решать по своему составу, ограждение держится."""
    _fence(q)
    q.active, q.why = False, why
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), f"причина {why} сняла ограждение"
    assert q.s.reserve_quorum_fenced()


@pytest.mark.parametrize("why", [["peer_old"], ["no_gateways"], ["no_pair"], ["keys", "peer_old"], ["overlap:10.0.0.0/8"]])
def test_a_structural_switch_off_lifts_the_fence(q, why):
    _fence(q)
    q.active, q.why = False, why
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), f"структурное выключение {why} не сняло ограждение"
    assert not q.s.reserve_quorum_fenced()
    assert (q.s.rsv_get("rsv_qlog").get("fenced") or {}).get("on") is False


def test_nothing_fenced_and_the_quorum_off_touches_nothing(q):
    _lead(q)
    q.active = False
    meta = q.s.db.jrn_meta()
    q.tick()
    assert q.s.db.jrn_meta() == meta, "выключение без ограждения тронуло журнал"


# ── принятие чужой эпохи: отказы ────────────────────────────────────────────

def test_a_failing_adopt_leaves_no_mark_and_the_journal_as_it_was(q, monkeypatch):
    _lead(q)
    e = q.s.reserve_quorum_epoch()

    def boom(*a, **k):
        raise RuntimeError("база занята")
    monkeypatch.setattr(q.s.db, "jrn_adopt", boom)
    assert q.probe("a1", e=e + 1) is not None, "отказ журнала оставил пробу без ответа"
    assert not q.s.reserve_quorum_adopted(), "отметка принятия осталась без принятой эпохи"
    assert q.meta() == ("self", "holder", e)


def test_an_adopt_the_journal_did_not_take_leaves_no_mark(q, monkeypatch):
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    monkeypatch.setattr(q.s.db, "jrn_adopt", lambda *a, **k: None)
    q.probe("a1", e=e + 1)
    assert not q.s.reserve_quorum_adopted(), "отметка принятия при непринятой эпохе — страховка не вернёт запись"


def test_a_follower_of_the_peer_is_not_told_it_lost_the_quorum(q):
    """Эпоху держит вторая сторона (принял по пробе): «да» пропали — это не моё
    лишение кворума, Т-К7 не нужен."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.probe("a1", e=e + 1)
    assert q.meta()[0] == "peer"
    acts = q.run(W + T_DOWN + 30)
    assert _notes(acts, "без кворума") == [], "следующий за второй стороной получил Т-К7"


# ── принятая эпоха → away ───────────────────────────────────────────────────

def test_an_epoch_adopted_by_qstate_at_home_moves_me_to_away(q, monkeypatch):
    """Не только проба: `qstate` второго сервера с эпохой больше моей — тот же факт."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.s.reserve_quorum_pair_in("qstate", {"l": "a", "e": e + 1, "holder": "self", "r": "lead"})
    assert q.meta() == ("peer", "follower", e + 1) and q.s.reserve_quorum_adopted()
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "down")
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "эпоха, принятая по qstate, оставила основу дома"


def test_an_adopted_epoch_survives_a_restart_and_still_moves_me_to_away(q, monkeypatch):
    """Отметка принятия на диске: после перезапуска страховка такта эпоху не
    возвращает, автомат уходит в away тем же правилом."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.probe("a1", e=e + 1)
    q.restart()
    q.s.reserve_epoch_check()
    assert q.meta() == ("peer", "follower", e + 1), "после перезапуска страховка вернула принятую эпоху"
    assert q.s.reserve_quorum_adopted()
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "down")
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "после перезапуска принятая эпоха не повела автомат"


def test_an_adopted_epoch_needs_the_peers_word_after_a_restart_too(q):
    """После перезапуска большинство «да» под новой эпохой — не повод к записи: нужна
    живая вторая сторона, сказавшая после принятия, что моих у неё нет."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.probe("a1", e=e + 1)
    q.restart()
    for _ in range(40):
        q.t += 5
        q.probe("b", e=e + 1)
        q.probe("a1", e=e + 1)
        q.tick()
    assert q.meta()[0] == "peer", f"после перезапуска «да» вернули запись без слова второй стороны: {q.meta()}"


# ── плановая пауза из синхронного кода ──────────────────────────────────────

def test_the_pause_from_sync_code_goes_to_the_bot_loop(q, monkeypatch):
    from awgbot.runtime import rsvactions
    got = []
    monkeypatch.setattr(rsvactions, "run_threadsafe", lambda acts: got.append(list(acts)))
    q.s.reserve_quorum_pause_sync(120)
    assert got and {a[0] for a in got[0]} <= {"send", "gwlink"}, got
    gw_slots = sorted(a[1] for a in got[0] if a[0] == "gwlink")
    assert gw_slots == [1, 2] and all(a[2] == "qpause" for a in got[0] if a[0] == "gwlink"), got
    until = next(a[-1]["until"] for a in got[0])
    left = timeutil.elapsed(timeutil.now(), timeutil.parse_iso(until))
    assert 100 <= left <= 121, f"срок паузы не тот: {until}"


def test_the_pause_from_sync_code_without_the_quorum_sends_nothing(q, monkeypatch):
    from awgbot.runtime import rsvactions
    got = []
    monkeypatch.setattr(rsvactions, "run_threadsafe", lambda acts: got.append(acts))
    q.active = False
    q.s.reserve_quorum_pause_sync()
    assert got == []


def test_a_broken_loop_does_not_break_the_update_step(q, monkeypatch):
    from awgbot.runtime import rsvactions

    def boom(acts):
        raise RuntimeError("цикла нет")
    monkeypatch.setattr(rsvactions, "run_threadsafe", boom)
    q.s.reserve_quorum_pause_sync()                           # не бросает


# ── бот основы на резерве: состав ───────────────────────────────────────────

@pytest.mark.parametrize("body", [{}, {"l": "", "members": []}, {"l": "a", "g": 1, "members": []}])
def test_a_composition_without_a_line_does_not_end_the_waiting(services, monkeypatch, body):
    """Состояние «жду состав» снимает только состав: слово `quorum` без линии или
    без узлов — не состав, гость остаётся без права записи."""
    settings.set_value("app.reserve.quorum.act", True)
    monkeypatch.setattr(config, "GUEST", True)
    q = _Q(services, monkeypatch)
    services.reserve_quorum_guest_start()
    services.reserve_quorum_guest_in(body)
    q.tick()
    q.t += T_DOWN
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), f"пустой состав {body} снял ограждение гостя"
    assert services.reserve_quorum_guest_phase() == "waiting", f"пустой состав {body} кончил ожидание"
