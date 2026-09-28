"""E2E: условная маршрутизация в интерфейсе — раздел клиента, тумблеры,
личный список адресов, админское разрешение.

Инфраструктура подменена (fake_routing), поэтому проверяем поведение диалога:
что видит пользователь и что реально меняется в БД.
"""
import pytest

from awgbot.bot.handlers import routing as routing_h
from awgbot.bot.callbacks import RoutingCB
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

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
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=c.id), c, services, FakeState())
    assert cb.answers and cb.answers[0][1] is True         # show_alert «недоступно»
    assert not any(s[0] == "edit_text" for s in nav.sent)


async def test_panel_opens_when_allowed(services, make_active_client, fake_bot):
    c = _allowed_client(services, make_active_client, 71)
    services.add_device(c.id, "Телефон")                  # у разрешённого — сразу в режиме
    cb, nav = _cb(fake_bot, 71)
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=c.id), c, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("🇷🇺 РФ-доступ: вкл на всех"), text
    # переключатель устройства — прямо на экране раздела, без промежуточного
    assert labels[0] == "✅ Телефон", labels
    assert labels[1:] == ["✅ Выбрать все", "➕ Сайт", "📋 Сайты", "⬅️ Назад"], labels


async def test_revoked_permission_blocks_stale_button(
        services, make_active_client, fake_bot):
    """У человека открыт экран со старыми кнопками, а разрешение уже отозвали —
    действие должно отбиться, а не сработать."""
    c = _allowed_client(services, make_active_client, 72)
    services.set_routing_allowed(c.id, False)
    c = services.db.get_client(c.id)
    cb, _ = _cb(fake_bot, 72)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=c.id), c, services)
    assert cb.answers[0][1] is True
    assert services.routing_profile_on(c.id) is False


# ── тумблеры ─────────────────────────────────────────────────────────────────

async def test_bulk_toggle_flips_and_persists(services, make_active_client, fake_bot):
    """Массовое действие с экрана устройств. Направление выводится из состояния:
    выключено — включаем всё, включено хоть что-то — выключаем всё."""
    c = _allowed_client(services, make_active_client, 73)
    services.add_device(c.id, "Телефон")                  # включено с рождения
    cb, _ = _cb(fake_bot, 73)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=c.id), c, services)
    assert services.routing_profile_on(c.id) is False

    c = services.db.get_client(c.id)
    cb2, _ = _cb(fake_bot, 73)
    await routing_h.routing_all_toggle(cb2, RoutingCB(action="all", ref=c.id), c, services)
    assert services.routing_profile_on(c.id) is True


# ── личный список ────────────────────────────────────────────────────────────

async def test_add_domains_reports_each_line(services, make_active_client, fake_bot):
    """Итог пачки — первой строкой экрана «📋 Сайты»: что взято, что нет и
    почему. Человек вставляет списком; молча взять половину — он не узнает,
    какой сайт так и открывается с зарубежного адреса."""
    c = _allowed_client(services, make_active_client, 77)
    st = FakeState()
    cb, nav = _cb(fake_bot, 77)
    await routing_h.routing_add_start(cb, RoutingCB(action="add", ref=c.id), c, services, st)
    msg = FakeMessage(text="https://www.bank.com/x\nсбер.мусор_\nnetflix.com",
                      chat_id=77, user_id=77, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, st)
    out = [s for s in msg.sent if s[0] == "answer"]
    assert len(out) == 1, "итог — не отдельным сообщением, а первой строкой экрана"
    first, rest = out[0][1].split("\n\n", 1)
    assert first.startswith("✅ Добавлено: bank.com, netflix.com · ⚠️ Не добавлено: сбер.мусор_ — "), first
    assert first.endswith("применится в теч. минуты, не сработало — переподключись"), first
    assert rest.startswith("📋 Свои сайты · 2"), "после ввода — экран «Сайты», откуда пришли"
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


async def test_delete_answers_with_a_popup_and_redraws_sites_in_place(
        services, make_active_client, fake_bot):
    """«➖» — сразу, без подтверждения: итог всплывашкой, список «Сайты»
    перерисован на месте. Сообщение-след на каждый убранный адрес засоряло
    бы чат."""
    c = _allowed_client(services, make_active_client, 78)
    services.set_routing_all(c.id, True)
    services.routing_add_domains(c.id, "megafon.ru\nozon.ru")
    c = services.db.get_client(c.id)
    cb, nav = _cb(fake_bot, 78)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=c.id, idx=0), c, services)
    assert cb.answers == [("megafon.ru убран · применится в теч. минуты", False)], cb.answers
    assert not any(s[0] == "answer" for s in nav.sent), "след в чате вместо всплывашки"
    text, labels = last_screen(nav)
    assert text.startswith("📋 Свои сайты · 1") and labels[0] == "➖ ozon.ru", (text, labels)
    assert "➖ megafon.ru" not in labels


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
    await routing_h.routing_clear_ask(cb, RoutingCB(action="clear", ref=c.id), c, services)
    assert any("Удалить" in str(s[1]) for s in nav.sent if s[0] == "edit_text")
    assert services.routing_domains(c.id) == ["a.com", "b.com"]   # ещё не тронуто

    cb2, _ = _cb(fake_bot, 80)
    await routing_h.routing_clear_apply(cb2, RoutingCB(action="clear_yes", ref=c.id), c, services)
    assert services.routing_domains(c.id) == []


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

    # «Шлюзы» при выключенной функции — только «Включить» и выход
    off = [b.text for row in kb.gateways_kb((), enabled=False).inline_keyboard for b in row]
    assert off == ["✅ Включить", "⬅️ В меню"], off

    # при включённой — «👥 Кому доступен», профили в НЁМ
    on = [b.text for row in kb.gateways_kb(()).inline_keyboard for b in row]
    assert "👥 Кому доступен" in on
    assert not any("К1" in t or "К2" in t for t in on), "профили не на экране «Шлюзы»"
    users = [b.text for row in kb.settings_routing_users(clients).inline_keyboard
             for b in row]
    assert "✅ К1" in users and "☑️ К2" in users    # кружок = состояние разрешения


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
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=0), None, services)

    assert services.routing_profile_on(c.id) is True
    assert services.db.routing_active_addresses(config.ADMIN_ID) == {c.id: [dc.address]}


