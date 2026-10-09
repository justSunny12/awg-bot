"""E2E: экраны трафика админа списком — «📊 Трафик за ММ.ГГ» (A03) и трафик
профиля (A04), слитые с прежними экранами РФ-доступа; строка РФ в карточках
профиля и устройства; ссылки /start traffic…/traffic_local… на эти экраны.

Цена ошибки: вложенная строка без «└ » читается как соседняя запись; старая
ссылка из истории чата (traffic_local…) ведёт в исключение вместо экрана.

Экраны сверяет эталон: A03 и A04 в типичном состоянии и по ссылкам
(adm.traffic*, adm.link.traffic*), нули не выводятся (adm.traffic.no_rf),
«🧐 Вне профилей» — позиция, порог, удалённое устройство
(adm.traffic.outside*), итог профиля без чужого трафика и без устройств без
трафика (adm.link.traffic_dev.idle), шлюз без РФ-ветки
(adm.link.traffic_dev.gw), ссылки правкой живого меню (adm.link.traffic*.live),
удалённый профиль по ссылке (adm.link.traffic_dev.missing), старая кнопка
(adm.traffic.old_back), матрица строки РФ в карточках и списке
(adm.cl.rf.*, adm.dev.rf.*, adm.traffic.rf_on/rf_off), сервер без функции
(adm.traffic.no_feature, adm.link.traffic_dev.no_feature). Здесь — сборка
списка и разбор битых ссылок.
"""
import pytest

from awgbot.bot.handlers.admin.panel import parse_link
from awgbot.bot.texts import fmt

pytestmark = pytest.mark.e2e


def test_list_separates_entries_with_a_blank_line_and_nests_with_the_same_mark():
    """Записи через пустую строку, вложенные — «└ » сразу под своей записью;
    одиночная вложенная строка (карточки, главная) — тем же знаком."""
    assert fmt.tree([("A", ["a1"]), ("B", []), ("C", ["c1", "c2"])]) == "A\n└ a1\n\nB\n\nC\n└ c1\n└ c2"
    assert fmt.tree([]) == ""
    assert fmt.sub_line("x") == "└ x"


@pytest.mark.parametrize("payload", ["traffic_local-abc-t", "traffic_local--t", "traffic_local-t",
                                     "traffic_local-abc"])
def test_broken_traffic_links_are_not_links(payload):
    """Суффикс без числа — не ссылка: /start с ним открывает главную (как
    любая чужая ссылка — снимок adm.link.unknown), а не исключение разбора."""
    assert parse_link(payload) is None


@pytest.mark.parametrize("payload", ["traffic-999999", "traffic_local-999999-t"])
def test_old_and_new_profile_traffic_links_parse_the_same(payload):
    """Обе формы ссылки на трафик профиля — один экран; профиля нет — главная
    (снимок adm.link.traffic_dev.missing)."""
    assert parse_link(payload) == ("traffic_dev", 999999)
