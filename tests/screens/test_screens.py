"""Эталоны экранов: каждый снимок каталога — через настоящий диспетчер роли,
запись сравнивается с tests/screens/<роль>.txt целиком (один тест на файл:
под xdist каждый файл пишет ровно один процесс, дифф падения — весь файл).

Цена ошибки: обработчик, потерявший кнопку при переносе (порядок роутеров,
фильтр, упаковка колбэка), не виден e2e-тестам, которые зовут его напрямую, —
у человека кнопка уходит в «Кнопка устарела». Текст, разметка или ряд
кнопок, тихо съехавшие в рефакторинге, видны здесь диффом эталона. Лимит
Telegram (4096, 64 байта колбэка, 200 знаков всплывашки) или битый HTML —
это отказ Bot API на живом экране, и человек остаётся без ответа.

Перезапись: pytest tests/screens --update-screens.
"""
from __future__ import annotations

import difflib
import inspect
import json
import os
import pathlib
import time

import pytest

from tests.screens import catalog, harness, marks, timelabels
from tests.screens.harness import take

pytestmark = pytest.mark.screens

HERE = pathlib.Path(__file__).parent
FILES = sorted({s.role for s in catalog.SHOTS})


def _section_roles():
    from awgbot.bot import roles
    # файл эталона → словарь роли, чьи общие разделы он обязан снять целиком
    return {"admin": roles.MAIN, "gateway": roles.GATEWAY}


SECTION_ROLE = _section_roles()


def _expected_labels(shot) -> set[str]:
    """Метка, которую ждёт сторож: пояс снимка «другой пояс» (base.in_zone) или
    пояс тестов — в момент часов снимка."""
    from zoneinfo import ZoneInfo
    from awgbot.core import config
    from awgbot.util import timeutil
    zone = getattr(shot.data, "zone", None)
    return {timeutil.tz_label(catalog.NOW, tz=ZoneInfo(zone) if zone else config.TZ)}


# ── вызванные построители: сверка эталона отдаёт их сторожу полноты ─────────
# Сторож полноты не снимает каталог второй раз: каждый элемент сверки пишет,
# какие построители вызвали его снимки, в каталог сеанса — общий для всех
# процессов xdist (родитель basetemp воркера) или basetemp без xdist. Файл
# «<роль>.started» — сверка роли началась, «<роль>.json» — дошла до конца
# (список вызванных), «<роль>.failed» — оборвалась раньше: её список неполон.
BUILDERS_START_S = 120   # сверка роли в сеансе есть, но не началась (упала подготовка)
BUILDERS_WAIT_S = 600    # началась и не кончилась (процесс xdist умер)


def _builders_dir(factory) -> pathlib.Path:
    base = factory.getbasetemp()
    path = (base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base) / "screens-builders"
    path.mkdir(exist_ok=True)
    return path


def _builders_write(folder: pathlib.Path, name: str, seen: set[str]) -> None:
    # через переименование: читатель в другом процессе не увидит половину файла
    tmp = folder / f"{name}.json.part"
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    tmp.replace(folder / f"{name}.json")


