"""Источники снимков событий через настоящий диспетчер (снимки каталога
tests/screens/shots_snaps): итог, отчёт или уведомление, чей список не влез,
оставляют хвост «и ещё N» ссылкой на снимок; снимок открывается по ссылке
и несёт весь список теми же строками HTML, что у источника; «⬅️ Назад» —
на экран-источник; запись снимка не удалась — хвост остаётся текстом.

Здесь — утверждения о поведении, а не сверка с эталоном: какая ссылка в
тексте (с меткой «-n» у следов и уведомлений, без неё — у итогов на живом
меню), сколько строк в снимке и совпадают ли они со строками источника.

Цена ошибки: хвост без ссылки — остаток списка не увидеть нигде (отказы
ввода, недоставленные, потерявшие связь); ссылка без «-n» из уведомления —
перерисовывает живое меню где-то выше в чате, человек «нажал — ничего»;
строка снимка, собранная иначе, чем в источнике, — в списке другие имена
или неэкранированная разметка; сбой записи снимка, уронивший итог, — человек
не узнаёт, что вообще применилось.
"""
from __future__ import annotations

import re

import pytest

from awgbot.bot.keyboards.common import NEXT_LABEL
from tests.screens import shots_snaps as ss
from tests.screens.base import Shot
from tests.screens.harness import take

pytestmark = pytest.mark.screens

_BY_ID = {s.id: s for s in ss.SHOTS}
_LINK = re.compile(r'<a href="https://t\.me/test_bot\?start=(list-snap-[a-z0-9]{10}(?:-n)?)">([^<]+)</a>')


async def _rec(tmp_path, fakes, shot):
    return await take(shot, tmp_path, fakes)


def _bodies(rec) -> list[str]:
    return [c.body for c in rec.calls if c.body]


def _with(rec, needle: str) -> str:
    found = [b for b in _bodies(rec) if needle in b]
    assert found, f"нет сообщения с «{needle}»: {[b[:80] for b in _bodies(rec)]}"
    return found[-1]


def _screen(rec) -> tuple[str, list[tuple[str, str]]]:
    drawn = [c for c in rec.calls if c.body is not None and c.markup is not None]
    assert drawn, f"экран не нарисован: {[c.head for c in rec.calls]}"
    c = drawn[-1]
    return c.body, [(b.text, b.callback_data) for row in c.markup.inline_keyboard for b in row]


def _snap_rows(text: str) -> list[str]:
    """Строки снимка на странице: всё после шапки."""
    return [ln for ln in text.split("\n\n", 1)[1].split("\n") if ln]


async def _all_rows(tmp_path, fakes, shot: Shot) -> tuple[str, list[str]]:
    """Шапка первой страницы и строки всех страниц снимка — листанием его же кнопками."""
    steps = list(shot.all_steps())
    head, rows, n = "", [], 0
    while True:
        text, buttons = _screen(await _rec(tmp_path, fakes, Shot(f"{shot.id}.w{n}", role=shot.role, steps=steps,
                                                                    data=shot.data)))
        head = head or text.split("\n", 1)[0]
        rows += _snap_rows(text)
        nxt = [d for t, d in buttons if t == NEXT_LABEL]
        if not nxt:
            return head, rows
        steps.append(("press", nxt[0]))
        n += 1
        assert n < 12, "листание снимка не кончается"


def _unmark(html: str) -> str:
    """Ссылки на экраны без метки «-n»: в уведомлении она есть, на экране снимка — нет."""
    return re.sub(r'(\?start=[A-Za-z0-9_-]+?)-n"', r'\1"', html)


# ── «Свои сайты»: админ, пользователь, гость ───────────────────────────────

