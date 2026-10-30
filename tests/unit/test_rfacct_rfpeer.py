"""Учёт РФ-трафика и РФ-доступ через второй сервер (infra/rfacct): счётчики
`rfp_up`/`rfp_dn` транзита — с адреса второй стороны из линка пары в линки
слотов и обратно, два правила сверх четырёх при паре, переписывание статики,
когда пара появилась или правил в ядре не хватает.

Ядро подменено подставным nft из tests/unit/test_rfacct.py.

Цена ошибки: без счётчиков транзита админ транзитного не видит, сколько чужого
РФ-трафика прошло через его шлюзы (Т-РТ3, Т-РТ11 — нули); счётчик по линку пары
целиком считал бы канал пары и журнал; число правил без пары остаётся «4» при
паре — статика переписывается на каждом опросе (или наоборот, никогда); правило
транзита по линку пары в обе стороны — один и тот же пакет считан дважды;
`delete` в статике — показания эпизода обнуляются каждой перерисовкой.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rfacct
from tests.unit.test_rfacct import SUBS, T, _rules, _state, nft

pytestmark = pytest.mark.unit
__all__ = ["nft"]

PEER4 = "10.99.98.2"
UP = (f'add rule {T} forward iifname "awgpair" oifname {{ "awglink", "awglink2" }} ip saddr {PEER4} '
      f'ip daddr != @private4 counter name "rfp_up"')
DN = (f'add rule {T} forward iifname {{ "awglink", "awglink2" }} oifname "awgpair" ip daddr {PEER4} '
      f'ip saddr != @private4 counter name "rfp_dn"')


def test_with_a_pair_two_transit_counter_rules_follow_the_four(monkeypatch):
    text = rfacct.render_static(["awglink", "awglink2", "awgpair"], SUBS, PEER4)
    rules = _rules(text)
    assert len(rules) == rfacct.rules_count(PEER4) == 6, rules
    assert rules[-2:] == [UP, DN], "правила транзита не те или не по линкам слотов:\n" + "\n".join(rules)
    assert f"add counter {T} rfp_up" in text and f"add counter {T} rfp_dn" in text


def test_the_users_rules_still_count_users_into_the_pair_link(monkeypatch):
    """У просящего трафик пользователя в транзит — его РФ-трафик: линк пары в
    четырёх правилах пользователей вместе с линками слотов."""
    rules = _rules(rfacct.render_static(["awglink", "awgpair"], SUBS, PEER4))
    users = [r for r in rules if "@clients4" in r]
    assert len(users) == 4 and all('{ "awglink", "awgpair" }' in r for r in users), users


def test_without_a_pair_there_are_four_rules_and_no_transit_rule():
    text = rfacct.render_static(["awglink"], SUBS)
    assert rfacct.rules_count() == 4 and len(_rules(text)) == 4
    assert "rfp_up\"" not in "\n".join(_rules(text)) and "awgpair" not in text


def test_only_the_pair_link_and_no_slot_links_gives_no_transit_rule():
    """Своих линков слотов нет — пересылать транзит некуда, правило по пустому
    набору интерфейсов nft не примет."""
    rules = _rules(rfacct.render_static(["awgpair"], SUBS, PEER4))
    assert not [r for r in rules if "rfp_" in r], rules


def test_the_transit_counters_survive_a_static_rewrite():
    """Счётчики объявляются `add counter` (идемпотентно), статика без `delete`:
    перезапись цепочки показаний не трогает."""
    text = rfacct.render_static(["awglink"], SUBS, PEER4)
    assert "delete" not in text and f"flush chain {T} forward" in text


def test_sync_rewrites_the_static_when_the_pair_appears(nft):
    st = _state({1: "10.8.1.2"})
    rfacct.sync(st, ["awglink"], SUBS, [(1, "10.8.1.2")])
    n = len(nft.applied)
    assert rfacct.sync(st, ["awglink", "awgpair"], SUBS, [(1, "10.8.1.2")], PEER4) is True
    assert len(nft.applied) == n + 1 and "rfp_up" in nft.applied[-1], "пара появилась — правил транзита нет"


def test_sync_with_a_pair_is_quiet_once_the_six_rules_stand(nft):
    """Ядро с шестью правилами при паре — без изменений тишина; четыре (таблицу
    переписал прежний выпуск) — статика заново."""
    st = _state({1: "10.8.1.2"}, rules=6)
    links = ["awglink", "awgpair"]
    assert rfacct.sync(st, links, SUBS, [(1, "10.8.1.2")], PEER4) is True
    n = len(nft.calls)
    assert rfacct.sync(st, links, SUBS, [(1, "10.8.1.2")], PEER4) is False
    assert len(nft.calls) == n, "при шести правилах и паре nft зовётся на каждом опросе"
    short = _state({1: "10.8.1.2"}, rules=4)
    assert rfacct.sync(short, links, SUBS, [(1, "10.8.1.2")], PEER4) is True
    assert len(_rules(nft.applied[-1])) == 6
