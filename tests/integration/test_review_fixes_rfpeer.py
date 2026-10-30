"""Правки по ревью РФ-доступа через второй сервер (domain/services/reserve/rfpeer/slot.py,
offer.py; infra/routing/rfpeer.py):

- Т-РТ8 — на каждый эпизод: `lost` снимается, когда пользование слотом 29 началось снова;
- уход с пропавшего слота 29 — на первый ЖИВОЙ свой (все мертвы — предпочтительный);
- политика слота 29 (правило по метке, таблица) — сразу при появлении и после
  пересоздания или переподнятия линка пары (штамп ifindex + mtime конфига), кэш вердикта
  слота — прочь; при активном 29 после пересоздания — маршрут в линк пары заново;
- широкие AllowedIPs — по смене, после переподнятия и раз в 10 тактов, не каждый такт;
  кэш живости линка пары не сбрасывается внутри такта;
- флаг объявления своего слота при уходе на живой — сброшен, при мёртвых — не тронут;
- при выключенном РФ-доступе пропажа слота молчит; причины no_gw/rt_off/peer — словами;
- сужение AllowedIPs — до перерисовки файервола и страховочно раз в 10 тактов без слота;
- объём эпизода второй стороны — дельтами по поколению таблицы учёта;
- тумблер «Мои шлюзы для резерва» из обработчика пересчитывает только согласие.

Сервер — «Alpha» с двумя своими слотами и парой с «Beta» (tests/integration/_rfpeer_common.py):
ядро — модель `Kernel`, зонды — `probe[слот]`, часы — модельные, такт — как в планировщике.

Цена ошибки: второй эпизод без Т-РТ8 — админ не знает, что российские сервисы снова
открываются с зарубежного адреса; уход на мёртвый предпочтительный при живом втором —
пользователи без РФ-доступа до следующего переключения; без политики слота сразу зонд
слота с меткой уходит основной таблицей через WAN и «проходит» — переключение на мёртвый
путь; маршрут не в линке после пересоздания — РФ-трафик в никуда; `awg show` каждые
30 с — лишняя работа и шум в журнале; «снова в строю» после ухода со слота — второе
письмо об одном событии; Т-РТ8 при выключенном РФ-доступе — тревога о том, что админ
сделал сам; файервол без входа при широких AllowedIPs — окно, где ответы из РФ
пускаются мимо таблицы; объём после перезагрузки «Beta» — только хвост эпизода.
"""
from __future__ import annotations

import os
import subprocess
import types

import pytest

from awgbot.core import config, settings
from awgbot.domain import gwsnapshot
from awgbot.domain.notices import reserve as rn
from awgbot.domain.services.reserve.rfpeer.offer import why_words
from awgbot.infra import rfacct
from awgbot.infra.nftguard import collect
from awgbot.infra.routing import base
from awgbot.infra.routing import rfpeer as rfp
from awgbot.util import timeutil

from tests.integration import _rfpeer_common as rp

pytestmark = pytest.mark.integration
rf = rp.rf
V = rp.VSLOT
GB = 1 << 30
NARROW = [f"{rp.PEER4}/32"]


def _ready(s) -> list:
    rp.peer_rfp(s, offer=True)
    return rp.settle(s)


def _to_peer(s) -> list:
    _ready(s)
    rp.own_down(s)
    return rp.ticks(s, s._rt_fail_need())


def _gone(s, cause: str = "off") -> list:
    rp.peer_rfp(s, offer=False, why=cause)
    return rp.tick(s)


def _back_on_peer(s) -> list:
    """Слот 29 вернулся при мёртвых своих — снова на нём (после интервала переключений)."""
    rp.peer_rfp(s, offer=True)
    rp.fresh_handshake(s)
    s.clock.step(600)
    notes: list = []
    for _ in range(s._rt_fail_need() + 3):
        notes += rp.tick(s)
        if s.active_gateway().id == V:
            break
    assert s.active_gateway().id == V, "подготовка: слот 29 не стал активным снова"
    return notes


# ── Т-РТ8 на каждый эпизод (rfp-1) ──────────────────────────────────────────

def test_a_second_episode_with_the_same_reason_is_told_again(rf):
    _to_peer(rf)
    first = rp.crit(_gone(rf, "off"))
    assert len(first) == 1, f"подготовка: Т-РТ8 первого эпизода: {first}"
    _back_on_peer(rf)
    assert rf.rsv_get("rsv_rfpeer").get("use") and not rf.rsv_get("rsv_rfpeer").get("lost"), \
        "начало пользования не сняло отметку прежнего Т-РТ8"
    again = rp.crit(_gone(rf, "off"))
    assert again == [rn.rf_transit_lost("Beta", why_words("Beta")["off"])], \
        f"второй эпизод с той же причиной — без Т-РТ8: {again}"


