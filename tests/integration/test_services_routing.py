"""Условная маршрутизация: трёхслойный флаг, личные списки, реконсиляция,
деградация при недоступном шлюзе.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock

pytestmark = pytest.mark.integration


def _device(services, client, name="Телефон"):
    return services.add_device(client.id, name)


# ── Трёхслойный флаг ─────────────────────────────────────────────────────────

def test_all_three_layers_required(services, db, make_active_client, fake_routing):
    """Адрес попадает в набор только когда подняты ВСЕ три слоя."""
    c = make_active_client()
    dc = _device(services, c)
    src = config.ROUTING_SET_SRC

    services.set_device_routing(dc.device_id, True)          # только нижний
    assert fake_routing.sets.get(src) == []

    services.set_routing_master(c.id, True)                  # + мастер клиента
    assert fake_routing.sets.get(src) == []

    services.set_routing_allowed(c.id, True)                 # + разрешение админа
    assert fake_routing.sets[src] == [dc.address]


def test_enabled_device_is_exempted_from_masquerade(
        services, make_active_client, fake_routing):
    """Плечо контейнера: включённое устройство должно выходить немаскараженным,
    иначе на хосте его не отличить от остальных и маркировать будет нечего."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    services.set_device_routing(dc.device_id, True)
    assert fake_routing.nat_exempt == [dc.address]

    services.set_device_routing(dc.device_id, False)
    assert fake_routing.nat_exempt == []


def test_revoking_permission_kills_effect_but_keeps_flags(
        services, db, make_active_client, fake_routing):
    """Отзыв разрешения гасит эффект, но НЕ разрушает настройку пользователя:
    вернули разрешение — всё как было, перенастраивать нечего."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    services.set_device_routing(dc.device_id, True)
    assert fake_routing.sets[config.ROUTING_SET_SRC] == [dc.address]

    services.set_routing_allowed(c.id, False)
    assert fake_routing.sets[config.ROUTING_SET_SRC] == []
    assert db.get_client(c.id).routing_master == 1            # нижние слои целы
    assert db.get_device(dc.device_id).routing_enabled == 1

    services.set_routing_allowed(c.id, True)
    assert fake_routing.sets[config.ROUTING_SET_SRC] == [dc.address]


def test_master_toggle_switches_all_devices_at_once(
        services, make_active_client, fake_routing):
    c = make_active_client()
    d1, d2 = _device(services, c, "A"), _device(services, c, "B")
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    services.set_device_routing(d1.device_id, True)
    services.set_device_routing(d2.device_id, True)
    assert sorted(fake_routing.sets[config.ROUTING_SET_SRC]) == sorted(
        [d1.address, d2.address])

    services.set_routing_master(c.id, False)
    assert fake_routing.sets[config.ROUTING_SET_SRC] == []


def test_blocked_device_stays_in_set(services, make_active_client, fake_routing):
    """Заблокированное устройство из набора НЕ убираем: DROP по его адресу стоит
    раньше стадии маркировки. Дублировать инвариант блокировок вторым механизмом
    значило бы завести источник расхождения."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_master(c.id, True)
    services.set_device_routing(dc.device_id, True)

    services._device_set_block(dc.device_id, DeviceBlock.USER)
    services.reconcile_routing()
    assert fake_routing.sets[config.ROUTING_SET_SRC] == [dc.address]


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


def test_domain_in_base_zone_is_refused_with_explanation(services, make_active_client):
    """.ru уже покрыт базовой директивой — добавлять незачем, и человеку надо
    сказать об этом, иначе он решит, что кнопка не работает."""
    c = make_active_client()
    res = services.routing_add_domains(c.id, "sberbank.ru")
    assert res.added == []
    assert "базов" in dict(res.rejected)["sberbank.ru"]


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

def test_dnsmasq_conf_has_base_zones_and_user_domains(
        services, make_active_client, fake_routing):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    conf = fake_routing.conf
    assert "ipset=/ru/" in conf
    assert f"ipset=/bank.com/{config.ROUTING_SET_USER_PREFIX}{c.id}" in conf


def test_dnsmasq_not_rewritten_when_nothing_changed(
        services, make_active_client, fake_routing):
    """Дифф-скип: рестарт dnsmasq роняет кэш ВСЕМ клиентам, дёргать его на
    каждой плановой реконсиляции — портить резолвинг на ровном месте."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    writes = fake_routing.conf_writes
    services.reconcile_routing()
    services.reconcile_routing()
    assert fake_routing.conf_writes == writes


# ── Наборы и цепочка ─────────────────────────────────────────────────────────

def test_orphan_sets_of_deleted_client_are_destroyed(
        services, db, make_active_client, fake_routing):
    """Осиротевший набор однажды совпал бы по имени с новым client_id — и чужие
    домены достались бы другому человеку."""
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    user_set = f"{config.ROUTING_SET_USER_PREFIX}{c.id}"
    assert user_set in fake_routing.sets

    db.delete_client(c.id)
    services.reconcile_routing()
    assert user_set not in fake_routing.sets


def test_chain_lists_only_live_clients(services, make_active_client, fake_routing):
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    assert fake_routing.chain == [c.id]


# ── Деградация при недоступном шлюзе ─────────────────────────────────────────

def test_link_down_disables_marking_and_alerts_admin_once(
        services, fake_routing):
    """Шлюз лёг → маркировка снимается, трафик уходит обычным путём. Админа
    уведомляем один раз на смену состояния, а не каждый тик."""
    fake_routing.link_age = 10
    services.routing_monitor()                       # первый тик: состояние ок
    assert fake_routing.marking is True

    fake_routing.link_age = 99999                    # хендшейк протух
    notes = services.routing_monitor()
    assert fake_routing.marking is False
    assert len(notes) == 1 and "недоступен" in notes[0].text
    assert services.routing_monitor() == []          # повтор — молчим

    fake_routing.link_age = 5                        # вернулся
    notes = services.routing_monitor()
    assert fake_routing.marking is True
    assert len(notes) == 1 and "строю" in notes[0].text


def test_no_handshake_at_all_counts_as_down(services, fake_routing):
    fake_routing.link_age = None
    services.routing_monitor()
    assert fake_routing.marking is False


def test_feature_disabled_is_a_no_op(services, make_active_client, fake_routing):
    """Фича выключена в конфиге — реконсиляция ничего не трогает и не падает."""
    fake_routing.enabled = False
    c = make_active_client()
    services.reconcile_routing()
    assert fake_routing.chain is None
    assert services.routing_monitor() == []
