"""lists.py — часть пакета admin (см. __init__): списки онлайн, истекающих, без профиля и трафик списком."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import (
    _e, human_bytes, device_state, plain_ip, rf_value, tree, sub_line, profile_link,
    admin_device_link, _n_devices, _BYTES_PER_GB, plural_ru)
from ..fmt import deep_link as _deep_link
from ..routing import ROUTING_NAME
from .common import TRAFFIC_PAYLOAD, month_label, _total, _LIST_SEP

# экраны-списки листаются на месте (bot/lists/page.paged): страница — до 30 строк
# и 3500 знаков; «🟡 Истекают» — по кнопкам «⏱»: 8 рядов по две, с листанием и «В меню»
EXPIRING_PER_PAGE = 16


# ─────────────────────────────────────────────────────────────────────────────
# Списки: онлайн, истекающие, без профиля
# ─────────────────────────────────────────────────────────────────────────────


def online_value(profiles: int, devices: int) -> str:
    """«2 профиля, 2 устройства» — значение счётчика онлайн; никого — «0»."""
    if not devices:
        return "0"
    return f"{profiles} {plural_ru(profiles, 'профиль', 'профиля', 'профилей')}, {_n_devices(devices)}"


def online_devices_page(rows, bot_username: str = "", refreshed: str = "", page: int = 0):
    """«📶 Онлайн: 2 профиля, 2 устройства · стр. 1 из 3» — профили и устройства,
    как на главной; записи через пустую строку, шлюзы — вверху, имя устройства —
    ссылка на карточку, имя профиля — на карточку профиля; устройства без
    профиля (и служебного) — «без профиля». rows — [(устройство, профиль или
    None)]. Длинный список — страницами (bot/lists/page.Page)."""
    from awgbot.bot.lists.page import Page, paged
    profiles = len({c.id for _d, c in rows if c is not None and not getattr(c, "is_service", 0)})
    value = online_value(profiles, len(rows))
    tail = f"{_LIST_SEP}{refreshed}" if refreshed else ""
    if not rows:
        text = ui.head("📶 Онлайн", value=value) + _LIST_SEP + "Сейчас никто не подключён" + tail
        return Page(text=text, cur=0, total=1, start=0, stop=0)
    items = []
    for d, c in rows:
        if getattr(d, "is_gateway", 0):
            items.append(f"🛰 {admin_device_link(d, bot_username)} [шлюз] · {plain_ip(d.address)}")
            continue
        who = ("без профиля" if c is None or getattr(c, "is_service", 0)
               else profile_link(c, bot_username))
        items.append(f"{device_state(d, for_admin=True)} {admin_device_link(d, bot_username)} · "
                     f"{who} · {plain_ip(d.address)}")
    return paged(items, page, header=lambda lbl: ui.head("📶 Онлайн", value=value, meta=[lbl]),
                 sep=_LIST_SEP, gap=_LIST_SEP, tail=tail)


def expiring_page(rows, bot_username: str = "", page: int = 0):
    """«🟡 Истекают: 2»; «[Ксюша] — 3 дн., до 27.09 18:00». Страница — столько
    строк, сколько кнопок «⏱» влезает (EXPIRING_PER_PAGE): строки и кнопки
    одной страницы — одни и те же профили (срез Page.start:Page.stop)."""
    from awgbot.bot.lists.page import Page, paged
    if not rows:
        text = ui.head("🟡 Истекают", value=str(len(rows))) + _LIST_SEP + "Истекающих подписок нет"
        return Page(text=text, cur=0, total=1, start=0, stop=0)
    items = []
    for c, _secs in rows:
        end = timeutil.parse_iso(c.period_end)
        items.append(f"{profile_link(c, bot_username)} — {timeutil.remaining_brief(end)}, "
                     f"до {timeutil.fmt_end_ui(end)}")
    return paged(items, page, header=lambda lbl: ui.head("🟡 Истекают", value=str(len(rows)), meta=[lbl]),
                 sep=_LIST_SEP, gap=_LIST_SEP, max_rows=EXPIRING_PER_PAGE)


def unassigned_text(n: int) -> str:
    if not n:
        return ui.head("📦 Без профиля", value="никого")
    return ui.head("📦 Без профиля", value=f"{n} — {'устройство добавлено' if n == 1 else 'устройства добавлены'} не ботом")


# ─────────────────────────────────────────────────────────────────────────────
# Трафик списком (общий трафик ведущий, РФ — вложенной строкой под записью)
# ─────────────────────────────────────────────────────────────────────────────

_OUTSIDE = "🧐 Из них вне профилей: {v} — удалённые устройства и первые минуты новых"


def _rf_sub(rf) -> list[str]:
    """Вложенная строка РФ под записью — только при ненулевой РФ-части."""
    if not rf or int(rf[0]) + int(rf[1]) <= 0:
        return []
    return [f"🇷🇺 {ROUTING_NAME}: {human_bytes(int(rf[0]) + int(rf[1]))}"]


def _tree_page(head_lines: list[str], branches: list, page: int, empty: str):
    """Шапка (первая строка — с меткой страницы) и записи дерева страницами."""
    from awgbot.bot.lists.page import Page, paged
    if not branches:
        return Page(text="\n".join(head_lines) + "\n\n" + empty, cur=0, total=1, start=0, stop=0)
    blocks = [tree([b]) for b in branches]

    def header(lbl: str) -> str:
        first = head_lines[0] + (f" · {lbl}" if lbl else "")
        return "\n".join([first, *head_lines[1:]])
    return paged(blocks, page, header=header, sep="\n\n", gap="\n\n")


def traffic_profiles_page(rows, bot_username: str = "", total: tuple[int, int] = (0, 0),
                          rf_total: tuple[int, int] | None = None, outside: int = 0, page: int = 0):
    """«📊 Трафик за сентябрь: <итог>», под шапкой — РФ-итог и «🧐 Вне профилей»
    (только при ненулевых значениях), затем профили по убыванию общего трафика
    (нулевые не выводятся) со своей РФ-строкой; длинный список — страницами."""
    head = [ui.head(f"📊 Трафик за {month_label()}", value=_total(*total))]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME}: {rf_value(*rf_total)}"))
        # «Вне профилей» — часть РФ-итога (удалённые устройства и первые минуты
        # новых), поэтому под шапкой, а не записью среди профилей
        if int(outside or 0) >= _BYTES_PER_GB // 100:
            head.append(sub_line(_OUTSIDE.format(v=human_bytes(outside))))
    branches = []
    for c, rx, tx, rf in [(c, rx, tx, rf) for c, rx, tx, rf in rows if int(rx) + int(tx) > 0]:
        branches.append((f"👤 {_deep_link(bot_username, f'{TRAFFIC_PAYLOAD}-{c.id}', c.name)}: "
                         f"{_total(rx, tx)}", _rf_sub(rf)))
    return _tree_page(head, branches, page, "Трафика за месяц ещё нет")


def traffic_devices_page(client, rows, total: tuple[int, int] = (0, 0),
                         rf_total: tuple[int, int] | None = None, bot_username: str = "", page: int = 0):
    """«📊 Трафик за сентябрь: <итог> · профиль [Ксюша]» — то же по устройствам профиля."""
    head = [ui.head(f"📊 Трафик за {month_label()}", value=_total(*total),
                    meta=[f"профиль {profile_link(client, bot_username)}"])]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME}: {rf_value(*rf_total)}"))
    branches = []
    live = [r for r in sorted(rows, key=lambda r: -(int(r[1]) + int(r[2]))) if int(r[1]) + int(r[2]) > 0]
    for d, rx, tx, rf in live:
        branches.append((f"{device_state(d, for_admin=True)} {_e(d.name)}: {_total(rx, tx)}",
                         _rf_sub(rf)))
    return _tree_page(head, branches, page, "Трафика за месяц ещё нет")
