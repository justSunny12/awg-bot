"""Сторожа рельс общих разделов (bot/sections): реестр, словари ролей и
кнопки сходятся друг с другом, а модули разделов не ветвятся по имени роли.

Цена ошибки: ID в корне роли без модуля — корень падает на KeyError у всех;
модуль ни в одном корне — мёртвый код, который тянут эталоны; BACK в никуда
— «⬅️ Назад» уходит в «Кнопка устарела»; кнопка правки на ключ без границ —
«Эта настройка недоступна»; ключ в KEYS двух разделов — агент открывает ввод
не в том разделе (раздел ключевых действий агента ищется по KEYS); сравнение
с именем роли в модуле раздела — второй роли достаётся чужое поведение
молча, и рельсы «один модуль на обе роли» перестают быть правдой.
"""
from __future__ import annotations

import ast
import itertools
import pathlib

import pytest
from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import roles, sections, texts
from awgbot.bot.sections.base import Confirm
from awgbot.core import settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
SECTIONS_DIR = ROOT / "awgbot" / "bot" / "sections"
BRS = [roles.MAIN, roles.GATEWAY]
REQUIRED = ("ID", "LABEL", "BACK", "KEYS", "BOUNDS", "CYCLES", "ACTIONS", "screen", "keyboard")


# ── нет ветвлений по роли ────────────────────────────────────────────────────

ROLE_WORDS = {"main", "gateway", "client"}