async def test_admin_panel_opens_without_client_in_context(services, make_active_client,
                                                           fake_bot):
    """Раздел РФ-доступа открывается админу, хотя client=None."""
    from awgbot.core import config
    make_active_client(tg_id=config.ADMIN_ID)
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=0), None, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("🇷🇺 РФ-доступ") and "➕ Сайт" in labels and "📋 Сайты" in labels, (text, labels)
    assert nav.sent[-1][2].inline_keyboard[-1][0].callback_data == "m:main"


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
    assert out.splitlines()[1] == "🟢 VPN работает · 🇷🇺 РФ-доступ выкл", out
    on = texts.greeting_client(c, True, (1, 3), True, routing_on=True)
    assert on.splitlines()[1] == "🟢 VPN работает · 🇷🇺 РФ-доступ 🟢", on
    broken = texts.greeting_client(c, True, (1, 3), False, routing_on=True)
    assert broken.splitlines()[1] == "🟢 VPN работает · 🇷🇺 РФ-доступ 🔴 не работает", broken


async def test_admin_toggles_client_master(services, make_active_client, fake_bot):
    """Админ переключает РФ-доступ ЧУЖОГО профиля: без этого разбор проблемы
    упирался бы в «включи у себя и перезайди»."""
    c = _allowed_client(services, make_active_client, 96)
    services.add_device(c.id, "Телефон")                  # включено с рождения
    cb, _ = _cb(fake_bot, 1)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=c.id), None, services)
    assert services.routing_profile_on(c.id) is False

    cb2, _ = _cb(fake_bot, 1)
    await routing_h.routing_all_toggle(cb2, RoutingCB(action="all", ref=c.id), None, services)
    assert services.routing_profile_on(c.id) is True


async def test_admin_master_refused_without_grant(services, make_active_client, fake_bot):
    c = make_active_client(tg_id=97)                      # разрешения нет
    cb, _ = _cb(fake_bot, 1)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=0), None, services)
    assert cb.answers[0][1] is True
    assert services.routing_profile_on(c.id) is False


def test_client_menu_button_position_and_state(monkeypatch):
    """Кнопка «🇷🇺 РФ-доступ» — в одном ряду с «💳 Подписка», слева; без
    кружка: состояние — строкой на главной, кнопка только ведёт в раздел.
    Не выдан — кнопки нет, «Подписка» одна в ряду."""
    from awgbot.bot import keyboards as kb
    rows = [[b.text for b in row] for row in kb.client_main(
        has_devices=True, routing_visible=True, client_id=1).inline_keyboard]
    assert ["🇷🇺 РФ-доступ", "💳 Подписка"] in rows, rows
    off = [[b.text for b in row] for row in kb.client_main(
        has_devices=True, routing_visible=False, client_id=1).inline_keyboard]
    assert ["💳 Подписка", "❓ Как подключить"] in off and not any("РФ" in t for r in off for t in r), off


