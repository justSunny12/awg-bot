"""E2E: раскладка настроек и значки — корень в два столбца, разделы и их выход,
кнопки правок,
порядок фильтров роутера настроек, галочки против кружков, значок шлюза."""
import pytest

from awgbot.bot.callbacks import SetCB
from awgbot.bot import sections
from awgbot.bot.handlers import settings as sh
from awgbot.bot.roles import MAIN
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

def test_settings_root_is_ten_buttons_in_two_columns():
    """Корень — десять кнопок парами: слева то, что трогают при настройке
    сервера, справа — реже; «⬅️ В меню» в паре с «⬆️ Обновления». РФ-доступа
    в корне нет — он живёт на главной («🛰 Шлюзы»), второй вход раздваивал бы
    одно и то же место."""
    from awgbot.bot.callbacks import Menu
    markup = sections.root.keyboard(MAIN)
    rows = [[b.text for b in r] for r in markup.inline_keyboard]
    assert rows == [["🔔 Уведомления", "🖥 Сервер AWG"], ["🛡 SSH-доступ", "✉️ E-mail"],
                    ["💳 Подписки", "💾 Бэкапы"], ["🩺 Мониторинг", "🔧 Сервис"],
                    ["⬆️ Обновления", "⬅️ В меню"]], rows
    datas = [b.callback_data for r in markup.inline_keyboard for b in r]
    assert datas == [SetCB(sec=s).pack() for s in ("notify", "srv", "fw", "email", "subs", "backup",
                                                  "mon", "svc", "upd")] + [Menu(action="main").pack()]
    assert not any("РФ" in t or "Шлюз" in t for r in rows for t in r)


@pytest.mark.parametrize("migration, available, orphans, expected", [
    ("", False, 0, [["🔁 Перезапуск AWG", "🔁 Перезапуск бота"], ["⬅️ Назад"]]),
    ("", True, 0, [["🔁 Перезапуск AWG", "🔁 Перезапуск бота"], ["🚚 Начать переезд"], ["⬅️ Назад"]]),
    ("", True, 2, [["🔁 Перезапуск AWG", "🔁 Перезапуск бота"], ["🚚 Начать переезд"],
                   ["⚠️ Переехавшие после отмены: 2"], ["⬅️ Назад"]]),
    ("running", True, 0, [["🔁 Перезапуск AWG", "🔁 Перезапуск бота"], ["👥 Кто не переехал"],
                          ["✅ Завершить", "↩️ Отменить"], ["⬅️ Назад"]]),
])
def test_service_section_offers_restarts_and_migration_by_state(migration, available, orphans, expected):
    """«🔧 Сервис»: перезапуски парой; переезд — по состоянию: идёт — кто не
    переехал, завершить, отменить; нет — начать (если настроен) и
    переехавшие после отмены (если есть). Лишняя кнопка переезда при
    ненастроенном переезде упиралась бы в отказ."""
    rows = [[b.text for b in r] for r in sections.svc.keyboard(MAIN, migration, available=available,
                                                                orphans=orphans).inline_keyboard]
    assert rows == expected, rows


def test_sections_moved_to_the_root_return_to_the_root():
    """Мониторинг и бэкапы теперь в корне: «Назад» из них — в корень, иначе
    человек попадает в раздел, из которого не входил."""
    from awgbot.bot import keyboards as kb
    for markup in (sections.mon.keyboard(MAIN), sections.backup.keyboard(MAIN), sections.svc.keyboard(MAIN),
                   kb.settings_subs(), sections.notify.keyboard(MAIN), sections.updates.keyboard(MAIN, False),
                   sections.email.keyboard(MAIN, True)):
        back = markup.inline_keyboard[-1][0]
        assert back.text == "⬅️ Назад" and back.callback_data == SetCB(sec="root").pack(), back
    back = sections.ncl.keyboard(MAIN).inline_keyboard[-1][0]
    assert back.callback_data == SetCB(sec="notify").pack(), "«События» — назад в «Уведомления»"


def test_every_edit_button_points_at_a_known_setting():
    """Опечатка в ключе кнопки превращает её в «Кнопка устарела», и
    раздел становится мёртвым. Сверяем ключи клавиатур со словарями текстов
    и границами разделов."""
    from awgbot.bot import keyboards as kb, texts
    from awgbot.bot.callbacks import SetCB
    known = set(texts.SETTINGS_TEXT) | {k for k in texts.SETTINGS_BOUNDS} | {
        k for m in sections.MODULES for k in m.BOUNDS}
    markups = [kb.settings_server(), kb.settings_firewall({"raw_allow": ["1.2.3.4"]}),
               sections.mon.keyboard(MAIN), kb.settings_subs(), sections.notify.keyboard(MAIN),
               sections.email.keyboard(MAIN, True), sections.backup.keyboard(MAIN)]
    checked = 0
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                if not b.callback_data.startswith("set:"):
                    continue
                cb = SetCB.unpack(b.callback_data)
                if cb.act != "edit":
                    continue
                if (cb.sec, cb.key) in (("fw", "port"), ("backup", "backup_when")):
                    continue                       # свои ветки ввода (порт SSH, день и час бэкапа)
                checked += 1
                assert cb.key in known, f"кнопка «{b.text}» ведёт в несуществующий ключ {cb.key}"
    assert checked >= 8, "проверять оказалось нечего — тест устарел"

