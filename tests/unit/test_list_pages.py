"""
Страницы экрана «весь список» (bot/lists/page.split и bot/lists.render):
бюджет 3500 единиц UTF-16 видимого текста и не больше 30 строк на страницу,
шапка «стр. k из m», зажим номера страницы.

Цена ошибки: страница, переросшая 4096 знаков, — отказ Bot API «message is
too long», и список, ради которого хвост стал ссылкой, не открывается вовсе;
строка, потерянная или повторённая на стыке страниц, — подсеть или имя,
которых человек не увидит; «стр. 1 из 1» и кнопки листания на коротком
списке — шум; номер страницы за концом (список сократился между нажатиями)
— пустой экран вместо последней страницы.
"""
from __future__ import annotations

import html
import random
import re

import pytest

from awgbot.bot import lists, ui
from awgbot.bot.callbacks import PageCB
from awgbot.bot.keyboards import common as kbm
from awgbot.bot.lists import page as pages

pytestmark = pytest.mark.unit

_TAGS = re.compile(r"<[^>]+>")


def _units(text: str) -> int:
    """Видимый текст в единицах UTF-16 — независимо от разбивателя."""
    return len(html.unescape(_TAGS.sub("", text)).encode("utf-16-le")) // 2


_PIECES = ["🛰", "🔗", "😀", "👨‍👩‍👧", "&amp;", "&lt;", "&gt;", "a", "Я", "10.240.1.0/24", " · ", "ё"]


def _row(rng: random.Random, i: int) -> str:
    """Строка с эмодзи вне BMP, сущностями и разметкой; номер — чтобы склейка
    различала строки."""
    body = "".join(rng.choice(_PIECES) for _ in range(rng.randint(1, rng.choice([5, 40, 200]))))
    return f"<code>{i}</code> {body} <a href=\"https://t.me/x?start=gw-{i}\">x</a>"


@pytest.mark.parametrize("seed", range(25))
def test_every_page_fits_the_budget_and_pages_glue_back_into_the_list(seed):
    """На случайных строках: каждая страница — не длиннее бюджета вместе с
    шапкой и разделителями и не больше 30 строк; страницы подряд — исходный
    список без потерь и повторов."""
    rng = random.Random(seed)
    rows = [_row(rng, i) for i in range(rng.randint(1, 260))]
    head = rng.choice([0, 40, 200])
    sep = rng.choice(["\n", "\n\n"])
    got = pages.split(rows, head, sep)
    assert sum(got, []) == rows, "страницы подряд — не исходный список"
    for n, chunk in enumerate(got):
        assert chunk, f"пустая страница {n}"
        assert len(chunk) <= pages.MAX_ROWS, f"страница {n}: {len(chunk)} строк"
        used = head + sum(_units(r) for r in chunk) + _units(sep) * (len(chunk) - 1)
        assert used <= pages.BUDGET, f"страница {n}: {used} единиц UTF-16 > {pages.BUDGET}"


def test_visible_counts_utf16_units_without_tags_and_with_entities_expanded():
    """Эмодзи вне BMP — две единицы (так считает Telegram), тег — ноль, «&amp;» — одна."""
    assert pages.visible("😀") == 2
    assert pages.visible("<b>ab</b>") == 2
    assert pages.visible("&amp;&lt;") == 2
    assert pages.visible('<a href="https://t.me/x?start=y">я</a>') == 1


def test_thirty_short_rows_fill_one_page_and_the_31st_opens_the_next():
    rows = [f"<code>r{i}</code>" for i in range(31)]
    got = pages.split(rows, 10)
    assert [len(c) for c in got] == [30, 1], [len(c) for c in got]


def test_a_row_longer_than_the_page_is_clipped_not_dropped():
    """Защитно: строка длиннее бюджета обрезается «…», а не роняет экран и не
    теряется; соседние строки — целиком."""
    rows = ["до", "x" * 5000, "после"]
    got = pages.split(rows, 20)
    flat = sum(got, [])
    assert flat[0] == "до" and flat[-1] == "после", flat
    assert flat[1].endswith("…") and _units(flat[1]) <= pages.BUDGET - 20, _units(flat[1])


def test_a_short_row_with_too_many_entities_loses_its_tags_without_a_false_ellipsis():
    """Строка короче бюджета, но с сущностями сверх предела страницы: теги
    снимаются, а «…» не ставится — ничего не отрезано."""
    row = " ".join(f"<code>a{i}</code>" for i in range(pages.MAX_ENTITIES + 1))
    got = sum(pages.split([row], 10), [])
    assert got == [html.escape(html.unescape(_TAGS.sub("", row)), quote=False)], got[0][-40:]
    assert not got[0].endswith("…")


