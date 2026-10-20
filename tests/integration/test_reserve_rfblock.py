"""Случаи 3 и Н3 (domain/services/reserve/rfblock.py): резерв видит основу,
а ни один шлюз на связи с ним — нет. Обе стороны.

Резерв: демпфер `rf_block_minutes` подряд (первый «вижу» — счёт заново,
3 ↔ Н3 — не сбрасывает), `ask{3 | nogw}` основе, повтор по причине отказа,
без ответа `ask_seconds` — `need` шлюзу «не вижу» (только случай 3) или
повтор `ask` (Н3); Н3 — только когда резерву есть чем дать РФ-доступ.
Основа: `view` → Т-РУ13 / Т-РУ21 / Т-РУ23 один раз на (случай, момент);
`ask` → `ask_no{link_down}` при неподнятом линке слота, `ask_no{gw_seen}`
при видимом своём шлюзе, `ask_no{busy}` при занятости, иначе путь кнопки
с видом rf | nogw (`go{rf}`).

Цена ошибки: переезд «недоступен из РФ» по короткому сбою пути у шлюза
(перезагрузка, OMV Apply) — все клиенты основы переподключаются ради
минут; `need` в Н3 — резерв собирает ключ, когда основа жива и сама может
решить; согласие основы при опущенном у неё линке слота — переезд, который
ничего не даёт клиентам; Т-РУ13 на каждый такт — шум у админа основы.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import settings
from awgbot.util import timeutil
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import (ADMIN, GW_SHARE, RSV_SHARE, box, gateway_point, gw_sent, notes,
                                                    sent)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

DAMPER = 15 * 60
ASK = 60


@pytest.fixture()
def mine(box, monkeypatch):
    """У резерва свой шлюз (РФ-доступ его клиентов через мои шлюзы разрешён)."""
    monkeypatch.setattr(box.s.db, "gateways", lambda: [object()])
    return box


def _run(box, seconds: float, step: float = 5) -> list[tuple]:
    """Такты голосования за seconds модельного времени; keepalive шлюзов — каждые 30 с."""
    acts, t = [], 0.0
    while t < seconds:
        box.advance(step)
        t += step
        if int(t) % 30 == 0:
            box.keepalive()
        acts += box.tick()
    return acts


def _asks(acts) -> list[dict]:
    return sent(acts, "ask")


# ── резерв: демпфер ─────────────────────────────────────────────────────────

def test_case_3_asks_the_primary_only_after_the_damper(box):
    box.primary(True)
    box.hello(1, sees=False)
    box.hello(2, sees=False)
    assert box.case == "3"
    early = _run(box, DAMPER - 10)
    assert not _asks(early), "ask основе раньше rf_block_minutes"
    assert not gw_sent(early, "need"), "need в случае 3 до просьбы основе"
    late = _run(box, 20)
    asks = _asks(late)
    assert len(asks) == 1 and asks[0]["case"] == "3", f"после демпфера нет ask{{3}}: {late}"
    assert sorted(v["name"] for v in asks[0]["voters"]) == ["NASPi", "Pi5"]


def test_the_first_see_resets_the_damper(box):
    box.primary(True)
    box.hello(1, sees=False)
    box.hello(2, sees=False)
    _run(box, DAMPER - 60)
    box.seen(2, True)                                   # случай 4 — счёт сброшен
    _run(box, 10)
    box.seen(2, False)                                  # снова 3 — считаем заново
    acts = _run(box, DAMPER - 30)
    assert not _asks(acts), "«вижу» не сбросил демпфер: ask раньше 15 мин нового счёта"
    assert _asks(_run(box, 60)), "после нового полного счёта ask не ушёл"


def test_switching_between_3_and_h3_does_not_reset_the_damper(mine):
    box = mine
    box.primary(True)
    box.hello(1, sees=False)
    _run(box, 10 * 60)
    box.gone(1)                                         # шлюз «не вижу» стал недоступен — Н3
    assert box.case == "H3"
    acts = _run(box, 5 * 60 + 10)
    asks = _asks(acts)
    assert len(asks) == 1 and asks[0]["case"] == "nogw", f"3 → Н3 сбросил счёт: {acts}"


def test_h3_does_not_ask_when_the_reserve_has_nothing_to_give(box, monkeypatch):
    """Н3 — ради РФ-доступа через шлюзы резерва: своих шлюзов нет или
    админ резерва запретил — просьбы нет вовсе."""
    box.primary(True)
    box.tick()
    assert box.case == "H3"
    assert not _asks(_run(box, DAMPER + 120)), "ask{nogw} без своих шлюзов"
    monkeypatch.setattr(box.s.db, "gateways", lambda: [object()])
    settings.set_value("app.reserve.rf_via_mine", False)
    assert not _asks(_run(box, DAMPER + 120)), "ask{nogw} при выключенном РФ-доступе через мои шлюзы"


def test_case_3_without_an_answer_asks_an_unseen_gateway_for_its_share(box):
    box.primary(True)
    box.hello(1, sees=False)
    _run(box, DAMPER + 5)
    needs, kind = [], ""
    for _ in range((ASK + 10) // 5):
        box.advance(5)
        box.keepalive()
        needs += gw_sent(box.tick(), "need")
        if needs:
            kind = (box.s.rsv_get("rsv_view").get("need") or {}).get("kind")
            break
    assert [s for s, _ in needs] == [1] and needs[0][1]["case"] == "3", f"основа молчит — нет need{{3}}: {needs}"
    assert kind == "rf", "need случая 3 — не переезд вида rf"


def _until_need(box) -> list:
    needs = []
    for _ in range((ASK + 10) // 5):
        box.advance(5)
        box.keepalive()
        needs += gw_sent(box.tick(), "need")
        if needs:
            break
    return needs


def test_the_share_in_case_3_opens_by_points_with_the_rf_kind(box):
    box.primary(True)
    box.hello(1, sees=False)
    _run(box, DAMPER + 5)
    assert _until_need(box), "need{3} не ушёл"
    box.share(1)
    assert len(box.launched) == 1, "доля на need{3} не взята"
    assert box.launched[0]["shares"] == [[1, RSV_SHARE], [gateway_point(1), GW_SHARE]]
    assert box.s.rsv_get("rsv_in")["kind"] == "rf"


def test_the_share_in_case_3_is_taken_even_if_a_tick_passed_after_need(box):
    """Доля приходит по гостевому каналу асинхронно; такт голосования (5 с)
    может пройти между `need` и `share` — как в случаях 1 и 5, где просьба
    живёт до ответа или до повтора через 30 с."""
    box.primary(True)
    box.hello(1, sees=False)
    _run(box, DAMPER + 5)
    assert _until_need(box), "need{3} не ушёл"
    box.advance(5)
    box.keepalive()
    box.tick()                                          # такт между need и share
    box.share(1)
    assert len(box.launched) == 1, "доля на need{3} отброшена: такт голосования стёр просьбу до ответа шлюза"


def test_h3_without_an_answer_repeats_ask_and_never_sends_need(mine):
    box = mine
    box.primary(True)
    box.tick()
    acts = _run(box, DAMPER + 5) + _run(box, 3 * ASK + 10)
    assert len(_asks(acts)) >= 3 and all(a["case"] == "nogw" for a in _asks(acts)), _asks(acts)
    assert not gw_sent(acts, "need"), "need в Н3"
    box.hello(1, sees=True)                             # вернулся «вижу» — 6, просить нечего
    assert box.case == "6"
    assert not _asks(_run(box, DAMPER + 120))


def test_busy_refusal_is_asked_again_after_ask_seconds_and_link_down_after_15_minutes(box):
    box.primary(True)
    box.hello(1, sees=False)
    _run(box, DAMPER + 5)
    box.s.reserve_ask_no_in("busy")
    assert not _asks(_run(box, ASK - 10)), "после busy спросил раньше ask_seconds"
    assert _asks(_run(box, 20)), "после busy не спросил снова"
    box.s.reserve_ask_no_in("link_down")
    assert not _asks(_run(box, 14 * 60)), "после link_down спросил раньше 15 мин"
    assert not gw_sent(_run(box, 30), "need"), "отказ основы обошли долями шлюза"
    assert _asks(_run(box, 2 * 60)), "после link_down через 15 мин не спросил снова"


def test_no_ask_while_the_zone_is_busy(box):
    box.s.reserve_set_in("serving")
    box.primary(True)
    box.hello(1, sees=False)
    acts = _run(box, DAMPER + ASK + 30)
    assert not _asks(acts) and not gw_sent(acts, "need")
    assert box.s.reserve_rf_line().startswith("⚠️"), "причина «переезд не идёт» не видна в карточке"


# ── основа: view ────────────────────────────────────────────────────────────

V3 = [{"slot": 1, "name": "NASPi", "see": False}, {"slot": 2, "name": "Pi5", "see": False}]


def test_view_3_tells_the_primary_admin_once_per_episode(services, rsv):
    rc.pair_row(services)
    first = services.reserve_view_in("3", "2026-10-21T03:11:00+03:00", V3)
    assert len(first) == 1 and first[0].tg_id == ADMIN and "недоступен из РФ" in first[0].text
    assert not first[0].text.endswith("."), "уведомление с точкой в конце"
    assert not services.reserve_view_in("3", "2026-10-21T03:11:00+03:00", V3), "Т-РУ13 повторилось"
    again = services.reserve_view_in("3", "2026-10-21T05:00:00+03:00", V3)
    assert len(again) == 1, "новый эпизод — без уведомления"


def test_view_4_and_h3_have_their_own_texts(services, rsv):
    rc.pair_row(services)
    v4 = services.reserve_view_in("4", "2026-10-21T03:11:00+03:00",
                                  [{"slot": 1, "name": "NASPi", "see": False}, {"slot": 2, "name": "Pi5", "see": True}])
    assert len(v4) == 1 and "переезда не будет" in v4[0].text and "«NASPi»" in v4[0].text
    h3 = services.reserve_view_in("H3", "2026-10-21T03:20:00+03:00", [])
    assert len(h3) == 1 and "ради РФ-доступа" in h3[0].text
    assert not services.reserve_view_in("6", "2026-10-21T03:30:00+03:00", []), "на штатный случай пришло уведомление"


# ── основа: ask ─────────────────────────────────────────────────────────────

@pytest.fixture()
def primary(services, rsv, make_active_client, monkeypatch):
    """Основа со своим шлюзом слота 1 (линк awglink), ключи переезда есть,
    резерв на связи, пакет у него свежий."""
    rc.pair_row(services)
    cl = make_active_client(name="Шлюз", tg_id=7001)
    dev = services.add_device(cl.id, "NASPi").device_id
    services.db.gateway_add(dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.rsv_update("rsv_peer_state", version="4.0.0", session=True)
    services.rsv_put("rsv_keys", {"m": "11" * 16, "a": "22" * 16, "m_gen": 1, "k": "33" * 32, "k_gen": 1})
    services.reserve_set_out("home")
    monkeypatch.setattr(services, "reserve_directions", lambda: {"in": (True, ""), "out": (True, "")})
    monkeypatch.setattr(services, "reserve_online", lambda: True)
    monkeypatch.setattr(services, "reserve_peer_ip", lambda: rc.ADDR_B)
    state = {"listen_rc": 0, "session": False, "hs": None, "awg": []}
    real = subprocess.run

    def run(argv, *a, **kw):
        if isinstance(argv, list) and argv[:2] == ["awg", "show"]:
            state["awg"].append(argv)
            return subprocess.CompletedProcess(argv, state["listen_rc"], b"47101\n" if not state["listen_rc"] else b"",
                                               b"Unable to access interface: No such device\n")
        return real(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    from awgbot.infra.routing import probes
    monkeypatch.setattr(probes, "link_handshake_age", lambda iface="": state["hs"])
    monkeypatch.setattr(services, "gwlink_session", lambda slot: {"since": "x"} if state["session"] else {})
    return state


SINCE = "2026-10-21T03:11:00+03:00"


def test_ask_3_with_the_slot_link_down_is_refused_with_link_down_and_told_once(services, primary):
    primary["listen_rc"] = 1
    acts = services.reserve_ask_in("3", SINCE, V3)
    assert sent(acts, "ask_no") == [{"reason": "link_down"}], acts
    told = notes(acts)
    assert len(told) == 1 and "awglink" in told[0].text and "дело не в РФ" in told[0].text
    assert services.reserve_out() == "home", "отказ, а переезд начат"
    again = services.reserve_ask_in("3", SINCE, V3)
    assert sent(again, "ask_no") == [{"reason": "link_down"}] and not notes(again), "Т-РУ22 повторилось"
    assert primary["awg"] and primary["awg"][0] == ["awg", "show", "awglink", "listen-port"]


def test_ask_3_with_the_links_up_starts_the_move_with_the_rf_kind_and_go_carries_it(services, primary):
    acts = services.reserve_ask_in("3", SINCE, V3)
    assert not sent(acts, "ask_no"), f"согласие, а ушёл отказ: {acts}"
    out = services.rsv_get("rsv_out")
    assert out["state"] == "leaving" and out["kind"] == "rf" and out["dead_since"] == SINCE
    services.rsv_put("rsv_pkg_sent", {"gen": 3, "at": timeutil.now_iso()})
    go = sent(services.reserve_out_tick(), "go")
    assert len(go) == 1 and go[0]["kind"] == "rf" and go[0]["gen"] == 3, f"go без вида rf: {go}"


def test_ask_nogw_while_the_primary_sees_its_own_gateway_is_refused_with_gw_seen(services, primary):
    primary.update(session=True, hs=30)
    acts = services.reserve_ask_in("nogw", SINCE, [])
    assert sent(acts, "ask_no") == [{"reason": "gw_seen"}]
    told = notes(acts)
    assert len(told) == 1 and rc.ADDR_B in told[0].text and "РФ-доступ работает" in told[0].text
    assert services.reserve_out() == "home"


def test_ask_nogw_with_a_stale_handshake_is_agreed_with_the_nogw_kind(services, primary):
    """Сессия канала есть, но хендшейк старше 180 с — шлюз для основы не виден:
    согласие, путь кнопки с видом nogw."""
    primary.update(session=True, hs=400)
    acts = services.reserve_ask_in("nogw", SINCE, [])
    assert not sent(acts, "ask_no")
    assert services.rsv_get("rsv_out")["kind"] == "nogw"


def test_ask_while_busy_is_refused_with_busy_and_no_notice(services, primary):
    services.reserve_set_in("serving")                  # у меня клиенты резерва
    acts = services.reserve_ask_in("3", SINCE, V3)
    assert sent(acts, "ask_no") == [{"reason": "busy"}] and not notes(acts)
    assert services.reserve_out() == "home"


# ── Т-РУ23 обещает переезд — только когда он возможен ───────────────────────

def _views_h3(acts) -> list[dict]:
    return [b for b in sent(acts, "view") if b.get("case") == "H3"]


def test_no_h3_warning_to_the_primary_when_the_reserve_has_no_gateways_to_give(box):
    """Т-РУ23 говорит админу основы «если так останется до …, клиенты
    переедут». Резерву нечем дать РФ-доступ (своих шлюзов нет) — Н3 не
    просит переезда никогда, и обещать его нельзя."""
    box.primary(True)
    acts = box.tick() + _run(box, 60)
    assert box.case == "H3"
    assert not _views_h3(acts), "основе ушёл view{H3} (Т-РУ23 «переедут»), а переезда в Н3 не будет"


def test_no_h3_warning_when_the_primary_has_no_voting_gateways_at_all(mine):
    """У основы ни одного шлюза с rsv1: «шлюзы не на связи с резервом» —
    не про её сеть, и направление без голосующих не готово."""
    box = mine
    box.s.rsv_update("rsv_peer_state", voters=[])
    box.primary(True)
    acts = box.tick() + _run(box, 60)
    assert not _views_h3(acts), "основе без шлюзов ушёл view{H3}"
