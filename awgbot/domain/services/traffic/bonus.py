"""bonus.py — бонус трафика при исчерпании: разбор ввода (N | N% | N% M | 0), расчёт от лимита профиля, подписи."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from awgbot.domain.services.types import BYTES_PER_GB

SETTING = "limits.traffic_bonus_gb"     # имя ключа — прежнее: «100» в conf читается как раньше
DEFAULT = 100
MAX_GB = 100000

_RE = re.compile(r"^\s*(\d{1,6})\s*(%)?(?:\s+(\d{1,6}))?\s*$")


@dataclass(frozen=True)
class BonusSpec:
    """Что выдавать при исчерпании. kind: off — бонуса нет; gb — ровно gb ГБ;
    pct — pct % лимита вверх до целых ГБ, не более половины лимита; pct_cap —
    pct % лимита вверх до целых ГБ, не более cap ГБ."""
    kind: str
    gb: int = 0
    pct: int = 0
    cap: int = 0

    def stored(self):
        """Значение для conf: число для «ГБ» и выкл., строка для процентов."""
        if self.kind == "off":
            return 0
        if self.kind == "gb":
            return self.gb
        if self.kind == "pct":
            return f"{self.pct}%"
        return f"{self.pct}% {self.cap}"


def parse(raw) -> BonusSpec:
    """Ввод или значение conf → BonusSpec. «50» — 50 ГБ, «10%» — процент,
    «10% 50» — процент не более 50 ГБ, «0» — выкл. Иное — ValueError."""
    m = _RE.match(str(raw if raw is not None else ""))
    if not m:
        raise ValueError(raw)
    n, pct, cap = int(m.group(1)), bool(m.group(2)), m.group(3)
    if not pct:
        if cap is not None or not 0 <= n <= MAX_GB:
            raise ValueError(raw)
        return BonusSpec("off") if n == 0 else BonusSpec("gb", gb=n)
    if not 1 <= n <= 100:
        raise ValueError(raw)
    if cap is None:
        return BonusSpec("pct", pct=n)
    c = int(cap)
    if not 1 <= c <= MAX_GB:
        raise ValueError(raw)
    return BonusSpec("pct_cap", pct=n, cap=c)


def current(settings) -> BonusSpec:
    """Из conf; испорченное значение — умолчание (100 ГБ), как прежде."""
    raw = settings.get(SETTING, DEFAULT)
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)                                    # «50.0», вписанное руками, — это 50
    try:
        return parse(raw)
    except ValueError:
        return BonusSpec("gb", gb=DEFAULT)


def bytes_for(spec: BonusSpec, limit_bytes: int) -> int:
    """Сколько байт выдать профилю с месячным лимитом limit_bytes. Лимита нет
    или бонус выключен — 0. Проценты — вверх до целых ГБ; «N%» — не более
    половины лимита, «N% M» — не более M ГБ."""
    if spec.kind == "off" or limit_bytes <= 0:
        return 0
    if spec.kind == "gb":
        return spec.gb * BYTES_PER_GB
    raw = limit_bytes * spec.pct / 100
    up = math.ceil(raw / BYTES_PER_GB) * BYTES_PER_GB
    if spec.kind == "pct":
        return int(min(up, limit_bytes // 2))
    return int(min(up, spec.cap * BYTES_PER_GB))


def gb_text(num_bytes: int) -> str:
    """«10 ГБ», «0,5 ГБ», «0,25 ГБ» — до сотых, без хвостовых нулей."""
    gb = round(num_bytes / BYTES_PER_GB, 2)
    s = f"{gb:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",") + " ГБ"


def screen_text(spec: BonusSpec) -> str:
    """Строка экрана «💳 Подписки»: «выкл.» / «50 ГБ» / «10% лимита» / «10% лимита, не более 50 ГБ»."""
    if spec.kind == "off":
        return "выкл."
    if spec.kind == "gb":
        return f"{spec.gb} ГБ"
    if spec.kind == "pct":
        return f"{spec.pct}% лимита"
    return f"{spec.pct}% лимита, не более {spec.cap} ГБ"


def button_text(spec: BonusSpec) -> str:
    """Подпись кнопки: «выкл.» / «50 ГБ» / «10%» / «10%, ≤ 50 ГБ»."""
    if spec.kind == "off":
        return "выкл."
    if spec.kind == "gb":
        return f"{spec.gb} ГБ"
    if spec.kind == "pct":
        return f"{spec.pct}%"
    return f"{spec.pct}%, ≤ {spec.cap} ГБ"


def admin_note(spec: BonusSpec, given: int, limit_bytes: int) -> str:
    """Скобка в уведомлении админу: у процентов — откуда число («10% лимита»);
    упёрлись в потолок — и он: у «N%» — «10% лимита, не более половины», у
    «N% M» — «10% лимита, не более 50 ГБ»; у «ГБ» — пусто."""
    if spec.kind == "pct":
        # упёрлись в половину лимита (маленький лимит, округление вверх) — так и сказать
        halved = given >= limit_bytes // 2 and math.ceil(limit_bytes * spec.pct / 100 / BYTES_PER_GB) * BYTES_PER_GB > given
        return f"{spec.pct}% лимита, не более половины" if halved else f"{spec.pct}% лимита"
    if spec.kind == "pct_cap":
        capped = given >= spec.cap * BYTES_PER_GB and limit_bytes * spec.pct / 100 > given
        return f"{spec.pct}% лимита, не более {spec.cap} ГБ" if capped else f"{spec.pct}% лимита"
    return ""
