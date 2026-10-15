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

import pytest

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
        "🔗 Связь подсетей: выключена вместе с адресами в туннеле — см. карточку шлюза")


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
    assert "из подсетей других шлюзов с включённым VPN-транзитом (Pi4)" in on
    assert "Потребуется перевыпуск конфигурации шлюза: Pi4" in on
    off = gateway_lan_ask(_dialog_state("служебное пространство 10.0.0.0/12 пересекается с подсетью клиентов"),
                          True, "10.9.1.1", [])
    assert "🔗 Связь подсетей:" not in off and "Потребуется перевыпуск" not in off


def test_a_slot_without_its_channel_is_not_asked_twice_in_the_dialog():
    """О перевыпуске — одна строка с именами: сам слот без канала в ней
    первым и один раз, даже если сервис назвал его среди соседей."""
    st = {**_dialog_state(""), "display": "Pi5", "channel": {"online": False}}
    text = gateway_lan_ask(st, True, "10.9.1.1", ["Pi5", "Pi4"])
    assert text.count("Потребуется перевыпуск") == 1 and "конфигурации шлюза: Pi5, Pi4" in text, text
    st["channel"] = {"online": True}
    assert "конфигурации шлюза: Pi5, Pi4" in gateway_lan_ask(st, True, "10.9.1.1", ["Pi5", "Pi4"])
    assert "Потребуется перевыпуск" not in gateway_lan_ask(st, True, "10.9.1.1", [])



def test_the_announcement_with_fifteen_slots_fits_telegram():
    """Сводка после обновления: 15 слотов с длинными именами и по 64 подсети —
    при стольких шлюзах до трёх пар на слот («и ещё N»), сообщение в пределах
    4096; два шлюза — до шести."""
    from awgbot.domain.notices.lanmap import lan_space_moved
    rows = [(f"<a href=\"https://t.me/b?start=gw-{sid}\">Шлюз-{sid:02d}-{'Ж' * 20} ({'Л' * 20})</a>",
             [(f"192.168.{i}.0/24", f"10.{239 + sid}.{i}.0/24") for i in range(64)]) for sid in range(1, 16)]
    text = lan_space_moved(rows, smb=True)
    assert "и ещё 61" in text
    assert len(plain_text(text)) <= TG_MAX, len(plain_text(text))
    assert "и ещё 58" in lan_space_moved(rows[:2], smb=False)


def test_nine_slots_and_more_show_up_to_three_addresses_each():
    """При девяти слотах и больше — до трёх адресов на слот, как в сводке."""
    many = {"state": "ok", "pairs_named": [(f"Pi{i}", "", _nets(i, 5)) for i in range(1, 10)]}
    line = texts.peer_nets_line(many)
    assert line.count("<code>") == 27 and line.count("и ещё 2") == 9, line
    few = {"state": "ok", "pairs_named": many["pairs_named"][:8]}
    assert texts.peer_nets_line(few).count("<code>") == 40


def test_the_off_dialog_names_who_needs_a_file():
    st = {**_dialog_state(""), "display": "Pi5", "channel": {"online": True}}
    text = gateway_lan_ask(st, False, "", ["Pi5", "Pi<4>"])
    assert "Потребуется перевыпуск конфигурации шлюза: Pi5, Pi&lt;4&gt;" in text, text
    assert "Потребуется перевыпуск" not in gateway_lan_ask(st, False, "", [])


def test_the_card_caps_peer_names_and_subnet_warnings():
    from awgbot.bot.texts.routing.card import lan_space_lines, peer_links_line
    assert peer_links_line(["A", "B", "C"]) == "🔗 Связь подсетей: с A, B, C"
    assert peer_links_line(["A", "B", "C", "D", "E"]) == "🔗 Связь подсетей: с A, B, C и ещё 2"
    issues = [{"kind": "wide", "net": f"10.{i}.0.0/15"} for i in range(5)]
    lines = lan_space_lines({"issues": issues})
    assert len(lines) == 4 and lines[-1] == "⚠️ Ещё 2 подсети вне адресов в туннеле: проверь их в «🗺 Подсети»", lines
    assert len(lan_space_lines({"issues": issues[:3]})) == 3


@pytest.mark.parametrize("name, label, star, fit, want", [
    ("Pi4", "ЕК-22П10", False, True, "Pi4 (ЕК-22П10)"),
    ("Raspberry Pi 5 NAS", "дача", False, True, "Raspberry… (дача)"),       # режется имя, подпись цела
    ("Raspberry Pi 5 NAS", "дача", True, True, "⭐ Raspberr… (дача)"),        # звезда в счёте
    ("Raspberry Pi 5 NAS", "дача", False, False, "Raspberry Pi 5 NAS (дача)"),  # одна в ряду — не режем
    ("Raspberry Pi 5 NAS2", "", False, True, "Raspberry Pi 5 NA…"),
    ("Pi5", "очень длинная подпись", False, True, "Pi5 (очень длинна…"),   # подпись длиннее половины — целиком
    ("Шлюз-01-ЖЖЖЖЖЖЖЖЖЖ", "", False, True, "Шлюз-01-ЖЖЖЖЖЖЖЖЖЖ"),         # ровно 18 — без «…»
    ("Шлюз-01-ЖЖЖЖЖЖЖЖЖЖЖ", "", False, True, "Шлюз-01-ЖЖЖЖЖЖЖЖЖ…"),        # 19 — с «…»
])
def test_the_slot_button_keeps_the_label_when_cutting(name, label, star, fit, want):
    """Подпись слота — то, чем различают шлюзы с похожими именами: при обрезке
    режется имя, подпись остаётся; предел 18 знаков считается со звездой."""
    from awgbot.bot.keyboards.routing import slot_button_label
    got = slot_button_label(name, label, star=star, fit=fit)
    assert got == want, got
    assert not fit or len(got) <= 18