def test_admin_card_button_above_block(monkeypatch):
    """В карточке профиля «🇷🇺 РФ-доступ» стоит в одном ряду с «🛑 Блок» и
    слева от него: это настройка, а не карательное действие. Без кружка —
    состояние режима на экране раздела и строкой карточки."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb
    c = models.Client(id=7, tg_id=500, name="Клиент", device_limit=1, block_reason=0,
                      is_service=0, activation_status="active", invite_code=None,
                      created_at="2026-01-01", routing_allowed=1)
    rows = [[b.text for b in row] for row in kb.admin_client_actions(
        c, routing_visible=True).inline_keyboard]
    assert ["🇷🇺 РФ-доступ", "🛑 Блок"] in rows, rows
    hidden = [b.text for row in kb.admin_client_actions(c, routing_visible=False).inline_keyboard
              for b in row]
    assert not any("РФ" in t for t in hidden) and "🛑 Блок" in hidden, hidden


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
    """У админа «🇷🇺 РФ-доступ» — в одном ряду с «📱 Мои устройства», без
    кружка: он такой же пользователь VPN, и режим ему нужен там же, где
    остальным. Не выдан — кнопки нет, «Мои устройства» одна в ряду."""
    from awgbot.bot import keyboards as kb
    rows = [[b.text for b in row] for row in kb.admin_main(
        routing_visible=True, self_client_id=2).inline_keyboard]
    assert rows[0] == ["📱 Мои устройства", "🇷🇺 РФ-доступ"], rows
    off = [[b.text for b in row] for row in kb.admin_main().inline_keyboard]
    assert off[0] == ["📱 Мои устройства"] and not any("РФ" in t for r in off for t in r), off


async def test_add_domains_without_dialog_context_lands_on_main_with_the_report(
        services, make_active_client, fake_bot):
    """Ввод пришёл, а экрана-контекста в диалоге нет (диалог старого образца,
    перезапуск бота): адреса всё равно приняты, итог — первой строкой главной,
    а не тупик без кнопок."""
    c = _allowed_client(services, make_active_client, 99)
    services.set_routing_all(c.id, True)
    c = services.db.get_client(c.id)
    msg = FakeMessage(text="bank.com", chat_id=99, user_id=99, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, FakeState())

    sent = [s for s in msg.sent if s[0] == "answer"]
    assert len(sent) == 1 and sent[0][2] is not None, sent
    first, rest = sent[0][1].split("\n\n", 1)
    assert first.startswith("✅ Добавлено: bank.com"), first
    assert rest.startswith("👋 "), "без контекста — главная роли"
    assert services.routing_domains(c.id) == ["bank.com"]


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


async def test_settings_section_is_always_shown_but_its_content_depends_on_provisioning(
        services, fake_bot, monkeypatch):
    """Раздел показывается ВСЕГДА: пока обвязка не развёрнута, он и есть место,
    где её разворачивают. А вот содержимое разное — экран развёртывания, экран
    «интерфейс задан, но линка нет» или обычные переключатели.
    """
    from awgbot.bot import keyboards as kb, texts
    from awgbot.bot.handlers import settings as sh
    from awgbot.core import config

    # вход — «🛰 Шлюзы» на главной, всегда; в корне настроек его больше нет
    from awgbot.bot.callbacks import SetCB
    main = [b for row in kb.admin_main().inline_keyboard for b in row]
    assert any(b.text == "🛰 Шлюзы" and b.callback_data == SetCB(sec="rt").pack() for b in main)
    labels = [b.text for row in kb.settings_root().inline_keyboard for b in row]
    assert not any("РФ-доступ" in l for l in labels), labels

    monkeypatch.setattr(config, "ROUTING_ENABLED", False)
    monkeypatch.setattr(services, "routing_provisioned", lambda: False)
    text, markup = await sh._screen("rt", services)
    assert text == texts.ROUTING_PROVISION_INTRO
    assert any("Развернуть" in b.text for row in markup.inline_keyboard for b in row)

    # Интерфейс уже вписан, но процесс ещё не перезапускался — честно говорим,
    # что функция спит, а не предлагаем развернуть ещё раз поверх готового.
    monkeypatch.setattr(services, "routing_provisioned", lambda: True)
    text, _ = await sh._screen("rt", services)
    assert text == texts.SETTINGS_ROUTING_ABSENT


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


async def test_new_device_of_a_granted_profile_comes_on(
        services, make_active_client, fake_routing):
    """Профилю разрешён РФ-доступ — новое устройство сразу в режиме: обещано
    «включено для всех твоих устройств». Без разрешения — выключено."""
    c = _allowed_client(services, make_active_client, 121)
    old = services.add_device(c.id, "Телефон")
    new = services.add_device(c.id, "Ноутбук")
    assert services.db.get_device(new.device_id).routing_on == 1
    assert services.routing_device_counts(c.id) == (2, 2)
    assert set(fake_routing.sets[_srcset(c.id)]) == {old.address, new.address}

    plain = make_active_client(tg_id=122)                 # разрешения нет
    d = services.add_device(plain.id, "Тел")
    assert services.db.get_device(d.device_id).routing_on == 0
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
    """Массовый выбор одной кнопкой «Выбрать все»: ☑️, пока включены не все
    (и частично — доводить набор до полного обычное действие); ✅, когда
    включены все, — её нажатие снимает всё."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb

    def _dev(i, on):
        return models.Device(id=i, client_id=1, name=f"D{i}", private_key="k",
                             public_key=f"p{i}", preshared_key="s",
                             address=f"10.8.1.{i}", block_reason=0,
                             routing_on=on, created_at="2026-01-01")

    def bulk(devs):
        on = sum(d.routing_on for d in devs)
        mk = kb.routing_panel(1, devs, enabled=on, total=len(devs), back_target="m:main")
        return [b.text for row in mk.inline_keyboard for b in row if "Выбрать все" in b.text]

    assert bulk([_dev(1, 0), _dev(2, 0)]) == ["☑️ Выбрать все"]
    assert bulk([_dev(1, 1), _dev(2, 0)]) == ["☑️ Выбрать все"]
    assert bulk([_dev(1, 1), _dev(2, 1)]) == ["✅ Выбрать все"]


