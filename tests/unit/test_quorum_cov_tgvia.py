"""Выход агента шлюза в Telegram через линк к резерву и перерезолв аплинка по
записи (domain/gateway/reserve/tgvia.py; поводы — store.reserve_state_in и
reserve_apply): таблица истинности «аплинк × awgrsv × флаг tg × линк»,
гистерезис двух ответов пробы, повтор правила метки раз в 10 минут и после
перезапуска агента, Т-К24 один раз на эпизод, серия перерезолва на номер
переезда с повтором и пределом.

Хост — `_Net`: рукопожатия интерфейсов (`gwguard.handshake_state`), есть ли
линк к резерву, ответ DNS-пробы через аплинк (журнал вызовов), строки юнита
обвязки. Случайность интервалов — середина диапазона; часы — аргумент `now`.

Цена ошибки: обход включается при живом аплинке — Bot API агента ходит через
второй сервер без нужды; не включается при мёртвом — бот шлюза молчит админу
ровно тогда, когда нужен; выключается по одному ответу — дребезг и пересоздание
сессии Bot API на каждом всплеске; правило метки не ставится заново после
перезапуска агента — cgroup юнита новая, метки нет, обход только на бумаге;
перерезолв один раз и сразу — запись у провайдера ещё старая, аплинк смотрит в
мёртвый адрес до запасного пути по рукопожатию; перерезолв без предела —
запрос к авторитетному серверу каждые 20 с бессрочно.
"""
from __future__ import annotations

import json
import types

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.reserve import store as storemod
from awgbot.domain.gateway.reserve import tgvia as tgmod
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from awgbot.infra.gwguard import rsv as rsvlink
from tests.unit.test_gw_reserve_agent import PRIMARY_IP, RECORD, RESERVE_IP, _checked, gw, host

pytestmark = pytest.mark.unit
__all__ = ["gw", "host"]

UP = "awgup"
FRESH, STALE = ("ok", 10), ("ok", 300)
T0 = 10_000.0


class _Net:
    def __init__(self, monkeypatch):
        self.hs: dict[str, tuple] = {UP: STALE, "awgrsv": FRESH}
        self.link = True
        self.answer = True
        self.probes: list[tuple[str, str]] = []
        self.subnet = "10.9.1.0/24"
        monkeypatch.setattr(config, "GW_UPLINK_IF", UP)
        monkeypatch.setattr(gwguard, "handshake_state", lambda iface: self.hs.get(iface, ("absent", None)))
        monkeypatch.setattr(rsvlink, "present", lambda: self.link)
        monkeypatch.setattr(gwguard, "unit_env", lambda k: {"CLIENT_SUBNET": self.subnet}.get(k, ""))
        monkeypatch.setattr(tgmod, "_dns_probe", self._probe)
        monkeypatch.setattr(tgmod, "random", types.SimpleNamespace(uniform=lambda a, b: 1.0))

    def _probe(self, resolver, iface, timeout=None):
        self.probes.append((resolver, iface))
        return self.answer


@pytest.fixture()
def net(monkeypatch):
    return _Net(monkeypatch)


def _section(**over) -> dict:
    sec = {"conf": "[Interface]\nPrivateKey = DUMMY\n", "tg": True, "name": "Alpha", "peer_name": "Beta", "slot": 1,
           "state": "idle", "move": 0, "self": PRIMARY_IP, "record": RECORD, "peer_host": RESERVE_IP}
    sec.update(over)
    return sec


def _agent(tmp_path) -> GatewayServices:
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE = tmp_path / "reserve.json"
    return svc


@pytest.fixture()
def agent(tmp_path, net):
    svc = _agent(tmp_path)
    storemod._write(_section(), svc._RSV_STORE)
    return svc


def _kinds(actions) -> list[str]:
    return [a[0] if a[0] not in ("tg", "tg_assert") else f"{a[0]}:{a[1]}" for a in actions]


def _notes(actions) -> list:
    return [n for a in actions if a[0] == "notify" for n in a[1]]


