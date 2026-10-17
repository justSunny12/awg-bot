"""Unit: формат журнала обеих ролей (runtime/main/common).

Под journald время в строку не пишется — его ставит journald
(`journalctl -o short-iso` — со смещением); вне journald — ISO-8601 со
смещением в поясе бота, тот же пояс, что в чате."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from awgbot.runtime.main import common
from awgbot.util import timeutil

pytestmark = pytest.mark.unit

# 15.09.2026 07:00:00.123 UTC
_CREATED = datetime(2026, 9, 15, 7, 0, 0, tzinfo=timezone.utc).timestamp() + 0.123


def _record(msg="y", name="x", level=logging.INFO):
    r = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    r.created = _CREATED
    r.msecs = 123.0
    return r


def _stream_env(fd: int) -> dict:
    st = os.fstat(fd)
    return {"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino}"}


def test_outside_journald_iso_with_offset_in_the_bot_zone():
    fmt = common.log_formatter(env={})
    assert fmt.format(_record()) == "2026-09-15T12:00:00.123+05:00 INFO x: y"


def test_fractional_offset(monkeypatch):
    monkeypatch.setattr(timeutil, "TZ", ZoneInfo("Asia/Kolkata"))
    assert common.log_formatter(env={}).format(_record()) == "2026-09-15T12:30:00.123+05:30 INFO x: y"


def test_bot_zone_not_the_process_zone(monkeypatch):
    """Ключ timezone переопределяет пояс хоста: asctime — в поясе бота, а не
    по time.localtime процесса (TZ процесса в тестах — Asia/Yekaterinburg)."""
    monkeypatch.setattr(timeutil, "TZ", ZoneInfo("Europe/Moscow"))
    assert common.log_formatter(env={}).format(_record(level=logging.WARNING)) == \
        "2026-09-15T10:00:00.123+03:00 WARNING x: y"


def test_under_journald_no_time_in_the_line(tmp_path):
    with open(tmp_path / "s", "w") as f:
        fd = f.fileno()
        fmt = common.log_formatter(env=_stream_env(fd), fd=fd)
        assert fmt.format(_record("часовой пояс", "awgbot.main")) == "INFO awgbot.main: часовой пояс"


def test_inherited_journal_stream_is_not_journald(tmp_path):
    """Переменная унаследована, а вывод перенаправлен в файл — время пишем сами."""
    with open(tmp_path / "a", "w") as a, open(tmp_path / "b", "w") as b:
        assert common.under_journald(_stream_env(a.fileno()), fd=a.fileno())
        assert not common.under_journald(_stream_env(a.fileno()), fd=b.fileno())
    assert not common.under_journald({"JOURNAL_STREAM": "мусор"})
    assert not common.under_journald({})


def test_zone_line_at_start(monkeypatch, caplog):
    from awgbot.core import config
    caplog.set_level(logging.INFO, logger="awgbot.main")
    common.log_zone()
    assert "часовой пояс: Asia/Yekaterinburg (UTC+5), пояс хоста" in caplog.text
    caplog.clear()
    monkeypatch.setattr(config, "TZ_SOURCE", "conf_bad")
    monkeypatch.setattr(config, "TZ_BAD_VALUE", "Europe/Moskva")
    common.log_zone()
    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING and "'Europe/Moskva'" in rec.getMessage() \
        and "беру пояс хоста Asia/Yekaterinburg (UTC+5)" in rec.getMessage()


def test_unknown_host_zone_is_a_warning(monkeypatch, caplog):
    from awgbot.core import config
    caplog.set_level(logging.INFO, logger="awgbot.main")
    monkeypatch.setattr(config, "TZ_HOST_KNOWN", False)
    common.log_zone()
    assert any(r.levelno == logging.WARNING and "пояс хоста не определён" in r.getMessage() for r in caplog.records)
