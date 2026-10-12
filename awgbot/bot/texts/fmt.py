"""Базовые форматтеры: экранирование, ссылки на экраны, дерево вложенных строк, объёмы, устройства, ссылки на людей, склонения.

Сами помощники — в util/textfmt (чистые, без слоя бота: ими пользуются и
тексты уведомлений домена); здесь — ре-экспорт под прежними именами и то, что
тянет тексты бота (rf_line — имя функции из texts/routing)."""

from __future__ import annotations

from awgbot.util.textfmt.base import (  # noqa: F401
    deep_link, profile_link, admin_device_link, _SUB, tree, sub_line, access_status_line, _e,
    _num, gb, _BYTES_PER_TB, unit_for, amount, volume, human_bytes, used_of_limit, _updown,
    rf_value, updown_brief, _BYTES_PER_GB, gb_str, _limit_devices_str, device_state, details,
    plain_ip, tg_link, client_link, owner_name, holder_name, owner_link, holder_link,
    _n_devices, num, more, unlimited, plural_ru, _days_word, _days)


def rf_line(rx: int, tx: int, label: str = "", *, arrows: bool = True) -> str:
    """Вложенная строка РФ-части под строкой трафика (карточки, главная):
    «└ 🇷🇺 РФ-доступ: 7.1 ГБ (↑0.7 ↓6.4)». label — готовая подпись вместо
    «🇷🇺 РФ-доступ» (ссылкой на экран трафика; флаг — часть ссылки)."""
    from .routing import ROUTING_NAME
    return sub_line(f"{label or f'🇷🇺 {ROUTING_NAME}'}: {rf_value(rx, tx, arrows=arrows)}")


__all__ = [
    "deep_link", "profile_link", "admin_device_link", "_SUB", "tree", "sub_line",
    "access_status_line", "_e", "_num", "gb", "_BYTES_PER_TB", "unit_for", "amount", "volume",
    "human_bytes", "used_of_limit", "_updown", "rf_value", "updown_brief", "_BYTES_PER_GB",
    "gb_str", "_limit_devices_str", "device_state", "details", "plain_ip", "tg_link",
    "client_link", "owner_name", "holder_name", "owner_link", "holder_link", "_n_devices", "num",
    "more", "unlimited", "plural_ru", "_days_word", "_days", "rf_line"]
