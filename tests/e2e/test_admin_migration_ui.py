"""E2E: экраны переезда админа — обзор по ссылке «🚚 Переезд» с главной
(/start migration) и переезд одного профиля (/start migration-<id>).

Цена ошибки: в шапке обзора — неизменившиеся параметры, и админ ищет, что
же меняется; ссылка «migration-<id>» не узнана — экран профиля не открыть.

Экраны сверяет эталон: обзор (adm.migration, adm.migration.params — порт и
ядро меняются, отстающие по убыванию остатка; adm.migration.empty —
переезжать некому), профиль (adm.link.migration_cl, adm.link.migration_cl.spread
— коннект старого пира, «не подключалось»; adm.link.migration_cl.missing —
профиля нет). Здесь — шапка из одинаковых значений и разбор ссылок.
"""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers.admin import panel

pytestmark = pytest.mark.e2e


def test_overview_head_drops_equal_values():
    """Одинаковые значения в шапку не попадают: «подсеть: 10.8.1 → 10.8.1»
    читается как ошибка настройки."""
    d = {"port": ("", ""), "subnet": ("10.8.1", "10.8.1"), "generation": ("", ""), "rows": []}
    assert texts.migration_overview_text(d).startswith("🚚 <b>Переезд</b>\n"), \
        "одинаковые значения попали в шапку"


@pytest.mark.parametrize("payload, link", [
    ("migration", ("migration", 0)), ("migration-12", ("migration_cl", 12)),
    ("migration-", None), ("migration-x", None), ("gwcfg-2", ("gwcfg", 2)), ("gwcfg-", None),
])
def test_parse_link_knows_migration_and_gateway_configuration(payload, link):
    assert panel.parse_link(payload) == link