def test_an_empty_list_is_one_empty_page():
    assert pages.split([], 10) == [[]]


# ── render: шапка, листание, зажим ──────────────────────────────────────────

_ROWS: list[str] = []


async def _build(services, ref, key, chat_id):
    return lists.View(title="🗺 Подсети NASPi &amp; co", rows=list(_ROWS), count=str(len(_ROWS)),
                      back=ui.back("m:main"))


@pytest.fixture()
def kind(monkeypatch):
    """Свой вид в реестре этой установки (основной бот, админ)."""
    from awgbot.bot import roles
    lists._load()
    monkeypatch.setitem(lists._REGISTRY, "zzpages", lists.Kind("zzpages", roles.current().name, ("admin",), _build))
    _ROWS.clear()
    yield "zzpages"
    _ROWS.clear()


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


async def test_a_single_page_has_no_page_counter_and_no_paging_row(kind, monkeypatch):
    _ROWS[:] = ([f"<code>10.0.{i}.0/24</code>" for i in range(5)])
    text, markup = await lists.render(None, kind, role="admin")
    assert "стр." not in text, text
    assert text.splitlines()[0] == "🗺 <b>Подсети NASPi &amp; co</b> · 5", text.splitlines()[0]
    assert _labels(markup) == ["⬅️ Назад"], _labels(markup)


async def test_many_pages_say_which_page_and_each_page_fits_telegram(kind, monkeypatch):
    rows = [f"<code>192.168.{i}.0/24</code> · в туннеле <code>10.240.{i}.0/24</code> 🛰" for i in range(64)]
    _ROWS[:] = (rows)
    seen: list[str] = []
    first, _ = await lists.render(None, kind, role="admin", page=0)
    total = int(re.search(r"стр\. 1 из (\d+)", first).group(1))
    assert total == 3, first.splitlines()[0]
    for p in range(total):
        text, markup = await lists.render(None, kind, role="admin", page=p)
        assert f"стр. {p + 1} из {total}" in text.splitlines()[0], text.splitlines()[0]
        assert _units(text) <= pages.BUDGET, f"стр. {p + 1}: {_units(text)}"
        body = text.split("\n\n", 1)[1].split("\n")
        assert len(body) <= pages.MAX_ROWS
        seen += body
        labels = _labels(markup)
        assert ("◀️ Пред. страница" in labels) == (p > 0), labels
        assert ("След. страница ▶️" in labels) == (p < total - 1), labels
        assert labels[-1] == "⬅️ Назад", labels
    assert seen == rows, "листанием видны не все строки или с повторами"


@pytest.mark.parametrize("asked, shown", [(99, 3), (-5, 1)])
async def test_a_page_out_of_range_is_clamped(kind, monkeypatch, asked, shown):
    """Список сократился между нажатиями — последняя страница, а не пустой экран."""
    _ROWS[:] = ([f"r{i}" for i in range(65)])
    text, _ = await lists.render(None, kind, role="admin", page=asked)
    assert f"стр. {shown} из 3" in text, text.splitlines()[0]


async def test_now_is_said_only_for_a_live_view_opened_from_a_notification(kind, monkeypatch):
    """«· сейчас» — данные на момент открытия, а не уведомления; на экране,
    открытом из карточки, это слово лишнее."""
    _ROWS[:] = (["r"])
    plain, _ = await lists.render(None, kind, role="admin", new=False)
    marked, _ = await lists.render(None, kind, role="admin", new=True)
    assert "сейчас" not in plain.splitlines()[0], plain
    assert marked.splitlines()[0].endswith(" · сейчас"), marked


async def test_another_role_or_an_unknown_kind_renders_nothing(kind, monkeypatch):
    """Чужая роль и незнакомый вид — None: вызывающий покажет главную, без
    слова о том, что такой список существует."""
    _ROWS[:] = (["r"])
    assert await lists.render(None, kind, role="client") is None
    assert await lists.render(None, kind, role="invited") is None
    assert await lists.render(None, "nosuchkind", role="admin") is None


async def _grouped(services, ref, key, chat_id):
    """Две группы: 40 имён без подписи и 40 адресов под подписью (как sshunres):
    якорь считается по началам групп, а не по подписям."""
    rows = [f"<code>h{i}.example</code>" for i in range(40)] + [ui.label("Держу")] + [f"<code>203.0.113.{i}</code>"
                                                                                       for i in range(40)]
    return lists.View(title="🛡 Не резолвятся", rows=rows, count="40", back=ui.back("m:main"),
                      labels=(40,), anchors=(0, 40))