def test_devices_screen_uses_checkmarks_not_status_dots():
    """Список с отметками — это выбор, и читаться должен как выбор адресатов
    объявления. Кружки _chk остаются там, где кнопка показывает СОСТОЯНИЕ
    чего-то одного, а не отмеченность в списке."""
    from awgbot.core import models
    from awgbot.bot import keyboards as kb

    def _dev(i, on):
        return models.Device(id=i, client_id=1, name=f"D{i}", private_key="k",
                             public_key=f"p{i}", preshared_key="s",
                             address=f"10.8.1.{i}", block_reason=0,
                             routing_on=on, created_at="2026-01-01")

    labels = [b.text for row in kb.routing_panel(
        1, [_dev(1, 1), _dev(2, 0)], enabled=1, total=2, back_target="m:main").inline_keyboard
        for b in row]
    assert "✅ D1" in labels and "☑️ D2" in labels
    assert not any(x.startswith(("🟢", "🔴")) for x in labels), labels


async def test_bulk_completes_partial_selection(services, make_active_client, fake_bot):
    """При частичном включении массовое действие ДОВОДИТ до полного, а не гасит.

    Иначе подпись и действие расходятся: кнопка говорит «включить все», а
    нажатие выключало бы уже включённое.
    """
    c = _allowed_client(services, make_active_client, 130)
    services.add_device(c.id, "Телефон")
    d2 = services.add_device(c.id, "Ноутбук")
    services.set_routing_device(d2.device_id, False)      # одно выключили руками
    assert services.routing_device_counts(c.id) == (1, 2)

    cb, _ = _cb(fake_bot, 130)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=c.id), c, services)
    assert services.routing_device_counts(c.id) == (2, 2)

    cb2, _ = _cb(fake_bot, 130)
    await routing_h.routing_all_toggle(cb2, RoutingCB(action="all", ref=c.id), c, services)
    assert services.routing_device_counts(c.id) == (0, 2)
    assert d2 is not None


async def test_admin_panel_back_goes_to_main_not_own_card(services, make_active_client,
                                                          fake_bot, monkeypatch):
    """Из раздела РФ-доступа админ возвращается на главную, а не в свою карточку.

    Профиль админа убран из списка профилей, вход в раздел — прямо с главной;
    карточки как экрана в текущей навигации попросту нет.
    """
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    admin = make_active_client(tg_id=config.ADMIN_ID)
    other = _allowed_client(services, make_active_client, 131)

    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=admin.id),
                                  None, services, FakeState())
    back = nav.sent[-1][2].inline_keyboard[-1][0].callback_data
    assert back == "m:main", back

    cb2, nav2 = _cb(fake_bot, config.ADMIN_ID)
    await routing_h.routing_panel(cb2, RoutingCB(action="panel", ref=other.id),
                                  None, services, FakeState())
    back2 = nav2.sent[-1][2].inline_keyboard[-1][0].callback_data
    assert back2 == f"c:open:{other.id}", back2


# ── раздел маршрутизации в настройках админа: списки, бандл, подразделы ──────

async def test_bundle_document_carries_menu_button_and_dims_settings(
        services, make_active_client, fake_bot, monkeypatch):
    """Бандл уходит с кнопкой «В меню», а экран настроек гаснет: живым остаётся
    одно меню — на самом бандле."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    monkeypatch.setattr(services, "gw_bundle_encrypted", lambda slot=None: (b"AWGGWB1\nxx", "b.enc"))
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    sent_docs = []

    async def answer_document(doc, caption=None, reply_markup=None, **kw):
        sent_docs.append((caption, reply_markup)); return msg
    msg.answer_document = answer_document
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle"), services)
    assert sent_docs and sent_docs[0][1] is not None, "у бандла нет кнопки «В меню»"
    assert any(r[0] == "edit_reply_markup" for r in fake_bot.records), "экран настроек не погашен"
    assert msg.message_id in services.db.pop_content_msg_ids(cfg.ADMIN_ID), \
        "инструкция не помечена как контент — «В меню» её не удалит"


async def test_bundle_menu_button_deletes_the_file_message(services, fake_bot, monkeypatch):
    """«В меню» на бандле до 3.1.0 (`bundle_menu`) удаляет само сообщение с
    файлом (внутри ключ линка), а не снимает клавиатуру, как общая кнопка
    обновлений; дальше — главная."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="bundle_menu"), services)
    assert msg.deleted, "сообщение с бандлом не удалено"
    assert any(r[0] == "answer" for r in fake_bot.records), "меню не показано"


def test_routing_lists_info_reports_count_age_and_period(services, monkeypatch):
    """Сводка по спискам: записи, возраст обновления, период — то, чего в чате
    не было вовсе, пока списки обновлялись молча."""
    import time
    from awgbot.core import settings
    monkeypatch.setattr(services, "_routing_read_cache", lambda name: ["a.ru", "b.ru"])
    monkeypatch.setattr(settings, "get", lambda k, d=None: 12 if k.endswith("lists_refresh_hours") else d)
    services.db.set_state(services._RT_LISTS_KEY, str(int(time.time()) - 7200))
    info = services.routing_lists_info()
    assert info["count"] == 2 and info["every_hours"] == 12
    assert 7000 <= info["age_seconds"] <= 7300
    from awgbot.bot import texts
    info["sources"] = 3
    line = texts.routing_params_text({"probe_seconds": 30, "window": 10, "availability": 50, "need": 5,
                                      "standby_minutes": 10, "interval_minutes": 30}, info).split("\n")[2]
    assert line.startswith("Списки: 2 ") and " из 3 источников, 2 ч назад · раз в 12 ч" in line, line


