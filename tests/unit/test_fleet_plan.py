"""Набор обновлений, план: выпуск набора, версии ролей в выпуске, цель кнопки,
участники хода, текст раздела и диалога.

Цена ошибки: версия роли, посчитанная не по адресатам, ставит шлюзу выпуск
только для сервера (или наоборот, не ставит нужный); цель, найденная только
для своей роли, не зажигает кнопку выпуска для шлюзов; шлюз, «бывший на
связи час назад», держащий ход или `needs_gw`, — набор, который ждёт
выключенное устройство.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts import fleet as ft
from awgbot.domain.fleet import order as fleet_order, role_target
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
    """Выпуск набора поверх сервера пишет только фиксация (logic-6): неудачный
    следующий ход его не трогает — шлюзы не тянутся ниже зафиксированного."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw={"requires": "3.4.0"}),
                   rel("v3.4.2", gw={"requires": "3.4.0"}))
    assert fleet.s.fleet_release() == "v3.4.0"
    fleet.s.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.1", "state": "running", "steps": []})
    fleet.s._fleet_commit(fleet.run(), fleet_order.FleetOut())
    assert fleet.s.fleet_release() == "v3.4.1", "зафиксированный ход к выпуску для шлюзов не стал выпуском набора"
    fleet.s.fleet_run_set({"id": "b1b2c3d4", "tag": "v3.4.2", "state": "rolled_back", "steps": []})
    assert fleet.s.fleet_release() == "v3.4.1", "откат следующего хода опустил выпуск набора"
    fleet.server("3.4.2")                            # сервер сменил версию сам — ключ не про него
    assert fleet.s.fleet_release() == "v3.4.2"


def test_a_server_not_on_a_release_build_has_no_fleet(fleet, monkeypatch):
    from awgbot.core import config
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.9.9")
    assert fleet.s.fleet_release() == "" and fleet.s.fleet_target() is None


def test_only_a_gateway_with_a_live_session_holds_the_run_and_needs_gw(fleet):
    """Шлюз, бывший на связи час назад, не держит ни ход, ни needs_gw."""
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
    assert ("⚠️ «Шлюз 1»: агент v3.3.1 командой сервера AWG не обновляется — обнови его в "
            "чате бота шлюза: «⚙️ Настройки» → «⬆️ Обновления»") in text
    assert ("🛰 «Шлюз 2»: v3.9.9.1 · ⚠️ Агент — сборка не из выпуска: по команде сервера AWG "
            "обновить не получится") in text


def test_the_queue_goes_standby_first_and_the_active_one_last(fleet):
    fleet.releases(rel("v3.4.0", both=True))
    for n in (1, 2, 3):
        fleet.slot(n)
    fleet.active(2)
    assert [r["slot"] for r in fleet.s.fleet_gateways()] == [1, 3, 2]


def test_the_section_and_the_dialog_say_the_plan(fleet):
    """Раздел и диалог: строки хостов, шлюз не на связи, хвост диалога."""
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
    assert lines[3] == "🛰 «Офис»: v3.4.0 → v3.5.0 · 🟡 не на связи с 12.10: обновится, когда выйдет на связь"
    dialog = ft.fleet_confirm(plan)
    assert dialog.startswith("⬆️ Обновить до v3.5.0?\nСначала обновится сервер AWG, затем шлюзы по одному")
    assert dialog.endswith("Не удастся обновить один из шлюзов — сервер и шлюзы вернутся на прежнюю версию. "
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
    assert "🛰 «Шлюз 1»: v3.4.1 · 🟡 Агент новее поставки сервера AWG — вернётся к v3.4.0" in text


async def test_the_blocked_line_is_escaped_and_fits_the_message(fleet, monkeypatch):
    """Вычитчик (fix4): «⛔ Обновление до … сейчас недоступно: …» — тег и
    причина экранированы (ui.toast HTML не экранирует), строка — в том же
    пределе 4096, что и список изменений, и не теряется за ним."""
    from awgbot.bot import sections
    from awgbot.bot.roles import MAIN
    from awgbot.infra import updates
    big = updates.Release(tag="v3.5.0", version=(3, 5, 0), body="\n".join(f"- пункт {i} " + "x" * 60
                                                                          for i in range(200)),
                          asset_url="https://api/assets/v3.5.0", sha256="a" * 64,
                          meta=rel("v3.5.0", both=True).meta)
    fleet.releases(rel("v3.4.0", both=True), big)
    fleet.slot(1, "3.4.0")
    monkeypatch.setattr(fleet.s, "update_block_reason", lambda release=None: "занято <b>переездом</b> & ждёт")
    text, _kb = await sections.updates.screen(MAIN, fleet.s, "cached")
    assert len(text) <= 4096, len(text)
    tail = text.rsplit("\n", 1)[-1]
    assert tail == "⛔ Обновление до v3.5.0 сейчас недоступно: занято &lt;b&gt;переездом&lt;/b&gt; &amp; ждёт", tail


# ── строка шлюза: догон не удался, повтор назначен ──────────────────────────

def _retry_gw(**st) -> dict:
    base = {"run": "", "state": "failed", "target": "v4.0.0", "error": "GitHub не ответил",
            "final": False, "next_at": "2026-10-22T07:30:00+03:00"}
    return {"slot": 1, "name": "Шлюз 1", "version": "3.4.1", "want": "4.0.0", "online": True,
            "release": True, "upd": True, "device": "x", "upd_state": {**base, **st}}


def test_a_failed_catch_up_waiting_for_a_retry_says_why_and_when():
    """Пока ждём повтор, шлюз отстаёт от выпуска — раздел обязан сказать,
    почему и когда следующая попытка; пустая строка оставляла админа гадать."""
    assert ft.gw_state_line(_retry_gw()) == \
        "⚠️ не обновился до v4.0.0: GitHub не ответил — повтор в 09:30 (UTC+5)"
    assert ft.gw_state_line(_retry_gw(state="rolled_back", error="")) == \
        "⚠️ не обновился до v4.0.0: rolled_back — повтор в 09:30 (UTC+5)", "без текста ошибки — состояние"


def test_after_the_last_retry_the_line_is_the_final_one():
    """Повторы исчерпаны (final) — прежняя строка «после выхода на связь»,
    без времени: его нет, повтора не будет."""
    assert ft.gw_state_line(_retry_gw(final=True, next_at="")) == \
        "⚠️ не обновился до v4.0.0 после выхода на связь: GitHub не ответил"
    assert ft.gw_state_line(_retry_gw(final=True)) == \
        "⚠️ не обновился до v4.0.0 после выхода на связь: GitHub не ответил", "final важнее остатка next_at"


def test_an_unreadable_retry_time_does_not_hide_the_line():
    assert ft.gw_state_line(_retry_gw(next_at="вчера")) == \
        "⚠️ не обновился до v4.0.0: GitHub не ответил — повтор в ?"


def test_a_failure_inside_a_run_is_not_shown_as_a_retry():
    """Неудача шага хода (run задан) — её показывает ход, не строка повтора."""
    assert "повтор в" not in ft.gw_state_line(_retry_gw(run="a1b2c3d4"))
