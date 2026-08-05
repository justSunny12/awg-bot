"""
Доктор обязан не только находить отказ, но и указывать на нужный слой.

Ошибка адресации здесь дороже ложного срабатывания: человек идёт чинить не ту
машину. Так и вышло в бою — линк молчал три с половиной часа, а слой докладывал
«Линк поднят» зелёным, и внимание уезжало на шлюз.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import config
from awgbot.infra import routing
from awgbot.runtime import routing_doctor as doc

pytestmark = pytest.mark.unit


def _cp(stdout=b"", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=b"")


@pytest.fixture
def healthy(monkeypatch):
    """Все слои зелёные — чтобы тест менял ровно одну переменную."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(routing, "self_check", lambda force=False: (True, "ок"))
    monkeypatch.setattr(routing, "link_peer_address", lambda: "10.99.99.2")
    monkeypatch.setattr(routing, "table_route", lambda: "default dev awglink")
    monkeypatch.setattr(routing, "rule_present", lambda: True)
    monkeypatch.setattr(routing, "mss_clamp_present", lambda: True)
    monkeypatch.setattr(routing, "probe_gateway", lambda *a, **k: routing.PROBE_OK)
    monkeypatch.setattr(routing, "last_probe_latency_ms", lambda: 40)
    monkeypatch.setattr(routing, "hook_present", lambda: True)
    monkeypatch.setattr(routing, "list_sets", lambda: [])


def _link_row(rows):
    return [r for r in rows if "Линк" in r[1]][0]


def test_fresh_handshake_is_green(healthy, monkeypatch):
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 42)
    assert _link_row(doc._probe_layers())[0] == doc._OK


def test_stale_handshake_is_a_failure_not_a_green_line(healthy, monkeypatch):
    """Хендшейк в три часа при пересогласовании раз в две минуты — мёртвый линк.

    Прежде слой красил зелёным всё, где хендшейк просто существует, и человек
    шёл чинить шлюз вместо стороны, которая молчит.
    """
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 12551)
    row = _link_row(doc._probe_layers())
    assert row[0] == doc._BAD, "протухший хендшейк обязан быть отказом"
    assert "шлюз" in row[2].lower(), "нужно назвать, где чинить"


def test_short_blip_does_not_redden_a_working_link(healthy, monkeypatch):
    """Порог с запасом: короткий провал связи не должен краснить исправный линк."""
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 200)
    assert _link_row(doc._probe_layers())[0] == doc._OK


def test_threshold_exceeds_wireguard_reject_time():
    """Порог обязан быть больше REJECT_AFTER_TIME, иначе ловил бы норму."""
    assert doc._STALE_HANDSHAKE > 180


# ── маршрут может существовать и вести не туда ───────────────────────────────

def test_route_pointing_away_from_awg_is_a_failure(monkeypatch):
    """Проверять наличие маршрута мало.

    После переезда на хост via-маршрут от контейнера пережил миграцию и
    перекрыл connected. Наружу всё уходило, ответы клиентам отправлялись в
    мёртвую docker-сеть — а доктор был полностью зелёным, потому что маршрут
    «есть». Три часа разбора стоили ровно этой строки.
    """
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")

    def fake_host(args, check=True, input_data=None, timeout=20):
        if args[:3] == ["ip", "route", "show"]:
            return _cp(b"10.8.1.0/24 via 172.29.172.2 dev amn0\n")
        if args[:2] == ["systemctl", "list-unit-files"]:
            return _cp(f"{config.ROUTING_DNSMASQ_SERVICE}.service enabled".encode())
        return _cp()

    monkeypatch.setattr(routing, "_host", fake_host)
    monkeypatch.setattr(routing, "_host_ok", lambda args: True)
    with pytest.raises(routing.RoutingUnavailable, match="мимо awg0"):
        routing._check_static_plumbing()


def test_connected_route_through_awg_passes(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")

    def fake_host(args, check=True, input_data=None, timeout=20):
        if args[:3] == ["ip", "route", "show"]:
            return _cp(b"10.8.1.0/24 dev awg0 proto kernel scope link src 10.8.1.0\n")
        if args[:2] == ["systemctl", "list-unit-files"]:
            return _cp(f"{config.ROUTING_DNSMASQ_SERVICE}.service enabled".encode())
        return _cp()

    monkeypatch.setattr(routing, "_host", fake_host)
    monkeypatch.setattr(routing, "_host_ok", lambda args: True)
    routing._check_static_plumbing()          # не бросает


# ── рубильник и пересборка цепочки ходят под одним замком ────────────────────

def test_liveness_tick_toggles_marking_under_the_lock():
    """Тик и реконсиляция трогают одну цепочку.

    Без общего замка тик может включить маркировку в тот момент, когда
    _routing_apply уже опустошил наборы под перезапись, а правила ещё старые.
    Тогда `! --match-set` по пустому набору матчит ВСЁ, и трафик включённых
    уезжает на шлюз вместе с заблокированным — отказ, ради которого в
    реконсиляции и стоит сторож на пустые списки.
    """
    import inspect
    from awgbot.domain import services

    src = inspect.getsource(services.Services.routing_liveness_tick)
    call = src.index("routing.set_marking_enabled")
    lock = src.rindex("with routing.mutation_lock", 0, call)
    assert lock < call, "рубильник дёргается вне замка"


def test_probe_stays_outside_the_lock():
    """Зонд длится секунды (сеть).

    Держать на это время реконсиляцию значило бы менять один отказ на другой.
    """
    import inspect
    from awgbot.domain import services

    src = inspect.getsource(services.Services.routing_liveness_tick)
    probe = src.index("self.routing_probe()")
    lock = src.index("with routing.mutation_lock")
    assert probe < lock, "зонд не должен держать замок"


# ── замок не держат на время сети ────────────────────────────────────────────

def test_list_download_happens_outside_the_mutation_lock():
    """Восемь источников по 15 с таймаута — до двух минут под замком.

    Тик живости берёт тот же замок, чтобы дёрнуть рубильник маркировки, и
    работает раз в 30 с с max_instances=1. Держи скачивание под замком — отвал
    шлюза во время обновления списков означал бы не «зарубежный адрес», а
    отсутствие интернета у всех, кому режим включён: ровно тот несимметричный
    отказ, ради которого тик и сделан частым.
    """
    import inspect
    from awgbot.domain import services

    src = inspect.getsource(services.Services.routing_update_lists)
    lock = src.rindex("with routing.mutation_lock")
    assert src.index("routing.fetch") < lock, "скачивание не должно держать замок"
    # запись результата — наоборот, под замком
    assert lock < src.index('_routing_write_cache("subnets"')
