"""
textfmt/ — чистые помощники форматирования текста: экранирование, объёмы,
склонения, ссылки на экраны и людей, шапка экрана и атомы итога.

Без aiogram и без слоя бота: ими пользуются и тексты бота (bot/texts,
bot/ui ре-экспортируют их под прежними именами), и тексты уведомлений домена
(domain/notices) — домен не тянет bot.texts.

Части: base — экранирование, ссылки на экраны, дерево вложенных строк,
объёмы, устройства, ссылки на людей, склонения; atoms — сборщик экрана и
атомы текста (шапка, статус, тумблер, подтверждение, итог, всплывашка).
"""
from __future__ import annotations

from .base import (
    deep_link, profile_link, admin_device_link, _SUB, tree, sub_line, access_status_line, _e,
    _num, gb, _BYTES_PER_TB, unit_for, amount, volume, human_bytes, used_of_limit, _updown,
    rf_value, updown_brief, _BYTES_PER_GB, gb_str, _limit_devices_str, device_state, details,
    plain_ip, addr_codes, tg_link, client_link, owner_name, holder_name, owner_link, holder_link,
    _n_devices, num, more, unlimited, plural_ru, _days_word, _days)
from .atoms import (
    STATUS, TOAST_MAX, TEXT_MAX, _TAG, st, tick, head, prompt, label, sub, screen, confirm,
    fail, result, changed, Toast, _u16, toast, note_budget)

__all__ = [
    "deep_link", "profile_link", "admin_device_link", "_SUB", "tree", "sub_line",
    "access_status_line", "_e", "_num", "gb", "_BYTES_PER_TB", "unit_for", "amount", "volume",
    "human_bytes", "used_of_limit", "_updown", "rf_value", "updown_brief", "_BYTES_PER_GB",
    "gb_str", "_limit_devices_str", "device_state", "details", "plain_ip", "addr_codes", "tg_link",
    "client_link", "owner_name", "holder_name", "owner_link", "holder_link", "_n_devices",
    "num", "more", "unlimited", "plural_ru", "_days_word", "_days",
    "STATUS", "TOAST_MAX", "TEXT_MAX", "_TAG", "st", "tick", "head", "prompt", "label", "sub",
    "screen", "confirm", "fail", "result", "changed", "Toast", "_u16", "toast", "note_budget"]
