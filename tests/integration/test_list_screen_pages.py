"""
Экраны, которые сами список, листаются на месте: «📶 Онлайн», «🟡 Истекают»,
«📊 Трафик» по профилям и по устройствам профиля, «🚚 Переезд» (обзор и
профиль), «🚚 Ещё не переехали», «⚠️ Переехали до отмены». Реальная БД и
фейковый awg-слой: строки собирают те же сервисы, что в бою.

Каждый экран — на 1, 25 и 61 запись: одна страница без «стр.», граница
прежнего предела в 25 строк (теперь — одна страница, «… и ещё N» нет), три
страницы. Страницы подряд — весь список без потерь и повторов; каждая —
в бюджете 3500 единиц видимого текста и 90 сущностей разметки; номер за
концом списка зажимается к последней странице.

Цена ошибки: запись, выпавшая на стыке страниц, — устройство или профиль,
которого админ не увидит вовсе (хвоста «и ещё N» больше нет, догадаться не
по чему); страница длиннее 4096 знаков или с лишними сущностями — отказ
Bot API, экран не открывается; номер страницы за концом после сокращения
списка — пустой экран вместо последней страницы.
"""
from __future__ import annotations

import datetime as _dt
import re

import pytest

from awgbot.bot import texts
from awgbot.bot.lists import page as pages
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

GB = 1024 ** 3
BOT = "test_bot"
# сколько записей → сколько страниц: 30 строк на страницу; у «Истекают» —
# 16 (столько кнопок «⏱» влезает в десять рядов)
SIZES = [(1, 1), (25, 1), (61, 3)]
SIZES_EXPIRING = [(1, 1), (25, 2), (61, 4)]
_LABEL = re.compile(r"стр\. (\d+) из (\d+)")


# ── построители состояния ────────────────────────────────────────────────────

