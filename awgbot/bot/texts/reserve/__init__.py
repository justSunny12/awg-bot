"""texts/reserve — тексты раздела «🛟 Резерв»: сопряжение (pairing), карточка и
раздел (section), уведомления (notices). Имя второго сервера везде — «резерв
«Beta»»; статусы — 🟢 жив / 🔴 недоступен с … / 🟢 несёт нагрузку с …."""
from __future__ import annotations

from . import notices, pairing, peeradmin, section

__all__ = ["notices", "pairing", "peeradmin", "section"]
