"""Кворум резерва, этап зондов, основной бот: ответчик и счёт «да» лидера
(quorum/leader), пробы и взгляд голосующего (quorum/voter), слова каналов
(quorum/inbound), такт и план сокетов (quorum/tick), бот основы на резерве
(ответчик на клоне, `quorum` и `qcount` локального канала), карточка
(quorum/screen и bot/texts/reserve/section) — концепт «кворум» §3.2, §3.3,
§6.1, §6.2. Решения лидера проверены здесь только на старте и при выключенных
`act`; полностью — в test_reserve_quorum_fence.

Хост — подмены `_reserve_quorum_common`; модельные часы — monotonic модулей
кворума; дамп интерфейса AWG — текстом в формате `awg show … dump` через
настоящий разбор (`parse_dump`).

Цена ошибки: проба, засчитанная с чужого адреса, — один шлюз голосует за
другой; «да» с неверной эпохой или взглядом «sus» — асимметричная потеря даёт
голос дважды; справка с зондами — «трафик идёт», когда пользователей нет;
`qview` без смены взгляда или второму серверу без `quo1` — шум или разрыв
канала пары; взгляд шлюза из канала не той линии — карточка врёт, кто кого
видит; лидер, пишущий решения на старте без большинства «да», или решения при
выключенных `act` — переезд и запись без кворума.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.core import config, settings
from awgbot.infra import awg
from awgbot.util.quorum.probe import Info, Probe, Reply
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_quorum_common as qc

pytestmark = pytest.mark.integration
rsv = rc.rsv
quo = qc.quo

ADDR = {"b": "169.254.88.4", "a1": "169.254.88.5", "a2": "169.254.88.6", "a": "169.254.88.3"}


@pytest.fixture()
def lead(quo, monkeypatch):
    """Линия «Alpha» включена (состав подтверждён, ключи кворума на месте), пиры-зонды
    на awg0; дамп awg0 — без устройств."""
    m = qc.enable(quo)
    quo.reserve_probes_refresh(m)
    quo.reserve_probe_peers_sync(m)
    quo.dump = "PRIV\tPUB\t43125\toff\n"
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: awg.parse_dump(quo.dump))
    return quo


def _probe(n: str, *, l: str = "a", g: int = 1, e: int = 0, w: str = "see", s: int = 7) -> bytes:
    return Probe(l, g, n, e, w, s).encode()


def _ask(svc, n: str, src: str | None = None, **kw) -> Reply | None:
    out = svc.reserve_quorum_probe_in(_probe(n, **kw), src or ADDR[n])
    return Reply.parse(out) if out else None


# ── ответчик ─────────────────────────────────────────────────────────────────

def test_the_responder_listens_on_the_second_address_at_home(lead):
    assert lead.reserve_quorum_responder_addr() == ("169.254.88.1", 8789)
    assert lead.quorum_runtime_plan()["responder"] == ("169.254.88.1", 8789)


def test_while_the_users_are_away_the_home_responder_keeps_counting_for_the_return(lead):
    """В отъезде ответчик дома не умолкает: его «да» — счёт Q2 для возврата «сразу».
    Умолкни он — основа не узнает, что голосующие снова до неё доходят, и
    пользователи не вернутся сами."""
    lead.rsv_update("rsv_out", state="away")
    assert lead.reserve_quorum_responder_addr() == ("169.254.88.1", 8789), "ответчик дома умолк в отъезде"
    r = _ask(lead, "b")
    assert r is not None and r.r == "follow", f"в отъезде ответчик дома — не в роли счёта для возврата: {r}"


def test_no_responder_while_the_vote_is_off(lead):
    """Голосование выключено (второй сервер 4.1.x) — ответчика нет: иначе пробы
    считаются без кворума и карточка рисует голоса, которых нет."""
    qc.peer_old(lead)
    assert lead.reserve_quorum_responder_addr() is None, "ответчик при выключенном голосовании"


def test_a_proper_probe_gets_a_reply_with_the_count(lead):
    r = _ask(lead, "b", s=4242)
    assert r is not None, "проба второго сервера с его адреса без ответа"
    assert (r.l, r.g, r.s, r.v) == ("a", 1, 4242, 3), r
    assert r.r in ("lead", "fenced") and r.y == 1


def test_a_probe_from_another_nodes_address_is_not_counted(lead):
    """Узел сверяется с адресом источника: шлюз 1 не голосует за шлюз 2."""
    assert _ask(lead, "a2", src=ADDR["a1"]) is None
    assert lead.reserve_quorum_count()["y"] == 0, "проба с чужого адреса засчитана"


@pytest.mark.parametrize("data", [b"", b"garbage", b'{"q":2}', _probe("b", l="b"), b"{" * 600])
def test_garbage_or_another_line_gets_no_reply(lead, data):
    assert lead.reserve_quorum_probe_in(data, ADDR["b"]) is None


def test_the_leaders_own_probe_is_not_a_vote(lead):
    assert _ask(lead, "a") is None


def test_yes_is_counted_only_for_see_with_the_current_epoch(lead):
    _ask(lead, "b")
    _ask(lead, "a1", w="sus")
    _ask(lead, "a2", e=5)
    c = lead.reserve_quorum_count()
    assert (c["y"], c["v"], c["t"]) == (1, 3, 2), f"«да» засчитано за sus или за чужую эпоху: {c}"
    assert c["voters"]["a1"]["w"] == "sus" and c["voters"]["a2"]["e"] == 5


def test_yes_fades_after_the_window(lead):
    _ask(lead, "b")
    _ask(lead, "a1")
    assert lead.reserve_quorum_count()["y"] == 2
    lead.clock.t += lead.reserve_quorum_timing().yes_window_s + 1
    assert lead.reserve_quorum_count()["y"] == 0, "«да» живёт дольше окна"


def test_a_greater_epoch_in_probes_is_logged_once(lead, caplog):
    with caplog.at_level(logging.WARNING, logger="awgbot.services"):
        _ask(lead, "b", e=9)
        _ask(lead, "a1", e=9)
    told = [r for r in caplog.records if "эпоху держит другая сторона" in r.getMessage()]
    assert len(told) == 1, f"предупреждение об эпохе не один раз: {[r.getMessage() for r in told]}"


def _jrn(s) -> tuple[str, str]:
    m = s.db.jrn_meta()
    return str(m["holder"]), str(m["role"])


def test_the_starting_leader_is_fenced_until_the_first_majority_of_yes(lead):
    """Старт лидера — без права записи: журнал ограждён (держатель self, роль
    follower), пока большинство «да» не придёт; первое большинство снимает
    ограждение молча. Пиши лидер на старте — после перезапуска без голосов
    у пользователей два бота с расходящимися базами; Т-К7/Т-К8 на старте — ложная
    тревога админу."""
    acts = lead.reserve_quorum_leader_tick()
    assert acts == [], f"старт лидера дал уведомление: {acts}"
    assert _jrn(lead) == ("self", "follower"), f"стартующий лидер пишет решения: {_jrn(lead)}"
    assert lead.reserve_quorum_count()["role"] == "fenced"
    assert (lead.rsv_get("rsv_qlog").get("fenced") or {}).get("on"), "эпизод старта без отметки"
    e = lead.reserve_quorum_epoch()
    for n in ("b", "a1"):
        _ask(lead, n, e=e)
    acts = lead.reserve_quorum_leader_tick()
    assert acts == [], f"первое большинство после старта дало уведомление: {acts}"
    assert _jrn(lead) == ("self", "holder"), f"большинство «да» не вернуло право записи: {_jrn(lead)}"
    assert lead.reserve_quorum_count()["role"] == "lead"


def test_with_act_off_the_leader_counts_but_takes_no_decisions(lead):
    """`app.reserve.quorum.act = false` — только наблюдение: счёт идёт, журнал не
    трогается, действий нет. Иначе выключатель решений не выключает ничего, и
    сбой голосования оставляет пользователей без бота."""
    settings.set_value("app.reserve.quorum", {**lead.reserve_quorum_settings(), "act": False})
    before = dict(lead.db.jrn_meta())
    e = lead.reserve_quorum_epoch()
    for n in ("b", "a1"):
        _ask(lead, n, e=e)
    acts = lead.reserve_quorum_leader_tick()
    assert lead.reserve_quorum_count()["y"] == 2, "счёт «да» не идёт при выключенных решениях"
    lead.clock.t += 300
    for _ in range(20):
        acts += lead.reserve_quorum_leader_tick()
        lead.clock.t += 5
    assert acts == [], f"такт лидера дал действия при act = false: {acts}"
    assert dict(lead.db.jrn_meta()) == before, "журнал тронут при act = false"


# ── справка ──────────────────────────────────────────────────────────────────

def _dump_line(pub: str, allowed: str, hs: int, rx: int) -> str:
    return f"{pub}\t(none)\t203.0.113.5:1\t{allowed}\t{hs}\t{rx}\t{rx}\t25\n"


def test_the_info_counts_devices_with_a_fresh_handshake_and_not_the_probes(lead):
    now = int(lead.clock.time())
    lead.dump = ("PRIV\tPUB\t43125\toff\n" + _dump_line("U1", "10.8.1.2/32", now - 60, 100)
                 + _dump_line("U2", "10.8.1.3/32", now - 900, 100) + _dump_line("Q4", "169.254.88.4/32", now - 5, 100))
    r = _ask(lead, "b")
    assert r.i.hs == 1, f"устройств на связи посчитано {r.i.hs}, а не одно (зонды и старые рукопожатия — не в счёт)"


def test_the_info_flow_follows_the_users_and_not_the_probes(lead):
    now = int(lead.clock.time())
    users = lambda rx: _dump_line("U1", "10.8.1.2/32", now, rx)              # noqa: E731
    probe = lambda rx: _dump_line("Q4", "169.254.88.4/32", now, rx)           # noqa: E731
    lead.dump = "PRIV\tPUB\t43125\toff\n" + users(100) + probe(100)
    assert _ask(lead, "b").i.flow is False, "трафик «идёт» по первому замеру"
    lead.clock.t += 31
    lead.dump = "PRIV\tPUB\t43125\toff\n" + users(100) + probe(5000)
    assert _ask(lead, "b").i.flow is False, "рост счётчика зонда принят за трафик пользователей"
    lead.clock.t += 31
    lead.dump = "PRIV\tPUB\t43125\toff\n" + users(900) + probe(5000)
    assert _ask(lead, "b").i.flow is True, "рост приёма пользователей не замечен"


def test_the_dump_is_read_not_more_often_than_every_30_seconds(lead, monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: calls.append(1) or [])
    for _ in range(5):
        _ask(lead, "b")
        lead.clock.t += 5
    assert len(calls) == 1, f"дамп awg0 читается на каждую пробу: {len(calls)}"
    lead.clock.t += 30
    _ask(lead, "b")
    assert len(calls) == 2


def test_a_failing_dump_still_answers(lead, monkeypatch):
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: (_ for _ in ()).throw(awg.AwgError("нет awg")))
    r = _ask(lead, "b")
    assert r is not None and r.i.hs == 0


# ── бот основы на резерве ────────────────────────────────────────────────────

@pytest.fixture()
def guest(quo, monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: [])
    quo.reserve_quorum_guest_in({"l": "a", "g": 3, "members": ["a", "b", "a1", "a2"], "port": 8789})
    return quo


def test_the_guest_responder_is_the_clones_second_address(guest):
    assert guest.reserve_quorum_responder_addr() == ("169.254.88.2", 8789)
    plan = guest.quorum_runtime_plan()
    assert plan == {"responder": ("169.254.88.2", 8789), "probes": {}}, "бот основы на резерве поднимает свои зонды"


def test_on_the_clone_the_primary_itself_votes_and_the_reserve_does_not(guest):
    r = _ask(guest, "a", g=3)
    assert r is not None and r.v == 3, "основа не голосует о своём клоне или v не без держателя клона"
    assert _ask(guest, "b", g=3) is None, "держатель клона голосует сам за себя"


def test_the_guest_sends_its_count_to_the_reserve_bot_once_per_change(guest):
    _ask(guest, "a", g=3)
    acts = guest.quorum_tick()
    sent = [a for a in acts if a[:2] == ("guestctl", "qcount")]
    assert len(sent) == 1 and sent[0][2]["v"] == 3 and sent[0][2]["y"] == 1, acts
    assert not [a for a in guest.quorum_tick() if a[0] == "guestctl"], "qcount без смены счёта"
    _ask(guest, "a1", g=3, s=9)
    assert [a for a in guest.quorum_tick() if a[0] == "guestctl"][0][2]["y"] == 2


def test_without_members_the_guest_has_no_responder(guest):
    guest.reserve_quorum_guest_in({"l": "", "members": []})
    assert guest.reserve_quorum_responder_addr() is None


def test_the_reserve_bot_tells_its_guest_the_line_from_st(quo):
    quo.rsv_update("rsv_peer_state", quo={"l": "b", "g": 2, "members": ["b", "a", "b1"], "active": True, "why": []})
    assert quo.reserve_quorum_for_guest() == {"l": "b", "g": 2, "members": ["b", "a", "b1"], "port": 8789}
    quo.rsv_update("rsv_peer_state", quo={})
    assert quo.reserve_quorum_for_guest() is None


def test_while_hosting_the_tick_sends_the_line_to_the_guest_once(quo):
    quo.rsv_update("rsv_peer_state", quo={"l": "b", "g": 2, "members": ["b", "a", "b1"], "active": True, "why": []})
    quo.reserve_set_in("serving")
    sent = [a for a in quo.quorum_tick() if a[:2] == ("guest", "quorum")]
    assert len(sent) == 1 and sent[0][2]["members"] == ["b", "a", "b1"]
    assert not [a for a in quo.quorum_tick() if a[:2] == ("guest", "quorum")], "состав гостю на каждом такте"


def test_the_guests_count_reaches_the_card(quo):
    qc.enable(quo)
    quo.rsv_update("rsv_out", state="away")
    quo.reserve_quorum_qcount_in({"y": 3, "v": 3, "r": "lead"})
    mine = quo.reserve_quorum_card()["mine"]
    assert (mine["leader_here"], mine["y"], mine.get("from_guest")) == (False, 3, True), mine


# ── голосующий о второй линии ────────────────────────────────────────────────

def _b_conf() -> str:
    from tests.integration.test_reserve_quorum_probes import _other_side_conf
    return _other_side_conf()


@pytest.fixture()
def voter(quo):
    quo.rsv_update("rsv_peer_state", quo={"l": "b", "g": 2, "members": ["b", "a", "b1"], "active": True, "why": []})
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _b_conf()})
    return quo


def _next_probe(s, acts=None) -> Probe:
    for _ in range(40):
        got = s.reserve_quorum_voter_tick()
        if acts is not None:
            acts += got
        probes = [a for a in got if a[0] == "probe"]
        if probes:
            assert probes[0][1] == "awgqbh" and probes[0][3] == ("169.254.88.65", 8789), probes
            return Probe.parse(probes[0][2])
        s.clock.t += 1
    raise AssertionError("за 40 с проба не ушла")


def _answer(s, p: Probe, **kw) -> list[tuple]:
    r = Reply(p.l, p.g, kw.get("e", 0), kw.get("r", "lead"), p.s, kw.get("y", 2), kw.get("v", 2),
              Info(hs=4, flow=True, at="2026-11-02T14:07:10"))
    return s.reserve_quorum_reply_in("awgqbh", r.encode())


def test_the_voter_probes_the_other_line_as_its_server_node(voter):
    p = _next_probe(voter)
    assert (p.l, p.g, p.n, p.w) == ("b", 2, "a", "new"), p


def test_probes_go_with_jitter_not_on_a_grid(voter):
    times = []
    for _ in range(8):
        _answer(voter, _next_probe(voter))
        times.append(voter.clock.t)
        voter.clock.t += 1
    gaps = {round(b - a) for a, b in zip(times, times[1:])}
    assert len(gaps) > 1, f"пробы строго периодичны — маячок: {gaps}"
    assert all(8 <= g <= 23 for g in gaps), f"интервал проб вне 15 с ± 40 %: {gaps}"


def test_two_replies_make_see_and_a_qview_goes_to_the_second_server_once(voter):
    assert _answer(voter, _next_probe(voter)) == []
    voter.clock.t += 1
    acts = _answer(voter, _next_probe(voter))
    assert [a[:2] for a in acts] == [("send", "qview")], acts
    body = acts[0][2]
    assert (body["l"], body["g"], body["w"]) == ("b", 2, "see") and body["i"]["hs"] == 4
    assert voter.rsv_get("rsv_view")["quo_views"]["awgqbh"]["w"] == "see", "взгляд не записан для карточки"
    voter.clock.t += 1
    assert _answer(voter, _next_probe(voter)) == [], "qview без смены взгляда"


def test_no_qview_to_a_peer_without_quo1_or_offline(voter):
    qc.peer_old(voter)
    _answer(voter, _next_probe(voter))
    voter.clock.t += 1
    assert _answer(voter, _next_probe(voter)) == [], "qview второму серверу 4.1.x — разрыв канала пары"
    assert voter.rsv_get("rsv_view")["quo_views"]["awgqbh"]["w"] == "see", "взгляд не записан, хотя слать некому"


def test_no_qview_while_the_pair_link_is_down(voter):
    voter.rsv_update("rsv_peer_state", session=False)
    _answer(voter, _next_probe(voter))
    voter.clock.t += 1
    assert _answer(voter, _next_probe(voter)) == []


def test_silence_becomes_uns_and_is_told_once(voter):
    acts = []
    for _ in range(80):
        acts += voter.reserve_quorum_voter_tick()
        voter.clock.t += 1
    views = [a[2]["w"] for a in acts if a[:2] == ("send", "qview")]
    assert views == ["uns"], f"молчание — не один «не вижу»: {views}"


def test_without_probe_confs_the_voter_tick_is_empty(quo):
    assert quo.reserve_quorum_voter_tick() == []


def test_a_reply_for_an_unknown_probe_is_ignored(voter):
    p = _next_probe(voter)
    other = Probe(p.l, p.g, p.n, p.e, p.w, p.s + 1)
    assert _answer(voter, other) == [] and _answer(voter, other) == []
    assert voter.reserve_quorum_reply_in("awgqzz", b"{}") == []


def test_my_view_reaches_the_card(voter):
    for _ in range(2):
        _answer(voter, _next_probe(voter), y=2, v=2)
        voter.clock.t += 1
    peer = voter.reserve_quorum_card()["peer"]
    assert (peer["my_w"], peer["y"], peer["yv"]) == ("see", 2, 2), peer


# ── слова каналов ────────────────────────────────────────────────────────────

VIEW = {"l": "a", "g": 1, "w": "uns", "since": "2026-11-02T14:05:00", "e": 0}


def test_qview_of_the_second_server_is_its_view_on_my_line(quo):
    quo.reserve_quorum_pair_in("qview", VIEW)
    views = quo.reserve_quorum_gw_views("a")
    assert views["b"]["w"] == "uns" and views["b"]["via"] == "pair", views


def test_an_unknown_view_is_dropped(quo):
    quo.reserve_quorum_pair_in("qview", {**VIEW, "w": "lead"})
    quo.reserve_quorum_link_in(1, "qview", {**VIEW, "w": ""})
    quo.reserve_quorum_guest_in_kind(1, "qview", {**VIEW, "w": "maybe"})
    assert quo.reserve_quorum_gw_views("a") == {} and quo.reserve_quorum_gw_views("b") == {}


def test_qview_of_my_gateway_over_the_config_channel_is_about_my_line(quo):
    quo.reserve_quorum_link_in(2, "qview", {**VIEW, "l": "a", "w": "see"})
    assert quo.reserve_quorum_gw_views("a")["a2"]["via"] == "link"


def test_qview_of_the_other_lines_gateway_over_the_guest_channel_is_about_its_line(quo):
    quo.reserve_guest_in(1, "qview", {**VIEW, "l": "b", "w": "see"})
    views = quo.reserve_quorum_gw_views("b")
    assert views["b1"]["w"] == "see" and views["b1"]["via"] == "guest", views
    assert quo.reserve_quorum_gw_views("a") == {}, "взгляд шлюза второй стороны лёг на мою линию"


def test_the_snapshot_quo_is_a_view_of_my_gateway_but_does_not_override_the_channel(quo):
    quo.reserve_quorum_snapshot_in(1, {"w": "uns", "since": "2026-11-02T14:05:00", "g": 1, "e": 0})
    assert quo.reserve_quorum_gw_views("a")["a1"]["via"] == "snap"
    quo.reserve_quorum_link_in(1, "qview", {**VIEW, "w": "see"})
    quo.reserve_quorum_snapshot_in(1, {"w": "see", "g": 1, "e": 0})
    assert quo.reserve_quorum_gw_views("a")["a1"]["via"] == "link", "снимок перебил свежий взгляд из канала"
    quo.reserve_quorum_snapshot_in(1, None)
    quo.reserve_quorum_snapshot_in(1, {"w": "мусор"})
    assert quo.reserve_quorum_gw_views("a")["a1"]["w"] == "see"


def test_qstate_is_remembered(quo):
    quo.reserve_quorum_pair_in("qstate", {"l": "b", "e": 7, "holder": "self", "r": "lead"})
    st = quo.__dict__["_quo_peer_state"]
    assert (st["l"], st["e"], st["holder"], st["r"]) == ("b", 7, "self", "lead")


def test_words_of_later_stages_change_nothing(quo):
    for kind in ("qmem", "qmem_ok", "qkeys", "qkeys_ok", "qpause"):
        assert quo.reserve_quorum_pair_in(kind, {"l": "b"}) == []
    assert quo.reserve_quorum_guest_in_kind(1, "qshare", {"l": "a", "q_gen": 1, "x": 5, "y": "ab" * 16}) == []


# ── такт и карточка ──────────────────────────────────────────────────────────

def test_the_tick_sends_qprobe_once_and_takes_no_decisions(quo):
    acts = quo.quorum_tick()
    sent = [a for a in acts if a[:2] == ("send", "qprobe")]
    assert len(sent) == 1 and "Address = 169.254.88.4/32" in sent[0][2]["conf"] and "_digest" not in sent[0][2]
    later = quo.quorum_tick()
    assert not [a for a in later if a[:2] == ("send", "qprobe")], "qprobe на каждом такте"
    kinds = {a[0] for a in acts + later}
    assert kinds <= {"send", "probe", "guest"}, f"такт этапа наблюдения дал решение: {kinds}"


def test_without_a_pair_the_tick_is_empty_and_forgets_the_voters(quo):
    quo.db.reserve_peer_delete()
    assert quo.quorum_tick() == []
    assert quo.quorum_runtime_plan() == {"responder": None, "probes": {}}


def test_the_card_shows_the_voters_by_name(lead):
    _ask(lead, "b")
    lead.reserve_quorum_link_in(2, "qview", {**VIEW, "w": "uns"})
    card = lead.reserve_quorum_card()
    assert card["on"] and card["mine"]["leader_here"] and card["mine"]["y"] == 1
    rows = {v["node"]: v for v in card["mine"]["voters"]}
    assert rows["b"]["name"] == "Beta" and rows["b"]["w"] == "see"
    assert rows["a2"]["w"] == "uns" and rows["a2"]["since"] == "2026-11-02T14:05:00"
    assert rows["a1"]["silent"], "шлюз, от которого проб не было, не помечен молчащим"


def test_the_card_text_carries_the_vote_block(lead):
    from awgbot.bot.texts.reserve.section import card_text
    _ask(lead, "b")
    _ask(lead, "a1")
    lead.reserve_quorum_leader_tick()
    text = card_text({**lead.reserve_card_data(), "dir_out": (True, "")})
    assert "🗳 Этот сервер видят 2 из 3 — пользователи переедут, если не увидят 2" in text, text
    assert "«Beta» — видит · «GW1» — видит · «GW2» — молчит" in text, text


def test_the_card_says_why_the_vote_is_off(quo):
    from awgbot.bot.texts.reserve.section import card_text
    quo.reserve_quorum_refresh()
    qc.peer_old(quo)
    text = card_text({**quo.reserve_card_data(), "dir_out": (True, "")})
    assert "🗳 Голосование не включено: у «Beta» нет v4.2.0" in text, text

