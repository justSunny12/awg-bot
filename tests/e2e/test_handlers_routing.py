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
    await routing_h.routing_all_toggle(cb, c, services)
    assert cb.answers[0][1] is True
    assert services.routing_profile_on(c.id) is False


# ── тумблеры ─────────────────────────────────────────────────────────────────

async def test_bulk_toggle_flips_and_persists(services, make_active_client, fake_bot):
    """Массовое действие с экрана устройств. Направление выводится из состояния:
    выключено — включаем всё, включено хоть что-то — выключаем всё."""
    c = _allowed_client(services, make_active_client, 73)
    services.add_device(c.id, "Телефон")
    cb, _ = _cb(fake_bot, 73)
    await routing_h.routing_all_toggle(cb, c, services)
    assert services.routing_profile_on(c.id) is True

    c = services.db.get_client(c.id)
    cb2, _ = _cb(fake_bot, 73)
    await routing_h.routing_all_toggle(cb2, c, services)
    assert services.routing_profile_on(c.id) is False


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
    services.set_routing_all(admin.id, True)
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
    await routing_h.routing_all_toggle(cb, None, services)

    assert services.routing_profile_on(c.id) is True
    assert services.db.routing_active_addresses(config.ADMIN_ID) == {c.id: [dc.address]}


async def test_admin_panel_opens_without_client_in_context(services, make_active_client,
                                                           fake_bot):
    """Раздел РФ-доступа открывается админу, хотя client=None."""
    from awgbot.core import config
    make_active_client(tg_id=config.ADMIN_ID)
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_panel(cb, None, services, FakeState())
    assert any(s[0] == "edit_text" for s in nav.sent)


def test_status_line_appears_for_everyone_granted(services, make_active_client):
    """Строка о РФ-шлюзе — в общем статусном блоке, рядом со статусом сервера.

    Показывается всем, кому админ функцию разрешил, — в том числе когда режим у
    человека выключен: она отвечает на вопрос «а работает ли оно вообще»,
    который иначе задаётся заходом в раздел, и полезнее всего как раз перед
    включением. Не разрешил — строки нет вовсе: рассказывать про механизм тому,
    кому он недоступен, значит шуметь.
    """
    from awgbot.bot import texts
    c = make_active_client(tg_id=95)
    assert services.routing_health_for_client(c) is None
    assert "РФ-доступ" not in texts.greeting_client(c, True, (1, 3), None)

    services.set_routing_allowed(c.id, True)
    c = services.db.get_client(c.id)
    ok = services.routing_health_for_client(c)
    assert ok is not None, "разрешено — строка обязана быть, даже при выключенном режиме"
    out = texts.greeting_client(c, True, (1, 3), ok)
    assert "🇷🇺 РФ-доступ:" in out


async def test_admin_toggles_client_master(services, make_active_client, fake_bot):
    """Админ переключает РФ-доступ ЧУЖОГО профиля: без этого разбор проблемы
    упирался бы в «включи у себя и перезайди»."""
    c = _allowed_client(services, make_active_client, 96)
    services.add_device(c.id, "Телефон")
    cb, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_all(cb, RoutingCB(action="all", ref=c.id), services)
    assert services.routing_profile_on(c.id) is True

    cb2, _ = _cb(fake_bot, 1)
    await admin_h.admin_routing_all(cb2, RoutingCB(action="all", ref=c.id), services)
    assert services.routing_profile_on(c.id) is False


async def test_admin_master_refused_without_grant(services, make_active_client, fake_bot):
    c = make_active_client(tg_id=97)                      # разрешения нет
    cb, _ = _cb(fake_bot, 1)
    await routing_h.routing_all_toggle(cb, None, services)
    assert cb.answers[0][1] is True
    assert services.routing_profile_on(c.id) is False


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
                      created_at="2026-01-01", routing_allowed=1)
    labels = [b.text for row in kb.admin_client_actions(
        c, has_devices=True, routing_visible=True,
        routing_on=True).inline_keyboard for b in row]
    rt = next(i for i, t in enumerate(labels) if "РФ-сервисам" in t)
    blk = next(i for i, t in enumerate(labels) if "локировать" in t)
    assert rt < blk, labels
    assert labels[rt].startswith("🟢")

    # Состояние ВЫВОДИТСЯ из устройств, поэтому клавиатура его получает
    # параметром, а не читает из профиля: колонки под него больше нет.
    off = [b.text for row in kb.admin_client_actions(
        c, has_devices=True, routing_visible=True,
        routing_on=False).inline_keyboard for b in row]
    assert next(x for x in off if "РФ-сервисам" in x).startswith("🔴")


