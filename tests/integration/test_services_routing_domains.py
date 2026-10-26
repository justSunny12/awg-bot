"""Условная маршрутизация: личные списки доменов, проекция в dnsmasq, досев
набора при добавлении домена.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

from awgbot.core import config

from tests.integration._services_routing_common import _device, _srcset

pytestmark = pytest.mark.integration


# ── Личные списки доменов ────────────────────────────────────────────────────

def test_add_domains_normalizes_and_reports(services, make_active_client):
    c = make_active_client()
    res = services.routing_add_domains(
        c.id, "https://www.bank.com/login\nNETFLIX.com\nмусор_тут")
    assert res.added == ["bank.com", "netflix.com"]
    assert "мусор_тут" in dict(res.rejected)


def test_add_domain_twice_is_reported_not_duplicated(services, make_active_client):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    res = services.routing_add_domains(c.id, "bank.com")
    assert res.added == []
    assert dict(res.rejected)["bank.com"] == "уже в списке"
    assert services.routing_domains(c.id) == ["bank.com"]


def test_ru_domain_is_accepted(services, make_active_client):
    """.ru-домен в личном списке — основной случай, а не исключение.

    Список описывает то, чему нужен РОССИЙСКИЙ адрес, и туда добавляют ровно то,
    что не попало в базовый: региональный банк, магазин, госсервис."""
    c = make_active_client()
    res = services.routing_add_domains(c.id, "example.ru")
    assert res.added == ["example.ru"]


def test_denylist_blocks_own_infrastructure(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "routing_denylist", lambda: ["vpn.example.org"])
    c = make_active_client()
    res = services.routing_add_domains(c.id, "mail.vpn.example.org good.com")
    assert res.added == ["good.com"]
    assert dict(res.rejected)["mail.vpn.example.org"] == "этот домен добавлять нельзя"


def test_limit_is_enforced(services, make_active_client, monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: 2 if "user_domains_max" in key else default)
    c = make_active_client()
    res = services.routing_add_domains(c.id, "a.com b.com c.com d.com")
    assert res.added == ["a.com", "b.com"]
    assert res.over_limit == 2
    assert res.limit == 2


def test_remove_and_clear(services, make_active_client):
    c = make_active_client()
    services.routing_add_domains(c.id, "a.com b.com")
    assert services.routing_remove_domain(c.id, "a.com") is True
    assert services.routing_remove_domain(c.id, "a.com") is False
    assert services.routing_domains(c.id) == ["b.com"]
    assert services.routing_clear_domains(c.id) == 1
    assert services.routing_domains(c.id) == []


# ── Проекция в dnsmasq ───────────────────────────────────────────────────────

def test_dnsmasq_conf_uses_per_client_set(
        services, make_active_client, fake_routing):
    """Личный домен идёт в набор своего профиля — общего набора нет вовсе."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    assert f"ipset=/bank.com/{config.ROUTING_SET_USER_PREFIX}{c.id}" in fake_routing.conf


