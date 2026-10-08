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
import pathlib

import pytest

from tests.screens import catalog, harness
from tests.screens.harness import take

pytestmark = pytest.mark.screens

HERE = pathlib.Path(__file__).parent
FILES = sorted({s.role for s in catalog.SHOTS})


@pytest.mark.parametrize("name", FILES)
async def test_screens_match_the_reference_file(name, request, tmp_path, frozen, fakes):
    """Все снимки роли — через диспетчер; ни одно нажатие не ушло в обработчик
    устаревшей кнопки; лимиты и HTML в порядке; запись совпадает с эталоном
    байт в байт."""
    shots = [s for s in catalog.SHOTS if s.role == name]
    parts, bad = [], []
    for shot in shots:
        rec = await take(shot, tmp_path, fakes)
        stale = [c.head for c in rec.everything
                 if "Кнопка устарела" in (c.toast or "") or "Кнопка устарела" in (c.body or "")]
        assert not stale, f"{shot.id}: нажатие не поймал ни один экран — ушло в устаревшую кнопку: {stale}"
        assert rec.calls, f"{shot.id}: бот ничего не ответил на последнее действие"
        bad += harness.problems(shot.id, rec, catalog.LABEL_EXCEPTIONS)
        spare = catalog.LABEL_EXCEPTIONS.get(shot.id, set()) - harness.long_labels(rec)
        assert not spare, f"{shot.id}: исключение больше не нужно — убери из LABEL_EXCEPTIONS: {spare}"
        parts.append(harness.serialize(shot.id, shot.title, rec))
    assert not bad, "нарушения ограничений Telegram:\n" + "\n".join(bad)
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