# ── режим — свойство профиля, не устройства ──────────────────────────────────

def test_device_card_has_no_routing_toggle(monkeypatch):
    """В КАРТОЧКЕ устройства тумблера нет — и это не значит, что режим не
    пер-девайсный.

    Переключатели живут на отдельном экране (RoutingCB action=devs): там всё
    состояние профиля видно разом и рядом лежит массовое действие. В карточке
    они терялись бы среди выдачи конфигов, лимитов и блокировок, а охват
    профиля не был бы виден нигде."""
    from awgbot.core import config, models
    from awgbot.bot import keyboards as kb
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    d = models.Device(id=1, client_id=7, name="Телефон", private_key="k",
                      public_key="p", preshared_key="s", address="10.8.1.2",
                      block_reason=0, created_at="2026-01-01")
    labels = [b.text for row in kb.device_actions(
        d, is_admin=False, back_target="m:devices").inline_keyboard for b in row]
    assert not any("РФ" in t for t in labels), labels


def test_admin_main_has_routing_under_devices(monkeypatch):
    """У админа кнопка в главном меню — сразу под «Мои устройства»: он такой же
    пользователь VPN, и режим ему нужен там же, где остальным."""
    from awgbot.core import config
    from awgbot.bot import keyboards as kb
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    labels = [b.text for row in kb.admin_main(
        0, self_has_devices=True, routing_visible=True, routing_on=True,
        self_client_id=2).inline_keyboard for b in row]
    assert "🟢 Доступ к РФ-сервисам" in labels
    assert labels.index("🟢 Доступ к РФ-сервисам") == labels.index("📱 Мои устройства") + 1

    # не разрешена — кнопки нет вовсе
    off = [b.text for row in kb.admin_main(0, self_has_devices=True).inline_keyboard
           for b in row]
    assert not any("РФ" in t for t in off)


async def test_add_domains_returns_to_panel(services, make_active_client, fake_bot):
    """После приёма адресов возвращаемся в раздел, а не оставляем тупик без
    кнопок: раньше диалог кончался отчётом, и приглашение «пришли адреса» так и
    висело в чате."""
    c = _allowed_client(services, make_active_client, 99)
    services.set_routing_all(c.id, True)
    c = services.db.get_client(c.id)
    msg = FakeMessage(text="bank.com", chat_id=99, user_id=99, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, FakeState())

    sent = [s for s in msg.sent if s[0] == "answer"]
    assert any("bank.com" in str(s[1]) for s in sent), "нет отчёта"
    # последнее сообщение — раздел с кнопками
    assert any("РФ-доступ" in str(s[1]) for s in sent), sent


async def test_revoked_permission_blocks_the_pending_input(
        services, make_active_client, fake_bot):
    """Между «пришли адреса» и отправкой списка админ мог отозвать разрешение.
    Состояние FSM про это не знает, поэтому проверять надо и здесь — иначе
    список примется у того, кому фича больше не положена."""
    c = _allowed_client(services, make_active_client, 91)
    services.set_routing_allowed(c.id, False)          # отозвали, пока он печатал
    c = services.db.get_client(c.id)

    msg = FakeMessage(text="bank.com", chat_id=91, user_id=91, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, FakeState())

    assert services.routing_domains(c.id) == [], "домен принят после отзыва доступа"


# ── переключатель режима в настройках ────────────────────────────────────────

def _acb(bot):
    from awgbot.core import config as _c
    nav = FakeMessage(chat_id=_c.ADMIN_ID, user_id=_c.ADMIN_ID, bot=bot)
    return FakeCallback(message=nav, user_id=_c.ADMIN_ID, bot=bot), nav


