"""РФ-доступ через второй сервер — сторона транзитного (domain/services/reserve/rfpeer/offer.py):
правило согласия, поле `rfp` в `st` (необязательное, в отпечатке смены), `rf_via`
гостя тем же правилом, такт согласия (файл для файервола, правило 151,
перерисовка, `st` по смене), эпизод второй стороны (Т-РТ10 один раз, Т-РТ11 с
объёмом за эпизод), удаление пары.

Сервер — «Alpha» с двумя слотами и парой с «Beta» (tests/integration/_rfpeer_common.py);
в этих тестах «Alpha» — транзитный: «Beta» ходит через её шлюзы.

Цена ошибки: согласие при мёртвом своём слоте — «Beta» переключает пользователей
на путь, который никуда не ведёт, и они теряют РФ-доступ, хотя их свои шлюзы,
может, и ответили бы; согласие при активном виртуальном слоте — петля: оба
сервера отдают РФ-трафик друг другу; после перезапуска окно замеров пусто —
согласие на мёртвый слот до первых пяти замеров; смена согласия без `st` —
вторая сторона не узнаёт об отзыве до следующей смены чего-нибудь ещё; правило
151 и таблица файервола не следуют за согласием — путь открыт после отзыва или
закрыт при согласии; Т-РТ10 после каждого перезапуска — админ получает одно и
то же снова; Т-РТ11 с отрицательным объёмом после перезагрузки.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.core import config, settings
from awgbot.domain.notices import reserve as rn
from awgbot.domain.services import Services
from awgbot.infra.routing import rfpeer as rfp
from awgbot.util import pairproto, timeutil
from awgbot.util.textfmt.base import human_bytes

from tests.integration import _rfpeer_common as rp

pytestmark = pytest.mark.integration
rf = rp.rf
MB = 1 << 20


# ── правило согласия: таблица истинности ───────────────────────────────────

def _want(via, gw, eng, link, virtual, avail) -> tuple[bool, str]:
    """Правило концепции: первое невыполненное — причиной."""
    for ok, why in ((via, "off"), (gw, "no_gw"), (eng, "rt_off"), (link, "down"), (not virtual, "peer"),
                    (avail, "down")):
        if not ok:
            return False, why
    return True, ""


ROWS = [r for r in itertools.product((True, False), repeat=6)
        if not (r[5] is False and r[4] is False and not r[1])]          # без слотов «активного» нет — строки равны


def _arrange(s, fake_routing, monkeypatch, via, gw, eng, link, virtual, avail):
    settings.set_value("app.reserve.rf_via_mine", via)
    fake_routing.enabled = eng
    s.db.set_state(s._RT_LINK_KEY, "1" if link else "0")
    if virtual:
        rp.peer_rfp(s, offer=True)                            # «Beta» даёт свои шлюзы — у меня стоит слот 29
        s.db.set_state(s._RT_ACTIVE_KEY, str(rp.VSLOT))       # и я сам хожу через них
    active = rp.VSLOT if virtual and gw and eng else 1
    if not avail:
        for _ in range(s._rt_fail_need()):
            s._rt_window_push(active, False)
    if not gw:
        monkeypatch.setattr(s.db, "gateways", lambda: [])


@pytest.mark.parametrize("via, gw, eng, link, virtual, avail", ROWS,
                         ids=["-".join(n if v else "no_" + n for n, v in
                                       zip(("via", "gw", "eng", "link", "virt", "avail"), r)) for r in ROWS])
def test_the_offer_follows_the_rule_and_names_the_first_unmet_condition(rf, fake_routing, monkeypatch,
                                                                        via, gw, eng, link, virtual, avail):
    _arrange(rf, fake_routing, monkeypatch, via, gw, eng, link, virtual, avail)
    want = _want(via, gw, eng, link, virtual and gw and eng, avail)
    assert rf.reserve_rf_offer() == want


@pytest.mark.parametrize("via, gw, eng, link, virtual, avail", ROWS[:16])
def test_the_guest_gets_rf_via_by_the_same_rule(rf, fake_routing, monkeypatch, via, gw, eng, link, virtual, avail):
    """`rf_via` гостю — то же согласие: при активном виртуальном слоте гость
    запасного слота не получает (иначе петля через зону)."""
    _arrange(rf, fake_routing, monkeypatch, via, gw, eng, link, virtual, avail)
    assert rf.reserve_rf_via_view()["rf_via"] is rf.reserve_rf_offer()[0]


def test_after_a_restart_with_an_empty_window_a_dead_slot_gives_down_not_an_offer(rf):
    """Свои слоты легли, рубильник снят; бот перезапустился — окно замеров в памяти
    пусто, и «доступен» о мёртвом слоте сказал бы только признак на диске."""
    rp.settle(rf)
    rp.own_down(rf)
    rp.ticks(rf, rf._rt_fail_need())
    assert rf.db.get_state(rf._RT_LINK_KEY) == "0" and rf.reserve_rf_offer() == (False, "down")
    again = Services(rf.db)
    assert again._rt_window(1) == [], "окно пережило перезапуск — тест ничего не проверяет"
    assert again.reserve_rf_offer() == (False, "down"), "после перезапуска — согласие на мёртвый слот"


def test_one_own_slot_down_moves_traffic_and_keeps_the_offer(rf):
    rp.settle(rf)
    rp.own_down(rf, 1)
    rp.ticks(rf, rf._rt_fail_need())
    assert rf.active_gateway().id == 2 and rf.reserve_rf_offer() == (True, "")


# ── поле rfp в st ──────────────────────────────────────────────────────────

def test_rfp_is_an_optional_st_field_and_must_be_a_dict(rf):
    """Прежний сервер линии без поля `rfp` — st принимается; поле не словарем — отказ разбора."""
    rp.settle(rf)
    body = rf.reserve_st_body()
    assert "rfp" in body
    assert pairproto.check(pairproto.PAIR_PROTO, "st", body, pairproto.INVITER)["rfp"] == body["rfp"]
    old = {k: v for k, v in body.items() if k != "rfp"}
    assert "rfp" not in pairproto.check(pairproto.PAIR_PROTO, "st", old, pairproto.INVITER), \
        "st без поля rfp не принят — сессия с прежним сервером рвётся"
    with pytest.raises(pairproto.ProtocolError):
        pairproto.check(pairproto.PAIR_PROTO, "st", {**body, "rfp": "offer"}, pairproto.INVITER)


def test_the_st_carries_offer_why_and_use_after_the_tick(rf):
    assert "rfp" not in rf.reserve_st_body() or rf.reserve_st_body()["rfp"]["offer"] is False
    rp.settle(rf)
    assert rf.reserve_st_body()["rfp"] == {"offer": True, "why": "", "use": ""}
    settings.set_value("app.reserve.rf_via_mine", False)
    rp.tick(rf)
    assert rf.reserve_st_body()["rfp"] == {"offer": False, "why": "off", "use": ""}


def test_without_a_pair_the_st_part_is_empty(rf):
    rf.db.reserve_peer_delete()
    assert rf.reserve_rfp_st_part() == {}
    assert rf.reserve_rfpeer_tick() == [] and rfp.read() == {}


def test_a_change_of_offer_or_only_of_why_changes_the_st_fingerprint(rf):
    rp.settle(rf)
    on = rf.reserve_st_body()
    rf.rsv_update("rsv_rfpeer", offer={"on": False, "why": "off", "since": "x"})
    off = rf.reserve_st_body()
    rf.rsv_update("rsv_rfpeer", offer={"on": False, "why": "no_gw", "since": "x"})
    no_gw = rf.reserve_st_body()
    assert rf._st_packed(on) != rf._st_packed(off), "смена согласия не меняет отпечаток — st не уйдёт"
    assert rf._st_packed(off) != rf._st_packed(no_gw), "смена только причины не меняет отпечаток"


def test_the_st_goes_again_right_after_the_offer_changes(rf):
    rp.settle(rf)
    assert rf.reserve_st_if_changed() is not None
    assert rf.reserve_st_if_changed() is None
    settings.set_value("app.reserve.rf_via_mine", False)
    rp.tick(rf)
    body = rf.reserve_st_if_changed()
    assert body is not None and body["rfp"]["offer"] is False, "отзыв согласия не ушёл второй стороне сразу"


def _st_from_peer(s, **over) -> dict:
    """`st` «Beta» — тело той же формы, что собирает этот сервер, без поля rfp и с заменами."""
    body = {k: v for k, v in s.reserve_st_body().items() if k != "rfp"}
    body.update(name="Beta", **over)
    return body


def test_the_peer_without_the_field_gives_no_offer_and_long_values_are_cut(rf):
    rf.reserve_st_in(_st_from_peer(rf))
    assert rf.reserve_peer_state()["rfp"] == {"offer": False, "why": "", "use": ""}, \
        "сторона без поля rfp — согласие"
    rf.reserve_st_in(_st_from_peer(rf, rfp={"offer": True, "why": "x" * 100, "use": "9" * 100}))
    got = rf.reserve_peer_state()["rfp"]
    assert got["offer"] is True and len(got["why"]) == 16 and len(got["use"]) == 32, got
    rf.reserve_st_in(_st_from_peer(rf, rfp="да"))
    assert rf.reserve_peer_state()["rfp"]["offer"] is False, "не словарь в поле — согласие"


# ── такт согласия: файл, правило 151, файервол ──────────────────────────────

def test_the_offer_opens_the_file_the_rule_and_the_table_once(rf):
    rp.settle(rf)
    assert rfp.read()["offer"] is True and rfp.read()["slot"] is False
    assert rf.k.rules == [rp.RULE151], rf.k.rules
    assert rf.fw[-1] == {"rfp_offer4": rp.PEER4}, f"таблица перерисована без правил транзита: {rf.fw}"
    n_fw, n_add = len(rf.fw), len(rf.k.calls)
    rp.ticks(rf, 5)
    assert len(rf.fw) == n_fw, "без смены согласия — перерисовка таблицы каждый такт"
    assert len(rf.k.calls) == n_add, "без смены согласия — ip rule каждый такт"


@pytest.mark.parametrize("revoke", ["switch_off", "own_slots_dead", "routing_off"])
def test_a_withdrawn_offer_closes_the_rule_the_file_and_the_table_in_the_same_tick(rf, fake_routing, revoke):
    rp.settle(rf)
    if revoke == "switch_off":
        settings.set_value("app.reserve.rf_via_mine", False)
        rp.tick(rf)
    elif revoke == "own_slots_dead":
        rp.own_down(rf)
        rp.ticks(rf, rf._rt_fail_need())
    else:
        fake_routing.enabled = False
        rp.tick(rf)
    assert rf.k.rules == [], f"правило 151 осталось после отзыва ({revoke})"
    assert rfp.read() == {}, "файл согласия остался — CLI нарисует транзит"
    assert rf.fw[-1] == {}, "таблица после отзыва — с правилами транзита"
    assert rf.rsv_get("rsv_rfpeer")["offer"]["on"] is False


def test_the_offer_survives_a_failing_ip_and_the_table_still_follows(rf):
    rf.k.ip_broken = True
    rp.settle(rf)
    assert rf.rsv_get("rsv_rfpeer")["offer"]["on"] is True and rf.fw[-1] == {"rfp_offer4": rp.PEER4}


def test_after_a_host_reboot_the_rule_comes_back_with_the_offer(rf):
    """Перезагрузка транзитного: правила в ядре нет, согласие на диске то же —
    первый такт после старта ставит правило заново (иначе путь «Beta» закрыт
    до ближайшей смены согласия, а её зонд проходит до хоста и дальше — нет)."""
    rp.settle(rf)
    rf.k.rules.clear()                                        # ядро после загрузки
    again = Services(rf.db)
    again.k, again.probe, again.switched, again.fw, again.clock = rf.k, rf.probe, rf.switched, rf.fw, rf.clock
    again._probe_slot = lambda g, active=False: rf.probe[g.id]
    again._gw_firewall_refresh = rf._gw_firewall_refresh
    rp.tick(again)
    assert rf.k.rules == [rp.RULE151], "после перезагрузки правило 151 не вернулось — транзит закрыт"


# ── эпизод второй стороны ───────────────────────────────────────────────────

def _episode_start(s, *, up=0, dn=0):
    rp.settle(s)
    s.k.up, s.k.dn = up, dn
    since = timeutil.now_iso()
    rp.peer_rfp(s, offer=False, why="peer", use=since)
    return since, rp.tick(s)


def test_the_peer_using_my_gateways_is_told_once_without_sound(rf):
    since, notes = _episode_start(rf)
    assert [(n.text, n.silent, n.critical, n.tg_id) for n in notes] == \
        [(rn.rf_peer_use("Beta", since), True, False, config.ADMIN_ID)], notes
    assert notes[0].text == ("🇷🇺 РФ-доступ пользователей «Beta» идёт через шлюзы этого сервера с 14:06 (UTC+5): "
                             "шлюзы «Beta» не отвечают\nИсходящий адрес у них — адрес твоего шлюза. "
                             "Выключить — «🛟 Резерв» → «Мои шлюзы для резерва»")
    assert not rp.ticks(rf, 5), "Т-РТ10 повторилось в том же эпизоде"


def test_a_restart_inside_the_episode_does_not_repeat_the_notice(rf):
    _episode_start(rf)
    again = Services(rf.db)
    assert again.reserve_rfpeer_tick() == [], "после перезапуска Т-РТ10 пришло снова"


def test_the_episode_end_tells_the_volume_through_my_gateways(rf):
    _episode_start(rf, up=5 * MB, dn=10 * MB)
    rf.k.up, rf.k.dn = 5 * MB + 120 * MB, 10 * MB + 1434 * MB
    rp.peer_rfp(rf, offer=False, why="peer", use="")
    notes = rp.tick(rf)
    assert [(n.text, n.silent, n.critical) for n in notes] == \
        [(rn.rf_peer_done("Beta", 120 * MB, 1434 * MB), True, False)], notes
    assert notes[0].text == (f"🇷🇺 РФ-доступ «Beta» снова идёт через его шлюзы — через твои прошло "
                             f"(↑ {human_bytes(120 * MB)} | ↓ {human_bytes(1434 * MB)})")
    assert "peer_use" not in rf.rsv_get("rsv_rfpeer") or not rf.rsv_get("rsv_rfpeer")["peer_use"]
    assert not rp.ticks(rf, 3), "Т-РТ11 повторилось"


def test_no_episode_no_end_notice(rf):
    rp.settle(rf)
    rp.peer_rfp(rf, offer=True, use="")
    assert not rp.ticks(rf, 3)


def test_counters_restarted_from_zero_never_give_a_negative_volume(rf):
    _episode_start(rf, up=900 * MB, dn=900 * MB)
    rf.k.up, rf.k.dn = 3 * MB, 7 * MB                         # перезагрузка: таблица учёта с нуля
    rp.peer_rfp(rf, offer=False, use="")
    text = rp.tick(rf)[0].text
    assert "-" not in text.split("прошло", 1)[1], text


def test_counters_restarted_from_zero_keep_what_passed_after_the_reboot(rf):
    """Перезагрузка посреди эпизода: всё, что прошло после неё, — тоже через мои
    шлюзы; итог не меньше показаний новой таблицы."""
    _episode_start(rf, up=900 * MB, dn=900 * MB)
    rf.k.up, rf.k.dn = 3 * MB, 7 * MB
    rp.peer_rfp(rf, offer=False, use="")
    text = rp.tick(rf)[0].text
    assert text.endswith(f"(↑ {human_bytes(3 * MB)} | ↓ {human_bytes(7 * MB)})"), \
        f"прошедшее после перезагрузки потеряно (база эпизода старше нового счётчика): {text}"


def test_the_card_data_carries_the_episode_with_its_volume(rf):
    since, _ = _episode_start(rf, up=MB, dn=MB)
    rf.k.up, rf.k.dn = 3 * MB, 5 * MB
    view = rf.reserve_rfpeer_view()
    assert view["peer_use"] == {"since": since, "up": 2 * MB, "dn": 4 * MB} and view["offer"] is True


# ── удаление пары ───────────────────────────────────────────────────────────

def test_unpairing_in_an_episode_removes_the_rule_the_file_and_the_state(rf):
    _episode_start(rf)
    assert rf.k.rules == [rp.RULE151]
    rf.reserve_unpair()
    assert rfp.read() == {} and rf.rsv_get("rsv_rfpeer") == {}
    assert rf.fw[-1] == {}, "таблица после удаления пары — с правилами транзита"
    assert rf.k.rules == [], "правило 151 пережило удаление пары: конфиг линка снят раньше, адрес неизвестен"
