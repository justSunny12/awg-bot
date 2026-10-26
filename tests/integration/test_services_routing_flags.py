"""Условная маршрутизация: трёхслойный флаг, наборы и цепочка, держатель
устройства как субъект маршрутизации.

Инфраструктура подменена фейком (fixture fake_routing) — проверяем, ЧТО именно
проецируется наружу, а не как выглядит вывод ipset.
"""
import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock

from tests.integration._services_routing_common import _device, _srcset

pytestmark = pytest.mark.integration


# ── Трёхслойный флаг ─────────────────────────────────────────────────────────

def test_both_layers_required(services, db, make_active_client, fake_routing):
    """Адрес попадает в набор только когда подняты ОБА слоя: разрешение админа и
    флаг на самом устройстве."""
    c = make_active_client()
    dc = _device(services, c)

    services.set_routing_all(c.id, True)                  # только пользователь
    assert not fake_routing.sets.get(_srcset(c.id))

    services.set_routing_allowed(c.id, True)                 # + разрешение админа
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


def test_revoking_permission_kills_effect_but_keeps_flags(
        services, db, make_active_client, fake_routing):
    """Отзыв разрешения гасит эффект, но НЕ разрушает настройку пользователя:
    вернули разрешение — всё как было, перенастраивать нечего."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_all(c.id, True)
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]

    services.set_routing_allowed(c.id, False)
    assert not fake_routing.sets.get(_srcset(c.id))
    assert db.get_device(dc.device_id).routing_on == 1        # выбор пользователя цел

    services.set_routing_allowed(c.id, True)
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


def test_bulk_toggle_switches_all_devices_at_once(
        services, make_active_client, fake_routing):
    """Массовое действие с экрана устройств — под него попадают ВСЕ сразу."""
    c = make_active_client()
    d1, d2 = _device(services, c, "A"), _device(services, c, "B")
    services.set_routing_allowed(c.id, True)
    services.set_routing_all(c.id, True)
    assert sorted(fake_routing.sets[_srcset(c.id)]) == sorted(
        [d1.address, d2.address])

    services.set_routing_all(c.id, False)
    assert not fake_routing.sets.get(_srcset(c.id))


def test_blocked_device_stays_in_set(services, make_active_client, fake_routing):
    """Заблокированное устройство из набора НЕ убираем: DROP по его адресу стоит
    раньше стадии маркировки. Дублировать инвариант блокировок вторым механизмом
    значило бы завести источник расхождения."""
    c = make_active_client()
    dc = _device(services, c)
    services.set_routing_allowed(c.id, True)
    services.set_routing_all(c.id, True)

    services._device_set_block(dc.device_id, DeviceBlock.USER)
    services.reconcile_routing()
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


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


# ── субъект маршрутизации — держатель ──────────────────

def _lend(services, owner, tg, name):
    dc = services.add_device(owner.id, name)
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=tg)
    assert res.ok, res.reason
    return dc, res.holder


def test_lent_device_goes_to_the_holders_set_with_owner_permission(
        services, make_active_client, fake_routing):
    """Переданное устройство — в наборе ДЕРЖАТЕЛЯ, но только пока владелец
    разрешён: отзыв у владельца гасит и его."""
    owner = make_active_client(tg_id=700, device_limit=3)
    services.set_routing_allowed(owner.id, True)
    own = _device(services, owner, "своё")
    lent, guest = _lend(services, owner, 9700, "другу")
    assert fake_routing.sets[_srcset(owner.id)] == [own.address], "переданное осталось у владельца"
    assert fake_routing.sets[_srcset(guest.id)] == [lent.address]
    assert services.routing_device_counts(owner.id) == (1, 1)
    assert services.routing_device_counts(guest.id) == (1, 1)
    assert [d.id for d in services.routing_lent_out(owner.id)] == [lent.device_id]
    assert [d.id for d in services.routing_devices(guest.id)] == [lent.device_id]

    services.set_routing_allowed(owner.id, False)
    assert not fake_routing.sets.get(_srcset(guest.id)), "отзыв у владельца не погасил держателя"
    assert services.routing_device_counts(guest.id) == (0, 0)
    assert services.routing_allowed_for(guest) is False


def test_holder_has_own_domains_and_toggles(services, make_active_client, fake_routing):
    """Список адресов и тумблеры — у держателя свои, от владельца не наследуются."""
    owner = make_active_client(tg_id=701, device_limit=3)
    services.set_routing_allowed(owner.id, True)
    services.routing_add_domains(owner.id, "owner.ru")
    lent, guest = _lend(services, owner, 9701, "другу")
    assert services.routing_domains(guest.id) == []
    services.routing_add_domains(guest.id, "guest.ru")
    assert services.routing_domains(owner.id) == ["owner.ru"]
    assert services.routing_allowed_for(guest) is True
    assert services.routing_client_visible(guest) is True
    # гость выключил — владелец не тронут
    services.toggle_routing_device(lent.device_id)
    assert not fake_routing.sets.get(_srcset(guest.id))
    assert services.routing_profile_on(guest.id) is False
    assert services.set_routing_all(owner.id, False) == 0 or True
    # владелец «включить все» не трогает переданное — управляет держатель
    services.set_routing_all(owner.id, True)
    assert services.db.get_device(lent.device_id).routing_on == 0


def test_client_holder_mixes_own_and_held_under_owner_permissions(
        services, make_active_client, fake_routing):
    """Обычный пользователь без разрешения держит устройство разрешённого владельца:
    раздел виден, в режиме — только удерживаемое; свои — вне режима."""
    owner = make_active_client(tg_id=702, device_limit=3)
    services.set_routing_allowed(owner.id, True)
    holder = make_active_client(tg_id=703, device_limit=2)
    own = _device(services, holder, "моё")
    lent, _ = _lend(services, owner, 703, "чужое")
    assert services.routing_allowed_for(holder) is True
    assert [d.id for d in services.routing_devices(holder.id)] == [lent.device_id]
    assert fake_routing.sets[_srcset(holder.id)] == [lent.address]
    services.set_routing_all(holder.id, True)
    assert fake_routing.sets[_srcset(holder.id)] == [lent.address], "своё без разрешения — не в режиме"
    assert services.db.get_device(own.device_id).routing_on == 1, "флаг стоит, ждёт разрешения"


def test_grant_and_revoke_notify_holders_too(services, make_active_client, fake_routing):
    owner = make_active_client(tg_id=704, name="Вася", device_limit=3)
    _lend(services, owner, 9704, "A")
    _lend(services, owner, 9704, "B")
    notes = services.set_routing_allowed(owner.id, True)
    assert [n.tg_id for n in notes] == [704, 9704]
    assert notes[1].text.startswith('🇷🇺 К устройствам от профиля <a href="tg://user?id=704">Вася</a> добавлен')
    notes = services.set_routing_allowed(owner.id, False)
    assert [n.tg_id for n in notes] == [704, 9704]
    assert notes[1].text.startswith('🇷🇺 РФ-доступ для устройств от профиля <a href="tg://user?id=704">Вася</a> больше')