@pytest.mark.parametrize("report, opened, back", [
    ("adm.snap.sites.report", "adm.snap.sites.open", "adm.snap.sites.back"),
    ("cl.snap.sites.report", "cl.snap.sites.open", "cl.snap.sites.back"),
    ("gst.snap.sites.report", "gst.snap.sites.open", None),
])
async def test_sites_rejects_tail_links_to_a_snapshot_of_all_forty(tmp_path, frozen, fakes, report, opened, back):
    """40 отказов одной причины: в итоге пять и «и ещё 35» ссылкой без «-n» (итог — на
    живом меню); в снимке все 40 — «<code>адрес</code> — причина» той же причиной,
    первые пять — те же адреса; «⬅️ Назад» — «📋 Свои сайты»."""
    rec = await _rec(tmp_path, fakes, _BY_ID[report])
    body = _with(rec, "⚠️ Не добавлено:")
    line = next(ln for ln in body.split("\n") if ln.startswith("⚠️ Не добавлено:"))
    links = _LINK.findall(line)
    assert links == [("list-snap-snapkey001", "и ещё 35")], f"хвост отказов: {line}"
    reason = line.rsplit("</a> — ", 1)[1]
    shown = re.findall(r"<code>([^<]+)</code>", line)
    head, rows = await _all_rows(tmp_path, fakes, _BY_ID[opened])
    assert head.startswith("⚠️ <b>Не добавлено</b> · 40 · стр. 1 из 2 · на "), head
    assert rows == [f"<code>bad{i:02d}..ru</code> — {reason}" for i in range(40)], \
        f"строки снимка не те, что у итога: {rows[:3]}"
    assert [re.search(r"<code>([^<]+)</code>", r).group(1) for r in rows[:5]] == shown, "первые пять — не те же"
    if back:
        text, _ = _screen(await _rec(tmp_path, fakes, _BY_ID[back]))
        assert text.startswith("📋 <b>Свои сайты</b>"), f"«⬅️ Назад» привела не на «Свои сайты»: {text[:80]}"


async def test_sites_report_keeps_a_text_tail_when_the_snapshot_is_not_written(tmp_path, frozen, fakes):
    rec = await _rec(tmp_path, fakes, _BY_ID["adm.snap.sites.failed"])
    body = _with(rec, "⚠️ Не добавлено:")
    assert "list-snap-" not in body, "ссылка на снимок, которого нет"
    assert re.search(r"</code> и ещё 35 — ", body), f"хвост не текстом: {body[:400]}"
    assert "Свои сайты" in body, "итог пришёл без экрана — сбой снимка уронил ответ"


# ── «🗺 Подсети»: 10 отказов ────────────────────────────────────────────────

async def test_subnet_rejects_tail_links_to_a_snapshot_named_after_the_gateway(tmp_path, frozen, fakes):
    rec = await _rec(tmp_path, fakes, _BY_ID["adm.snap.lan.report"])
    body = _with(rec, "⚠️ Не принято:")
    items = [ln for ln in body.split("\n") if ln.startswith("• ")]
    assert len(items) == 4 and _LINK.findall(items[-1]) == [("list-snap-snapkey001", "и ещё 7")], items
    text, buttons = _screen(await _rec(tmp_path, fakes, _BY_ID["adm.snap.lan.open"]))
    assert text.split("\n", 1)[0].startswith("⚠️ <b>Подсети NASPi: не принято</b> · 10 · на "), \
        f"в заголовке нет имени устройства шлюза: {text[:100]}"
    rows = _snap_rows(text)
    assert len(rows) == 10 and ["• " + r for r in rows[:3]] == items[:3], \
        f"строки снимка не те, что в итоге:\n{rows[:3]}\n{items[:3]}"
    assert [t for t, _ in buttons] == ["⬅️ Назад"], buttons
    card, _ = _screen(await _rec(tmp_path, fakes, _BY_ID["adm.snap.lan.back"]))
    assert card.startswith("🛰 <b>NASPi</b>"), f"«⬅️ Назад» — не карточка слота: {card[:80]}"


async def test_subnet_rejects_keep_a_text_tail_when_the_snapshot_is_not_written(tmp_path, frozen, fakes):
    body = _with(await _rec(tmp_path, fakes, _BY_ID["adm.snap.lan.failed"]), "⚠️ Не принято:")
    assert "list-snap-" not in body and body.count("\n• и ещё 7") == 1, body[:600]


# ── отчёт рассылки ──────────────────────────────────────────────────────────

