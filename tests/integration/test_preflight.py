"""preflight: fatal-проверки валят старт с внятным текстом, warning'и копятся
и не роняют бота."""
import sqlite3

import pytest

from awgbot.runtime import preflight


def test_fatal_passes_when_no_db_yet(tmp_path, monkeypatch):
    """Первый запуск: файла БД нет — не проблема (init_schema создаст)."""
    from awgbot.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    preflight.check_fatal()                     # не бросает


def test_fatal_passes_on_healthy_db(tmp_path, monkeypatch):
    from awgbot.core import config
    db = tmp_path / "bot.db"
    con = sqlite3.connect(str(db)); con.execute("CREATE TABLE t (x)"); con.commit(); con.close()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    preflight.check_fatal()


def test_fatal_on_corrupt_db(tmp_path, monkeypatch):
    from awgbot.core import config
    db = tmp_path / "bot.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 500)   # битый файл
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    with pytest.raises(preflight.PreflightError) as ei:
        preflight.check_fatal()
    assert "БД" in str(ei.value)                # текст человекочитаемый


def test_fatal_on_unwritable_datadir(tmp_path, monkeypatch):
    """data-dir нельзя создать/записать: родитель — файл, а не каталог. mkdir
    падает NotADirectoryError даже под root (chmod root игнорит, поэтому режим-
    биты не годятся для симуляции)."""
    from awgbot.core import config
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    bad = afile / "sub"                         # путь ПОД файлом → mkdir не сможет
    monkeypatch.setattr(config, "DATA_DIR", bad)
    monkeypatch.setattr(config, "DB_PATH", bad / "bot.db")
    with pytest.raises(preflight.PreflightError) as ei:
        preflight.check_fatal()
    assert "data-dir" in str(ei.value)


def test_warnings_low_disk_and_container(tmp_path, monkeypatch):
    from awgbot.core import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    class Svc:
        def server_ok(self):
            return False                        # контейнер молчит → warning

    # диск: подменяем на «мало места» (свой namedtuple — не трогаем приватный
    # shutil._ntuple_diskusage, он не API и может уехать между версиями Python)
    from collections import namedtuple
    _du = namedtuple("usage", "total used free")
    monkeypatch.setattr(preflight.shutil, "disk_usage",
                        lambda p: _du(0, 0, 1 * 1024 * 1024))
    # awg.read_file недоступен → ещё warning
    warns = preflight.collect_warnings(Svc())
    assert any("мало места" in w for w in warns)
    assert any("контейнер" in w.lower() for w in warns)
    # format не падает
    assert "Замечания при запуске" in preflight.format_warnings(warns)


def test_warnings_never_raise(monkeypatch):
    """Сбой отдельной проверки не роняет collect_warnings."""
    class Svc:
        def server_ok(self):
            raise RuntimeError("boom")
    warns = preflight.collect_warnings(Svc())   # не бросает
    assert isinstance(warns, list)


def test_format_warnings_escapes_html():
    """str(e) в warning'е может содержать угловые скобки — они не должны ломать
    HTML-отправку (иначе предупреждение молча потеряется)."""
    msg = preflight.format_warnings(["ошибка <Foo object at 0x1> & прочее"])
    assert "&lt;Foo" in msg and "&amp;" in msg
    assert "<b>" in msg                          # своя разметка осталась


def test_imap_warning_when_enabled_and_unreachable(monkeypatch):
    """Фича email-выхода активна, IMAP недоступен → warning с адресом."""
    from awgbot.core import config
    monkeypatch.setattr(config, "EMAIL_RESUME_ENABLED", True)
    monkeypatch.setattr(config, "EMAIL_IMAP_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "EMAIL_IMAP_PORT", 1)      # закрытый порт
    monkeypatch.setattr(config, "EMAIL_RESUME_LOGIN", "x@y")
    monkeypatch.setattr(config, "EMAIL_RESUME_PASSWORD", "p")

    class Svc:
        def server_ok(self):
            return True
    warns = preflight.collect_warnings(Svc())
    assert any("IMAP" in w for w in warns)


def test_imap_skipped_when_disabled(monkeypatch):
    """Фича спит (не сконфижена) → IMAP не трогаем вовсе."""
    from awgbot.core import config
    monkeypatch.setattr(config, "EMAIL_RESUME_ENABLED", False)
    called = []
    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL",
                        lambda *a, **k: called.append(1))
    class Svc:
        def server_ok(self):
            return True
    preflight.collect_warnings(Svc())
    assert called == []