@pytest.fixture()
def grouped(monkeypatch):
    from awgbot.bot import roles
    lists._load()
    monkeypatch.setitem(lists._REGISTRY, "zzgroups", lists.Kind("zzgroups", roles.current().name, ("admin",), _grouped,
                                                                anchored=True))
    return "zzgroups"


async def test_the_anchor_of_a_group_without_a_label_before_it_opens_its_page(grouped):
    """Хвост второй группы («держу прошлый адрес: … и ещё N») ведёт к странице с
    её подписью, хотя подпись у вида одна: якорь — номер группы, первая без
    подписи тоже считается."""
    text, _ = await lists.render(None, grouped, role="admin", anchor=2, chat_id=5)
    assert "стр. 2 из 3" in text.splitlines()[0], text.splitlines()[0]
    assert "<b>Держу:</b>" in text.split("\n\n", 1)[1], "на странице якоря нет подписи второй группы"
    from awgbot.bot import paging
    assert paging.page_of(5, lists.screen_name(grouped), 0) == 1, "страница якоря не запомнена"
    first, _ = await lists.render(None, grouped, role="admin", anchor=1, chat_id=5)
    assert "стр. 1 из 3" in first.splitlines()[0], first.splitlines()[0]


async def test_an_anchor_of_a_group_the_view_has_not_got_falls_back_to_the_remembered_page(grouped):
    from awgbot.bot import paging
    paging.remember(6, lists.screen_name(grouped), 0, 2)
    text, _ = await lists.render(None, grouped, role="admin", anchor=3, chat_id=6, page=2)
    assert "стр. 3 из 3" in text.splitlines()[0], text.splitlines()[0]


async def test_a_link_opens_the_first_page_without_growing_the_page_memory(kind, monkeypatch):
    """Ссылка /start несёт ref из ввода человека: открытие по ссылке сбрасывает
    страницу, но записи в памяти страниц на каждый ref не заводит (иначе
    «list-snap-1», «list-snap-2», … растили бы словарь процесса)."""
    from types import SimpleNamespace
    from awgbot.bot import paging
    from awgbot.bot.handlers import common
    from awgbot.util import listlink

    async def _delete():
        pass

    async def _shown(message, services, text, markup, new=False):
        pass

    monkeypatch.setattr(common, "show_parts", _shown)
    _ROWS[:] = [f"r{i}" for i in range(65)]
    message = SimpleNamespace(chat=SimpleNamespace(id=9), delete=_delete)
    paging.remember(9, lists.screen_name(kind), 777, 2)
    assert await lists.open_link(message, None, "admin", None, listlink.parse(f"list-{kind}-777"))
    assert paging.page_of(9, lists.screen_name(kind), 777) == 0, "по ссылке — не первая страница"
    assert (9, lists.screen_name(kind), 777) not in paging._pages, "открытие по ссылке завело запись в памяти"
    assert await lists.open_link(message, None, "admin", None, listlink.parse(f"list-{kind}-778"))
    assert (9, lists.screen_name(kind), 778) not in paging._pages


async def test_a_kind_of_the_other_installation_is_not_served(monkeypatch):
    """Виды агента у основного бота и виды основного у агента не открываются:
    данных под ними у этой установки нет."""
    from awgbot.core import config
    monkeypatch.setattr(config, "ROLE", "client")
    monkeypatch.setattr(config, "GUEST", False)
    assert lists.kind_for("gwnets", "admin") is not None
    assert lists.kind_for("sshfrom", "admin") is None
    monkeypatch.setattr(config, "ROLE", "gateway")
    assert lists.kind_for("sshfrom", "admin") is not None
    assert lists.kind_for("gwnets", "admin") is None


# ── paged: экран, который сам список, — страницей на месте ──────────────────

def _head(lbl: str) -> str:
    return ui.head("📶 Онлайн", value="61", meta=[lbl])


def test_paged_single_page_has_no_label_and_keeps_the_tail():
    pg = pages.paged(["a", "b"], 0, header=_head, sep="\n\n", gap="\n\n", tail="\n\nобновлено")
    assert pg.text == "📶 <b>Онлайн:</b> 61\n\na\n\nb\n\nобновлено", repr(pg.text)
    assert (pg.cur, pg.total, pg.start, pg.stop, pg.has_prev, pg.has_next) == (0, 1, 0, 2, False, False)


