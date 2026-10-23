"""Базовые форматтеры: экранирование, ссылки на экраны, дерево вложенных строк, объёмы, устройства, ссылки на людей, склонения.

Сами помощники — в util/textfmt (чистые, без слоя бота: ими пользуются и
тексты уведомлений домена); здесь — ре-экспорт под прежними именами и то, что
тянет тексты бота (rf_line — имя функции из texts/routing)."""

from __future__ import annotations

from awgbot.util.textfmt.base import (  # noqa: F401
    deep_link, refreshed_link, profile_link, admin_device_link, _SUB, tree, sub_line, access_status_line, _e,
    _num, gb, _BYTES_PER_TB, unit_for, amount, volume, human_bytes, used_of_limit, _updown,
    rf_value, updown_brief, _BYTES_PER_GB, gb_str, _limit_devices_str, device_state, details,
    plain_ip, tg_link, client_link, owner_name, holder_name, owner_link, holder_link,
    _n_devices, num, more, unlimited, plural_ru, _days_word, _days)


def _gb_size(gb: float) -> str:
    """Объём: ГБ — целыми, ТБ — с двумя знаками (1024 ГБ = 1 ТБ)."""
    if gb >= 1024:
        return f"{gb / 1024:.2f}".replace(".", ",") + " ТБ"
    return f"{round(gb)} ГБ"


def disk_pair(used_gb: float, total_gb: float) -> str:
    """«10/200 ГБ», «100 ГБ/1,68 ТБ», «1,20/1,68 ТБ» — занято/всего."""
    if total_gb < 1024:
        return f"{round(used_gb)}/{round(total_gb)} ГБ"
    if used_gb < 1024:
        return f"{round(used_gb)} ГБ/{_gb_size(total_gb)}"
    return f"{used_gb / 1024:.2f}".replace(".", ",") + f"/{_gb_size(total_gb)}"


def disk_lines(disks) -> list[str]:
    """Строки носителей для панели: один — «диск 10/200 ГБ»; несколько — по два в
    строку, с номерами: «диск 1 - 10/200 ГБ · диск 2 - 100 ГБ/1,68 ТБ». disks —
    [[занято ГБ, всего ГБ, точка], …] или [{used_gb, total_gb}, …]."""
    pairs = []
    for d in disks or []:
        used, total = (d.get("used_gb"), d.get("total_gb")) if isinstance(d, dict) else (d[0], d[1])
        if used is None or total is None:
            continue
        pairs.append(disk_pair(float(used), float(total)))
    if not pairs:
        return []
    if len(pairs) == 1:
        return [f"диск {pairs[0]}"]
    items = [f"диск {i} - {p}" for i, p in enumerate(pairs, 1)]
    return [" · ".join(items[i:i + 2]) for i in range(0, len(items), 2)]


def rf_line(rx: int, tx: int, label: str = "", *, arrows: bool = True) -> str:
    """Вложенная строка РФ-части под строкой трафика (карточки, главная):
    «└ 🇷🇺 РФ-доступ: 7.1 ГБ (↑0.7 ↓6.4)». label — готовая подпись вместо
    «🇷🇺 РФ-доступ» (ссылкой на экран трафика; флаг — часть ссылки)."""
    from .routing import ROUTING_NAME
    return sub_line(f"{label or f'🇷🇺 {ROUTING_NAME}'}: {rf_value(rx, tx, arrows=arrows)}")


__all__ = [
    "deep_link", "refreshed_link", "profile_link", "admin_device_link", "_SUB", "tree", "sub_line",
    "access_status_line", "_e", "_num", "gb", "_BYTES_PER_TB", "unit_for", "amount", "volume",
    "human_bytes", "used_of_limit", "_updown", "rf_value", "updown_brief", "_BYTES_PER_GB",
    "gb_str", "_limit_devices_str", "device_state", "details", "plain_ip", "tg_link",
    "client_link", "owner_name", "holder_name", "owner_link", "holder_link", "_n_devices", "num",
    "more", "unlimited", "plural_ru", "_days_word", "_days", "rf_line"]
