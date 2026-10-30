"""РФ-доступ через второй сервер — сторона просящего (domain/services/reserve/rfpeer/slot.py
и автомат слотов): виртуальный слот «шлюзы «Beta»» (29, линк пары) последним
кандидатом, не липкий; его наличие, вердикт зондом, `AllowedIPs` линка пары,
политика слота, учёт РФ, уход с пропавшего слота, тексты Т-РТ6/Т-РТ7/Т-РТ8/Т-РТ9,
перезапуски, имя в шапке, кворум не задет.

Сервер — «Alpha» с двумя своими слотами и парой с «Beta» (tests/integration/_rfpeer_common.py):
зонды — `probe[слот]`, ядро — модель, часы — модельные, такт — как в
планировщике (живость слотов, затем транзит).

Цена ошибки: слот на мёртвом линке пары или без согласия «Beta» — РФ-трафик
пользователей уходит в никуда, а рубильник не снимается; слот раньше живого
своего — пользователи получают адрес чужого шлюза при живом своём; липкий
слот — остаются на нём и после возврата своего шлюза; пропавший активный слот
без немедленного ухода — РФ-доступ пропадает молча до пяти минут (маршрут
таблицы РФ-доступа — в снятый линк); Т-РТ8 не приходит — админ не знает, что
российские сервисы у всех открываются с зарубежного адреса; Т-РТ8 каждый такт —
критичные письма потоком; узкие `AllowedIPs` после переподнятия линка —
ответы из РФ отбрасываются линком; холодный старт на слот второго сервера —
пользователи идут через чужие шлюзы при живых своих; ссылка «gw-29» в шапке —
«Такого шлюза нет»; транзит, тронувший эпоху или кворум, — ложный переезд.
"""
from __future__ import annotations

import itertools
import os

import pytest

from awgbot.core import config, settings
from awgbot.domain.notices import reserve as rn
from awgbot.domain.services import Services
from awgbot.infra import routing
from awgbot.infra.routing import rfpeer as rfp
from awgbot.util import timeutil

from tests.integration import _rfpeer_common as rp

pytestmark = pytest.mark.integration
rf = rp.rf
V = rp.VSLOT


def _ready(s) -> list:
    """Согласие «Beta» на диске, линк пары жив, три хороших у всех."""
    rp.peer_rfp(s, offer=True)
    return rp.settle(s)


def _to_peer(s) -> list:
    """Свои оба легли, слот 29 готов — переключение на него."""
    _ready(s)
    rp.own_down(s)
    return rp.ticks(s, s._rt_fail_need())


# ── наличие слота ───────────────────────────────────────────────────────────

ROWS = list(itertools.product((True, False), repeat=6))


@pytest.mark.parametrize("pair, guest, own, eng, offer, fresh", ROWS,
                         ids=["-".join(n if v else "no_" + n for n, v in
                                       zip(("pair", "guest", "own", "eng", "offer", "hs"), r)) for r in ROWS])
def test_the_peer_slot_exists_only_when_every_condition_holds(rf, fake_routing, monkeypatch,
                                                               pair, guest, own, eng, offer, fresh):
    if not pair:
        rf.db.reserve_peer_delete()
    monkeypatch.setattr(config, "GUEST", guest)
    if not own:
        monkeypatch.setattr(rf.db, "gateways", lambda: [])
    fake_routing.enabled = eng
    rp.peer_rfp(rf, offer=offer)
    rf.k.hs_age = 60 if fresh else 181
    slot = rf.rt_peer_slot()
    want = pair and not guest and own and eng and offer and fresh
    assert (slot is not None) is want
    if want:
        assert (slot.id, slot.link_if, slot.device_id, slot.label) == (V, "awgpair", 0, "шлюзы «Beta»")


def test_the_slot_list_is_stable_across_repeated_reads(rf):
    """Слоты читают такт, экраны и шапка по многу раз — состав от числа чтений не зависит."""
    rp.peer_rfp(rf, offer=True)
    first = [g.id for g in rf.rt_slots()]
    assert first == [1, 2, V], "слот второго сервера не последний"
    for _ in range(3):
        assert [g.id for g in rf.rt_slots()] == first


def test_without_own_slots_there_is_no_peer_slot_in_the_list(rf, monkeypatch):
    rp.peer_rfp(rf, offer=True)
    monkeypatch.setattr(rf.db, "gateways", lambda: [])
    assert rf.rt_slots() == [] and rf.active_gateway() is None, "РФ-доступа не было — и транзита нет"


