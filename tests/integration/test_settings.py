"""Ядро settings: dotted-доступ, типы, round-trip с комментариями, diff,
on-change только по изменившимся, игнор самозаписи."""
import textwrap

import pytest

from awgbot.core import settings


@pytest.fixture
def conf(tmp_path):
    """Каталог conf с парой файлов (с комментариями) + инициализация settings."""
    (tmp_path / "limits.yaml").write_text(textwrap.dedent("""\
        # conf/limits.yaml — лимиты потребления.
        traffic_bonus_gb: 100     # разовая доп.квота, ГБ
        traffic_warn_percent: 80  # порог предупреждения, %
    """), encoding="utf-8")
    (tmp_path / "app.yaml").write_text(textwrap.dedent("""\
        # conf/app.yaml
        network:
          ssh_port: 22            # порт sshd хоста
        scheduler:
          monitor_minutes: 3
    """), encoding="utf-8")
    (tmp_path / "quiet_hours.yaml").write_text(
        "quiet_hours_enabled: true\nquiet_hours_start: 20\n", encoding="utf-8")
    settings.init(tmp_path)
    yield tmp_path
    settings._on_change.clear()
    # ВАЖНО: этот фикстур переинициализировал ГЛОБАЛЬНЫЙ settings на tmp_path.
    # Вернуть его на репозиторный conf/, иначе последующие тесты (мигрированные
    # чтения через settings.get) увидят удалённый tmp и свалятся на дефолты.
    from awgbot.core import config
    settings.init(config.CONF_DIR)


def test_get_dotted_and_types(conf):
    assert settings.get("limits.traffic_bonus_gb") == 100
    assert settings.get_int("app.network.ssh_port") == 22
    assert settings.get_bool("quiet_hours.quiet_hours_enabled") is True
    assert settings.get("app.scheduler.monitor_minutes") == 3


def test_get_missing_returns_default(conf):
    assert settings.get("limits.nope", "d") == "d"
    assert settings.get("nofile.key", 42) == 42
    assert settings.get_int("app.network.ssh_port.deeper", 7) == 7  # путь сквозь скаляр


def test_bad_key_raises(conf):
    with pytest.raises(KeyError):
        settings.get("flatkey")            # без '<файл>.<путь>'


def test_set_preserves_comments(conf):
    settings.set_value("limits.traffic_bonus_gb", 250)
    text = (conf / "limits.yaml").read_text(encoding="utf-8")
    assert "traffic_bonus_gb: 250" in text
    assert "# conf/limits.yaml — лимиты потребления." in text   # шапка цела
    assert "# разовая доп.квота, ГБ" in text                    # инлайн-коммент цел
    assert settings.get("limits.traffic_bonus_gb") == 250       # кэш обновлён


def test_set_nested_preserves_siblings(conf):
    settings.set_value("app.network.ssh_port", 2222)
    assert settings.get_int("app.network.ssh_port") == 2222
    assert settings.get("app.scheduler.monitor_minutes") == 3   # соседняя ветка цела
    text = (conf / "app.yaml").read_text(encoding="utf-8")
    assert "# порт sshd хоста" in text


def test_on_change_fires_only_for_changed(conf):
    hits = []
    settings.on_change("limits.traffic_bonus_gb", lambda k, v: hits.append((k, v)))
    settings.on_change("app.network.ssh_port", lambda k, v: hits.append((k, v)))
    settings.set_value("limits.traffic_bonus_gb", 300)
    assert hits == [("limits.traffic_bonus_gb", 300)]           # ssh_port не дёрнут


def test_on_change_prefix_match(conf):
    hits = []
    settings.on_change("app.network", lambda k, v: hits.append(k))  # префикс
    settings.set_value("app.network.ssh_port", 2200)
    assert hits == ["app.network.ssh_port"]


def test_reload_detects_external_edit(conf):
    (conf / "limits.yaml").write_text(
        "traffic_bonus_gb: 500\ntraffic_warn_percent: 80\n", encoding="utf-8")
    changed = settings.reload()
    assert "limits.traffic_bonus_gb" in changed
    assert "limits.traffic_warn_percent" not in changed        # не менялось
    assert settings.get("limits.traffic_bonus_gb") == 500


def test_reload_noop_when_unchanged(conf):
    assert settings.reload() == []


def test_self_writing_flag_clears(conf):
    assert settings.is_self_writing() is False
    settings.set_value("limits.traffic_bonus_gb", 111)
    assert settings.is_self_writing() is False                 # снят после записи


def test_migrated_read_is_hot(conf, monkeypatch):
    """Мигрированное чтение идёт через settings в точке использования: смена
    значения в кэше видна СРАЗУ, без рестарта. Проверяем сквозь notifier
    (_silent_now читает quiet_hours.* через settings.get)."""
    from awgbot.bot import notifier
    # окно тихих часов 0..24 → всегда тихо, если включено
    settings.set_value("quiet_hours.quiet_hours_enabled", True)
    settings.set_value("quiet_hours.quiet_hours_start", 0)
    settings.set_value("quiet_hours.quiet_hours_end", 23)
    assert notifier._silent_now(force_sound=False) is True
    # выключаем тихие часы на горячую — та же функция сразу видит новое
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    assert notifier._silent_now(force_sound=False) is False


def test_broken_yaml_keeps_previous_values(conf):
    """Битый YAML при reload: warning + прежние значения, без исключений."""
    assert settings.get("limits.traffic_bonus_gb") == 100
    (conf / "limits.yaml").write_text("traffic_bonus_gb: [unclosed", encoding="utf-8")
    changed = settings.reload()                    # не должно упасть
    assert changed == []                           # битый файл кэш не менял
    assert settings.get("limits.traffic_bonus_gb") == 100   # прежнее живо


def test_broken_yaml_at_init_skipped(tmp_path):
    (tmp_path / "good.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("x: [broken", encoding="utf-8")
    settings.init(tmp_path)                        # не падает
    assert settings.get("good.x") == 1
    assert settings.get("bad.x", "dflt") == "dflt"
    from awgbot.core import config
    settings.init(config.CONF_DIR)                 # восстановить для остальных


def test_set_value_creates_missing_file(tmp_path):
    """set_value на файл, которого нет в conf_dir (новый конфиг не досеян),
    не роняет KeyError — создаёт файл и пишет значение."""
    (tmp_path / "app.yaml").write_text("x: 1\n", encoding="utf-8")
    settings.init(tmp_path)
    assert not (tmp_path / "notifications.yaml").exists()
    settings.set_value("notifications.client_events.activation", False)   # не падает
    assert (tmp_path / "notifications.yaml").exists()
    assert settings.get_bool("notifications.client_events.activation") is False
    from awgbot.core import config
    settings.init(config.CONF_DIR)
