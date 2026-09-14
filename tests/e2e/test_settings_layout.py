"""E2E: раскладка настроек и значки — порядок разделов, кнопки правок,
порядок фильтров роутера настроек, галочки против кружков, значок шлюза."""
import pytest

from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)



def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ── раскладка корня настроек ─────────────────────────────────────────────────

def test_settings_root_order_and_names():
    """Порядок — от того, что трогают при настройке сервера, к тому, что
    трогают раз в полгода. Мониторинг и бэкапы уехали в «Обслуживание»: корень
    распух до десяти строк, а открывают их не ради настройки, а когда чинят."""
    from awgbot.bot import keyboards as kb
    rows = [b.text for row in kb.settings_root().inline_keyboard for b in row]
    assert rows == ["🔔 Уведомления", "🖥 Сервер AWG", "🛡 Доступ по SSH",
                    "🇷🇺 Условная маршрутизация", "✉️ E-mail", "💳 Параметры подписок",
                    "🔄 Обслуживание", "⬆️ Обновления бота", "⬅️ В меню"]


def test_maintenance_holds_monitoring_and_backups_first():
    from awgbot.bot import keyboards as kb
    rows = [b.text for row in kb.settings_svc().inline_keyboard for b in row]
    assert rows[:4] == ["📊 Мониторинг", "💾 Резервное копирование",
                        "🔄 Перезапустить AWG", "🔄 Перезапустить бота"]


def test_moved_sections_return_to_maintenance():
    """Выход из раздела обязан вести туда, откуда в него вошли, — иначе
    «Назад» выбрасывает в корень, и человек ищет, где он был."""
    from awgbot.bot import keyboards as kb
    for markup in (kb.settings_mon(), kb.settings_backup()):
        assert markup.inline_keyboard[-1][0].callback_data == SetCB(sec="svc").pack()

def test_every_edit_button_points_at_a_known_setting():
    """Опечатка в ключе кнопки превращает её в «Эта настройка недоступна», и
    раздел становится мёртвым. Сверяем ключи клавиатур со словарями текстов."""
    from awgbot.bot import keyboards as kb, texts
    from awgbot.bot.callbacks import SetCB
    known = set(texts.SETTINGS_TEXT) | set(texts.SETTINGS_BOUNDS)
    markups = [kb.settings_server(), kb.settings_firewall({"raw_allow": ["1.2.3.4"]}),
               kb.settings_mon(), kb.settings_subs(), kb.settings_notify()]
    checked = 0
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                if not b.callback_data.startswith("set:"):
                    continue
                cb = SetCB.unpack(b.callback_data)
                if cb.act != "edit":
                    continue
                checked += 1
                assert cb.key in known, f"кнопка «{b.text}» ведёт в несуществующий ключ {cb.key}"
    assert checked >= 8, "проверять оказалось нечего — тест устарел"

def test_gateway_device_keeps_its_icon_in_the_button_list():
    """В текстовых списках шлюз был 🛰, а в кнопках «Мои устройства» — обычным
    телефоном: две функции иконки разошлись. Перепутать шлюз с телефоном там,
    где их удаляют и блокируют, дороже всего."""
    from awgbot.bot import keyboards as kb, texts

    from awgbot.util import timeutil

    class _Traffic:
        last_handshake = int(timeutil.now().timestamp())

    class _Dev:
        id, name, block_reason, friend, traffic_limit = 1, "Шлюз", 0, None, 0
        is_gateway, is_managed, private_key = 1, 1, "priv"
        address, traffic = "10.8.1.5", _Traffic()

    class _Phone(_Dev):
        id, name, is_gateway = 2, "iPhone", 0
        traffic = type("T", (), {"last_handshake": 0})()

    labels = [b.text for row in kb.client_devices([_Dev(), _Phone()]).inline_keyboard
              for b in row]
    assert any(l.startswith("🛰") for l in labels), labels
    assert any(l.startswith("📱") for l in labels), labels
    assert texts.device_emoji(_Dev()) == "🛰", "текстовый список разошёлся с кнопками"
    # Значок ровно один: кружок онлайна тут пробовали и убрали — два подряд в
    # каждой строке превращают список в рябь.
    assert not any("🟢" in l or "🔴" in l for l in labels), labels


def test_routing_domain_list_uses_minus_and_a_bin_for_the_whole_list():
    """Минус убирает одну запись — то же, что в разделе доступа по SSH.
    Корзина остаётся там, где сносят всё разом."""
    from awgbot.bot import keyboards as kb
    markup = kb.routing_panel(1, master_on=True, enabled=1, total=2,
                              domains=["ozon.ru", "mail.ru"], back_target="menu:main")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "➖ ozon.ru" in labels and "➖ mail.ru" in labels
    assert "🗑 Очистить список" in labels
    assert not any(l.startswith("🧹") for l in labels), "метла осталась"

# ── значки состояния: где кружок, где галочка ────────────────────────────────

