"""Пара после сопряжения на сервисах: `st` второй стороны (смена имени,
адреса, admin_id, совместимости), живость линка пары тактом планировщика,
уведомления после сопряжения, удаление пары с обеих сторон, роль админа
второго сервера и её сводка.

Цена ошибки: смена admin_id второй стороны, после которой прежний аккаунт
ещё видит экран резерва, — чужой человек читает состояние этого сервера;
удаление пары, пока клиенты у резерва, — клиенты без сервера и без пути
назад; «недоступен» дважды или ни разу — админ не верит уведомлениям;
сводка экрана роли с полями клиентов — админ другого сервера видит чужих
клиентов.
"""
from __future__ import annotations

import pytest

from awgbot.core import access_cache, config
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.types import ServiceError
from awgbot.util import pairproto

from tests.integration import _reserve_common as rc
from tests.integration._reserve_common import ADMIN, PEER_ADMIN

pytestmark = pytest.mark.integration
rsv = rc.rsv


def _st(**over) -> dict:
    body = {"ready": True, "name": "Beta", "version": "4.0.0", "caps": list(compat.CAPS), "gen": 0, "ports": [45871],
            "self": rc.HOST_B, "out": "home", "in": "idle", "pkg": {"gen": 0, "fp": ""}, "voters": [],
            "admin_id": PEER_ADMIN, "admin_opened": False, "gw": True}
    body.update(over)
    return body


@pytest.fixture()
def paired(services, rsv):
    rc.pair_as_inviter(services, rsv)
    return rsv


# ── st ───────────────────────────────────────────────────────────────────────

def test_my_st_passes_the_dictionary_and_says_who_my_admin_is(services, paired):
    body = services.reserve_st_body()
    pairproto.check(pairproto.PAIR_PROTO, "st", body, pairproto.INVITER)
    assert body["admin_id"] == ADMIN and body["ready"] is True and body["name"] == "Alpha"


def test_a_changed_admin_id_rebinds_the_role_and_notifies_once(services, paired):
    access_cache.put(777, "client", None)
    notes = services.reserve_st_in(_st(admin_id=555))
    assert services.reserve_peer()["peer_admin_id"] == 555
    assert services.reserve_is_peer_admin(555) and not services.reserve_is_peer_admin(PEER_ADMIN), \
        "прежний аккаунт сохранил роль"
    assert access_cache.get(777) is None, "кэш доступа не сброшен — прежний аккаунт ещё 15 с с ролью"
    changed = [n for n in notes if "сменился админ" in n.text]
    assert len(changed) == 1 and changed[0].tg_id == ADMIN, notes
    again = services.reserve_st_in(_st(admin_id=555))
    assert not [n for n in again if "сменился админ" in n.text], "уведомление о смене админа второй раз"


def test_the_first_admin_id_binds_quietly(services, rsv):
    """Строка пары без admin_id (0): первое значение — привязка, не «смена»."""
    rc.pair_row(services, admin=0)
    notes = services.reserve_st_in(_st(admin_id=PEER_ADMIN))
    assert services.reserve_peer()["peer_admin_id"] == PEER_ADMIN
    assert not [n for n in notes if "сменился админ" in n.text], notes


def test_st_updates_the_name_and_the_address(services, paired):
    services.reserve_st_in(_st(name="Beta-2", self="203.0.113.70"))
    peer = services.reserve_peer()
    assert (peer["name"], peer["host"]) == ("Beta-2", "203.0.113.70")


def test_a_newer_peer_turns_its_direction_off_once(services, paired):
    """Т-РУ9: второй сервер обновился (v4.1.0), этот — v4.0.0: его клиентов
    этот сервер больше не примет — уведомление один раз на расхождение."""
    services.reserve_st_in(_st())
    notes = services.reserve_st_in(_st(version="4.1.0"))
    off = [n for n in notes if n.text.startswith("⚠️ Резерв")]
    assert len(off) == 1, f"направление выключилось по версии молча: {notes}"
    assert not services.reserve_st_in(_st(version="4.1.0")), "то же расхождение — второе уведомление"


def test_a_peer_that_lost_a_capability_turns_my_direction_off_once(services, paired):
    """Второй сервер откатился на выпуск без зоны (`zone1`): мои клиенты к нему
    не переедут — уведомление один раз, повтор того же `st` — тишина."""
    services.reserve_st_in(_st())
    caps = [c for c in compat.CAPS if c != "zone1"]
    notes = services.reserve_st_in(_st(caps=caps))
    off = [n for n in notes if n.text.startswith("⚠️ Резерв")]
    assert len(off) == 1 and "принимать клиентов" in off[0].text, notes
    assert not services.reserve_st_in(_st(caps=caps))


