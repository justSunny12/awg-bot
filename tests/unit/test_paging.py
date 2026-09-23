"""
Листание длинных списков кнопками: правило «не больше десяти кнопок на
экране» (keyboards.common.page_slice/page_nav), упаковка PageCB в 64 байта
Telegram и роутер листания (bot/paging.py), который запоминает страницу и
заново отдаёт диспетчеру колбэк экрана.

Цена ошибки: экран, переросший десять кнопок, — ровно то, от чего правило
вводили; элемент, до которого листанием не дойти, — профиль или адрес,
который нельзя открыть или убрать; кнопка, чья упаковка переросла 64 байта,
роняет построение всего экрана (aiogram поднимает ValueError), и раздел
перестаёт открываться у всех с длинным списком.
"""
from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import paging
from awgbot.bot.callbacks import (AdminSelfCB, ClientCB, Menu, PageCB)
from awgbot.bot.keyboards import admin as kba
from awgbot.bot.keyboards import broadcast as kbb
from awgbot.bot.keyboards import client as kbc
from awgbot.bot.keyboards import common as kbm
from awgbot.bot.keyboards import gateway as kbg
from awgbot.bot.keyboards import routing as kbr
from awgbot.bot.keyboards import settings as kbs
from awgbot.core.enums import ActivationStatus

MAX = kbm.MAX_BUTTONS


# ── page_slice ───────────────────────────────────────────────────────────────

def _walk(n: int, static: int):
    """Все страницы списка из n элементов: [(chunk, page, prev, next)]."""
    items = [f"e{i}" for i in range(n)]
    pages, page = [], 0
    while True:
        chunk, page, prev, nxt = kbm.page_slice(items, page, static)
        pages.append((chunk, page, prev, nxt))
        if not nxt:
            return items, pages
        page += 1
        assert page < 1000, "листание не кончается"


@pytest.mark.parametrize("static", range(0, 8))
@pytest.mark.parametrize("n", [0, 1, 2, 5, 7, 8, 9, 10, 11, 12, 17, 25, 40])
def test_every_page_fits_ten_buttons_and_the_pages_cover_the_whole_list(static, n):
    """Сумма «постоянные + элементы + листание» ни на одной странице не больше
    десяти, и листанием доступен каждый элемент ровно один раз, с номером по
    полному списку — по нему удаляют и переключают."""
    items, pages = _walk(n, static)
    seen = []
    for chunk, page, prev, nxt in pages:
        total = static + len(chunk) + int(prev) + int(nxt)
        assert total <= MAX, f"static={static}, n={n}, страница {page}: {total} кнопок"
        seen += chunk
    assert seen == list(enumerate(items)), "элементы потеряны, повторены или номер не по полному списку"


def test_a_list_that_fits_has_no_pages():
    """Влезает целиком — никаких кнопок листания: лишняя кнопка на коротком
    списке — шум."""
    items = list(range(MAX - 3))
    assert kbm.page_slice(items, 0, static=3) == (list(enumerate(items)), 0, False, False)
    assert kbm.page_slice([], 5, static=3) == ([], 0, False, False), "пустой список — пустая страница 0"


def test_one_over_the_room_turns_on_paging_with_room_for_both_arrows():
    items = list(range(MAX - 3 + 1))                 # на один больше, чем влезает
    chunk, page, prev, nxt = kbm.page_slice(items, 0, static=3)
    assert (page, prev, nxt) == (0, False, True)
    assert len(chunk) == MAX - 3 - 2, "на страницу — место под обе стрелки"
    chunk, page, prev, nxt = kbm.page_slice(items, 1, static=3)
    assert (page, prev, nxt) == (1, True, False)
    assert [i for i, _ in chunk] == list(range(MAX - 3 - 2, len(items)))


@pytest.mark.parametrize("asked", [3, 50, 10**6])
def test_a_page_past_the_end_falls_back_to_the_last_one(asked):
    """Список укоротился (удалили элементы с последней страницы), а страница
    запомнена старая: показываем последнюю, а не пустой экран без стрелок."""
    items = list(range(20))
    chunk, page, prev, nxt = kbm.page_slice(items, asked, static=1)
    last = kbm.page_slice(items, 10**9, static=1)
    assert page == last[1] and chunk == last[0] and chunk, "не последняя страница"
    assert prev is True and nxt is False


