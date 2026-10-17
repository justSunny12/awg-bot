"""Unit: core/zone — пояс показа из ключа timezone или пояс хоста.

Пути и окружение подменяются параметрами resolve/host_zone, ФС хоста не
трогается."""
from __future__ import annotations

import shutil
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo, TZPATH

import pytest

from awgbot.core import zone

pytestmark = pytest.mark.unit


def _zoneinfo_file(name: str) -> Path:
    for base in TZPATH:
        p = Path(base) / name
        if p.is_file():
            return p
    pytest.skip(f"в системной базе поясов нет {name}")


def _paths(tmp_path, *, link: str = "", copy: str = "", tz_file: str = ""):
    """Поддельные /etc/localtime и /etc/timezone во временном каталоге."""
    lt = tmp_path / "etc" / "localtime"
    lt.parent.mkdir(parents=True, exist_ok=True)
    if link:
        db = tmp_path / "usr" / "share" / "zoneinfo" / link
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_zoneinfo_file(link), db)
        lt.symlink_to(db)
    elif copy:
        shutil.copy(_zoneinfo_file(copy), lt)
    tzf = tmp_path / "etc" / "timezone"
    if tz_file:
        tzf.write_text(tz_file + "\n", encoding="utf-8")
    return {"env": {}, "localtime": str(lt), "timezone_file": str(tzf)}


def test_key_set_wins_over_host(tmp_path):
    z = zone.resolve("Europe/Berlin", **_paths(tmp_path, link="Asia/Yekaterinburg"))
    assert (z.tz, z.name, z.source, z.host_name, z.bad_value) == \
        (ZoneInfo("Europe/Berlin"), "Europe/Berlin", "conf", "Asia/Yekaterinburg", "")


def test_no_key_takes_env_tz_first(tmp_path):
    kw = _paths(tmp_path, link="Europe/Berlin")
    kw["env"] = {"TZ": ":Asia/Kolkata"}
    z = zone.resolve("", **kw)
    assert (z.tz, z.name, z.source) == (ZoneInfo("Asia/Kolkata"), "Asia/Kolkata", "host")


def test_localtime_symlink_gives_the_name_from_the_path(tmp_path):
    z = zone.resolve("", **_paths(tmp_path, link="Asia/Yekaterinburg"))
    assert (z.tz, z.name, z.source, z.host_name) == \
        (ZoneInfo("Asia/Yekaterinburg"), "Asia/Yekaterinburg", "host", "Asia/Yekaterinburg")


def test_debian_timezone_file(tmp_path):
    z = zone.resolve("", **_paths(tmp_path, tz_file="Europe/London"))
    assert (z.tz, z.name) == (ZoneInfo("Europe/London"), "Europe/London")


def test_localtime_plain_file_has_rules_but_no_name(tmp_path):
    from datetime import datetime
    z = zone.resolve("", **_paths(tmp_path, copy="Asia/Kolkata"))
    assert z.name == "" and z.source == "host"
    assert datetime(2026, 7, 1, tzinfo=z.tz).utcoffset().total_seconds() == 5.5 * 3600


def test_nothing_found_is_utc(tmp_path):
    z = zone.resolve("", **_paths(tmp_path))
    assert (z.tz, z.name, z.source) == (timezone.utc, "UTC", "host")


def test_unknown_key_falls_back_to_host_instead_of_failing(tmp_path):
    z = zone.resolve("Europe/Moskva", **_paths(tmp_path, link="Europe/London"))
    assert (z.tz, z.name, z.source, z.bad_value) == \
        (ZoneInfo("Europe/London"), "Europe/London", "conf_bad", "Europe/Moskva")
    assert zone.resolve("../../etc/passwd", **_paths(tmp_path)).source == "conf_bad"


def test_empty_key_is_no_key(tmp_path):
    kw = _paths(tmp_path, link="Asia/Yekaterinburg")
    assert zone.resolve("   ", **kw).source == "host"
    assert zone.resolve("", **kw).name == "Asia/Yekaterinburg"


def test_tests_run_in_the_pinned_zone():
    """Закрепление в conftest: шаблон без ключа — пояс «хоста» тестов."""
    from awgbot.core import config
    assert (config.TZ_NAME, config.TZ_SOURCE) == ("Asia/Yekaterinburg", "host")


def test_template_has_no_active_timezone_key():
    """Шаблон: ключ закомментирован, а не пуст — пустое значение не дало бы
    стартовать прежней версии при откате."""
    import re
    text = (Path(__file__).resolve().parents[2] / "conf" / "app.yaml").read_text(encoding="utf-8")
    assert not re.search(r"^\s*timezone:", text, re.M)
    assert re.search(r'^# timezone: "[A-Za-z_/]+"$', text, re.M)
