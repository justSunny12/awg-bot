"""Каталог снимков экранов — склейка модулей ролей.

Snapshot-модули (shots_admin, shots_client, shots_gateway) держат свои SHOTS и
LABEL_EXCEPTIONS; здесь — общий список, константы и Shot из base для тестов.
"""
from __future__ import annotations

from tests.screens import shots_admin, shots_client, shots_gateway
from tests.screens.base import CLIENT_TG, DISPATCHER, HOST, NOW, PEERS, VERSION, Shot, owner

__all__ = ["CLIENT_TG", "DISPATCHER", "HOST", "NOW", "PEERS", "VERSION", "Shot", "owner", "SHOTS", "LABEL_EXCEPTIONS"]

SHOTS = [*shots_admin.SHOTS, *shots_client.SHOTS, *shots_gateway.SHOTS]

LABEL_EXCEPTIONS: dict[str, set[str]] = {**shots_admin.LABEL_EXCEPTIONS, **shots_client.LABEL_EXCEPTIONS,
                                         **shots_gateway.LABEL_EXCEPTIONS}
