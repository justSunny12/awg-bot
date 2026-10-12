"""model.py — часть пакета updates (см. __init__): релиз, разбор версий;
условия релиза — из метаданных (meta.py), запасным путём из хэштегов тела."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import meta as _meta

# vMAJOR.MINOR.PATCH (ведущее «v» необязательно). Пре-релизные суффиксы не
# поддерживаем сознательно — релизы строго числовые.
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


class UpdateError(Exception):
    """Сетевая/протокольная ошибка обновления (гасится вызывающим)."""


# Запасной путь — хэштеги тела, как их пишут релизы до метаданных и читают
# боты 3.2.x (meta.py, «переход»). Адресаты по строке на роль:
# «#requires_main_2.10.0» (основной бот), «#requires_gw_2.10.0» (агент
# шлюза); версия в нём — минимум, с которого на релиз прыгают напрямую.
# Прежний формат (#main_bot / #gw_bot / #all_bots, без минимума) читается ради
# старых релизов; релиз без хэштегов вовсе считается общим.
_REQUIRES_RE = re.compile(r"#requires_(main|gw)_(v?\d+\.\d+\.\d+(?:\.\d+)?)\b")
_AUDIENCE_RE = re.compile(r"#(main_bot|gw_bot|all_bots)\b")
_ROLE_KEY = {"gateway": "gw"}              # любая другая роль — основной бот
_ROLE_TAG = {"gateway": "gw_bot"}

# Поколение AmneziaWG в поставке — «#awg_gen2» в теле или `awg_gen` в
# метаданных. Его надо знать ДО скачивания (обновление на поколение дальше
# цели идущего переезда запрещено), а в архиве оно лежит внутри. Не объявлено
# — поколение то же, что у установленного: до v2.11.0 поколениями никто не
# управлял.
_GENERATION_RE = re.compile(r"#awg_gen(\d+)\b")


@dataclass(frozen=True)
class Release:
    tag: str                    # как в GitHub, напр. "v1.2.0"
    version: tuple              # (1, 2, 0) — для сравнения
    body: str                   # тело релиза (changelog этой версии, без заголовка)
    asset_url: Optional[str]    # API-URL ассета-поставки (для octet-stream)
    sha256: Optional[str]       # эталонный sha256 из assets[].digest (hex)
    title: str = ""             # заголовок релиза без тега
    meta: Optional[dict] = None  # проверенные метаданные (meta.py) или None
    meta_bad: str = ""          # ассет метаданных есть, но не прочитан — причина

    def _by_body(self) -> bool:
        """Читать ли условия из тела: метаданных нет, ассет не битый и релиз
        моложе ступени «только метаданные»."""
        return self.meta is None and not self.meta_bad and self.version[:3] < _meta.META_ONLY_FROM

    def usable(self) -> bool:
        """Рассматривается ли релиз вовсе — целью или звеном цепочки минимумов.
        Нет — его минимум не учёлся бы, и хост перепрыгнул бы ступень."""
        return self.meta is not None or self._by_body()

    def skip_reason(self) -> str:
        """Почему релиз не рассматривается ("" — рассматривается)."""
        if self.usable():
            return ""
        return self.meta_bad or "нет метаданных релиза"

    def awg_generation(self) -> int:
        """Поколение AmneziaWG этой поставки; 0 — не объявлено."""
        if self.meta is not None:
            return int(self.meta["awg_gen"])
        m = _GENERATION_RE.search(self.body or "") if self._by_body() else None
        return int(m.group(1)) if m else 0

    def requirements(self) -> dict:
        """{'main': (2,10,0), 'gw': (2,10,0)} — роли, которым релиз адресован,
        с минимумом для прямого прыжка. Пусто — адресатов не названо."""
        if self.meta is not None:
            return _meta.requires(self.meta)
        if not self._by_body():
            return {}
        return {k: parse_version(v) for k, v in _REQUIRES_RE.findall(self.body or "")}

    def requires(self, role: str = "") -> Optional[tuple]:
        """Минимальная версия, с которой ЭТА роль прыгает на релиз напрямую;
        None — ограничения нет (в т.ч. релизы прежнего формата)."""
        return self.requirements().get(_ROLE_KEY.get(role, "main"))

    def needs(self, role: str = "") -> Optional[tuple]:
        """Версия ДРУГОЙ стороны, нужная этой роли для релиза: агенту — сервера
        AWG (`roles.gw.needs_main`), основному боту — шлюзов
        (`roles.main.needs_gw`). Только из метаданных; None — условия нет.
        Хотфикс X.Y.Z.P условий не несёт."""
        if self.meta is None or len(self.version) > 3:
            return None
        return _meta.needs(self.meta, _ROLE_KEY.get(role, "main"))

    def audience(self) -> frozenset:
        """Адресаты в терминах прежних хэштегов (main_bot / gw_bot / all_bots);
        пусто — релиз общий (только у тела без хэштегов)."""
        req = self.requirements()
        if req or self.meta is not None:
            return frozenset({"main_bot" if k == "main" else "gw_bot" for k in req})
        if not self._by_body():
            return frozenset()
        return frozenset(_AUDIENCE_RE.findall(self.body or ""))

    def applies_to(self, role: str) -> bool:
        if not self.usable():
            return False
        tags = self.audience()
        if not tags and self.meta is None:
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
