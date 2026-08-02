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


def _srcset(client_id):
    """Имя src-набора клиента: общего набора больше нет — каждый включённый
    профиль получает своё правило и свой набор адресов."""
    from awgbot.infra import routing as _rt
    return _rt.src_set(client_id)


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
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


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

# ── карточка админского профиля ──────────────────────────────────────────────

def test_profile_cards_have_no_routing_controls(monkeypatch):
    """Управление РФ-доступом живёт в настройках, а не в карточках профилей:
    это настройка сервиса, а не свойство клиента. В карточках его быть не должно
    ни у обычного профиля, ни у админского."""
    from awgbot.core import config, models
    from awgbot.bot import keyboards as kb

    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    client = models.Client(id=5, tg_id=1, name="Профиль", device_limit=0,
                           block_reason=0, is_service=0, activation_status="active",
                           invite_code=None, created_at="2026-01-01")
    for is_owner in (True, False):
        markup = kb.admin_client_actions(client, has_devices=True,
                                         is_admin_owner=is_owner)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert not any("РФ-доступ" in t for t in labels), \
            f"кнопка осталась в карточке при is_admin_owner={is_owner}"


def test_settings_screen_lists_clients_only_when_enabled():
    """Список профилей показываем только при включённой функции: раздавать
    разрешения на выключённое — приглашение к «я же разрешил, почему не работает»."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb

    clients = [models.Client(id=i, tg_id=100 + i, name=f"К{i}", device_limit=0,
                             block_reason=0, is_service=0, activation_status="active",
                             invite_code=None, created_at="2026-01-01",
                             routing_allowed=i % 2)
               for i in (1, 2)]

    off = [b.text for row in kb.settings_routing(False, clients).inline_keyboard
           for b in row]
    assert not any("К1" in t or "К2" in t for t in off)
    assert any("🔴" in t and "Условная маршрутизация" in t for t in off)

    on = [b.text for row in kb.settings_routing(True, clients).inline_keyboard
          for b in row]
    assert "🟢 К1" in on and "🔴 К2" in on          # кружок = состояние разрешения
    assert any("🟢" in t and "Условная маршрутизация" in t for t in on)


async def test_grant_from_settings_screen(services, make_active_client, fake_bot):
    """Выдача разрешения из настроек — тот же верхний слой флага."""
    from awgbot.bot.handlers import settings as settings_h
    from awgbot.bot.callbacks import SetCB
    from awgbot.core import config

    c = make_active_client(tg_id=91)
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await settings_h.routing_action(
        cb, SetCB(sec="rt", act="do", key="allow", val=str(c.id)), services)
    assert services.db.get_client(c.id).routing_allowed == 1

    cb2, _ = _cb(fake_bot, config.ADMIN_ID)
    await settings_h.routing_action(
        cb2, SetCB(sec="rt", act="do", key="allow", val=str(c.id)), services)
    assert services.db.get_client(c.id).routing_allowed == 0


def test_admin_has_access_without_grant(services, make_active_client):
    """Админу разрешение не требуется — он его сам и выдаёт. Иначе пришлось бы
    отмечать галочку себе, а в списке профилей появилась бы бессмысленная строка."""
    from awgbot.core import config
    admin = make_active_client(tg_id=config.ADMIN_ID)
    assert admin.routing_allowed == 0
    assert services.routing_allowed_for(admin) is True
    assert admin.id not in [c.id for c in services.routing_grantable_clients()]

    dc = services.add_device(admin.id, "Телефон")
    services.set_routing_master(admin.id, True)
    services.set_device_routing(dc.device_id, True)
    assert services.db.routing_active_addresses(config.ADMIN_ID) == {admin.id: [dc.address]}


async def test_admin_can_enable_feature_for_himself(services, make_active_client,
                                                    fake_bot, monkeypatch):
    """Middleware отдаёт админу role=admin и client=None, поэтому клиентский
    роутер должен уметь достать его собственный профиль сам — иначе админ мог бы
    раздавать доступ другим, но не включить функцию себе."""
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    c = make_active_client(tg_id=config.ADMIN_ID)
    dc = services.add_device(c.id, "Телефон")

    # client=None — ровно то, что придёт из middleware для админа
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_master(cb, None, services)
    cb2, _ = _cb(fake_bot, config.ADMIN_ID)
    await admin_h.admin_routing_device(cb2, RoutingCB(action="dev", ref=dc.device_id), services)

    assert services.db.get_client(c.id).routing_master == 1
    assert services.db.get_device(dc.device_id).routing_enabled == 1
    assert services.db.routing_active_addresses(config.ADMIN_ID) == {c.id: [dc.address]}


async def test_admin_panel_opens_without_client_in_context(services, make_active_client,
                                                           fake_bot):
    """Раздел РФ-доступа открывается админу, хотя client=None."""
    from awgbot.core import config
    make_active_client(tg_id=config.ADMIN_ID)
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_panel(cb, None, services, FakeState())
    assert any(s[0] == "edit_text" for s in nav.sent)


def test_infobox_line_appears_only_when_granted(services, make_active_client):
    """Строка «Доступ к РФ-сервисам» появляется, как только админ разрешил, и
    показывает положение переключателя самого клиента."""
    from awgbot.bot import texts
    c = make_active_client(tg_id=95)
    assert services.routing_access_for_client(c) is None      # не разрешено — строки нет
    assert "РФ-сервисам" not in texts.greeting_client(c, True, (1, 3), None, None)

    services.set_routing_allowed(c.id, True)
    c = services.db.get_client(c.id)
    assert services.routing_access_for_client(c) is False      # разрешено, но выключено
    out = texts.greeting_client(c, True, (1, 3), None, False)
    assert "Доступ к РФ-сервисам" in out and "🔴 выключен" in out

    services.set_routing_master(c.id, True)
    c = services.db.get_client(c.id)
    assert services.routing_access_for_client(c) is True
    assert "🟢 включен" in texts.greeting_client(c, True, (1, 3), None, True)


# ── админ управляет клиентскими настройками ──────────────────────────────────

async def test_admin_toggles_client_master(services, make_active_client, fake_bot):
    """Админ переключает РФ-доступ ЧУЖОГО профиля: без этого разбор проблемы
    упирался бы в «включи у себя и перезайди»."""
    c = _allowed_client(services, make_active_client, 96)
    cb, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_master(cb, RoutingCB(action="master", ref=c.id), services)
    assert services.db.get_client(c.id).routing_master == 1

    cb2, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_master(cb2, RoutingCB(action="master", ref=c.id), services)
    assert services.db.get_client(c.id).routing_master == 0


async def test_admin_master_refused_without_grant(services, make_active_client, fake_bot):
    c = make_active_client(tg_id=97)                      # разрешения нет
    cb, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_master(cb, RoutingCB(action="master", ref=c.id), services)
    assert cb.answers[0][1] is True
    assert services.db.get_client(c.id).routing_master == 0


async def test_admin_toggles_client_device(services, make_active_client, fake_bot,
                                           fake_routing):
    """И тумблер на УСТРОЙСТВЕ чужого профиля — тоже админу."""
    from awgbot.core import config
    c = _allowed_client(services, make_active_client, 98)
    services.set_routing_master(c.id, True)
    dc = services.add_device(c.id, "Чужой телефон")

    cb, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_device(cb, RoutingCB(action="dev", ref=dc.device_id), services)
    assert services.db.get_device(dc.device_id).routing_enabled == 1
    assert fake_routing.sets[_srcset(c.id)] == [dc.address]


def test_client_menu_button_position_and_state(monkeypatch):
    """Кнопка «Доступ к РФ-сервисам» — сразу под «Управлять подпиской», с кружком."""
    from awgbot.bot import keyboards as kb
    labels = [b.text for row in kb.client_main(
        has_devices=True, routing_visible=True, client_id=1, routing_on=True
    ).inline_keyboard for b in row]
    assert "🟢 Доступ к РФ-сервисам" in labels
    assert labels.index("🟢 Доступ к РФ-сервисам") == labels.index("⚙️ Управлять подпиской") + 1

    off = [b.text for row in kb.client_main(
        has_devices=True, routing_visible=True, client_id=1, routing_on=False
    ).inline_keyboard for b in row]
    assert "🔴 Доступ к РФ-сервисам" in off


def test_admin_card_button_above_block(monkeypatch):
    """В карточке профиля кнопка стоит НАД «Заблокировать»: это настройка,
    а не карательное действие."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb
    c = models.Client(id=7, tg_id=500, name="Клиент", device_limit=1, block_reason=0,
                      is_service=0, activation_status="active", invite_code=None,
                      created_at="2026-01-01", routing_allowed=1, routing_master=1)
    labels = [b.text for row in kb.admin_client_actions(
        c, has_devices=True, routing_visible=True).inline_keyboard for b in row]
    rt = next(i for i, t in enumerate(labels) if "РФ-сервисам" in t)
    blk = next(i for i, t in enumerate(labels) if "локировать" in t)
    assert rt < blk, labels
    assert labels[rt].startswith("🟢")
