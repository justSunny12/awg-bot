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
    _routing_apply ещё перекладывает наборы и цепочку. Пересобирается и то, по
    чему матчит правило, и список правил — увидеть это состояние наполовину
    значит метить не тем набором и не для тех профилей.
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
    """Источники с таймаутом по 15 с — заметное время под замком.

    Тик живости берёт тот же замок, чтобы дёрнуть рубильник маркировки, и
    работает раз в 30 с с max_instances=1. Держи скачивание под замком — и
    отвал шлюза во время обновления списков не гасился бы до конца загрузки,
    то есть тик, сделанный частым ради быстрой реакции, простаивал бы именно
    там, где реакция нужна.
    """
    import inspect
    from awgbot.domain import services

    src = inspect.getsource(services.Services.routing_update_lists)
    lock = src.rindex("with routing.mutation_lock")
    assert src.index("routing.fetch") < lock, "скачивание не должно держать замок"
    # запись результата — наоборот, под замком
    assert lock < src.index('_routing_write_cache("home_domains"')


def test_marking_rule_has_no_negation():
    """Правило метит то, что В НАБОРЕ, — без `!`.

    Единственная модель: на шлюз уходит только перечисленное в наборе. Разница
    с упразднённой обратной моделью — ровно один символ в правиле, и перепутать
    их нечем: обе версии собираются, обе выглядят рабочими, а трафик едет в
    противоположные стороны. Отсюда же следует, что пустой набор безопасен.
    """
    import inspect
    from awgbot.infra import routing as infra_routing

    src = inspect.getsource(infra_routing.rebuild_chain)
    body = src.split('"""', 2)[-1]              # без докстринга
    assert '"--match-set"' in body
    assert '"!"' not in body, "вернулась инверсия: набор снова означал бы заграницу"


# ── кэш самопроверки: «не работает» обязан перепроверяться ────────────────────

@pytest.fixture()
def selfcheck_env(monkeypatch):
    """Управляемое окружение самопроверки: state['up'] — есть ли интерфейс.

    Считаем ЗАХОДЫ в проверку: суть теста в том, сколько раз вердикт сверяется
    с железом, а не какой он.
    """
    import types
    state = types.SimpleNamespace(up=True, calls=0)

    def host_ok(args):
        if args[:3] == ["ip", "link", "show"]:
            state.calls += 1
            return state.up
        return True

    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(routing, "_check_host_tools", lambda: None)
    monkeypatch.setattr(routing, "_check_static_plumbing", lambda: None)
    monkeypatch.setattr(routing, "_host_ok", host_ok)
    from awgbot.infra import awg as _awg
    monkeypatch.setattr(_awg, "in_container", lambda: False)
    routing.invalidate_self_check()
    yield state
    routing.invalidate_self_check()


def test_negative_verdict_rechecks_itself(selfcheck_env, monkeypatch):
    """Окружение чинится СНАРУЖИ — обновлением ядра, перезапуском линка, правкой
    руками, — и бот обязан это заметить сам.

    Прежде кэш сбрасывался только там, где окружение менял сам бот, поэтому
    вердикт «интерфейса нет», вынесенный на минуту простоя, держался до
    перезапуска бота или до ближайшего обновления списков — до шести часов
    после того, как причина устранена. Всё это время маршрутизация выключена
    у ВСЕХ пользователей.
    """
    selfcheck_env.up = False
    assert routing.self_check()[0] is False
    was = selfcheck_env.calls

    routing.self_check()                       # в пределах TTL к железу не ходим
    assert selfcheck_env.calls == was

    selfcheck_env.up = True
    monkeypatch.setattr(routing, "_selfcheck_at",
                        routing._selfcheck_at - routing._SELFCHECK_BAD_TTL - 1)
    ok, why = routing.self_check()
    assert ok is True and why == "ок"
    assert selfcheck_env.calls > was, "вердикт не перепроверился с железом"


def test_positive_verdict_is_not_rechecked(selfcheck_env, monkeypatch):
    """«Ок» держим бессрочно: проверка ходит в подпроцессы, а available() зовут
    ещё и при отрисовке экранов. Ложное «ок» ловится зондом живости, ложное
    «не работает» не ловится ничем."""
    assert routing.self_check()[0] is True
    was = selfcheck_env.calls

    monkeypatch.setattr(routing, "_selfcheck_at",
                        routing._selfcheck_at - routing._SELFCHECK_BAD_TTL * 10)
    for _ in range(5):
        routing.self_check()
    assert selfcheck_env.calls == was, "положительный вердикт перепроверяется зря"


def test_force_still_bypasses_the_cache(selfcheck_env):
    """force=True остаётся способом спросить железо немедленно — на нём держится
    сброс после того, как бот сам поменял окружение."""
    assert routing.self_check()[0] is True
    was = selfcheck_env.calls
    selfcheck_env.up = False
    assert routing.self_check(force=True)[0] is False
    assert selfcheck_env.calls > was