def test_paged_label_slice_and_tail_on_every_page():
    """Три страницы: «стр. k из 3» в шапке, срез [start, stop) — ровно строки
    этой страницы (по нему «🟡 Истекают» рисует кнопки), хвост экрана — на
    каждой; страницы подряд — весь список."""
    rows = [f"<code>r{i:02d}</code>" for i in range(61)]
    seen = []
    for k in range(3):
        pg = pages.paged(rows, k, header=_head, sep="\n", gap="\n\n", tail="\n\nконец")
        assert pg.text.splitlines()[0] == f"📶 <b>Онлайн:</b> 61 · стр. {k + 1} из 3", pg.text.splitlines()[0]
        assert pg.text.endswith("\n\nконец")
        body = pg.text.split("\n\n")[1].split("\n")
        assert body == rows[pg.start:pg.stop], f"стр. {k + 1}: срез {pg.start}:{pg.stop} — не строки страницы"
        seen += body
    assert seen == rows, "страницы подряд — не весь список"


@pytest.mark.parametrize("asked, cur", [(99, 2), (3, 2), (-1, 0), (None, 0)])
def test_paged_clamps_the_page_number(asked, cur):
    """Список сократился между нажатиями — последняя страница; мусор — первая."""
    rows = [f"r{i}" for i in range(61)]
    pg = pages.paged(rows, asked, header=_head)
    assert pg.cur == cur and f"стр. {cur + 1} из 3" in pg.text, (asked, pg.cur, pg.text.splitlines()[0])


def test_paged_counts_the_header_and_the_tail_into_the_budget():
    """Шапка и хвост экрана — в том же бюджете 3500: длинная строка «обновлено»
    не выталкивает страницу за предел Telegram."""
    rows = ["я" * 300 for _ in range(30)]
    tail = "\n\n" + "х" * 600
    for k in range(pages.paged(rows, 0, header=_head, tail=tail).total):
        pg = pages.paged(rows, k, header=_head, tail=tail)
        assert pages.visible(pg.text) <= pages.BUDGET, f"стр. {k + 1}: {pages.visible(pg.text)}"


def test_paged_label_room_is_reserved_before_the_split():
    """Метка «стр. 10 из 12» длиннее «стр. 1 из 2»: место под неё — заранее,
    иначе строка, впритык влезшая без метки, переполнит страницу с меткой."""
    row = "ж" * 119              # 29 строк впритык к бюджету без метки: метка «· стр. k из m» их переполнила бы
    rows = [row] * 400
    first = pages.paged(rows, 0, header=_head, sep="\n")
    assert first.total >= 10, first.total
    for k in range(first.total):
        pg = pages.paged(rows, k, header=_head, sep="\n")
        assert pages.visible(pg.text) <= pages.BUDGET, f"стр. {k + 1}: {pages.visible(pg.text)}"


# ── «🟡 Истекают»: кнопки «⏱» той же страницы, десять рядов ─────────────────

def _expiring_rows(n: int, name=lambda i: f"Профиль {i:02d}"):
    from types import SimpleNamespace
    from awgbot.util import timeutil
    import datetime as _dt
    base = _dt.datetime(2026, 9, 15, 12, 0, tzinfo=_dt.timezone.utc)
    return [(SimpleNamespace(id=100 + i, name=name(i), tg_id=7000 + i,
                             period_end=timeutil.to_iso(base + _dt.timedelta(hours=1 + i))), 3600 * (1 + i))
            for i in range(n)]


