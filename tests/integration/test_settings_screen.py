"""Клавиатура раздела обновлений: расписание циклом без «никогда», кнопка
обновления до найденной версии. Запись
настроек в YAML и кэш — tests/integration/test_settings.py."""
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


def test_quiet_hours_bounds_are_defined():
    from awgbot.bot import texts
    for key in ("quiet_hours.quiet_hours_start", "limits.traffic_bonus_gb",
                "app.scheduler.backup_hour"):
        lo, hi, label, unit = texts.SETTINGS_BOUNDS[key]
        assert lo <= hi and label and unit


def test_settings_updates_kb_has_no_never_and_reads_it_as_month(conf):
    """«Никогда» из расписания ушло: старое значение в конфиге клавиатура
    показывает месяцем, а «Уведомлять» — только по мьюту в БД (его выставит
    раздел при открытии и старт бота). Кнопки «никогда» нет вовсе."""
    from awgbot.bot import keyboards as kb
    settings.set_value("updates.poll_schedule", "never")
    labels = [b.text for row in kb.settings_updates(muted=True).inline_keyboard for b in row]
    assert labels == ["☑️ Уведомлять", "📅 Проверка: месяц", "⬅️ Назад"], labels
    assert not any("никогда" in t.lower() for t in labels)
    labels = [b.text for row in kb.settings_updates(muted=False).inline_keyboard for b in row]
    assert labels[0] == "✅ Уведомлять", labels


def test_settings_updates_kb_shows_current_schedule_on_the_cycle_button(conf):
    from awgbot.bot import keyboards as kb
    from awgbot.bot.callbacks import SetCB
    for sched, word in (("day", "день"), ("week", "неделя"), ("month", "месяц")):
        settings.set_value("updates.poll_schedule", sched)
        markup = kb.settings_updates(muted=False)
        btn = next(b for row in markup.inline_keyboard for b in row if b.text.startswith("📅"))
        assert btn.text == f"📅 Проверка: {word}", btn.text
        assert btn.callback_data == SetCB(sec="upd", act="cycle", key="updates.poll_schedule").pack()


def test_settings_updates_kb_offers_the_update_only_when_not_blocked(conf):
    """«⬆️ Обновить до vX» — первой кнопкой, когда цель найдена и не
    заблокирована; при блоке кнопки нет (причина — строкой в тексте)."""
    from awgbot.bot import keyboards as kb
    labels = [b.text for row in kb.settings_updates(False, target_tag="v3.3.1").inline_keyboard for b in row]
    assert labels[0] == "⬆️ Обновить до v3.3.1", labels
    labels = [b.text for row in kb.settings_updates(False, target_tag="3.3.1").inline_keyboard for b in row]
    assert labels[0] == "⬆️ Обновить до v3.3.1", "тег без буквы — с буквой, а не голым числом"
    labels = [b.text for row in kb.settings_updates(False, target_tag="v3.3.1",
                                                    blocked="нужен python3.12").inline_keyboard for b in row]
    assert not any(t.startswith("⬆️") for t in labels), labels