async def test_routing_lists_info_and_controls(services, fake_bot, monkeypatch):
    """Пикер периода пишет горячий ключ; «обновить сейчас» зовёт обновление с force."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.core import settings
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    written = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [])
    forced = []
    monkeypatch.setattr(services, "routing_update_lists", lambda force=False: forced.append(force) or 7)
    async def noop_render(cb, sec, services_): pass
    monkeypatch.setattr(sh, "_render", noop_render)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)

    await sh.pick(cb, SetCB(sec="rt", act="pick", key="lists", val="12"), services)
    assert written["app.routing.lists_refresh_hours"] == 12

    cb.answers.clear()
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="lists_refresh"), services)
    assert forced == [True], "«обновить сейчас» не форсирует обновление"
    # ровно один ответ на колбэк: второй Telegram не показывает
    assert len(cb.answers) == 1 and "7 записей" in (cb.answers[0][0] or "")

def test_routing_section_buttons_depend_on_gateway():
    """«🛰 Шлюзы»: слоты кнопками («⭐» у предпочтительного), при одном —
    «➕ Резерв» рядом; при двух — «▶️ Переключить на <резерв>» и тумблеры;
    без шлюза — «🛰 Назначить»; «Кому доступен» и «Параметры» при включённой
    функции всегда, «⬅️ В меню» — последней."""
    from types import SimpleNamespace
    from awgbot.bot import keyboards as kb
    from awgbot.bot.callbacks import GwSlotCB
    one = [{"gateway": SimpleNamespace(id=1), "device": SimpleNamespace(name="NASPi"),
            "active": True, "link_ok": True, "preferred": False, "issued_at": "", "handshake_age": 3}]
    rows = [[b.text for b in row] for row in kb.gateways_kb(one).inline_keyboard]
    assert rows == [["NASPi", "➕ Резерв"], ["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]], rows
    rows = [[b.text for b in row] for row in kb.gateways_kb(one, can_add=False).inline_keyboard]
    assert rows[0] == ["NASPi"], "потолок слотов — без «➕ Резерв»"
    two = [dict(one[0], preferred=True),
           {"gateway": SimpleNamespace(id=2), "device": SimpleNamespace(name="Pi4"),
            "active": False, "link_ok": True, "preferred": False, "issued_at": "", "handshake_age": 5}]
    markup = kb.gateways_kb(two, can_add=False, failover_on=True, peer_nets_on=False)
    rows = [[b.text for b in row] for row in markup.inline_keyboard]
    assert rows == [["⭐ NASPi", "Pi4"], ["▶️ Переключить на Pi4"], ["✅ Автопереключение", "☑️ Связь подсетей"],
                    ["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]], rows
    assert markup.inline_keyboard[1][0].callback_data == GwSlotCB(action="switch_ask", slot=2, val="l").pack(), \
        "со списка: отмена подтверждения вернёт в «Шлюзы»"
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        GwSlotCB(action="card", slot=1).pack(), GwSlotCB(action="card", slot=2).pack()]
    rows = [[b.text for b in row] for row in kb.gateways_kb([]).inline_keyboard]
    assert rows == [["🛰 Назначить"], ["👥 Кому доступен", "⚙️ Параметры"], ["⬅️ В меню"]], rows
    off = [b.text for row in kb.gateways_kb((), enabled=False).inline_keyboard for b in row]
    assert off == ["✅ Включить", "⬅️ В меню"]

    params = [b.text for row in kb.settings_routing_lists(6).inline_keyboard for b in row]
    assert "🔄 Списки: 6 ч" in params and "⬇️ Обновить списки" in params


async def test_routing_subsections_render_and_are_empty_when_off(services, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.core import settings, config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: True)
    monkeypatch.setattr(services, "routing_lists_info",
                        lambda: {"count": 3, "updated_at": None, "age_seconds": 7200,
                                 "every_hours": 12, "sources": 2})
    monkeypatch.setattr(services, "routing_grantable_clients", lambda: [])
    monkeypatch.setattr(services, "routing_monitor_info", lambda: {
        "probe_seconds": 30, "window": 10, "availability": 50, "need": 5,
        "standby_minutes": 10, "interval_minutes": 30})
    # старые подразделы «Списки» и «Мониторинг» ведут в «⚙️ Параметры»
    for sec in ("rt_lists", "rt_mon", "rt_params"):
        text, markup = await sh._screen(sec, services)
        assert text.startswith("⚙️ Параметры РФ-доступа\n"), (sec, text)
        assert "Списки: 3 записи из 2 источников, 2 ч назад · раз в 12 ч" in text.split("\n"), text
    text, markup = await sh._screen("rt_users", services)
    assert text.startswith("👥 Кому доступен РФ-доступ (тебе — всегда)"), text
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: False)
    text, markup = await sh._screen("rt_lists", services)
    assert text == "🇷🇺 РФ-доступ выключен — раздел пуст, пока он не включён", text


async def test_bundle_button_in_the_card_issues_the_file_without_an_intro_screen(services, fake_bot, monkeypatch):
    """«⚙️ Конфигурация шлюза» в карточке слота ведёт на выпуск этого слота, а
    промежуточного экрана «что произойдёт» больше нет: раздел `rt_bundle`
    ничего не рисует (из старого сообщения — обычные настройки)."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot import keyboards as kb
    from awgbot.core import settings as st
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROUTING_ENABLED", True)
    monkeypatch.setattr(st, "get_bool", lambda key, default=False: True)
    from types import SimpleNamespace
    from awgbot.bot.callbacks import GwSlotCB
    state = {"gateway": SimpleNamespace(id=1, home_subnets=[], label="", lan_mode=0), "device": SimpleNamespace(name="NASPi"),
             "active": True, "link_ok": True, "preferred": True, "issued_at": "", "handshake_age": 3}
    markup = kb.gateway_card(state, back_to_list=False)
    btn = [b for row in markup.inline_keyboard for b in row if "Конфигурация" in b.text][0]
    assert btn.callback_data == GwSlotCB(action="bundle", slot=1).pack()
    assert not hasattr(kb, "settings_routing_bundle"), "клавиатура упразднённого экрана вернулась"
    text, _markup = await sh._screen("rt_bundle", services, "1")
    assert "Что произойдёт" not in text, "промежуточный экран перед выпуском вернулся"


