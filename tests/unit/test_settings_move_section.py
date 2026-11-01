"""core/settings, перенос раздела при init: блок `reserve` из app.yaml (до 4.2.0)
ложится поверх шаблона conf/reserve.yaml — значения админа целы, комментарии
шаблона и app.yaml на месте; раздел в app.yaml остаётся копией для отката на выпуск
линии ниже (его код читает только app.yaml) с меткой `moved` — второй раз не
переносится, правки копии не действуют; раздел без метки (бэкап прежнего выпуска) —
переносится снова."""
from __future__ import annotations

import pytest

from awgbot.core import settings

pytestmark = pytest.mark.unit

APP = ("# шапка\nnetwork:\n  server_host: 1.2.3.4   # адрес\n"
       "reserve:\n  ready: true\n  pair_port: 47200\n  quorum:\n    probe_seconds: 20\n    act: true\n")
RSV = ("# шаблон\nready: false                 # тумблер\npair_port: 47101\npkg_minutes: 30\n"
       "quorum:\n  probe_seconds: 15            # проба\n  no_seconds: 45\n")


def _conf(tmp_path, app: str = APP, rsv: str | None = RSV):
    d = tmp_path / "conf"; d.mkdir()
    (d / "app.yaml").write_text(app, encoding="utf-8")
    if rsv is not None:
        (d / "reserve.yaml").write_text(rsv, encoding="utf-8")
    return d


def test_the_section_moves_over_the_template_keeping_values_and_comments(tmp_path):
    from tests.conftest import restore_settings
    d = _conf(tmp_path)
    settings.init(d)
    try:
        assert settings.get("reserve.ready") is True and settings.get_int("reserve.pair_port") == 47200
        assert settings.get_int("reserve.pkg_minutes") == 30, "ключ шаблона, которого не было в разделе, потерян"
        assert settings.get_int("reserve.quorum.probe_seconds") == 20 and settings.get_int("reserve.quorum.no_seconds") == 45
        assert settings.get("reserve.quorum.act") is True, "ключ раздела, которого нет в шаблоне, потерян"
        legacy = settings.get("app.reserve")
        assert legacy["moved"] == "reserve.yaml" and legacy["ready"] is True and legacy["pair_port"] == 47200, \
            "копия для отката потеряла значения"
        app, rsv = (d / "app.yaml").read_text(encoding="utf-8"), (d / "reserve.yaml").read_text(encoding="utf-8")
        assert "перенесён в reserve.yaml" in app and "# шапка" in app and "# адрес" in app, app
        assert "# шаблон" in rsv and "# тумблер" in rsv and "# проба" in rsv and "moved" not in rsv, rsv
        settings.set_value("app.reserve.pair_port", 1)    # правка копии — не действует и не переносится
        settings.init(d)                                  # повторный старт — без перемен
        assert (d / "reserve.yaml").read_text(encoding="utf-8") == rsv
        assert settings.get_int("reserve.pair_port") == 47200
    finally:
        restore_settings()


def test_without_the_template_the_section_becomes_the_file(tmp_path):
    from tests.conftest import restore_settings
    d = _conf(tmp_path, rsv=None)
    settings.init(d)
    try:
        assert settings.get("reserve.ready") is True and settings.get_int("reserve.quorum.probe_seconds") == 20
        assert settings.get("app.reserve.moved") == "reserve.yaml"
    finally:
        restore_settings()


def test_a_broken_app_yaml_is_left_alone(tmp_path, caplog):
    """app.yaml не разбирается — переноса нет, строка в журнал, init не падает; reserve.yaml не тронут."""
    import logging
    from tests.conftest import restore_settings
    d = _conf(tmp_path, app="reserve:\n  ready: true\n  ready: false\n")
    caplog.set_level(logging.WARNING)
    settings.init(d)
    try:
        assert (d / "reserve.yaml").read_text(encoding="utf-8") == RSV
        assert any("не перенесён" in r.getMessage() for r in caplog.records)
    finally:
        restore_settings()


def test_a_restored_old_section_without_the_mark_moves_again(tmp_path):
    """Восстановлен бэкап прежнего выпуска: раздел без метки — его значения снова главные."""
    from tests.conftest import restore_settings
    d = _conf(tmp_path)
    settings.init(d)
    try:
        (d / "app.yaml").write_text(APP.replace("pair_port: 47200", "pair_port: 47300"), encoding="utf-8")
        settings.init(d)
        assert settings.get_int("reserve.pair_port") == 47300 and settings.get("app.reserve.moved") == "reserve.yaml"
    finally:
        restore_settings()


def test_no_section_no_writes(tmp_path):
    from tests.conftest import restore_settings
    d = _conf(tmp_path, app="network:\n  server_host: 1.2.3.4\n")
    before = (d / "app.yaml").stat().st_mtime_ns, (d / "reserve.yaml").stat().st_mtime_ns
    settings.init(d)
    try:
        assert ((d / "app.yaml").stat().st_mtime_ns, (d / "reserve.yaml").stat().st_mtime_ns) == before
    finally:
        restore_settings()
