"""Заглушка агента шлюза для тестов клиента канала: все методы, которые клиент
у сервисов зовёт, — с поведением «нечего делать» (как у агента без своих
списков, без соседей и без отложенных дел). Фейки тестов наследуют её и
переопределяют то, что проверяют."""
from __future__ import annotations


class AgentStub:
    # снимок и роли переопределяют наследники
    def services_applied_hash(self) -> str:
        return ""

    def own_applied_hash(self) -> str:
        return ""

    def applied_pending_get(self):
        return None

    def applied_pending_set(self, ok, error, fp) -> None:
        pass

    def services_local(self) -> list:
        return []

    def own_pending_events(self):
        return "", []

    def own_unsent(self) -> bool:
        return False

    def apply_own_lists(self, msg: dict) -> dict:
        return {"ok": False, "skipped": "init", "hash": "", "n": 0, "error": ""}

    def own_fill(self, path) -> None:
        pass

    def own_reconcile(self) -> bool:
        return False

    def own_retry(self):
        return None

    def own_nudge_due(self) -> bool:
        return False

    def apply_peer_services(self, digest: str, items: list) -> dict:
        return {"ok": True, "hash": digest, "n": 0, "error": ""}

    def services_retry(self):
        return None

    def services_nudge_due(self) -> bool:
        return False

    def services_scan(self) -> bool:
        return False

    def installed_report_pending(self) -> bool:
        return False

    def installed_report_done(self) -> None:
        pass