async def test_the_broadcast_report_links_both_tails_with_the_new_mark(tmp_path, frozen, fakes):
    """Отчёт — след события: оба хвоста ссылками с «-n»; в снимке недоставленных —
    все 30 «профиль Имя» (ссылки без метки — это уже экран), первые три — те же, что в
    отчёте; в снимке адресатов — все 32."""
    rec = await _rec(tmp_path, fakes, _BY_ID["adm.snap.bc.report"])
    body = _with(rec, "✅ Разослано:")
    assert ("list-snap-snapkey001-n", "и ещё 20") in _LINK.findall(body), "хвост адресатов — не ссылка с «-n»"
    und = next(ln for ln in body.split("\n") if ln.startswith("⚠️ Не доставлено:"))
    assert _LINK.findall(und)[-1] == ("list-snap-snapkey002-n", "и ещё 27"), und
    named = re.findall(r'<a href="[^"]+">Подписчик \d\d</a>', und)
    head, rows = await _all_rows(tmp_path, fakes, _BY_ID["adm.snap.bc.undelivered"])
    assert head.startswith("⚠️ <b>Не доставлено</b> · 30 · на "), head
    assert len(rows) == 30 and all(r.startswith("профиль <a href=") for r in rows), rows[:3]
    assert ["профиль " + _unmark(n) for n in named] == rows[:3], f"{named} ≠ {rows[:3]}"
    assert not any("-n\"" in r for r in rows), "ссылки на профили в снимке — с меткой уведомления"
    head, rows = await _all_rows(tmp_path, fakes, _BY_ID["adm.snap.bc.audience"])
    assert head.startswith("📢 <b>Адресаты рассылки</b> · 32 · стр. 1 из 2 · на "), head
    assert len(rows) == 32, f"в снимке адресатов {len(rows)}"


async def test_the_broadcast_report_keeps_text_tails_when_snapshots_fail(tmp_path, frozen, fakes):
    body = _with(await _rec(tmp_path, fakes, _BY_ID["adm.snap.bc.failed"]), "✅ Разослано:")
    assert "list-snap-" not in body, body[:300]
    assert "</a> [+20] — доставлено" in body and "</a> и ещё 27 — бот заблокирован" in body, body


def _bc_guests(services, mp):
    """Как в снимке отчёта: 30 подписчиков не получили объявление, и двое гостей (те,
    с кем Ксюша делится «Ноутбуком» и «Телефоном») — тоже."""
    from awgbot.bot.handlers.admin import broadcast as bc
    from tests.screens.shots_admin_common import LAPTOP, PHONE
    who = ss._bc_thirty(services, mp)
    for dev, tg in ((LAPTOP, 2050), (PHONE, 2051)):
        assert services.activate_friend(services.make_device_friendly(dev), tg_id=tg).ok
    inner = bc.broadcast

    async def broadcast(bot, tg_ids, text, photos=(), by_tg=None):
        ok, failed = await inner(bot, [t for t in tg_ids if t not in (2050, 2051)], text, photos, by_tg)
        return ok, list(failed) + [t for t in tg_ids if t in (2050, 2051)]
    mp.setattr(bc, "broadcast", broadcast)
    return who


async def test_guests_without_a_profile_are_one_line_of_the_undelivered_snapshot(tmp_path, frozen, fakes):
    """Двое гостей не получили объявление: в снимке недоставленных — 30 профилей и одна
    строка «2 адресата без профиля…»; строки «полный список в журнале бота» нет — в
    снимок вошли все, журнал ничего не хранит."""
    steps = [*ss._BC, ss.snap("snapkey002", new=True)]
    head, rows = await _all_rows(tmp_path, fakes, Shot("bc.guests", role="admin", steps=steps, data=_bc_guests))
    assert re.match(r"⚠️ <b>Не доставлено</b> · 32 · (стр\. 1 из 2 · )?на ", head), head
    assert rows[-1] == "2 адресата без профиля — те, с кем делятся устройствами", \
        f"последняя строка снимка: {rows[-1]!r}"
    assert not any("полный список в журнале бота" in r for r in rows), \
        "в снимке строка «в журнале бота», хотя список вошёл целиком (гости — одной строкой)"


