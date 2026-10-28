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
`requires` = `UPDATE_FROM` в файле нового выпуска — дело выпуска
(`release_meta --main X --gw X`, X = UPDATE_FROM).
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
    """Файл версии дерева есть: либо `requires` обеих ролей = UPDATE_FROM
    (выпущенное дерево), либо UPDATE_FROM выше обоих (сдвиг минимума ждёт
    следующего выпуска). Разные минимумы у ролей при сдвиге — не бывает:
    ступень-минимум адресуется обеим (RELEASING §2)."""
    mine = _releases().get(__version__)
    if mine is None:
        pytest.skip(f"docs/releases/v{__version__}.json ещё нет — дерево до выпуска")
    req = _requires(mine)
    assert set(req) == {"main", "gw"}, f"выпуск {__version__} адресован не обеим ролям: {sorted(req)}"
    if req == {"main": compat.UPDATE_FROM, "gw": compat.UPDATE_FROM}:
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