def role_branches(path: pathlib.Path) -> list[str]:
    """Места в исходнике, где модуль раздела узнаёт роль не из словаря:
    сравнение с .name / именем роли / ROLE, roles.current(), config.ROLE,
    импорт словарей ролей (MAIN, GATEWAY, roles) — модуль получает br параметром."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def where(node, what):
        found.append(f"{path.name}:{node.lineno}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Attribute) and side.attr in ("name", "ROLE"):
                    where(node, f"сравнение с .{side.attr}")
                elif isinstance(side, ast.Constant) and side.value in ROLE_WORDS:
                    where(node, f"сравнение с «{side.value}»")
        elif isinstance(node, ast.Match):
            subj = node.subject
            if isinstance(subj, ast.Attribute) and subj.attr in ("name", "ROLE"):
                where(node, f"match по .{subj.attr}")
        elif isinstance(node, ast.Attribute) and node.attr in ("ROLE", "current", "pick"):
            where(node, f"обращение к .{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = {a.name for a in node.names}
            if mod.endswith("roles") or "roles" in names or names & {"MAIN", "GATEWAY"}:
                where(node, f"импорт словаря ролей ({mod}: {', '.join(sorted(names))})")
        elif isinstance(node, ast.Import):
            if any(a.name.endswith(".roles") for a in node.names):
                where(node, "импорт словаря ролей")
    return found


def test_section_modules_never_ask_which_role_they_serve():
    files = sorted(SECTIONS_DIR.glob("*.py"))
    assert {p.stem for p in files} >= {m.__name__.rsplit(".", 1)[-1] for m in sections.MODULES}
    bad = [hit for p in files for hit in role_branches(p)]
    assert not bad, "модуль раздела узнаёт роль мимо словаря br:\n" + "\n".join(bad)


@pytest.mark.parametrize("src", [
    'def f(br):\n    if br.name == "gateway":\n        return 1\n',
    'def f(br):\n    return 1 if "main" != br.name else 2\n',
    'from awgbot.core import config\nX = config.ROLE == "gateway"\n',
    'from awgbot.bot import roles\ndef f():\n    return roles.current()\n',
    'from awgbot.bot.roles import GATEWAY\n',
    'def f(br):\n    match br.name:\n        case "main":\n            return 1\n',
])
def test_the_role_branch_guard_sees_each_form(tmp_path, src):
    """Сторож не пустой: каждая форма ветвления по роли им поймана."""
    probe = tmp_path / "probe.py"
    probe.write_text(src, encoding="utf-8")
    assert role_branches(probe), src


# ── реестр и словари ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("br", BRS, ids=lambda b: b.name)
def test_every_root_and_subsection_id_is_a_module_or_a_role_section(br):
    for sec in (*br.settings_root, *br.subsections):
        assert sec in sections.SECTIONS or sec in br.root_labels, \
            f"{br.name}: «{sec}» в корне/подразделах — ни модуля, ни ролевого раздела"
    both = set(br.root_labels) & set(sections.SECTIONS)
    assert not both, f"{br.name}: ролевой раздел совпал по ID с общим: {both}"
    assert set(br.root_labels) <= set(br.settings_root), f"{br.name}: подпись раздела, которого нет в корне"


def test_every_module_is_reachable_by_some_role():
    for m in sections.MODULES:
        assert any(sections.available(br, m.ID) for br in BRS), f"раздел «{m.ID}» не доступен ни одной роли"


@pytest.mark.parametrize("m", sections.MODULES, ids=lambda m: m.ID)
def test_every_module_declares_the_whole_contract(m):
    missing = [a for a in REQUIRED if not hasattr(m, a)]
    assert not missing, f"«{m.ID}»: нет {missing}"
    assert sections.SECTIONS[m.ID] is m
    if m.ID == "root":
        assert m.BACK == ""
    else:
        assert m.BACK in sections.SECTIONS, f"«{m.ID}»: BACK «{m.BACK}» ведёт в никуда"
    assert set(m.BOUNDS) <= set(m.KEYS), f"«{m.ID}»: границы для ключей не своего раздела"
    assert set(m.CYCLES) <= set(m.KEYS), f"«{m.ID}»: цикл для ключа не своего раздела"
    for key, (lo, hi, label, _unit) in m.BOUNDS.items():
        assert lo <= hi and label, (m.ID, key)


def test_keys_belong_to_exactly_one_section():
    """section_of однозначен: ключевое действие агента (tgl/edit/cyc) находит
    раздел по KEYS — ключ в двух разделах открыл бы ввод не там."""
    owners: dict[str, list[str]] = {}
    for m in sections.MODULES:
        for key in m.KEYS:
            owners.setdefault(key, []).append(m.ID)
    shared = {k: v for k, v in owners.items() if len(v) > 1}
    assert not shared, f"ключи в нескольких разделах: {shared}"
    for key, (sec,) in owners.items():
        assert sections.section_of(key) == sec, key
    assert sections.section_of("no.such.key") == ""


# ── кнопки модулей ───────────────────────────────────────────────────────────

# образцы аргументов построителей по имени параметра; новый параметр без
# образца — красный тест, а не молча пропущенный построитель
SAMPLES = {"encryption": (False, True), "configured": (False, True), "has_secret": (False, True),
           "muted": (False, True), "target_tag": ("", "v9.9.9"), "blocked": ("", "переезд"),
           "migration": ("", "running"), "available": (False, True), "orphans": (0, 2)}


def _builders(m):
    import inspect
    for name, fn in inspect.getmembers(m, inspect.isfunction):
        if fn.__module__ == m.__name__ and (name == "keyboard" or name.endswith("_kb")):
            yield name, fn


def _variants(fn, br, m):
    import inspect
    params = list(inspect.signature(fn).parameters)[1:]          # первый — br
    choices = []
    for p in params:
        if p == "back_sec":
            choices.append([s for s in (*br.settings_root, *br.subsections) if sections.available(br, s)])
            continue
        assert p in SAMPLES, f"{m.ID}.{fn.__name__}: нет образца для параметра «{p}»"
        choices.append(SAMPLES[p])
    for combo in itertools.product(*choices):
        yield fn(br, *combo)


def _buttons(br, monkeypatch):
    """(модуль, построитель, подпись, колбэк) — все кнопки всех построителей
    доступных роли модулей, при включённых и выключенных тумблерах."""
    out = []
    for flag in (True, False):
        monkeypatch.setattr(settings, "get_bool", lambda k, d=False, flag=flag: flag)
        for m in sections.MODULES:
            if not sections.available(br, m.ID):
                continue
            for name, fn in _builders(m):
                for markup in _variants(fn, br, m):
                    assert isinstance(markup, InlineKeyboardMarkup), (m.ID, name)
                    for row in markup.inline_keyboard:
                        for b in row:
                            if b.callback_data:
                                out.append((m, name, b.text, b.callback_data))
    return out


@pytest.mark.parametrize("br", BRS, ids=lambda b: b.name)
def test_every_section_button_leads_somewhere_real(br, monkeypatch):
    """Каждая кнопка общего раздела разбирается словарём роли и ведёт к
    существующему: open — в доступный раздел (или ролевой), toggle/edit/cycle
    — в свой раздел (у агента — по KEYS), edit — к ключу с границами или к
    текстовому ключу, cycle — к ряду значений или к своему обработчику
    модуля, do — к действию модуля (ключ с «!» — к подтверждению)."""
    checked = {"open": 0, "toggle": 0, "edit": 0, "cycle": 0, "do": 0}
    for m, name, label, data in _buttons(br, monkeypatch):
        where = f"{br.name}: {m.ID}.{name} «{label}» ({data})"
        packed = br.cb.parse(data)
        if packed is None:
            continue                              # не колбэк разделов (меню, обновление)
        got = sections.resolve(br, data)
        sec, act, key, _val = packed
        if got is None:
            assert act == "open" and sec in br.root_labels or sec not in sections.SECTIONS and act == "do", \
                f"{where}: колбэк раздела, который роли недоступен"
            continue                              # ролевой раздел / действие роли (переезд)
        rsec, act, key, _val = got
        checked[act] = checked.get(act, 0) + 1
        target = sections.SECTIONS.get(rsec)
        assert target is not None, f"{where}: раздел не найден — ответ «Кнопка устарела»"
        if act in ("toggle", "edit", "cycle"):
            assert rsec == m.ID, f"{where}: ключевое действие ушло в раздел «{rsec}»"
        if act == "edit":
            assert sections.bounds(key) or key in texts.SETTINGS_TEXT or key == "backup_when", \
                f"{where}: ключ правки без границ"
        elif act == "cycle":
            assert sections.cycle_values(key) is not None or hasattr(target, "cycle"), \
                f"{where}: цикл без ряда значений"
        elif act == "do":
            base = target.ACTIONS.get(key[:-1]) if key.endswith("!") else None
            assert key in target.ACTIONS or isinstance(base, Confirm), f"{where}: действия нет"
    assert all(checked[a] for a in ("open", "toggle", "edit", "do")), f"проверять оказалось нечего: {checked}"


@pytest.mark.parametrize("br", BRS, ids=lambda b: b.name)
def test_the_root_lists_exactly_the_role_sections(br):
    """Корень рисуется из словаря: по кнопке на ID корня и «⬅️ В меню»."""
    markup = sections.root.keyboard(br)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == [sections.label(br, s) for s in br.settings_root] + ["⬅️ В меню"], labels