def test_a_missing_awg_binary_means_no_slot_and_no_crash(rf):
    rp.peer_rfp(rf, offer=True)
    rf.k.hs_age = None
    assert rf.rt_peer_slot() is None


# ── автомат слотов ──────────────────────────────────────────────────────────

def test_own_slots_down_and_the_peer_slot_ready_switch_in_the_same_tick(rf):
    notes = _to_peer(rf)
    assert rf.active_gateway().id == V and rf.switched == ["awgpair"], rf.switched
    assert rf.db.get_state(rf._RT_LINK_KEY) == "1", "рубильник снят, хотя слот второго сервера жив"
    assert rp.crit(notes) == [], f"критичное при живом запасном пути: {rp.crit(notes)}"
    on = [n for n in notes if n.text.startswith("🔁 РФ-доступ — через шлюзы «Beta»")]
    assert len(on) == 1 and on[0].silent, rp.texts(notes)
    assert not [t for t in rp.texts(notes) if "Шлюз переключён" in t or "останусь" in t.lower()], rp.texts(notes)
    assert rf.reserve_rfp_st_part()["use"], "st.rfp.use не выставлен — «Beta» не узнает об эпизоде"


def test_the_switch_notice_names_the_return_time(rf):
    notes = _to_peer(rf)
    since = rf.rsv_get("rsv_rfpeer")["use"]["since"]
    until = timeutil.to_iso(timeutil.shift(timeutil.parse_iso(since), 600))
    assert rn.rf_transit_on("Beta", since, until) in rp.texts(notes)


def test_two_own_slots_take_the_second_own_before_the_peer(rf):
    _ready(rf)
    rp.own_down(rf, 1)
    notes = rp.ticks(rf, rf._rt_fail_need())
    assert rf.active_gateway().id == 2 and rf.switched == ["awglink2"], "слот второго сервера раньше живого своего"
    assert not rf.reserve_rfp_st_part()["use"] and not [t for t in rp.texts(notes) if "шлюзы «Beta»" in t]


def test_own_down_and_no_offer_drop_the_switch_with_the_reason_tail(rf):
    _ready(rf)
    rp.peer_rfp(rf, offer=False, why="down")
    rp.own_down(rf)
    notes = rp.ticks(rf, rf._rt_fail_need())
    assert rf.db.get_state(rf._RT_LINK_KEY) == "0"
    red = rp.crit(notes)
    assert len(red) == 1 and red[0].startswith("🔴 Шлюз"), rp.texts(notes)
    tail = red[0].rsplit("\n", 1)[-1]
    assert tail.startswith("Запасного РФ-доступа через шлюзы «Beta» нет: ") and len(tail) > 45, \
        f"«🔴» без хвоста Т-РТ9 — админ не знает, почему нет запасного пути: {red[0]}"
    # дословная причина — та же, что в строке карточки Т-РТ2 (tests/e2e/test_reserve_rfpeer_ui.py)


def test_own_down_and_the_peer_slot_not_answering_drop_the_switch(rf):
    _ready(rf)
    rf.probe[V] = "down"
    rp.own_down(rf)
    notes = rp.ticks(rf, rf._rt_fail_need())
    assert rf.active_gateway().id == 1 and "awgpair" not in rf.switched
    assert rf.db.get_state(rf._RT_LINK_KEY) == "0" and len(rp.crit(notes)) == 1
    assert not [t for t in rp.texts(notes) if "Резервный шлюз" in t and "Beta" in t], \
        "письмо «резервный не отвечает» о слоте второго сервера — это строка карточки"


def test_back_to_the_own_slot_after_three_good_and_ten_minutes(rf):
    _to_peer(rf)
    rf.probe[1] = "ok"
    notes = rp.ticks(rf, rf._RT_UP_STREAK + 2)
    assert rf.active_gateway().id == V, "вернулись раньше 10 минут после переключения"
    rf.clock.step(600)
    notes = rp.ticks(rf, 1)
    assert rf.active_gateway().id == 1 and rf.switched[-1] == "awglink"
    assert rp.texts(notes) == [rn.rf_transit_back("Beta", "Pi4")], rp.texts(notes)
    assert rp.texts(notes)[0] == "🟢 РФ-доступ снова через свой шлюз «Pi4» — шлюзы «Beta» больше не нужны"
    assert rn.rf_transit_back("Beta", "Pi4", "дача") == "🟢 РФ-доступ снова через свой шлюз «Pi4» (дача) — шлюзы «Beta» больше не нужны"
    assert not rf.reserve_rfp_st_part()["use"], "st.rfp.use не снят — «Beta» считает эпизод идущим"
    assert rp.ticks(rf, 3) == []


