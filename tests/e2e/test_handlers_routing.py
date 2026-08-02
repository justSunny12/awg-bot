"""E2E: условная маршрутизация в интерфейсе — раздел клиента, тумблеры,
личный список адресов, админское разрешение.

Инфраструктура подменена (fake_routing), поэтому проверяем поведение диалога:
что видит пользователь и что реально меняется в БД.
"""
import pytest

from awgbot.bot.handlers import admin as admin_h
from awgbot.bot.handlers import routing as routing_h
from awgbot.bot.callbacks import RoutingCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid, data=""):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(data=data, message=nav, user_id=uid, bot=bot), nav


def _allowed_client(services, make_active_client, tg_id):
    """Клиент с выданным разрешением админа (верхний слой флага)."""
    c = make_active_client(tg_id=tg_id)
    services.set_routing_allowed(c.id, True)
    return services.db.get_client(c.id)


# ── видимость: без разрешения фичи нет вовсе ─────────────────────────────────

async def test_panel_hidden_without_permission(services, make_active_client, fake_bot):
    """Пока админ не разрешил, фича невидима — иначе каждый первый пойдёт
    спрашивать, что это за пункт и почему не работает."""
    c = make_active_client(tg_id=70)
    cb, nav = _cb(fake_bot, 70)
    await routing_h.routing_panel(cb, c, services, FakeState())
    assert cb.answers and cb.answers[0][1] is True         # show_alert «недоступно»
    assert not any(s[0] == "edit_text" for s in nav.sent)


async def test_panel_opens_when_allowed(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 71)
    cb, nav = _cb(fake_bot, 71)
    await routing_h.routing_panel(cb, c, services, FakeState())
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_revoked_permission_blocks_stale_button(
        services, make_active_client, fake_bot):
    """У человека открыт экран со старыми кнопками, а разрешение уже отозвали —
    действие должно отбиться, а не сработать."""
    c = _allowed_client(services, make_active_client, 72)
    services.set_routing_allowed(c.id, False)
    c = services.db.get_client(c.id)
    cb, _ = _cb(fake_bot, 72)
    await routing_h.routing_master(cb, c, services)
    assert cb.answers[0][1] is True
    assert services.db.get_client(c.id).routing_master == 0


# ── тумблеры ─────────────────────────────────────────────────────────────────

async def test_master_toggle_flips_and_persists(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 73)
    cb, _ = _cb(fake_bot, 73)
    await routing_h.routing_master(cb, c, services)
    assert services.db.get_client(c.id).routing_master == 1

    c = services.db.get_client(c.id)
    cb2, _ = _cb(fake_bot, 73)
    await routing_h.routing_master(cb2, c, services)
    assert services.db.get_client(c.id).routing_master == 0


async def test_device_toggle_applies_to_infrastructure(
        services, make_active_client, fake_bot, fake_routing):
    from awgbot.core import config
    c = _allowed_client(services, make_active_client, 74)
    services.set_routing_master(c.id, True)
    c = services.db.get_client(c.id)
    dc = services.add_device(c.id, "Телефон")

    cb, _ = _cb(fake_bot, 74)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id),
                                          c, services)
    assert services.db.get_device(dc.device_id).routing_enabled == 1
    assert fake_routing.sets[config.ROUTING_SET_SRC] == [dc.address]


async def test_device_toggle_rejects_foreign_device(
        services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 75)
    other = make_active_client(tg_id=76)
    dc = services.add_device(other.id, "Чужой")
    cb, _ = _cb(fake_bot, 75)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id),
                                          c, services)
    assert cb.answers[0][1] is True
    assert services.db.get_device(dc.device_id).routing_enabled == 0


# ── личный список ────────────────────────────────────────────────────────────

async def test_add_domains_reports_each_line(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 77)
    msg = FakeMessage(text="https://www.bank.com/x\nсбер.мусор_\nnetflix.com",
                      chat_id=77, user_id=77, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, FakeState())
    out = "".join(s[1] for s in msg.sent if s[0] == "answer")
    assert "bank.com" in out and "netflix.com" in out
    assert "Не добавлено" in out                    # мусорная строка объяснена
    assert set(services.routing_domains(c.id)) == {"bank.com", "netflix.com"}


async def test_delete_by_stale_index_does_not_remove_wrong_domain(
        services, make_active_client, fake_bot):
    """Индекс из старого экрана не должен удалить не тот адрес: список
    перечитывается, границы проверяются."""
    c = _allowed_client(services, make_active_client, 78)
    services.routing_add_domains(c.id, "a.com b.com")
    cb, _ = _cb(fake_bot, 78)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=c.id, idx=9),
                                   c, services)
    assert services.routing_domains(c.id) == ["a.com", "b.com"]
    assert cb.answers[0][1] is True


async def test_delete_removes_selected_domain(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 79)
    services.routing_add_domains(c.id, "a.com b.com")
    cb, _ = _cb(fake_bot, 79)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=c.id, idx=0),
                                   c, services)
    assert services.routing_domains(c.id) == ["b.com"]


async def test_clear_confirm_then_apply(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 80)
    services.routing_add_domains(c.id, "a.com b.com")
    cb, nav = _cb(fake_bot, 80)
    await routing_h.routing_clear_ask(cb, c, services)
    assert any("Удалить" in str(s[1]) for s in nav.sent if s[0] == "edit_text")
    assert services.routing_domains(c.id) == ["a.com", "b.com"]   # ещё не тронуто

    cb2, _ = _cb(fake_bot, 80)
    await routing_h.routing_clear_apply(cb2, c, services)
    assert services.routing_domains(c.id) == []


# ── админское разрешение ─────────────────────────────────────────────────────

async def test_admin_allow_toggles_permission(services, make_active_client,
                                              fake_bot, monkeypatch):
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    c = make_active_client(tg_id=81)
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await admin_h.client_routing_allow(cb, RoutingCB(action="allow", ref=c.id), services)
    assert services.db.get_client(c.id).routing_allowed == 1


async def test_admin_allow_refuses_when_feature_broken(
        services, make_active_client, fake_bot, fake_routing):
    """Разрешать фичу, которая не поднимется, — обещать пользователю то, чего
    нет: он включит тумблеры и будет ждать эффекта."""
    from awgbot.core import config
    fake_routing.enabled = False
    c = make_active_client(tg_id=82)
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await admin_h.client_routing_allow(cb, RoutingCB(action="allow", ref=c.id), services)
    assert cb.answers[0][1] is True
    assert services.db.get_client(c.id).routing_allowed == 0


async def test_admin_revoke_keeps_user_settings(services, make_active_client,
                                                fake_bot, monkeypatch):
    """Отзыв гасит эффект, но настройку пользователя сохраняет."""
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    c = _allowed_client(services, make_active_client, 83)
    services.set_routing_master(c.id, True)
    dc = services.add_device(c.id, "Телефон")
    services.set_device_routing(dc.device_id, True)

    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await admin_h.client_routing_allow(cb, RoutingCB(action="allow", ref=c.id), services)
    assert services.db.get_client(c.id).routing_allowed == 0
    assert services.db.get_client(c.id).routing_master == 1
    assert services.db.get_device(dc.device_id).routing_enabled == 1