@pytest.mark.parametrize("asked", [-1, -100, None])
def test_a_negative_or_missing_page_is_the_first(asked):
    chunk, page, prev, nxt = kbm.page_slice(list(range(20)), asked, static=1)
    assert page == 0 and prev is False and nxt is True and chunk[0] == (0, 0)


# ── page_nav ─────────────────────────────────────────────────────────────────

def test_page_nav_draws_arrows_that_point_to_the_neighbour_pages():
    back = Menu(action="clients").pack()
    kb = InlineKeyboardBuilder()
    assert kbm.page_nav(kb, "clients", 7, 3, True, True, back) == 2
    buttons = [b for row in kb.as_markup().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [kbm.PREV_LABEL, kbm.NEXT_LABEL]
    prev, nxt = (PageCB.unpack(b.callback_data) for b in buttons)
    assert (prev.screen, prev.ref, prev.page, prev.back) == ("clients", 7, 2, back)
    assert (nxt.screen, nxt.ref, nxt.page, nxt.back) == ("clients", 7, 4, back)


def test_page_nav_without_neighbours_draws_nothing():
    kb = InlineKeyboardBuilder()
    assert kbm.page_nav(kb, "clients", 0, 0, False, False, "m:clients") == 0
    assert kb.as_markup().inline_keyboard == []
    kb = InlineKeyboardBuilder()
    assert kbm.page_nav(kb, "clients", 0, 0, False, True, "m:clients") == 1
    assert [b.text for row in kb.as_markup().inline_keyboard for b in row] == [kbm.NEXT_LABEL]


# ── все экраны с листанием: десять кнопок и 64 байта ────────────────────────

# id с запасом — семь цифр: живые базы до этого не дорастут, а упаковка
# кнопки листания несёт id в двух местах (ref и внутри back)
BIG = 9_999_999


def _dev(i: int):
    return SimpleNamespace(id=BIG - i, name=f"u{i:04d}", block_reason=0, is_managed=True,
                           is_gateway=0, is_lent=False, friend=None,
                           address=f"10.8.{i // 250}.{i % 250 + 2}", routing_on=bool(i % 2))


def _cli(i: int):
    return SimpleNamespace(id=BIG - i, name=f"u{i:04d}", block_reason=0,
                           activation_status=ActivationStatus.ACTIVE, routing_allowed=bool(i % 2),
                           effective_period_end="", status="active")


def _devs(n):
    return [_dev(i) for i in range(n)]


def _clis(n):
    return [_cli(i) for i in range(n)]


def _doms(n):
    return [f"u{i:04d}.example" for i in range(n)]


_BACK = Menu(action="main").pack()

# Все 19 вызовов page_nav и все «render», с которыми их зовут хендлеры
# (client.py: cb.data пункта меню; selfops.py: AdminSelfCB; devices.py:
# ClientCB gen_for с id профиля). Каждая функция — (n, page) → клавиатура.
SCREENS = {
    "lanlist": lambda n, p: kbg.gateway_lan_list_kb(
        [("ru" if i % 3 == 0 else "vpn", d) for i, d in enumerate(_doms(n))], page=p),
    "gwssh (новая обвязка)": lambda n, p: kbg.gateway_ssh_kb(
        {"allow": [f"u{i:04d}.dyn.example" for i in range(n)], "new_plumbing": True, "filter": bool(n)}, page=p),
    "gwssh (старая обвязка)": lambda n, p: kbg.gateway_ssh_kb(
        {"allow": [f"u{i:04d}.dyn.example" for i in range(n)], "new_plumbing": False}, page=p),
    "devices": lambda n, p: kbc.client_devices(_devs(n), page=p),
    "devices (админ себе)": lambda n, p: kbc.client_devices(
        _devs(n), page=p, render=AdminSelfCB(action="devices").pack()),
    "gdevices": lambda n, p: kbc.guest_devices(_devs(n), page=p),
    "pick (клиент)": lambda n, p: kbc.pick_device(_devs(n), "gen_file", page=p,
                                                  render=Menu(action="gen_file").pack()),
    "pick (админ себе)": lambda n, p: kbc.pick_device(_devs(n), "gen_link", page=p,
                                                      render=AdminSelfCB(action="gen_link").pack()),
    "pick (админ профилю)": lambda n, p: kbc.pick_device(
        _devs(n), "gen_link", back_cb=ClientCB(action="open", client_id=BIG).pack(), page=p,
        render=ClientCB(action="gen_for", client_id=BIG).pack(), ref=BIG),
    "gpick": lambda n, p: kbc.guest_pick_device(_devs(n), "gen_file", page=p),
    "deldev": lambda n, p: kbc.pick_device_to_delete(_devs(n), page=p),
    "guidedev (можно добавить)": lambda n, p: kbc.guide_connect_devices(
        _devs(n), (0, 0), guide="connect_apple", page=p),
    "guidedev (лимит)": lambda n, p: kbc.guide_connect_devices(
        _devs(n), (n, n or 1), guide="connect_apple", page=p),
    "fw": lambda n, p: kbs.settings_firewall(
        {"enabled": True, "raw_allow": [f"u{i:04d}.example" for i in range(n)]}, page=p),
    "fw (фильтр выключен)": lambda n, p: kbs.settings_firewall(
        {"enabled": False, "raw_allow": [f"u{i:04d}.example" for i in range(n)]}, page=p),
    "rtdevs": lambda n, p: kbr.routing_devices(BIG, _devs(n), back_target=_BACK, page=p),
    "rtpanel": lambda n, p: kbr.routing_panel(BIG, master_on=True, domains=_doms(n), enabled=1,
                                              total=2, back_target=_BACK, page=p),
    "gwpick": lambda n, p: kbr.gateway_pick(_devs(n), slot=2, page=p),
    "rtusers": lambda n, p: kbr.settings_routing_users(_clis(n), page=p),
    "addpick": lambda n, p: kba.pick_client_for_add_device(_clis(n), page=p),
    "clients": lambda n, p: kba.admin_clients(_clis(n), page=p),
    "clidevs": lambda n, p: kba.admin_client_device_list(_devs(n), BIG, page=p),
    "unassigned": lambda n, p: kba.unassigned_devices(_devs(n), page=p),
    "reassign": lambda n, p: kba.reassign_targets(BIG, _clis(n), page=p),
    "bcast": lambda n, p: kbb.broadcast_targets(_clis(n), set(), extend=True, page=p),
}


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize("screen", sorted(SCREENS))
@pytest.mark.parametrize("n", [0, 1, 6, 9, 10, 11, 23])
def test_each_paged_screen_keeps_ten_buttons_and_reaches_every_item(screen, n):
    """Реальные клавиатуры, а не арифметика: сколько кнопок легло на экран и
    доходит ли листание (по страницам из самих стрелок) до каждого элемента."""
    build = SCREENS[screen]
    page, found, turns = 0, [], 0
    while True:
        buttons = _buttons(build(n, page))
        assert len(buttons) <= MAX, f"{screen}, {n} эл., стр. {page}: {len(buttons)} кнопок"
        found += [f"u{i:04d}" for i in range(n) if any(f"u{i:04d}" in b.text for b in buttons)]
        nxt = [b for b in buttons if b.text == kbm.NEXT_LABEL]
        if not nxt:
            break
        page = PageCB.unpack(nxt[0].callback_data).page
        turns += 1
        assert turns < 100, "листание не кончается"
    assert sorted(found) == [f"u{i:04d}" for i in range(n)], (
        f"{screen}: до части элементов не долистать или они повторяются")


@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_the_page_button_fits_64_bytes_on_the_longest_real_screen(screen):
    """Самый длинный реальный back (id в семь цифр, трёхзначный номер
    страницы на списке в полторы тысячи элементов): упаковка кнопки листания
    обязана уложиться в лимит Telegram — иначе aiogram роняет построение
    всего экрана."""
    n = 1500
    markup = SCREENS[screen](n, 10**6)               # последняя страница — номер длиннее всего
    pages = [b.callback_data for b in _buttons(markup) if b.callback_data.startswith("pg|")]
    assert pages, f"{screen}: на длинном списке нет кнопок листания"
    for data in pages:
        size = len(data.encode())
        assert size <= 64, f"{screen}: {size} байт — {data}"
        cb = PageCB.unpack(data)
        assert cb.page >= 100, "проверяли не трёхзначный номер страницы"


def test_an_oversized_page_button_fails_loudly_at_packing():
    """Если однажды back перерастёт лимит — пусть это будет понятная ошибка
    при сборке клавиатуры, а не молча обрезанный колбэк, который ведёт не туда."""
    long_back = ClientCB(action="gen_for", client_id=BIG).pack() + "x" * 60
    with pytest.raises(ValueError, match="too long"):
        PageCB(screen="pick", ref=BIG, page=100, back=long_back).pack()


# ── роутер листания ──────────────────────────────────────────────────────────

CHAT = 424242


class _Session(BaseSession):
    """Сессия бота без сети: всё, что бот отправил бы в Telegram, записывается."""

    def __init__(self):
        super().__init__()
        self.calls: list = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *a, **k):                # pragma: no cover
        raise AssertionError("скачиваний в листании нет")
        yield b""

    async def close(self):
        pass


