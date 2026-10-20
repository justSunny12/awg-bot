"""Основа, пока её клиенты у резерва (domain/services/reserve/away.py): отказы
действий над хостом, строка главного экрана админа (Т-Р17), блокировки,
передаваемые резерву.

Цена ошибки: обновление, миграция или восстановление, прошедшие при `away`, —
изменения хоста расходятся с репликой, которую резерв вернёт обратно, и после
слияния часть настроек откатывается; строка главной, молчащая о переезде, —
админ ищет, почему бот отвечает с другого сервера; блокировка, не дошедшая
до резерва, — заблокированный клиент ходит через него без ограничений.
"""
from __future__ import annotations

import types

import pytest

from awgbot.domain.services.reserve import away as awaymod
from awgbot.domain.services.types import ServiceError
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.mark.parametrize("state", ["leaving", "away", "returning"])
def test_every_host_action_is_refused_while_the_clients_are_not_at_home(mv, state):
    svc = mv.svc
    svc.reserve_set_out(state)
    release = types.SimpleNamespace(tag="v9.9.9")
    calls = [lambda: svc.fleet_start("v9.9.9"), lambda: svc.migration_prepare(), lambda: svc.restore_wait_start({"slots": []}),
             lambda: svc.apply_update(release)]
    for call in calls:
        with pytest.raises(ServiceError, match="отложены до возврата"):
            call()


def test_the_refusal_names_the_reserve(mv):
    mv.svc.reserve_set_out("away")
    with pytest.raises(ServiceError) as e:
        mv.svc.reserve_refuse_if_away()
    assert "Beta" in str(e.value)


def test_nothing_is_refused_at_home_or_without_a_pair(mv, services):
    mv.svc.reserve_refuse_if_away()
    assert isinstance(mv.svc.fleet_start("v9.9.9"), str), "дома набор отвечает как обычно (причиной или пустой строкой)"


def test_without_a_pair_nothing_is_refused(services, rsv):
    services.reserve_refuse_if_away()


# ── Т-Р17 ───────────────────────────────────────────────────────────────────

def test_the_main_line_is_empty_in_the_ordinary_state_and_without_a_pair(mv, services):
    assert mv.svc.reserve_main_line() == ""


def test_the_main_line_without_a_pair_is_empty(services, rsv):
    assert services.reserve_main_line() == ""


def test_the_main_line_follows_the_move_and_the_return(mv):
    svc = mv.svc
    svc.reserve_set_out("leaving")
    assert svc.reserve_main_line() == "🛟 Клиенты переезжают на резерв «Beta» — жду смену записи"
    svc.reserve_set_out("away", taken_at="2026-10-21T03:12:00+05:00")
    line = svc.reserve_main_line()
    assert line.startswith("🛟 Клиенты и бот — на «Beta» с ") and "03:12" in line
    svc.reserve_set_out("returning")
    assert svc.reserve_main_line() == "🛟 Клиенты возвращаются с резерва «Beta»"


def test_the_main_line_shows_the_forwarding_after_the_return_until_its_term(mv):
    svc = mv.svc
    soon = timeutil.to_iso(timeutil.shift(timeutil.now(), 86400))
    svc.reserve_set_out("home", relay_until=soon)
    assert "пересылка до" in svc.reserve_main_line()
    svc.reserve_set_out("home", relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), -60)))
    assert svc.reserve_main_line() == "", "срок прошёл — строка исчезает сама"
    svc.reserve_set_out("home", relay_until="не дата")
    assert svc.reserve_main_line() == ""


def test_the_main_line_of_the_reserve_while_hosting(mv):
    svc = mv.svc
    svc.reserve_set_in("serving", taken_at="2026-10-21T03:12:00+05:00")
    assert svc.reserve_main_line().startswith("🛟 У тебя клиенты и бот «Beta» с ") and "03:12" in svc.reserve_main_line()
    svc.reserve_set_in("relay", relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), 86400)))
    assert "возвращаются к нему — пересылка до" in svc.reserve_main_line()
    svc.reserve_set_in("idle")


def test_the_main_line_warns_when_the_reserve_is_unreachable(mv):
    svc = mv.svc
    svc._lv.state = "down"
    svc.rsv_update("rsv_peer_state", down_since="2026-10-21T13:58:00+05:00")
    line = svc.reserve_main_line()
    assert line.startswith("🔴 Резерв «Beta» недоступен с ") and "13:58" in line


def test_the_move_line_takes_precedence_over_the_unreachable_reserve(mv):
    svc = mv.svc
    svc._lv.state = "down"
    svc.reserve_set_out("away", taken_at="2026-10-21T03:12:00+05:00")
    assert svc.reserve_main_line().startswith("🛟 Клиенты и бот — на «Beta»")


# ── блокировки резерву ──────────────────────────────────────────────────────

def test_blocked_addresses_go_to_the_reserve_only_while_away_and_only_on_change(mv, make_active_client):
    svc = mv.svc
    client = make_active_client(name="Профиль", tg_id=7001)
    dev = svc.add_device(client.id, "Телефон")
    assert svc.reserve_blocks_to_send() is None, "дома блокировки резерву не нужны"
    svc.reserve_set_out("away")
    assert svc.reserve_blocks_to_send() == [], "первый раз — текущий (пустой) набор, чтобы резерв узнал состояние"
    assert svc.reserve_blocks_to_send() is None, "набор не изменился — повторять незачем"
    svc.db.update_device_fields(dev.device_id, block_reason=1)
    addr = svc.db.get_device(dev.device_id).address
    assert svc.reserve_blocks_to_send() == [addr]
    assert svc.reserve_blocks_to_send() is None
    svc.db.update_device_fields(dev.device_id, block_reason=0)
    assert svc.reserve_blocks_to_send() == [], "блокировку сняли — резерв должен узнать"
    svc.reserve_set_out("home")
    assert svc.reserve_blocks_to_send() is None
    svc.reserve_set_out("away")
    assert svc.reserve_blocks_to_send() == [], "после возврата и нового ухода набор шлётся заново"


def test_the_lever_button_flag_reads_the_setting(mv):
    from awgbot.core import settings
    settings.set_value("app.reserve.return", "button")
    assert mv.svc.reserve_return_lever_button() is True
    settings.set_value("app.reserve.return", "immediate")
    assert mv.svc.reserve_return_lever_button() is False
    assert awaymod.AWAY_REFUSED
