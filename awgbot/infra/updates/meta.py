"""meta.py — часть пакета updates (см. __init__): метаданные релиза —
JSON-ассет `awg-bot-release.json` рядом с поставкой.

Условия обновления (адресаты, минимумы, поколение ядра, версия другой роли)
читаются из него, а не из тела журнала: тело — для людей. Ассет лежит вне
архива — его читают ДО скачивания поставки. Целостность — как у поставки:
`assets[].digest` от GitHub.

Схема 1:
    {"schema": 1, "version": "3.3.1", "awg_gen": 1,
     "roles": {"main": {"requires": "3.3.0", "needs_gw": "3.3.0"},
               "gw":   {"requires": "3.3.0", "needs_main": "3.3.0"}}}
Есть ключ роли — релиз ей адресован; `requires` обязателен (минимум для
прямого прыжка); `needs_gw` — только у main (серверу нужны шлюзы не ниже),
`needs_main` — только у gw (агенту нужен сервер не ниже); у хотфикса
X.Y.Z.P условий на другую роль нет. Неизвестные ключи и роли пропускаются
(нужна хотя бы одна известная; опечатки ловят инструмент выпуска и сторож
журнала): новые поля добавляются с тем же номером схемы, смена номера —
только через релиз, который читает оба.

Переход: релизы моложе META_ONLY_FROM без ассета читаются по телу (хэштеги
`#requires_<роль>_X`, `#awg_genN` — их же читают боты 3.2.x); с
META_ONLY_FROM нет ассета — релиз не рассматривается. Битый ассет — не
рассматривается никогда: это ошибка выпуска или подмена, телу не верим.
"""
from __future__ import annotations

import json
import re

SCHEMA = 1
META_ONLY_FROM = (3, 3, 2)          # с этой версии — только метаданные, тело не читается
META_MAX = 16 * 1024                # байт: больше — не метаданные
ROLES = ("main", "gw")
_NEEDS_FIELD = {"main": "needs_gw", "gw": "needs_main"}

_VER3 = re.compile(r"^\d+\.\d+\.\d+$")
_VER34 = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?$")


class MetaError(ValueError):
    """Метаданные не прошли проверку — релиз не рассматривается."""


def _ver(raw) -> tuple:
    return tuple(int(x) for x in str(raw).split("."))


def check(meta, tag: str) -> dict:
    """Проверить разобранные метаданные релиза tag; вернуть их же. Ошибка —
    MetaError с причиной словами (для журнала и экрана)."""
    if not isinstance(meta, dict):
        raise MetaError("не объект JSON")
    if meta.get("schema") != SCHEMA:
        raise MetaError(f"неизвестная схема {meta.get('schema')!r}")
    version = meta.get("version")
    if not isinstance(version, str) or not _VER34.match(version):
        raise MetaError("нет версии")
    if version != str(tag).lstrip("v"):
        raise MetaError(f"версия {version} не совпадает с тегом {tag}")
    gen = meta.get("awg_gen")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        raise MetaError("нет поколения ядра")
    roles = meta.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise MetaError("нет адресатов")
    hotfix = len(version.split(".")) > 3
    known = [r for r in roles if r in ROLES]
    if not known:
        raise MetaError("нет ни одной известной роли")
    for role in known:                    # неизвестные роли — пропуск: вперёд-совместимость
        data = roles[role]
        if not isinstance(data, dict):
            raise MetaError(f"роль {role}: не объект")
        req = data.get("requires")
        if not isinstance(req, str) or not _VER34.match(req):
            raise MetaError(f"роль {role}: нет минимума")
        for field, owner in (("needs_gw", "main"), ("needs_main", "gw")):
            if field not in data:
                continue
            if role != owner:
                raise MetaError(f"роль {role}: {field} ставится только роли {owner}")
            if hotfix:
                raise MetaError(f"хотфикс не несёт {field}")
            if not isinstance(data[field], str) or not _VER3.match(data[field]):
                raise MetaError(f"роль {role}: {field} — версия в три цифры")
    return meta


def parse(raw: bytes, tag: str) -> dict:
    """Байты ассета → проверенные метаданные релиза tag (MetaError — нет)."""
    if len(raw) > META_MAX:
        raise MetaError(f"больше {META_MAX // 1024} КБ")
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise MetaError(f"не JSON: {e}")
    return check(meta, tag)


def requires(meta: dict) -> dict:
    """{'main': (3,3,0), …} — адресаты и их минимумы."""
    return {r: _ver(d["requires"]) for r, d in meta["roles"].items() if r in ROLES}


def needs(meta: dict, role_key: str):
    """Версия другой стороны, нужная роли role_key (main|gw), или None."""
    data = meta["roles"].get(role_key) or {}
    raw = data.get(_NEEDS_FIELD[role_key])
    return _ver(raw) if raw and len(meta["version"].split(".")) == 3 else None


def body_lines(meta: dict) -> list[str]:
    """Машинные строки тела для читателей 3.2.x — только у релизов моложе
    META_ONLY_FROM: `#requires_<роль>_X` и `#awg_genN`. Перекрёстных условий
    в теле нет никогда."""
    if _ver(meta["version"])[:3] >= META_ONLY_FROM:
        return []
    out = [f"#requires_{r}_{meta['roles'][r]['requires']}" for r in ROLES if r in meta["roles"]]
    return out + [f"#awg_gen{meta['awg_gen']}"]
