"""Снимки экранов админа (эталон admin.txt): жмёт админ, диспетчер основного бота.

Группы — как в перечне экранов: главная и ссылки с неё, устройства, РФ-доступ,
профили, новый профиль и объявление, настройки, вне меню, ссылки /start.
Снимки — по модулю на раздел (shots_admin_<раздел>), общие построители
состояния — в shots_admin_common и shots_admin_common_gw; здесь — только
порядок, в котором снимки идут в эталоне.
Состояние БД каждого снимка строят построители (_people, _slots, …):
порядок создания в них фиксирован, поэтому номера профилей, устройств и
слотов в колбэках снимков — константы (сверяются assert'ом в построителе).
Хост (sshd, файервол, ядро AWG, сеть шлюза) в снимках не трогается: его
ответы — фиксированные словари и подмены методов сервисов.
"""
from __future__ import annotations

from tests.screens import shots_admin_broadcast as broadcast
from tests.screens import shots_admin_devices as devices
from tests.screens import shots_admin_events as events
from tests.screens import shots_admin_fleet as fleet
from tests.screens import shots_admin_gateways as gateways
from tests.screens import shots_admin_gw_cards as gw_cards
from tests.screens import shots_admin_gw_channel as gw_channel
from tests.screens import shots_admin_gw_keys as gw_keys
from tests.screens import shots_admin_home as home
from tests.screens import shots_admin_join as join
from tests.screens import shots_admin_migration as migration
from tests.screens import shots_admin_profiles as profiles
from tests.screens import shots_admin_rf as rf
from tests.screens import shots_admin_settings as settings
from tests.screens import shots_admin_settings_flows as settings_flows

# Порядок — порядок эталона admin.txt: новый снимок дописывается в список
# своего раздела, новый список — сюда, туда, где его снимки должны встать.
SHOTS = [
    # ── шаг 1: перечень экранов по разделам ─────────────────────────────────
    *home.MAIN,
    *devices.DEVICES,
    *rf.RF,
    *profiles.PROFILES,
    *profiles.NEW,
    *broadcast.BROADCAST,
    *settings.SETTINGS,
    *fleet.FLEET,
    *gateways.GATEWAYS,
    *migration.MIGRATION,
    *home.LINKS,
    *home.OUTSIDE,
    # ── шаг 3: диалоги до итога, файл копии, события без действия ───────────
    *profiles.DIALOGS,
    *broadcast.DIALOGS,
    *settings_flows.DIALOGS,
    *settings_flows.RESTORE,
    *events.EVENTS,
    # ── шаг 5: построители клавиатур без снимка и ветки, которые держали e2e ─
    *profiles.DELETE_PARTIAL,
    *devices.GW_NO_SLOT,
    *events.GENERATION,
    *migration.OVERVIEW,
    *home.BRANCHES,
    *devices.DEVICES_GW,
    *profiles.ICONS,
    *settings.BRANCHES,
    *rf.TRAFFIC,
    *rf.RF_LINES,
    *rf.SELFCHECK,
    *settings.SSH,
    *settings_flows.SECTIONS,
    *profiles.CARDS,
    *home.MENU,
    *devices.ADMIN,
    *profiles.NEW_INPUT,
    *profiles.EXTEND,
    *profiles.BLOCK,
    *settings_flows.MAIL_BACKUP,
    *broadcast.BRANCHES,
    *rf.PROFILE,
    *gw_cards.CARDS,
    *gateways.SLOTS,
    *join.JOIN,
    *gw_channel.CHANNEL,
    *gw_keys.CFG,
    *gw_keys.REKEY,
    *gw_keys.REKEY_DECLINED,
    *gw_keys.TOKEN_SWAP,
    *gw_cards.SMB,
    *gw_cards.SMB_ONE,
]

# у каждого снимка — построитель: он же сбрасывает модульные подмены прежних
assert all(sh.data is not None for sh in SHOTS), [sh.id for sh in SHOTS if sh.data is None]

# Длинная подпись в ряду из 2+ разрешена макетом экрана — {id снимка: подписи};
# длина считается без селектора варианта («☑️ Аварии на e-mail» — 18).
LABEL_EXCEPTIONS: dict[str, set[str]] = {   # «Простой со звуком» — 19 знаков, принято пользователем
    "adm.set.mon": {"✅ Простой со звуком"}, "adm.set.mon.edit.done": {"✅ Простой со звуком"},
    "adm.set.mon.quiet": {"☑️ Простой со звуком"},
}