def test_within_one_episode_the_lost_notice_is_still_once(rf):
    _to_peer(rf)
    _gone(rf, "off")
    assert not rp.crit(rp.ticks(rf, 5)), "Т-РТ8 повторилось в том же эпизоде"


# ── уход на первый живой свой (rfp-2) ───────────────────────────────────────

def test_the_gone_peer_slot_hands_traffic_to_the_first_alive_own_slot(rf):
    _to_peer(rf)
    rf.probe[2] = "ok"                                        # ожил второй, первый (⭐) мёртв
    rp.ticks(rf, rf._RT_UP_STREAK)
    assert rf.active_gateway().id == V, "подготовка: интервал не держит слот 29"
    n = len(rf.switched)
    notes = _gone(rf, "off")
    assert rf.active_gateway().id == 2 and rf.switched[n:][:1] == ["awglink2"], \
        f"трафик ушёл на мёртвый предпочтительный при живом втором: {rf.switched[n:]}"
    assert rp.crit(notes) == [], f"Т-РТ8 при живом своём: {rp.crit(notes)}"
    assert rp.texts(notes).count(rn.rf_transit_back("Beta", "Pi5")) == 1, \
        f"Т-РТ7 не называет живой слот 2: {rp.texts(notes)}"


def test_with_all_own_dead_the_traffic_goes_to_the_preferred(rf):
    _to_peer(rf)
    n = len(rf.switched)
    _gone(rf, "off")
    assert rf.active_gateway().id == 1 and rf.switched[n:][:1] == ["awglink"], rf.switched[n:]


# ── политика слота и штамп линка (rfp-3) ────────────────────────────────────