def test_lists_of_choices_use_ticks_not_circles():
    """Кружок читается как «жив или лежит» — состояние того, что перечислено.
    В списках «кому разрешено» и «о чём уведомлять» нужен знак ВЫБОРА."""
    from awgbot.bot import keyboards as kb

    class _C:
        def __init__(self, cid, allowed):
            self.id, self.name, self.routing_allowed = cid, f"К{cid}", allowed

    labels = [b.text for row in kb.settings_routing_users([_C(1, True), _C(2, False)]).inline_keyboard
              for b in row]
    assert labels[:2] == ["✅ К1", "☑️ К2"]
    notify = [b.text for row in kb.settings_notify_clients().inline_keyboard for b in row]
    assert all(not l.startswith(("🟢", "🔴")) for l in notify), notify
    # а вот у переключателей сервиса кружок остаётся
    rt = [b.text for row in kb.settings_routing(True, has_gateway=True).inline_keyboard for b in row]
    assert rt[0].startswith("🟢"), rt


def test_client_list_circle_means_online_not_subscription():
    """«Кто сейчас в сети» из списка было не узнать, а состояние подписки и так
    видно в карточке. ⏳ остаётся за теми, кто ещё не активировал доступ."""
    from awgbot.bot import keyboards as kb
    from awgbot.core.enums import ActivationStatus, SubStatus

    class _C:
        def __init__(self, cid, name, act=ActivationStatus.ACTIVE, status=SubStatus.ACTIVE):
            self.id, self.name = cid, name
            self.activation_status, self.status = act, status
            self.block_reason = 0

    clients = [_C(1, "Онлайн"), _C(2, "Офлайн"),
               _C(3, "Ждёт", act=ActivationStatus.PENDING),
               _C(4, "Истёк", status=SubStatus.EXPIRED)]
    labels = [b.text for row in kb.admin_clients(clients, online_ids={1, 4}).inline_keyboard
              for b in row]
    assert labels[0].startswith("🟢") and labels[1].startswith("🔴")
    assert labels[2].startswith("⏳")
    assert labels[3].startswith("🟢"), "истёкший, но подключённый — всё равно онлайн"

# ── порядок фильтров в роутере настроек ──────────────────────────────────────

def test_specific_settings_handlers_are_registered_before_the_generic_one():
    """Специфичные обработчики раздела обязаны стоять ВЫШЕ общего do_action.

    Фильтры проверяются в порядке регистрации, а у do_action он широкий
    (`F.act == "do"`). Окажись он первым — он перехватил бы и sec="mig", и
    sec="rt": ключ не подошёл бы ни к одной его ветке, функция закончилась бы
    молча, колбэк остался бы без ответа, а на кнопке — вечный спиннер.

    Ровно это и случилось на боевом сервере: рычаг переезда нажимался, хендлер
    отрабатывал за две миллисекунды и не делал ничего. Прямой вызов функции в
    тестах этого поймать не мог — маршрутизация там не участвует.
    """

    order = [h.callback.__name__ for h in sh.router.callback_query.handlers]
    generic = order.index("do_action")
    for specific in ("routing_action", "migration_action"):
        assert order.index(specific) < generic, (
            f"{specific} зарегистрирован после do_action — тот перехватит его "
            f"колбэки, и кнопка будет крутиться без ответа")
    # Та же история с edit_value (`F.act == "edit"`): ключа "port" в
    # SETTINGS_BOUNDS нет, и «Задать порт» у переезда отвечала бы «Эта
    # настройка недоступна».
    assert order.index("migration_port_ask") < order.index("edit_value"), (
        "migration_port_ask зарегистрирован после edit_value — кнопка «Задать "
        "порт» упрётся в «настройка недоступна»")


async def test_migration_port_button_opens_the_port_prompt(services, fake_bot):
    """«Задать порт» у подготовки переезда: первый хендлер, чьи фильтры
    пропускают этот колбэк, — именно ввод порта, и введённое дальше
    проверяется как порт."""
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot import texts

    assert _first_matching_handler(sh.router, SetCB(sec="mig_prep", act="edit", key="port")) \
        == "migration_port_ask"

    cb, nav = _acb(fake_bot)
    state = FakeState()
    await sh.migration_port_ask(cb, state, services)
    assert any(s[0] == "edit_text" and texts.MIGRATION_ASK_PORT in s[1] for s in nav.sent)

    bad = _amsg(fake_bot, "abc")
    await sh.migration_port_received(bad, state, services)
    assert any("1 до 65535" in s[1] for s in bad.sent), "буквы — не порт"


def _first_matching_handler(router, cb_data) -> str:
    """Имя хендлера, который aiogram выбрал бы для этого callback_data: первый
    по порядку регистрации, чьи CallbackData-фильтры совпали."""
    for h in router.callback_query.handlers:
        ok = True
        for flt in h.filters:
            cls = getattr(flt.callback, "callback_data", None)
            rule = getattr(flt.callback, "rule", None)
            if cls is None or cls is not type(cb_data):
                ok = False
                break
            if rule is not None and not rule.resolve(cb_data):
                ok = False
                break
        if ok:
            return h.callback.__name__
    return ""

def test_notify_section_layout_and_profiles_submenu(monkeypatch):
    from awgbot.bot import keyboards as kbs
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: d)
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: d)
    rows = [[b.text for b in r] for r in kbs.settings_notify().inline_keyboard]
    assert rows[0] == ["🔴 E-mail при недоступности Telegram"]
    assert rows[1] == ["🟢 Тихие часы"]
    assert rows[-2] == ["👥 События профилей"] and rows[-1][0].endswith("Назад")
    assert not any("Активация" in b for r in rows for b in r), "события профилей ушли в подменю"
    sub = [[b.text for b in r] for r in kbs.settings_notify_clients().inline_keyboard]
    assert sub[0] == ["✅ Активация профиля"] and len(sub) == 5
