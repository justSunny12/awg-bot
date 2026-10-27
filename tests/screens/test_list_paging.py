"""Листание экранов-списков через настоящий диспетчер: нажатие «След.
страница ▶️» идёт в общий роутер листания (bot/paging.turn_page), тот
запоминает страницу и отдаёт экрану его же колбэк заново.

Здесь — не сверка с эталоном, а утверждения о поведении: листанием по
кнопкам самого экрана видна каждая запись ровно один раз; «⏱» на
странице «🟡 Истекают» продлевает того, чья строка на ней; `/start online`
после листания — та же страница; список, сократившийся между нажатиями,
открывается последней страницей; «⬅️ Назад» и «⬅️ В меню» ведут туда, откуда
пришли.

Цена ошибки: кнопка листания, которую не поймал ни один экран, — «Кнопка
устарела» вместо второй страницы, и записей за первой страницей админ не
увидит вовсе; «⏱» с чужой страницы продлевает подписку не тому человеку;
ссылка «обновлено … 🔄» сбрасывает на первую страницу — человек теряет место
в длинном списке на каждом обновлении.
"""
from __future__ import annotations

import re

import pytest

from awgbot.bot.callbacks import ClientCB, Menu, SetCB
from awgbot.bot.keyboards.common import NEXT_LABEL, PREV_LABEL
from tests.screens.base import Shot
from tests.screens.harness import take
from tests.screens.shots_admin_lists import FIRST, _exp, _mig, _on, _trdev, _turn

pytestmark = pytest.mark.screens

_ONLINE = ("press", Menu(action="online"))
_EXPIRING = ("press", Menu(action="expiring"))


def _screen(rec) -> tuple[str, list[list]]:
    """Последний нарисованный экран записи: текст и ряды кнопок (подпись, колбэк)."""
    drawn = [c for c in rec.calls if c.body is not None and c.markup is not None]
    assert drawn, f"экран не нарисован: {[c.head for c in rec.calls]}"
    c = drawn[-1]
    return c.body, [[(b.text, b.callback_data) for b in row] for row in c.markup.inline_keyboard]


def _button(rows, label: str) -> str | None:
    found = [d for row in rows for t, d in row if t == label]
    return found[0] if found else None


async def _shot(tmp_path, fakes, sid: str, data, steps) -> tuple[str, list[list]]:
    rec = await take(Shot(sid, role="admin", steps=steps, data=data), tmp_path, fakes)
    return _screen(rec)


async def test_online_pages_turned_by_their_own_buttons_show_every_device_once(tmp_path, frozen, fakes):
    """61 устройство: «След. страница ▶️» с самого экрана — три страницы,
    каждое устройство ровно на одной; «◀️ Пред. страница» с последней —
    снова вторая, та же."""
    steps, seen, texts = [_ONLINE], [], []
    while True:
        text, rows = await _shot(tmp_path, fakes, f"walk{len(steps)}", _on(61), steps)
        texts.append(text)
        seen += re.findall(r">Тел (\d\d)<", text)
        assert rows[-1] == [("⬅️ В меню", "m:main")], rows[-1]
        nxt = _button(rows, NEXT_LABEL)
        if nxt is None:
            break
        steps = [*steps, ("press", nxt)]
        assert len(steps) < 10, "листание не кончается"
    assert len(texts) == 3, f"61 устройство — {len(texts)} страниц"
    assert seen == [f"{i:02d}" for i in range(61)], "листанием видны не все устройства или с повторами"
    prev = _button(rows, PREV_LABEL)
    assert prev is not None, "на последней странице нет «◀️ Пред. страница»"
    back, _ = await _shot(tmp_path, fakes, "walkback", _on(61), [*steps, ("press", prev)])
    assert back == texts[1], "листание назад открыло не ту страницу"


async def test_start_online_after_turning_opens_the_same_page(tmp_path, frozen, fakes):
    """«обновлено … 🔄» (/start online) после листания — та же страница, а не первая."""
    p2 = [_ONLINE, _turn("online", Menu(action="online"), 1)]
    turned, _ = await _shot(tmp_path, fakes, "p2", _on(61), p2)
    again, rows = await _shot(tmp_path, fakes, "again", _on(61), [*p2, ("start", "online")])
    assert "стр. 2 из 3" in again.splitlines()[0], again.splitlines()[0]
    assert re.findall(r">Тел (\d\d)<", again) == re.findall(r">Тел (\d\d)<", turned), \
        "/start online после листания — другие строки"
    assert _button(rows, PREV_LABEL) and _button(rows, NEXT_LABEL), rows