# ── итог завершения переезда ────────────────────────────────────────────────

async def test_the_migration_finish_trace_links_all_fifty_dropped_with_the_new_mark(tmp_path, frozen, fakes):
    rec = await _rec(tmp_path, fakes, _BY_ID["adm.snap.mig.finish"])
    body = _with(rec, "Потеряли связь (50)")
    assert _LINK.findall(body) == [("list-snap-snapkey001-n", "и ещё 30")], body[:800]
    shown = body.split("Потеряли связь (50): ", 1)[1].split(" <a ", 1)[0].split(", ")
    text, buttons = _screen(await _rec(tmp_path, fakes, _BY_ID["adm.snap.mig.open"]))
    assert text.startswith("⚠️ <b>Потеряли связь при завершении переезда</b> · 50 · стр. 1 из 2"), text[:120]
    rows = _snap_rows(text)
    assert rows[:20] == shown, "первые двадцать в снимке — не те, что в итоге"
    assert all(re.fullmatch(r"Профиль \d\d — Тел \d\d", r) for r in rows), f"не «профиль — устройство»: {rows[:3]}"
    assert buttons[-1] == ("⬅️ В меню", "m:main"), buttons


async def test_the_migration_finish_trace_carries_the_new_mark_as_a_notice_does(tmp_path, frozen, fakes, monkeypatch):
    """Метка «-n» на ссылке следа ставится тем же путём, что у уведомлений и отчёта
    рассылки (links.mark_new): иначе сторож меток эталона считает её меткой на экране."""
    from tests.screens import marks
    notified = marks.record(monkeypatch)
    rec = await _rec(tmp_path, fakes, _BY_ID["adm.snap.mig.finish"])
    assert marks.problems("adm.snap.mig.finish", rec, notified) == [], \
        "ссылка следа переезда с «-n» собрана мимо links.mark_new"


# ── «➕ Адрес» SSH-доступа: сервер и агент ─────────────────────────────────

@pytest.mark.parametrize("opened, back, back_title", [
    ("adm.snap.fw.open", "adm.snap.fw.back", "🛡 <b>SSH-доступ</b>"),
    ("gw.snap.ssh.open", "gw.snap.ssh.back", "🛡 <b>SSH-доступ</b>"),
])
async def test_ssh_allow_added_tail_opens_all_fifteen(tmp_path, frozen, fakes, opened, back, back_title):
    shot = _BY_ID[opened]
    report = Shot(f"{opened}.report", role=shot.role, steps=shot.all_steps()[:-1], data=shot.data)
    body = _with(await _rec(tmp_path, fakes, report), "✅ Адреса для SSH-доступа: добавлено")
    assert _LINK.findall(body) == [("list-snap-snapkey001", "и ещё 3")], body[:900]
    text, _ = _screen(await _rec(tmp_path, fakes, shot))
    assert text.startswith("✅ <b>Добавлено для SSH-доступа</b> · 15 · на "), text[:100]
    assert _snap_rows(text) == [f"<code>198.51.100.{n}</code>" for n in range(1, 16)], _snap_rows(text)
    assert re.findall(r"<code>[^<]+</code>", body.split("\n")[0]) == _snap_rows(text)[:12], "первые двенадцать — не те"
    after, _ = _screen(await _rec(tmp_path, fakes, _BY_ID[back]))
    assert after.startswith(back_title), after[:80]


# ── агент: «➕ В туннель» на 400 доменов ────────────────────────────────────

async def test_the_transit_result_tail_opens_every_skipped_line(tmp_path, frozen, fakes):
    rec = await _rec(tmp_path, fakes, _BY_ID["gw.snap.transit"])
    body = _with(rec, "✅ Добавлено: 200")
    note = body.split("\n\n", 1)[0].split("\n")
    assert _LINK.findall(note[-1]) == [("list-snap-snapkey001", "и ещё 133 строки")], note[-1]
    shown = note[1:-1]
    head, rows = await _all_rows(tmp_path, fakes, _BY_ID["gw.snap.transit.open"])
    assert head.startswith("🔀 <b>Итог: в туннель</b> · 200 · стр. 1 из "), head
    assert rows == [f"⚠️ <code>bad-name-{i:03d}</code>: не домен, пропущен" for i in range(200)], rows[:2]
    assert rows[:len(shown)] == shown and len(shown) + 133 == 200, "строки снимка не те, что в итоге"
    lan, _ = _screen(await _rec(tmp_path, fakes, _BY_ID["gw.snap.transit.back"]))
    assert lan.startswith("🔀 <b>VPN-транзит</b>"), lan[:80]


