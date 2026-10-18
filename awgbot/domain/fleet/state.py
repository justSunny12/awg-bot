"""state.py — часть пакета fleet (см. __init__): ход набора `fleet_target` и
состояние шлюза в наборе `gwupd_<слот>` в server_state.

Ход: {"id", "tag", "prev", "main_prev", "digest", "why", "at", "state",
"steps", "undo", "failed_on", "reason", "revert_failed", "msg", "server",
"main_undone"}; main_prev — версия сервера до хода, server — шаг сервера в
пути (server.py), main_undone — сервер уже откачен; state — running |
rolling_back | done | rolled_back | rollback_failed. steps — что сделано
вперёд по порядку ([["main", тег], ["gw", слот, тег]]), undo — сколько из них
ещё не откачено (откат идёт с конца). msg — живое сообщение [чат, id].

Шлюз: {"id", "run", "target", "prev", "state", "step", "error", "why",
"hold", "tries", "at", "off_since", "next_at", "digest", "lost_from",
"pending"}; state — queued |
sent | accepted | done | failed | rolled_back | refused | lost | reverting |
reverted | revert_failed. run — id хода (пусто — догон или следование вне
хода); hold — шлюз держит прежнее до фиксации или отката этого хода.

Выпуск набора поверх версии сервера (выпуск только для шлюзов) — отдельный
ключ `fleet_release`: его пишет только фиксация хода.
"""
from __future__ import annotations

import json
import secrets

RUN_KEY = "fleet_target"
RELEASE_KEY = "fleet_release"
SLOT_KEY = "gwupd"   # слотовый ключ — в реестре gateway_link/slotkeys.py

RUNNING, ROLLING_BACK, DONE, ROLLED_BACK, ROLLBACK_FAILED = (
    "running", "rolling_back", "done", "rolled_back", "rollback_failed")
ACTIVE_RUN = (RUNNING, ROLLING_BACK)            # ход идёт: команды шлёт только он
FAILURES = ("failed", "rolled_back", "refused")  # неудача доступного шлюза — откат набора
IN_FLIGHT = ("queued", "sent", "accepted")      # шаг шлюза ещё не закончен


def new_id() -> str:
    return secrets.token_hex(4)


class FleetStateMixin:
    """Чтение и запись хода и состояний шлюзов."""

    _FLEET_SLOT_KEY = "gwupd"           # = SLOT_KEY: литералом — его видит сторож реестра

    def fleet_run(self) -> dict:
        data = self.db.get_state_json(RUN_KEY, {})
        return data if isinstance(data, dict) else {}

    def fleet_run_set(self, run: dict) -> None:
        self.db.set_state(RUN_KEY, json.dumps(run, ensure_ascii=False) if run else "")

    def fleet_slot(self, slot_id: int) -> dict:
        from awgbot.domain.services.gateway_link import slotkeys     # лениво: пакет сервисов подмешивает fleet
        data = self.db.get_state_json(slotkeys.key(self._FLEET_SLOT_KEY, slot_id), {})
        return data if isinstance(data, dict) else {}

    def fleet_slot_set(self, slot_id: int, data: dict) -> None:
        from awgbot.domain.services.gateway_link import slotkeys
        self.db.set_state(slotkeys.key(self._FLEET_SLOT_KEY, slot_id), json.dumps(data, ensure_ascii=False) if data else "")

    def fleet_running(self) -> bool:
        return self.fleet_run().get("state") in ACTIVE_RUN
