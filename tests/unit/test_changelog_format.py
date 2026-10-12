"""docs/CHANGELOG.md — источник тел релизов, docs/releases/vX.json — источник
метаданных релиза (awg-bot-release.json). Боты 3.2.x читают первые строки
записи; с 3.3.0 — метаданные. Запись без адресата никому не уедет; с обоими —
уедет обеим ролям, и агент шлюза обновится ради кода, которого у него нет.
Переход: у 3.3.0 и 3.3.1 машинные строки в теле обязательны, стоят только в
первом блоке и совпадают с метаданными; с 3.3.2 тело — только для людей, а
ступень 3.3.x с минимумом 3.3.0 обязана быть у обеих ролей. Перекрёстных
условий в теле нет никогда."""
from __future__ import annotations

import json
import re
from pathlib import Path

from awgbot.infra.updates import meta as release_meta

ROOT = Path(__file__).resolve().parents[2]
RELEASES = ROOT / "docs" / "releases"
META_FROM = (3, 3, 0)             # первая запись с метаданными
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


def _rests() -> dict[str, list[str]]:
    """Тег → строки тела ПОСЛЕ первого блока (машинные строки там бот тоже
    прочёл бы: разбор тела ищет хэштеги по всему тексту)."""
    text = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = list(_HEAD.finditer(text))
    out = {}
    for i, m in enumerate(heads):
        body = text[m.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        _, _, rest = body.partition("\n\n")
        out[m.group(1)] = rest.splitlines()
    return out


def _ver(tag: str) -> tuple:
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def _meta(tag: str):
    """Метаданные записи из docs/releases (None — файла нет)."""
    path = RELEASES / f"{tag}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def test_every_entry_since_the_jump_model_names_its_audience_with_a_floor():
    for tag, lines in _entries():
        ver = _ver(tag)
        if ver < (2, 18, 0) or ver[:3] >= release_meta.META_ONLY_FROM:
            continue                      # с 3.3.2 адресаты — только в метаданных
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
        meta = _meta(tag)
        if meta is not None:
            roles = set(meta.get("roles") or {})
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
        meta = _meta(tag)
        for role, data in ((meta or {}).get("roles") or {}).items():
            floor = data["requires"]
            assert floor in audience and role in audience[floor], \
                f"{tag}: минимум {floor} для роли {role} в метаданных — тот релиз роли не адресовался"





# ── метаданные релиза и переход с хэштегов ───────────────────────────────────

_DRAFT = re.compile(r"#(gw_needs_main|main_needs_gw|needs_main|needs_gw)_")
_MACHINE = re.compile(r"#(requires_(main|gw)_|awg_gen|main_bot|gw_bot|all_bots)")
_OTHER = {"needs_gw": "gw", "needs_main": "main"}


_MACHINE_ANY = re.compile(r"#(requires_(main|gw)_|awg_gen\d|main_bot\b|gw_bot\b|all_bots\b)")


def _meta_problems(tag: str, lines: list[str], meta, audience: dict[str, set[str]],
                   rest=()) -> list[str]:
    """Нарушения одной записи журнала (пусто — запись в порядке). lines —
    первый блок записи, rest — остальное тело."""
    out = [f"{tag}: «{ln}» — условие на другую роль пишется только в метаданных"
           for ln in list(lines) + list(rest) if _DRAFT.search(ln)]
    ver = _ver(tag)
    if ver < META_FROM:
        return out
    out += [f"{tag}: «{ln.strip()}» — машинная строка вне первого блока записи"
            for ln in rest if _MACHINE_ANY.search(ln)]
    if meta is None:
        return out + [f"{tag}: нет docs/releases/{tag}.json"]
    try:
        release_meta.check(meta, tag)
    except release_meta.MetaError as e:
        return out + [f"{tag}: метаданные: {e}"]
    machine = [ln for ln in lines if _MACHINE.match(ln)]
    if ver[:3] >= release_meta.META_ONLY_FROM:
        if machine:
            out.append(f"{tag}: с 3.3.2 машинных строк в теле нет — только метаданные")
    elif machine != release_meta.body_lines(meta):
        out.append(f"{tag}: строки тела {machine} не совпадают с метаданными "
                   f"{release_meta.body_lines(meta)}")
    for role, data in meta["roles"].items():
        for field, other in _OTHER.items():
            need = data.get(field)
            if need and other not in audience.get(need, set()):
                out.append(f"{tag}: {field} {need} — релиз роли {other} с такой версией не адресовался")
    return out


def test_metadata_and_bodies_agree_with_the_transition_rule():
    audience, rests = _audience_by_version(), _rests()
    for tag, lines in _entries():
        problems = _meta_problems(tag, lines, _meta(tag), audience, rests.get(tag, []))
        assert not problems, problems


def _step_problems(entries) -> list[str]:
    """Ступень перехода: если в журнале есть запись с META_ONLY_FROM, то для
    КАЖДОЙ роли есть запись в [3.3.0, META_ONLY_FROM) со строкой тела
    `#requires_<роль>_X`, X ≥ 3.3.0. Иначе хост 3.2.x этой роли увидел бы
    релизы с 3.3.2 «общими без минимума» и перепрыгнул бы 3.3.0."""
    entries = list(entries)
    if not any(_ver(tag)[:3] >= release_meta.META_ONLY_FROM for tag, _ in entries):
        return []
    out = []
    for role in ("main", "gw"):
        held = False
        for tag, lines in entries:
            if not (META_FROM <= _ver(tag)[:3] < release_meta.META_ONLY_FROM):
                continue
            for ln in lines:
                m = re.fullmatch(rf"#requires_{role}_(\d+\.\d+\.\d+(?:\.\d+)?)", ln)
                if m and _ver(m.group(1)) >= META_FROM:
                    held = True
        if not held:
            out.append(f"нет ступени для роли {role}: запись 3.3.x до 3.3.2 с #requires_{role}_3.3.0")
    return out


def test_the_transition_step_holds_32x_readers_of_both_roles():
    assert not _step_problems(_entries()), _step_problems(_entries())


def test_the_step_checker_catches_a_missing_role():
    step = [("v3.3.0", ["#requires_main_3.2.0", "#requires_gw_3.2.0", "#awg_gen1"]),
            ("v3.3.1", ["#requires_main_3.3.0", "#requires_gw_3.3.0", "#awg_gen1"]),
            ("v3.3.2", [])]
    assert _step_problems(step) == []
    assert _step_problems(step[:1]) == [], "до 3.3.2 ступень не нужна"
    no_gw = [step[0], ("v3.3.1", ["#requires_main_3.3.0", "#awg_gen1"]), step[2]]
    assert _step_problems(no_gw) and "gw" in _step_problems(no_gw)[0]
    hotfix = [step[0], ("v3.3.0.1", ["#requires_main_3.3.0", "#requires_gw_3.3.0", "#awg_gen1"]), step[2]]
    assert _step_problems(hotfix) == [], "ступенью может быть и хотфикс 3.3.0.P"


def test_every_metadata_file_is_valid_and_named_by_its_version():
    """docs/releases/vX.json — то, что выпуск загрузит вторым ассетом: схема,
    версия = имя файла. Файл опережает запись журнала (пишется до выпуска)."""
    for path in sorted(RELEASES.glob("v*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        release_meta.check(meta, path.stem)


def test_the_metadata_checker_catches_every_mistake():
    """Записей с метаданными в журнале пока нет — проверка проверяется на
    образцах, чтобы первая запись не прошла кривой."""
    aud = {"3.2.0": {"main", "gw"}, "3.3.0": {"main", "gw"}, "3.3.1": {"main"}}

    def meta(version, roles, gen=1):
        return {"schema": 1, "version": version, "awg_gen": gen, "roles": roles}
    m330 = meta("3.3.0", {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}})
    body330 = ["#requires_main_3.2.0", "#requires_gw_3.2.0", "#awg_gen1"]
    assert _meta_problems("v3.3.0", body330, m330, aud) == []
    m332 = meta("3.3.2", {"main": {"requires": "3.3.0", "needs_gw": "3.3.0"},
                          "gw": {"requires": "3.3.0", "needs_main": "3.3.0"}})
    assert _meta_problems("v3.3.2", [], m332, aud) == []
    cases = {
        "нет файла": ("v3.3.0", body330, None),
        "строки тела у 3.3.0 не те": ("v3.3.0", ["#requires_main_3.2.0", "#awg_gen1"], m330),
        "строки тела у 3.3.2": ("v3.3.2", ["#requires_main_3.3.0", "#awg_gen1"], m332),
        "черновой хэштег в теле": ("v3.3.2", ["#gw_needs_main_3.3.0"], m332),
        "черновое короткое имя": ("v3.2.9", ["#requires_gw_3.2.0", "#needs_main_3.3.0"], None),
        "версия не той роли": ("v3.3.2", [], meta("3.3.2", {"main": {"requires": "3.3.0",
                                                                       "needs_gw": "3.3.1"}})),
        "версия не тег": ("v3.3.2", [], meta("3.3.3", {"main": {"requires": "3.3.0"}})),
        "needs у хотфикса": ("v3.3.2.1", [], meta("3.3.2.1", {"gw": {"requires": "3.3.0",
                                                                       "needs_main": "3.3.0"}})),
    }
    for why, (tag, lines, m) in cases.items():
        assert _meta_problems(tag, lines, m, aud), f"не поймано: {why}"
    assert _meta_problems("v3.3.0", body330, m330, aud, ["- текст", "см. #requires_gw_3.2.0"]), \
        "машинная строка вне первого блока"
    assert _meta_problems("v3.3.2", [], m332, aud, ["- про #gw_needs_main_3.3.0"]), "черновой хэштег в тексте"
    assert _meta_problems("v3.3.0", body330, m330, aud, ["- обычный текст"]) == []
