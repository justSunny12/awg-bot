"""Матрица взглядов резерва (domain/services/reserve/vote.decide): случай по
взгляду резерва на основу и взглядам голосующих шлюзов — и сторожа «ожидания
нет».

Правило матрицы записано здесь своим словарём «взгляд резерва × что говорят
отвечающие» (все «вижу» / все «не вижу» / смешанно / никого), а не повтором
кода: таблица истинности перебирает 1, 2 и 3 шлюза во всех сочетаниях и
сверяет каждую клетку. Строки пользователя Н1–Н4 и два неназванных сочетания
— отдельными именованными случаями (их недоступные шлюзы в decide не
попадают: набор голосующих считает vote._voters, это проверяет
tests/integration/test_reserve_vote.py на той же таблице с поколениями долей).

Цена ошибки: не та клетка — переезд при живой основе (случаи 2, 4, 6) или
его отсутствие при мёртвой (1, 5); таймер «подождать недоступного шлюза» —
простой клиентов ровно тогда, когда авария настоящая; ключ voter_grace в
шаблоне — обещание настройки, которой больше нет.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from awgbot.domain.services.reserve import vote

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]

# взгляд резерва × что говорят отвечающие → случай (таблица матрицы)
TABLE = {
    (False, "all_unseen"): "1",
    (False, "all_see"): "2",
    (False, "mixed"): "5",
    (False, "none"): "H4",
    (True, "all_unseen"): "3",
    (True, "mixed"): "4",
    (True, "all_see"): "6",
    (True, "none"): "H3",
}
MOVES = {"1", "5"}                  # переезд по долям шлюзов сразу


def _kind(views: dict) -> str:
    vals = set(views.values())
    if not vals:
        return "none"
    if vals == {"see"}:
        return "all_see"
    if vals == {"unseen"}:
        return "all_unseen"
    return "mixed"


@pytest.mark.parametrize("sees", [False, True], ids=["reserve_blind", "reserve_sees"])
@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_every_cell_of_the_truth_table_matches_the_matrix(sees, n):
    """Каждое сочетание взглядов 0–3 голосующих: итог — клетка таблицы."""
    for combo in itertools.product(("see", "unseen"), repeat=n):
        views = {slot: v for slot, v in zip((1, 2, 3), combo)}
        want = TABLE[(sees, _kind(views))]
        got = vote.decide(sees, views)
        assert got == want, f"резерв {'видит' if sees else 'не видит'}, шлюзы {views}: {got}, а по матрице {want}"


def test_the_order_of_gateways_does_not_change_the_case():
    """Решение — по составу, не по порядку слотов: «первый ответивший» не
    решает за остальных."""
    for sees in (False, True):
        for combo in itertools.permutations(("see", "unseen", "see")):
            views = dict(zip((3, 1, 2), combo))
            assert vote.decide(sees, views) == vote.decide(sees, dict(sorted(views.items())))


# ── строки пользователя и неназванные сочетания ─────────────────────────────
# Недоступные шлюзы в набор не входят — в decide их просто нет.

NAMED = [
    # (имя, резерв видит, голосующие, случай)
    ("H1_some_see_rest_unreachable", True, {1: "see"}, "6"),
    ("H2_some_unseen_rest_unreachable", False, {1: "unseen"}, "1"),
    ("H3_reserve_sees_nobody_answers", True, {}, "H3"),
    ("H4_nobody_at_all", False, {}, "H4"),
    ("unnamed_blind_some_see_rest_unreachable", False, {2: "see"}, "2"),
    ("unnamed_sees_some_unseen_rest_unreachable", True, {2: "unseen"}, "3"),
]


@pytest.mark.parametrize("name,sees,views,case", NAMED, ids=[n[0] for n in NAMED])
def test_the_named_rows_of_the_matrix(name, sees, views, case):
    assert vote.decide(sees, views) == case, name


def test_examples_with_two_gateways_from_the_matrix():
    """Примеры с двумя шлюзами — построчно (недоступный в набор не входит)."""
    rows = [
        (False, {1: "unseen", 2: "unseen"}, "1"),
        (False, {1: "unseen"}, "1"),
        (False, {1: "see", 2: "see"}, "2"),
        (False, {1: "see"}, "2"),
        (False, {1: "see", 2: "unseen"}, "5"),
        (False, {}, "H4"),
        (True, {1: "unseen", 2: "unseen"}, "3"),
        (True, {1: "unseen"}, "3"),
        (True, {1: "unseen", 2: "see"}, "4"),
        (True, {1: "see"}, "6"),
        (True, {1: "see", 2: "see"}, "6"),
        (True, {}, "H3"),
    ]
    for sees, views, case in rows:
        assert vote.decide(sees, views) == case, (sees, views)


def test_a_move_by_shares_is_decided_only_when_the_reserve_does_not_see_the_primary():
    """Сразу (без демпфера) переезжают только 1 и 5 — и оба только при
    «резерв не видит»; при живой для резерва основе — никогда."""
    for n in range(4):
        for combo in itertools.product(("see", "unseen"), repeat=n):
            views = dict(zip((1, 2, 3), combo))
            assert vote.decide(True, views) not in MOVES, f"переезд по долям при живой для резерва основе: {views}"
    assert set(vote.MOVE_CASES) == MOVES and set(vote.RF_CASES) == {"3", "H3"}


# ── «ожидания нет» ──────────────────────────────────────────────────────────

def _settings_keys(path: Path) -> set[str]:
    """Ключи настроек, которые модуль читает (settings.get*("…"))."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "settings"
                and node.args and isinstance(node.args[0], ast.Constant)):
            out.add(str(node.args[0].value))
    return out


