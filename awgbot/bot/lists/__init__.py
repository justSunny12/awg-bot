"""
lists — экран «весь список»: куда ведёт хвост «и ещё N» обрезанного списка.

Хвост строки-сводки — ссылка `?start=list-<вид>[-<ref>][-n]` (грамматика —
util/listlink); экран собирается в момент открытия из тех же данных, что и
источник, листается страницами (page) и возвращает «⬅️ Назад» на экран-
источник. Из уведомления или следа события (метка «-n») экран открывается
новым сообщением внизу, в шапке живого вида — «· сейчас»: данные на момент
открытия, а не уведомления.

Каждый вид сам несёт роль и установку (основной бот или агент шлюза):
роутер списков стоит до роутеров ролей и их фильтров не наследует, поэтому
вход из /start и колбэк ListCB сверяют роль до сборки строк. Чужая роль или
незнакомый вид — False: вызывающий покажет главную роли, без слова о том,
что такой список существует.

Части: page — разбиватель страниц; router — ListCB; kinds — виды по
установкам (slots, server — основной бот; agent — агент шлюза; snap — снимки
событий, обе установки и все роли: infra/db/listsnap).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from awgbot.bot import ui

log = logging.getLogger(__name__)


@dataclass
class View:
    """Что вид отдаёт экрану: шапка (значок и имя, имя экранировано), число в
    шапке, строки (готовый HTML), «⬅️ Назад», текст пустого списка целиком."""
    title: str
    rows: list[str] = field(default_factory=list)
    back: tuple | None = None
    count: str = ""
    empty: str = ""
    sep: str = "\n"                              # «\n\n» — строки с ведущим значком
    live: bool = True
    labels: tuple[int, ...] = ()                 # номера строк-подписей групп: страница продолжения повторяет подпись
    at: str = ""                                 # «на 27.10 14:05 (UTC+3)» — снимок события, последним в шапке


@dataclass(frozen=True)
class Kind:
    name: str
    branch: str                                   # main | gateway | * — установка, где вид есть
    roles: tuple[str, ...]
    build: Callable[..., Awaitable[View | None]]  # build(services, ref, key, chat_id) → View
    anchored: bool = False                         # ref — номер группы, к которой ведёт хвост (см. render)


_REGISTRY: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    _REGISTRY[kind.name] = kind
    return kind


def _load() -> None:
    if _REGISTRY:
        return
    from .kinds import agent, server, slots, snap  # noqa: F401 — виды регистрируются при импорте


def kind_for(name: str, role: str) -> Kind | None:
    """Вид, если он есть у этой установки и этой роли."""
    from awgbot.bot import roles
    _load()
    k = _REGISTRY.get(name)
    if k is None or role not in k.roles or k.branch not in (roles.current().name, "*"):
        return None
    return k


def screen_name(kind: str) -> str:
    return f"ls_{kind}"


async def render(services, kind: str, *, role: str, ref: int = 0, key: str = "", new: bool = False,
                 page: int = 0, chat_id: int = 0, anchor: int = 0):
    """(text, markup) экрана вида или None (вида нет у роли, сборка упала).
    anchor — номер группы (с 1): открыть страницу, где она начинается (хвост
    второй группы ведёт к ней, а не на первую страницу); страница запоминается."""
    from awgbot.bot import paging
    from awgbot.bot.callbacks import ListCB, PageCB
    from . import page as pages
    k = kind_for(kind, role)
    if k is None:
        return None
    try:
        view = await k.build(services, int(ref or 0), str(key or ""), int(chat_id or 0))
    except Exception as e:                            # noqa: BLE001
        log.warning("lists: вид %s/%s не собрался: %s", kind, ref, e)
        return None
    if view is None:
        return None
    back_rows = [view.back or _menu(role)]
    if not view.rows:
        return view.empty or ui.head(view.title), ui.rows(*back_rows)
    now = "сейчас" if (view.live and new) else ""
    probe = ui.head(view.title, meta=[view.count, "стр. 99 из 99", now, view.at])
    widest = max((pages.visible(view.rows[i]) for i in view.labels), default=0)
    chunks = pages.split(view.rows, pages.visible(probe) + 2 + (widest + 1 if view.labels else 0), view.sep,
                         pages.MAX_ROWS - 1 if view.labels else pages.MAX_ROWS,     # место под повтор подписи
                         pages.entities(probe) + max((pages.entities(view.rows[i]) for i in view.labels), default=0))
    # номера строк по страницам; подпись группы последней строкой страницы — на следующую
    spans: list[list[int]] = []
    at = 0
    for c in chunks:
        spans.append(list(range(at, at + len(c))))
        at += len(c)
    for n in range(len(spans) - 1):
        if len(spans[n]) > 1 and spans[n][-1] in view.labels:
            spans[n + 1].insert(0, spans[n].pop())
    total = len(spans)
    cur = min(max(int(page or 0), 0), total - 1)
    if anchor and 0 < anchor <= len(view.labels):
        target = view.labels[anchor - 1]
        cur = next((n for n, sp in enumerate(spans) if target in sp), cur)
        paging.remember(chat_id, screen_name(kind), int(ref or 0), cur)
    head = ui.head(view.title, meta=[view.count, f"стр. {cur + 1} из {total}" if total > 1 else "", now, view.at])
    body = [view.rows[i] for i in spans[cur]]
    first = spans[cur][0] if spans[cur] else 0
    if body and first not in view.labels:
        label = max((i for i in view.labels if i < first), default=None)
        if label is not None:                             # группа началась на прошлой странице — подпись снова
            body.insert(0, view.rows[label])
    text = head + "\n\n" + view.sep.join(body)
    back = ListCB(kind=kind, ref=int(ref or 0), key=str(key or ""), new=1 if new else 0).pack()
    nav = []
    if cur > 0:
        nav.append(ui.btn(_prev(), PageCB(screen=screen_name(kind), ref=int(ref or 0), page=cur - 1, back=back)))
    if cur < total - 1:
        nav.append(ui.btn(_next(), PageCB(screen=screen_name(kind), ref=int(ref or 0), page=cur + 1, back=back)))
    return text, ui.rows(nav or None, *back_rows)


def _menu(role: str) -> tuple:
    """«⬅️ В меню» — на главную роли того, кто смотрит (у гостя она своя)."""
    from awgbot.bot import roles
    if role == "invited":
        from awgbot.bot.callbacks import FriendCB
        return ui.to_menu(FriendCB(action="refresh"))
    return ui.to_menu(roles.current().cb.menu())


def _prev() -> str:
    from awgbot.bot.keyboards.common import PREV_LABEL
    return PREV_LABEL


def _next() -> str:
    from awgbot.bot.keyboards.common import NEXT_LABEL
    return NEXT_LABEL


async def open_link(message, services, role: str, client, link) -> bool:
    """Вход из /start list-…: команду из чата убрать (служебная), экран — на месте
    живого меню или, с меткой «-n», новым сообщением. False — вида у роли нет
    (вызывающий покажет главную)."""
    from awgbot.bot import paging
    from awgbot.bot.handlers.common import show_parts
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    if kind_for(link.kind, role) is None:
        return False
    paging.remember(message.chat.id, screen_name(link.kind), link.ref, 0)   # по ссылке — первая страница
    k = kind_for(link.kind, role)
    parts = await render(services, link.kind, role=role, ref=link.ref, key=link.key, new=link.new,
                         chat_id=message.chat.id, anchor=link.ref if k.anchored else 0)
    if parts is None:
        return False
    await show_parts(message, services, *parts, new=link.new)
    return True


def make_router():
    from .router import make_router as _make
    return _make()


__all__ = ["View", "Kind", "register", "kind_for", "render", "open_link", "make_router", "screen_name"]
