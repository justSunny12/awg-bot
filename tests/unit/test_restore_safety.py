"""
Восстановление обязано быть обратимым.

`awg-bot restore` перезаписывает БД, конфиг и секреты. Запускают его ровно
тогда, когда уже всё плохо, — а значит ошибиться снимком проще всего. Без
снимка «до» старый архив молча уносит всё, что появилось после него; однажды так
и исчезли три дня состояния.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "awg-bot.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _restore(script: str) -> str:
    return script.split("cmd_restore()", 1)[1].split("\ncmd_uninstall()", 1)[0]


def test_restore_snapshots_current_state_first(script):
    body = _restore(script)
    assert "prerestore" in body, "снимок текущего состояния не делается"
    snap = body.index("prerestore")
    overwrite = body.index('cp -a {} "$DATA_DIR/"')
    assert snap < overwrite, "снимок обязан быть ДО перезаписи"


def test_restore_snapshot_is_taken_after_the_service_stops(script):
    """Иначе БД снимается под записью бота и снимок может оказаться битым."""
    body = _restore(script)
    assert body.index('systemctl stop "$SERVICE"') < body.index("prerestore")


def test_restore_tells_how_to_undo(script):
    """Путь к снимку без команды возврата бесполезен в панике."""
    body = _restore(script)
    assert "awg-bot restore $pre" in body


def test_restore_removes_stale_wal(script):
    """WAL/SHM принадлежат заменяемому файлу БД.

    Рядом с чужой базой они в лучшем случае будут отброшены по несовпадению
    соли, в худшем — доложены в файл, которому не родня.
    """
    body = _restore(script)
    assert "db-wal" in body and "db-shm" in body
    assert body.index("db-wal") < body.index('cp -a {} "$DATA_DIR/"')
