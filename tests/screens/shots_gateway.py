"""Снимки экранов агента шлюза (gateway.txt): жмёт админ, диспетчер агента.

Снимки — по модулю на раздел (shots_gateway_<раздел>); двойник агента и
хоста (`_agent(...)`, `_Host`, `_Agent`) и построитель снимка `_shot` — в
shots_gateway_agent, общие данные хоста — в shots_gateway_common; здесь —
только порядок, в котором снимки идут в эталоне.

События без действия человека (первая панель, обещание перезапуска, итоги
обновления и восстановления, перезагрузка хоста) — шагом call теми же
функциями, что зовёт старт агента.
"""
from __future__ import annotations

from tests.screens import shots_gateway_backup as backup
from tests.screens import shots_gateway_document as document
from tests.screens import shots_gateway_lan as lan
from tests.screens import shots_gateway_lists as lists
from tests.screens import shots_gateway_panel as panel
from tests.screens import shots_gateway_settings as settings
from tests.screens import shots_gateway_ssh as ssh

# ── снимки ───────────────────────────────────────────────────────────────────
# Порядок — порядок эталона gateway.txt: новый снимок дописывается в список
# своего раздела, новый список — сюда, туда, где его снимки должны встать.
SHOTS = [
    *panel.PANEL,
    *backup.WIZARD,
    *document.DOCUMENT,
    *lan.LAN,
    *settings.SETTINGS,
    *ssh.SSH,
    *settings.MONITOR,
    *backup.BACKUPS,
    *settings.SERVICE,
    *backup.FILES,
    *panel.EVENTS,
    *lists.LISTS,
]

# Длинная подпись в ряду из 2+ разрешена макетом экрана — {id снимка: подписи};
# длина считается без селектора варианта («☑️ Аварии на e-mail» — 18).
# «✅/☑️ Простой со звуком» — подпись тумблера по решению пользователя (15.10.2026): 19 в ряду из двух
_LOUD_ON, _LOUD_OFF = {"✅ Простой со звуком"}, {"☑️ Простой со звуком"}
LABEL_EXCEPTIONS: dict[str, set[str]] = {
    "gw.set.mon": _LOUD_ON, "gw.set.mon.quiet": _LOUD_OFF, "gw.set.mon.toggle": _LOUD_OFF,
    "gw.set.mon.edit.done": _LOUD_ON, "gw.set.mon.minutes.done": _LOUD_ON, "gw.set.mon.streak_one": _LOUD_ON,
}
