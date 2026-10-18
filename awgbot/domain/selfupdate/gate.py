"""gate.py — часть пакета selfupdate (см. __init__): условие на версию другой
роли для выбора ступени и пропущенные релизы.

Условие (`roles.main.needs_gw`, `roles.gw.needs_main` в метаданных) больше
не блокировка для человека, а правило порядка набора (domain/fleet): ступень
сервера считается с условием «участники хода не ниже» — держат только
доступные шлюзы (Р22), агент проверяет `needs_main` сам по версии сервера из
`role`, получив команду. Здесь — только проверка по списку версий
(`_update_gate`) и пропущенные релизы (update_skipped) для строки раздела.
"""
from __future__ import annotations

from dataclasses import dataclass

from awgbot.core import config
from awgbot.infra import updates

SERVER_OLD = "old"          # сервер прислал `role` без версии — старше 3.3.0


@dataclass(frozen=True)
class UpdatePeer:
    """Другая сторона для условия обновления."""
    slot: int               # слот шлюза; 0 — сервер AWG (у агента)
    name: str               # имя шлюза без подписи; у агента пусто
    version: str            # «3.3.0»; "" — неизвестна; SERVER_OLD — сервер старше 3.3.0
    fresh: bool             # держит условие (участник хода; у агента — сервер всегда)
    seen: str = ""          # ISO последней связи

    def meets(self, need: tuple) -> bool:
        return updates.version_at_least(self.version, need)


class SelfUpdateGateMixin:
    """Условие на версию другой роли и пропущенные релизы."""

    def _update_role(self) -> str:
        return config.ROLE or ""

    def _update_gate(self, peers=None):
        """peer(need) для выбора ступени или None — условий нет: все из peers,
        кто держит (fresh), не ниже need."""
        if peers is None:
            return None
        hard = [p for p in peers if p.fresh]
        return lambda need: all(p.meets(need) for p in hard)

    def update_skipped(self) -> list:
        """[(тег, причина)] релизов новее установленного, которые не
        рассматриваются: метаданные не прочитаны (О8). По сохранённому списку
        последней проверки, без сети."""
        releases = self._update_cached_releases()
        installed = updates.parse_version(config.INSTALLED_VERSION)
        if releases is None or installed is None:
            return []
        return [(r.tag, r.skip_reason()) for r in updates.skipped(installed, releases)]