async def test_settings_section_hidden_without_a_gateway(services, fake_bot, monkeypatch):
    """Раздел настроек существует, только если РФ-шлюз сконфигурирован.

    Кнопку при пустом gw_interface не рисуем, но колбэк приходит и из старого
    сообщения в истории чата. Открыть раздел, которого нет, значит показать
    переключатели, ничего не делающие.
    """
    from awgbot.bot import keyboards as kb, texts
    from awgbot.bot.handlers import settings as sh
    from awgbot.core import config

    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    labels = [b.text for row in kb.settings_root().inline_keyboard for b in row]
    assert not any("маршрутизация" in l for l in labels), labels

    text, _ = await sh._screen("rt", services)
    assert text == texts.SETTINGS_ROUTING_ABSENT

    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    labels = [b.text for row in kb.settings_root().inline_keyboard for b in row]
    assert any("маршрутизация" in l for l in labels), labels


# ── пер-девайсные переключатели ──────────────────────────────────────────────

async def test_device_toggle_touches_only_that_device(
        services, make_active_client, fake_bot, fake_routing):
    """Переключатель одного устройства не задевает соседей.

    Ради этого всё и затевалось: в один профиль попадают устройства с разными
    требованиями — например, шлюз, которому маршрутизация противопоказана,
    рядом с телефоном, которому она нужна.
    """
    c = _allowed_client(services, make_active_client, 120)
    phone = services.add_device(c.id, "Телефон")
    gw = services.add_device(c.id, "Шлюз")
    services.set_routing_all(c.id, True)

    cb, _ = _cb(fake_bot, 120)
    await routing_h.routing_device_toggle(
        cb, RoutingCB(action="dev", ref=gw.device_id), c, services)

    assert services.db.get_device(gw.device_id).routing_on == 0
    assert services.db.get_device(phone.device_id).routing_on == 1
    assert fake_routing.sets[_srcset(c.id)] == [phone.address]
    assert services.routing_device_counts(c.id) == (1, 2)


async def test_new_device_stays_off_even_when_others_are_on(
        services, make_active_client, fake_routing):
    """Новое устройство приходит ВЫКЛЮЧЕННЫМ, даже если у профиля режим включён.

    Наследовать «включено» опаснее: устройство, которому маршрутизация не нужна,
    начало бы ходить через шлюз молча. Промах в обратную сторону виден — на
    кнопке профиля стоит счётчик «включено N из M».
    """
    c = _allowed_client(services, make_active_client, 121)
    old = services.add_device(c.id, "Телефон")
    services.set_routing_all(c.id, True)

    new = services.add_device(c.id, "Ноутбук")
    assert services.db.get_device(new.device_id).routing_on == 0
    assert services.routing_device_counts(c.id) == (1, 2)
    assert fake_routing.sets[_srcset(c.id)] == [old.address]


async def test_device_toggle_rejects_foreign_device(
        services, make_active_client, fake_bot):
    """Чужой device_id из старого сообщения не должен переключаться."""
    a = _allowed_client(services, make_active_client, 122)
    b = _allowed_client(services, make_active_client, 123)
    victim = services.add_device(b.id, "Чужой")
    services.set_routing_all(b.id, True)

    cb, _ = _cb(fake_bot, 122)
    await routing_h.routing_device_toggle(
        cb, RoutingCB(action="dev", ref=victim.device_id), a, services)

    assert cb.answers[0][1] is True
    assert services.db.get_device(victim.device_id).routing_on == 1


def test_devices_screen_bulk_button_follows_state():
    """Кнопка массового действия одна, и её смысл зависит от состояния: пока
    включено хоть что-то, осмысленно только выключить всё."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb

    def _dev(i, on):
        return models.Device(id=i, client_id=1, name=f"D{i}", private_key="k",
                             public_key=f"p{i}", preshared_key="s",
                             address=f"10.8.1.{i}", block_reason=0,
                             routing_on=on, created_at="2026-01-01")

    off = kb.routing_devices(1, [_dev(1, 0), _dev(2, 0)], back_target="m:main")
    assert off.inline_keyboard[0][0].text.endswith("Включить все")

    mixed = kb.routing_devices(1, [_dev(1, 1), _dev(2, 0)], back_target="m:main")
    assert mixed.inline_keyboard[0][0].text.endswith("Выключить все")
