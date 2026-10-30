"""Ничья кворума (domain/services/reserve/quorum/tie.py; концепт «кворум» §3.7.5,
§6.4, §7.3): v чётно, «да» = «нет» = v/2 без молчащих дольше T_tie.

Кандидат — Т-К5б своему админу (звук), Т-К5в админу спорного сервера в его
роли (звук, кнопка аварийного кода) — только если тот открывал бота; окно
кода — пока свой взгляд «не вижу»; ничьей нет ≥ `tie_clear_s` — Т-К6 тем же
адресатам. Лидер — Т-К5 поимённо со своей справкой по копиям `qview`, отбой
Т-К6; карточка — строка Т-К4, диалог переезда — строка Т-К18.

Мир кандидата — резерв «Alpha» (сторона `a`) в паре с «Beta»
(tests/integration/_reserve_vote_common.py): линия `b` с составом b, a, b1, b2,
b3 (голосующие у лидера `b` — a, b1, b2, b3; v = 4), шлюзы «Beta» — слоты 1–3
на гостевом канале; свой взгляд — машина зонда `awgqbh` с выставленным
взглядом. Мир лидера — основа «Alpha» дома (tests/integration/_reserve_move.py),
своя линия `a` с составом a, b, a1, a2, a3 (v = 4); пробы — настоящие
датаграммы на ответчик, «нет» — `qview` своих шлюзов по каналу конфигурации и
второго сервера по каналу пары. Часы — модельные.

Цена ошибки: тревога ничьей на каждый такт — админы глушат бота и пропускают
настоящую аварию; тревоги нет — пользователи «Alpha» без VPN, а оба бота
молчат, потому что сами ничего не решат; Т-К5в админу, который бота не
открывал, — Telegram отвергнет сообщение, а Т-К5б при этом врёт «бот написал
ему»; окно кода при своём «вижу» — аварийный код принимается, когда кандидат
сам видит живой сервер; отбой без тревоги или тревога без отбоя — админы не
знают, кончилось ли.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve.quorum import candidate as cand_mod
from awgbot.domain.services.reserve.quorum import fence as fence_mod
from awgbot.domain.services.reserve.quorum import leader as leader_mod
from awgbot.util import probenet, rsvkeys
from awgbot.util.quorum.probe import Info, Probe
from awgbot.util.quorum.voter import Voter

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_vote_common import box, notes

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["box"]

LINE = "b"
MEMBERS = ["b", "a", "b1", "b2", "b3"]
T_TIE = 180
CLEAR = 60
INFO = {"hs": 37, "flow": True, "at": "2026-10-30T14:07:00+03:00"}
RSV_Y = "ab" * rsvkeys.M_LEN
QBOX = "00" * rsvkeys.BOX_LEN


def _by_addr(ns) -> dict[int, list]:
    out: dict[int, list] = {}
    for n in ns:
        out.setdefault(int(n.tg_id), []).append(n)
    return out


def _tie_notes(acts, word: str = "Ничья") -> dict[int, list]:
    return _by_addr([n for n in notes(acts) if word in n.text])


# ── кандидат ────────────────────────────────────────────────────────────────

class _Cand:
    def __init__(self, b, monkeypatch, members=MEMBERS):
        self.b = b
        self.s = b.s
        monkeypatch.setattr(cand_mod, "time", types.SimpleNamespace(monotonic=b.clock.monotonic, time=b.clock.time))
        settings.set_value("app.reserve.quorum.act", True)
        self.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": list(members), "active": True, "why": []})
        assert self.s.reserve_quorum_side() == "a", "подготовка: резерв в тестах — сторона a"
        self.s.reserve_qkeys_in({"l": LINE, "m_gen": 1, "q_gen": 4, "t": 3, "x": rsvkeys.RESERVE_X,
                                 "y": RSV_Y, "qbox": QBOX, "c_gen": 1, "cy": RSV_Y, "cbox": QBOX})
        self.v = Voter(LINE, "a")
        self.v.view = "see"
        self.v.epoch = 7
        self.s._qvoters[f"awgq{LINE}h"] = self.v
        for slot in range(1, len(members) - 1):
            b.hello(slot)
        b.peer_admin_opened(True)

    def own(self, w: str) -> None:
        self.v.view = w

    def view(self, slot: int, w: str, *, info: dict | None = None, since: str = "") -> list[tuple]:
        body = {"l": LINE, "g": 1, "w": w, "e": 7, "since": since or f"2026-10-30T14:0{slot}:{len(w)}0+03:00"}
        if info:
            body["i"] = dict(info)
        return self.s.reserve_guest_in(slot, "qview", body)

    def tick(self) -> list[tuple]:
        return self.s.reserve_quorum_candidate_tick(self.b.clock.t)

    def run(self, seconds: float, step: float = 5) -> list[tuple]:
        acts: list[tuple] = []
        for _ in range(int(seconds // step)):
            self.b.advance(step)
            acts += self.tick()
        return acts

    def tie(self, own: str = "uns") -> list[tuple]:
        """Два «вижу» и два «не вижу»; справку несут видящие шлюзы."""
        self.own(own)
        if own == "uns":
            self.view(1, "uns")
            self.view(2, "see", info=INFO)
            self.view(3, "see", info=INFO)
        else:
            self.view(1, "uns")
            self.view(2, "uns")
            self.view(3, "see", info=INFO)
        return self.tick()

    def untie(self) -> None:
        """Шлюз 1 снова видит «Beta»: «нет» 1 из 4 — ни ничьей, ни большинства."""
        self.own("uns")
        self.view(1, "see", info=INFO, since="2026-10-30T14:20:00+03:00")
        self.view(2, "see", info=INFO)
        self.view(3, "see", info=INFO)


@pytest.fixture()
def c(box, monkeypatch):
    return _Cand(box, monkeypatch)


def test_a_tie_held_for_t_tie_tells_each_admin_once(c):
    acts = c.tie()
    acts += c.run(T_TIE - 5)
    assert not _tie_notes(acts), "тревога ничьей раньше T_tie"
    acts = c.run(10)
    got = _tie_notes(acts)
    assert set(got) == {config.ADMIN_ID, rc.PEER_ADMIN}, f"адресаты тревоги ничьей: {sorted(got)}"
    mine, peer = got[config.ADMIN_ID], got[rc.PEER_ADMIN]
    assert len(mine) == 1 and len(peer) == 1, "тревога ничьей — не одно сообщение каждому"
    assert mine[0].force_sound and peer[0].force_sound, "тревога ничьей без звука"
    assert peer[0].keyboard == "peer_code", "Т-К5в без кнопки аварийного кода"
    assert "бот написал ему" in mine[0].text, mine[0].text
    assert "о твоём сервере" in peer[0].text, peer[0].text
    more = c.run(600)
    assert not _tie_notes(more), "ничья держится — тревога повторяется"


def test_the_tie_alarm_carries_the_traffic_note_of_a_seeing_gateway(c):
    c.tie()
    acts = c.run(T_TIE + 5)
    text = _tie_notes(acts)[config.ADMIN_ID][0].text
    assert "на связи 37 устройств, трафик идёт" in text, f"справка видящего шлюза не в тревоге: {text}"
    assert "со слов" in text and "шлюз" in text, f"не сказано, со слов кого справка: {text}"
    assert "Видят:" in text and "Не видят:" in text


def test_the_freshest_traffic_note_wins(c):
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "see", info={**INFO, "hs": 5, "at": "2026-10-30T14:01:00+03:00"})
    c.view(3, "see", info={**INFO, "hs": 9, "at": "2026-10-30T14:06:00+03:00"})
    c.tick()
    text = _tie_notes(c.run(T_TIE + 5))[config.ADMIN_ID][0].text
    assert "на связи 9 устройств" in text and "шлюз 3" in text, f"справка не самая свежая: {text}"


def test_without_a_traffic_note_the_alarm_says_there_is_none(c):
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "see")
    c.view(3, "see")
    c.tick()
    text = _tie_notes(c.run(T_TIE + 5))[config.ADMIN_ID][0].text
    assert "Справки о трафике «Beta» нет" in text, text


def test_a_peer_admin_who_never_opened_the_bot_gets_nothing_and_the_alarm_says_so(c):
    c.b.peer_admin_opened(False)
    c.tie()
    got = _tie_notes(c.run(T_TIE + 5))
    assert rc.PEER_ADMIN not in got, "Т-К5в админу, который бота не открывал — Telegram его не доставит"
    assert "написать ему некуда" in got[config.ADMIN_ID][0].text, got[config.ADMIN_ID][0].text


def test_own_uns_at_a_tie_opens_the_code_window(c):
    c.tie("uns")
    c.run(T_TIE + 5)
    assert c.s.reserve_code_window()["open"], "ничья и свой «не вижу» — приём аварийного кода закрыт"


def test_own_see_at_a_tie_does_not_open_the_code_window(c):
    acts = c.tie("see")
    acts += c.run(T_TIE + 5)
    assert _tie_notes(acts), "предпосылка: ничья при своём «вижу» не наступила"
    assert not c.s.reserve_code_window()["open"], "кандидат сам видит «Beta», а аварийный код принимается"


def test_the_tie_mark_is_kept_for_the_card(c):
    c.tie()
    c.run(T_TIE + 5)
    tie = c.s.rsv_get("rsv_qlog").get("tie") or {}
    assert tie.get("told") is True and tie.get("episode") and tie.get("side") == "cand" and tie.get("l") == LINE, tie
    assert c.s.reserve_quorum_tie_view("cand").get("since") == tie["episode"]


def test_no_tie_for_tie_clear_s_tells_the_all_clear_to_the_same_admins_and_closes_the_window(c):
    c.tie()
    c.run(T_TIE + 5)
    c.untie()
    acts = c.tick() + c.run(CLEAR - 10)
    assert not _tie_notes(acts, "Ничьей"), "отбой раньше tie_clear_s"
    acts = c.run(15)
    got = _tie_notes(acts, "Ничьей")
    assert set(got) == {config.ADMIN_ID, rc.PEER_ADMIN}, f"адресаты отбоя: {sorted(got)}"
    assert all(len(v) == 1 and v[0].force_sound for v in got.values()), "отбой — не одно громкое сообщение каждому"
    assert "остались на «Beta»" in got[config.ADMIN_ID][0].text, got[config.ADMIN_ID][0].text
    assert not c.s.reserve_code_window()["open"], "ничьей нет — окно кода осталось открытым"
    assert (c.s.rsv_get("rsv_qlog").get("tie") or {}).get("told") is False
    assert c.s.reserve_quorum_tie_view() == {}, "карточка показывает ничью после отбоя"
    assert not _tie_notes(c.run(300), "Ничьей"), "отбой повторяется"


def test_a_tie_back_within_the_minute_is_the_same_episode(c):
    c.tie()
    c.run(T_TIE + 5)
    c.untie()
    c.tick()
    c.run(CLEAR - 20)
    acts = c.tie() + c.run(T_TIE + 30)
    assert not _tie_notes(acts) and not _tie_notes(acts, "Ничьей"), "дребезг меньше минуты — новая тревога или отбой"


def test_a_new_episode_after_the_all_clear_is_told_again(c):
    c.tie()
    c.run(T_TIE + 5)
    first = c.s.rsv_get("rsv_qlog")["tie"]["episode"]
    c.untie()
    c.tick()
    c.run(CLEAR + 5)
    acts = c.tie() + c.run(T_TIE + 5)
    got = _tie_notes(acts)
    assert len(got.get(config.ADMIN_ID, [])) == 1 and len(got.get(rc.PEER_ADMIN, [])) == 1, "второй эпизод ничьей без тревоги"
    assert c.s.rsv_get("rsv_qlog")["tie"]["episode"] >= first


def test_the_admins_get_only_the_alarm_and_the_all_clear_for_one_episode(c):
    """Концепт «кворум» §3.7.5, §6.4: одно сообщение на эпизод каждому и отбой —
    тем же адресатам. Окно кода, открытое ничьей, не должно добавлять своих
    уведомлений (Т-РУ7 «голосов не хватает», «приём кода закрыт»): кнопка кода
    уже в Т-К5в, а второй текст о той же аварии противоречит первому."""
    acts = c.tie() + c.run(T_TIE + 5)
    c.untie()
    acts += c.tick() + c.run(CLEAR + 5)
    got = _by_addr(notes(acts))
    assert {k: len(v) for k, v in got.items()} == {config.ADMIN_ID: 2, rc.PEER_ADMIN: 2}, \
        {k: [n.text.split("\n")[0] for n in v] for k, v in got.items()}


def test_with_an_odd_v_there_is_never_a_tie(box, monkeypatch):
    c = _Cand(box, monkeypatch, members=["b", "a", "b1", "b2"])
    c.own("see")
    c.view(1, "uns")
    acts = c.tick() + c.run(T_TIE * 3)
    assert not _tie_notes(acts) and not c.s.rsv_get("rsv_qlog").get("tie"), "ничья при нечётном v"


def test_with_act_off_there_is_no_alarm(c):
    settings.set_value("app.reserve.quorum.act", False)
    acts = c.tie() + c.run(T_TIE + 5)
    assert not notes(acts), "act=false: кандидат шлёт тревогу ничьей"


# ── лидер ───────────────────────────────────────────────────────────────────

LMEMBERS = ["a", "b", "a1", "a2", "a3"]
W = 40


class _Lead:
    def __init__(self, svc, monkeypatch):
        self.s = svc
        self.t = 50_000.0
        self.seq = 0
        ns = types.SimpleNamespace(monotonic=lambda: self.t)
        monkeypatch.setattr(leader_mod, "time", ns)
        monkeypatch.setattr(fence_mod, "time", ns)
        monkeypatch.setattr(svc, "reserve_quorum_active", lambda: True)
        monkeypatch.setattr(svc, "reserve_quorum_off_reasons", lambda: [])
        monkeypatch.setattr(svc, "_quorum_info", lambda: Info(hs=3, flow=True, at="2026-10-30T14:07:00+03:00"))
        monkeypatch.setattr(svc, "_quorum_correlation_cached", lambda: [])
        svc.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": list(LMEMBERS)},
                       mine={"l": "a", "g": 1, "members": list(LMEMBERS)})
        self.yes = ["b", "a1"]

    def probe(self, node: str, w: str = "see") -> None:
        self.seq += 1
        p = Probe("a", 1, node, self.s.reserve_quorum_epoch(), w, self.seq)
        self.s.reserve_quorum_probe_in(p.encode(), probenet.node_addr("a", node))

    def gw_view(self, slot: int, w: str) -> None:
        self.s.reserve_quorum_link_in(slot, "qview", {"l": "a", "g": 1, "w": w, "e": self.s.reserve_quorum_epoch(),
                                                      "since": "2026-10-30T14:05:00+03:00"})

    def run(self, seconds: float, step: float = 5) -> list[tuple]:
        acts: list[tuple] = []
        for i in range(int(seconds // step)):
            self.t += step
            if i % 3 == 0:
                for n in self.yes:
                    self.probe(n)
            acts += self.s.reserve_quorum_leader_tick(self.t)
        return acts

    def start(self) -> None:
        for n in self.yes:
            self.probe(n)
        self.s.reserve_quorum_leader_tick(self.t)
        assert self.s.db.jrn_meta()["role"] == "holder", "подготовка: лидер не снял стартовое ограждение"

    def tie(self) -> list[tuple]:
        self.gw_view(2, "uns")
        self.gw_view(3, "uns")
        return self.run(5)


@pytest.fixture()
def ld(mv, monkeypatch):
    settings.set_value("app.reserve.quorum.act", True)
    w = _Lead(mv.svc, monkeypatch)
    w.start()
    return w


def test_the_leader_at_a_tie_tells_its_admin_by_name_once_after_t_tie(ld):
    acts = ld.tie() + ld.run(T_TIE - 10)
    assert not _tie_notes(acts), "Т-К5 раньше T_tie"
    acts = ld.run(15)
    got = _tie_notes(acts)
    assert list(got) == [config.ADMIN_ID] and len(got[config.ADMIN_ID]) == 1, f"Т-К5: {got}"
    n = got[config.ADMIN_ID][0]
    assert n.force_sound, "Т-К5 без звука"
    assert "«Beta»" in n.text and "«Шлюз 1»" in n.text, f"видящие не названы: {n.text}"
    assert "«Шлюз 2»" in n.text and "«Шлюз 3»" in n.text, f"не видящие не названы: {n.text}"
    assert "на связи 3 устройства, трафик идёт" in n.text, f"своя справка не в Т-К5: {n.text}"
    assert "«🔀 Переезд на «Beta»»" in n.text
    assert not _tie_notes(ld.run(600)), "Т-К5 повторяется, пока ничья держится"


def test_the_leader_tie_alarm_has_the_move_and_reserve_buttons(ld):
    """Концепт «кворум» §6.4: Т-К5 — кнопки «🔀 Переезд на «Beta»» и «🛟 Резерв»:
    админ, у которого VPN не доходит до пользователей, переводит их из тревоги."""
    from awgbot.bot import notifier
    ld.tie()
    n = _tie_notes(ld.run(T_TIE + 5))[config.ADMIN_ID][0]
    assert n.keyboard == "quorum_tie", f"Т-К5 без своей клавиатуры: keyboard={n.keyboard!r}"
    labels = [b.text for row in notifier.KEYBOARDS["quorum_tie"]().inline_keyboard for b in row]
    assert any(t.startswith("🔀 Переезд") for t in labels) and "🛟 Резерв" in labels, labels


def test_the_leader_all_clear_after_tie_clear_s_once(ld):
    ld.tie()
    ld.run(T_TIE + 5)
    ld.yes = ["b", "a1", "a2"]
    ld.gw_view(2, "see")
    acts = ld.run(CLEAR - 10)
    assert not _tie_notes(acts, "Ничьей"), "отбой Т-К6 раньше tie_clear_s"
    acts = ld.run(20)
    got = _tie_notes(acts, "Ничьей")
    assert list(got) == [config.ADMIN_ID] and len(got[config.ADMIN_ID]) == 1, got
    assert got[config.ADMIN_ID][0].force_sound
    assert "видят 3 из 4" in got[config.ADMIN_ID][0].text, got[config.ADMIN_ID][0].text
    assert not _tie_notes(ld.run(300), "Ничьей"), "Т-К6 повторяется"


def test_the_leader_tells_a_new_episode_again(ld):
    ld.tie()
    ld.run(T_TIE + 5)
    ld.yes = ["b", "a1", "a2"]
    ld.gw_view(2, "see")
    ld.run(CLEAR + 10)
    ld.yes = ["b", "a1"]
    ld.run(W + 5)                                         # «да» шлюза 2 вышло из окна
    acts = ld.tie() + ld.run(T_TIE + 5)
    assert len(_tie_notes(acts).get(config.ADMIN_ID, [])) == 1, "второй эпизод ничьей у лидера без Т-К5"


def test_a_silent_voter_is_no_tie_for_the_leader(ld):
    """«Нет» лидер знает только по копиям qview; молчащий — не «нет»."""
    ld.gw_view(2, "uns")
    acts = ld.run(T_TIE * 2)
    assert not _tie_notes(acts), "лидер счёл молчащий шлюз за «нет» и объявил ничью"


def test_the_card_shows_the_tie_line_and_the_move_dialog_the_count(ld):
    from awgbot.bot.texts.reserve.section import quorum_mine_lines
    assert "quorum" not in ld.s.reserve_move_dialog_data(), "Т-К18 у лидера, который держится"
    ld.tie()
    ld.run(T_TIE + 5)
    lines = quorum_mine_lines(ld.s.reserve_quorum_card())
    assert any("Ничья" in x and "видят 2, не видят 2" in x for x in lines), f"нет строки Т-К4: {lines}"
    assert ld.s.reserve_move_dialog_data().get("quorum") == {"y": 2, "v": 4}, "Т-К18 при ничьей"


def test_a_tie_about_the_other_server_is_not_shown_as_a_tie_about_this_one(ld):
    """Отметка ничьей одна (`rsv_qlog.tie`), а ничьи две разные: о своём сервере
    (я — лидер) и о втором (я — кандидат, `side = cand`). Ничья о «Beta» в блоке
    «📤» и в диалоге «🔀 Перевести…» — админ решит, что это его сервер не видят."""
    ld.run(10)
    ld.s.rsv_update("rsv_qlog", tie={"episode": "2026-10-30T14:05:00+03:00", "told": True, "side": "cand", "l": "b"})
    card = ld.s.reserve_quorum_card()
    assert not card["mine"].get("tie_since"), f"ничья о втором сервере в блоке своего: {card['mine']}"
    assert "quorum" not in ld.s.reserve_move_dialog_data(), "Т-К18 из-за ничьей о втором сервере"


# ── правки этапа решения: принятая эпоха и возврат правилом лидера ──────────

class _Q3(_Lead):
    def __init__(self, svc, monkeypatch):
        super().__init__(svc, monkeypatch)
        svc.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": ["a", "b", "a1", "a2"]},
                       mine={"l": "a", "g": 1, "members": ["a", "b", "a1", "a2"]})

    def probe_e(self, node: str, e: int) -> None:
        self.seq += 1
        self.s.reserve_quorum_probe_in(Probe("a", 1, node, e, "see", self.seq).encode(), probenet.node_addr("a", node))

    def meta(self):
        m = self.s.db.jrn_meta()
        return m["holder"], m["role"], int(m["epoch"])


@pytest.fixture()
def q3(mv, monkeypatch):
    settings.set_value("app.reserve.quorum.act", True)
    w = _Q3(mv.svc, monkeypatch)
    w.start()
    return w


def test_an_adopted_epoch_with_the_majority_back_comes_home_with_a_new_epoch(q3, monkeypatch):
    """Основу не видят — она лишена кворума; кандидат забирал (qtake{e} →
    голосующие под e + 1), основа приняла эпоху по пробе, а открытие не
    состоялось. Большинство «да» снова здесь — само по себе не доказательство
    (это обычное Q2 после переезда): запись возвращается только по факту живой
    второй стороны, сказавшей после принятия, что моих пользователей у неё нет;
    до него основа следует (один писатель)."""
    from awgbot.util import timeutil
    e = q3.s.reserve_quorum_epoch()
    q3.t += W + 1                                         # «да» вышли из окна
    q3.s.reserve_quorum_leader_tick(q3.t)
    q3.t += 20                                            # и так — T_down
    q3.s.reserve_quorum_leader_tick(q3.t)
    assert q3.meta() == ("self", "follower", e), f"предпосылка: лидер не лишён кворума: {q3.meta()}"
    q3.probe_e("a1", e + 1)
    assert q3.meta() == ("peer", "follower", e + 1), "предпосылка: эпоха по пробе не принята"
    q3.s.reserve_epoch_check()
    assert q3.meta()[0] == "peer", "страховка такта сняла принятую эпоху"
    assert q3.s.reserve_quorum_adopted(), "отметка принятия не на диске"
    for _ in range(40):                                   # «да» под e + 1 дольше окна U — без слова второй стороны
        q3.t += 5
        q3.probe_e("b", e + 1)
        q3.probe_e("a1", e + 1)
        q3.s.reserve_quorum_leader_tick(q3.t)
    assert q3.meta()[0] == "peer", f"большинство «да» вернуло запись без слова второй стороны: {q3.meta()}"
    # вторая сторона жива и после принятия говорит, что моих пользователей у неё нет
    monkeypatch.setattr(q3.s, "reserve_peer_seen", lambda: "ok")
    q3.s.rsv_update("rsv_peer_state", session=True, inn="idle", st_stale=False,
                    at=timeutil.to_iso(timeutil.shift(timeutil.now(), 3600)))
    for _ in range(40):
        q3.t += 5
        q3.probe_e("b", e + 1)
        q3.probe_e("a1", e + 1)
        q3.s.reserve_quorum_leader_tick(q3.t)
        if q3.meta()[0] == "self":
            break
    assert q3.meta() == ("self", "holder", e + 2), f"возврат по факту второй стороны не дал эпоху здесь + 1: {q3.meta()}"
    assert not q3.s.reserve_quorum_adopted(), "отметка принятия не снята"


def test_a_holder_peer_not_adopted_by_the_quorum_is_not_taken_back(q3):
    """Держатель `peer` по возврату кнопкой (`released`) — не принятая по кворуму
    эпоха: «да» его к записи не возвращают (стенд quo-4, сценарий з)."""
    e = q3.s.reserve_quorum_epoch()
    q3.s.db.jrn_adopt(e + 1, why="тест: возврат кнопкой")
    for _ in range(40):
        q3.t += 5
        q3.probe_e("b", e + 1)
        q3.probe_e("a1", e + 1)
        q3.s.reserve_quorum_leader_tick(q3.t)
    assert q3.meta() == ("peer", "follower", e + 1), f"«да» вернули к записи держателя peer без кворума: {q3.meta()}"


# ── принятая эпоха дома — переезд мимо меня ─────────────────────────────────

def _adopt_at_home(q3) -> int:
    e = q3.s.reserve_quorum_epoch()
    q3.t += W + 1
    q3.s.reserve_quorum_leader_tick(q3.t)
    q3.t += 20
    q3.s.reserve_quorum_leader_tick(q3.t)
    q3.probe_e("a1", e + 1)
    assert q3.meta() == ("peer", "follower", e + 1), "предпосылка: эпоха по пробе не принята"
    assert q3.s.reserve_out() == "home", "предпосылка: основа не дома"
    return e


def test_an_adopted_epoch_at_home_without_the_peers_word_moves_me_to_away(q3, monkeypatch):
    """Кандидат забрал (голосующие под e + 1), основа приняла эпоху, а второй сервер
    молчит: умер сразу после взятия или основа лежала (отметка принятия на диске).
    Дома с держателем `peer` основа следовала бы бессрочно — Q1 о своём боте на
    резерве и «⚠️ Вернуть без …» есть только в away: такт переводит в away тем же
    правилом, что `in = serving`."""
    _adopt_at_home(q3)
    monkeypatch.setattr(q3.s, "reserve_peer_seen", lambda: "down")
    q3.s.reserve_out_tick()
    assert q3.s.reserve_out() == "away", "принятая эпоха без слова второй стороны оставила основу дома"
    assert q3.meta()[0] == "peer", "переход в away тронул держателя"
    assert q3.s.reserve_quorum_adopted(), "отметка принятия снята переходом"
    assert q3.s.reserve_quorum_leader_host() != "a", "в away лидер линии — бот основы на резерве (Q1 о нём)"


def test_an_adopted_epoch_with_the_live_peer_idle_after_it_stays_home(q3, monkeypatch):
    """Вторая сторона жива и после принятия говорит, что моих у неё нет (открытие
    не состоялось): эпоху вернёт правило лидера за окно U — переезда нет."""
    from awgbot.util import timeutil
    _adopt_at_home(q3)
    monkeypatch.setattr(q3.s, "reserve_peer_seen", lambda: "ok")
    q3.s.rsv_update("rsv_peer_state", session=True, inn="idle", st_stale=False,
                    at=timeutil.to_iso(timeutil.shift(timeutil.now(), 3600)))
    q3.s.reserve_out_tick()
    assert q3.s.reserve_out() == "home", "живая вторая сторона без моих пользователей — а основа ушла в away"


def test_an_adopted_epoch_is_a_fact_for_the_automaton_only_under_the_quorum(q3, monkeypatch):
    _adopt_at_home(q3)
    monkeypatch.setattr(q3.s, "reserve_peer_seen", lambda: "down")
    monkeypatch.setattr(q3.s, "_quorum_rules_home", lambda: False)
    q3.s.reserve_out_tick()
    assert q3.s.reserve_out() == "home", "без решений по кворуму принятая эпоха повела автомат"