def test_compatibility_back_says_so_once(services, paired):
    """Т-РУ9: расхождение ушло (вторая сторона вернула возможность) —
    «снова совместим» один раз."""
    services.reserve_st_in(_st())
    caps = [c for c in compat.CAPS if c != "zone1"]
    services.reserve_st_in(_st(caps=caps))
    notes = services.reserve_st_in(_st())
    assert len([n for n in notes if "снова совместим" in n.text]) == 1, notes
    assert not [n for n in services.reserve_st_in(_st()) if "снова совместим" in n.text]


def test_my_own_update_turns_my_direction_off_once(services, paired, monkeypatch):
    """Т-РУ9: этот сервер обновился до v4.1.0, второй остался на v4.0.0 —
    его `st` после переподключения прежний, но мои клиенты к нему больше не
    переедут: уведомление один раз."""
    services.reserve_st_in(_st())
    monkeypatch.setattr(config, "INSTALLED_VERSION", "4.1.0")
    notes = services.reserve_st_in(_st())
    off = [n for n in notes if n.text.startswith("⚠️ Резерв")]
    assert len(off) == 1, f"после своего обновления направление выключилось молча: {notes}"


def test_st_with_garbage_numbers_does_not_crash(services, paired):
    services.reserve_st_in(_st(ports=["443", 45871, None]))
    assert services.reserve_peer_state()["ports"] == [45871]


# ── удаление ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("direction,state,frag", [("out", "away", "сейчас у «Beta»"), ("out", "leaving", "у «Beta»"),
                                                   ("in", "serving", "сейчас здесь"), ("in", "taking", "здесь")])
def test_unpair_is_refused_while_clients_are_moved(services, paired, direction, state, frag):
    getattr(services, f"reserve_set_{direction}")(state)
    assert frag in services.reserve_unpair_check()
    with pytest.raises(ServiceError, match="сначала верни"):
        services.reserve_unpair()
    assert services.reserve_paired() and "--rollback" not in paired.modes


def test_unpair_drops_the_link_the_row_the_keys_and_the_role(services, paired):
    access_cache.put(PEER_ADMIN, "stranger", None)
    services.reserve_unpair()
    assert not services.reserve_paired() and paired.modes[-1] == "--rollback" and not paired.conf.exists()
    assert all(services.rsv_get(k) == {} for k in ("rsv_keys", "rsv_peer_state", "rsv_peer_admin", "rsv_out")), \
        "ключи резерва пережили удаление пары"
    assert not services.reserve_is_peer_admin(PEER_ADMIN), "роль админа второго сервера пережила пару"
    assert access_cache.get(PEER_ADMIN) is None
    assert services.reserve_code() == "" and services.reserve_link_role() == ""
    assert paired.fw[-1] is False, "файервол не перерисован после удаления"


def test_gone_from_the_peer_unpairs_even_while_moved(services, paired):
    """Вторая сторона удалила пару — у неё её больше нет; держать нашу
    половину бессмысленно и в переезде (уведомление объясняет)."""
    services.reserve_set_out("away")
    notes = services.reserve_peer_gone()
    assert not services.reserve_paired()
    assert len(notes) == 1 and "удалил пару" in notes[0].text


def test_unpair_without_a_pair_is_refused(services, rsv):
    with pytest.raises(ServiceError, match="[Рр]езерва нет"):
        services.reserve_unpair()


# ── живость: такт ────────────────────────────────────────────────────────────

class _Rx:
    """Счётчик линка пары и часы такта: alive — вторая сторона шлёт keepalive
    (счётчик растёт на каждом чтении), иначе застыл; часы модуля живости —
    те же, что у тактов (слово по каналу отмечается ими)."""
    def __init__(self, services, monkeypatch):
        import types
        from awgbot.domain.services.reserve import liveness as lvm
        self.value, self.alive, self.clock = 100, False, 1000.0
        monkeypatch.setattr(lvm, "time", types.SimpleNamespace(monotonic=lambda: self.clock))

        def read():
            if self.alive:
                self.value += 148
            return self.value
        monkeypatch.setattr(services, "_pair_rx", read)

    def ticks(self, services, start: float, end: float):
        out = []
        t = start
        while t <= end:
            self.clock = t
            out.append(services.reserve_liveness_tick(now=t))
            t += 5
        return out


def _texts(acts, frag: str):
    return [n for _, notes in acts for n in notes if frag in n.text]


