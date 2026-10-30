"""Окно аварийного кода без решения по голосам (domain/services/reserve/
codewin.py, quorum/candidate.py): два новых случая, когда голосов для переезда
нет и не будет, а код админа основы — единственный путь.

- **v = 1** (Б): у второй стороны шлюзов с агентом 4.2.0 нет, голосует только
  этот сервер. Кандидат есть, свой взгляд — зонд к её AWG; окно открывается
  только по своему «не вижу» при свободной зоне, «забираю», опроса, ничьей,
  точек нет ни при каком взгляде и времени. Переходы v 1 → 2 и 2 → 1.
- **Запасной сигнал по линку пары** (В): кворум второй стороны не включён
  (цифры, блок зондов, слот, состав не подтверждён, ключи не у всех) —
  кандидата нет; если второй сервер не на связи по линку пары дольше
  `up_seconds`, окно открывается с причиной (Т-К21д), связь вернулась —
  закрывается, один раз на эпизод.

Мир — `_Box`/`Cand` из _reserve_vote_common: модельные часы (и стенные),
взгляд резерва на основу по линку пары, линия второй стороны словом `st.quo`.

Цена ошибки: при v = 1 «забираю» по одному голосу — переезд при живой основе,
чей бот видит только сеть этого сервера; окно, не открывшееся при v = 1 или
без кворума, — пользователи мёртвой основы без VPN до утра, админ основы даже
не знает, что нужен код; окно при занятой зоне — код поверх чужих
пользователей; тревога на каждый такт или окно, не закрытое вернувшейся
связью, — ложная авария у двух админов.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.core import settings
from awgbot.util import timeutil
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import (ADMIN, LINE, PEER_ADMIN, T_UP, Cand, box, gw_sent, notes, sent)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

SOLO = ["b", "a"]                                     # состав линии «Beta» без шлюзов: голосую только я


def _solo(box) -> Cand:
    box.peer_admin_opened()
    return Cand(box, members=SOLO)


def _full_tick(c: Cand) -> list[tuple]:
    """Такт кворума резерва в части окна кода: кандидат, затем запасной сигнал."""
    return c.tick() + c.s.reserve_code_link_watch()


def _run(c: Cand, seconds: float, step: float = 5) -> list[tuple]:
    acts: list[tuple] = []
    for _ in range(int(seconds // step)):
        c.b.advance(step)
        acts += _full_tick(c)
    return acts


def _code(s) -> dict:
    return dict(s.rsv_get("rsv_view").get("code") or {})


# ── v = 1: таблица ──────────────────────────────────────────────────────────

CASES = list(itertools.product(["see", "sus", "uns", "new"], [True, False], [True, False]))


@pytest.mark.parametrize("own, link_up, zone_free", CASES,
                         ids=[f"{o}-{'link' if lk else 'nolink'}-{'free' if z else 'busy'}" for o, lk, z in CASES])
def test_with_v_1_the_window_opens_only_on_own_unseen_with_a_free_zone(box, own, link_up, zone_free):
    c = _solo(box)
    box.primary(link_up, since=timeutil.now_iso())
    if not zone_free:
        box.s.reserve_set_in("taking")
    c.own(own)
    acts = _full_tick(c) + _run(c, 2 * T_UP)
    want = own == "uns" and zone_free
    assert box.s.reserve_code_window()["open"] is want, \
        f"v = 1, свой взгляд {own}, линк {'жив' if link_up else 'нет'}, зона {'свободна' if zone_free else 'занята'}: " \
        f"окно {'открыто' if box.s.reserve_code_window()['open'] else 'закрыто'}"
    assert not gw_sent(acts, "qtake") and not gw_sent(acts, "qask") and not sent(acts, "ask") and not box.launched


@pytest.mark.parametrize("own", ["see", "sus", "uns", "new", "held"])
def test_with_v_1_there_is_never_a_take_whatever_the_view_and_time(box, own):
    """«Забираю» при v = 1 не выдаётся ни при каком взгляде — модельные часы до
    10 × up_seconds; исполнения решения (просьба, точки) нет."""
    c = _solo(box)
    box.primary(False, since=timeutil.now_iso())
    c.own(own)
    acts = _full_tick(c) + _run(c, 10 * T_UP)
    assert not sent(acts, "ask") and not gw_sent(acts, "qneed") and not box.launched, \
        f"v = 1, взгляд {own}: решение по одному голосу: {[a for a in acts if a[0] in ('send', 'gw')]}"
    assert c.stage() == "", f"v = 1: исполнение решения {c.stage()!r}"
    assert box.s.reserve_quorum_candidate_view().get("v") == 1


def test_with_v_1_the_window_tells_both_admins_once_per_episode(box):
    c = _solo(box)
    box.primary(False, since=timeutil.now_iso())
    c.own("uns")
    first = _full_tick(c)
    by = {n.tg_id: n for n in notes(first)}
    assert set(by) == {ADMIN, PEER_ADMIN}, f"Т-К21г не обоим админам: {[(n.tg_id, n.text) for n in notes(first)]}"
    assert by[PEER_ADMIN].force_sound and by[PEER_ADMIN].keyboard == "peer_code", by[PEER_ADMIN]
    assert not notes(_run(c, 10 * T_UP)), "Т-К21г повторяется на каждом такте"


def test_with_v_1_the_window_text_does_not_promise_an_automatic_move(box):
    """Т-К21г: голосует только этот сервер — «увидят большинство — переедут сами»
    и «не видят 1 из 1, нужно 1» были бы неправдой: автоматического переезда
    при v = 1 нет. Текст говорит, что шлюзов «Beta» в голосовании нет."""
    c = _solo(box)
    c.own("uns")
    texts = {n.tg_id: n.text for n in notes(_full_tick(c))}
    assert texts, "окно при v = 1 не открылось"
    for who, text in texts.items():
        assert "переедут сами" not in text and "1 из 1" not in text, f"v = 1, админ {who}: обещание без голосов: {text}"
        assert "шлюз" in text, f"v = 1, админ {who}: не сказано, что шлюзов в голосовании нет: {text}"


def test_with_v_1_the_same_episode_again_is_silent_for_the_reserve_and_quiet_for_the_primary(box):
    c = _solo(box)
    c.own("uns")
    _full_tick(c)
    box.s.reserve_code_window_set(False)                   # окно закрыто (например, на время ввода)
    box.advance(5)
    again = notes(_full_tick(c))
    assert [n.tg_id for n in again] == [PEER_ADMIN] and again[0].silent, \
        f"повтор эпизода: {[(n.tg_id, n.silent) for n in again]}"


def test_with_v_1_own_see_closes_the_window_and_tells_the_primary_admin(box):
    c = _solo(box)
    c.own("uns")
    _full_tick(c)
    c.own("see")
    acts = _full_tick(c)
    assert not box.s.reserve_code_window()["open"], "v = 1: свой «вижу» — окно осталось"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "переезд не нужен" in told[0].text, [n.text for n in notes(acts)]
    assert "Большинство" not in told[0].text, f"v = 1: «большинство» из одного голоса: {told[0].text}"


def test_with_v_1_there_is_no_tie_alarm(box):
    c = _solo(box)
    c.own("uns")
    acts = _full_tick(c) + _run(c, 600)
    assert not [n for n in notes(acts) if "Ничья" in n.text], "ничья при одном голосующем"


# ── переходы v ──────────────────────────────────────────────────────────────

def test_v_1_to_2_moves_from_the_window_to_the_decision_without_losing_the_own_view(box):
    """Шлюз «Beta» объявил quo1: v 1 → 2. Свой «не вижу» сохранён, шлюз тоже не
    видит — большинство «нет» 2 из 2: окно v = 1 закрыто, через T_up «забираю»."""
    c = _solo(box)
    c.own("uns")
    _full_tick(c)
    assert box.s.reserve_code_window()["open"], "подготовка: окно v = 1 не открылось"
    box.peer_line(members=["b", "a", "b1"])
    box.hello(1)
    c.view(1, "uns")
    acts = _full_tick(c)
    assert box.s.reserve_quorum_candidate_view()["v"] == 2 and box.s.reserve_quorum_candidate_view()["own"] == "uns"
    assert not box.s.reserve_code_window()["open"], "v 1 → 2 с большинством «нет»: окно v = 1 осталось"
    acts += _run(c, T_UP + 5)
    assert gw_sent(acts, "qtake") == [(1, {"l": LINE, "e": 7})], f"v = 2, «нет» 2 из 2 — не забрано: {acts}"


def test_v_2_to_1_drops_the_decision_and_keeps_the_window_on_own_unseen(box):
    """Решение шло (окно кода в ходе решения — точек нет), шлюз выпал из состава:
    v 2 → 1 — исполнение снято, окно остаётся по правилу v = 1 (свой «не вижу»)."""
    box.online = False
    box.peer_admin_opened()
    c = Cand(box, members=["b", "a", "b1"])
    c.own("uns")
    c.view(1, "uns")
    _full_tick(c)
    _run(c, T_UP + 5)
    assert c.stage() == "points", f"подготовка: решение не дошло до точек: {c.stage()!r}"
    box.peer_line(members=SOLO)
    acts = _full_tick(c)
    assert c.stage() == "", "v 2 → 1: исполнение решения не снято"
    assert box.s.reserve_code_window()["open"], "v 2 → 1 при своём «не вижу»: окна кода нет"
    acts += _run(c, 2 * T_UP)
    assert not gw_sent(acts, "qneed") and not box.launched, "v = 1: точки просятся дальше"


def test_v_2_to_1_with_own_see_closes_the_decision_window(box):
    box.online = False
    box.peer_admin_opened()
    c = Cand(box, members=["b", "a", "b1"])
    c.own("uns")
    c.view(1, "uns")
    _full_tick(c)
    _run(c, T_UP + 5 + 60 + 5)                            # точки не пришли 2 × NEED_RETRY_S — окно в ходе решения
    assert box.s.reserve_code_window()["open"], "подготовка: окно в ходе решения не открылось"
    c.own("see")
    box.peer_line(members=SOLO)
    _full_tick(c)
    assert not box.s.reserve_code_window()["open"], "v 2 → 1 при своём «вижу»: окно кода осталось"


# ── запасной сигнал по линку пары ───────────────────────────────────────────

REASONS = [("timing:up_seconds 50 < …", "цифры времени"), ("overlap:10.0.0.0/8", "блок зондов"),
           ("slot:61", "слот"), ("not_acked", "состав"), ("keys", "ключи")]


def _off_line(box, why: str) -> None:
    box.peer_admin_opened()
    box.peer_line(active=False, why=[why])


def _link_down(box) -> None:
    box.primary(False, since=timeutil.now_iso())


def _watch(box, seconds: float, step: float = 5) -> list[tuple]:
    acts: list[tuple] = []
    for _ in range(int(seconds // step)):
        box.advance(step)
        acts += box.s.reserve_quorum_candidate_tick(box.clock.t) + box.s.reserve_code_link_watch()
    return acts


@pytest.mark.parametrize("why, word", REASONS, ids=[w for w, _ in REASONS])
def test_without_her_quorum_the_window_opens_after_t_up_of_no_link_and_names_the_reason(box, why, word):
    _off_line(box, why)
    _link_down(box)
    early = _watch(box, T_UP - 5)
    assert not box.s.reserve_code_window()["open"] and not notes(early), f"{why}: окно раньше up_seconds"
    acts = _watch(box, 10)
    assert box.s.reserve_code_window()["open"] and _code(box.s).get("by") == "link", f"{why}: окна по линку нет"
    by = {n.tg_id: n for n in notes(acts)}
    assert set(by) == {ADMIN, PEER_ADMIN}, f"{why}: Т-К21д не обоим админам: {[(n.tg_id, n.text) for n in notes(acts)]}"
    assert by[PEER_ADMIN].force_sound and by[PEER_ADMIN].keyboard == "peer_code"
    for n in by.values():
        assert "голосование о" in n.text and "не включено" in n.text, n.text
        assert word in n.text, f"{why}: в Т-К21д нет причины «{word}»: {n.text}"
    assert not notes(_watch(box, 10 * T_UP)), f"{why}: Т-К21д повторяется в том же эпизоде"


def test_the_link_coming_back_closes_the_link_window_and_says_so(box):
    _off_line(box, "keys")
    _link_down(box)
    _watch(box, T_UP + 5)
    assert box.s.reserve_code_window()["open"]
    box.primary(True)
    acts = _watch(box, 5)
    assert not box.s.reserve_code_window()["open"], "связь вернулась — окно по линку осталось"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "снова на связи" in told[0].text and "переезд не нужен" in told[0].text, \
        [n.text for n in notes(acts)]


def test_her_quorum_switching_on_hands_the_window_over_without_a_false_all_clear(box):
    """Кворум второй стороны включился, а связи по-прежнему нет: окно по линку
    уступает кандидату. Админу основы нельзя писать «снова на связи — переезд
    не нужен» — связи нет, и окно кандидата откроется следом."""
    _off_line(box, "keys")
    _link_down(box)
    _watch(box, T_UP + 5)
    assert _code(box.s).get("by") == "link"
    c = Cand(box, hello=False)                             # линия b включена, шлюзы молчат
    c.own("uns")
    acts = _watch(box, 10)
    assert _code(box.s).get("by") != "link", "кворум включился — окно по линку не уступило кандидату"
    told = [n.text for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert not [t for t in told if "снова на связи" in t], f"ложный отбой при живой аварии: {told}"


def test_a_second_link_loss_is_a_new_episode_told_again(box):
    _off_line(box, "not_acked")
    _link_down(box)
    assert notes(_watch(box, T_UP + 5))
    box.primary(True)
    _watch(box, 10)
    _link_down(box)
    acts = _watch(box, T_UP + 5)
    assert {n.tg_id for n in notes(acts) if "аварийн" in n.text} == {ADMIN, PEER_ADMIN}, \
        "второй обрыв связи — новый эпизод не сказан"


def test_with_v_1_the_link_signal_does_not_open_its_own_window(box):
    """Линия «Beta» включена с v = 1 — окно ведёт кандидат по своему взгляду (Б);
    по линку пары второго окна (и второго Т-К21) нет, даже если свой взгляд «вижу»."""
    c = _solo(box)
    c.own("see")
    _link_down(box)
    acts = _watch(box, 3 * T_UP)
    assert not box.s.reserve_code_window()["open"] and not notes(acts), \
        f"v = 1: окно по линку пары рядом с кандидатом: {_code(box.s)}"


@pytest.mark.parametrize("busy", ["taking", "serving"])
def test_no_link_window_while_the_zone_is_busy(box, busy):
    _off_line(box, "keys")
    box.s.reserve_set_in(busy)
    _link_down(box)
    acts = _watch(box, 2 * T_UP)
    assert not box.s.reserve_code_window()["open"] and not notes(acts), f"окно по линку при зоне {busy}"


@pytest.mark.parametrize("block", ["ready_off", "mine_away"])
def test_no_link_window_when_an_opening_could_not_start_anyway(box, block):
    """Предусловия открытия (готовность, мои пользователи здесь) не выполнены —
    код всё равно ничего не откроет: окно и звонок админу основы — обещание,
    которого не выполнить."""
    _off_line(box, "keys")
    if block == "ready_off":
        settings.set_value("app.reserve.ready", False)
    else:
        box.s.reserve_set_out("away")
    assert box.s.reserve_open_block(), "подготовка: предусловия открытия выполнены"
    _link_down(box)
    acts = _watch(box, 2 * T_UP)
    assert not box.s.reserve_code_window()["open"] and not notes(acts), \
        f"{block}: окно кода и Т-К21д, хотя открытие невозможно: {[n.text for n in notes(acts)]}"


def test_the_link_window_without_a_peer_admin_tells_only_mine(box):
    box.peer_line(active=False, why=["keys"])
    _link_down(box)
    ns = notes(_watch(box, T_UP + 5))
    assert [n.tg_id for n in ns] == [ADMIN], [(n.tg_id, n.text) for n in ns]
    assert "некуда" in ns[0].text, ns[0].text