def _builders_read(folder: pathlib.Path, name: str, scheduled: bool) -> set[str] | None:
    """Список построителей роли от её сверки в этом сеансе. Сверка роли в
    сеансе есть (scheduled) — ждём её итога: под xdist она идёт в другом
    процессе, и тот же процесс её раньше сторожа уже прошёл (элементы
    раздаются по порядку сборки, сверка в файле выше). None — сверки нет
    (отбор -k), она оборвалась или не дала итога за отведённое время —
    тогда сторож снимает роль сам."""
    started = folder / f"{name}.started"
    done, failed = folder / f"{name}.json", folder / f"{name}.failed"
    if not scheduled:
        return None
    t0 = time.monotonic()
    while not (done.exists() or failed.exists()):
        waited = time.monotonic() - t0
        if waited > BUILDERS_WAIT_S or (waited > BUILDERS_START_S and not started.exists()):
            return None
        time.sleep(0.2)
    if not done.exists():
        return None
    return set(json.loads(done.read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", FILES)
async def test_screens_match_the_reference_file(name, request, tmp_path, tmp_path_factory, frozen, fakes,
                                                monkeypatch):
    """Все снимки роли — через диспетчер; ни одно нажатие не ушло в обработчик
    устаревшей кнопки; лимиты и HTML в порядке; каждый общий раздел, доступный
    роли файла, снят хотя бы одним снимком; запись совпадает с эталоном байт
    в байт. Ссылки на экраны в уведомлениях — с меткой «-n», на экранах — без
    неё (tests/screens/marks). Вызванные построители клавиатур уходят сторожу
    полноты (_builders_write)."""
    shots = [s for s in catalog.SHOTS if s.role == name]
    drawn = harness.record_section_screens(monkeypatch)
    notified = marks.record(monkeypatch)
    folder = _builders_dir(tmp_path_factory)
    (folder / f"{name}.started").touch()
    try:
        with harness.BuilderCalls(harness.keyboard_builders()) as calls:
            parts, bad, circles = await _take_role(shots, tmp_path, fakes, notified)
    except BaseException:
        (folder / f"{name}.failed").touch()
        raise
    _builders_write(folder, name, calls.seen)
    assert not bad, "нарушения ограничений Telegram:\n" + "\n".join(bad)
    if name == "admin":
        unused = set(harness.CIRCLE_ACTIONS) - circles
        assert not unused, f"исключение кружка больше не нужно — убери из CIRCLE_ACTIONS: {unused}"
    br = SECTION_ROLE.get(name)
    if br is not None:
        from awgbot.bot import sections
        unseen = sorted(m.ID for m in sections.MODULES
                        if sections.available(br, m.ID) and (br.name, m.ID) not in drawn)
        assert not unseen, f"{name}: общие разделы роли без снимка — их экран не виден в эталоне: {unseen}"
    got = "\n".join(parts)
    ref = HERE / f"{name}.txt"
    if request.config.getoption("--update-screens"):
        ref.write_text(got, encoding="utf-8")
        return
    assert ref.exists(), f"нет эталона {ref.name} — сними: pytest tests/screens --update-screens"
    want = ref.read_text(encoding="utf-8")
    diff = "".join(difflib.unified_diff(want.splitlines(True), got.splitlines(True),
                                        f"{name}.txt (эталон)", f"{name}.txt (снято)"))
    assert got == want, f"экраны разошлись с эталоном (--update-screens — перезаписать):\n{diff}"


async def _take_role(shots, tmp_path, fakes, notified):
    """Снимки роли по порядку каталога: записи, нарушения лимитов и действия
    кружка; падает на первом снимке с устаревшим нажатием или лишним исключением."""
    parts, bad, circles = [], [], set()
    for shot in shots:
        rec = await take(shot, tmp_path, fakes)
        # ровно текст обработчика устаревшей кнопки (handlers/stale.py); alert
        # раздела «Кнопка устарела — открой раздел заново» — осознанный ответ
        from awgbot.bot.handlers.stale import STALE_BUTTON
        stale = [c.head for c in rec.everything
                 if STALE_BUTTON in (c.toast or "") or STALE_BUTTON in (c.body or "")]
        assert not stale, f"{shot.id}: нажатие не поймал ни один экран — ушло в устаревшую кнопку: {stale}"
        assert rec.calls, f"{shot.id}: бот ничего не ответил на последнее действие"
        bad += harness.problems(shot.id, rec, catalog.LABEL_EXCEPTIONS)
        bad += marks.problems(shot.id, rec, notified)
        bad += harness.listmsg_problems(shot.id, rec)
        found, used = timelabels.problems(shot.id, rec, catalog.TIME_LABEL_EXCEPTIONS, _expected_labels(shot))
        bad += found
        spare_t = catalog.TIME_LABEL_EXCEPTIONS.get(shot.id, set()) - used
        assert not spare_t, f"{shot.id}: исключение больше не нужно — убери из TIME_LABEL_EXCEPTIONS: {spare_t}"
        spare = catalog.LABEL_EXCEPTIONS.get(shot.id, set()) - harness.long_labels(rec)
        assert not spare, f"{shot.id}: исключение больше не нужно — убери из LABEL_EXCEPTIONS: {spare}"
        circles |= harness.circle_actions_seen(rec)
        parts.append(harness.serialize(shot.id, shot.title, rec))
    return parts, bad, circles


def test_every_shot_id_is_unique_and_names_its_role():
    """Один id — один снимок; префикс id — роль (adm/cl/gst/gw): по id ищут
    экран в эталоне и в отчёте о нарушениях."""
    ids = [s.id for s in catalog.SHOTS]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
    prefix = {"admin": "adm.", "client": "cl.", "guest": "gst.", "gateway": "gw."}
    wrong = [s.id for s in catalog.SHOTS if not s.id.startswith(prefix[s.role])]
    assert not wrong, wrong
    unknown = set(catalog.LABEL_EXCEPTIONS) - set(ids)
    assert not unknown, f"исключения для снимков, которых нет: {unknown}"


async def test_a_button_no_screen_takes_is_seen_as_stale(tmp_path, frozen, fakes):
    """Сторож достижимости не пустой: кнопка, которую не разбирает ни один
    экран (упаковка прежней версии), проходит весь диспетчер до обработчика
    устаревшей — и запись это показывает. Иначе проверка «ни одно нажатие не
    ушло в устаревшую» была бы зелёной всегда."""
    from awgbot.bot.handlers.stale import STALE_BUTTON
    rec = await take(catalog.Shot("adm.stale", role="admin", press=["cs:manage:1"]), tmp_path, fakes)
    heads = [c.head for c in rec.everything]
    assert f"~ {STALE_BUTTON}" in heads, heads
    assert "- markup off #1" in heads, "у старого сообщения остались живые кнопки"
    assert any(h.startswith("+ send #") for h in heads), "главная не пришла новым сообщением"


@pytest.mark.parametrize("press", ["peer_ask", "peer_yes:1", "peer_yes:0"])
async def test_the_removed_peer_nets_buttons_land_on_stale(tmp_path, frozen, fakes, press):
    """Тумблер «↔️ Связь подсетей» снят — связь подсетей стала частью
    VPN-транзита. Кнопки прежних сообщений (диалог и подтверждение) не ловит
    ни один экран: всплывашка «Кнопка устарела» и главная новым сообщением, а
    не спиннер и не прежнее переключение."""
    from awgbot.bot.callbacks import GwSlotCB
    from awgbot.bot.handlers.stale import STALE_BUTTON
    from tests.screens.shots_admin_common_gw import _gw
    action, _, val = press.partition(":")
    shot = catalog.Shot(f"adm.stale.{action}", role="admin", press=[GwSlotCB(action=action, val=val)],
                        data=_gw(2, lan2=True))
    rec = await take(shot, tmp_path, fakes)
    heads = [c.head for c in rec.everything]
    assert f"~ {STALE_BUTTON}" in heads, heads
    assert any(h.startswith("+ send #") for h in heads), "главная не пришла новым сообщением"


def test_every_keyboard_function_says_what_it_returns():
    """Построитель отличается от помощника аннотацией результата; без неё
    новая клавиатура выпала бы из сторожа полноты молча."""
    bare = harness.unannotated_keyboard_functions()
    assert not bare, f"нет аннотации результата: {bare}"


async def test_every_keyboard_builder_is_drawn_by_some_shot(request, tmp_path, tmp_path_factory, frozen, fakes):
    """Полнота каталога: каждый публичный построитель клавиатур вызван хотя бы
    одним снимком. Новая клавиатура без снимка — красный тест: её экран
    никто не видел в эталоне, и рефакторинг унесёт его без следа. Исключение
    (UNREACHABLE) — только для построителя, которого не рисует ни действие
    человека, ни событие; исключение, которое не нужно, — тоже красное.

    Каталог второй раз не снимается: вызванные построители каждой роли пишет
    её сверка с эталоном (test_screens_match_the_reference_file) в каталог
    сеанса. Порядок под xdist явный: сверка роли выбрана в сеансе — сторож
    ждёт её итога (файл роли); роль не сверяется в этом сеансе (отбор -k,
    прогон одного теста) или сверка оборвалась — сторож снимает роль сам.
    Так полнота считается по всему каталогу при любом отборе."""
    builders = harness.keyboard_builders()
    folder = _builders_dir(tmp_path_factory)
    scheduled = {it.callspec.params["name"] for it in request.session.items
                 if it.originalname == "test_screens_match_the_reference_file"
                 and it.path == request.node.path}
    seen, own = set(), []
    for name in FILES:
        got = _builders_read(folder, name, name in scheduled)
        if got is None:
            own.append(name)
        else:
            seen |= got
    if own:
        with harness.BuilderCalls(builders) as calls:
            for shot in catalog.SHOTS:
                if shot.role in own:
                    await take(shot, tmp_path, fakes)
        seen |= calls.seen
    unknown = set(catalog.UNREACHABLE) - set(builders)
    assert not unknown, f"в UNREACHABLE имена, которых нет среди построителей: {sorted(unknown)}"
    spare = set(catalog.UNREACHABLE) & seen
    assert not spare, f"исключение больше не нужно — построитель снят: {sorted(spare)}"
    missing = sorted(set(builders) - seen - set(catalog.UNREACHABLE))
    assert not missing, ("построители клавиатур без снимка:\n" + "\n".join(
        f"  {k} — {(inspect.getdoc(builders[k]) or '').splitlines()[0] if inspect.getdoc(builders[k]) else ''}"
        for k in missing))