def test_the_voting_module_reads_no_timer_that_waits_for_an_unreachable_gateway():
    """Сторож: модуль голосования читает только пороги живости, готовность и
    анти-дребезг. Ключ вида «сколько ждать шлюз» появился — значит решение
    снова откладывается до ответа недоступного (было Р24, отменено)."""
    path = ROOT / "awgbot" / "domain" / "services" / "reserve" / "vote.py"
    keys = _settings_keys(path)
    allowed = {"app.reserve.suspect_seconds", "app.reserve.probe_seconds", "app.reserve.ready",
               "app.reserve.min_interval_minutes"}
    assert keys <= allowed, f"модуль голосования читает новые настройки: {sorted(keys - allowed)}"
    src = path.read_text(encoding="utf-8").lower()
    assert "grace" not in src, "в модуле голосования снова «grace» — ожидание недоступного шлюза"
    consts = {t.id for node in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(node, ast.Assign)
              for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()}
    # FAIL_DAMPER_S — пауза после срыва открытия (не ожидание шлюза)
    assert consts == {"NEED_RETRY_S", "CODE_TRIES", "CODE_WINDOW_S", "MOVE_CASES", "RF_CASES", "FAIL_DAMPER_S"}, (
        f"новые постоянные модуля голосования (таймер ожидания?): {sorted(consts)}")


def test_the_only_rf_timer_is_the_damper_and_its_retry():
    """Демпфер случаев 3 и Н3 — единственный таймер решения; кроме него
    rfblock читает только ожидание ответа основы и разрешение РФ-доступа."""
    keys = _settings_keys(ROOT / "awgbot" / "domain" / "services" / "reserve" / "rfblock.py")
    assert keys <= {"app.reserve.rf_block_minutes", "app.reserve.ask_seconds", "app.reserve.rf_via_mine"}, sorted(keys)


def test_the_template_has_no_voter_grace_key():
    text = (ROOT / "conf" / "app.yaml").read_text(encoding="utf-8")
    assert "voter_grace" not in text, "в шаблоне app.yaml ключ ожидания голосующего — его больше нет"
    for key in ("suspect_seconds:", "probe_seconds:", "rf_block_minutes:", "ask_seconds:", "min_interval_minutes:"):
        assert key in text, f"в шаблоне нет ключа {key}"
