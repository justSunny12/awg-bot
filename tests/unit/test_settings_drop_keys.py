"""core/settings.drop_keys: ключи снимаются из app.yaml с сохранением
комментариев; снятое возвращается списком, подписчики изменений слышат его."""
from __future__ import annotations

import pytest

from awgbot.core import settings

pytestmark = pytest.mark.unit


def test_drop_keys_keeps_comments_and_reports_changes(tmp_path):
    from tests.conftest import restore_settings
    d = tmp_path / "conf"; d.mkdir()
    (d / "app.yaml").write_text("# шапка\nscheduler:\n  a: 1        # оставить\n  backup_day: 3   # снять\n"
                                "  b: 2\n", encoding="utf-8")
    settings.init(d)
    try:
        seen = []
        settings.on_change("app.scheduler", lambda k, v: seen.append(k))
        assert settings.drop_keys(["app.scheduler.backup_day", "app.scheduler.nope", "nofile.x.y"]) == \
            ["app.scheduler.backup_day"]
        text = (d / "app.yaml").read_text(encoding="utf-8")
        assert "backup_day" not in text and "# шапка" in text and "# оставить" in text and "b: 2" in text, text
        assert settings.get("app.scheduler.backup_day") is None and settings.get_int("app.scheduler.a") == 1
        assert "app.scheduler.backup_day" in seen
        assert settings.drop_keys(["app.scheduler.backup_day"]) == []
    finally:
        restore_settings()