def test_down_and_back_notify_once_each(services, paired, monkeypatch):
    rx = _Rx(services, monkeypatch)
    acts = rx.ticks(services, 1000, 1200)
    downs = _texts(acts, "Нет связи с резервом")
    assert len(downs) == 1 and downs[0].tg_id == ADMIN, downs
    assert [a for a, _ in acts].count("ping") == 3
    assert services.reserve_peer_seen() == "down" and services.reserve_down_since()
    rx.alive = True
    back = rx.ticks(services, 1205, 1400)
    assert len(_texts(back, "восстановлена")) == 1, back
    assert not _texts(back, "Нет связи с резервом")
    assert services.reserve_peer_seen() == "ok" and services.reserve_down_since() == ""


def test_a_word_on_the_channel_after_down_still_says_back_once(services, paired, monkeypatch):
    """«Недоступен» ушло, затем вторая сторона вернулась и первым дошло слово
    по каналу (сессия открылась раньше такта, увидевшего рост счётчика), —
    «снова на связи» всё равно приходит, один раз: иначе админ остаётся с
    последним уведомлением «недоступен» при живой паре, а `down_since` в
    карточке не снимается."""
    rx = _Rx(services, monkeypatch)
    rx.ticks(services, 1000, 1100)
    assert services.reserve_peer_seen() == "down"
    rx.clock = 1102
    services.reserve_session_opened({"caps": []})
    back = rx.ticks(services, 1105, 1105)                 # счётчик стоит: «снова на связи» — только по слову
    assert len(_texts(back, "восстановлена")) == 1, f"после слова по каналу «снова на связи» не пришло: {back}"
    assert services.reserve_down_since() == "", "в карточке «нет связи с …» при живой паре"


def test_a_pong_with_a_stranger_nonce_is_not_life(services, paired, monkeypatch):
    rx = _Rx(services, monkeypatch)
    acts = rx.ticks(services, 1000, 1040)
    assert acts[-1][0] == "ping"
    services.reserve_pong_in("чужой-нонс")
    assert services.reserve_peer_seen() == "suspect", "pong с чужим нонсом снял подозрение"
    services.reserve_pong_in(services.reserve_ping_nonce())
    assert services.reserve_peer_seen() == "ok"


def test_no_pair_no_liveness(services, rsv, monkeypatch):
    rx = _Rx(services, monkeypatch)
    acts = rx.ticks(services, 1000, 1200)
    assert all(a == ("", []) for a in acts), "без пары такт живости что-то делает"


# ── роль админа второго сервера ─────────────────────────────────────────────

def test_the_role_belongs_to_exactly_the_peer_admin(services, paired):
    assert services.reserve_is_peer_admin(PEER_ADMIN)
    assert not services.reserve_is_peer_admin(PEER_ADMIN + 1)
    assert not services.reserve_is_peer_admin(ADMIN)


def test_the_same_admin_id_on_both_servers_is_the_admin_not_the_role(services, rsv):
    rc.pair_row(services, admin=ADMIN)
    assert not services.reserve_is_peer_admin(ADMIN), "свой админ попал в роль админа второго сервера"


SUMMARY_KEYS = {"peer", "me", "version", "direction_in", "awg_ok", "link_online", "seen", "down_since", "inn",
                "in_since", "pkg_at", "pkg_gen", "rf_via_mine", "has_gateway", "traffic", "peer_ready", "my_ready",
                "code_needed", "code_since", "gw_views", "case", "case_since", "rf_line", "in_kind", "zone_links",
                "mem_free", "has_client", "self_admin", "tg_pool"}


def test_the_role_summary_has_no_client_fields(services, paired, make_active_client, monkeypatch):
    """Сторож: сводка экрана роли — из таблицы пары, ключей `rsv_*` и
    счётчиков; новых полей без пересмотра нет, клиентов этого сервера в ней
    нет ни одним значением."""
    who = make_active_client(name="Иван Секретный", tg_id=5050)
    services.add_device(who.id, "Телефон-Ивана")
    monkeypatch.setattr(services, "server_ok", lambda: True)
    d = services.reserve_peer_admin_summary()
    assert set(d) == SUMMARY_KEYS, f"поля сводки разошлись: {set(d) ^ SUMMARY_KEYS}"
    flat = repr(d)
    assert "Иван" not in flat and "Телефон" not in flat and "5050" not in flat, "в сводке роли данные клиента"
    assert set(d["peer"]) <= {"id", "name", "host", "pair_if", "pair_port", "peer_port", "pair_cidr", "listener",
                              "bot_username", "peer_admin_id", "created_at"}


def test_opening_the_bot_is_remembered_once(services, paired):
    services.reserve_peer_admin_opened()
    first = services.rsv_get("rsv_peer_admin")["opened_at"]
    services.reserve_peer_admin_opened()
    assert services.rsv_get("rsv_peer_admin")["opened_at"] == first
    assert services.reserve_st_body()["admin_opened"] is True
