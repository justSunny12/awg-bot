"""Потолки памяти зоны резерва от размера данных основы: процесс открытия
(openproc `rsvopen_sandbox_props` — MemoryMax от размера guest.pkg,
`rsvzone.open_memory_mb`), бот основы на резерве (юнит гостя — MemoryMax от
размера базы, `rsvzone.guest_memory_mb`, процесс открытия передаёт его скрипту
зоны в GUEST_MEMORY_MB при --take), предупреждение в карточке резерва о большом
пакете (больше 64 МБ).

Скрипт гоняется bash'ем по функции write_units (обвязка `_unit_text` из
test_reserve_zone_script), обёртка `rsvzone.take` — с подставленным `run`.

Цена ошибки: потолки, не связанные с размером базы (192M и 160M при базе в
сотни мегабайт, она в tmpfs и копией в памяти процесса), — процесс открытия
или гость убиты OOM посреди аварийного переезда, когда чинить уже поздно;
потолок ниже прежнего для маленькой базы — то же самое там, где раньше
работало; мусор в GUEST_MEMORY_MB в юните — гость не стартует вовсе; админ
резерва не знает, что переезд к нему потребует столько памяти.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.core import config
from awgbot.domain.services.reserve import openproc
from awgbot.infra import rsvzone

from tests.integration import _reserve_common as rc
from tests.unit.test_reserve_zone_script import _unit_text

pytestmark = pytest.mark.integration
rsv = rc.rsv
MB = 1024 * 1024


@pytest.mark.parametrize("size, want", [(0, 192), (10 * MB, 192), (30 * MB, 218), (100 * MB, 428), (100 * MB + 1, 431)])
def test_the_open_process_ceiling_grows_with_the_package_but_not_below_the_old_one(size, want):
    assert rsvzone.open_memory_mb(size) == want


@pytest.mark.parametrize("size, want", [(0, 160), (20 * MB, 160), (40 * MB, 176), (300 * MB, 696)])
def test_the_guest_ceiling_grows_with_the_base_but_not_below_the_old_one(size, want):
    assert rsvzone.guest_memory_mb(size) == want


def _unit_mem(text: str) -> tuple[str, str]:
    lines = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))
    return lines["MemoryMax"], lines["MemoryHigh"]


def test_the_guest_unit_takes_its_ceiling_from_the_open_process(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _unit_mem(_unit_text(tmp_path / "a")) == ("160M", "128M"), "без значения — прежний потолок"
    assert _unit_mem(_unit_text(tmp_path / "b", GUEST_MEMORY_MB="696")) == ("696M", "664M")


@pytest.mark.parametrize("bad", ["много", "12", "-5", "160M"])
def test_garbage_or_a_too_small_value_gives_the_old_ceiling(tmp_path, bad):
    assert _unit_mem(_unit_text(tmp_path, GUEST_MEMORY_MB=bad)) == ("160M", "128M"), bad


def test_take_passes_the_guest_ceiling_to_the_script(monkeypatch):
    seen = {}
    monkeypatch.setattr(rsvzone, "run", lambda mode, env=None, stdin=None, **kw: seen.update(mode=mode, env=env) or "")
    rsvzone.take("[Interface]\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[], memory_mb=696)
    assert seen["mode"] == "take" and seen["env"]["GUEST_MEMORY_MB"] == "696"
    rsvzone.take("[Interface]\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[])
    assert "GUEST_MEMORY_MB" not in seen["env"], "без значения — умолчание скрипта"


def test_the_sandbox_ceiling_is_taken_from_the_package_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "reserve").mkdir()
    props = openproc.rsvopen_sandbox_props(str(tmp_path))
    assert "MemoryMax=192M" in props, "пакета нет — прежний потолок"
    with open(tmp_path / "reserve" / "guest.pkg", "wb") as f:
        f.truncate(100 * MB)
    props = openproc.rsvopen_sandbox_props(str(tmp_path))
    assert "MemoryMax=428M" in props, props


def test_a_package_over_64_mb_is_a_warning_on_the_reserve_card(services, rsv):
    rc.pair_row(services)
    services.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "size": 64 * MB})
    assert "памяти" not in card_text(services.reserve_card_data()), "64 МБ ровно — ещё без строки"
    services.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "size": 100 * MB})
    d = services.reserve_card_data()
    assert d["pkg_mem"] == {"size": "0,1 ГБ", "need": "0,71 ГБ"}, d["pkg_mem"]
    assert "└ ⚠️ данные для переезда «" in card_text(d) and "понадобится до 0,71 ГБ памяти" in card_text(d), card_text(d)
