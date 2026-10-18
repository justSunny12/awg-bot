"""Набор обновлений, план: выпуск набора, версии ролей в выпуске, цель кнопки,
участники хода, текст раздела и диалога.

Цена ошибки: версия роли, посчитанная не по адресатам, ставит шлюзу выпуск
только для сервера (или наоборот, не ставит нужный); цель, найденная только
для своей роли, не зажигает кнопку выпуска для шлюзов; шлюз, «бывший на
связи час назад», держащий ход или `needs_gw`, — набор, который ждёт
выключенное устройство (Р22).
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts import fleet as ft
from awgbot.domain.fleet import role_target
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit


def test_role_versions_follow_the_addressees():
    """Версия роли в выпуске T — новейший ≤ T, адресованный роли."""
    releases = [rel("v3.4.0", both=True), rel("v3.4.1", main={"requires": "3.4.0"}),
                rel("v3.4.2", gw={"requires": "3.4.0"}), rel("v3.5.0", both=True)]
    by = {r.tag: r for r in releases}
    assert role_target(releases, "main", by["v3.4.2"].version).tag == "v3.4.1"
    assert role_target(releases, "gateway", by["v3.4.1"].version).tag == "v3.4.0", "выпуск только для сервера шлюзы не трогает"
    assert role_target(releases, "gateway", by["v3.4.2"].version).tag == "v3.4.2"
    assert role_target(releases, "main", by["v3.5.0"].version).tag == "v3.5.0"
    assert role_target(releases, "gateway", (3, 3, 9)) is None


def test_the_button_target_is_the_newest_release_of_any_role(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw={"requires": "3.4.0"}))
    assert fleet.s.fleet_target().tag == "v3.4.1", "выпуск только для шлюзов кнопку не зажёг"
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw={"requires": "3.4.0"}),
                   rel("v3.4.2", main={"requires": "3.4.0"}))
    assert fleet.s.fleet_target().tag == "v3.4.2"


def test_the_target_stops_at_a_skipped_release_and_the_generation_ceiling(fleet, monkeypatch):
    from awgbot.infra import updates
    broken = updates.Release(tag="v3.4.2", version=(3, 4, 2), body="", asset_url="u", sha256="b" * 64,
                             meta=None, meta_bad="метаданные не прочитаны")
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True), broken, rel("v3.4.3", both=True))
    assert fleet.s.fleet_target().tag == "v3.4.1", "цель перешагнула пропущенный выпуск"
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True, gen=2))
    monkeypatch.setattr(fleet.s, "_generation_ceiling", lambda: 1)
    assert fleet.s.fleet_target() is None, "цель выше потолка поколения при идущем переезде"


def test_the_fleet_release_is_the_server_version_and_a_gw_only_run_moves_it(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw={"requires": "3.4.0"}))
    assert fleet.s.fleet_release() == "v3.4.0"
    fleet.s.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.1", "state": "done", "steps": []})
    assert fleet.s.fleet_release() == "v3.4.1", "зафиксированный ход к выпуску для шлюзов не стал выпуском набора"
    fleet.s.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.1", "state": "rolled_back", "steps": []})
    assert fleet.s.fleet_release() == "v3.4.0"


def test_a_server_not_on_a_release_build_has_no_fleet(fleet, monkeypatch):
    from awgbot.core import config
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.9.9")
    assert fleet.s.fleet_release() == "" and fleet.s.fleet_target() is None


def test_only_a_gateway_with_a_live_session_holds_the_run_and_needs_gw(fleet):
    """Р22: шлюз, бывший на связи час назад, не держит ни ход, ни needs_gw."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw={"requires": "3.4.0"}),
                   rel("v3.4.2", main={"requires": "3.4.0", "needs_gw": "3.4.1"}, gw={"requires": "3.4.0"}))
    fleet.slot(1, "3.4.0", online=False)
    from awgbot.util import timeutil
    fleet.s.db.set_state("gwlink_seen_1", timeutil.now_iso())        # был на связи только что
    rows = fleet.s.fleet_gateways()
    assert rows[0]["online"] is False and rows[0]["participant"] is False
    step, needs_gw = fleet.s.fleet_server_step(fleet.s.fleet_release_of("v3.4.2"))
    assert step.tag == "v3.4.2" and needs_gw is False, "недоступный шлюз удержал ступень сервера"
    fleet.online(1, "3.4.0")
    step, needs_gw = fleet.s.fleet_server_step(fleet.s.fleet_release_of("v3.4.2"))
    assert step is None and needs_gw is True, "доступный шлюз ниже needs_gw не удержал сервер"