@pytest.fixture()
def dp(monkeypatch):
    """Диспетчер с настоящим роутером листания и экраном «Клиенты», который
    записывает, с какой страницы его попросили нарисоваться."""
    monkeypatch.setattr(paging, "_pages", {})
    # роутер модульный и в процессе один; после теста — отцепить от диспетчера
    monkeypatch.setattr(paging.router, "_parent_router", None)
    d = Dispatcher(storage=MemoryStorage())
    d.include_router(paging.router)
    screen = Router(name="screen")
    d.drawn = []

    @screen.callback_query(Menu.filter(F.action == "clients"))
    async def clients(cb: CallbackQuery):
        d.drawn.append((cb.data, paging.page_of(cb.message.chat.id, "clients")))

    d.include_router(screen)
    return d


def _update(data: str, chat_id: int = CHAT) -> Update:
    msg = Message(message_id=7, date=datetime.datetime.now(), chat=Chat(id=chat_id, type="private"),
                  text="👥 Клиенты")
    return Update(update_id=1, callback_query=CallbackQuery(
        id="q1", chat_instance="ci", **{"from": User(id=chat_id, is_bot=False, first_name="A")},
        message=msg, data=data))


async def test_turning_the_page_remembers_it_and_redraws_the_screen_with_its_own_handler(dp):
    """Стрелка несёт страницу и колбэк экрана: роутер запоминает страницу и
    отдаёт колбэк экрана диспетчеру заново — экран рисуется своим же
    хендлером уже с новой страницы. Запомни он страницу после перерисовки —
    стрелка «листала» бы с опозданием на одно нажатие."""
    session = _Session()
    bot = Bot("42:DUMMY", session=session)
    back = Menu(action="clients").pack()
    await dp.feed_update(bot, _update(PageCB(screen="clients", page=2, back=back).pack()))
    assert dp.drawn == [(back, 2)], f"экран не перерисован или со старой страницы: {dp.drawn}"
    assert paging.page_of(CHAT, "clients") == 2
    assert paging.page_of(CHAT + 1, "clients") == 0, "страница одного чата досталась другому"
    assert any(type(m).__name__ == "AnswerCallbackQuery" for m in session.calls), (
        "нажатие не подтверждено — у человека крутятся часики на кнопке")