# ── админ в ЧУЖОМ разделе: правки уходят тому профилю, чья панель открыта ────

def _admin_cb(bot):
    from awgbot.core import config
    return _cb(bot, config.ADMIN_ID)


async def _foreign_setup(services, make_active_client, monkeypatch, tg=140):
    """Админ со своим профилем и чужой профиль с разрешением, режимом и парой
    адресов. Возвращает (admin, other)."""
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    admin = make_active_client(tg_id=config.ADMIN_ID)
    services.add_device(admin.id, "Мой")
    other = _allowed_client(services, make_active_client, tg)
    services.add_device(other.id, "Чужой")
    services.set_routing_all(other.id, True)
    services.routing_add_domains(other.id, "a.ru\nb.ru")
    return admin, other


async def test_admin_edits_foreign_list_not_his_own(services, make_active_client,
                                                    fake_bot, monkeypatch):
    """Из панели чужого профиля «➕ / ➖ / 🗑» правят ЧУЖОЙ список. Раньше все
    четыре действия брали профиль «чей чат» — то есть самого админа: адреса
    клиента уходили админу, а «удалить a.ru» стирало его собственный."""
    admin, other = await _foreign_setup(services, make_active_client, monkeypatch)

    cb, _ = _admin_cb(fake_bot)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=other.id, idx=0),
                                   None, services)
    assert services.routing_domains(other.id) == ["b.ru"]
    assert services.routing_domains(admin.id) == []

    st = FakeState()
    cb, _ = _admin_cb(fake_bot)
    await routing_h.routing_add_start(cb, RoutingCB(action="add", ref=other.id),
                                      None, services, st)
    msg = FakeMessage(text="z.ru", chat_id=admin.tg_id, user_id=admin.tg_id, bot=fake_bot)
    await routing_h.routing_add_apply(msg, None, services, st)
    assert set(services.routing_domains(other.id)) == {"b.ru", "z.ru"}
    assert services.routing_domains(admin.id) == []
    # после приёма — «Сайты» чужого профиля, «Назад» — в его раздел
    sites = [s for s in msg.sent if s[0] == "answer" and s[2] is not None][-1]
    assert sites[2].inline_keyboard[-1][0].callback_data == RoutingCB(action="panel", ref=other.id).pack()
    assert "➖ z.ru" in [b.text for row in sites[2].inline_keyboard for b in row]

    cb, _ = _admin_cb(fake_bot)
    await routing_h.routing_clear_apply(cb, RoutingCB(action="clear_yes", ref=other.id),
                                        None, services)
    assert services.routing_domains(other.id) == []


async def test_admin_own_list_without_ref_and_client_cannot_reach_foreign(
        services, make_active_client, fake_bot, monkeypatch):
    """Без ref админ правит свой список (вход с главной). Клиент с чужим ref в
    кнопке всё равно правит только свой: чужой id для него не существует."""
    admin, other = await _foreign_setup(services, make_active_client, monkeypatch, tg=141)
    services.set_routing_all(admin.id, True)
    st = FakeState()
    cb, _ = _admin_cb(fake_bot)
    await routing_h.routing_add_start(cb, RoutingCB(action="add", ref=0), None, services, st)
    msg = FakeMessage(text="mine.ru", chat_id=admin.tg_id, user_id=admin.tg_id, bot=fake_bot)
    await routing_h.routing_add_apply(msg, None, services, st)
    assert services.routing_domains(admin.id) == ["mine.ru"]

    other = services.db.get_client(other.id)
    cb, _ = _cb(fake_bot, other.tg_id)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=admin.id, idx=0),
                                   other, services)
    assert services.routing_domains(admin.id) == ["mine.ru"], "клиент удалил у админа"
    assert services.routing_domains(other.id) == ["b.ru"]


