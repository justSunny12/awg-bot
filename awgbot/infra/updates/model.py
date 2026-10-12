"""model.py — часть пакета updates (см. __init__): релиз, разбор версий и
хэштегов тела релиза."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# vMAJOR.MINOR.PATCH (ведущее «v» необязательно). Пре-релизные суффиксы не
# поддерживаем сознательно — релизы строго числовые.
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


class UpdateError(Exception):
    """Сетевая/протокольная ошибка обновления (гасится вызывающим)."""


# Адресаты релиза — хэштегами в теле, по строке на роль: «#requires_main_2.10.0»
# (основной бот), «#requires_gw_2.10.0» (агент шлюза); версия в нём — минимум,
# с которого на релиз прыгают напрямую. Прежний формат (#main_bot / #gw_bot /
# #all_bots, без минимума) читается ради старых релизов; релиз без хэштегов
# вовсе считается общим. У ролей разные установленные версии — это норма,
# поставка едет целиком.
_REQUIRES_RE = re.compile(r"#requires_(main|gw)_(v?\d+\.\d+\.\d+(?:\.\d+)?)\b")
_AUDIENCE_RE = re.compile(r"#(main_bot|gw_bot|all_bots)\b")
_ROLE_KEY = {"gateway": "gw"}              # любая другая роль — основной бот
_ROLE_TAG = {"gateway": "gw_bot"}

# Поколение AmneziaWG в поставке — тоже хэштегом в теле: «#awg_gen2». Его надо
# знать ДО скачивания (обновление на поколение дальше цели идущего переезда
# запрещено), а в ассете оно лежит внутри архива. Нет хэштега — поколение то
# же, что у установленного: до v2.11.0 поколениями никто не управлял.
_GENERATION_RE = re.compile(r"#awg_gen(\d+)\b")

# Условие на версию ДРУГОЙ роли — тоже хэштегом в теле, читается слева
# направо: «#gw_needs_main_3.3.0» — агенту этого релиза нужен сервер AWG не
# ниже 3.3.0 (читает только агент); «#main_needs_gw_3.3.0» — основному боту
# нужны шлюзы не ниже 3.3.0 (читает только основной бот). Версия — три цифры;
# с четвёртой (хотфикс) строка не совпадает вовсе. Строка чужой роли не значит
# ничего: агенту всё равно, что серверу нужно от шлюзов.
_NEEDS_RE = re.compile(r"#(gw_needs_main|main_needs_gw)_(v?\d+\.\d+\.\d+)(?![.\d])")
_NEEDS_KEY = {"gateway": "gw_needs_main"}  # любая другая роль — основной бот


@dataclass(frozen=True)
class Release:
    tag: str                    # как в GitHub, напр. "v1.2.0"
    version: tuple              # (1, 2, 0) — для сравнения
    body: str                   # тело релиза (changelog этой версии, без заголовка)
    asset_url: Optional[str]    # API-URL ассета-поставки (для octet-stream)
    sha256: Optional[str]       # эталонный sha256 из assets[].digest (hex)
    title: str = ""             # заголовок релиза без тега

    def awg_generation(self) -> int:
        """Поколение AmneziaWG этой поставки; 0 — не объявлено."""
        m = _GENERATION_RE.search(self.body or "")
        return int(m.group(1)) if m else 0

    def requirements(self) -> dict:
        """{'main': (2,10,0), 'gw': (2,10,0)} — роли, которым релиз адресован,
        с минимумом для прямого прыжка. Пусто — хэштегов нового формата нет."""
        return {k: parse_version(v) for k, v in _REQUIRES_RE.findall(self.body or "")}

    def requires(self, role: str = "") -> Optional[tuple]:
        """Минимальная версия, с которой ЭТА роль прыгает на релиз напрямую;
        None — ограничения нет (в т.ч. релизы прежнего формата)."""
        return self.requirements().get(_ROLE_KEY.get(role, "main"))

    def needs(self, role: str = "") -> Optional[tuple]:
        """Версия ДРУГОЙ стороны, нужная этой роли для релиза: агенту — сервера
        AWG (#gw_needs_main_X), основному боту — шлюзов (#main_needs_gw_X).
        None — условия нет. Хотфикс X.Y.Z.P условий не несёт: строки в его
        теле не читаются (их и не ставят — docs/RELEASING.md §2)."""
        if len(self.version) > 3:
            return None
        want = _NEEDS_KEY.get(role, "main_needs_gw")
        found = [parse_version(v) for k, v in _NEEDS_RE.findall(self.body or "") if k == want]
        return max(found) if found else None

    def audience(self) -> frozenset:
        """Адресаты в терминах прежних хэштегов (main_bot / gw_bot / all_bots);
        пусто — релиз общий."""
        req = self.requirements()
        if req:
            return frozenset({"main_bot" if k == "main" else "gw_bot" for k in req})
        return frozenset(_AUDIENCE_RE.findall(self.body or ""))

    def applies_to(self, role: str) -> bool:
        tags = self.audience()
        if not tags:
            return True
        return "all_bots" in tags or _ROLE_TAG.get(role, "main_bot") in tags


def version_at_least(ver: str, floor: tuple[int, ...]) -> bool:
    """«3.1.0» ≥ (3, 1, 0)? Нечисловое — False (агент не назвал версию)."""
    parts = parse_version(str(ver or ""))
    return bool(parts) and tuple(parts[:len(floor)]) >= floor


def parse_version(tag: str) -> Optional[tuple]:
    """'v1.2.0'|'1.2.0' → (1,2,0); 'v1.2.0.3' → (1,2,0,3); иначе None.

    Четвёртая цифра — хотфикс поверх выпущенной версии. Кортежи разной длины
    Python сравнивает как надо: (1,2,0) < (1,2,0,3) < (1,2,1), — дополнять
    нулём нельзя, иначе 1.2.0 и 1.2.0.0 стали бы одним и тем же, и бот на
    хотфиксе считал бы себя базовой версией.
    """
    m = _SEMVER_RE.match(tag.strip())
    if not m:
        return None
    head = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return head + (int(m.group(4)),) if m.group(4) else head
