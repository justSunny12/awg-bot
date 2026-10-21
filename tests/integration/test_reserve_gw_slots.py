"""Шлюзы основы у резерва — только номера слотов: основа шлёт в `st.voters`
слоты без имён, резерв в `view`/`ask` основе — слоты и взгляд, в своих
текстах (Т-РН3, хвост Т-РУ4, экран роли) — «шлюз N»; имена устройств в
текстах основы подставляет она сама из своей базы (нет шлюза в базе —
«Шлюз N»).

Цена ошибки: имя устройства шлюза (часто — место или человек: «Дача»,
«У мамы») уходит администратору чужого сервера; основа, поверившая имени из
тела резерва, называет в уведомлении шлюз так, как его назвала другая
сторона, — подмена текста чужим админом; имена старой основы, осевшие в `st`,
продолжают всплывать в текстах резерва.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import peeradmin as pa_t
from awgbot.domain.notices import reserve as rn
from awgbot.domain.services.reserve.vote import ReserveVoteMixin
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import NAMES, box, gw_sent, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]


# ── основа: тело st ─────────────────────────────────────────────────────────

@pytest.fixture()
def primary(services, rsv, make_active_client, monkeypatch):
    """Основа с двумя шлюзами («Дача» — слот 1 с rsv1, «Офис» — слот 2 без rsv1)."""
    rc.pair_row(services)
    cl = make_active_client(name="Шлюзы", tg_id=7001)
    for slot, name in ((1, "Дача"), (2, "Офис")):
        dev = services.add_device(cl.id, name).device_id
        services.db.gateway_add(dev, "awglink" if slot == 1 else f"awglink{slot}", 443 + slot, f"10.99.9{slot}.0/30",
                                slot_id=slot)
    monkeypatch.setattr(services, "gwlink_agent_caps", lambda gid: ["rsv1"] if int(gid) == 1 else [])
    return services


def test_the_st_of_the_primary_names_its_voters_by_slot_only(primary):
    body = primary.reserve_st_body()
    assert body["voters"] == [{"slot": 1}], f"в st резерву — не только номера слотов: {body['voters']}"
    assert "Дача" not in repr(body) and "Офис" not in repr(body), "имя устройства шлюза ушло второй стороне"


def test_a_failure_of_the_gateway_list_gives_an_empty_voters_body(primary, monkeypatch):
    def boom():
        raise RuntimeError("база занята")
    monkeypatch.setattr(primary, "_reserve_gateways_view", boom)
    assert primary.reserve_st_body()["voters"] == []


@pytest.mark.parametrize("voters, want", [
    ([{"slot": 1, "see": False}, {"slot": 2, "see": True}], ("«Дача»", "«Офис»")),
    ([{"slot": 1, "name": "Чужое имя", "see": False}, {"slot": 2, "see": True}], ("«Дача»", "«Офис»")),
])
def test_the_primary_names_the_gateways_of_a_view_from_its_own_base(primary, voters, want):
    """Т-РУ21: имена — из базы основы, имя в теле резерва не слушается."""
    told = primary.reserve_view_in("4", "2026-10-21T03:11:00+03:00", voters)
    assert len(told) == 1 and all(w in told[0].text for w in want), told[0].text
    assert "Чужое имя" not in told[0].text, "основа повторила имя шлюза, присланное резервом"
    assert not told[0].text.rstrip().endswith("."), "уведомление с точкой в конце"


def test_a_slot_unknown_to_the_primary_is_called_by_its_number(primary):
    told = primary.reserve_view_in("3", "2026-10-21T03:11:00+03:00", [{"slot": 5, "see": False}])
    assert len(told) == 1 and "«Шлюз 5»" in told[0].text, told[0].text


@pytest.mark.parametrize("slot", ["мусор", None, {"x": 1}])
def test_a_garbage_slot_in_the_view_does_not_break_the_notice(primary, slot):
    told = primary.reserve_view_in("3", "2026-10-21T03:12:00+03:00", [{"slot": slot, "see": False}])
    assert len(told) == 1 and "Шлюз" in told[0].text


# ── резерв: имена шлюзов основы — по номеру слота ───────────────────────────

def test_the_reserve_names_a_gateway_of_the_primary_by_its_slot():
    assert ReserveVoteMixin.reserve_gw_name(2) == "шлюз 2"
    assert ReserveVoteMixin.reserve_gw_name("3") == "шлюз 3"


def test_names_left_in_the_st_of_an_older_primary_do_not_reach_the_reserve_texts(box):
    """Фикстура кладёт в st основы имена (как слала прежняя основа) — в
    текстах резерва их всё равно нет."""
    assert box.s.reserve_peer_state()["voters"][0].get("name") == NAMES[1], "предпосылка теста не выполнена"
    assert [g["name"] for g in box.s.reserve_gw_views()] == ["шлюз 1", "шлюз 2", "шлюз 3"]


def test_view_and_ask_to_the_primary_carry_no_names(box):
    box.primary(True)
    acts = box.hello(2, sees=True) + box.hello(1, sees=False)
    for body in sent(acts, "view"):
        assert all(set(v) == {"slot", "see"} for v in body["voters"]), body
    assert all("name" not in v for _slot, body in gw_sent(acts, "view") for v in body.get("voters") or []), \
        "имена в view шлюзам"


def test_the_hosting_tail_names_the_gateways_by_slot_without_quotes(box):
    """Хвост Т-РУ4 (случай 5): «частично: не видят шлюз 1, видят шлюз 2;
    шлюз 3 не на связи с этим сервером»."""
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=True)
    tail = box.s._hosting_extra("5")
    assert "не видят шлюз 1" in tail and "видят шлюз 2" in tail and "шлюз 3 не на связи с этим сервером" in tail, tail
    assert "«" not in tail and not any(n in tail for n in NAMES.values()), tail


@pytest.mark.parametrize("why, head", [("gw_sees", "ℹ️ Шлюз 2 снова на связи и видит сервер"),
                                       ("gw_unseen", "ℹ️ Шлюз 2 снова на связи и не видит сервер")])
def test_the_closing_notice_writes_the_slot_name_with_a_capital_and_no_quotes(why, head):
    text = rn.code_window_closed("шлюз 2", why)
    assert text.startswith(head) and "«" not in text and not text.endswith("."), text


def test_the_closing_notice_for_the_primary_itself_keeps_its_quoted_name():
    assert rn.code_window_closed("Beta", "primary_seen") == "ℹ️ «Beta» снова на связи — переезд не нужен"


def _screen(box, **over) -> str:
    d = box.s.reserve_peer_admin_summary()
    d.update(over)
    return pa_t.screen(d)


def test_the_role_screen_names_the_gateway_links_by_slot(box):
    box.hello(1)
    box.hello(2)
    box.hello(3)
    text = _screen(box, zone_links=[1, 2, 3])
    assert "🟢 линки твоих шлюзов на связи: шлюз 1, шлюз 2, шлюз 3" in text, text
    assert not any(n in text for n in NAMES.values()), "имя шлюза основы на экране роли резерва"


def test_the_role_screen_names_a_gateway_without_a_link_by_slot(box):
    box.hello(1)
    box.hello(2)
    text = _screen(box, zone_links=[1, 2, 3])
    assert "⚠️ линк: шлюз 3 без связи" in text and "«" not in text.split("⚠️ линк:")[1].splitlines()[0], text