def test_dnsmasq_not_rewritten_when_nothing_changed(
        services, make_active_client, fake_routing):
    """Дифф-скип: рестарт dnsmasq роняет кэш ВСЕМ пользователям, дёргать его на
    каждой плановой реконсиляции — портить резолвинг на ровном месте."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    writes = fake_routing.conf_writes
    services.reconcile_routing()
    services.reconcile_routing()
    assert fake_routing.conf_writes == writes


def test_user_toggle_does_not_restart_the_resolver(
        services, make_active_client, fake_routing):
    """Нажатие тумблера не должно трогать конфиг dnsmasq.

    Его применение стоит РЕСТАРТА резолвера, а тот роняет кэш и на секунды
    лишает DNS всех пользователей разом — включая тех, кто ничего не переключал.
    Пока состав конфига зависел от того, у кого режим ВКЛЮЧЁН, каждое нажатие
    любым пользователем переписывало все ~600 строк и перезапускало dnsmasq.
    Снаружи это «включил режим — на минуту всё отвалилось», а для того, кто в
    этот момент резолвил имя, — отказ на ровном месте.

    Теперь конфиг держится на том, кому функция РАЗРЕШЕНА: это решение админа,
    а не пользователя, и меняется оно редко.
    """
    c = make_active_client()
    services.set_routing_allowed(c.id, True)
    _device(services, c)
    services.set_routing_all(c.id, False)                 # выключил сам
    services.reconcile_routing()

    writes = fake_routing.conf_writes
    services.set_routing_all(c.id, True)
    services.set_routing_all(c.id, False)
    services.set_routing_all(c.id, True)
    assert fake_routing.conf_writes == writes, "тумблер перезапустил резолвер"


def test_allowed_but_switched_off_profile_marks_nothing(
        services, make_active_client, fake_routing):
    """Прогретый набор назначений безопасен при выключенном тумблере.

    Разделение составов держится на том, что правило требует ОБОИХ совпадений:
    источник в rt_src_u<N> И назначение в vpn_u<N>. У выключенного профиля
    src-набор обязан быть пустым — иначе прогрев набора назначений начал бы
    метить трафик тому, кто режим не включал.
    """
    c = make_active_client()
    services.set_routing_allowed(c.id, True)
    _device(services, c)
    services.set_routing_all(c.id, False)                 # выключил сам
    services.reconcile_routing()

    assert f"{config.ROUTING_SET_USER_PREFIX}{c.id}" in fake_routing.sets, \
        "набор назначений должен существовать и прогреваться заранее"
    assert not fake_routing.sets.get(_srcset(c.id)), \
        "у выключенного профиля src-набор обязан быть пуст"


# ── Досев набора при добавлении домена ───────────────────────────────────────

def test_added_domain_is_preseeded_into_the_set(
        services, make_active_client, fake_routing):
    """Домен добавляют ровно тогда, когда сайт только что не открылся, — адрес
    уже в кэше браузера, и клиент переспросит DNS не скоро. Если ждать его
    запроса, набор стоит пустым и сайт продолжает не открываться: «добавил, но
    не работает; потом само заработало». Поэтому резолвим сами.
    """
    from awgbot.infra import routing
    c = make_active_client()
    fake_routing.dns["bank.com"] = ["203.0.113.7", "203.0.113.8"]
    services.routing_add_domains(c.id, "bank.com")

    members = fake_routing.sets[routing.user_set(c.id)]
    assert "203.0.113.7" in members and "203.0.113.8" in members


def test_preseed_keeps_what_dnsmasq_collected(
        services, make_active_client, fake_routing):
    """Досев ДОПИСЫВАЕТ: в наборе живут адреса, накопленные dnsmasq по запросам
    пользователя, и перезапись оставила бы маркировать нечем."""
    from awgbot.infra import routing
    c = make_active_client()
    services.routing_add_domains(c.id, "first.com")
    fake_routing.sets[routing.user_set(c.id)].append("198.51.100.1")   # «от dnsmasq»

    fake_routing.dns["second.com"] = ["203.0.113.9"]
    services.routing_add_domains(c.id, "second.com")
    assert "198.51.100.1" in fake_routing.sets[routing.user_set(c.id)]


def test_failed_resolve_does_not_break_the_add(
        services, make_active_client, fake_routing):
    """Резолв — не критичный путь: набор всё равно наполнится по запросам
    пользователя, поэтому неудача не должна ронять добавление."""
    def boom(dom, timeout=3.0):
        raise OSError("нет сети")
    from awgbot.infra import routing
    import pytest
    monkey = pytest.MonkeyPatch()
    monkey.setattr(routing.feeds, "resolve_a", lambda dom, timeout=3.0: [])
    try:
        c = make_active_client()
        res = services.routing_add_domains(c.id, "unresolvable.invalid")
        assert res.added == ["unresolvable.invalid"]
    finally:
        monkey.undo()


def test_hot_switch_off_wins_over_a_healthy_gateway(services, fake_routing, monkeypatch):
    """Реконсиляция и тик живости уже однажды разошлись во мнениях и спорили за
    рубильник: один снимал политику, другой немедленно возвращал. Условие «фича
    выключена» обязано перевешивать любой вердикт зонда."""
    from awgbot.core import settings
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert fake_routing.marking is True

    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=None: False if k == "app.routing.enabled"
                        else real(k, d))
    services.routing_liveness_tick()
    services.routing_liveness_tick()
    assert fake_routing.marking is False, "зонд пересилил выключенную фичу"