CAUSES = [
    ("off", "его админ выключил «Мои шлюзы для резерва»"),
    ("down", "шлюз «Beta» не отвечает"),
    ("link", "нет связи с «Beta»"),
]


def _gone(s, cause: str) -> list:
    """Слот 29 пропал при активном: отзыв согласия (с причиной «Beta») или обрыв линка пары."""
    if cause == "link":
        s.k.hs_age = 400
        rp.fresh_handshake(s)
    else:
        rp.peer_rfp(s, offer=False, why=cause)
    return rp.tick(s)


@pytest.mark.parametrize("cause", [c for c, _ in CAUSES])
def test_the_peer_slot_gone_while_active_moves_traffic_home_in_the_same_tick(rf, cause):
    _to_peer(rf)
    n = len(rf.switched)
    _gone(rf, cause)
    assert rf.switched[n:] == ["awglink"], f"маршрут РФ-доступа остался в линке пары ({cause}): {rf.switched}"
    assert rf.db.get_state(rf._RT_ACTIVE_KEY) == "1", "активным записан пропавший слот"


@pytest.mark.parametrize("cause, why", CAUSES)
def test_the_peer_slot_gone_with_own_dead_is_told_once_as_critical(rf, cause, why):
    _to_peer(rf)
    notes = _gone(rf, cause)
    red = rp.crit(notes)
    assert len(red) == 1 and red[0].startswith("🔴 РФ-доступ через шлюзы «Beta» прекратился"), rp.texts(notes)
    assert not [t for t in rp.texts(notes) if t.startswith("🟢 РФ-доступ снова через свой шлюз")], \
        "«снова через свой шлюз» при мёртвых своих"
    assert not rp.crit(rp.ticks(rf, 5)), "Т-РТ8 повторилось"


@pytest.mark.parametrize("cause, why", CAUSES)
def test_the_lost_notice_names_the_peer_in_its_reason(rf, cause, why):
    _to_peer(rf)
    red = rp.crit(_gone(rf, cause))
    assert red == [rn.rf_transit_lost("Beta", why)], red


def test_waiting_for_the_interval_says_nothing_about_the_returning_own_slot(rf):
    """Свой ожил раньше 10 минут: ждём молча — ни «остаётся в резерве» (он заберёт
    трафик сам), ни иного письма о нём."""
    _to_peer(rf)
    rf.probe[1] = "ok"
    notes = rp.ticks(rf, rf._RT_UP_STREAK + 2)
    assert rf.active_gateway().id == V
    assert [t for t in rp.texts(notes) if "Pi4" in t] == [], rp.texts(notes)


def test_the_peer_slot_gone_while_active_with_own_alive_says_back_once(rf):
    _to_peer(rf)
    rf.probe[1] = "ok"
    rp.ticks(rf, rf._RT_UP_STREAK)                            # свой ожил, 10 минут ещё не прошли
    assert rf.active_gateway().id == V
    rp.peer_rfp(rf, offer=False, why="off")
    notes = rp.tick(rf)
    assert rf.active_gateway().id == 1
    assert rp.crit(notes) == [], "Т-РТ8 при живом своём"
    assert rp.texts(notes).count(rn.rf_transit_back("Beta", "Pi4")) == 1, rp.texts(notes)


def test_unpairing_in_an_episode_goes_home_and_tells_the_reason(rf):
    _to_peer(rf)
    n = len(rf.switched)
    rf.reserve_unpair()
    notes = rp.ticks(rf, 2)
    assert rf.switched[n:][:1] == ["awglink"], f"после удаления пары трафик в снятом линке: {rf.switched}"
    assert [t for t in rp.crit(notes) if "резерв удалён" in t], rp.texts(notes)


def test_manual_hold_keeps_the_own_slot(rf):
    _ready(rf)
    rf.db.set_state(rf._RT_HOLD_KEY, "1")
    rp.own_down(rf)
    rp.ticks(rf, rf._rt_fail_need() + 2)
    assert rf.active_gateway().id == 1 and "awgpair" not in rf.switched, "автомат снял ручное удержание"


def test_failover_off_keeps_the_own_slot(rf):
    settings.set_value("app.routing.failover.enabled", False)
    _ready(rf)
    rp.own_down(rf)
    rp.ticks(rf, rf._rt_fail_need() + 2)
    assert rf.active_gateway().id == 1 and "awgpair" not in rf.switched