@pytest.fixture()
def policy(rf, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(rf, "_ensure_gateway_policy", lambda: calls.append(1))
    return calls


def _touch_conf() -> None:
    conf = f"{config.AWG_DIR}/awgpair.conf"
    st = os.stat(conf)
    os.utime(conf, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_the_peer_slot_gets_its_policy_in_the_tick_it_appears_and_the_verdict_cache_is_dropped(rf, policy):
    rp.settle(rf)                                             # согласия «Beta» нет — слота нет
    assert not rf.rsv_get("rsv_rfpeer").get("slot"), "подготовка: слот 29 уже есть"
    rf.__dict__["_rt_spare_last"] = {V: "down"}
    rf.__dict__["_rt_spare_next"] = {V: 1e12}
    rp.peer_rfp(rf, offer=True)
    rp.fresh_handshake(rf)
    policy.clear()
    rf.reserve_rf_slot_sync()
    assert policy == [1], "появился слот 29 — политика слота не поставлена в том же такте"
    assert rf.__dict__["_rt_spare_last"] == {} and rf.__dict__["_rt_spare_next"] == {}, \
        "вердикт слота 29 до политики остался в кэше"


def test_a_steady_peer_slot_does_not_redo_its_policy(rf, policy):
    _ready(rf)
    policy.clear()
    for _ in range(5):
        rf.reserve_rf_slot_sync()
    assert policy == [], "политика слота 29 заново без смены линка"


def test_a_pair_link_raised_from_a_new_config_redoes_the_policy(rf, policy):
    _ready(rf)
    rf.reserve_rf_slot_sync()
    policy.clear()
    _touch_conf()
    rf.reserve_rf_slot_sync()
    assert policy == [1], "конфиг линка пары переписан — политика слота не заново"


def test_a_recreated_pair_interface_redoes_the_policy_and_the_route_when_active(rf, policy, monkeypatch):
    _to_peer(rf)
    idx = {"v": 7}
    monkeypatch.setattr(rfp, "ifindex", lambda: idx["v"])
    rf.reserve_rf_slot_sync()
    policy.clear()
    n = len(rf.switched)
    idx["v"] = 8                                              # --adopt / рестарт юнита пары
    rf.reserve_rf_slot_sync()
    assert policy == [1], "линк пары пересоздан — политика слота не заново"
    assert rf.switched[n:] == ["awgpair"], f"при активном 29 маршрут не возвращён в линк пары: {rf.switched[n:]}"


def test_a_recreated_pair_interface_with_an_own_active_slot_leaves_the_route(rf, policy, monkeypatch):
    _ready(rf)
    idx = {"v": 7}
    monkeypatch.setattr(rfp, "ifindex", lambda: idx["v"])
    rf.reserve_rf_slot_sync()
    n = len(rf.switched)
    idx["v"] = 8
    rf.reserve_rf_slot_sync()
    assert policy and rf.switched[n:] == [], f"активный свой — маршрут уведён в линк пары: {rf.switched[n:]}"


# ── awg show раз в 10 тактов (rfp-5) ────────────────────────────────────────

@pytest.fixture()
def counted(rf, monkeypatch):
    """Счёт вызовов `awg show awgpair …` поверх модели ядра."""
    seen: list[list[str]] = []

    def run(args, **kw):
        seen.append(list(args))
        return rf.k.run(args, **kw)
    monkeypatch.setattr(base, "subprocess", types.SimpleNamespace(
        run=run, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    return seen


def _shows(seen, what: str) -> int:
    return sum(1 for a in seen if a[:4] == ["awg", "show", "awgpair", what])


def test_a_steady_peer_slot_reads_allowed_ips_only_every_tenth_tick(rf, counted):
    _ready(rf)
    counted.clear()
    rp.ticks(rf, 20)
    n = _shows(counted, "allowed-ips")
    assert 1 <= n <= 2, f"AllowedIPs линка пары читаются {n} раз за 20 тактов без смен"


def test_the_handshake_is_read_once_per_tick(rf, counted):
    _ready(rf)
    rp.fresh_handshake(rf)
    counted.clear()
    rp.tick(rf)
    assert _shows(counted, "latest-handshakes") == 1, \
        f"рукопожатие линка пары читается {_shows(counted, 'latest-handshakes')} раз за такт"


# ── флаг объявления своего (rfp-6) ──────────────────────────────────────────

def test_leaving_the_peer_slot_for_an_alive_own_is_told_once(rf):
    _to_peer(rf)
    rf.probe[1] = "ok"
    rp.ticks(rf, rf._RT_UP_STREAK)
    notes = _gone(rf, "off")
    assert rf.active_gateway().id == 1
    assert rf.db.get_state(rf._rt_keys(1)[1]) == "0", "флаг объявления живого своего не сброшен"
    texts = rp.texts(notes + rp.ticks(rf, 3))
    assert texts.count(rn.rf_transit_back("Beta", "Pi4")) == 1, texts
    again = [t for t in texts if "снова в строю" in t]
    assert again == [], f"уход со слота 29 на живой свой — Т-РТ7 и ещё «снова в строю» о том же: {again}"


def test_leaving_the_peer_slot_with_own_dead_keeps_the_announcement(rf):
    _to_peer(rf)
    flag = rf.db.get_state(rf._rt_keys(1)[1])
    _gone(rf, "off")
    assert rf.db.get_state(rf._rt_keys(1)[1]) == flag, "флаг объявления мёртвого своего тронут"
    later = rp.crit(rp.ticks(rf, 5))
    assert not [t for t in later if t.startswith("🔴 Шлюз")], f"повторное «🔴 Шлюз … не отвечает»: {later}"


# ── РФ-доступ выключен и причины словами (rfp-7) ────────────────────────────

def test_a_gone_peer_slot_says_nothing_when_rf_access_is_switched_off(rf, fake_routing):
    _to_peer(rf)
    fake_routing.enabled = False                              # РФ-доступ выключил сам админ
    notes = rp.ticks(rf, 2)
    words = [t for t in rp.texts(notes) if "РФ-доступ" in t]
    assert words == [], f"при выключенном РФ-доступе — письма о слоте второго сервера: {words}"


@pytest.mark.parametrize("code", ["no_gw", "rt_off", "peer"])
def test_the_peer_refusal_codes_are_told_in_words(rf, code):
    _to_peer(rf)
    red = rp.crit(_gone(rf, code))
    assert red == [rn.rf_transit_lost("Beta", why_words("Beta")[code])], \
        f"причина {code} не своими словами: {red}"


# ── сужение до файервола (rfp-9) ────────────────────────────────────────────

def test_allowed_ips_are_narrowed_before_the_firewall_is_redrawn(rf, monkeypatch):
    _ready(rf)
    seen: list[tuple] = []
    monkeypatch.setattr(rf, "_gw_firewall_refresh",
                        lambda strict=False: seen.append((list(rf.k.allowed), collect.rfpeer())))
    _gone(rf, "off")
    without = [allowed for allowed, fw in seen if "rfp_self4" not in fw]
    assert without, f"подготовка: файервол без правил просящего не перерисован: {seen}"
    assert without[0] == NARROW, f"файервол без входа просящего при широких AllowedIPs: {without[0]}"


def test_without_the_slot_the_narrowing_is_retried_every_tenth_tick(rf):
    _ready(rf)
    _gone(rf, "off")
    rf.k.allowed = [f"{rp.PEER4}/32", "0.0.0.0/0"]            # сужение при пропаже не прошло
    rp.ticks(rf, 10)
    assert rf.k.allowed == NARROW, f"широкие AllowedIPs без слота остались: {rf.k.allowed}"


def test_already_narrow_allowed_ips_are_not_set_again(rf):
    _ready(rf)
    _gone(rf, "off")
    n = len(rf.k.awg_sets())
    rp.ticks(rf, 12)
    assert len(rf.k.awg_sets()) == n, "страховочное сужение — awg set при уже узких AllowedIPs"


# ── объём эпизода второй стороны (rfp-4) ────────────────────────────────────

@pytest.fixture()
def boot(monkeypatch):
    b = {"id": "boot-1"}
    monkeypatch.setattr(gwsnapshot, "boot_id", lambda: b["id"])
    return b


def _episode_start(s, *, up=0, dn=0):
    rp.settle(s)
    s.k.up, s.k.dn = up, dn
    since = timeutil.now_iso()
    rp.peer_rfp(s, offer=False, why="peer", use=since)
    rp.tick(s)
    return since


def test_the_counters_carry_the_generation_of_the_table(rf, boot, monkeypatch):
    assert rf.reserve_rf_peer_counters()["gen"] == "boot-1:7"
    monkeypatch.setattr(rfacct, "read", lambda: None)
    assert rf.reserve_rf_peer_counters() == {"up": 0, "dn": 0, "gen": ""}


def test_a_peer_reboot_inside_the_episode_keeps_the_volume_before_it(rf, boot):
    """База 1 ГБ, до перезагрузки «Beta» прошло 2 ГБ (накоплено 10-м тактом), после неё
    счётчик с нуля до 0,5 ГБ: за эпизод — 2,5 ГБ, а не 0,5."""
    _episode_start(rf, up=GB, dn=GB)
    rf.k.up = rf.k.dn = 3 * GB
    rp.ticks(rf, 10)
    boot["id"] = "boot-2"
    rf.k.up = rf.k.dn = GB // 2
    ep = rf.reserve_rf_peer_episode()
    assert (ep["up"], ep["dn"]) == (2 * GB + GB // 2, 2 * GB + GB // 2), f"перезагрузка обнулила эпизод: {ep}"
    rp.peer_rfp(rf, offer=False, why="peer", use="")
    done = rp.texts(rp.tick(rf))
    assert done == [rn.rf_peer_done("Beta", 2 * GB + GB // 2, 2 * GB + GB // 2)], done


def test_without_a_generation_change_the_volume_is_the_plain_difference(rf, boot):
    _episode_start(rf, up=GB, dn=GB)
    rf.k.up = rf.k.dn = 3 * GB
    rp.ticks(rf, 10)
    rf.k.up = rf.k.dn = 4 * GB
    ep = rf.reserve_rf_peer_episode()
    assert (ep["up"], ep["dn"]) == (3 * GB, 3 * GB), ep


# ── тумблер из обработчика (rfp-8) ──────────────────────────────────────────

def test_offer_now_without_a_pair_does_nothing(rf):
    rf.db.reserve_peer_delete()
    before = dict(rf.rsv_get("rsv_rfpeer"))
    settings.set_value("app.reserve.rf_via_mine", False)
    rf.reserve_rfpeer_offer_now()
    assert dict(rf.rsv_get("rsv_rfpeer")) == before, "без пары согласие пересчитано"


def test_offer_now_withdraws_the_offer_and_leaves_the_slot_and_the_episode(rf):
    _ready(rf)
    assert rf.k.rules == [rp.RULE151], "подготовка: согласия с правилом 151 нет"
    cur = dict(rf.rsv_get("rsv_rfpeer"))
    settings.set_value("app.reserve.rf_via_mine", False)
    rf.reserve_rfpeer_offer_now()
    after = rf.rsv_get("rsv_rfpeer")
    assert (after.get("offer") or {}).get("on") is False and after["offer"]["why"] == "off", after.get("offer")
    assert after.get("slot") == cur.get("slot") and after.get("use") == cur.get("use"), \
        "пересчёт согласия тронул слот или эпизод"
    assert rf.k.rules == [], "правило 151 пережило снятие согласия"
