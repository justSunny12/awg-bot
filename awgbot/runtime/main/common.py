"""common.py — часть пакета main (см. __init__): журнал и метка свежей установки."""

from __future__ import annotations

import logging

FRESH_INSTALL_MARKER = ".fresh-install"   # кладёт установщик агента (awg-bot.sh), снимает первый запуск

log = logging.getLogger("awgbot.main")