def test_right_after_an_own_switch_the_peer_slot_waits_for_the_interval(rf):
    _ready(rf)
    rp.own_down(rf, 1)
    rp.ticks(rf, rf._rt_fail_need())
    assert rf.active_gateway().id == 2
    rp.own_down(rf, 2)
    rp.ticks(rf, rf._rt_fail_need())
    assert rf.active_gateway().id == 2, "второе переключение подряд раньше интервала"
    rf.clock.step(600)
    rp.tick(rf)
    assert rf.active_gateway().id == V and rf.switched[-1] == "awgpair"


# ── вердикт слота 29 ───────────────────────────────────────────────────────

@pytest.fixture()
def real_probe(rf, monkeypatch):
    """Вердикт слота 29 — настоящий `_spare_verdict` с подменённым зондом наружу."""
    calls = []
    verdict = {"v": "down"}
    from awgbot.domain.services.reserve.rfzone import ReserveRfZoneMixin
    monkeypatch.setattr(rf, "_spare_verdict", ReserveRfZoneMixin._spare_verdict.__get__(rf))
    part = next(m for m in routing._PARTS if "probe_gateway" in vars(m))
    monkeypatch.setattr(part, "probe_gateway", lambda targets, port, **kw: calls.append(kw) or verdict["v"])
    return calls, verdict


def test_the_peer_slot_is_probed_through_its_own_table_by_its_mark(rf, real_probe):
    calls, verdict = real_probe
    rp.peer_rfp(rf, offer=True)
    slot = rf.rt_peer_slot()
    verdict["v"] = "ok"
    assert rf._spare_verdict(slot, False) == "ok"
    assert calls[-1]["iface"] == "awgpair" and calls[-1]["mark"] == 1 << V, calls


def test_pair_channel_traffic_is_no_evidence_for_the_peer_slot(rf, fake_routing, real_probe):
    """По линку пары идут канал пары и журнал: рост rx не доказывает путь в РФ —
    решает только зонд."""
    calls, verdict = real_probe
    rp.peer_rfp(rf, offer=True)
    fake_routing.link_rx_step = 1 << 20
    slots = [rf.db.gateway(1), rf.db.gateway(2), rf.rt_peer_slot()]
    verdict["v"] = "down"
    out = rf._probe_slots(slots, slots[0])
    assert out[V] == "down", "живой канал пары сочтён живым путём в РФ"


# ── AllowedIPs ─────────────────────────────────────────────────────────────

def test_allowed_ips_widen_with_the_slot_and_narrow_without(rf):
    _ready(rf)
    assert rf.k.wide, "слот есть, а AllowedIPs узкие — ответы из РФ отбросит линк"
    assert rfp.read()["slot"] is True and rf.fw[-1].get("rfp_self4") == rp.SELF4
    rp.peer_rfp(rf, offer=False, why="off")
    rp.tick(rf)
    assert rf.k.allowed == [f"{rp.PEER4}/32"], rf.k.allowed
    assert "rfp_self4" not in rf.fw[-1], "слот пропал — таблица с правилами просящего"


def test_no_change_means_no_awg_set(rf):
    _ready(rf)
    n = len(rf.k.awg_sets())
    rp.ticks(rf, 12)
    assert len(rf.k.awg_sets()) == n, "awg set на тактах без изменений"


def test_a_link_raised_from_its_config_is_widened_within_one_tick(rf):
    _ready(rf)
    rp.ticks(rf, 2)
    rf.k.allowed = [f"{rp.PEER4}/32"]                         # --reassert / смена ключей: конфиг переписан
    conf = f"{config.AWG_DIR}/awgpair.conf"
    os.utime(conf, ns=(os.stat(conf).st_atime_ns, os.stat(conf).st_mtime_ns + 1_000_000))
    rp.tick(rf)
    assert rf.k.wide, "переподнятый линк пары остался узким дольше такта"


# ── политика слота 29 ──────────────────────────────────────────────────────

def test_the_peer_slot_gets_its_table_rule_and_clamp_and_loses_them(rf, monkeypatch):
    laid = []
    part = next(m for m in routing._PARTS if "ensure_policy" in vars(m))
    monkeypatch.setattr(part, "ensure_policy", lambda active_iface="", slots=(): laid.append(list(slots)))
    _ready(rf)
    assert (V, "awgpair") in rf.gateway_slots_policy()
    assert laid and (V, "awgpair") in laid[-1], "таблица 129, правило метки и MSS-кламп слоту не ставятся"
    rp.peer_rfp(rf, offer=False, why="off")
    rp.ticks(rf, 2)
    assert (V, "awgpair") not in rf.gateway_slots_policy()
    assert ("drop", V) in rf.slot_policy, "таблица 129 и правило метки пережили пропажу слота"


