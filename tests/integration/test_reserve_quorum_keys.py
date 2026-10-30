"""Ключи вокруг `M` на стороне основы и второго сервера (quorum/keys.py, keys.py,
outbound_return.py, gwside.py, codewin.py): прямой «2 из n» самого `M` нет;
с сопряжения — прямая аварийного ключа `Mc` и ящик кода (код админу), `qkeys`
без многочлена; включение кворума (шлюз `quo1` в составе) — только многочлен
большинства и ящик кворума, `M`, `K`, пакет, ящик кода и код прежние, Т-К12 без
звука и без слов о коде, Т-К12б; смена порога — только многочлен; снятие шлюза —
`M`, `K` и многочлен новые, код прежний; выключение — многочлен и ящик кворума
прочь, код прежний; возврат после переезда по точкам — код прежний, по коду —
новый; `qkeys`/`qkeys_ok`; точка шлюзу разделом `quorum`; подтверждение ключей
по `qkeys_ok` и полю `quo.q` снимков шлюзов; вход процесса открытия.

Ключи и пакеты — настоящие (util/rsvkeys, util/rsvpkg/seal, tools/rsvopen),
хост и сеть не трогаются.

Цена ошибки: код, сменившийся при включении голосования или при снятии шлюза, —
админ основы держит в сейфе код, который больше не действует, и узнаёт об этом в
аварию; ключ кворума, не собирающийся из точек резерва и шлюзов, — кворум решил
переезд, а пакет не открыть; ящик или точка чужой длины, принятые резервом, —
открытие падает уже в аварии; кворум, объявленный включённым до того, как точки
дошли до всех, — голоса есть, а ключа под них нет; многочлен при одном голосующем
— порог 1, и единственная точка второго сервера открывает `M` в одиночку.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import section
from awgbot.domain import gwcfg
from awgbot.domain.gwcfg import check as gwcheck, prints as gwprints
from awgbot.domain.notices import reserve as rn
from awgbot.util import pairproto, rsvkeys, shamir, timeutil, wgkeys
from awgbot.util.rsvpkg import seal
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_quorum import PEER_ADMIN, line, notes, setup_line
from tests.integration._reserve_vote_common import Cand, box, code_window_outside
from tools import rsvopen

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["line", "box", "mv"]


def _keys(s) -> dict:
    return dict(s.reserve_keys())


def _line_code(s) -> str:
    """Код — точка x = 255 прямой аварийного ключа `Mc` из хранилища основы."""
    q = s.reserve_qkeys()
    return rsvkeys.code_encode(shamir.Line(bytes.fromhex(q["mc"]), bytes.fromhex(q["ma"])).point(rsvkeys.CODE_X)[1])


def _code_opens(s, code: str, body: dict, blob: bytes) -> bytes:
    """`M` из кода, точки второго сервера и ящика кода тела qkeys (процесс открытия)."""
    m, _k = rsvopen.recover_keys({"code": code, "cpoint": [body["x"], body["cy"]], "cbox": body["cbox"]}, blob)
    return m


def _blob(s) -> bytes:
    """Пакет переезда текущего поколения: заголовок под `M`, части под `K`."""
    k = _keys(s)
    return seal.seal({"vpn": {"peers": []}}, k=bytes.fromhex(k["k"]), m=bytes.fromhex(k["m"]), pkg_gen=1,
                     m_gen=int(k["m_gen"]))


def _qkeys(s) -> dict:
    """Тело qkeys «Beta» прямо сейчас — то, что у неё лежит (часы повтора и
    подтверждение не мешают собрать его снова)."""
    s.__dict__.pop("_qkeys_sent", None)
    acked = dict(s.reserve_qkeys().get("acked") or {})
    s.rsv_update("rsv_qkeys", acked={})
    body = s.reserve_qkeys_body()
    s.rsv_update("rsv_qkeys", acked=acked)
    s.__dict__.pop("_qkeys_sent", None)
    assert body is not None, "предпосылка: qkeys не собрался"
    return body


@pytest.fixture()
def on(line):
    """Кворум «Alpha» включён: шлюз 1 с quo1, «Beta» и шлюз приняли ключи."""
    line.add_gw(1)
    line.enable()
    line.s.reserve_code_notify_clear()
    return line


# ── включение ───────────────────────────────────────────────────────────────

def test_enabling_makes_only_the_polynomial_and_keeps_m_k_the_code_line_and_the_code(line):
    """Шлюзы вошли в состав: многочлен под порог состава и ящик кворума; `M`, `K`,
    прямая кода и сам код — прежние байт в байт, нового пакета и нового кода админу нет
    (код в сейфе админа с сопряжения продолжает действовать)."""
    line.add_gw(1)
    line.add_gw(2)
    k0, q0, code0 = _keys(line.s), dict(line.s.reserve_qkeys()), line.s.reserve_code()
    assert not line.s.reserve_quorum_poly_on() and q0["poly"] == [] and q0["t"] == 0, "предпосылка: многочлен до состава"
    line.s.__dict__.pop("_rsv_pkg_due_at", None)
    line.propose()
    line.ack_members()
    line.sync_keys()
    k1, q1 = _keys(line.s), line.s.reserve_qkeys()
    m = line.s.reserve_quorum_proposed()
    assert line.s.reserve_quorum_poly_on() and (q1["q_gen"], q1["t"], len(q1["poly"])) == (1, m.t("a"), m.t("a")), \
        f"многочлен не под порог состава: {q1}"
    for f in ("m", "m_gen", "k", "k_gen"):
        assert k1[f] == k0[f], f"включение сменило {f}: {k0[f]} → {k1[f]}"
    assert (q1["c_gen"], q1["mc"], q1["ma"]) == (q0["c_gen"], q0["mc"], q0["ma"]), "включение сменило прямую кода"
    assert line.s.reserve_code() == code0 == _line_code(line.s), "включение сменило аварийный код"
    assert line.s.reserve_code_notify_pending() == "", "новый код админу при включении голосования"
    assert line.s.__dict__.get("_rsv_pkg_due_at") is None, "включение заказало новый пакет — M и K прежние"
    body, blob = _qkeys(line.s), _blob(line.s)
    assert _code_opens(line.s, code0, body, blob).hex() == k0["m"], "код с сопряжения не открывает ящик кода после включения"
    assert body["qbox"] and body["thr"] == m.t("a"), "ящик кворума не ушёл второму серверу"


@pytest.mark.parametrize("opened", [True, False], ids=["peer_admin_opened", "peer_admin_never_opened"])
def test_enabling_tells_the_admin_silently_once_without_a_word_about_the_code(line, opened):
    line.add_gw(1, name="Дача")
    line.s.rsv_update("rsv_peer_admin", opened=opened)
    line.propose()
    line.ack_members()
    told = notes(line.sync_keys())
    mine = [n for n in told if n.tg_id == rc.ADMIN]
    assert len(mine) == 1 and mine[0].silent and not mine[0].force_sound, f"Т-К12 админу основы — один раз и без звука: {told}"
    assert "«Beta»" in mine[0].text and "«Дача»" in mine[0].text, mine[0].text
    assert "код" not in mine[0].text.lower(), f"Т-К12 говорит о коде, а код прежний: {mine[0].text}"
    peer = [n for n in told if n.tg_id == PEER_ADMIN]
    if opened:
        assert len(peer) == 1 and peer[0].silent and "«Alpha»" in peer[0].text, f"Т-К12б: {told}"
    else:
        assert not peer, "Т-К12б админу второго сервера, который этот бот не открывал"
    assert not [n for n in told if n.text.rstrip().endswith(".")], "уведомление с точкой в конце"
    m_gen = int(_keys(line.s)["m_gen"])
    assert notes(line.sync_keys()) == [], "Т-К12 пришло второй раз"
    assert int(_keys(line.s)["m_gen"]) == m_gen, "повтор такта сменил поколение M ещё раз"


def test_voting_is_not_announced_before_the_second_server_confirmed_the_line(line):
    """Концепт §2.2: кворум включён, только когда поколение состава подтверждено
    вторым сервером; «Beta» не ответила qmem_ok ни разу — Т-К12 «голосование
    включено» и смена кода были бы неправдой: голосования нет и, пока «Beta»
    молчит, не будет."""
    line.add_gw(1)
    line.propose()
    acts = line.sync_keys()
    assert not line.s.reserve_quorum_poly_on(), "многочлен создан до подтверждения состава"
    assert not notes(acts), f"Т-К12 до qmem_ok: {[n.text for n in notes(acts)]}"


def test_the_code_from_pairing_and_the_quorum_points_both_open_the_package_after_enabling(line):
    """После включения два пути к `M`: код с сопряжения (ящик кода) и точки большинства
    (ящик кворума); прежних входов прямой самого `M` нет."""
    line.add_gw(1)
    code = line.s.reserve_code()
    line.propose()
    line.ack_members()
    line.sync_keys()
    body, blob = _qkeys(line.s), _blob(line.s)
    assert _code_opens(line.s, code, body, blob).hex() == _keys(line.s)["m"], "код с сопряжения не открыл пакет"
    point = line.s.gwcfg_quorum(line.s.db.gateways()[0])["point"]
    m, _k = rsvopen.recover_keys({"qpoints": [[body["x"], body["y"]], [point["x"], point["y"]]], "qbox": body["qbox"]}, blob)
    assert m.hex() == _keys(line.s)["m"], "точки резерва и шлюза не открыли пакет"


def test_a_gateway_without_quo1_makes_no_polynomial_and_the_keys_stay(line):
    """Шлюз без `quo1` в состав не входит: многочлена нет — порог при одном голосующем
    был бы 1 (единственная точка второго сервера открыла бы `M`); `M` и код прежние,
    `qkeys` уходит без многочлена (ящик кода)."""
    line.add_gw(1, quo=False)
    before, code = _keys(line.s), line.s.reserve_code()
    line.propose()
    assert not line.s.reserve_quorum_proposed().gateways, "предпосылка: шлюз без quo1 в составе"
    for _ in range(3):
        line.s.quorum_tick()
        line.sync_keys()
    assert not line.s.reserve_quorum_poly_on() and int(_keys(line.s)["m_gen"]) == int(before["m_gen"])
    assert line.s.reserve_code() == code, "код сменился без многочлена"
    body = _qkeys(line.s)
    assert "qbox" not in body and body["cbox"], f"qkeys без ящика кода или с многочленом: {sorted(body)}"
def test_with_only_the_second_server_voting_one_point_cannot_open(line):
    """Шлюзов с quo1 нет — голосует только второй сервер (v = 1): у «Beta»
    единственная точка многочлена ключа кворума, а порог не ниже 2 — открыть
    пакет одной своей точкой она не может, только по аварийному коду."""
    line.add_gw(1, quo=False)
    line.propose()
    line.ack_members()
    assert line.s.reserve_quorum_proposed().members == ("a", "b")
    line.sync_keys()
    q = line.s.reserve_qkeys()
    if q.get("on") and q.get("poly"):
        assert int(q.get("t") or 0) >= 2, f"v = 1: порог многочлена {q.get('t')} — «Beta» откроет пакет одна"
    body = line.s.reserve_qkeys_body()
    assert body is not None and body.get("cbox"), "v = 1: ящика кода у второго сервера нет — аварийный код не откроет"
    assert not line.s.reserve_quorum_decisions(), "v = 1: решения по голосам разрешены"


# ── смена состава ───────────────────────────────────────────────────────────

def test_gateways_joining_with_a_new_threshold_get_a_new_polynomial_and_keep_m_k_and_the_code(on):
    """Шлюзы 2 и 3 объявили quo1: v 2 → 4, порог 2 → 3 — новый многочлен, а
    M, K, прямая кода и сам код — прежние; нового кода админу нет."""
    k0, q0, code0 = _keys(on.s), dict(on.s.reserve_qkeys()), on.s.reserve_code()
    on.add_gw(2)
    on.add_gw(3)
    on.propose()
    on.sync_keys()
    k1, q1 = _keys(on.s), on.s.reserve_qkeys()
    assert (q1["q_gen"], q1["t"]) == (q0["q_gen"] + 1, 3), f"порог не сменился: {q1['q_gen']}, {q1['t']}"
    assert q1["poly"] != q0["poly"]
    assert (k1["m"], k1["m_gen"], k1["k"]) == (k0["m"], k0["m_gen"], k0["k"]), "подключение шлюза сменило M или K"
    assert (q1["c_gen"], q1["mc"], q1["ma"]) == (q0["c_gen"], q0["mc"], q0["ma"]) and on.s.reserve_code() == code0, \
        "подключение шлюза сменило аварийный код"
    assert on.s.reserve_code_notify_pending() == ""


def test_a_threshold_that_does_not_change_keeps_the_polynomial(on):
    on.add_gw(2)                                          # v 2 → 3, порог тот же — 2
    q0 = dict(on.s.reserve_qkeys())
    on.propose()
    on.sync_keys()
    assert on.s.reserve_qkeys()["q_gen"] == q0["q_gen"], "многочлен заново без смены порога"


def test_waiting_for_the_peer_does_not_churn_the_polynomial(on):
    """Шлюз 1 откатился ниже quo1: предложен состав без шлюзов, «Beta» ещё не
    подтвердила. Такты идут каждые 5 с — многочлен и qkeys второму серверу не
    должны рождаться заново на каждом (иначе подтверждение ключей не догонит
    никогда, а «Beta» получает qkeys каждые 60 с)."""
    on.gw_quo(1, False)
    on.propose()
    on.sync_keys()
    q1 = int(on.s.reserve_qkeys().get("q_gen") or 0)
    for _ in range(3):
        on.sync_keys()
    assert int(on.s.reserve_qkeys().get("q_gen") or 0) == q1, \
        f"многочлен рождается заново каждый такт: {q1} → {on.s.reserve_qkeys().get('q_gen')}"


def test_a_removed_gateway_gets_new_m_k_and_polynomial_but_the_code_stays(on):
    """Снятие шлюза (§2.7): у него осталась точка прежнего многочлена — M, K и
    многочлен новые; прямая кода со шлюзом не связана — код прежний, Т-РУ14 нет."""
    k0, q0, code0 = _keys(on.s), dict(on.s.reserve_qkeys()), on.s.reserve_code()
    on.s._reserve_after_gateway_change("после снятия шлюза")
    k1, q1 = _keys(on.s), on.s.reserve_qkeys()
    assert int(k1["m_gen"]) == int(k0["m_gen"]) + 1 and k1["m"] != k0["m"] and k1["k"] != k0["k"]
    assert q1["q_gen"] == q0["q_gen"] + 1 and q1["poly"] != q0["poly"], "многочлен прежний после снятия шлюза"
    assert on.s.reserve_code() == code0 and q1["c_gen"] == q0["c_gen"], "код сменился при снятии шлюза"
    assert on.s.reserve_code_notify_pending() == "", "Т-РУ14 «новый код» при снятии шлюза под кворумом"
    body, blob = _qkeys(on.s), _blob(on.s)
    assert body["m_gen"] == int(k1["m_gen"]) and body["q_gen"] == q1["q_gen"], "qkeys не про новые поколения"
    assert _code_opens(on.s, code0, body, blob).hex() == k1["m"], "ящик кода не пересобран вокруг нового M"
    pt = on.s.gwcfg_quorum(on.s.db.gateways()[0])["point"]
    m, _k = rsvopen.recover_keys({"qpoints": [[1, body["y"]], [pt["x"], pt["y"]]], "qbox": body["qbox"]}, blob)
    assert m.hex() == k1["m"], "ящик кворума не пересобран вокруг нового M"


def test_without_the_polynomial_a_removed_gateway_changes_m_but_not_the_code(line):
    """Линия без многочлена (шлюз ещё не в составе): снятие шлюза — новое `M` и `K` и
    ящик кода вокруг нового `M`; прямая кода со шлюзом не связана — код прежний, Т-РУ14 нет."""
    line.add_gw(1)
    k0, q0, code0 = _keys(line.s), dict(line.s.reserve_qkeys()), line.s.reserve_code()
    line.s._reserve_after_gateway_change("после снятия шлюза")
    k1, q1 = _keys(line.s), line.s.reserve_qkeys()
    assert int(k1["m_gen"]) == int(k0["m_gen"]) + 1 and k1["m"] != k0["m"] and k1["k"] != k0["k"]
    assert line.s.reserve_code() == code0 and q1["c_gen"] == q0["c_gen"], "код сменился при снятии шлюза"
    assert line.s.reserve_code_notify_pending() == "", "Т-РУ14 при снятии шлюза"
    assert not line.s.reserve_quorum_poly_on(), "снятие шлюза создало многочлен"
    body = _qkeys(line.s)
    assert body["m_gen"] == int(k1["m_gen"]) and "qbox" not in body
    assert _code_opens(line.s, code0, body, _blob(line.s)).hex() == k1["m"], "прежний код не открывает ящик нового M"


def test_after_a_new_m_the_second_server_gets_qkeys_again(on):
    on.s._reserve_after_gateway_change("после снятия шлюза")
    body = on.s.reserve_qkeys_body()
    assert body is not None and body["m_gen"] == int(_keys(on.s)["m_gen"]), "новое поколение M не ушло второму серверу"
    assert not on.s.reserve_quorum_keys_ok(on.s.reserve_quorum_mine()), "новые ключи сочтены подтверждёнными до qkeys_ok"
    # смена поколения линию не выключает: до подтверждения действует прежнее (§3.7.10) — ответчик и счёт идут
    assert "keys" not in on.s.reserve_quorum_off_reasons(), "смена M выключила голосование до подтверждения новых ключей"


# ── выключение ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("why", ["not_wanted", "last_gateway_rolled_back"])
def test_switching_off_drops_the_polynomial_and_the_quorum_box_and_keeps_m_and_the_code(on, why):
    """Условия кворума пропали или шлюзов `quo1` в составе не осталось: многочлен прочь,
    `qkeys` второму серверу — без части многочлена (его ящик кворума уходит); прямая кода и
    код прежние, нового кода админу и уведомлений нет; `M` и `K` — прежние, пока держатели
    точек в составе, и новые, когда последний шлюз ушёл (его точка могла остаться у чужого)."""
    k0, q0, code0 = _keys(on.s), dict(on.s.reserve_qkeys()), on.s.reserve_code()
    if why == "not_wanted":
        acts = on.s.reserve_quorum_keys_sync(on.s.reserve_quorum_proposed(), False)
    else:
        on.gw_quo(1, False)
        on.propose()
        on.ack_members()
        acts = on.sync_keys()
    q1 = on.s.reserve_qkeys()
    assert not on.s.reserve_quorum_poly_on() and q1["poly"] == [] and q1["t"] == 0, f"{why}: многочлен остался"
    assert on.s.reserve_quorum_keys_on(), f"{why}: вместе с многочленом снят ящик кода"
    k1 = _keys(on.s)
    if why == "not_wanted":                                   # шлюзы в составе остались — ключ прежний
        assert (k1["m"], k1["m_gen"], k1["k"]) == (k0["m"], k0["m_gen"], k0["k"]), f"{why}: выключение сменило M или K"
    else:                                                     # держатель точки ушёл — M и K новые
        assert k1["m"] != k0["m"] and int(k1["m_gen"]) == int(k0["m_gen"]) + 1, f"{why}: держатель точки ушёл, а M прежний"
    assert (q1["c_gen"], q1["mc"], q1["ma"]) == (q0["c_gen"], q0["mc"], q0["ma"]) and on.s.reserve_code() == code0, \
        f"{why}: выключение сменило код"
    assert on.s.reserve_code_notify_pending() == "" and not notes(acts), f"{why}: уведомление при выключении"
    on.advance(61)
    body = on.s.reserve_qkeys_body()
    assert body is not None, f"{why}: второму серверу не ушло, что ящика кворума больше нет"
    assert not {"q_gen", "t", "y", "qbox"} & set(body), f"{why}: qkeys после выключения несёт многочлен: {sorted(body)}"
    pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.INVITER)


def test_after_switching_off_the_ack_without_a_polynomial_confirms_the_keys(on):
    """Подтверждение `qkeys` без многочлена (q_gen 0) закрывает ключи: повтора нет, ключи
    на месте у второго сервера — иначе qkeys шёл бы раз в минуту вечно."""
    on.s.reserve_quorum_keys_sync(on.s.reserve_quorum_proposed(), False)
    on.s.reserve_qkeys_ok_in({"l": "a", "m_gen": int(_keys(on.s)["m_gen"]), "q_gen": 0, "c_gen": on.gens()["c_gen"]})
    on.advance(120)
    assert on.s.reserve_qkeys_body() is None, "после qkeys_ok без многочлена ключи шлются снова"


def test_the_second_server_drops_the_quorum_box_when_qkeys_comes_without_a_polynomial(line):
    """У второго сервера ящик кворума и точка многочлена уходят по `qkeys` без части
    многочлена; ящик кода остаётся — переезд по коду возможен и после выключения."""
    line.s.reserve_qkeys_in(_peer_body())
    assert line.s.reserve_quorum_peer_keys()["qbox"], "предпосылка: ящик кворума принят"
    bare = {k: v for k, v in _peer_body().items() if k not in ("q_gen", "t", "y", "qbox")}
    acts = line.s.reserve_qkeys_in(bare)
    assert acts and acts[0][2]["q_gen"] == 0, f"подтверждение без многочлена не с q_gen 0: {acts}"
    peer = line.s.reserve_quorum_peer_keys()
    assert (peer["q_gen"], peer["t"], peer["y"], peer["qbox"]) == (0, 0, "", ""), f"ящик кворума у второго сервера остался: {peer}"
    assert peer["cbox"] == bare["cbox"] and peer["cy"] == bare["cy"], "вместе с ящиком кворума ушёл ящик кода"
def test_the_last_gateway_rolled_back_takes_its_old_point_out_of_the_game(on):
    """Переход v 2 → 1 (шлюз откатился, `quo1` нет): у шлюза осталась точка
    прежнего многочлена, у «Beta» — своя; при прежнем пороге 2 их двух хватает
    на M. Поэтому многочлен прежнего состава и ящик кворума у второго сервера
    уходят, M и K — новые, а код (прямая кода со шлюзом не связана) — прежний."""
    k0, q0, code0 = _keys(on.s), dict(on.s.reserve_qkeys()), on.s.reserve_code()
    on.gw_quo(1, False)
    on.propose()
    on.ack_members()
    on.sync_keys()
    k1, q1 = _keys(on.s), on.s.reserve_qkeys()
    assert k1["m"] != k0["m"] and int(k1["m_gen"]) == int(k0["m_gen"]) + 1, \
        "v 2 → 1: M прежний — точка откатившегося шлюза и точка резерва открывают пакет"
    assert not q1.get("poly") or q1.get("poly") != q0.get("poly"), "v 2 → 1: многочлен прежнего состава остался"
    assert on.s.reserve_code() == code0, "v 2 → 1: код сменился, хотя прямая кода со шлюзом не связана"


# ── возврат после переезда ──────────────────────────────────────────────────

@pytest.fixture()
def qaway(mv, make_active_client, monkeypatch):
    """Кворум «Alpha» включён, пользователи возвращаются с «Beta» (шаг relay)."""
    w = setup_line(mv.svc, monkeypatch, make_active_client)
    mv.rsv.gw = False
    w.add_gw(1)
    w.enable()
    mv.svc.reserve_code_notify_clear()
    mv.svc.reserve_set_out("returning", kind="dead", gen=7, return_started_at=timeutil.now_iso())
    mv.svc._out_step("relay", 300)
    return w


@pytest.mark.parametrize("in_by, kind, code_changes", [
    ("share", "dead", False), ("", "dead", False), ("code", "dead", True), ("", "code", True)],
    ids=["by_points", "by_points_no_word", "by_code_word", "by_code_kind"])
def test_the_return_after_a_crash_move_changes_the_code_only_after_a_move_by_code(qaway, in_by, kind, code_changes):
    s = qaway.s
    k0, q0, code0 = _keys(s), dict(s.reserve_qkeys()), s.reserve_code()
    s.rsv_update("rsv_out", kind=kind, go_sent=False, record_fact_at=timeutil.now_iso())
    s.rsv_update("rsv_peer_state", in_by=in_by)
    s.reserve_wiped_in()
    k1, q1 = _keys(s), s.reserve_qkeys()
    assert int(k1["m_gen"]) == int(k0["m_gen"]) + 1 and k1["k"] != k0["k"], "после переезда без моего K — прежнее M"
    assert q1["q_gen"] == q0["q_gen"] + 1, "резерв знал Mq — многочлен прежний"
    body, blob = _qkeys(s), _blob(s)
    assert body["m_gen"] == int(k1["m_gen"]), "второму серверу не ушёл ящик кода нового M"
    if code_changes:
        assert q1["c_gen"] == q0["c_gen"] + 1 and q1["mc"] != q0["mc"] and s.reserve_code() != code0, \
            "после переезда по коду прямая кода и код прежние — резерв знает Mc"
        assert s.reserve_code_notify_pending(), "новый код после переезда по коду админу не поставлен"
        with pytest.raises(rsvopen.OpenError):
            _code_opens(s, code0, body, blob)                 # код, который видел резерв, больше не действует
        assert _code_opens(s, s.reserve_code(), body, blob).hex() == k1["m"], "новый код не открывает ящик"
    else:
        assert (q1["c_gen"], q1["mc"]) == (q0["c_gen"], q0["mc"]) and s.reserve_code() == code0, \
            "после переезда по точкам код сменился"
        assert s.reserve_code_notify_pending() == "", "Т-РУ14 после переезда по точкам"
        assert _code_opens(s, code0, body, blob).hex() == k1["m"], "прежний код не открывает ящик нового M"


def test_the_return_after_a_move_by_my_k_keeps_every_key(qaway):
    s = qaway.s
    k0, q0 = _keys(s), dict(s.reserve_qkeys())
    s.rsv_update("rsv_out", kind="button", go_sent=True, record_fact_at=timeutil.now_iso())
    s.reserve_wiped_in()
    assert int(_keys(s)["m_gen"]) == int(k0["m_gen"]) and s.reserve_qkeys()["q_gen"] == q0["q_gen"]


# ── qkeys / qkeys_ok ────────────────────────────────────────────────────────

def test_qkeys_carries_the_reserve_points_and_both_boxes_of_the_current_generations(on):
    body = _qkeys(on.s)
    q, k = on.s.reserve_qkeys(), _keys(on.s)
    assert (body["l"], body["x"], body["m_gen"], body["q_gen"], body["c_gen"], body["thr"]) == \
        ("a", 1, int(k["m_gen"]), q["q_gen"], q["c_gen"], q["t"]), body
    poly = shamir.Poly(tuple(bytes.fromhex(c) for c in q["poly"]))
    assert body["y"] == poly.point(1)[1].hex(), "в qkeys не точка резерва многочлена"
    assert body["cy"] == shamir.Line(bytes.fromhex(q["mc"]), bytes.fromhex(q["ma"])).point(1)[1].hex()
    for box_ in ("qbox", "cbox"):
        assert len(bytes.fromhex(body[box_])) == rsvkeys.BOX_LEN == 56, f"{box_} не той длины"
    assert body["y"] != q["poly"][0] and q["mc"] not in repr(body) and k["m"] not in repr(body), \
        "в qkeys ушёл сам секрет (Mq, Mc или M)"


def test_qkeys_is_repeated_not_more_often_than_every_60_seconds_and_stops_after_qkeys_ok(line):
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    assert line.s.reserve_qkeys_body() is not None
    line.advance(59)
    assert line.s.reserve_qkeys_body() is None, "qkeys повторился раньше 60 с"
    line.advance(1)
    assert line.s.reserve_qkeys_body() is not None, "без ответа qkeys не повторился через 60 с"
    line.ack_keys()
    line.advance(120)
    assert line.s.reserve_qkeys_body() is None, "после qkeys_ok ключи шлются снова"


def test_a_new_pair_session_sends_unconfirmed_qkeys_at_once(line):
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    assert line.s.reserve_qkeys_body() is not None and line.s.reserve_qkeys_body() is None
    line.s.reserve_session_opened({})
    assert line.s.reserve_qkeys_body() is not None, "после новой сессии пары qkeys ждут 60 с"


def test_qkeys_goes_from_pairing_without_a_polynomial_and_stops_after_qkeys_ok(line):
    """Сразу после сопряжения (шлюзов в составе нет): `qkeys` несёт точку прямой кода
    и ящик кода, без части многочлена, проходит схему канала; `qkeys_ok` без многочлена
    закрывает ключи — без него переезд по коду невозможен с первого дня пары."""
    acts = line.s.quorum_tick()
    sent = [a[2] for a in acts if a[:2] == ("send", "qkeys")]
    assert len(sent) == 1, f"qkeys после сопряжения не ушёл тактом: {[a[:2] for a in acts]}"
    body = sent[0]
    assert set(body) == {"l", "m_gen", "x", "c_gen", "cy", "cbox"}, f"qkeys без многочлена: {sorted(body)}"
    pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.INVITER)
    assert _code_opens(line.s, line.s.reserve_code(), body, _blob(line.s)).hex() == _keys(line.s)["m"], \
        "код с сопряжения не открывает ящик кода"
    m = line.s.reserve_quorum_proposed()
    assert not line.s.reserve_quorum_keys_ok(m), "ключи на месте до qkeys_ok"
    line.s.reserve_qkeys_ok_in({"l": "a", "m_gen": body["m_gen"], "q_gen": 0, "c_gen": body["c_gen"]})
    assert line.s.reserve_quorum_keys_ok(m), "qkeys_ok без многочлена не подтвердил ключи линии без шлюзов"
    line.advance(120)
    assert not [a for a in line.s.quorum_tick() if a[:2] == ("send", "qkeys")], "после qkeys_ok ключи шлются снова"


def test_a_qkeys_ok_of_older_m_does_not_confirm_the_code_box(line):
    """Подтверждение ящика кода прежнего поколения `M` (снятие шлюза до ответа) не
    закрывает ключи: у второго сервера ящик старого `M` — код нового пакета не откроет."""
    body = _qkeys(line.s)
    line.add_gw(1)
    line.s._reserve_after_gateway_change("после снятия шлюза")
    line.s.reserve_qkeys_ok_in({"l": "a", "m_gen": body["m_gen"], "q_gen": 0, "c_gen": body["c_gen"]})
    assert line.s.reserve_qkeys_body() is not None or line.s.reserve_qkeys().get("acked", {}).get("m_gen") != \
        int(_keys(line.s)["m_gen"]), "подтверждение старого M сочтено подтверждением нового"
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())


@pytest.mark.parametrize("body", [{"l": "b", "m_gen": 9, "q_gen": 9, "c_gen": 9}, {"m_gen": 2, "q_gen": 1, "c_gen": 1}],
                         ids=["other_line", "no_line"])
def test_a_qkeys_ok_of_another_line_does_not_confirm_my_keys(line, body):
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    line.s.reserve_qkeys_ok_in(body)
    assert line.s.reserve_qkeys().get("acked") == {}, "подтверждение чужой линии записано как моё"


def _peer_body(**over) -> dict:
    """qkeys линии «Beta» (`b`) — как его собрала бы она сама."""
    m = rsvkeys.new_master()
    qg, cg = rsvkeys.QuorumGen.new(4, 2), rsvkeys.CodeGen.new(2)
    body = {"l": "b", "m_gen": 3, "q_gen": 4, "thr": 2, "x": 1, "y": qg.reserve_point()[1].hex(), "qbox": qg.qbox(m).hex(),
            "c_gen": 2, "cy": cg.reserve_point()[1].hex(), "cbox": cg.cbox(m).hex()}
    body.update(over)
    return body


def test_the_second_server_keeps_the_points_and_boxes_and_answers_qkeys_ok(line):
    body = _peer_body()
    acts = line.s.reserve_qkeys_in(body)
    assert acts == [("send", "qkeys_ok", {"l": "b", "m_gen": 3, "q_gen": 4, "c_gen": 2})], acts
    peer = line.s.reserve_quorum_peer_keys()
    assert {("thr" if k == "t" else k): peer[k] for k in peer if k in body or k == "t"} == body, \
        "точки и ящики второй линии легли не как пришли (порог thr хранится как t)"
    mine = line.s.reserve_qkeys()
    assert not line.s.reserve_quorum_poly_on() and mine["poly"] == [], "приём чужих ключей создал мой многочлен"


def test_the_second_server_takes_qkeys_without_a_polynomial(line):
    """`qkeys` линии без шлюзов в составе: только прямая кода и ящик кода — принят,
    подтверждён с q_gen 0, часть многочлена у второго сервера пуста."""
    body = {k: v for k, v in _peer_body().items() if k not in ("q_gen", "t", "y", "qbox")}
    pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.ACCEPTOR)
    acts = line.s.reserve_qkeys_in(body)
    assert acts == [("send", "qkeys_ok", {"l": "b", "m_gen": 3, "q_gen": 0, "c_gen": 2})], acts
    peer = line.s.reserve_quorum_peer_keys()
    assert (peer["cbox"], peer["cy"], peer["m_gen"]) == (body["cbox"], body["cy"], 3)
    assert (peer["q_gen"], peer["t"], peer["y"], peer["qbox"]) == (0, 0, "", "")


@pytest.mark.parametrize("over", [
    {"x": 2}, {"x": 255}, {"qbox": "ab" * 55}, {"cbox": "ab" * 57}, {"y": "ab" * 15}, {"cy": "ab" * 17},
    {"y": "zz" * 16}, {"qbox": ""}, {"l": "a"}, {"l": "c"}, {"cbox": ""}, {"cy": ""}],
    ids=["gateway_point", "code_point", "short_qbox", "long_cbox", "short_y", "long_cy", "y_not_hex", "no_qbox",
         "my_own_line", "bad_line", "no_code_box", "no_code_point"])
def test_a_wrong_qkeys_is_refused_and_not_kept(line, over):
    acts = line.s.reserve_qkeys_in(_peer_body(**over))
    assert acts == [], f"{over}: на негодные ключи ушло подтверждение"
    assert not line.s.reserve_quorum_peer_keys(), f"{over}: негодные ключи сохранены рядом с пакетом"


def test_the_pair_channel_hands_qkeys_and_qkeys_ok_to_the_keys(line):
    assert line.s.reserve_quorum_pair_in("qkeys", _peer_body())[0][1] == "qkeys_ok"
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    line.s.reserve_quorum_pair_in("qkeys_ok", {"l": "a", **line.gens()})
    assert line.s.reserve_qkeys()["acked"] == line.gens()


def test_qkeys_ok_carries_m_gen_through_the_pair_protocol():
    """Без m_gen в словаре канала подтверждение нового поколения M неотличимо
    от прежнего — основа сочла бы ключи на месте, а у «Beta» ящики старого M."""
    from awgbot.util import pairproto
    assert "m_gen" in pairproto.PAIR_KINDS["qkeys_ok"][1]


# ── подтверждение ключей и причины ──────────────────────────────────────────

def test_the_quorum_is_on_only_when_the_peer_and_every_gateway_hold_the_current_keys(line):
    line.add_gw(1)
    line.add_gw(2)
    line.propose()
    line.ack_members()
    line.sync_keys()
    assert line.s.reserve_quorum_off_reasons() == ["keys"], line.s.reserve_quorum_off_reasons()
    assert line.s.reserve_quorum_wanted() and not line.s.reserve_quorum_active()
    line.ack_keys()
    assert "keys" in line.s.reserve_quorum_off_reasons(), "кворум включён, пока точки шлюзов не дошли"
    line.gateways_applied(1)
    assert "keys" in line.s.reserve_quorum_off_reasons(), "кворум включён без точки шлюза 2"
    line.snaps[2] = {"quo": {"w": "see", "g": 1, "e": 0, "q": line.gens()["q_gen"] - 1}}
    assert "keys" in line.s.reserve_quorum_off_reasons(), "точка прежнего многочлена у шлюза 2 сочтена текущей"
    line.gateways_applied(1, 2)
    assert line.s.reserve_quorum_active(), line.s.reserve_quorum_off_reasons()
    card = "\n".join(section.quorum_mine_lines(line.s.reserve_quorum_card()))
    assert "не включено" not in card, card


def test_an_ack_of_older_generations_does_not_confirm_the_keys(on):
    on.s._reserve_after_gateway_change("после снятия шлюза")
    old = on.gens()
    on.s.reserve_qkeys_ok_in({"l": "a", **{**old, "m_gen": old["m_gen"] - 1}})
    on.gateways_applied()
    assert not on.s.reserve_quorum_keys_ok(on.s.reserve_quorum_mine()), "подтверждение прежнего M сочтено подтверждением нового"
    assert on.s.reserve_qkeys_body() is not None, "qkeys нового M не уходят после подтверждения прежнего"


def test_the_card_says_the_keys_are_not_everywhere_yet(line):
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    text = "\n".join(section.quorum_mine_lines(line.s.reserve_quorum_card()))
    assert "Голосование не включено: ключи голосования ещё не у всех" in text, text


# ── документ шлюза ──────────────────────────────────────────────────────────

def _links(s, *slots: int) -> None:
    """Стороны зоны слотов уже получены от резерва (как после slots_ok)."""
    links = {}
    for slot in slots:
        priv, _ = wgkeys.keypair()
        _, zpub = wgkeys.keypair()
        links[str(slot)] = {"priv": priv, "psk": wgkeys.psk(),
                            "zone": {"pub": zpub, "port": 45000 + slot, "host": rc.HOST_A, "cidr": f"10.99.97.{4 * slot}/30",
                                     "chan": 0, "obf": {}}}
    s.rsv_put("rsv_gw_links", links)


def test_the_reserve_section_never_carries_a_share_of_m(line):
    """Прямой самого `M` нет: ни до включения кворума, ни после в разделе reserve
    документа шлюза доли нет — с ней шлюз и резерв открыли бы `M` мимо большинства."""
    line.add_gw(1)
    _links(line.s, 1)
    gw = line.s.db.gateways()[0]
    for stage in ("до включения", "после включения"):
        if stage == "после включения":
            line.enable()
        sec = line.s.gwcfg_reserve(gw)
        assert sec is not None and "share" not in sec, f"{stage}: в разделе reserve доля прямой M: {sec}"
        gwcfg.check_reserve(gwcfg.parse({"gen": 1, "hash": "ab" * 8, "reserve": sec})["sections"]["reserve"])


def test_the_quorum_section_carries_the_gateways_point_and_passes_its_check(on):
    gw = on.s.db.gateways()[0]
    sec = on.s.gwcfg_quorum(gw)
    q = on.s.reserve_qkeys()
    poly = shamir.Poly(tuple(bytes.fromhex(c) for c in q["poly"]))
    assert sec["point"] == {"x": 2, "y": poly.point(2)[1].hex(), "q_gen": q["q_gen"]}, sec["point"]
    checked = gwcheck.check_quorum(gwcfg.parse({"gen": 1, "hash": "ab" * 8, "quorum": sec})["sections"]["quorum"])
    assert checked["point"] == sec["point"], "шлюз отбросил свою точку при проверке раздела"
    before = gwprints.quorum_print(sec)
    on.s._reserve_after_gateway_change("после снятия шлюза")
    assert gwprints.quorum_print(on.s.gwcfg_quorum(gw)) != before, "новый многочлен не меняет отпечаток раздела — шлюз не применит"


def test_no_point_in_the_quorum_section_without_the_polynomial(line):
    line.add_gw(1)
    line.propose()
    sec = line.s.gwcfg_quorum(line.s.db.gateways()[0])
    assert sec is not None and sec["point"] == {}, sec


def test_a_gateway_without_quo1_gets_no_quorum_section(on):
    on.add_gw(2, quo=False)
    on.propose()
    assert on.s.gwcfg_quorum(on.s.db.gateways()[1]) is None, "агенту 4.1.x — раздел quorum (и точка)"


# ── открытие пакета ─────────────────────────────────────────────────────────

def test_the_reserve_point_and_a_gateway_point_open_the_package(on):
    """Сквозной путь: точка резерва из qkeys и точка шлюза из раздела quorum
    того же многочлена + ящик кворума → M, который открывает K пакета."""
    body, blob = _qkeys(on.s), _blob(on.s)
    point = on.s.gwcfg_quorum(on.s.db.gateways()[0])["point"]
    assert point["q_gen"] == body["q_gen"]
    m, k = rsvopen.recover_keys({"qpoints": [[body["x"], body["y"]], [point["x"], point["y"]]], "qbox": body["qbox"]}, blob)
    assert (m.hex(), k.hex()) == (_keys(on.s)["m"], _keys(on.s)["k"])


def test_with_threshold_3_two_points_do_not_open_and_three_do(on):
    on.add_gw(2)
    on.add_gw(3)
    on.propose()
    on.ack_members()
    on.sync_keys()
    body, blob = _qkeys(on.s), _blob(on.s)
    assert body["thr"] == 3
    pts = [[body["x"], body["y"]]]
    for gw in on.s.db.gateways()[:2]:
        p = on.s.gwcfg_quorum(gw)["point"]
        pts.append([p["x"], p["y"]])
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": pts[:2], "qbox": body["qbox"]}, blob)
    m, _k = rsvopen.recover_keys({"qpoints": pts, "qbox": body["qbox"]}, blob)
    assert m.hex() == _keys(on.s)["m"]


def test_points_of_the_old_polynomial_do_not_open_after_a_gateway_was_removed(on):
    """Снятый шлюз унёс точку прежнего многочлена, у резерва — прежний ящик:
    новый пакет (новое M) ими не открыть."""
    old_body = _qkeys(on.s)
    old_point = on.s.gwcfg_quorum(on.s.db.gateways()[0])["point"]
    on.s._reserve_after_gateway_change("после снятия шлюза")
    blob = _blob(on.s)
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": [[1, old_body["y"]], [old_point["x"], old_point["y"]]], "qbox": old_body["qbox"]}, blob)


def test_the_code_keeps_opening_after_a_gateway_was_removed(on):
    """Код прежний — и по-прежнему открывает: прямая кода та же, ящик кода — новый вокруг нового M."""
    code = on.s.reserve_code()
    on.s._reserve_after_gateway_change("после снятия шлюза")
    body, blob = _qkeys(on.s), _blob(on.s)
    m, _k = rsvopen.recover_keys({"code": code, "cpoint": [1, body["cy"]], "cbox": body["cbox"]}, blob)
    assert m.hex() == _keys(on.s)["m"]


# ── аварийный код у резерва ─────────────────────────────────────────────────

@pytest.fixture()
def h4(box, monkeypatch):
    from awgbot.infra import rsvzone
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    box.peer_admin_opened()
    box.primary(False)
    code_window_outside(Cand(box))                        # окно кода открыл кандидат: свой «не вижу», шлюзы молчат
    return box


CODE = rsvkeys.code_encode(bytes(range(16)))


def _pkg_m_gen(s) -> int:
    return int(s.rsv_get("rsv_pkg_in").get("m_gen") or 0)


def test_the_code_goes_to_the_opening_with_the_code_point_and_box(h4):
    cg, m = rsvkeys.CodeGen.new(1), rsvkeys.new_master()
    cy, cbox = cg.reserve_point()[1].hex(), cg.cbox(m).hex()
    h4.s.rsv_update("rsv_qkeys", peer={"l": "b", "m_gen": _pkg_m_gen(h4.s), "q_gen": 0, "t": 0, "x": 1, "y": "",
                                       "qbox": "", "c_gen": 1, "cy": cy, "cbox": cbox})
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert res["ok"], res
    assert h4.launched == [{"kind": "code", "code": CODE.replace("-", ""), "cpoint": [1, cy], "cbox": cbox}], h4.launched


def test_a_code_box_of_another_generation_of_m_is_no_data_for_the_move(h4):
    """Ящик кода не того поколения `M`, что пакет у резерва: прежнего пути с долей нет —
    процесс открытия не запускается, ответ — «данных для переезда нет»."""
    h4.s.rsv_update("rsv_qkeys", peer={"l": "b", "m_gen": _pkg_m_gen(h4.s) + 1, "x": 1, "cy": "ab" * 16, "cbox": "cd" * 56})
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and res["why"] == "open_failed" and "данных для переезда нет" in res["reason"], res
    assert h4.launched == [], "процесс открытия запущен без ящика кода"


# ── тексты ──────────────────────────────────────────────────────────────────

def test_the_enabling_notices_name_the_servers_and_end_without_a_period():
    t = rn.quorum_enabled("Beta", ["Дача", "Офис"])
    assert "«Beta»" in t and "«Дача»" in t and "«Офис»" in t and not t.endswith("."), t
    assert "код" not in t.lower(), f"Т-К12 говорит о коде, а включение код не меняет: {t}"
    assert "шлюзы" not in rn.quorum_enabled("Beta", []), "«и шлюзы» без шлюзов"
    p = rn.quorum_enabled_peer("Alpha")
    assert "«Alpha»" in p and not p.endswith("."), p
