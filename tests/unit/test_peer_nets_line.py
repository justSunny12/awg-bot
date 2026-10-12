"""Строка «↔️ Связь подсетей» экрана «🛰 Шлюзы» и диалог «🔀 VPN-транзит»: пары
в адресах в туннеле — до шести на слот, «и ещё N»; экран при 15 слотах с
длинными именами и многими подсетями укладывается в лимит Telegram; при
выключенных адресах в туннеле — отсылка к карточке, строки о связи с
соседями в диалоге нет.

Цена ошибки: экран длиннее 4096 знаков Telegram не принимает — «Шлюзы» не
открываются вовсе; обещание связи, которой нет, — человек ищет поломку в сети.
"""
from __future__ import annotations

from types import SimpleNamespace

from awgbot.bot import texts
from awgbot.bot.texts.routing.dialogs import gateway_lan_ask
from tests.screens.harness import plain as plain_text

TG_MAX = 4096


def _nets(slot: int, n: int) -> list[str]:
    return [f"10.{239 + slot}.{100 + i}.0/24" for i in range(n)]


def test_up_to_six_addresses_per_slot_then_and_n_more():
    info = {"state": "ok", "pairs_named": [("Pi4", "дача", _nets(1, 8)), ("Pi5", "", _nets(2, 6))]}
    line = texts.peer_nets_line(info)
    assert line.count("<code>") == 12
    assert "<code>10.240.105.0/24</code> и ещё 2 ↔️ Pi5: " in line, line
    assert "и ещё" not in line.split("↔️ Pi5")[1], "ровно шесть — без «и ещё 0»"


def test_the_off_state_points_to_the_card():
    assert texts.peer_nets_line({"state": "off"}) == (
        "↔️ Связь подсетей: выключена вместе с адресами в туннеле — см. карточку шлюза")


def test_the_gateways_screen_with_fifteen_slots_fits_telegram():
    """15 слотов (предел адресов в туннеле), длинные имена и подписи, у
    каждого — много подсетей: экран всё равно в пределах 4096."""
    states = []
    for sid in range(1, 16):
        gw = SimpleNamespace(id=sid, label="Л" * 20, lan_subnets=[], lan_mode=1)
        states.append({"gateway": gw, "device": SimpleNamespace(name=f"Шлюз-{sid:02d}-{'Ж' * 12}"),
                       "active": sid == 1, "preferred": sid == 1, "link_ok": True, "ping": (43, 0),
                       "display": f"Шлюз-{sid:02d}"})
    info = {"state": "ok", "pairs_named": [(st["device"].name, "Л" * 20, _nets(sid, 12))
                                           for sid, st in enumerate(states, 1)]}
    text = texts.gateways_text(states, peer_info=info, switched_at="2026-10-12T10:00:00+03:00",
                               lists={"count": 41200, "age_seconds": 7200}, auto_on=False)
    visible = plain_text(text)                        # Telegram считает текст без разметки
    assert len(visible) <= TG_MAX, len(visible)


def _dialog_state(problem: str) -> dict:
    me = {"gateway": SimpleNamespace(id=2, label="", lan_subnets=["192.168.70.0/24"], lan_mode=0),
          "device": SimpleNamespace(name="Pi5")}
    other = {"gateway": SimpleNamespace(id=1, label="", lan_subnets=["192.168.1.0/24"], lan_mode=1),
             "device": SimpleNamespace(name="Pi4")}
    return {**me, "states": [other, me], "channel": {"online": True}, "lan_space": {"problem": problem}}


def test_the_transit_dialog_promises_no_link_while_the_addresses_are_off():
    on = gateway_lan_ask(_dialog_state(""), True, "10.9.1.1", ["Pi4"])
    assert "Сеть свяжется с сетями других шлюзов с VPN-транзитом (Pi4)" in on
    assert "после включения перевыпусти конфигурацию: Pi4" in on
    off = gateway_lan_ask(_dialog_state("служебное пространство 10.0.0.0/12 пересекается с подсетью клиентов"),
                          True, "10.9.1.1", [])
    assert "Сеть свяжется" not in off and "Агентам v3.2" not in off


def test_a_slot_without_its_channel_is_not_asked_twice_in_the_dialog():
    """Сам слот без канала диалог уже просит перевыпустить («Потребуется
    перевыпуск конфигурации шлюза») — в списке соседей второй раз его нет."""
    st = {**_dialog_state(""), "display": "Pi5", "channel": {"online": False}}
    text = gateway_lan_ask(st, True, "10.9.1.1", ["Pi5", "Pi4"])
    assert "после включения перевыпусти конфигурацию: Pi4" in text and "конфигурацию: Pi5" not in text, text
    st["channel"] = {"online": True}
    assert "перевыпусти конфигурацию: Pi5, Pi4" in gateway_lan_ask(st, True, "10.9.1.1", ["Pi5", "Pi4"])

