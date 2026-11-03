"""core/settings, перенос раздела при init: блок `reserve` из app.yaml (до 4.2.0)
ложится поверх шаблона conf/reserve.yaml — значения админа целы, комментарии
шаблона и app.yaml на месте; раздел в app.yaml остаётся копией для отката на выпуск
линии ниже (его код читает только app.yaml) с меткой `moved` и отпечатками значений —
второй раз не переносится, а ключ, разошедшийся с отпечатком (после отката его правил
код прежней линии), переносится заново; раздел без метки (бэкап прежнего выпуска) —
переносится снова. Значения читаются как у бота (PyYAML): app.yaml с дублем ключа
переносится, метка ложится в reserve.yaml."""
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
        assert set(legacy["moved_sum"]) == {"ready", "pair_port", "quorum"}, legacy
        before = app
        settings.init(d)                                  # повторный старт — без записей
        assert (d / "reserve.yaml").read_text(encoding="utf-8") == rsv
        assert (d / "app.yaml").read_text(encoding="utf-8") == before
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


def test_the_rollback_edit_of_the_copy_moves_again_and_keeps_newer_edits(tmp_path):
    """4.2.0 перенёс → карточка правит reserve.yaml → откат: 4.1.x пишет копию в app.yaml
    тем же round-trip (метка остаётся) → снова 4.2.0: правка отката перенесена, правка
    карточки 4.2.0 в ключе, который откат не трогал, цела; следующий старт — без записей."""
    from tests.conftest import restore_settings
    d = _conf(tmp_path)
    settings.init(d)
    try:
        settings.set_value("reserve.pair_port", 47555)              # карточка 4.2.0
        settings.set_value("app.reserve.ready", False)              # 4.1.x после отката
        settings.set_value("app.reserve.quorum", {"probe_seconds": 25, "act": True})
        settings.init(d)                                            # снова 4.2.0
        assert settings.get("reserve.ready") is False, "правка отката потеряна"
        assert settings.get_int("reserve.quorum.probe_seconds") == 25
        assert settings.get_int("reserve.quorum.no_seconds") == 45, "ключ шаблона в подразделе потерян"
        assert settings.get_int("reserve.pair_port") == 47555, "правка 4.2.0 затёрта копией"
        app, rsv = (d / "app.yaml").read_text(encoding="utf-8"), (d / "reserve.yaml").read_text(encoding="utf-8")
        settings.set_value("reserve.ready", True)
        settings.init(d)                                            # отпечаток обновлён — второй раз не переносит
        assert settings.get("reserve.ready") is True
        assert (d / "app.yaml").read_text(encoding="utf-8") == app
        assert "# тумблер" in rsv and "moved" not in rsv, rsv
    finally:
        restore_settings()


def test_a_mark_without_sums_adopts_the_copy(tmp_path):
    """Метка без отпечатков (сборки 4.2.0 до них): копия — та, что перенесли; не переносится заново,
    отпечатки дописываются."""
    from tests.conftest import restore_settings
    d = _conf(tmp_path, app=APP.replace("    act: true\n", "    act: true\n  moved: reserve.yaml\n"),
              rsv=RSV.replace("ready: false", "ready: false"))
    settings.init(d)
    try:
        assert settings.get("reserve.ready") is False, "копия с меткой перенесена заново"
        assert set(settings.get("app.reserve.moved_sum")) == {"ready", "pair_port", "quorum"}
    finally:
        restore_settings()


DUP = APP.replace("  server_host: 1.2.3.4   # адрес\n", "  server_host: 1.2.3.4   # адрес\n  server_host: 5.6.7.8\n")


def test_app_yaml_with_a_duplicate_key_still_moves_the_section(tmp_path):
    """Дубль ключа в любом разделе app.yaml (ручная правка): бот читает файл PyYAML и
    работает — перенос тоже. Метку в app.yaml не записать — она ложится в reserve.yaml;
    повторный старт не переносит заново и правки reserve.yaml не затирает; исправленный
    app.yaml получает метку, запасная снимается."""
    from tests.conftest import restore_settings
    d = _conf(tmp_path, app=DUP)
    settings.init(d)
    try:
        assert settings.move_failures() == []
        assert settings.get("reserve.ready") is True and settings.get_int("reserve.pair_port") == 47200
        assert settings.get("app.network.server_host") == "5.6.7.8"
        assert (d / "app.yaml").read_text(encoding="utf-8") == DUP, "app.yaml с дублем переписан"
        assert set(settings.get("reserve.moved_from")["app.yaml"]) == {"ready", "pair_port", "quorum"}
        settings.set_value("reserve.ready", False)                  # кнопка карточки
        settings.init(d)
        assert settings.get("reserve.ready") is False, "правка reserve.yaml затёрта повторным переносом"
        rsv = (d / "reserve.yaml").read_text(encoding="utf-8")
        settings.init(d)
        assert (d / "reserve.yaml").read_text(encoding="utf-8") == rsv, "повторный старт с дублем пишет reserve.yaml"
        # дубль убрали, заодно поправили копию: правка переносится, метка — в app.yaml
        (d / "app.yaml").write_text(DUP.replace("  server_host: 5.6.7.8\n", "").replace("pair_port: 47200",
                                                                                          "pair_port: 47400"),
                                    encoding="utf-8")
        settings.init(d)
        assert settings.get_int("reserve.pair_port") == 47400 and settings.get("reserve.ready") is False
        assert settings.get("app.reserve.moved") == "reserve.yaml" and settings.get("reserve.moved_from") is None
        assert "moved" not in (d / "reserve.yaml").read_text(encoding="utf-8")
    finally:
        restore_settings()


def test_a_failed_move_is_told_to_the_admin(tmp_path, caplog):
    """reserve.yaml не разбирается — переноса нет, строка в журнал и замечание при запуске; init не падает."""
    import logging
    from awgbot.runtime import preflight
    from tests.conftest import restore_settings
    broken = RSV + "ready: true\n"
    d = _conf(tmp_path, rsv=broken)
    caplog.set_level(logging.WARNING)
    settings.init(d)
    try:
        assert (d / "reserve.yaml").read_text(encoding="utf-8") == broken
        assert any("не перенесён" in r.getMessage() for r in caplog.records)
        assert "moved" not in (d / "app.yaml").read_text(encoding="utf-8"), "метка без переноса"
        warns = preflight._settings_move_warnings()
        assert len(warns) == 1 and "раздел reserve из conf/app.yaml не перенесён в conf/reserve.yaml" in warns[0], warns
        assert "found duplicate key" in warns[0], warns
    finally:
        restore_settings()
    assert settings.move_failures() == []


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


def test_the_move_shim_is_still_needed():
    """Сторож снятия: перенос держат, пока UPDATE_FROM ниже 4.2.0 (хосты 4.0/4.1 прыгают сюда
    напрямую), копию и повторный перенос — пока COMPAT_FLOOR ниже 4.2.0 (откат, бэкапы)."""
    from awgbot.core import compat
    if not settings._MOVED_SECTIONS:
        return
    v = lambda raw: tuple(int(x) for x in raw.split(".")[:3])      # noqa: E731
    assert v(compat.UPDATE_FROM) < (4, 2, 0) or v(compat.COMPAT_FLOOR) < (4, 2, 0), \
        ("шим переноса пора снять вместе с копией: UPDATE_FROM и COMPAT_FLOOR не ниже 4.2.0 — "
         "убрать _MOVED_SECTIONS и шагом снять раздел reserve из живых app.yaml (drop_keys app.reserve)")