async def test_a_list_that_shrank_opens_its_last_page(tmp_path, frozen, fakes):
    """Человек на стр. 3 из 3, а 30 устройств отключились: обновление — стр. 2
    из 2 с последней записью, без «След. страница ▶️»."""
    async def offline(services, bot):
        for d, _c in services.online_devices()[31:]:
            services.db.update_device_fields(d.id, last_handshake=0)
    steps = [_ONLINE, *[_turn("online", Menu(action="online"), p) for p in (1, 2)],
             ("call", offline, "отключились"), ("start", "online")]
    text, rows = await _shot(tmp_path, fakes, "clamp", _on(61), steps)
    assert "стр. 2 из 2" in text.splitlines()[0], text.splitlines()[0]
    assert re.findall(r">Тел (\d\d)<", text) == ["29", "30"], "последняя страница — не последние записи"
    assert _button(rows, NEXT_LABEL) is None and _button(rows, PREV_LABEL), rows


@pytest.mark.parametrize("n, total", [(25, 2), (61, 4)])
async def test_expiring_buttons_on_each_page_extend_the_profiles_of_its_lines(tmp_path, frozen, fakes, n, total):
    """«🟡 Истекают»: на каждой странице кнопки «⏱» — ровно профили её строк,
    рядов не больше десяти; «⏱» второй страницы открывает продление того
    профиля, что на ней."""
    steps, pages = [_EXPIRING], 0
    while True:
        text, rows = await _shot(tmp_path, fakes, f"exp{n}_{pages}", _exp(n), steps)
        pages += 1
        assert len(rows) <= 10, f"стр. {pages}: {len(rows)} рядов"
        ext = [ClientCB.unpack(d).client_id for row in rows for _t, d in row if d.startswith("c:extend_exp:")]
        shown = [int(x) for x in re.findall(r"start=cl-(\d+)", text)]
        assert ext == shown, f"стр. {pages}: кнопки {ext}, строки {shown}"
        if pages == 2:
            target = ext[0]
            ext_text, _ = await _shot(tmp_path, fakes, f"exp{n}_tap", _exp(n),
                                      [*steps, ("press", ClientCB(action="extend_exp", client_id=target))])
            assert f"Профиль {target - FIRST:02d}" in ext_text, ext_text.splitlines()[:2]
        nxt = _button(rows, NEXT_LABEL)
        if nxt is None:
            break
        steps = [*steps, ("press", nxt)]
    assert pages == total, f"{n} профилей — {pages} страниц, ждали {total}"


async def test_back_from_a_turned_traffic_page_is_the_traffic_by_profile(tmp_path, frozen, fakes):
    """Трафик профиля, стр. 2: «⬅️ Назад» — трафик по профилям; листание у
    экрана профиля своё (страница профиля не путается со страницей списка)."""
    open_ = [("start", ""), ("start", f"traffic-{FIRST}")]
    p2 = [*open_, _turn("traffic_dev", ClientCB(action="traffic", client_id=FIRST), 1, FIRST)]
    text, rows = await _shot(tmp_path, fakes, "trdev2", _trdev(61), p2)
    assert "стр. 2 из 3" in text.splitlines()[0] and "профиль" in text.splitlines()[0], text.splitlines()[0]
    assert rows[-1] == [("⬅️ Назад", "m:traffic")], rows[-1]
    back, back_rows = await _shot(tmp_path, fakes, "trdevback", _trdev(61), [*p2, ("press", "m:traffic")])
    assert back.startswith("📊 <b>Трафик за ") and "профиль" not in back.splitlines()[0], back.splitlines()[0]
    assert "стр." not in back.splitlines()[0], "у списка профилей — страница профиля"
    assert back_rows[-1] == [("⬅️ В меню", "m:main")], back_rows[-1]


async def test_pending_turns_and_returns_to_the_service_section(tmp_path, frozen, fakes):
    """«🚚 Ещё не переехали», 61: стр. 2 — через роутер листания тем же
    действием раздела; «⬅️ Назад» — «🔧 Сервис»."""
    pending = SetCB(sec="mig", act="do", key="pending")
    p2 = [("press", pending), _turn("mig_pending", pending, 1)]
    text, rows = await _shot(tmp_path, fakes, "pend2", _mig(61), p2)
    assert "стр. 2 из 3" in text.splitlines()[0], text.splitlines()[0]
    assert re.findall(r"Тел (\d\d)", text) == [f"{i:02d}" for i in range(30, 60)], "вторая страница — не те строки"
    assert rows[-1] == [("⬅️ Назад", SetCB(sec="svc").pack())], rows[-1]
    svc, _ = await _shot(tmp_path, fakes, "pendback", _mig(61), [*p2, ("press", SetCB(sec="svc"))])
    assert svc.startswith("🔧 <b>Сервис"), svc.splitlines()[0]