def _on(agent, now: float = T0) -> list[tuple]:
    acts = agent.reserve_tgvia_tick(now)
    assert ("tg", "on") in acts, f"подготовка: обход не включился: {acts}"
    return acts


def _run(agent, start: float, seconds: float, step: float = 5.0) -> list[tuple[float, tuple]]:
    out, t = [], start
    while t <= start + seconds:
        out += [(t, a) for a in agent.reserve_tgvia_tick(t)]
        t += step
    return out


# ── когда включать ──────────────────────────────────────────────────────────

UPLINK = {"stale": STALE, "fresh": FRESH, "never": ("never", None), "unknown": ("unknown", None), "absent": ("absent", None)}
RSV = {"fresh": FRESH, "stale": STALE, "never": ("never", None), "unknown": ("unknown", None)}


@pytest.mark.parametrize("up", list(UPLINK))
@pytest.mark.parametrize("rsv", list(RSV))
@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize("link", [True, False])
def test_the_bypass_goes_on_only_with_a_dead_uplink_a_live_reserve_link_and_the_flag(tmp_path, net, up, rsv, flag, link):
    """Включить: флаг `tg` в разделе, линк к резерву есть, рукопожатие аплинка старше
    порога или его не было, рукопожатие awgrsv свежее. `unknown` аплинка — не
    «мёртв» (разовый сбой команды), его нет вовсе — решать не по чему."""
    svc = _agent(tmp_path)
    storemod._write(_section(tg=flag), svc._RSV_STORE)
    net.hs = {UP: UPLINK[up], "awgrsv": RSV[rsv]}
    net.link = link
    acts = svc.reserve_tgvia_tick(T0)
    want = flag and link and up in ("stale", "never") and rsv == "fresh"
    assert (("tg", "on") in acts) == want, f"аплинк {up}, awgrsv {rsv}, tg {flag}, линк {link}: {acts}"
    assert bool(svc.reserve_section().get("tg_via")) == want


def test_without_an_uplink_interface_there_is_no_bypass(agent, monkeypatch):
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "")
    assert agent.reserve_tgvia_tick(T0) == []


def test_without_the_reserve_section_nothing_is_done_and_the_state_is_forgotten(agent):
    _on(agent)
    agent._RSV_STORE.unlink()
    assert agent.reserve_tgvia_tick(T0 + 30) == []
    assert "_tgvia" not in agent.__dict__, "состояние обхода пережило снятие раздела"


# ── включение: отметки и уведомление ────────────────────────────────────────

def test_switching_on_marks_the_section_tells_the_admin_quietly_and_pushes_the_snapshot(agent):
    acts = _on(agent)
    assert _kinds(acts) == ["tg:on", "notify", "push"], acts
    sec = agent.reserve_section()
    assert sec["tg_via"] is True and sec["tg_since"], sec
    (note,) = _notes(acts)
    assert note.silent and not note.force_sound, "Т-К24 со звуком"
    assert note.text.startswith("📨 Аплинк до «Alpha» молчит с ") and "через «Beta»" in note.text, note.text
    assert not note.text.endswith("."), "уведомление с точкой в конце"
    assert agent.reserve_tgvia_view() == {"via": True, "since": sec["tg_since"]}


def test_the_bypass_notice_goes_once_per_episode(agent):
    _on(agent)
    later = _run(agent, T0 + 5, 1800)
    assert not [a for _, a in later if a[0] == "notify"], "Т-К24 повторилось в том же эпизоде обхода"
    assert not [a for _, a in later if a == ("tg", "on")], "обход включается повторно, уже будучи включённым"


def test_a_new_episode_tells_the_admin_again(agent, net):
    _on(agent)
    net.hs[UP] = FRESH
    off = _run(agent, T0 + 15, 200)
    assert ("tg", "off") in [a for _, a in off], "подготовка: обход не выключился"
    net.hs[UP] = STALE
    again = _run(agent, T0 + 220, 30)
    assert len([a for _, a in again if a[0] == "notify"]) == 1, "новый эпизод обхода без Т-К24"


