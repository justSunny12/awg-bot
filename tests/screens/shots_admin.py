"""Снимки экранов админа (эталон admin.txt): жмёт админ, диспетчер основного бота."""
from __future__ import annotations

from awgbot.bot.callbacks import SetCB

from tests.screens.base import Shot

SHOTS = [
    Shot("adm.set.root", role="admin", press=[SetCB(sec="root")], title="корень настроек"),
]

# Длинная подпись в ряду из 2+ разрешена макетом экрана — {id снимка: подписи};
# те же исключения, что в tests/unit/test_ui_layout.py.
LABEL_EXCEPTIONS: dict[str, set[str]] = {}
