"""Замена устройства слота командой подключения и её отмена, «🔁 Настроить
заново».

Линк слота сразу переходит на ключи токена — прежнее устройство отрезано;
отмена, срок и третий неверный код возвращают ему прежние ключи
(.join-prev); верный код — устройство заменено, снимок прежнего забыт,
.join-prev прочь; настройки бота шлюза: новое устройство — вопрос (keep_back
why=device), то же устройство заново — молча (why=same).

Цена ошибки: прежнее устройство, оставшееся без линка после отмены; слот в
замене, через который автомат пускает трафик; ключи прежнего устройства,
пережившие верный код.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwkeep
from awgbot.util import gwlink

from tests.integration import _join_common as jc
from tests.integration._join_common import Gateway, accept, connect, slot_with_device

pytestmark = pytest.mark.integration
admin, vps = jc.admin, jc.vps                     # фикстуры сцены

ITEMS = {"conf": {"quiet_hours.quiet_hours_start": 21}, "fw": {}, "mail": None}


def _replace(services, vps, *, same: bool = False, device_id=None):
    gw = slot_with_device(services, vps, 1)
    before = vps.peer_pub("awglink")
    res = services.gwjoin_issue(slot_id=1, same=same, device_id=device_id)
    return gw, before, res, Gateway(res["command"])


def test_replacing_puts_the_link_on_token_keys_and_keeps_the_old_ones_aside(services, vps, fake_routing):
    gw, before, res, _g = _replace(services, vps)
    mode, env = vps.runs[-1]
    assert mode == "--join-stage" and env["JOIN_PREV"] == "1"
    assert vps._p(vps.live("awglink"), ".join-prev").exists() and vps._p(vps.gw("awglink"), ".join-prev").exists()
    assert vps.peer_pub("awglink") != before and vps.stamped("awglink")
    assert res["state"]["kind"] == "replace" and res["state"]["prev_device"] == gw.device_id
    assert services.db.gateway(1).device_id == gw.device_id, "устройство заменено до кода"
    assert services.gwjoin_replacing(1) and services._rt_unavailable(1), "слот в замене доступен автомату"


def test_a_slot_in_replacement_leaves_the_transit_and_the_neighbours(services, vps):
    """Подсети слота в замене уходят из PEER_LAN_NETS соседей, сам слот — из
    VPN-транзита: туннель до него сейчас у другой машины."""
    slot_with_device(services, vps, 1)
    services.add_device(services.admin_client().id, "Pi4")
    slot_with_device(services, vps, 2, "Pi4")
    for n in (1, 2):
        services.db.gateway_update(n, lan_subnets=[f"192.168.{n}.0/24"], lan_mode=1)
    assert services.gateway_peer_nets(2) == ["10.240.0.0/16"]
    services.gwjoin_issue(slot_id=1)
    assert [g.id for g in services.gateway_transit_slots()] == [2]
    assert services.gateway_peer_nets(2) == [], "подсети слота в замене остались у соседа"


@pytest.mark.parametrize("how", ["cancel", "expired", "code"])
def test_cancel_expiry_and_three_wrong_codes_give_the_link_back_at_once(services, vps, how):
    gw, before, res, gwside = _replace(services, vps)
    jid = res["state"]["id"]
    if how == "cancel":
        assert services.gwjoin_cancel(1, jid)["state"] == "closing"
    elif how == "expired":
        st = services.gwjoin(1)
        st["expires"] = st["issued"]
        services._gwjoin_save(1, st)
        assert [c["slot"] for c in services.gwjoin_tick()] == [1]
    else:
        connect(services, gwside, 1)
        wrong = "AAAAAA" if gwside.code != "AAAAAA" else "BBBBBB"
        for _ in range(3):
            out = services.gwjoin_code(1, jid, wrong)
        assert out["result"] == "refused"
    services.gwjoin_revert(1, how)
    assert vps.modes[-1] == "--join-revert"
    assert vps.peer_pub("awglink") == before, "прежнему устройству ключи не вернулись"
    assert not vps._p(vps.live("awglink"), ".join-prev").exists() and not vps.stamped("awglink")
    assert services.db.gateway(1).device_id == gw.device_id and services.gwjoin(1) == {}
    assert not services.gwjoin_replacing(1)


def _keep_block(services, dev_pub: str) -> None:
    services._gwkeep_save(1, {"items": gwkeep.clean(ITEMS), "hash": gwkeep.print_(ITEMS), "dev": dev_pub,
                              "at": "2026-10-01T10:00:00+03:00"})


def test_the_right_code_replaces_the_device_and_asks_about_the_settings(services, vps):
    """Верный код замены: устройство слота — новое «Шлюз», снимок и
    возможности прежнего забыты, .join-prev прочь после фиксации; первый
    hello нового агента — вопрос о настройках (keep_back why=device)."""
    gw, _before, _res, gwside = _replace(services, vps)
    old = services.db.get_device(gw.device_id)
    _keep_block(services, old.public_key)
    services.gwlink_session_opened(1, "3.3.0", gwlink.PROTO, caps=("cfg1", "keep1"))
    connect(services, gwside, 1)
    accept(services, vps, gwside, 1)
    new = services.db.get_device(services.db.gateway(1).device_id)
    assert new.id != old.id and new.name == "Шлюз"
    assert services.db.get_device(old.id).is_gateway == 0
    assert not services.db.get_state("gwlink_caps_1"), "возможности прежнего агента пережили замену"
    assert vps.modes[-1] == "--join-drop" and not vps._p(vps.live("awglink"), ".join-prev").exists()
    reply, body = services.gwkeep_hello(1, "", True, False)
    assert reply == "keep_back" and body["why"] == "device"


def test_reconfigure_keeps_the_device_and_returns_the_settings_silently(services, vps):
    """«🔁 Настроить заново»: то же устройство, новые ключи; настройки бота
    шлюза возвращаются молча (keep_back why=same)."""
    gw, _before, res, gwside = _replace(services, vps, same=True)
    assert res["state"]["kind"] == "same" and res["state"]["device"] == gw.device_id
    _keep_block(services, services.db.get_device(gw.device_id).public_key)
    connect(services, gwside, 1)
    accept(services, vps, gwside, 1)
    assert services.db.gateway(1).device_id == gw.device_id
    reply, body = services.gwkeep_hello(1, "", True, True)
    assert reply == "keep_back" and body["why"] == "same"


def test_replacement_by_an_own_device(services, vps):
    """Замена своим устройством из «моих»: после кода в слоте — оно."""
    services.add_device(services.admin_client().id, "Pi4")
    pi4 = next(d for d in services.db.list_devices(services.admin_client().id) if d.name == "Pi4")
    gw, _before, res, gwside = _replace(services, vps, device_id=pi4.id)
    assert res["state"]["device"] == pi4.id
    connect(services, gwside, 1)
    accept(services, vps, gwside, 1)
    assert services.db.gateway(1).device_id == pi4.id != gw.device_id