def test_the_undelivered_bypass_notice_is_retried(agent):
    """Т-К24 уходит в момент, когда Telegram агента только что переехал на awgrsv:
    не дошло — повтор раз в минуту, как у Т-РА3/Т-РА4 (очередь `pending_notes` в
    reserve.json). Без записи в очередь уведомление теряется бесследно."""
    _on(agent)
    again = agent.reserve_notes_retry(T0 + 120)
    texts = [n.text for n in _notes(again)]
    assert any(t.startswith("📨 Аплинк до «Alpha»") for t in texts), \
        f"недоставленное Т-К24 не повторяется: очередь {agent.reserve_section().get('pending_notes')}"


def test_without_a_peer_name_the_notice_says_the_reserve(tmp_path, net):
    svc = _agent(tmp_path)
    storemod._write(_section(peer_name=""), svc._RSV_STORE)
    (note,) = _notes(svc.reserve_tgvia_tick(T0))
    assert note.text.endswith("через резерв"), note.text


# ── выключение: гистерезис ──────────────────────────────────────────────────

def test_two_answers_in_a_row_with_a_fresh_handshake_switch_the_bypass_off(agent, net):
    _on(agent)
    net.hs[UP] = FRESH
    ev = _run(agent, T0 + 15, 200)
    offs = [t for t, a in ev if a == ("tg", "off")]
    assert offs, "аплинк снова жив — обход не выключился"
    first_probe = T0 + 60                                   # проба во время обхода — через 60 с от включения
    assert offs == [first_probe + 60], f"обход выключен не вторым ответом подряд: {offs}"
    assert not [a for _, a in ev if a[0] == "notify"], "отбой обхода с уведомлением — по тексту его нет"
    sec = agent.reserve_section()
    assert sec["tg_via"] is False and sec["tg_since"] == ""
    assert ("push",) in [a for t, a in ev if t == offs[0]], "выключение без снимка серверу"
    assert agent.reserve_tgvia_view() == {}


def test_a_missed_answer_between_two_restarts_the_count(agent, net, monkeypatch):
    """Ответ, пропуск, ответ — это не «два подряд»: выключает только четвёртая
    проба (второй ответ после пропуска)."""
    _on(agent)
    net.hs[UP] = FRESH
    answers = iter([True, False, True, True])
    monkeypatch.setattr(tgmod, "_dns_probe", lambda resolver, iface, timeout=None: next(answers))
    ev = _run(agent, T0 + 15, 300)
    offs = [t for t, a in ev if a == ("tg", "off")]
    assert offs == [T0 + 60 * 4], f"пропуск ответа не сбросил счёт подряд: выключено в {offs}"


def test_answers_with_a_stale_handshake_keep_the_bypass(agent, net):
    """Ответ пробы при старом рукопожатии — не повод: обход держится, пробу при этом
    не гоняют впустую."""
    _on(agent)
    ev = _run(agent, T0 + 15, 600)
    assert ("tg", "off") not in [a for _, a in ev]
    assert net.probes == [], "проба аплинка при старом рукопожатии"


def test_a_fresh_handshake_without_answers_keeps_the_bypass(agent, net):
    _on(agent)
    net.hs[UP] = FRESH
    net.answer = False
    ev = _run(agent, T0 + 15, 600)
    assert ("tg", "off") not in [a for _, a in ev], "рукопожатие без ответа пробы выключило обход"
    assert len(net.probes) >= 9, f"проба во время обхода не раз в минуту: {len(net.probes)}"


def test_the_probe_asks_the_users_resolver_through_the_uplink(agent, net):
    _on(agent)
    net.hs[UP] = FRESH
    _run(agent, T0 + 15, 60)
    assert net.probes and set(net.probes) == {("10.9.1.1", UP)}, net.probes


def test_without_the_users_subnet_there_is_no_probe_and_no_switch_off(agent, net):
    _on(agent)
    net.hs[UP] = FRESH
    net.subnet = ""
    ev = _run(agent, T0 + 15, 600)
    assert net.probes == [] and ("tg", "off") not in [a for _, a in ev]


