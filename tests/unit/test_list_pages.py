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
