"""Шлюзы основы у резерва — только номера слотов: в `st` основы имён и
списка шлюзов нет (только признак `gw`), резерв в своих текстах (Т-РН3,
экран роли, кворум) называет шлюз основы «шлюз N»; имена устройств в
текстах основы подставляет она сама из своей базы.

Цена ошибки: имя устройства шлюза (часто — место или человек: «Дача»,
«У мамы») уходит администратору чужого сервера; основа, поверившая имени из
тела резерва, называет в уведомлении шлюз так, как его назвала другая
сторона, — подмена текста чужим админом; имена старой основы, осевшие в `st`,
продолжают всплывать в текстах резерва.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import peeradmin as pa_t
from awgbot.domain.services.reserve.gwguest import ReserveGwGuestMixin
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import Cand, box, code_window_outside, notes

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


def test_the_st_of_the_primary_carries_no_gateway_list_and_no_names(primary):
    body = primary.reserve_st_body()
    assert "voters" not in body, f"в st резерву — список шлюзов: {body.get('voters')}"
    assert body.get("gw") is True, "признак «у основы есть шлюз» не дошёл"
    assert "Дача" not in repr(body) and "Офис" not in repr(body), "имя устройства шлюза ушло второй стороне"


# ── резерв: имена шлюзов основы — по номеру слота ───────────────────────────

def test_the_reserve_names_a_gateway_of_the_primary_by_its_slot():
    assert ReserveGwGuestMixin.reserve_gw_name(2) == "шлюз 2"
    assert ReserveGwGuestMixin.reserve_gw_name("3") == "шлюз 3"


def test_the_code_window_texts_name_no_gateway_of_the_primary(box):
    """Т-К21 и Т-РН3 о сервере основы: ни имён её шлюзов, ни их номеров в
    кавычках — только имя сервера и счёт голосов."""
    box.peer_admin_opened()
    c = Cand(box)
    texts = [n.text for n in notes(code_window_outside(c))]
    box.hello(1)
    texts += [n.text for n in notes(c.view(1, "see") + c.tick())]
    assert len(texts) == 3, texts
    for t in texts:
        assert "Дача" not in t and "Офис" not in t and "шлюз" not in t.lower(), t
        assert not t.endswith("."), f"уведомление с точкой в конце: {t}"


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
    assert "Дача" not in text and "Офис" not in text, "имя шлюза основы на экране роли резерва"


def test_the_role_screen_names_a_gateway_without_a_link_by_slot(box):
    box.hello(1)
    box.hello(2)
    text = _screen(box, zone_links=[1, 2, 3])
    assert "⚠️ линк: шлюз 3 без связи" in text and "«" not in text.split("⚠️ линк:")[1].splitlines()[0], text
