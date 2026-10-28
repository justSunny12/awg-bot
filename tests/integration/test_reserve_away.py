"""Основа, пока её пользователи у резерва (domain/services/reserve/away.py): отказы
действий над хостом, строка главного экрана админа (Т-Р17), блокировки,
передаваемые резерву.

Цена ошибки: обновление, миграция или восстановление, прошедшие при `away`, —
изменения хоста расходятся с репликой, которую резерв вернёт обратно, и после
слияния часть настроек откатывается; строка главной, молчащая о переезде, —
админ ищет, почему бот отвечает с другого сервера; блокировка, не дошедшая
до резерва, — заблокированный пользователь ходит через него без ограничений.
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
# Статус резерва в шапке главной — {name, dot, text}, как строка сервера:
# 🟢 переехать можно сейчас или резерв несёт пользователей, 🟡 не готов (с причиной)
# или идёт переезд/возврат, 🔴 резерв недоступен.

def _st(svc) -> tuple:
    s = svc.reserve_main_status()
    return s["name"], s["dot"], s["text"]


def _clock(iso: str) -> str:
    return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))


def test_online_without_confirmed_data_is_not_green(mv):
    """На связи, направление готово, но пакета у резерва ещё нет — переехать
    сейчас не на чем: зелёный кружок обещал бы админу переезд, которого не будет."""
    assert _st(mv.svc) == ("Beta", "🟡", "не готов: данных для переезда ещё нет")


def test_ready_with_fresh_data_is_green_and_names_the_data_time(mv):
    """Готово всё и пакет подтверждён — 🟢 и время данных: по нему админ судит,
    насколько свежим будет переезд."""
    rm.confirm_package(mv.svc)
    at = mv.svc.rsv_get("rsv_pkg_sent")["at"]
    assert _st(mv.svc) == ("Beta", "🟢", f"резерв готов · данные от {_clock(at)}")
    assert mv.svc.reserve_main_line() == f"🛟 Beta 🟢 резерв готов · данные от {_clock(at)}", \
        "строка одним текстом — имя, кружок, суть через пробел"


def test_readiness_off_here_is_named_as_this_side_even_with_data(mv):
    """«Готов быть резервом» выключен у себя — переезда не будет, сколько бы
    данных ни было; причина говорит «у тебя», а не про второй сервер — иначе
    админ идёт чинить не ту сторону."""
    from awgbot.core import settings
    settings.set_value("app.reserve.ready", False)
    rm.confirm_package(mv.svc)
    assert _st(mv.svc) == ("Beta", "🟡", "не готов: у тебя выключено «Готов быть резервом»")


def test_peer_readiness_off_is_said_without_repeating_its_name(mv):
    """У второго сервера выключена готовность: его имя уже в начале строки,
    причина — просто «выключено», без «у тебя» (иначе админ ищет переключатель у себя)."""
    rm.st_in(mv.svc, ready=False)
    rm.confirm_package(mv.svc)
    name, dot, text = _st(mv.svc)
    assert (dot, text) == ("🟡", "не готов: выключено «Готов быть резервом»"), text


def test_stale_data_is_not_green(mv):
    """Пакет подтверждён давно (старше двух часов) — переезд вернул бы пользователей
    в прошлое: 🟡 с причиной про устаревшие данные, а не «готов» со старым временем."""
    old = timeutil.to_iso(timeutil.shift(timeutil.now(), -3 * 3600))
    mv.svc.rsv_put("rsv_pkg_sent", {"gen": 7, "fp": "DUMMY-fp", "at": old})
    assert _st(mv.svc) == ("Beta", "🟡", "не готов: данные для переезда устарели")


@pytest.mark.parametrize("why,want", [
    ("здесь выключено «Готов быть резервом»", "у тебя выключено «Готов быть резервом»"),
    ("у «Beta» выключено «Готов быть резервом»", "выключено «Готов быть резервом»"),
    ("у «Beta»: на сервере нет dnsmasq", "на сервере нет dnsmasq"),
    ("здесь: на сервере нет dnsmasq", "у тебя: на сервере нет dnsmasq"),
    ("данные для переезда у резерва устарели", "данные для переезда устарели"),
    ("«Beta» не читает формат данных переезда — нужен выпуск новее",
     "не читает формат данных переезда — нужен выпуск новее"),
    ("«Beta» не умеет принимать пользователей — нужен выпуск новее", "не умеет принимать пользователей — нужен выпуск новее"),
    ("у «Beta» v4.0.0, здесь v4.1.0 — нужен резерв не младше", "v4.0.0, у тебя v4.1.0 — нужен резерв не младше"),
    ("порт AWG 443 занят на обоих серверах", "порт AWG 443 занят на обоих серверах"),
], ids=["своя готовность", "чужая готовность", "чужая зона", "своя зона", "устарели", "формат", "возможности",
        "версии", "прочее как есть"])
def test_the_main_reason_drops_the_repeated_name_and_says_you_for_this_side(why, want):
    """Имя резерва уже стоит в начале строки главной: причина его не повторяет,
    своя сторона — «у тебя», а не «здесь». Причины карточки, которых замена
    не касается, проходят без изменений — иначе на главной теряется суть."""
    assert awaymod.ReserveAwayMixin._main_reason(why, "Beta") == want


@pytest.mark.parametrize("why", ["у «Gamma» выключено «Готов быть резервом»",
                                 "«Gamma» не читает формат данных переезда — нужен выпуск новее",
                                 "у «Gamma» v4.0.0, здесь v4.1.0 — нужен резерв не младше"])
def test_the_main_reason_does_not_strip_another_name(why):
    """Срезается только имя своего резерва: чужое имя в причине — уже не повтор."""
    got = awaymod.ReserveAwayMixin._main_reason(why, "Beta")
    assert "«Gamma»" in got, got


def test_before_the_first_session(services, rsv):
    """Пара записана, сессии с резервом не было — так и сказано."""
    rc.pair_row(services)
    assert _st(services) == ("Beta", "🟡", "связи ещё не было")


def test_after_a_session_dropped_it_says_not_online_now(mv):
    """Сессия была и только что оборвалась (до «недоступен» — проба после 40 с
    тишины): строка не утверждает, что связи не было никогда, — иначе админ
    ищет ошибку в сопряжении, которого нет."""
    mv.svc.rsv_update("rsv_peer_state", session=False, session_at="2026-10-21T13:58:00+05:00")
    assert mv.svc.reserve_peer_seen() != "down"
    assert _st(mv.svc) == ("Beta", "🟡", "сейчас не на связи"), mv.svc.rsv_get("rsv_peer_state")


def test_without_a_pair_the_status_and_the_line_are_empty(services, rsv):
    """Пары нет — на главной нечего рисовать."""
    assert services.reserve_main_status() == {}
    assert services.reserve_main_line() == ""


def test_the_status_follows_the_move_and_the_return(mv):
    svc = mv.svc
    svc.reserve_set_out("leaving")
    assert _st(svc) == ("Beta", "🟡", "твои пользователи переезжают — жду смену записи")
    svc.reserve_set_out("away", taken_at="2026-10-21T03:12:00+05:00")
    assert _st(svc) == ("Beta", "🟢", f"несёт твоих пользователей и бот с {_clock('2026-10-21T03:12:00+05:00')}")
    svc.reserve_set_out("returning")
    assert _st(svc) == ("Beta", "🟡", "твои пользователи возвращаются на сервер")


def test_the_forwarding_after_the_return_is_shown_until_its_term(mv):
    svc = mv.svc
    soon = timeutil.to_iso(timeutil.shift(timeutil.now(), 86400))
    svc.reserve_set_out("home", relay_until=soon)
    date = timeutil.fmt_dt_ui(timeutil.parse_iso(soon))
    assert _st(svc) == ("Beta", "🟡", f"твои пользователи возвращаются на сервер — пересылка до {date}")
    rm.confirm_package(svc)
    svc.reserve_set_out("home", relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), -60)))
    assert _st(svc)[1:] == ("🟢", f"резерв готов · данные от {_clock(svc.rsv_get('rsv_pkg_sent')['at'])}"), \
        "срок прошёл — строка пересылки исчезает сама"
    svc.reserve_set_out("home", relay_until="не дата")
    assert _st(svc)[1] == "🟢", "срок не читается — обычное состояние, а не падение главной"


@pytest.mark.parametrize("state", ["taking", "serving", "releasing"])
def test_the_reserve_hosting_the_peer_clients_is_green(mv, state):
    """Резерв несёт пользователей второго сервера — это его работа, а не тревога."""
    mv.svc.reserve_set_in(state, taken_at="2026-10-21T03:12:00+05:00")
    assert _st(mv.svc) == ("Beta", "🟢", f"чужие пользователи и бот у тебя с {_clock('2026-10-21T03:12:00+05:00')}")


def test_the_reserve_forwarding_the_peer_clients_back(mv):
    soon = timeutil.to_iso(timeutil.shift(timeutil.now(), 86400))
    mv.svc.reserve_set_in("relay", relay_until=soon)
    assert _st(mv.svc) == ("Beta", "🟡", f"чужие пользователи возвращаются на резерв — пересылка до "
                                          f"{timeutil.fmt_dt_ui(timeutil.parse_iso(soon))}")


def test_an_unreachable_reserve_is_red(mv):
    """Линк пары молчит — 🔴 и с какого времени; даже с данными «готов» не говорим."""
    svc = mv.svc
    rm.confirm_package(svc)
    svc._lv.state = "down"
    svc.rsv_update("rsv_peer_state", down_since="2026-10-21T13:58:00+05:00")
    assert _st(svc) == ("Beta", "🔴", f"недоступен с {_clock('2026-10-21T13:58:00+05:00')}")


def test_the_move_takes_precedence_over_the_unreachable_reserve(mv):
    """Пользователи у резерва — главное сказать, где они; «недоступен» тут — шум."""
    svc = mv.svc
    svc._lv.state = "down"
    svc.reserve_set_out("away", taken_at="2026-10-21T03:12:00+05:00")
    assert _st(svc)[1:2] == ("🟢",) and _st(svc)[2].startswith("несёт твоих пользователей и бот с ")


# ── блокировки резерву ──────────────────────────────────────────────────────

def test_blocked_addresses_go_to_the_reserve_only_while_away_and_only_on_change(mv, make_active_client):
    """Блокировки основы — зоне резерва, только когда пользователи у резерва, а решает
    основа (бот на резерве остановлен: отказ слияния — VPN у резерва, решения здесь)."""
    svc = mv.svc
    client = make_active_client(name="Профиль", tg_id=7001)
    dev = svc.add_device(client.id, "Телефон")
    assert svc.reserve_blocks_to_send() is None, "дома блокировки резерву не нужны"
    svc.reserve_set_out("away")
    assert svc.reserve_following() and svc.reserve_blocks_to_send() is None, \
        "гость решает сам — устаревшие блокировки основы ему не нужны"
    svc.reserve_set_out("away", guest_stopped=True)
    assert svc.reserve_blocks_to_send() == [], "первый раз — текущий (пустой) набор, чтобы резерв узнал состояние"
    assert svc.reserve_blocks_to_send() is None, "набор не изменился — повторять незачем"
    svc.db.update_device_fields(dev.device_id, block_reason=1)
    addr = svc.db.get_device(dev.device_id).address
    assert svc.reserve_blocks_to_send() == [addr]
    assert svc.reserve_blocks_to_send() is None
    svc.db.update_device_fields(dev.device_id, block_reason=0)
    assert svc.reserve_blocks_to_send() == [], "блокировку сняли — резерв должен узнать"
    svc.reserve_set_out("home", guest_stopped=False)
    assert svc.reserve_blocks_to_send() is None
    svc.reserve_set_out("away", guest_stopped=True)
    assert svc.reserve_blocks_to_send() == [], "после возврата и нового ухода набор шлётся заново"


def test_the_lever_button_flag_reads_the_setting(mv):
    from awgbot.core import settings
    settings.set_value("app.reserve.return", "button")
    assert mv.svc.reserve_return_lever_button() is True
    settings.set_value("app.reserve.return", "immediate")
    assert mv.svc.reserve_return_lever_button() is False
    assert awaymod.AWAY_REFUSED
