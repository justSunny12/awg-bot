"""Снимки экранов клиента (client.txt) и гостя (guest.txt): жмёт владелец профиля
или гость, диспетчер основного бота."""
from __future__ import annotations

from tests.screens.base import Shot, owner

SHOTS = [
    Shot("cl.main", role="client", start="", data=owner, title="главная по /start"),
]

LABEL_EXCEPTIONS: dict[str, set[str]] = {}
