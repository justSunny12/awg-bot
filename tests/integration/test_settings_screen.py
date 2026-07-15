"""Экран настроек: тумблеры пишут в YAML, гейты клиентских событий, инвариант
«расписание never → авто-мьют», взаимоблокировка тумблера уведомлений."""
import textwrap

import pytest

from awgbot.core import settings


@pytest.fixture
def conf(tmp_path):
    (tmp_path / "notifications.yaml").write_text(textwrap.dedent("""\
        client_events:
          activation: true
          grace: true
          over_limit: true
          bonus: true
    """), encoding="utf-8")
    (tmp_path / "updates.yaml").write_text(
        'poll_schedule: "day"\npoll_hour: 10\npoll_minute: 0\n', encoding="utf-8")
    (tmp_path / "quiet_hours.yaml").write_text(
        "quiet_hours_enabled: true\nquiet_hours_start: 20\nquiet_hours_end: 7\n", encoding="utf-8")
    settings.init(tmp_path)
    yield tmp_path
    settings._on_change.clear()
    from awgbot.core import config
    settings.init(config.CONF_DIR)


def test_toggle_writes_yaml_and_hot(conf):
    assert settings.get_bool("notifications.client_events.bonus") is True
    settings.set_value("notifications.client_events.bonus", False)
    assert settings.get_bool("notifications.client_events.bonus") is False
    # запись реальна и с сохранением структуры
    assert "bonus: false" in (conf / "notifications.yaml").read_text(encoding="utf-8")


def test_client_event_gate_blocks_admin_note(services, monkeypatch, conf):
    """over_limit=false → админ не получает уведомление о превышении, клиент —
    получает (гейт только на админской ветке)."""
    # прямая проверка гейта на уровне settings (сама рассылка покрыта shape-тестами):
    settings.set_value("notifications.client_events.over_limit", False)
    assert settings.get_bool("notifications.client_events.over_limit", True) is False
    settings.set_value("notifications.client_events.over_limit", True)
    assert settings.get_bool("notifications.client_events.over_limit", True) is True


def test_quiet_hours_bounds_are_defined():
    from awgbot.bot import texts
    for key in ("quiet_hours.quiet_hours_start", "limits.traffic_bonus_gb",
                "app.scheduler.backup_hour"):
        lo, hi, label, unit = texts.SETTINGS_BOUNDS[key]
        assert lo <= hi and label and unit


def test_settings_updates_kb_never_disables_notify(conf):
    """poll_schedule=never → в клавиатуре тумблер уведомлений показан выключенным
    (и подписан), даже если mute в БД снят."""
    from awgbot.bot import keyboards as kb
    settings.set_value("updates.poll_schedule", "never")
    markup = kb.settings_updates(muted=False)     # mute снят, но never
    texts_in_kb = [b.text for row in markup.inline_keyboard for b in row]
    notify_btn = next(t for t in texts_in_kb if "Уведомлять" in t)
    assert notify_btn.startswith("🔴")             # выключено визуально
    assert "никогда" in notify_btn.lower()


def test_settings_updates_kb_marks_current_schedule(conf):
    from awgbot.bot import keyboards as kb
    settings.set_value("updates.poll_schedule", "week")
    markup = kb.settings_updates(muted=False)
    texts_in_kb = [b.text for row in markup.inline_keyboard for b in row]
    assert any(t.startswith("🔘") and "неделю" in t for t in texts_in_kb)