@pytest.mark.parametrize("what", ["flag", "link"])
def test_the_flag_or_the_link_gone_switch_the_bypass_off_at_once(agent, net, what):
    _on(agent)
    if what == "flag":
        sec = agent.reserve_section()
        sec["tg"] = False
        storemod._write(sec, agent._RSV_STORE)
    else:
        net.link = False
    acts = agent.reserve_tgvia_tick(T0 + 1)                   # раньше окна проверки 15 с
    assert ("tg", "off") in acts, f"{what}: обход не выключен сразу: {acts}"
    assert agent.reserve_tgvia_tick(T0 + 2) == [], "выключение повторяется"


def test_the_uplink_interface_gone_switches_the_bypass_off(agent, net, monkeypatch):
    _on(agent)
    monkeypatch.setattr(config, "GW_UPLINK_IF", "")
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: (_ for _ in ()).throw(RuntimeError("нет")))
    acts = agent.reserve_tgvia_tick(T0 + 15)
    assert ("tg", "off") in acts, acts


# ── правило метки заново ────────────────────────────────────────────────────

def test_the_mark_rule_is_asserted_every_ten_minutes_while_on(agent):
    _on(agent)
    ev = _run(agent, T0 + 5, 1900)
    at = [t for t, a in ev if a == ("tg_assert", "on")]
    assert at[0] == T0 + 5, f"первое подтверждение правила метки не сразу: {at}"
    gaps = {b - a for a, b in zip(at, at[1:])}
    assert gaps == {600.0}, f"правило метки подтверждается не раз в 10 минут: {at}"


def test_an_agent_restart_with_the_bypass_on_asserts_the_mark_first(agent, tmp_path):
    """После перезапуска агента cgroup юнита новая: правило метки — заново на
    первом же такте, не через 10 минут."""
    _on(agent)
    fresh = _agent(tmp_path)
    acts = fresh.reserve_tgvia_tick(T0 + 1)
    assert acts and acts[0] == ("tg_assert", "on"), f"после перезапуска правило метки не подтверждено первым: {acts}"


def test_without_the_bypass_the_mark_is_not_asserted(agent, net):
    net.hs[UP] = FRESH
    ev = _run(agent, T0, 1300)
    assert not [a for _, a in ev if a[0] in ("tg", "tg_assert")], ev


# ── перерезолв по записи ────────────────────────────────────────────────────

PENDING = {"name": RECORD, "expect": RESERVE_IP, "why": "переезд", "move": 3}


def _pending(agent, **over) -> dict:
    p = {**PENDING, **over}
    sec = agent.reserve_section()
    sec["reresolve_pending"] = p
    storemod._write(sec, agent._RSV_STORE)
    return p


def _reresolves(ev) -> list[float]:
    return [t for t, a in ev if a[0] == "reresolve"]


def test_the_reresolve_is_repeated_every_twenty_seconds_until_the_record_gives_the_address(agent, net):
    net.hs[UP] = FRESH
    p = _pending(agent)
    ev = _run(agent, T0, 65)
    assert _reresolves(ev) == [T0, T0 + 20, T0 + 40, T0 + 60], _reresolves(ev)
    assert [a for _, a in ev if a[0] == "reresolve"][0] == ("reresolve", p)
    agent.reserve_reresolve_done(p, "fail")
    assert agent.reserve_section().get("reresolve_pending") == p, "неудачный перерезолв закрыл повод"
    agent.reserve_reresolve_done(p, "set")
    assert agent.reserve_section().get("reresolve_pending") is None
    assert _reresolves(_run(agent, T0 + 70, 100)) == [], "перерезолв после установленного адреса"


@pytest.mark.parametrize("result,closes", [("set", True), ("same", True), ("skip:ip", True), ("skip:no-uplink", True),
                                           ("skip:no-endpoint", True), ("skip:other-name", True),
                                           ("fail", False), ("", False), ("skip:dns", False)])