async def test_a_page_button_without_a_screen_only_remembers_the_page(dp):
    session = _Session()
    bot = Bot("42:DUMMY", session=session)
    await dp.feed_update(bot, _update(PageCB(screen="clients", page=3).pack()))
    assert dp.drawn == [], "без back перерисовывать нечего"
    assert paging.page_of(CHAT, "clients") == 3
    assert [type(m).__name__ for m in session.calls] == ["AnswerCallbackQuery"]


def test_page_of_an_unknown_chat_or_list_is_the_first(monkeypatch):
    """Рестарт бота теряет страницы: список открывается с первой. Незнакомый
    чат, список, параметр или вовсе None вместо чата — тоже первая, а не
    исключение посреди хендлера."""
    monkeypatch.setattr(paging, "_pages", {})
    paging.remember(CHAT, "clidevs", 5, 3)
    assert paging.page_of(CHAT, "clidevs", 5) == 3
    assert paging.page_of(CHAT, "clidevs", 6) == 0, "страница профиля 5 досталась профилю 6"
    assert paging.page_of(CHAT, "clients") == 0
    assert paging.page_of(None, "clidevs", 5) == 0
    assert paging.page_of(0, "clients") == 0
    paging.remember(CHAT, "clients", 0, -4)
    assert paging.page_of(CHAT, "clients") == 0, "отрицательная страница запомнена как есть"