def _crowd(services, n: int, *, online: bool = False, traffic: bool = False) -> list[tuple[int, int]]:
    """n профилей «Профиль NN» по одному устройству «Тел NN»; online —
    устройства на связи; traffic — у каждого свой трафик за месяц (порядок
    по убыванию однозначен). → [(профиль, устройство)]."""
    now = int(timeutil.now().timestamp())
    out = []
    for i in range(n):
        created = services.create_client(f"Профиль {i:02d}", 1, "month")
        res = services.activate_client(created.invite_code, 50_000 + i)
        assert res.ok, res.reason
        dev = services.add_device(created.client_id, f"Тел {i:02d}")
        out.append((created.client_id, dev.device_id))
    if online:
        for _c, d in out:
            services.db.update_device_fields(d, last_handshake=now)
    if traffic:
        services.db.add_traffic_bulk([(d, (i + 1) * GB // 10, GB // 100) for i, (_c, d) in enumerate(out)])
    return out


def _one_with_devices(services, n: int, *, traffic: bool = False, live: bool = False) -> int:
    """Один профиль «Ксюша» с n устройствами «Тел NN»."""
    created = services.create_client("Ксюша", n, "year")
    assert services.activate_client(created.invite_code, 49_999).ok
    ids = [services.add_device(created.client_id, f"Тел {i:02d}").device_id for i in range(n)]
    if traffic:
        services.db.add_traffic_bulk([(d, (i + 1) * GB // 10, GB // 100) for i, d in enumerate(ids)])
    if live:
        hour_ago = int(timeutil.now().timestamp()) - 3600
        for d in ids:
            services.db.update_device_fields(d, last_handshake=hour_ago)
    return created.client_id


def _expiring(services, n: int) -> None:
    """n месячных профилей, у каждого подписка кончается в свой час (до двух суток)."""
    now = timeutil.now()
    for i in range(n):
        created = services.create_client(f"Профиль {i:02d}", 1, "month")
        assert services.activate_client(created.invite_code, 50_000 + i).ok
        services.db.update_client_fields(
            created.client_id, period_start=timeutil.to_iso(now - _dt.timedelta(days=29)),
            period_end=timeutil.to_iso(now + _dt.timedelta(hours=2, minutes=30 * i)))


# ── обход страниц ────────────────────────────────────────────────────────────

def _walk(render) -> list:
    """Все страницы экрана подряд: render(page) → Page. Каждая страница —
    своё место в листании, метка «стр. k из m» — только при m > 1,
    видимый текст и сущности разметки — в бюджете."""
    first = render(0)
    out = [first]
    for k in range(1, first.total):
        out.append(render(k))
    for k, pg in enumerate(out):
        assert (pg.cur, pg.total) == (k, first.total), f"страница {k}: cur={pg.cur} total={pg.total}"
        assert pg.has_prev == (k > 0) and pg.has_next == (k < first.total - 1), \
            f"страница {k} из {first.total}: листание назад={pg.has_prev}, вперёд={pg.has_next}"
        label = _LABEL.findall(pg.text)
        if first.total > 1:
            assert label == [(str(k + 1), str(first.total))], f"страница {k}: метка {label} в шапке"
            assert pg.text.split("\n", 1)[0].count("стр.") == 1, "метка страницы — не в первой строке шапки"
        else:
            assert not label, f"одна страница, а в шапке «стр.»: {pg.text[:120]!r}"
        assert pages.visible(pg.text) <= pages.BUDGET, \
            f"страница {k}: {pages.visible(pg.text)} единиц UTF-16 > {pages.BUDGET}"
    return out


def _glued(pgs, pattern: str) -> list[str]:
    """Записи всех страниц подряд — по метке «NN» в имени."""
    return [m for pg in pgs for m in re.findall(pattern, pg.text)]


def _expect(n: int) -> list[str]:
    return [f"{i:02d}" for i in range(n)]


# ── «📶 Онлайн» ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, total", SIZES)
def test_online_pages_show_every_device_once(services, n, total):
    """Онлайн по страницам: всего страниц — по числу устройств, все устройства
    на своих местах (порядок сервиса — по имени профиля), «и ещё N» нет."""
    _crowd(services, n, online=True)
    rows = services.online_devices()
    refreshed = texts.refreshed_line(BOT, texts.ONLINE_PAYLOAD, timeutil.now())
    pgs = _walk(lambda p: texts.online_devices_page(rows, BOT, refreshed=refreshed, page=p))
    assert len(pgs) == total, f"{n} устройств — {len(pgs)} страниц, ждали {total}"
    assert _glued(pgs, r"Тел (\d\d)") == _expect(n), "устройства потеряны или повторены на стыке страниц"
    assert all("и ещё" not in pg.text for pg in pgs), "хвост «… и ещё N» вернулся"
    assert all(pg.text.endswith(refreshed) for pg in pgs), "строка «обновлено … 🔄» — не последней на странице"


def test_online_page_beyond_the_end_is_clamped_to_the_last(services):
    """Список сократился между нажатиями: страница 3 из прежних трёх — теперь
    последняя из двух, а не пустой экран."""
    _crowd(services, 61, online=True)
    for _d, _c in services.online_devices()[31:]:
        services.db.update_device_fields(_d.id, last_handshake=0)
    rows = services.online_devices()
    assert len(rows) == 31
    pg = texts.online_devices_page(rows, BOT, page=2)
    assert (pg.cur, pg.total) == (1, 2), f"зажим: cur={pg.cur} total={pg.total}"
    assert re.findall(r"Тел (\d\d)", pg.text) == ["29", "30"], "последняя страница — не та"
    assert texts.online_devices_page(rows, BOT, page=-3).cur == 0, "отрицательный номер не зажат к первой"


def test_online_empty_is_one_line_without_paging(services):
    pg = texts.online_devices_page([], BOT)
    assert (pg.cur, pg.total, pg.has_prev, pg.has_next) == (0, 1, False, False)
    assert "Сейчас никто не подключён" in pg.text and "стр." not in pg.text


def test_online_full_page_stays_within_the_markup_entity_limit(services):
    """30 строк по три сущности (устройство, профиль, адрес) — 90; шапка
    (жирное) и строка «обновлено … 🔄» (ссылка) несут свои. Предел 90 —
    на всю страницу: больше Bot API на стенде не проверяли."""
    _crowd(services, 61, online=True)
    rows = services.online_devices()
    refreshed = texts.refreshed_line(BOT, texts.ONLINE_PAYLOAD, timeutil.now())
    for p in range(3):
        pg = texts.online_devices_page(rows, BOT, refreshed=refreshed, page=p)
        assert pages.entities(pg.text) <= pages.MAX_ENTITIES, \
            f"стр. {p + 1}: {pages.entities(pg.text)} сущностей разметки > {pages.MAX_ENTITIES}"


# ── «🟡 Истекают» ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, total", SIZES_EXPIRING)
def test_expiring_pages_hold_sixteen_profiles_and_show_everyone_once(services, n, total):
    """Истекают по 16 на страницу — столько кнопок «⏱» влезает в десять
    рядов; ближайшие сверху, все на своих местах."""
    _expiring(services, n)
    rows = services.expiring_subscriptions()
    assert len(rows) == n
    pgs = _walk(lambda p: texts.expiring_page(rows, BOT, page=p))
    assert len(pgs) == total, f"{n} профилей — {len(pgs)} страниц, ждали {total}"
    assert all(pg.stop - pg.start <= texts.EXPIRING_PER_PAGE for pg in pgs), \
        [(pg.start, pg.stop) for pg in pgs]
    assert _glued(pgs, r"Профиль (\d\d)") == _expect(n), "профили потеряны или повторены на стыке"
    assert [pg.start for pg in pgs] == [16 * k for k in range(total)], "срез страницы разошёлся с её строками"


def test_expiring_empty_has_no_buttons_but_the_menu(services):
    from awgbot.bot import keyboards as kb
    pg = texts.expiring_page([], BOT)
    assert "Истекающих подписок нет" in pg.text and "стр." not in pg.text
    assert [b.text for row in kb.expiring_kb([], pg).inline_keyboard for b in row] == ["⬅️ В меню"]


# ── «📊 Трафик» ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n, total", SIZES)
def test_traffic_by_profile_pages_show_every_profile_once(services, n, total):
    """Трафик по профилям: по убыванию, все профили с трафиком на своих
    местах; шапка с итогом — на каждой странице, метка — в её первой строке."""
    _crowd(services, n, traffic=True)
    rows = services.traffic_by_profile()
    pgs = _walk(lambda p: texts.traffic_profiles_page(rows, BOT, (n * GB, 0), page=p))
    assert len(pgs) == total, f"{n} профилей — {len(pgs)} страниц, ждали {total}"
    assert _glued(pgs, r"Профиль (\d\d)") == list(reversed(_expect(n))), "профили потеряны, повторены или не по убыванию"
    assert all(pg.text.startswith("📊 <b>Трафик за ") for pg in pgs), "шапка не на каждой странице"
    assert all("и ещё" not in pg.text for pg in pgs)


@pytest.mark.parametrize("n, total", SIZES)
def test_traffic_by_device_pages_show_every_device_once(services, n, total):
    cid = _one_with_devices(services, n, traffic=True)
    client = services.db.get_client(cid)
    rows = services.traffic_by_device(cid)
    pgs = _walk(lambda p: texts.traffic_devices_page(client, rows, (n * GB, 0), bot_username=BOT, page=p))
    assert len(pgs) == total, f"{n} устройств — {len(pgs)} страниц, ждали {total}"
    assert _glued(pgs, r"Тел (\d\d)") == list(reversed(_expect(n))), "устройства потеряны, повторены или не по убыванию"
    assert all("профиль" in pg.text.split("\n", 1)[0] for pg in pgs), "имя профиля пропало из шапки страницы"


def test_traffic_page_beyond_the_end_is_clamped(services):
    _crowd(services, 61, traffic=True)
    rows = services.traffic_by_profile()
    pg = texts.traffic_profiles_page(rows, BOT, (GB, 0), page=99)
    assert (pg.cur, pg.total) == (2, 3) and re.findall(r"Профиль (\d\d)", pg.text) == ["00"], pg.text[-200:]


# ── «🚚 Переезд» и списки переезда ───────────────────────────────────────────

def _running(services, profiles: int) -> None:
    """Переезд идёт: profiles профилей по одному устройству, все недавно активны."""
    hour_ago = int(timeutil.now().timestamp()) - 3600
    for _c, d in _crowd(services, profiles):
        services.db.update_device_fields(d, last_handshake=hour_ago)
    services.migration_start()


@pytest.mark.parametrize("n, total", SIZES)
def test_migration_overview_pages_show_every_profile_once(services, mig, n, total):
    _running(services, n)
    d = services.migration_overview()
    pgs = _walk(lambda p: texts.migration_overview_page(d, BOT, page=p))
    assert len(pgs) == total, f"{n} профилей — {len(pgs)} страниц, ждали {total}"
    assert sorted(_glued(pgs, r"Профиль (\d\d)")) == _expect(n), "профили потеряны или повторены на стыке"
    assert all(pg.text.startswith("🚚 <b>Переезд</b>") for pg in pgs)


@pytest.mark.parametrize("n, total", SIZES)
def test_migration_client_pages_show_every_device_once(services, mig, n, total):
    cid = _one_with_devices(services, n, live=True)
    services.migration_start()
    client = services.db.get_client(cid)
    rows = services.migration_client_devices(cid)
    pgs = _walk(lambda p: texts.migration_client_page(client, rows, BOT, page=p))
    assert len(pgs) == total, f"{n} устройств — {len(pgs)} страниц, ждали {total}"
    assert _glued(pgs, r"Тел (\d\d)") == _expect(n), "устройства потеряны или повторены на стыке"
    assert all(f"0 из {n} " in pg.text.split("\n", 1)[0] for pg in pgs), "счёт профиля пропал из шапки страницы"


@pytest.mark.parametrize("n, total", SIZES)
def test_migration_pending_pages_show_everyone_once(services, mig, n, total):
    """«🚚 Ещё не переехали»: подсказка «Каждому нужно заново получить
    конфигурацию» — на каждой странице, иначе на второй непонятно, что делать."""
    _running(services, n)
    rows = services.migration_pending()
    pgs = _walk(lambda p: texts.migration_pending_page(rows, page=p))
    assert len(pgs) == total, f"{n} устройств — {len(pgs)} страниц, ждали {total}"
    assert _glued(pgs, r"Тел (\d\d)") == _expect(n), "устройства потеряны или повторены на стыке"
    assert all(pg.text.endswith("прежняя работает до завершения переезда") for pg in pgs)
    assert all(f"({n})" in pg.text.split("\n", 1)[0] for pg in pgs), "общее число пропало из шапки страницы"


@pytest.mark.parametrize("n, total", SIZES)
def test_migration_orphans_pages_show_everyone_once(services, mig, n, total):
    _running(services, n)
    now = int(timeutil.now().timestamp())
    for twin in services.db.twins_by_origin().values():
        services.db.update_device_fields(twin, last_handshake=now)
    services.migration_cancel()
    rows = services.migration_orphan_rows()
    assert len(rows) == n
    pgs = _walk(lambda p: texts.migration_orphans_page(rows, page=p))
    assert len(pgs) == total, f"{n} устройств — {len(pgs)} страниц, ждали {total}"
    assert sorted(_glued(pgs, r"Тел (\d\d)")) == _expect(n), "устройства потеряны или повторены на стыке"
    assert all(pg.text.endswith("перенастрой их на старый") for pg in pgs)
