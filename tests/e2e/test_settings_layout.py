"""E2E: раскладка настроек — кнопки правок и порядок фильтров роутера
настроек.

Корень настроек, «🔧 Сервис» по состояниям переезда, выход из разделов в
корень, список сайтов РФ-доступа — в эталоне (adm.set.root, adm.set.svc*,
adm.set.mig.cancel.yes, adm.set.*, adm.rf.sites), значки списков
(adm.clients.icons, adm.devices.gw), тихие часы с границами
(adm.set.notify.quiet), выключенное автопереключение тумблером ☑️, а не
кружком (adm.gw.failover.off); здесь — проверки, не сводящиеся к одному
экрану."""
import pytest

from awgbot.bot.callbacks import SetCB
from awgbot.bot import sections
from awgbot.bot.handlers import settings as sh
from awgbot.bot.roles import MAIN

pytestmark = pytest.mark.e2e


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


def test_migration_port_button_opens_the_port_prompt():
    """«Задать порт» у подготовки переезда: первый хендлер, чьи фильтры
    пропускают этот колбэк, — именно ввод порта (приглашение и отказ на
    буквы — снимки adm.set.mig_prep.port и adm.set.mig_prep.port.bad)."""
    assert _first_matching_handler(sh.router, SetCB(sec="mig_prep", act="edit", key="port")) \
        == "migration_port_ask"


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