@pytest.mark.parametrize("n", [1, 15, 16, 17, 25, 61])
def test_expiring_page_fits_ten_rows_and_buttons_are_its_own_rows(n):
    """Страница «Истекают» — восемь рядов по две «⏱», ряд листания и «⬅️ В
    меню»: ровно десять, когда список длиннее страницы. Кнопки — те же
    профили, что строки этой страницы: продление с кнопки чужой страницы —
    подписка не того, кого человек видит."""
    from awgbot.bot import texts
    from awgbot.bot.callbacks import ClientCB
    from awgbot.bot.keyboards import admin as kba
    rows = _expiring_rows(n)
    total = texts.expiring_page(rows, "test_bot").total
    assert total == max(1, -(-n // texts.EXPIRING_PER_PAGE)), f"{n} профилей — {total} страниц"
    reached = []
    for p in range(total):
        pg = texts.expiring_page(rows, "test_bot", page=p)
        markup = kba.expiring_kb(rows, pg)
        kb_rows = markup.inline_keyboard
        assert len(kb_rows) <= kbm.MAX_ROWS, f"стр. {p + 1}: {len(kb_rows)} рядов"
        if total > 1 and pg.stop - pg.start == texts.EXPIRING_PER_PAGE:
            assert len(kb_rows) == kbm.MAX_ROWS, f"полная страница — не десять рядов: {len(kb_rows)}"
        ids = [ClientCB.unpack(b.callback_data).client_id for r in kb_rows for b in r
               if b.callback_data.startswith("c:extend_exp:")]
        shown = [int(m) for m in re.findall(r"start=cl-(\d+)", pg.text)]
        assert ids == shown, f"стр. {p + 1}: кнопки {ids}, строки {shown}"
        assert all(len(r) <= 2 for r in kb_rows), "«⏱» — не по две в ряд"
        assert [b.text for b in kb_rows[-1]] == ["⬅️ В меню"], kb_rows[-1]
        nav = [b.text for b in kb_rows[-2]] if total > 1 else []
        want = ([kbm.PREV_LABEL] if p > 0 else []) + ([kbm.NEXT_LABEL] if p < total - 1 else [])
        assert nav == want, f"стр. {p + 1}: листание {nav}"
        for b in kb_rows[-2] if total > 1 else []:
            cb = PageCB.unpack(b.callback_data)
            assert (cb.screen, cb.back) == ("expiring", "m:expiring"), cb
        reached += ids
    assert reached == [100 + i for i in range(n)], "листанием «⏱» видны не все профили или с повторами"



async def test_a_user_opening_snapshot_links_with_numbers_does_not_grow_the_page_memory(monkeypatch):
    """У пользователя «list-snap-<число>» — ввод человека, разбирается как вид
    snap со слотом: сотня таких /start (перебор, пересланные ссылки) не
    заводит в памяти процесса ни одной записи страниц — а ответ у каждой
    «📋 Этого списка больше нет»."""
    from types import SimpleNamespace
    from awgbot.bot import paging
    from awgbot.bot.handlers import common
    from awgbot.util import listlink

    shown = []

    async def _delete():
        pass

    async def _show(message, services, text, markup, new=False):
        shown.append(text)

    monkeypatch.setattr(common, "show_parts", _show)
    monkeypatch.setattr(paging, "_pages", {})
    message = SimpleNamespace(chat=SimpleNamespace(id=7101), delete=_delete)
    for n in range(1, 101):
        link = listlink.parse(f"list-snap-{n}")
        assert link is not None and link.ref == n, link
        assert await lists.open_link(message, None, "client", None, link), f"list-snap-{n}: вид snap у пользователя не открылся"
    assert paging._pages == {}, f"память страниц выросла на ссылках пользователя: {len(paging._pages)} записей"
    assert shown and set(shown) == {"📋 Этого списка больше нет"}, set(shown)


@pytest.fixture()
def agent(monkeypatch):
    from awgbot.core import config
    monkeypatch.setattr(config, "ROLE", "gateway")
    lists._load()


async def test_the_peers_anchor_of_sshfrom_opens_their_page_even_without_lan_subnets(agent):
    """«🛡 Из подсетей других шлюзов: … и ещё N» ведёт на «list-sshfrom-2»: когда
    подсетей локальной сети нет, их группа пустая, но номер группы в ссылке
    тот же — открывается первая страница с подписью подсетей шлюзов, а не
    страница, запомненная с прошлого раза."""
    from types import SimpleNamespace
    from awgbot.bot import paging
    services = SimpleNamespace(ssh_screen=lambda: {"lan": [], "peer_nets": [f"10.24{k}.{i}.0/24"
                                                                           for k in range(1, 3) for i in range(60)]})
    paging.remember(77, lists.screen_name("sshfrom"), 2, 3)
    text, _ = await lists.render(services, "sshfrom", role="admin", ref=2, anchor=2, chat_id=77, page=3)
    assert "стр. 1 из" in text.splitlines()[0], f"якорь пустой первой группы промахнулся: {text.splitlines()[0]}"
    assert "<b>Из подсетей других шлюзов:</b>" in text, text[:300]
    assert paging.page_of(77, lists.screen_name("sshfrom"), 2) == 0, "страница якоря не запомнена"


async def test_the_held_anchor_of_sshunres_without_held_addresses_keeps_the_page(agent):
    """Прошлых адресов уже нет (имена снова резолвятся), а ссылка «list-sshunres-2»
    в старом итоге осталась: группы нет — страница как без якоря, без падения."""
    from types import SimpleNamespace
    services = SimpleNamespace(ssh_screen=lambda: {"unresolved": [f"h{i:02d}.example" for i in range(80)], "held": []})
    text, _ = await lists.render(services, "sshunres", role="admin", ref=2, anchor=2, chat_id=78, page=1)
    assert "стр. 2 из" in text.splitlines()[0], text.splitlines()[0]
    assert "Держу прошлые адреса" not in text, text[:300]