async def test_device_switches_consistent_for_admin_and_client(
        services, make_active_client, fake_bot, monkeypatch):
    """Экран устройств одного профиля одинаков для клиента и админа, и
    переключения любого из них видны обоим — состояние одно, в БД."""
    admin, other = await _foreign_setup(services, make_active_client, monkeypatch, tg=142)
    d2 = services.add_device(other.id, "Второй")
    services.set_routing_device(d2.device_id, False)      # выключили руками
    other = services.db.get_client(other.id)

    def labels(nav):
        return [b.text for row in nav.sent[-1][2].inline_keyboard for b in row]

    cb_a, nav_a = _admin_cb(fake_bot)
    await routing_h.routing_panel(cb_a, RoutingCB(action="devs", ref=other.id),
                                  None, services, FakeState())
    cb_c, nav_c = _cb(fake_bot, other.tg_id)
    await routing_h.routing_panel(cb_c, RoutingCB(action="devs", ref=other.id),
                                  other, services, FakeState())
    assert labels(nav_a) == labels(nav_c)
    assert any(l.startswith("✅ Чужой") for l in labels(nav_a))
    assert any(l.startswith("☑️ Второй") for l in labels(nav_a))

    # админ включил второе — клиент видит; клиент выключил первое — админ видит
    cb, _ = _admin_cb(fake_bot)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=d2.device_id),
                                          None, services)
    cb_c, nav_c = _cb(fake_bot, other.tg_id)
    await routing_h.routing_panel(cb_c, RoutingCB(action="devs", ref=other.id),
                                  other, services, FakeState())
    assert all(l.startswith("✅") for l in labels(nav_c) if "Чужой" in l or "Второй" in l)

    first = [d for d in services.db.list_devices(other.id) if d.name == "Чужой"][0]
    cb, _ = _cb(fake_bot, other.tg_id)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=first.id),
                                          other, services)
    cb_a, nav_a = _admin_cb(fake_bot)
    await routing_h.routing_panel(cb_a, RoutingCB(action="devs", ref=other.id),
                                  None, services, FakeState())
    assert any(l.startswith("☑️ Чужой") for l in labels(nav_a))
    assert services.routing_device_counts(other.id) == (1, 2)


async def test_feature_toggle_blocks_both_editors_and_keeps_device_flags(
        services, make_active_client, fake_bot, monkeypatch):
    """Разрешение отозвано — раздел закрыт и клиенту, и админу в его панели;
    вернули — флаги устройств и список адресов как были."""
    admin, other = await _foreign_setup(services, make_active_client, monkeypatch, tg=143)
    before = services.routing_device_counts(other.id)
    services.set_routing_allowed(other.id, False)
    other = services.db.get_client(other.id)

    cb, nav = _cb(fake_bot, other.tg_id)
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=0), other, services,
                                  FakeState())
    assert cb.answers and cb.answers[0][1] is True
    cb, nav = _admin_cb(fake_bot)
    await routing_h.routing_delete(cb, RoutingCB(action="del", ref=other.id, idx=0),
                                   None, services)
    assert cb.answers and cb.answers[0][1] is True
    assert services.routing_domains(other.id) == ["a.ru", "b.ru"]

    services.set_routing_allowed(other.id, True)
    assert services.routing_device_counts(other.id) == before
    cb, nav = _admin_cb(fake_bot)
    await routing_h.routing_panel(cb, RoutingCB(action="panel", ref=other.id), None,
                                  services, FakeState())
    text, labels = last_screen(nav)
    assert "Свои сайты: a.ru, b.ru" in text and "📋 Сайты: 2" in labels, (text, labels)
    cb, nav = _admin_cb(fake_bot)
    await routing_h.routing_sites(cb, RoutingCB(action="sites", ref=other.id), None,
                                  services, FakeState())
    _, labels = last_screen(nav)
    assert labels[:2] == ["➖ a.ru", "➖ b.ru"], labels


# ── выдача разрешения: включает все устройства и уведомляет владельца ────────