def test_agents_that_cannot_take_the_command_do_not_participate(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    fleet.slot(1, "3.3.1", caps=("cfg1",))                     # агент 3.3.x — без upd1
    fleet.slot(2, "3.9.9.1")                                    # своя сборка: такого выпуска нет
    rows = {r["slot"]: r for r in fleet.s.fleet_gateways()}
    assert rows[1]["participant"] is False and rows[1]["upd"] is False
    assert rows[2]["participant"] is False and rows[2]["release"] is False
    plan = fleet.s.fleet_plan(fleet.s.fleet_target())
    text = ft.fleet_section(plan)
    assert ("⚠️ «Шлюз 1»: агент v3.3.1 командой сервера AWG не обновляется — обнови его последний раз в "
            "чате бота шлюза: «⚙️ Настройки» → «⬆️ Обновления»") in text
    assert ("🛰 «Шлюз 2»: v3.9.9.1 · ⚠️ Агент — сборка не из выпуска (v3.9.9.1): по команде сервера AWG "
            "обновить не получится") in text


def test_the_queue_goes_standby_first_and_the_active_one_last(fleet):
    fleet.releases(rel("v3.4.0", both=True))
    for n in (1, 2, 3):
        fleet.slot(n)
    fleet.active(2)
    assert [r["slot"] for r in fleet.s.fleet_gateways()] == [1, 3, 2]


def test_the_section_and_the_dialog_say_the_plan(fleet):
    """Т-У1 и Т-У3: строки хостов, шлюз не на связи, хвост диалога."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1, "3.4.0")
    fleet.slot(2, "3.4.0", online=False, name="Офис")
    target = fleet.s.fleet_target()
    plan = fleet.s.fleet_plan(target)
    text = ft.fleet_section(plan)
    lines = text.split("\n")
    assert lines[0] == "⬆️ <b>Обновления</b> · доступна v3.5.0"
    assert lines[1] == "🖥 Сервер AWG: v3.4.0 → v3.5.0"
    assert lines[2] == "🛰 «Шлюз 1»: v3.4.0 → v3.5.0"
    assert lines[3] == "🛰 «Офис»: v3.4.0 → v3.5.0 · 🟡 не на связи с 12.10 — обновится, когда выйдет на связь"
    dialog = ft.fleet_confirm(plan)
    assert dialog.startswith("⬆️ Обновить до v3.5.0?\nСначала обновится сервер AWG, затем шлюзы по одному")
    assert dialog.endswith("Не удастся обновить один из шлюзов — весь набор вернётся на прежнюю версию. "
                           "Шлюзы не на связи обновятся, когда выйдут на связь")


def test_a_server_only_release_leaves_the_gateways(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", main={"requires": "3.4.0"}))
    fleet.slot(1, "3.4.0")
    plan = fleet.s.fleet_plan(fleet.s.fleet_target())
    assert "🛰 Шлюзы: остаются на v3.4.0 — в v3.4.1 правки только для сервера AWG" in ft.fleet_section(plan)


def test_needs_gw_turns_the_dialog_around(fleet):
    fleet.releases(rel("v3.4.0", both=True),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.5.0"}, gw={"requires": "3.4.0"}))
    fleet.slot(1, "3.4.0")
    plan = fleet.s.fleet_plan(fleet.s.fleet_target())
    assert plan["needs_gw"] is True
    assert ft.fleet_confirm(plan).startswith(
        "⬆️ Обновить до v3.5.0?\nСерверу AWG v3.5.0 нужны агенты шлюзов не ниже v3.5.0 — сначала обновятся шлюзы")


def test_an_actual_fleet_shows_one_line_of_gateways(fleet):
    fleet.releases(rel("v3.4.0", both=True))
    fleet.slot(1, "3.4.0")
    fleet.slot(2, "3.4.0", name="Офис")
    text = ft.fleet_section(fleet.s.fleet_plan(None))
    assert text == ("⬆️ <b>Обновления</b> · v3.4.0 🟢 актуальна\n🖥 Сервер AWG: v3.4.0\n"
                    "🛰 «Шлюз 1»: v3.4.0 · «Офис»: v3.4.0")


def test_an_agent_newer_than_the_fleet_says_it_will_follow(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    fleet.slot(1, "3.4.1")
    text = ft.fleet_section(fleet.s.fleet_plan(None))
    assert "🛰 «Шлюз 1»: v3.4.1 · 🟡 Агент v3.4.1 новее сервера AWG — вернётся к v3.4.0" in text
