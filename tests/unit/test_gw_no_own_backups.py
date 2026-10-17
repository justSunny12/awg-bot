"""
Своих бэкапов у шлюза нет (настройки шлюза — в бэкапе сервера AWG, копия
настроек — по каналу конфигурации): на старте агент стирает следы копий
прежних версий — фразу и случайный ключ, отметки месячной копии и почты
«вслед за сервером», вопрос и памятку о фразе с сервера, ключи расписания
`app.scheduler.backup_*` из своего conf, файлы копий из каталога бэкапов. Возврата на прежние версии нет —
держать их незачем; повторный старт ничего не трогает.

Цена ошибки: фраза в базе шлюза — секрет без дела (с ней прежняя версия
шифровала копии с ключами линка); вопрос о фразе в очереди висел бы
навсегда — раздела в документе больше нет и ответить на него нечем.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import settings
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.gateway.keep import LEFTOVER_CONF, LEFTOVER_STATE
from awgbot.infra.db import Database
from tests.unit import test_gw_cfg_apply as _apply

pytestmark = pytest.mark.unit

conf_dir = _apply.conf_dir


@pytest.fixture()
def svc(tmp_path, conf_dir):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


def _old_gateway(svc) -> None:
    """Шлюз 3.3.1: фраза, ключ, отметки, вопрос о фразе с памяткой и почтой рядом."""
    for key in LEFTOVER_STATE:
        svc.db.set_state(key, "DUMMY-" + key)
    svc.db.set_state(svc._CFG_WAIT_KEY, json.dumps({"backup": {"mine": "a", "server": "b"},
                                                    "mail": {"mine": "", "server": "d"}}))
    svc.db.set_state(svc._CFG_PENDING_KEY, json.dumps({"backup": {"phrase": "DUMMY-server-phrase"},
                                                       "mail": {"login": "box@example.org"}}))
    svc.db.set_state(svc._CFG_KEEP_KEY, json.dumps({"backup": "ef" * 8}))


def test_the_start_wipes_the_leftovers_of_own_backups(svc, conf_dir):
    _old_gateway(svc)
    assert settings.get("app.scheduler.backup_day") is not None, "в conf нет ключей — тест ничего не проверяет"
    assert svc.backup_leftovers_drop() is True
    assert all(not svc.db.get_state(k) for k in LEFTOVER_STATE), [k for k in LEFTOVER_STATE if svc.db.get_state(k)]
    assert list(svc.cfg_wait()) == ["mail"] and list(svc.cfg_pending()) == ["mail"], "почта ушла вместе с фразой"
    assert svc.cfg_keep() == {} and not svc.db.get_state(svc._CFG_KEEP_KEY)
    assert svc.cfg_questions() == ["mail"]
    for key in LEFTOVER_CONF:
        assert settings.get(key) is None, key
    text = (conf_dir / "app.yaml").read_text(encoding="utf-8")
    assert "backup_" not in text, "ключ расписания копий остался в app.yaml"
    assert "monthly_reset_day" in text and "# сброс месячного трафика" in text, "соседние ключи и комментарии пострадали"
    assert svc.backup_leftovers_drop() is False, "второй старт снова что-то стёр"


def test_a_clean_gateway_has_nothing_to_wipe_but_the_conf_keys(svc, conf_dir):
    """Новая установка 3.4.0: в базе следов нет; шаблон conf общий с сервером —
    ключи расписания снимаются, всё прочее нетронуто."""
    before = {k: settings.get(k) for k in ("app.scheduler.monthly_reset_day", "app.gateway.monitor_minutes")}
    assert svc.backup_leftovers_drop() is True
    assert svc.backup_leftovers_drop() is False
    assert {k: settings.get(k) for k in before} == before


def test_a_broken_conf_does_not_stop_the_start(svc, conf_dir):
    """conf сломан руками — ключи подождут, база всё равно чистится, старт идёт."""
    _old_gateway(svc)
    (conf_dir / "app.yaml").write_text("scheduler:\n  backup_day: 1\nscheduler:\n  x: [\n", encoding="utf-8")
    assert svc.backup_leftovers_drop() is True
    assert not svc.db.get_state("backup_passphrase")


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


def test_the_start_wipes_old_backup_files(svc, conf_dir, tmp_path, monkeypatch):
    """Файлы копий прежних версий — с ключами линка, применить их нельзя:
    стираются; чужое в каталоге не трогается; каталога нет — не ошибка."""
    from awgbot.core import config
    bk = tmp_path / "backups"
    monkeypatch.setattr(config, "BACKUP_DIR", bk)
    assert svc.backup_leftovers_drop() is True            # каталога нет: ключи conf, без падения
    bk.mkdir()
    olds = ["awg-bot-backup-gw-20260914_030000.tgz.enc", "awg-gw-pi-2026-09-14.tgz",
            "restore-pending.tgz.enc", "snap.tar.gz"]
    for name in olds:
        (bk / name).write_bytes(b"DUMMY")
    (bk / "notes.txt").write_text("своё", encoding="utf-8")
    assert svc.backup_leftovers_drop() is True
    assert sorted(p.name for p in bk.iterdir()) == ["notes.txt"]
    assert svc.backup_leftovers_drop() is False
