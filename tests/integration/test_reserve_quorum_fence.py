"""Правило лидера кворума с действиями (domain/services/reserve/quorum/leader.py,
fence.py; концепт «кворум» §3.4, §3.7.12, §5.2): старт без права записи до
первого большинства «да», лишение кворума через `jrn_fence` (Т-К7 без звука
один раз на эпизод, отметка `rsv_qlog.fenced`), возврат через `jrn_unfence`
после окна U (Т-К8), принятие чужой эпохи по пробе и по `qstate`
(`jrn_adopt`), снятие ограждения при выключенном кворуме, роль основы в `home` у линии с
кворумом — только по факту.

Мир — основа «Alpha» в `home` в паре с «Beta» (tests/integration/_reserve_move.py),
своя линия `a` с составом a, b, a1, a2 (голосующих у лидера три: b, a1, a2;
«да» ≥ v/2 — от двух). Включённость кворума (ключи у всех держателей,
подтверждения) — подмена одного предиката `reserve_quorum_active`: её собирают
тесты этапа ключей. Пробы — настоящие датаграммы `Probe` на ответчик с адресов
зондов узлов; часы правила лидера — модельные.

Цена ошибки: лидер без большинства продолжает писать решения — при переезде
по точкам у пользователей два бота с расходящимися базами; лидер, не
вернувшийся к записи, — бот молчит пользователям, хотя его все видят; Т-К7 на
каждый такт — шум у админа, Т-К8 после старта — ложная тревога «кворум
вернулся», хотя его не теряли; принятая по пробе чужая эпоха, которую тут же
снимает страховка такта, — два писателя под разными эпохами.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve.quorum import fence as fence_mod
from awgbot.domain.services.reserve.quorum import leader as leader_mod
from awgbot.util import probenet
from awgbot.util.quorum.probe import Info, Probe, Reply

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

MEMBERS = ["a", "b", "a1", "a2"]
W = 40              # окно «да»
T_DOWN = 20
U = 30


class _Q:
    """Сценарий лидера: модельные часы, пробы голосующих, такт."""

    def __init__(self, svc, monkeypatch):
        self.s = svc
        self.t = 50_000.0
        self.seq = 0
        ns = types.SimpleNamespace(monotonic=lambda: self.t)
        monkeypatch.setattr(leader_mod, "time", ns)
        monkeypatch.setattr(fence_mod, "time", ns)
        self.active = True
        monkeypatch.setattr(svc, "reserve_quorum_active", lambda: self.active)
        monkeypatch.setattr(svc, "_quorum_info", lambda: Info(hs=3, flow=True, at=""))
        svc.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": list(MEMBERS)},
                       mine={"l": "a", "g": 1, "members": list(MEMBERS)})

    def advance(self, s: float) -> None:
        self.t += float(s)

    def probe(self, node: str, w: str = "see", e: int | None = None, l: str = "a") -> bytes | None:
        self.seq += 1
        e = self.s.reserve_quorum_epoch() if e is None else e
        p = Probe(l, 1, node, int(e), w, self.seq)
        return self.s.reserve_quorum_probe_in(p.encode(), probenet.node_addr(l, node))

    def tick(self) -> list[tuple]:
        return self.s.reserve_quorum_leader_tick(self.t)

    def meta(self) -> tuple[str, str, int]:
        m = self.s.db.jrn_meta()
        return m["holder"], m["role"], int(m["epoch"])

    def fenced_mark(self) -> dict:
        return dict(self.s.rsv_get("rsv_qlog").get("fenced") or {})


def _notes(actions) -> list:
    out = []
    for a in actions or []:
        if a and a[0] == "notify":
            out += list(a[1])
    return out


@pytest.fixture()
def q(mv, monkeypatch):
    settings.set_value("app.reserve.quorum.act", True)
    return _Q(mv.svc, monkeypatch)


def _lead(q) -> None:
    """Старт и первое большинство — лидер пишет."""
    q.tick()
    q.probe("b")
    q.probe("a1")
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), "подготовка: лидер не вернулся к записи после старта"


def _fence(q) -> list[tuple]:
    """Голоса пропали дольше W + T_down — лишение кворума; действия такта лишения."""
    q.advance(W + 1)
    assert q.tick() == [], "лишение раньше T_down"
    q.advance(T_DOWN)
    return q.tick()


# ── старт ───────────────────────────────────────────────────────────────────

def test_a_starting_leader_writes_nothing_until_the_first_majority_of_yes(q):
    """Концепт «кворум» §3.7.12: за время, пока лидера не было, кандидат мог
    забрать пользователей — первые секунды без права записи."""
    acts = q.tick()
    assert q.meta()[:2] == ("self", "follower"), "старт лидера оставил право записи до первых «да»"
    assert "старт" in q.s.db.jrn_meta()["why"], q.s.db.jrn_meta()["why"]
    assert q.s.reserve_quorum_fenced(), "загородка бота не знает, что лидер без права записи"
    assert _notes(acts) == [], "старт лидера — не повод для Т-К7"
    assert q.fenced_mark().get("start") is True and q.fenced_mark().get("on") is True


def test_the_first_majority_after_start_unfences_without_the_u_window_and_says_nothing(q):
    q.tick()
    q.probe("b")
    q.probe("a1")
    acts = q.tick()                                       # тот же момент: окна U нет
    assert q.meta()[:2] == ("self", "holder"), "первое большинство после старта ждёт окно U"
    assert not q.s.reserve_quorum_fenced()
    assert _notes(acts) == [], "Т-К8 после старта: кворум не теряли — «вернулся» ложь"
    assert q.fenced_mark().get("on") is False


def test_one_yes_of_three_is_not_a_majority_at_start(q):
    q.tick()
    q.probe("b")
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "одно «да» из трёх сняло ограждение старта"


# ── лишение кворума ─────────────────────────────────────────────────────────

def test_losing_the_majority_longer_than_t_down_fences_and_tells_the_admin_silently_once(q):
    _lead(q)
    acts = _fence(q)
    assert q.meta()[:2] == ("self", "follower"), "лидер без большинства дольше T_down пишет решения"
    assert q.meta()[2] == q.s.reserve_quorum_epoch() and q.s.reserve_quorum_fenced()
    notes = _notes(acts)
    assert len(notes) == 1 and notes[0].tg_id == config.ADMIN_ID, notes
    assert notes[0].silent, "Т-К7 со звуком — решение 7: без звука"
    assert "без кворума" in notes[0].text and "Alpha" in notes[0].text and "0 из 3" in notes[0].text, notes[0].text
    mark = q.fenced_mark()
    assert mark.get("on") and mark.get("since") and mark.get("told"), mark
    # повтор решения в том же эпизоде — без второго Т-К7
    again = q.s.reserve_quorum_fence_apply("fence", q.s._qleader)
    assert _notes(again) == [], "Т-К7 второй раз в том же эпизоде"
    assert _notes(q.tick()) == [], "такт после лишения повторил Т-К7"


def test_a_single_yes_does_not_keep_the_leader(q):
    _lead(q)
    q.advance(W + 1)
    q.probe("a2")                                        # одно «да» из трёх — меньше половины
    q.tick()
    q.advance(T_DOWN)
    q.probe("a2")
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "одно «да» из трёх удержало лидера"


def test_majority_back_longer_than_u_unfences_and_tells_t_k8(q):
    _lead(q)
    _fence(q)
    q.probe("b")
    q.probe("a1")
    assert _notes(q.tick()) == []
    q.advance(U - 5)
    q.probe("b")
    q.probe("a1")
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "возврат к записи раньше окна U"
    q.advance(5)
    q.probe("b")
    q.probe("a1")
    acts = q.tick()
    assert q.meta()[:2] == ("self", "holder"), "большинство «да» дольше U не вернуло право записи"
    assert not q.s.reserve_quorum_fenced()
    notes = _notes(acts)
    assert len(notes) == 1 and notes[0].silent and "Кворум вернулся" in notes[0].text and "2 из 3" in notes[0].text, notes
    assert q.fenced_mark().get("on") is False and q.fenced_mark().get("until")


def test_a_new_episode_after_the_return_tells_t_k7_again(q):
    _lead(q)
    _fence(q)
    for _ in range(2):
        q.probe("b")
        q.probe("a1")
        q.tick()
        q.advance(U)
    q.probe("b")
    q.probe("a1")
    q.tick()
    assert q.meta()[:2] == ("self", "holder")
    first_since = q.fenced_mark().get("since")
    acts = _fence(q)
    assert len(_notes(acts)) == 1, "Т-К7 второго эпизода не пришёл"
    assert q.fenced_mark().get("on") is True and q.fenced_mark().get("told") is True
    assert q.fenced_mark().get("since") >= first_since


def test_held_yes_is_not_counted_for_the_return(q):
    """Концепт «кворум» §3.4.3: взгляд `held` (обязательство «нет») лидер не
    считает — иначе отдавшие «нет» вернули бы его к записи при новом держателе."""
    _lead(q)
    _fence(q)
    for _ in range(3):
        q.probe("b", "held")
        q.probe("a1", "held")
        q.tick()
        q.advance(U)
    assert q.meta()[:2] == ("self", "follower"), "«да» под обязательством вернули лидера к записи"


# ── без решений ─────────────────────────────────────────────────────────────

def test_with_act_off_the_count_runs_but_the_journal_is_not_touched(q):
    settings.set_value("app.reserve.quorum.act", False)
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), "act=false: старт лидера всё равно снял право записи"
    q.probe("b")
    q.probe("a1")
    q.tick()
    acts = _fence(q)
    assert q.meta()[:2] == ("self", "holder"), "act=false: лишение кворума тронуло журнал"
    assert _notes(acts) == [] and not q.s.reserve_quorum_fenced()
    c = q.s.reserve_quorum_count()
    assert c["role"] == "fenced" and c["y"] == 0 and c["v"] == 3, "наблюдение: счёт для карточки не идёт"


def test_the_quorum_switched_off_while_fenced_lifts_the_fence(q):
    """Лидер без голосующих решений не лишается: выключенный кворум (шлюз снят,
    состав распался) не должен оставить бота без права записи навсегда."""
    _lead(q)
    _fence(q)
    q.active = False
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), "кворум выключен, а ограждение осталось"
    assert not q.s.reserve_quorum_fenced() and q.fenced_mark().get("on") is False


def test_the_tick_insurance_does_not_lift_the_quorum_fence(q):
    """Концепт «кворум» §5.2 п. 3: правило «пользователи здесь → держатель»
    снимало бы ограждение на каждом такте."""
    _lead(q)
    _fence(q)
    q.s.reserve_epoch_sync()
    q.s.reserve_epoch_check()
    assert q.meta()[:2] == ("self", "follower"), "страховка такта сняла ограждение кворума"


def test_unfence_if_off_leaves_a_follower_of_the_peer_alone(q):
    """Ограждение — только `self` + `follower`; следование за чужой эпохой
    снятие ограждения не трогает."""
    q.s.db.jrn_adopt(5, why="тест: эпоха у второй стороны")
    q.s.rsv_update("rsv_qlog", fenced={"on": True, "since": "2026-10-30T10:00:00+03:00"})
    q.active = False
    q.tick()
    assert q.meta() == ("peer", "follower", 5), "снятие ограждения перевело следующего в держатели"


# ── чужая эпоха ─────────────────────────────────────────────────────────────

def test_a_probe_with_a_larger_epoch_adopts_it_and_still_gets_a_reply(q):
    """Голосующий узнал от кандидата `qtake{e}` эпоху e + 1 — прежний лидер
    принимает её без приращения (`jrn_adopt`), следует; ответ уходит, иначе
    голосующий сочтёт лидера мёртвым."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    raw = q.probe("a1", e=e + 1)
    assert raw is not None, "проба с эпохой больше — без ответа"
    r = Reply.parse(raw)
    assert r is not None and r.e == e + 1, "ответ несёт прежнюю эпоху: голосующий не узнает, что лидер принял новую"
    assert q.meta() == ("peer", "follower", e + 1), "эпоха больше моей не принята"
    assert not q.s.reserve_quorum_fenced(), "следующий — не лишённый кворума: держатель не я"


