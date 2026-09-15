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
