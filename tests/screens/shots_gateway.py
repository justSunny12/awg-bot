"""Снимки экранов агента шлюза (gateway.txt): жмёт админ, диспетчер агента."""
from __future__ import annotations

from awgbot.bot.callbacks import GwCB

from tests.screens.base import Shot

SHOTS = [
    Shot("gw.set.notify", role="gateway", press=[GwCB(action="notify")], title="по умолчанию"),
]

LABEL_EXCEPTIONS: dict[str, set[str]] = {
    "gw.set.notify": {"☑️ Аварии на e-mail"},
}