# ── учёт РФ ────────────────────────────────────────────────────────────────

def test_the_pair_link_is_an_rf_link_only_with_a_pair_and_own_links(rf, monkeypatch):
    assert rf.rf_links()[-1] == "awgpair" and "awglink" in rf.rf_links()
    assert rf._rf_pair_peer4() == rp.PEER4
    rf.db.reserve_peer_delete()
    assert "awgpair" not in rf.rf_links() and rf._rf_pair_peer4() == ""
    rp.rc.pair_row(rf)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    monkeypatch.setattr(rf.db, "gateways", lambda: [])
    assert rf.rf_links() == [], "учёт по одному линку пары без своих линков"


# ── перезапуски ────────────────────────────────────────────────────────────

def _restart(s):
    again = Services(s.db)
    again.k, again.probe, again.switched, again.fw, again.clock, again.slot_policy = \
        s.k, s.probe, s.switched, s.fw, s.clock, s.slot_policy
    again._probe_slot = lambda g, active=False: s.probe[g.id]
    again._spare_verdict = lambda g, active: s.probe[g.id]
    again._rt_standby_interval = lambda: 0
    again._rt_window_size = lambda: 10
    again._gw_firewall_refresh = s._gw_firewall_refresh
    return again


def test_a_warm_start_in_an_episode_keeps_the_peer_slot_without_switching(rf, monkeypatch):
    _to_peer(rf)
    laid = []
    part = next(m for m in routing._PARTS if "ensure_policy" in vars(m))
    monkeypatch.setattr(part, "ensure_policy", lambda active_iface="", slots=(): laid.append(active_iface))
    again = _restart(rf)
    n = len(rf.switched)
    notes = rp.tick(again)
    assert again.active_gateway().id == V and rf.switched[n:] == [], "тёплый старт переключил трафик"
    assert laid and set(laid) == {"awgpair"}, f"первый такт вернул маршрут на мёртвый свой линк: {laid}"
    assert notes == [], rp.texts(notes)


def test_a_cold_start_chooses_only_among_own_slots(rf, monkeypatch):
    _to_peer(rf)
    from awgbot.infra import bootid
    rf.db.set_state(rf._RT_BOOT_KEY, "boot-before")
    monkeypatch.setattr(bootid, "read_boot_id", lambda path=bootid.BOOT_ID_PATH: "boot-after")
    again = _restart(rf)
    got = again.routing_cold_start()
    assert got.id in (1, 2), f"холодный старт выбрал слот второго сервера: {got.id}"
    assert again.db.get_state(again._RT_ACTIVE_KEY) in ("1", "2")


# ── имя и ссылки ───────────────────────────────────────────────────────────

def test_the_peer_slot_is_named_by_the_peer_without_a_card_link(rf, monkeypatch):
    monkeypatch.setattr(rf, "bot_username", "alpha_vpn_bot", raising=False)
    rp.peer_rfp(rf, offer=True)
    slot = rf.rt_peer_slot()
    assert rf._gw_display(slot) == "шлюзы «Beta»"
    assert "gw-29" not in rf._gw_link(slot) and "gw-29" not in rf._gw_card_link(slot)


def test_the_admin_header_names_the_peer_slot_and_does_not_link_it(rf, monkeypatch):
    from awgbot.bot.texts.routing.access import routing_admin_status_line
    _to_peer(rf)
    info = rf.routing_admin_status()
    assert info["active"] == "шлюзы «Beta»", info
    line = routing_admin_status_line(info, "alpha_vpn_bot")
    assert "gw-29" not in line, f"ссылка на несуществующую карточку слота: {line}"


# ── кворум не задет ────────────────────────────────────────────────────────

def test_the_episode_leaves_the_epoch_and_the_quorum_untouched(rf):
    before = (rf.db.jrn_meta(), rf.rsv_get("rsv_quorum"), rf.reserve_out(), rf.reserve_in())
    _to_peer(rf)
    rf.probe[1] = "ok"
    rp.ticks(rf, 3)
    rf.clock.step(600)
    rp.ticks(rf, 2)
    after = (rf.db.jrn_meta(), rf.rsv_get("rsv_quorum"), rf.reserve_out(), rf.reserve_in())
    assert after == before, "эпизод транзита сдвинул эпоху, кворум или направления"