async def test_grant_enables_all_devices_and_notifies_owner(services, make_active_client,
                                                            fake_bot, monkeypatch):
    """Админ выдал доступ — режим сразу на всех устройствах профиля (обещано в
    уведомлении), владельцу приходит уведомление. Отзыв — уведомление, флаги
    устройств не трогаются; повторное нажатие того же — без уведомления."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot import texts
    from awgbot.core import config
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    c = make_active_client(tg_id=150)
    services.add_device(c.id, "Тел"); services.add_device(c.id, "Ноут")
    assert services.routing_device_counts(c.id) == (0, 0), "без разрешения устройств в режиме нет"

    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    fake_bot.records.clear()
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow", val=str(c.id)), services)
    assert services.routing_device_counts(c.id) == (2, 2)
    sent = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 150]
    assert sent and sent[0][2] == texts.ROUTING_GRANTED_NOTICE

    services.set_routing_device([d for d in services.db.list_devices(c.id)][0].id, False)
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    fake_bot.records.clear()
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="allow", val=str(c.id)), services)
    assert services.db.get_client(c.id).routing_allowed == 0
    assert services.routing_device_counts(c.id) == (0, 0), "без разрешения — ничего не в режиме"
    assert sum(d.routing_on for d in services.db.list_devices(c.id)) == 1, "отзыв стёр флаги устройств"
    sent = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 150]
    assert sent and sent[0][2] == texts.ROUTING_REVOKED_NOTICE

    assert services.set_routing_allowed(c.id, False) == [], "то же состояние — молча"


def test_pending_friend_device_has_hourglass_icon(services, make_active_client):
    from awgbot.bot import keyboards as kb, texts
    c = make_active_client(tg_id=151, device_limit=3)
    services.add_device(c.id, "Своё")
    friend = services.add_device(c.id, "Другу")
    services.make_device_friendly(friend.device_id)
    devs = {d.name: d for d in services.db.list_devices(c.id)}
    assert texts.device_emoji(devs["Своё"]) == "📱"
    assert texts.device_emoji(devs["Другу"]) == "⏳"
    labels = [b.text for row in kb.client_devices(devs.values()).inline_keyboard for b in row]
    # на кнопках — значок состояния: ⏳ ждёт друга, ⚪ не подключалось
    assert "⏳ Другу" in labels and "⚪ Своё" in labels, labels


# ── общий выключатель фичи: выключение через подтверждение ──────────────────

async def test_global_switch_off_needs_confirmation_and_on_is_immediate(
        services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.core import config, settings
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    state = {"app.routing.enabled": True}
    real_bool = settings.get_bool
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: state.get(k, real_bool(k, d)))
    monkeypatch.setattr(settings, "set_value",
                        lambda k, v: state.__setitem__(k, v) or [k])
    monkeypatch.setattr(services, "reconcile_routing", lambda: None)

    # включено → нажатие показывает вопрос, значение не тронуто
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await sh.toggle(cb, SetCB(sec="rt", act="toggle", key="app.routing.enabled"), services)
    text, labels = last_screen(nav)
    assert text.startswith("🔴 Выключить РФ-доступ для всех?"), text
    assert labels == ["⬅️ Отмена", "🔴 Выключить"], labels
    assert state["app.routing.enabled"] is True

    # подтверждение — выключено, раздел перерисован
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await sh.routing_action(cb, SetCB(sec="rt", act="do", key="off!"), services)
    assert state["app.routing.enabled"] is False
    assert cb.answers and "выключен" in cb.answers[0][0]

    # выключено → нажатие включает сразу, без вопроса; шлюз тут же замеряется:
    # пока фича спала, о нём молчали (и на старте тоже), лежит — сказать сейчас
    from types import SimpleNamespace
    from awgbot.infra import routing as rt
    monkeypatch.setattr(services.db, "gateways", lambda: [SimpleNamespace(id=1)])
    monkeypatch.setattr(services, "routing_status", lambda: (True, ""))

    async def _no_render(cb, sec, services_):        # раздел рисует полную карточку шлюза — не о нём тест
        pass
    monkeypatch.setattr(sh, "_render", _no_render)
    monkeypatch.setattr(services, "routing_probe", lambda: rt.PROBE_DOWN)
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await sh.toggle(cb, SetCB(sec="rt", act="toggle", key="app.routing.enabled"), services)
    assert state["app.routing.enabled"] is True
    said = [s[1] for s in nav.sent if s[0] == "answer"]
    assert said and said[-1].startswith("⚠️ шлюз РФ-доступа не отвечает при включении"), said

    # шлюз отвечает — молчим
    await sh.routing_action(_cb(fake_bot, config.ADMIN_ID)[0],
                            SetCB(sec="rt", act="do", key="off!"), services)
    monkeypatch.setattr(services, "routing_probe", lambda: rt.PROBE_OK)
    cb, nav = _cb(fake_bot, config.ADMIN_ID)
    await sh.toggle(cb, SetCB(sec="rt", act="toggle", key="app.routing.enabled"), services)
    assert state["app.routing.enabled"] is True
    assert not [s for s in nav.sent if s[0] == "answer" and "шлюз" in (s[1] or "")]


async def test_device_switch_and_select_all_redraw_the_section_in_place(
        services, make_active_client, fake_bot):
    """Переключатель устройства — прямо на экране раздела: нажатие
    перерисовывает его на месте, итог — всплывашкой. «Выбрать все» — ☑️,
    пока включены не все; включили вручную последнее — ✅; нажатие на ✅
    выключает все."""
    c = _allowed_client(services, make_active_client, 160)
    a = services.add_device(c.id, "iPhone")
    b = services.add_device(c.id, "MacBook")
    c = services.db.get_client(c.id)
    cb, nav = _cb(fake_bot, 160)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=b.device_id), c, services)
    assert cb.answers[-1] == ("выключено", False)
    text, labels = last_screen(nav)
    assert text.startswith("🇷🇺 РФ-доступ: вкл на 1 из 2"), text
    assert labels[:3] == ["✅ iPhone", "☑️ MacBook", "☑️ Выбрать все"], labels
    assert not any(s[0] == "answer" for s in nav.sent), "экран не на месте"

    cb, nav = _cb(fake_bot, 160)
    await routing_h.routing_device_toggle(cb, RoutingCB(action="dev", ref=b.device_id), c, services)
    assert last_screen(nav)[1][:3] == ["✅ iPhone", "✅ MacBook", "✅ Выбрать все"], \
        "включили по одному — «Выбрать все» не стала ✅"

    cb, nav = _cb(fake_bot, 160)
    await routing_h.routing_all_toggle(cb, RoutingCB(action="all", ref=c.id), c, services)
    assert cb.answers[-1] == ("Выключено на всех", False)
    text, labels = last_screen(nav)
    assert labels[:3] == ["☑️ iPhone", "☑️ MacBook", "☑️ Выбрать все"], labels
    assert text.startswith("🇷🇺 РФ-доступ: выкл\nВключишь — банки, госуслуги"), text
    assert services.routing_device_counts(c.id) == (0, 2) and a is not None