def test_gateway_device_keeps_its_icon_in_the_button_list():
    """В текстовых списках шлюз был 🛰, а в кнопках «Мои устройства» — обычным
    телефоном: две функции иконки разошлись. Перепутать шлюз с телефоном там,
    где их удаляют и блокируют, дороже всего."""
    from awgbot.bot import keyboards as kb

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
    assert labels[0] == "🛰 Шлюз", labels
    # у остальных значок — состояние (⛔ ⏳ 🟢 ⚪): не подключалось — офлайн
    assert labels[1] == "⚪ iPhone", labels
    # Значок ровно один: у шлюза, который онлайн, кружок к 🛰 не добавляется —
    # два подряд в каждой строке превращают список в рябь.
    assert not any("🟢" in l for l in labels), labels


def test_routing_domain_list_uses_minus_and_a_bin_for_the_whole_list():
    """Минус убирает одну запись — то же, что в разделе доступа по SSH.
    Корзина остаётся там, где сносят всё разом."""
    from awgbot.bot import keyboards as kb
    markup = kb.routing_sites(1, ["ozon.ru", "mail.ru"])
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "➖ ozon.ru" in labels and "➖ mail.ru" in labels
    assert "🗑 Очистить" in labels
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
    notify = [b.text for row in sections.ncl.keyboard(MAIN).inline_keyboard for b in row]
    assert all(not l.startswith(("🟢", "🔴")) for l in notify), notify
    # переключатели «Шлюзов» — тоже тумблеры ✅/☑️: кружок остаётся состоянию
    # объектов (онлайн, работает), не настройкам
    from types import SimpleNamespace as NS
    states = [{"gateway": NS(id=i), "device": NS(name=n), "active": i == 1, "preferred": i == 1}
              for i, n in ((1, "NASPi"), (2, "Pi4"))]
    rt = [b.text for row in kb.gateways_kb(states, failover_on=False, peer_nets_on=True).inline_keyboard
          for b in row]
    assert "☑️ Автопереключение" in rt and "✅ Связь подсетей" in rt, rt
    assert not any(t.startswith(("🟢", "🔴")) for t in rt), rt


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

    from awgbot.core.blocks import ClientBlock
    clients = [_C(1, "Онлайн"), _C(2, "Офлайн"),
               _C(3, "Ждёт", act=ActivationStatus.PENDING),
               _C(4, "Истёк", status=SubStatus.EXPIRED),
               _C(5, "Блок"), _C(6, "Пауза")]
    clients[4].block_reason = int(ClientBlock.ADMIN_SILENT) | int(ClientBlock.PAUSED)
    clients[5].block_reason = int(ClientBlock.PAUSED)
    labels = [b.text for row in kb.admin_clients(clients, online_ids={1, 4, 5, 6}).inline_keyboard
              for b in row]
    assert labels[:6] == ["🟢 Онлайн", "⚪ Офлайн", "⏳ Ждёт", "🟢 Истёк", "⛔ Блок", "⏸️ Пауза"], \
        "один значок по приоритету: ⛔ блок → ⏸️ пауза → ⏳ не активирован → 🟢 онлайн → ⚪ нет"

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
    # И с «Изменить порт» в разделе SSH — тот же ключ "port" вне SETTINGS_BOUNDS.
    assert order.index("ssh_port_ask") < order.index("edit_value"), (
        "ssh_port_ask зарегистрирован после edit_value — кнопка «Изменить порт» "
        "упрётся в «настройка недоступна»")
    assert _first_matching_handler(sh.router, SetCB(sec="fw", act="edit", key="port")) \
        == "ssh_port_ask"
    assert _first_matching_handler(sh.router, SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")) \
        == "edit_value", "«Добавить адрес» по-прежнему идёт общим вводом"


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
    """«🔔 Уведомления»: тумблеры ✅/☑️, границы тихих часов и пороги — только
    у включённого; «Аварии на e-mail» и «👥 События» — парой; события
    профилей — в подменю по два в ряд."""
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: d)
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: d)
    rows = [[b.text for b in r] for r in sections.notify.keyboard(MAIN).inline_keyboard]
    assert rows == [["✅ Тихие часы"], ["С 20:00", "До 07:00"], ["✅ Алерты хоста"],
                    ["CPU 80%", "RAM 80%", "Диск 80%"], ["☑️ Аварии на e-mail", "👥 События"],
                    ["⬅️ Назад"]], rows
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: False)
    rows = [[b.text for b in r] for r in sections.notify.keyboard(MAIN).inline_keyboard]
    assert rows == [["☑️ Тихие часы"], ["☑️ Алерты хоста"], ["☑️ Аварии на e-mail", "👥 События"],
                    ["⬅️ Назад"]], "выключено — без границ и порогов"
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: d)
    sub = [[b.text for b in r] for r in sections.ncl.keyboard(MAIN).inline_keyboard]
    assert sub == [["✅ Активация", "✅ Отсрочка"], ["✅ Лимит исчерпан", "✅ Бонусный объём"],
                   ["⬅️ Назад"]], sub