def test_which_results_close_the_reason(agent, result, closes):
    p = _pending(agent)
    agent.reserve_reresolve_done(p, result)
    assert (agent.reserve_section().get("reresolve_pending") is None) == closes, result


def test_the_reresolve_gives_up_after_ten_minutes(agent, net):
    net.hs[UP] = FRESH
    _pending(agent)
    ev = _run(agent, T0, 900)
    tries = _reresolves(ev)
    assert tries[-1] <= T0 + tgmod.RERESOLVE_MAX_S, f"перерезолв после предела: {tries[-3:]}"
    assert len(tries) == tgmod.RERESOLVE_MAX_S // 20 + 1, len(tries)
    assert agent.reserve_section().get("reresolve_pending") is None, "повод не снят после предела"


def test_a_new_move_starts_its_own_series_at_once(agent, net):
    net.hs[UP] = FRESH
    _pending(agent)
    _run(agent, T0, 10)
    p2 = _pending(agent, why="возврат", expect=PRIMARY_IP, move=3)
    acts = agent.reserve_tgvia_tick(T0 + 11)
    assert ("reresolve", p2) in acts, "новый повод ждёт окна прежней серии"


def test_a_late_result_of_the_previous_reason_does_not_close_the_new_one(agent):
    old = _pending(agent)
    new = _pending(agent, move=4)
    agent.reserve_reresolve_done(old, "set")
    assert agent.reserve_section().get("reresolve_pending") == new


# ── поводы перерезолва: слова резерва и основы ──────────────────────────────

def test_the_serving_word_of_the_reserve_asks_for_the_second_servers_address_once_per_move(gw, host):
    gw.reserve_state_in("serving", 3)
    p = gw.reserve_section().get("reresolve_pending")
    assert p == {"name": RECORD, "expect": RESERVE_IP, "why": "переезд", "move": 3}, p
    gw.reserve_reresolve_done(p, "set")
    gw.reserve_state_in("serving", 3)
    assert gw.reserve_section().get("reresolve_pending") is None, "тот же переезд снова поднял перерезолв"
    gw.reserve_state_in("serving", 4)
    assert (gw.reserve_section().get("reresolve_pending") or {}).get("move") == 4, "новый переезд без перерезолва"


def test_the_idle_word_does_not_ask_for_a_reresolve(gw, host):
    gw.reserve_state_in("idle", 3)
    assert gw.reserve_section().get("reresolve_pending") is None


def test_the_return_word_of_the_primary_asks_for_its_own_address_once(gw, host):
    gw.reserve_apply(_checked(gw.conf, state="away", move=2))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    p = gw.reserve_section().get("reresolve_pending")
    assert p == {"name": RECORD, "expect": PRIMARY_IP, "why": "возврат", "move": 2}, p
    gw.reserve_reresolve_done(p, "same")
    gw.reserve_apply(_checked(gw.conf, state="away", move=2))
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    assert gw.reserve_section().get("reresolve_pending") is None, "тот же возврат снова поднял перерезолв"


def test_a_return_word_older_than_the_reserves_move_asks_nothing(gw, host):
    gw.reserve_apply(_checked(gw.conf, state="away", move=2))
    gw.reserve_state_in("serving", 3)
    gw.reserve_reresolve_done(gw.reserve_section()["reresolve_pending"], "set")
    gw.reserve_apply(_checked(gw.conf, state="idle", move=2))
    assert gw.reserve_section().get("reresolve_pending") is None, "слово основы старее переезда резерва повело аплинк"


def test_the_bypass_marks_survive_a_new_document(gw, host, net):
    gw.reserve_apply(_checked(gw.conf, tg=True))
    _on(gw)
    gw.reserve_apply(_checked(gw.conf, tg=True, move=1))
    sec = gw.reserve_section()
    assert sec.get("tg_via") is True and sec.get("tg_since"), "новый документ стёр отметки обхода"
    assert json.loads(gw._RSV_STORE.read_text(encoding="utf-8"))["tg_via"] is True