# ── Т-Ж4 и отчёт восстановления: уведомления ────────────────────────────────

@pytest.mark.parametrize("notice, opened, title", [
    ("adm.snap.tj4.crash", "adm.snap.tj4.crash.open", "⚠️ <b>Не перенесены изменения, сделанные здесь перед переездом на «резерв»</b>"),
    ("adm.snap.tj4.c9", "adm.snap.tj4.c9.open", "⚠️ <b>Не перенесены изменения от бота на «резерв»</b>"),
    ("adm.snap.restore", "adm.snap.restore.open", "⚠️ <b>Не восстановлено из бэкапа</b>"),
])
async def test_a_notice_shows_five_lines_and_links_the_rest_with_the_new_mark(tmp_path, frozen, fakes, notice,
                                                                             opened, title):
    rec = await _rec(tmp_path, fakes, _BY_ID[notice])
    body = _with(rec, "list-snap-")
    lines = body.split("\n")
    items = [ln for ln in lines if ln.startswith("• ")]
    total = 9 if "tj4" in notice else 8
    assert len(items) == 5, f"в уведомлении {len(items)} строк, а не пять"
    assert lines[-1] == f'… <a href="https://t.me/test_bot?start=list-snap-snapkey001-n">и ещё {total - 5}</a>', \
        lines[-1]
    text, buttons = _screen(await _rec(tmp_path, fakes, _BY_ID[opened]))
    assert text.startswith(f"{title} · {total} · на "), text[:160]
    rows = _snap_rows(text)
    assert len(rows) == total and ["• " + r for r in rows[:5]] == items, \
        f"строки снимка — не строки уведомления без «• »:\n{rows[:2]}\n{items[:2]}"
    assert buttons == [("⬅️ В меню", "m:main")], buttons


async def test_a_tj4_with_five_disputes_has_no_tail_and_no_snapshot(tmp_path, frozen, fakes):
    async def fire(services, bot):
        from awgbot.bot.notifier import send_notifications
        await send_notifications(bot, services._tail_notes("crash", 1, [dict(d) for d in ss.DISPUTED9[:5]], 5))

    async def count(services, bot):
        n = services.db._connection().execute(
            "SELECT COUNT(*) FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchone()[0]
        await bot.send_message(1, f"снимков: {n}")
    rec = await _rec(tmp_path, fakes, Shot("tj4.five", role="admin", data=ss._people,
                                           steps=[("call", fire, "Т-Ж4"), ("call", count, "счёт")]))
    assert _bodies(rec)[-1] == "снимков: 0", "снимок при пяти спорных — хвоста нет, снимок лишний"


# ── мелочи: хвоста нет — снимка нет ─────────────────────────────────────────

async def test_five_rejects_of_a_reason_make_no_snapshot(tmp_path, frozen, fakes):
    from awgbot.bot.callbacks import RoutingCB
    from tests.screens.shots_admin_common import KS

    async def count(services, bot):
        n = services.db._connection().execute(
            "SELECT COUNT(*) FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchone()[0]
        await bot.send_message(1, f"снимков: {n}")
    five = "\n".join(f"bad{i}..ru" for i in range(5))
    rec = await _rec(tmp_path, fakes, Shot("sites.five", role="admin", data=ss._ks_sites, steps=[
        ("press", RoutingCB(action="sites", ref=KS)), ("press", RoutingCB(action="add", ref=KS)), ("text", five),
        ("call", count, "счёт")]))
    assert _bodies(rec)[-1] == "снимков: 0", "пять отказов показаны все — снимок лишний"

