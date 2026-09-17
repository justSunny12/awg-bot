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
    import awgbot.core.config as _cfg_rt; monkeypatch.setattr(_cfg_rt, "AWG_RUNTIME", "docker")
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


def test_mail_warning_when_configured_and_check_fails():
    """Ящик настроен, вход не проходит → warning с указанием на раздел."""
    class Svc:
        def server_ok(self): return True
        def email_resume_enabled(self): return True
        def email_check(self): return False, "IMAP отверг логин/пароль"
        def email_env_leftover(self): return False
        def backup_env_leftover(self): return False
    warns = preflight.collect_warnings(Svc())
    assert any("почта: IMAP отверг" in w and "E-mail" in w for w in warns)


def test_mail_check_is_skipped_when_not_configured():
    checked = []

    class Svc:
        def server_ok(self): return True
        def email_resume_enabled(self): return False
        def email_check(self): checked.append(1); return True, ""
    warns = preflight.collect_warnings(Svc())
    assert checked == []
    assert not any("env" in w for w in warns)


def _routing_svc(probed: list, verdict: str):
    from types import SimpleNamespace

    class Svc:
        db = SimpleNamespace(gateways=lambda: [SimpleNamespace(id=1)])
        def server_ok(self): return True
        def email_resume_enabled(self): return False
        def routing_status(self): return True, ""
        def routing_probe(self): probed.append(1); return verdict
        def routing_startup_warnings(self):
            from awgbot.bot import texts as _texts
            w = _texts.routing_gateway_warning(self.routing_probe(), at_start=True)
            return [w] if w else []
    return Svc()


def test_gateway_is_not_probed_while_the_feature_is_off(monkeypatch):
    """Выключенная в настройках функция спит: «шлюз не отвечает на старте»
    приходило при выключенной фиче — замер и замечание только при включённой."""
    from awgbot.core import config, settings
    from awgbot.infra import routing as rt
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    real = settings.get_bool
    state = {"app.routing.enabled": False}
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: state.get(k, real(k, d)))
    probed: list = []
    warns = preflight.collect_warnings(_routing_svc(probed, rt.PROBE_DOWN))
    assert probed == [] and not any("шлюз" in w for w in warns)

    state["app.routing.enabled"] = True
    warns = preflight.collect_warnings(_routing_svc(probed, rt.PROBE_DOWN))
    assert probed == [1]
    assert any(w.startswith("шлюз условной маршрутизации не отвечает на старте") for w in warns)
    warns = preflight.collect_warnings(_routing_svc(probed, rt.PROBE_OK))
    assert not any("шлюз" in w for w in warns)


def test_known_server_liveness_is_not_measured_twice():
    """Старт уже измерил живость awg — второй exec в замечаниях незачем."""
    asked = []

    class Svc:
        def server_ok(self): asked.append(1); return True
        def email_resume_enabled(self): return False
    warns = preflight.collect_warnings(Svc(), server_ok=False)
    assert asked == [] and any("не отвечает" in w or "молчит" in w.lower() or "awg" in w.lower()
                               for w in warns), warns
    preflight.collect_warnings(Svc())
    assert asked == [1], "без переданной живости — один замер, как раньше"
