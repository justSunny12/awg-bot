"""Сторож минимума обновления: `UPDATE_FROM` (awgbot/core/compat.py) — это
`requires` обеих ролей в метаданных выпуска (docs/releases/vX.json,
docs/RELEASING.md §2). Константу читают `awg-bot update` и установщик, чтобы
отказать прыжку через минимум до остановки сервиса; метаданные читает
цепочка бота. Разойдись они — терминал пустил бы хост, которому бот сказал
бы «сначала 4.0.0», или наоборот, отказал бы тому, кого бот обновил бы.

Версия дерева между выпусками равна последнему выпуску (бамп — при выпуске),
поэтому «файл метаданных текущей версии» не отличает выпущенное дерево от
дерева со сдвигом минимума, который ещё только выйдет. Сторож держит то, что
верно в любом состоянии: минимум — версия, выпущенная и адресованная обеим
ролям; минимум не ниже минимумов последнего выпуска (он только растёт, и
единственное допустимое расхождение — сдвиг вверх, который выпуск запишет в
свой файл как `requires`); минимум не новее последнего выпуска. Равенство
`requires` = `UPDATE_FROM` в файле нового выпуска держит сам инструмент
выпуска: `release_meta --main X --gw X` отказывает при X ≠ UPDATE_FROM; выпуск
одной роли (как 4.0.1) сверяет её одну.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awgbot.core import compat
from awgbot.__version__ import __version__

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "docs" / "releases"


def _key(v: str) -> tuple:
    k = compat.version_key(v)
    assert k, f"версия не читается: {v!r}"
    return k[:3]


def _releases() -> dict[str, dict]:
    out = {}
    for f in RELEASES.glob("v*.json"):
        meta = json.loads(f.read_text(encoding="utf-8"))
        out[str(meta["version"])] = meta
    assert out, "в docs/releases нет ни одного файла метаданных"
    return out


def _requires(meta: dict) -> dict[str, str]:
    return {role: str(spec["requires"]) for role, spec in meta.get("roles", {}).items() if "requires" in spec}


def test_update_from_is_a_released_step_addressed_to_both_roles():
    """Минимум — только версия, которую обе роли получили (RELEASING §2):
    иначе подсказка «сначала обнови до v…» вела бы на выпуск, которого у
    роли нет."""
    step = _releases().get(compat.UPDATE_FROM)
    assert step is not None, f"UPDATE_FROM {compat.UPDATE_FROM}: нет docs/releases/v{compat.UPDATE_FROM}.json"
    assert set(_requires(step)) == {"main", "gw"}, \
        f"выпуск {compat.UPDATE_FROM} адресован не обеим ролям: {sorted(_requires(step))}"


def test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases():
    """Минимум только растёт: ниже `requires` последнего выпуска — ошибка (бот
    пустил бы хост, которому терминал откажет); новее последнего выпуска —
    прыгать было бы не с чего. Выше `requires` — сдвиг минимума, который
    ближайший выпуск обязан записать в свой файл."""
    rel = _releases()
    latest = max(rel, key=_key)
    assert _key(latest) <= _key(__version__), f"последний выпуск {latest} новее дерева {__version__}"
    req = _requires(rel[latest])
    assert set(req) >= {"main"}, f"у последнего выпуска {latest} нет минимума main"
    for role, floor in req.items():
        assert _key(compat.UPDATE_FROM) >= _key(floor), \
            f"UPDATE_FROM {compat.UPDATE_FROM} ниже минимума {role} последнего выпуска {latest} ({floor}) — минимум не опускается"
    assert _key(compat.UPDATE_FROM) <= _key(latest), \
        f"UPDATE_FROM {compat.UPDATE_FROM} новее последнего выпуска {latest} — прыгать было бы не с чего"


def test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards():
    """Файл версии дерева есть: либо `requires` его адресатов = UPDATE_FROM
    (выпущенное дерево; выпуск одной роли сверяет её одну), либо UPDATE_FROM
    выше всех (сдвиг минимума ждёт следующего выпуска). Разные минимумы у ролей при сдвиге — не бывает:
    ступень-минимум адресуется обеим (RELEASING §2)."""
    mine = _releases().get(__version__)
    if mine is None:
        pytest.skip(f"docs/releases/v{__version__}.json ещё нет — дерево до выпуска")
    req = _requires(mine)
    assert req, f"выпуск {__version__} без адресата: {mine}"
    if all(v == compat.UPDATE_FROM for v in req.values()):   # выпуск одной роли (как 4.0.1) — сверяем её
        return
    assert all(_key(compat.UPDATE_FROM) > _key(v) for v in req.values()), (
        f"UPDATE_FROM {compat.UPDATE_FROM} против requires выпуска {__version__} {req}: "
        "не равенство и не сдвиг вверх — сторож терминала и цепочка бота разошлись бы")


def test_the_shell_side_reads_the_constant_exactly_as_python_sees_it():
    """`update_from` в install/lib/update.sh и `src_update_from` установщика —
    sed по той же строке: число, которое увидит админ в отказе, обязано быть
    числом из модуля."""
    import re
    import subprocess
    src = (ROOT / "install" / "lib" / "update.sh").read_text(encoding="utf-8")
    head = "update_from() {"
    assert head in src
    fn = head + src.split(head, 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    r = subprocess.run(["bash", "-c", fn + f'update_from "{ROOT}"'], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.stdout.strip() == compat.UPDATE_FROM, r.stdout + r.stderr
    installer = (ROOT / "install" / "awg-bot-install.sh").read_text(encoding="utf-8")
    pat = re.search(r"sed -nE 's/\^UPDATE_FROM[^']*'", installer)
    assert pat, "установщик не читает UPDATE_FROM"
    r = subprocess.run(["bash", "-c", f'{pat.group(0)} "{ROOT}/awgbot/core/compat.py"'],
                       capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert r.stdout.strip() == compat.UPDATE_FROM, r.stdout + r.stderr


# ── сторож на подставном каталоге выпусков ───────────────────────────────────

def _catalog(tmp_path, monkeypatch, releases: dict[str, dict], *, tree: str, update_from: str):
    """Каталог docs/releases из файлов {версия: {роль: requires}}, версия дерева и
    UPDATE_FROM — подставные: сторож проверяется на состоянии «после выпуска»,
    которого в репозитории ещё нет."""
    import sys
    me = sys.modules[__name__]
    d = tmp_path / "releases"; d.mkdir()
    for ver, roles in releases.items():
        meta = {"schema": 1, "version": ver, "awg_gen": 1,
                "roles": {role: {"requires": req} for role, req in roles.items()}}
        (d / f"v{ver}.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(me, "RELEASES", d)
    monkeypatch.setattr(me, "__version__", tree)
    monkeypatch.setattr(compat, "UPDATE_FROM", update_from)


_BEFORE = {"4.0.0": {"main": "3.3.0", "gw": "3.3.0"}, "4.0.1": {"main": "3.3.0"},
           "4.0.2": {"main": "3.3.0", "gw": "3.3.0"}}


def test_after_the_release_of_the_shift_the_tree_agrees_with_its_own_file(tmp_path, monkeypatch):
    """Выпуск 4.1.0 записал requires 4.0.0 обеим ролям, дерево — 4.1.0, UPDATE_FROM
    4.0.0: все три сторожа зелёные, и третий идёт веткой равенства, а не
    пропуском «файла ещё нет»."""
    _catalog(tmp_path, monkeypatch, {**_BEFORE, "4.1.0": {"main": "4.0.0", "gw": "4.0.0"}},
             tree="4.1.0", update_from="4.0.0")
    test_update_from_is_a_released_step_addressed_to_both_roles()
    test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases()
    test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards()   # без skip


def test_a_release_minimum_above_update_from_is_caught(tmp_path, monkeypatch):
    """Выпуск записал requires выше константы (4.0.2), а UPDATE_FROM остался 4.0.0:
    бот скажет «сначала 4.0.2», терминал пустит 4.0.0 — оба сторожа обязаны
    покраснеть."""
    _catalog(tmp_path, monkeypatch, {**_BEFORE, "4.1.0": {"main": "4.0.2", "gw": "4.0.2"}},
             tree="4.1.0", update_from="4.0.0")
    with pytest.raises(AssertionError, match="ниже минимума"):
        test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases()
    with pytest.raises(AssertionError, match="не равенство и не сдвиг вверх"):
        test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards()


def test_a_minimum_released_to_one_role_only_or_never_released_is_caught(tmp_path, monkeypatch):
    """Подсказка «сначала обнови до v4.0.1» на шлюзе вела бы на выпуск, которого
    у шлюза нет; на 4.0.5 — на выпуск, которого нет вовсе."""
    _catalog(tmp_path, monkeypatch, dict(_BEFORE), tree="4.0.2", update_from="4.0.1")
    with pytest.raises(AssertionError, match="адресован не обеим ролям"):
        test_update_from_is_a_released_step_addressed_to_both_roles()
    monkeypatch.setattr(compat, "UPDATE_FROM", "4.0.5")
    with pytest.raises(AssertionError, match="нет docs/releases/v4.0.5.json"):
        test_update_from_is_a_released_step_addressed_to_both_roles()


def test_a_shift_waiting_for_the_next_release_passes_and_a_minimum_ahead_of_releases_does_not(tmp_path, monkeypatch):
    """Дерево 4.0.2 (последний выпуск), UPDATE_FROM поднят до 4.0.2 — сдвиг ждёт
    выпуска: зелёный; UPDATE_FROM новее последнего выпуска — прыгать не с чего."""
    _catalog(tmp_path, monkeypatch, dict(_BEFORE), tree="4.0.2", update_from="4.0.2")
    test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases()
    test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards()
    monkeypatch.setattr(compat, "UPDATE_FROM", "4.1.0")
    with pytest.raises(AssertionError, match="новее последнего выпуска"):
        test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases()


def test_a_release_to_one_role_with_its_minimum_at_update_from_passes(tmp_path, monkeypatch):
    """Выпуск только main (как 4.0.1) с requires = UPDATE_FROM: дерево выпущено,
    сторож зелёный — выпуск одной роли не обязан ломать сборку следующей."""
    _catalog(tmp_path, monkeypatch, {**_BEFORE, "4.1.0": {"main": "4.0.0", "gw": "4.0.0"},
                                     "4.1.1": {"main": "4.0.0"}}, tree="4.1.1", update_from="4.0.0")
    test_update_from_is_a_released_step_addressed_to_both_roles()
    test_update_from_never_falls_below_the_last_released_minimum_and_never_outruns_the_releases()
    test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards()   # без skip


def test_a_release_to_one_role_with_a_minimum_above_update_from_is_caught(tmp_path, monkeypatch):
    """Выпуск одной роли — не лазейка: его requires выше константы ловится так же."""
    _catalog(tmp_path, monkeypatch, {**_BEFORE, "4.1.0": {"main": "4.0.0", "gw": "4.0.0"},
                                     "4.1.1": {"main": "4.0.2"}}, tree="4.1.1", update_from="4.0.0")
    with pytest.raises(AssertionError, match="не равенство и не сдвиг вверх"):
        test_the_released_tree_agrees_with_its_own_metadata_or_carries_a_shift_upwards()
