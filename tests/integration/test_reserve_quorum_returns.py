"""Возвраты по кворуму на стороне основы (domain/services/reserve/quorum/returns.py,
outbound.reserve_healthy, outbound_return.reserve_force_home; концепт «кворум»
§3.7.8, §7.2 Т-К19).

«Сразу» — Q2: пока пользователи у резерва, ответчик дома считает «да»
голосующих (роль `follow`, эпоха не сверяется); «здорова» — «да» строго
больше половины v, линк пары жив, Telegram достижим (вместо «свой AWG
отвечает» и «шлюз на связи»). Аварийный — Q1 о своём клоне: свой зонд
`awgqar` и `qview{t = r}` своих шлюзов по каналу конфигурации; «не видят» >
v/2 дольше T_up — `qtake` своим шлюзам и запись на себя путём С9 без условия
«резерв не на связи 10 мин».

Мир — основа «Alpha» в отъезде у «Beta» (tests/integration/_reserve_move.py),
своя линия `a` с составом a, b, a1, a2 (у лидера-клона голосующие a, a1, a2;
у ответчика дома — b, a1, a2; v = 3). Включённость кворума — подмена
предиката `reserve_quorum_active` (её собирают тесты ключей). Пробы — настоящие
датаграммы на ответчик; часы — модельные, общие с автоматом возврата.

Цена ошибки: Q2, отбросивший «да» за «чужую» эпоху, — основа не вернёт
пользователей сама никогда; Q2 «здоров» при одном «да» из трёх — возврат на
основу, до которой пользователи не доходят; аварийный возврат при живом клоне —
два бота у одних пользователей; без него — мёртвый резерв держит
пользователей основы, пока админ не найдёт С9; `qtake` о клоне, ушедший
голосующему о доме, — эпоха и обязательство не того зонда.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import outbound
from awgbot.domain.services.reserve.quorum import leader as leader_mod
from awgbot.domain.services.reserve.quorum import returns as returns_mod
from awgbot.util import probenet
from awgbot.util.quorum.probe import Info, Probe, Reply
from awgbot.util.quorum.voter import Voter

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

MEMBERS = ["a", "b", "a1", "a2"]
T_UP = 100


class _Away:
    def __init__(self, mv, monkeypatch, *, away: bool = True):
        self.w = mv
        self.s = mv.svc
        self.seq = 0
        ns = types.SimpleNamespace(monotonic=mv.clock.monotonic)
        monkeypatch.setattr(leader_mod, "time", ns)
        monkeypatch.setattr(returns_mod, "time", ns)
        self.active = True
        monkeypatch.setattr(self.s, "reserve_quorum_active", lambda: self.active)
        monkeypatch.setattr(self.s, "_quorum_info", lambda: Info(hs=2, flow=True, at=""))
        self.sessions = {1: True, 2: True}
        monkeypatch.setattr(self.s, "gwlink_session", lambda gid: {"agent": "4.2.0"} if self.sessions.get(int(gid)) else {},
                            raising=False)
        settings.set_value("app.reserve.return", "immediate")
        self.s.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": list(MEMBERS)},
                          mine={"l": "a", "g": 1, "members": list(MEMBERS)})
        mv.rsv.gw = False
        if away:
            rm.go_away(self.s)
            rm.st_in(self.s, **{"in": "serving"})
        self.clone = Voter("a", "a")                       # свой зонд к клону у резерва
        self.clone.view = "see"
        self.clone.epoch = 9
        self.s._qvoters["awgqar"] = self.clone

    # ── Q2 ─────────────────────────────────────────────────────────────────
    def probe(self, node: str, *, e: int = 9, w: str = "see") -> Reply | None:
        self.seq += 1
        raw = self.s.reserve_quorum_probe_in(Probe("a", 1, node, e, w, self.seq).encode(), probenet.node_addr("a", node))
        return Reply.parse(raw) if raw else None

    def healthy_for(self, seconds: float, yes: tuple[str, ...]) -> list[tuple]:
        """Такт автомата каждые 3 с, пробы `yes` — каждые 15 с."""
        acts: list[tuple] = []
        end = self.w.clock.t + seconds
        i = 0
        while self.w.clock.t < end:
            if i % 5 == 0:
                for n in yes:
                    self.probe(n)
            acts += self.s.reserve_out_tick()
            self.w.clock.advance(3)
            i += 1
        return acts

    # ── Q1 ─────────────────────────────────────────────────────────────────
    def gw_view(self, slot: int, w: str, *, t: str = "r", since: str = "") -> None:
        body = {"l": "a", "g": 1, "w": w, "e": 9, "since": since or f"2026-10-30T14:0{slot}:{len(w)}0+03:00"}
        if t:
            body["tgt"] = t
        self.s.reserve_quorum_link_in(slot, "qview", body)

    def tick(self) -> list[tuple]:
        return self.s.reserve_quorum_back_tick(self.w.clock.t)

    def run(self, seconds: float, step: float = 5) -> list[tuple]:
        acts: list[tuple] = []
        for _ in range(int(seconds // step)):
            self.w.clock.advance(step)
            acts += self.tick()
        return acts


@pytest.fixture()
def aw(mv, monkeypatch):
    return _Away(mv, monkeypatch)


def gwlink(acts, kind: str) -> list[tuple]:
    return [(a[1], a[3]) for a in acts if a and a[0] == "gwlink" and a[2] == kind]


# ── Q2: «сразу» ─────────────────────────────────────────────────────────────

def test_the_home_responder_answers_away_with_the_follow_role(aw):
    r = aw.probe("a1")
    assert r is not None, "в отъезде ответчик дома молчит — голосующие сочтут основу мёртвой"
    assert r.r == "follow", f"роль ответа из отъезда {r.r!r}"
    assert aw.s.reserve_quorum_q2() == {"y": 1, "v": 3, "majority": False}


def test_q2_counts_yes_whatever_epoch_and_leaves_the_journal_alone(aw):
    """Эпоху линии в отъезде держит бот основы на резерве: голосующие знают её,
    а не эпоху основы. Проба с эпохой больше своей — не «чужая эпоха»."""
    before = dict(aw.s.db.jrn_meta())
    aw.probe("b", e=before["epoch"] + 5)
    aw.probe("a1", e=0)
    assert aw.s.reserve_quorum_q2()["y"] == 2, "Q2 отбросил «да» за эпоху"
    after = aw.s.db.jrn_meta()
    assert (after["holder"], after["role"], after["epoch"]) == (before["holder"], before["role"], before["epoch"]), \
        f"проба в отъезде тронула журнал: {before} → {dict(after)}"


def test_q2_healthy_needs_strictly_more_than_half_and_replaces_own_awg_check(aw):
    aw.probe("a1")
    assert aw.s.reserve_healthy() is False, "одно «да» из трёх — «здорова»"
    aw.probe("b")
    assert aw.s.reserve_healthy() is True, "два «да» из трёх, линк и Telegram — не «здорова»"
    aw.s.db.set_state("last_server_ok", "0")
    assert aw.s.reserve_healthy() is True, "под кворумом «свой AWG отвечает» всё ещё решает"


def test_q2_still_needs_the_pair_link(aw):
    aw.probe("a1")
    aw.probe("b")
    aw.s._lv.state = "down"
    assert aw.s.reserve_healthy() is False, "линк пары молчит, а Q2 «здорова» — слияние не пройдёт"


def test_q2_majority_for_healthy_s_starts_the_return(aw):
    acts = aw.healthy_for(outbound.HEALTHY_S - 30, ("b", "a1"))
    assert ("send", "back_req", {}) not in acts and aw.s.reserve_out() == "away", "возврат раньше HEALTHY_S"
    acts = aw.healthy_for(60, ("b", "a1"))
    assert ("send", "back_req", {}) in acts, "Q2 большинство дольше HEALTHY_S — возврат «сразу» не начат"


def test_q2_minority_never_starts_the_return(aw):
    acts = aw.healthy_for(2 * outbound.HEALTHY_S, ("a1",))
    assert ("send", "back_req", {}) not in acts and aw.s.reserve_out() == "away", "возврат по одному «да» из трёх"


def test_a_leftover_act_off_does_not_take_q2_away_and_home_has_no_q2(aw):
    """Ключа `quorum.act` больше нет: прежний `false` не возвращает старый путь
    здоровья — в отъезде здоровье решает Q2; кворум выключен или дома — Q2 нет."""
    settings.set_value("app.reserve.quorum.act", False)
    assert aw.s.reserve_quorum_healthy() is False, "прежний act=false снял Q2 со здоровья в отъезде"
    aw.active = False
    assert aw.s.reserve_quorum_healthy() is None, "кворум выключен, а Q2 решает здоровье"
    aw.active = True
    aw.s.reserve_set_out("home")
    assert aw.s.reserve_quorum_healthy() is None, "дома Q2 решает здоровье"


# ── Q1: аварийный возврат ───────────────────────────────────────────────────

def _majority(aw) -> list[tuple]:
    aw.clone.view = "uns"
    aw.gw_view(1, "uns")
    return aw.tick()


def test_own_uns_and_one_gateway_uns_asks_the_other_gateway_about_the_clone(aw):
    acts = _majority(aw)
    assert gwlink(acts, "qask") == [(2, {"l": "a", "g": 1, "tgt": "r"})], f"опрос о клоне: {acts}"


def test_no_majority_on_the_clone_for_t_up_takes_the_users_home(aw):
    acts = _majority(aw)
    acts += aw.run(T_UP - 10)
    assert ("take_home",) not in acts and not gwlink(acts, "qtake"), "аварийный возврат раньше T_up"
    acts = aw.run(15)
    assert sorted(gwlink(acts, "qtake")) == [(1, {"l": "a", "e": 9, "tgt": "r"}), (2, {"l": "a", "e": 9, "tgt": "r"})], \
        f"qtake о клоне — всем голосующим с живым каналом: {gwlink(acts, 'qtake')}"
    assert ("take_home",) in acts, "большинство не видит клон дольше T_up — Telegram не забран"
    assert aw.s.reserve_online(), "предпосылка: резерв на связи — условие «10 мин не на связи» не ждётся"
    out = aw.s.rsv_get("rsv_out")
    assert out.get("await_merge") and out.get("quorum_return", {}).get("no") == 2, out
    assert ("take_home",) not in aw.run(60), "аварийный возврат повторяется"


def test_a_majority_seeing_the_clone_does_nothing(aw):
    aw.clone.view = "see"
    aw.gw_view(1, "see")
    aw.gw_view(2, "uns")
    acts = aw.tick() + aw.run(T_UP * 3)
    assert acts == [], f"клон видят двое из трёх, а основа действует: {acts}"
    assert aw.s.reserve_out() == "away"


def test_a_dead_gateway_channel_does_not_count_its_no(aw):
    aw.sessions[1] = False
    acts = _majority(aw) + aw.run(T_UP + 10)
    assert ("take_home",) not in acts, "«нет» шлюза без канала конфигурации в счёте"


def test_views_about_the_server_at_home_do_not_count_for_the_clone(aw):
    aw.clone.view = "uns"
    aw.gw_view(1, "uns", t="")
    aw.gw_view(2, "uns", t="h")
    acts = aw.tick() + aw.run(T_UP + 10)
    assert ("take_home",) not in acts, "взгляды шлюзов на сервер дома решили о клоне"
    assert aw.s.reserve_quorum_back_view()["no"] == 1


def test_own_see_back_releases_the_gateways_that_said_no(aw):
    _majority(aw)
    aw.clone.view = "see"
    acts = aw.tick()
    assert gwlink(acts, "qrel") == [(1, {"l": "a", "e": 9, "tgt": "r"})], f"qrel о клоне: {acts}"
    assert ("take_home",) not in acts + aw.run(T_UP + 10)


def test_a_gateway_that_said_no_and_now_sees_the_clone_still_gets_qrel(aw):
    """Как у кандидата о второй линии (концепт «кворум» §3.5.1): шлюз, сказавший
    «нет» о клоне на опрос, держит обязательство; без `qrel` его пробы клону идут
    `held` до 10 минут, и живой клон лишается голоса."""
    _majority(aw)
    aw.gw_view(1, "see", since="2026-10-30T14:30:00+03:00")
    acts = aw.tick()
    assert (1, {"l": "a", "e": 9, "tgt": "r"}) in gwlink(acts, "qrel"), f"шлюзу 1 qrel не ушёл: {gwlink(acts, 'qrel')}"


def _solo(aw) -> None:
    """Состав из двух серверов: о клоне голосую только я (v = 1)."""
    aw.s.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": ["a", "b"]},
                    mine={"l": "a", "g": 1, "members": ["a", "b"]})


def test_with_v_1_there_is_no_emergency_return_by_q1(aw):
    """Голосует только сама основа: аварийного возврата по Q1 нет ни при каком
    времени — один голос судьбу бота на резерве не решает; счёт для карточки идёт."""
    _solo(aw)
    aw.clone.view = "uns"
    acts = aw.tick() + aw.run(10 * T_UP)
    assert ("take_home",) not in acts and not gwlink(acts, "qask") and not gwlink(acts, "qtake"), \
        f"v = 1: аварийный возврат по Q1 действует: {acts}"
    assert aw.s.reserve_quorum_back_view().get("v") == 1, aw.s.reserve_quorum_back_view()


def test_with_v_1_q2_is_the_vote_of_the_second_server(aw):
    _solo(aw)
    aw.probe("b")
    assert aw.s.reserve_quorum_q2() == {"y": 1, "v": 1, "majority": True}, aw.s.reserve_quorum_q2()


def test_a_leftover_act_off_does_not_stop_the_emergency_return(aw):
    settings.set_value("app.reserve.quorum.act", False)
    acts = _majority(aw) + aw.run(T_UP + 30)
    assert ("take_home",) in acts, "прежний act=false остановил аварийный возврат по Q1"


def test_at_home_there_is_no_clone_candidate(aw):
    aw.s.reserve_set_out("home")
    acts = _majority(aw) + aw.run(T_UP + 10)
    assert acts == [] and aw.s.reserve_quorum_back_view() == {}, "дома основа голосует о своём клоне"


# ── Т-К19 ───────────────────────────────────────────────────────────────────

def test_the_force_dialog_says_how_many_see_the_bot_at_the_reserve(aw):
    from awgbot.bot.texts.reserve.section import force_dialog
    assert aw.s.reserve_force_dialog_data()["quorum"] == {}, "о клоне ещё никто не сказал, а строка Т-К19 есть"
    assert "Бот на" not in force_dialog(aw.s.reserve_force_dialog_data())
    aw.clone.view = "see"
    aw.gw_view(1, "see")
    aw.gw_view(2, "uns")
    aw.tick()
    d = aw.s.reserve_force_dialog_data()
    assert d["quorum"].get("y") == 2 and d["quorum"].get("v") == 3, d["quorum"]
    assert "Бот на «Beta» видят 2 из 3 — если он жив" in force_dialog(d), force_dialog(d)


def test_the_force_dialog_says_nobody_sees_the_bot_at_the_reserve(aw):
    from awgbot.bot.texts.reserve.section import force_dialog
    aw.clone.view = "uns"
    aw.gw_view(1, "uns")
    aw.gw_view(2, "uns")
    aw.tick()
    assert "Бот на «Beta» не видит никто." in force_dialog(aw.s.reserve_force_dialog_data())


def test_a_clone_view_sent_over_the_config_channel_stays_a_clone_view(aw):
    """Сквозь канал: агент кладёт `("link", "qview", {..., "tgt": "r"})`, канал
    конфигурации упаковывает подписанной строкой, сервер разбирает её по полю `t`
    (runtime/linkserver/inbound). Цель `t = r` обязана дойти — иначе «не вижу
    клон» шлюза ляжет в его взгляд на сервер дома, а аварийный возврат не
    увидит ни одного голоса шлюзов."""
    import os
    from awgbot.util import gwlink
    key = os.urandom(32)
    line = gwlink.pack(key, "qview", {"l": "a", "g": 1, "w": "uns", "e": 9, "since": "2026-10-30T14:05:00+03:00", "tgt": "r"},
                       seq=1)
    msg = gwlink.unpack(key, line)
    aw.s.reserve_quorum_link_in(1, str(msg.get("t")), msg)
    assert "a1" in aw.s.reserve_quorum_gw_views("a", "r"), \
        f"взгляд о клоне после канала не о клоне (поле t в строке: {msg.get('t')!r})"
    assert "a1" not in aw.s.reserve_quorum_gw_views("a", "h"), "взгляд о клоне лёг во взгляд на сервер дома"


# ── корреляция голосующих (Т-К15) ───────────────────────────────────────────

@pytest.fixture()
def corr(mv, monkeypatch):
    """Своя линия a, b, a1, a2, a3 дома; адреса — подмена эндпоинтов линков и
    адреса второго сервера."""
    svc = mv.svc
    svc.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": ["a", "b", "a1", "a2", "a3"]},
                   mine={"l": "a", "g": 1, "members": ["a", "b", "a1", "a2", "a3"]})
    ips = {"peer": "192.0.2.7", 1: "203.0.113.5:51820", 2: "198.51.100.40:443", 3: None}
    monkeypatch.setattr(svc, "reserve_peer_ip", lambda: ips["peer"])
    monkeypatch.setattr(svc, "gateway_external_ip", lambda slot: ips.get(int(slot)))
    return svc, ips


def test_no_shared_net_no_correlation(corr):
    svc, _ = corr
    assert svc.reserve_quorum_correlation() == [], "голосующие из разных сетей — строка Т-К15"


def test_two_gateways_in_one_24_are_named_with_the_net(corr):
    svc, ips = corr
    ips[2] = "203.0.113.77:51820"
    assert svc.reserve_quorum_correlation() == [(["Шлюз 1", "Шлюз 2"], "203.0.113.0/24")]


def test_the_peer_server_and_a_gateway_in_one_net(corr):
    svc, ips = corr
    ips[1] = "192.0.2.200:51820"
    assert svc.reserve_quorum_correlation() == [(["Beta", "Шлюз 1"], "192.0.2.0/24")]


def test_three_voters_in_one_net_and_an_unknown_endpoint(corr):
    svc, ips = corr
    ips.update({1: "203.0.113.5:1", 2: "203.0.113.6:2", 3: "203.0.113.7:3"})
    assert svc.reserve_quorum_correlation() == [(["Шлюз 1", "Шлюз 2", "Шлюз 3"], "203.0.113.0/24")]
    ips[3] = None
    assert svc.reserve_quorum_correlation() == [(["Шлюз 1", "Шлюз 2"], "203.0.113.0/24")], \
        "шлюз без известного эндпоинта посчитан в общей сети"


def test_without_my_line_there_is_no_correlation(corr):
    svc, ips = corr
    ips[2] = "203.0.113.77:51820"
    svc.rsv_update("rsv_quorum", active=None)
    assert svc.reserve_quorum_correlation() == []


# ── принятая дома эпоха → away → аварийный возврат ──────────────────────────

def test_an_epoch_adopted_at_home_with_a_dead_reserve_ends_in_a_forced_return(mv, monkeypatch):
    """Кандидат забрал пользователей (голосующие под e + 1) и умер, не сказав `st`;
    основа дома приняла эпоху по пробе шлюза. Такт переводит её в away, Q1 о своём
    боте на резерве (клон не видит никто) — аварийный возврат: запись на себя,
    Telegram, эпоха здесь. Без перехода в away основа следовала бы мёртвому
    резерву бессрочно."""
    aw = _Away(mv, monkeypatch, away=False)
    s = aw.s
    e = s.reserve_quorum_epoch()
    monkeypatch.setattr(s, "reserve_peer_seen", lambda: "down")
    aw.probe("a1", e=e + 1)
    assert s.db.jrn_meta()["holder"] == "peer", "предпосылка: эпоха по пробе не принята"
    assert s.reserve_out() == "home"
    s.reserve_out_tick()
    assert s.reserve_out() == "away", "принятая дома эпоха без слова резерва не повела автомат в away"
    aw.clone.view = "uns"
    aw.gw_view(1, "uns")
    aw.gw_view(2, "uns")
    acts = aw.tick() + aw.run(T_UP + 10)
    assert ("take_home",) in acts, f"мёртвый резерв держит пользователей основы: {acts}"
    meta = s.db.jrn_meta()
    assert (meta["holder"], meta["role"]) == ("self", "holder"), f"после аварийного возврата запись не здесь: {meta}"
    assert not s.reserve_quorum_adopted(), "отметка принятия пережила возврат"
    assert s.rsv_get("rsv_out").get("await_merge"), "возврат без резерва не отмечен — реплика при встрече не вольётся"
