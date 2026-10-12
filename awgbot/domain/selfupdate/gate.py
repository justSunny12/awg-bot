"""gate.py — часть пакета selfupdate (см. __init__): условие на версию другой
роли — кто другая сторона, выполнено ли условие, что ждёт и кто молчит.

Версии другой стороны роль отдаёт сама методом update_peers(): основной бот —
версии агентов по слотам (gwchannel/versions), агент — версию сервера AWG из
последнего `role` (gateway/report). Нет метода — у роли условий нет, выбор
цели как до сверки.

Жёстко условие действует только на тех, кто был на связи за
UPDATE_PEER_FRESH_DAYS: молчащий дольше шлюз не держит сервер — о нём
предупреждение, обновиться можно. У агента другая сторона одна, и она всегда
«на связи» в этом смысле: без известной версии сервера релиз с условием ждёт.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from awgbot.core import config
from awgbot.infra import updates

# Шлюз, бывший на связи за столько дней, держит обновление сервера жёстко;
# молчащий дольше — только предупреждение (решение 12.10.2026).
UPDATE_PEER_FRESH_DAYS = 2

SERVER_OLD = "old"          # сервер прислал `role` без версии — старше 3.3.0


@dataclass(frozen=True)
class UpdatePeer:
    """Другая сторона для условия обновления."""
    slot: int               # слот шлюза; 0 — сервер AWG (у агента)
    name: str               # имя шлюза без подписи; у агента пусто
    version: str            # «3.3.0»; "" — неизвестна; SERVER_OLD — сервер старше 3.3.0
    fresh: bool             # на связи за UPDATE_PEER_FRESH_DAYS: условие жёсткое
    seen: str = ""          # ISO последней связи — для «не на связи с …»

    def meets(self, need: tuple) -> bool:
        return updates.version_at_least(self.version, need)


def _need_str(need: tuple) -> str:
    return ".".join(str(x) for x in need)


class SelfUpdateGateMixin:
    """Условие на версию другой роли: проверка, ожидание, молчащие."""

    _WAITING_KEY = "update_waiting"

    def _update_role(self) -> str:
        return config.ROLE or ""

    def _update_peers(self) -> Optional[list]:
        """Другая сторона роли или None — у роли условий нет."""
        fn = getattr(self, "update_peers", None)
        return list(fn()) if callable(fn) else None

    def _update_gate(self, peers=...):
        """peer(need) для выбора цели или None — условий нет."""
        peers = self._update_peers() if peers is ... else peers
        if peers is None:
            return None
        hard = [p for p in peers if p.fresh]
        return lambda need: all(p.meets(need) for p in hard)

    def _update_note_waiting(self, releases: list, peers=...) -> None:
        """Записать новейший релиз, который ждёт другую сторону, — по списку,
        без сети. Пусто — ничего не ждёт."""
        import json
        installed = updates.parse_version(config.INSTALLED_VERSION)
        gate = self._update_gate(peers)
        if installed is None or gate is None:
            self.db.set_state(self._WAITING_KEY, "")
            return
        role, ceiling = self._update_role(), self._generation_ceiling()
        rel = updates.waiting_target(installed, releases, role, ceiling, gate)
        need = updates.chain_need(installed, releases, rel, role, ceiling) if rel else None
        self.db.set_state(self._WAITING_KEY, json.dumps(
            {"tag": rel.tag, "need": _need_str(need)}) if rel is not None and need else "")

    def update_waiting(self) -> Optional[dict]:
        """Что ждёт другую сторону — для шапки, панели, раздела и всплывашки:
        {"tag", "need", "role", "peers": [UpdatePeer, …] — кто держит}. None —
        ничего не ждёт. Без сети: запись последней проверки плюс версии
        другой стороны сейчас; кто успел догнать — уже не держит, а не
        держит никто — и ожидания нет (запись устарела до пересчёта)."""
        raw = self.db.get_state_json(self._WAITING_KEY, {})
        if not isinstance(raw, dict) or not raw.get("tag"):
            return None
        need = updates.parse_version(str(raw.get("need") or ""))
        peers = self._update_peers()
        if need is None or peers is None:
            return None
        holding = [p for p in peers if p.fresh and not p.meets(need)]
        if not holding:
            return None
        return {"tag": str(raw["tag"]), "need": _need_str(need), "role": self._update_role(),
                "peers": holding}

    def update_silent_peers(self, target) -> Optional[dict]:
        """Молчащие шлюзы, чью версию цель уже не обслуживает: {"tag", "need",
        "peers"} или None. Только по сохранённому списку релизов (условие —
        по цепочке до цели) и только для тех, кто не на связи за
        UPDATE_PEER_FRESH_DAYS: на связи и ниже — цель была бы недопустима."""
        if target is None:
            return None
        peers = self._update_peers()
        releases = self._update_cached_releases()
        installed = updates.parse_version(config.INSTALLED_VERSION)
        if not peers or releases is None or installed is None:
            return None
        rel = next((r for r in releases if r.tag == getattr(target, "tag", None)), None)
        if rel is None:
            return None
        need = updates.chain_need(installed, releases, rel, self._update_role(),
                                  self._generation_ceiling())
        if need is None:
            return None
        silent = [p for p in peers if not p.fresh and not p.meets(need)]
        if not silent:
            return None
        return {"tag": rel.tag, "need": _need_str(need), "peers": silent}
