"""docs/CHANGELOG.md — источник тел релизов: первые строки записи читают боты.
Запись без строки роли никому не уедет; запись с обеими строками уедет обеим
ролям — и агент шлюза обновится ради кода, которого у него нет."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_HEAD = re.compile(r"^## (v\d+\.\d+\.\d+(?:\.\d+)?) — .*$", re.M)


def _entries():
    text = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = list(_HEAD.finditer(text))
    for i, m in enumerate(heads):
        body = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        lines = body.strip().splitlines()
        # хэштеги — первые строки записи, до первой пустой
        tags = []
        for ln in lines:
            if not ln.strip():
                break
            tags.append(ln.strip())
        yield m.group(1), tags


def test_every_entry_since_the_jump_model_names_its_audience_with_a_floor():
    for tag, lines in _entries():
        ver = tuple(int(x) for x in tag[1:].split("."))
        if ver < (2, 18, 0):
            continue
        tags = [ln for ln in lines if ln.startswith("#")]
        roles = [ln for ln in tags if re.fullmatch(r"#requires_(main|gw)_\d+\.\d+\.\d+(\.\d+)?", ln)]
        assert roles, f"{tag}: нет строки роли — релиз никому не уедет"
        assert len(roles) == len(set(roles)), f"{tag}: роль названа дважды"
        assert any(ln.startswith("#awg_gen") for ln in tags), f"{tag}: нет поколения ядра"
        assert not any(ln in ("#main_bot", "#gw_bot", "#all_bots") for ln in tags), \
            f"{tag}: прежний формат адресата"


def test_main_only_releases_are_not_addressed_to_the_gateway():
    """Резолвер клиентов и его хотфиксы — код основного бота: у агента шлюза
    клиентов нет. Обе строки на них отправили агента обновляться впустую."""
    for tag, lines in _entries():
        if tag in ("v2.19.0", "v2.19.0.2", "v2.19.0.3"):
            assert not any(ln.startswith("#requires_gw_") for ln in lines), tag


def _audience_by_version() -> dict[str, set[str]]:
    """Версия → роли, которым релиз был адресован (оба формата хэштегов)."""
    out: dict[str, set[str]] = {}
    for tag, lines in _entries():
        roles: set[str] = set()
        for ln in lines:
            if ln == "#all_bots":
                roles |= {"main", "gw"}
            elif ln == "#main_bot":
                roles.add("main")
            elif ln == "#gw_bot":
                roles.add("gw")
            m = re.fullmatch(r"#requires_(main|gw)_.*", ln)
            if m:
                roles.add(m.group(1))
        if not any(ln.startswith("#") for ln in lines):
            roles = {"main", "gw"}            # релизы до хэштегов — общие
        out[tag[1:]] = roles
    return out


def test_required_floor_is_a_release_that_role_actually_received():
    """Не каждый патч едет в каждую роль: минимум в `#requires_gw_X` обязан
    быть релизом, адресованным шлюзу, иначе шлюз никогда на нём не стоял и
    «ступень» ведёт в версию, которой у роли не было."""
    audience = _audience_by_version()
    for tag, lines in _entries():
        for ln in lines:
            m = re.fullmatch(r"#requires_(main|gw)_v?(\d+\.\d+\.\d+(?:\.\d+)?)", ln)
            if not m:
                continue
            role, floor = m.groups()
            assert floor in audience, f"{tag}: минимум {floor} — нет такого релиза в журнале"
            assert role in audience[floor], \
                f"{tag}: минимум {floor} для роли {role} — тот релиз роли не адресовался"


# ── перекрёстные строки: версия другой роли ──────────────────────────────────
# «#gw_needs_main_X» — агенту релиза нужен сервер AWG не ниже X; «#main_needs_gw_X»
# — основному боту нужны шлюзы не ниже X. Читаются слева направо; X — релиз,
# который той роли был адресован. Хотфиксы X.Y.Z.P их не несут.

_CROSS = re.compile(r"#(gw_needs_main|main_needs_gw)_(.*)")
_CROSS_ROLE = {"gw_needs_main": ("gw", "main"), "main_needs_gw": ("main", "gw")}


def _cross_problems(tag: str, lines: list[str], audience: dict[str, set[str]]) -> list[str]:
    """Нарушения перекрёстных строк одной записи (пусто — запись в порядке)."""
    out = []
    tags = [ln for ln in lines if ln.startswith("#")]
    for ln in tags:
        if re.match(r"#needs_(main|gw)_", ln):
            out.append(f"{tag}: «{ln}» — черновое имя, нужно #gw_needs_main_ / #main_needs_gw_")
    cross = [(i, ln) for i, ln in enumerate(tags) if _CROSS.fullmatch(ln)]
    if not cross:
        return out
    if len(tag[1:].split(".")) > 3:
        out.append(f"{tag}: у хотфикса строк условий не бывает")
    req = [i for i, ln in enumerate(tags) if ln.startswith("#requires_")]
    gen = [i for i, ln in enumerate(tags) if ln.startswith("#awg_gen")]
    roles = {m.group(1) for ln in tags if (m := re.fullmatch(r"#requires_(main|gw)_.*", ln))}
    for i, ln in cross:
        kind, ver = _CROSS.fullmatch(ln).groups()
        own, other = _CROSS_ROLE[kind]
        if req and i < max(req) or gen and i > min(gen):
            out.append(f"{tag}: «{ln}» — после #requires_*, перед #awg_gen")
        if own not in roles:
            out.append(f"{tag}: «{ln}» без #requires_{own}_ — роль его не прочтёт")
        if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
            out.append(f"{tag}: «{ln}» — версия в три цифры")
        elif other not in audience.get(ver, set()):
            out.append(f"{tag}: «{ln}» — релиз {ver} роли {other} не адресовался")
    return out


def test_cross_role_lines_in_the_changelog_are_well_formed():
    audience = _audience_by_version()
    for tag, lines in _entries():
        assert not _cross_problems(tag, lines, audience), _cross_problems(tag, lines, audience)


def test_the_cross_role_checker_catches_every_mistake():
    """Строк условий в журнале пока нет (первым их несёт 3.3.1) — проверка
    проверяется на образцах, чтобы первая запись с ними не прошла кривой."""
    aud = {"3.3.0": {"main", "gw"}, "3.3.1": {"main"}, "3.2.1": {"gw"}}
    good = ["#requires_main_3.3.0", "#requires_gw_3.3.0",
            "#gw_needs_main_3.3.0", "#main_needs_gw_3.3.0", "#awg_gen2"]
    assert _cross_problems("v3.3.2", good, aud) == []
    cases = {
        "хотфикс": ("v3.3.2.1", ["#requires_gw_3.3.0", "#gw_needs_main_3.3.0", "#awg_gen2"]),
        "до #requires": ("v3.3.2", ["#gw_needs_main_3.3.0", "#requires_gw_3.3.0", "#awg_gen2"]),
        "после #awg_gen": ("v3.3.2", ["#requires_gw_3.3.0", "#awg_gen2", "#gw_needs_main_3.3.0"]),
        "чужая роль": ("v3.3.2", ["#requires_main_3.3.0", "#gw_needs_main_3.3.0", "#awg_gen2"]),
        "черновое имя": ("v3.3.2", ["#requires_gw_3.3.0", "#needs_main_3.3.0", "#awg_gen2"]),
        "четыре цифры": ("v3.3.2", ["#requires_gw_3.3.0", "#gw_needs_main_3.3.0.1", "#awg_gen2"]),
        "версия не той роли": ("v3.3.2", ["#requires_main_3.3.0", "#main_needs_gw_3.3.1", "#awg_gen2"]),
        "нет такого релиза": ("v3.3.2", ["#requires_gw_3.3.0", "#gw_needs_main_3.2.9", "#awg_gen2"]),
    }
    for why, (tag, lines) in cases.items():
        assert _cross_problems(tag, lines, aud), f"не поймано: {why}"