def test_an_adopted_epoch_survives_the_tick_insurance(q):
    """Принятая по пробе эпоха — факт (концепт «кворум» §5.2: следование в `home` —
    по эпохе больше своей). Страховка такта, вернувшая держателя `self` с
    приращением, дала бы второго писателя рядом с кандидатом, забравшим
    пользователей."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.probe("a1", e=e + 1)
    q.s.reserve_epoch_check()
    assert q.meta() == ("peer", "follower", e + 1), "страховка такта сняла принятую по пробе эпоху"


def test_a_probe_with_a_larger_epoch_does_nothing_to_the_journal_with_act_off(q):
    settings.set_value("app.reserve.quorum.act", False)
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    assert q.probe("a1", e=e + 1) is not None
    assert q.meta() == ("self", "holder", e), "act=false: проба тронула журнал"


def test_qstate_of_my_line_with_a_larger_epoch_adopts_and_a_smaller_one_does_not(q):
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.s.reserve_quorum_pair_in("qstate", {"l": "a", "e": e, "holder": "self", "r": "lead"})
    assert q.meta() == ("self", "holder", e), "qstate с той же эпохой отнял право записи"
    q.s.reserve_quorum_pair_in("qstate", {"l": "b", "e": e + 7, "holder": "self", "r": "lead"})
    assert q.meta() == ("self", "holder", e), "qstate чужой линии принят за мою эпоху"
    q.s.reserve_quorum_pair_in("qstate", {"l": "a", "e": e + 1, "holder": "peer", "r": "lead"})
    assert q.meta() == ("peer", "follower", e + 1), "qstate своей линии с эпохой больше не принят"


# ── бот основы на резерве ───────────────────────────────────────────────────

def test_the_guest_leader_fences_its_own_base_the_same_way(services, monkeypatch):
    """Лидер-гость (клон основы у резерва) — то же правило на своей базе:
    состав ему даёт бот резерва (`quorum{l, g, members}`), голосующие — a, a1, a2."""
    settings.set_value("app.reserve.quorum.act", True)
    monkeypatch.setattr(config, "GUEST", True)
    q = _Q(services, monkeypatch)
    services.reserve_quorum_guest_in({"l": "a", "g": 1, "members": MEMBERS, "port": 8789})
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "гость на старте лидера пишет решения"
    q.probe("a1")
    q.probe("a")
    q.tick()
    assert q.meta()[:2] == ("self", "holder"), "гость с большинством «да» не вернулся к записи"
    q.advance(W + 1)
    q.tick()
    q.advance(T_DOWN)
    acts = q.tick()
    assert q.meta()[:2] == ("self", "follower") and len(_notes(acts)) == 1, "гость без большинства не лишён кворума"


# ── роль в `home` у линии с кворумом ────────────────────────────────────────

def test_with_the_quorum_a_foreign_webhook_at_home_does_not_make_a_follower(q):
    """Концепт «кворум» §5.2 п. 1 (4.2.0): 409 и чужой вебхук — только остановка
    поллинга; роль — по фактам (эпоха, лишение кворума)."""
    _lead(q)
    q.s.rsv_update("rsv_peer_state", st_stale=True)        # резерв не подтвердил, что моих не несёт
    q.s.reserve_set_poll("away", why="webhook")
    q.s.reserve_epoch_sync()
    assert not q.s.db.jrn_following(), "слово Telegram у линии с кворумом отдало право записи"
    assert q.s._follow_wanted()[1].startswith("кворум"), q.s._follow_wanted()


def test_without_the_quorum_a_foreign_webhook_at_home_still_makes_a_follower(q):
    """Линия на матрице — правило 4.1.0 без изменений."""
    q.active = False
    q.s.rsv_update("rsv_peer_state", st_stale=True)
    q.s.reserve_set_poll("away", why="webhook")
    q.s.reserve_epoch_sync()
    assert q.s.db.jrn_following(), "без кворума вебхук у резерва больше не отдаёт право записи"


def test_with_the_quorum_a_reserve_serving_my_clients_after_home_makes_a_follower(q):
    _lead(q)
    rm.st_in(q.s, **{"in": "serving"})
    q.s.reserve_epoch_sync()
    assert q.s.db.jrn_following(), "резерв несёт моих пользователей, а основа с кворумом пишет решения"
