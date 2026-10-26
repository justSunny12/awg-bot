"""Уведомления пользователям о переезде (Т-РК1, бот основы на резерве) и о
возврате (Т-РК2, бот основы на основе): кому, когда и сколько раз
(domain/services/reserve/notify.py).

Цена ошибки: «переподключи VPN» тем, кто и так уже на новом сервере, —
ложная тревога и лишний трафик; не пришедшее тем, кто сидит на старом
пути, — человек считает, что VPN сломан; второй раз на то же событие —
пользователи глохнут к сообщениям админа; тем, кто уже месяц не заходит, —
письмо «из ниоткуда» и вопрос «откуда он про меня знает».
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import notify
from awgbot.infra import awg, rsvzone
from awgbot.util import timeutil
from awgbot.util.rsvpkg import bot as pkgbot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import PEER_IP

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

NOW = int(timeutil.now().timestamp())


@pytest.fixture()
def world(mv, make_active_client, monkeypatch):
    """Несколько пользователей с устройствами; `dump` — что показывает `awg show dump` клона/сервера."""
    svc = mv.svc
    mv.dump = []
    mv.zone_state = {}
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: list(mv.dump), raising=False)
    monkeypatch.setattr(rsvzone, "state", lambda *a: dict(mv.zone_state))
    svc.db.set_state("online_polled_at", str(NOW))

    def device(name, tg, *, hs=None, block=0):
        client = make_active_client(name=name, tg_id=tg)
        dev = svc.add_device(client.id, "Телефон")
        if hs is not None:
            svc.db.update_device_fields(dev.device_id, last_handshake=hs)
        if block:
            svc.db.update_device_fields(dev.device_id, block_reason=block)
        return dev.device_id, svc.db.get_device(dev.device_id).public_key

    mv.device = device
    return mv


def _tgs(notes) -> list[int]:
    return sorted(n.tg_id for n in notes)


# ── Т-РК1: переезд ──────────────────────────────────────────────────────────

def test_only_devices_that_were_online_at_the_poll_get_the_move_notice(world):
    svc = world.svc
    world.device("На связи", 7001, hs=NOW - 60)
    world.device("Давно не заходил", 7002, hs=NOW - 16 * 60)
    world.device("Без рукопожатий", 7003)
    world.device("На границе", 7004, hs=NOW - 15 * 60)
    notes = svc.guest_move_notify()
    assert _tgs(notes) == [7001, 7004], "окно «на связи» — 15 минут к моменту опроса, граница включительно"
    assert all("переехал" in n.text and "выключи и включи" in n.text for n in notes)


def test_blocked_devices_and_the_admin_get_nothing(world):
    svc = world.svc
    world.device("Заблокирован", 7001, hs=NOW - 10, block=1)
    world.device("Админ", config.ADMIN_ID, hs=NOW - 10)
    world.device("Обычный", 7003, hs=NOW - 10)
    assert _tgs(svc.guest_move_notify()) == [7003]


def test_devices_already_moved_to_the_clone_are_not_asked_to_reconnect(world):
    """Рукопожатие на клоне после `taken_at` зоны — устройство уже переехало."""
    svc = world.svc
    world.zone_state = {"taken_at": timeutil.to_iso(timeutil.shift(timeutil.now(), -100))}
    taken = int(timeutil.parse_iso(world.zone_state["taken_at"]).timestamp())
    _id1, pub1 = world.device("Переехал", 7001, hs=NOW - 50)
    _id2, _pub2 = world.device("Сидит на старом", 7002, hs=NOW - 50)
    world.dump = [{"public_key": pub1, "last_handshake": taken + 30}]
    assert _tgs(svc.guest_move_notify()) == [7002]


def test_a_handshake_on_the_clone_before_the_take_does_not_count_as_moved(world):
    svc = world.svc
    world.zone_state = {"taken_at": timeutil.to_iso(timeutil.shift(timeutil.now(), -100))}
    taken = int(timeutil.parse_iso(world.zone_state["taken_at"]).timestamp())
    _id1, pub1 = world.device("Старый след", 7001, hs=NOW - 50)
    world.dump = [{"public_key": pub1, "last_handshake": taken - 500}]
    assert _tgs(svc.guest_move_notify()) == [7001]


def test_an_unreadable_clone_dump_means_nobody_moved_yet(world, monkeypatch):
    svc = world.svc
    world.zone_state = {"taken_at": timeutil.now_iso()}
    world.device("Один", 7001, hs=NOW - 10)

    def boom(iface=None):
        raise OSError("awg show упал")
    monkeypatch.setattr(awg, "show_dump", boom, raising=False)
    assert _tgs(svc.guest_move_notify()) == [7001]


def test_two_devices_of_one_client_make_one_message(world):
    svc = world.svc
    cid_dev, _ = world.device("Двое", 7001, hs=NOW - 10)
    client_id = svc.db.get_device(cid_dev).client_id
    second = svc.add_device(client_id, "Ноутбук")
    svc.db.update_device_fields(second.device_id, last_handshake=NOW - 20)
    assert _tgs(svc.guest_move_notify()) == [7001]


def test_the_move_notice_comes_once_per_move_even_when_nobody_was_online(world):
    svc = world.svc
    assert svc.guest_move_notify() == [], "некому писать"
    world.device("Опоздавший", 7001, hs=NOW - 10)
    assert svc.guest_move_notify() == [], "событие одно — второй раз рассылка не повторяется"
    assert svc.db.get_state(notify.MOVE_NOTIFIED_KEY)


def test_the_marker_of_the_notice_travels_in_the_replica_so_the_primary_does_not_resend(world):
    assert notify.MOVE_NOTIFIED_KEY in pkgbot.GUEST_STATE


def test_the_rf_variant_names_the_reason(world):
    world.device("Один", 7001, hs=NOW - 10)
    notes = world.svc.guest_move_notify(rf=True)
    assert "недоступен из РФ" in notes[0].text
    assert not notes[0].text.rstrip().endswith(".")


def test_the_move_notice_has_no_forced_sound(world):
    """Звук — по общим правилам тихих часов: принудительного звука у уведомлений пользователям нет."""
    world.device("Один", 7001, hs=NOW - 10)
    assert not world.svc.guest_move_notify()[0].force_sound


# ── Т-РК2: возврат ──────────────────────────────────────────────────────────

def _home_after_return(w, *, minutes_ago: float, relay_until: str = ""):
    fact = timeutil.to_iso(timeutil.shift(timeutil.now(), -int(minutes_ago * 60)))
    w.svc.reserve_set_out("home", back_fact_at=fact, notified=False, relay_until=relay_until)


def _peer(pub, ip, hs):
    return {"public_key": pub, "endpoint": f"{ip}:51820", "last_handshake": hs}


def test_nothing_is_sent_before_ten_minutes_after_the_record_fact_and_the_wait_is_not_consumed(world):
    svc = world.svc
    _id, pub = world.device("Через резерв", 7001, hs=NOW - 10)
    world.dump = [_peer(pub, PEER_IP, NOW - 60)]
    _home_after_return(world, minutes_ago=9)
    assert svc.reserve_return_notify_tick() == []
    assert svc.rsv_get("rsv_out")["notified"] is False, "ожидание съело событие: рассылка так и не придёт"


def test_only_devices_still_going_through_the_reserve_are_asked_to_reconnect(world):
    svc = world.svc
    _a, pub_a = world.device("Через резерв", 7001, hs=NOW - 10)
    _b, pub_b = world.device("Уже на основе", 7002, hs=NOW - 10)
    _c, pub_c = world.device("Давно молчит", 7003, hs=NOW - 10)
    _d, pub_d = world.device("Заблокирован", 7004, hs=NOW - 10, block=1)
    world.dump = [_peer(pub_a, PEER_IP, NOW - 60), _peer(pub_b, "203.0.113.99", NOW - 60),
                  _peer(pub_c, PEER_IP, NOW - 16 * 60), _peer(pub_d, PEER_IP, NOW - 60),
                  {"public_key": "DUMMY-unknown", "endpoint": f"{PEER_IP}:1", "last_handshake": NOW - 5},
                  {"public_key": pub_b, "endpoint": "", "last_handshake": NOW - 5}]
    _home_after_return(world, minutes_ago=11)
    notes = svc.reserve_return_notify_tick()
    assert _tgs(notes) == [7001], "эндпоинт — адрес резерва и рукопожатие за 15 минут; больше никого"
    assert "вернулся на основной" in notes[0].text and "запасной путь" in notes[0].text
    assert svc.reserve_return_notify_tick() == [], "Т-РК2 — один раз на возврат"


def test_the_return_notice_names_the_forwarding_date(world):
    svc = world.svc
    _a, pub = world.device("Через резерв", 7001, hs=NOW - 10)
    world.dump = [_peer(pub, PEER_IP, NOW - 60)]
    until = timeutil.to_iso(timeutil.shift(timeutil.now(), 5 * 86400))
    _home_after_return(world, minutes_ago=11, relay_until=until)
    text = svc.reserve_return_notify_tick()[0].text
    assert "до " in text and "запасной путь" in text


def test_nobody_on_the_reserve_means_no_notice_but_the_event_is_consumed(world):
    svc = world.svc
    _home_after_return(world, minutes_ago=11)
    assert svc.reserve_return_notify_tick() == []
    assert svc.rsv_get("rsv_out")["notified"] is True


def test_an_unreadable_dump_sends_nothing_and_consumes_the_event(world, monkeypatch):
    svc = world.svc
    _home_after_return(world, minutes_ago=11)

    def boom(iface=None):
        raise OSError("нет awg")
    monkeypatch.setattr(awg, "show_dump", boom, raising=False)
    assert svc.reserve_return_notify_tick() == []
    assert svc.rsv_get("rsv_out")["notified"] is True


def test_without_a_record_fact_there_is_nothing_to_announce(world):
    svc = world.svc
    svc.reserve_set_out("home", back_fact_at="", notified=False)
    assert svc.reserve_return_notify_tick() == [] and svc.rsv_get("rsv_out")["notified"] is True


@pytest.mark.parametrize("state", ["away", "leaving", "returning"])
def test_the_return_notice_is_not_sent_while_the_clients_are_elsewhere(world, state):
    svc = world.svc
    _home_after_return(world, minutes_ago=11)
    svc.reserve_set_out(state)
    assert svc.reserve_return_notify_tick() == []


def test_a_normal_home_state_without_a_return_never_notifies(world):
    assert world.svc.reserve_return_notify_tick() == []
