"""digest установки: установщик одной командой и ручной `awg-bot update` знают
выпуск, из которого поставлен код.

Метка бэкапа берёт digest выпуска из базы (update_installed_digest), и без
него переход на версию бэкапа недоступен. Его ставило только самообновление
кнопкой — у сервера, поставленного одной командой (умер прежний — новый
ставят так), бэкапы были без digest ровно там, где он нужнее всего. Теперь
установщик (verify_archive) и `awg-bot update` при совпадении sha256 архива с
ассетом выпуска той же версии пишут $DATA_DIR/installed-digest, бот на старте
переносит его в базу. Своя сборка (--skip-verify) и нет ответа GitHub — файла
нет. Функции скриптов прогоняются bash'ем из настоящих файлов, curl подменён.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "install" / "awg-bot-install.sh"
BOT = ROOT / "awg-bot.sh"
_BLOB = b"DUMMY release archive\n"
_HEX = hashlib.sha256(_BLOB).hexdigest()
_META_HEX = hashlib.sha256(b"DUMMY meta").hexdigest()


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


def _curl(bin_dir: Path, mode: str) -> None:
    """mode: match — выпуск с ассетами поставки и метаданных (поставка второй);
    other — у выпуска другой sha256; down — нет сети (curl падает)."""
    if mode == "down":
        body = "#!/bin/sh\nexit 6\n"
    else:
        tgz = _HEX if mode == "match" else "0" * 64
        body = ("#!/bin/sh\necho \"$@\" >> \"$CALLS\"\ncat <<'EOF'\n{\n  \"tag_name\": \"v3.4.0\",\n"
                "  \"assets\": [\n    {\n      \"name\": \"awg-bot-release.json\",\n"
                f"      \"digest\": \"sha256:{_META_HEX}\"\n    }},\n    {{\n      \"name\": \"awg-bot.tgz\",\n"
                f"      \"digest\": \"sha256:{tgz}\"\n    }}\n  ]\n}}\nEOF\n")
    p = bin_dir / "curl"
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)


def _tree(tmp: Path, version: str = "3.4.0") -> tuple[Path, Path]:
    src = tmp / "src"
    (src / "awgbot").mkdir(parents=True)
    (src / "conf").mkdir()
    (src / "awgbot" / "__version__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (src / "conf" / "updates.yaml").write_text('repo: "DUMMY/awg-bot"\n', encoding="utf-8")
    tgz = tmp / "awg-bot.tgz"
    tgz.write_bytes(_BLOB)
    return src, tgz


def _run(prog: str, tmp: Path, curl: str, env: dict | None = None) -> subprocess.CompletedProcess:
    bin_dir = tmp / "bin"; bin_dir.mkdir(exist_ok=True)
    _curl(bin_dir, curl)
    (tmp / "calls").write_text("", encoding="utf-8")
    e = {"PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin", "CALLS": str(tmp / "calls"), **(env or {})}
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20, env=e)


# ── установщик ──────────────────────────────────────────────────────────────

def _installer(tmp: Path, *, skip: bool = False, curl: str = "match", env: dict | None = None):
    src = INSTALLER.read_text(encoding="utf-8")
    s, tgz = _tree(tmp)
    data = tmp / "data"; data.mkdir()
    prog = "\n".join([
        "set -euo pipefail",                       # как в настоящем установщике
        'log() { echo "LOG $*"; }', 'die() { echo "DIE $*"; exit 1; }',
        f'SKIP_VERIFY={1 if skip else 0}; DATA_DIR="{data}"',
        _func(src, "verify_archive"), _func(src, "note_installed_digest"),
        f'verify_archive "{tgz}" "{s}"', "note_installed_digest",
        'echo "VERIFIED=${AWG_BOT_VERIFIED_SHA256:-}"',
    ])
    r = _run(prog, tmp, curl, env)
    f = data / "installed-digest"
    return r, (f.read_text(encoding="utf-8") if f.exists() else None), f


def test_installer_writes_the_file_when_the_archive_matches_the_release(tmp_path):
    r, text, f = _installer(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert text == f"3.4.0 {_HEX}\n"
    assert f.stat().st_mode & 0o777 == 0o600
    assert "целостность: sha256 совпал с релизом v3.4.0" in r.stdout
    # поставка — не первым ассетом: сверка — со всеми digest выпуска, не с первым
    assert f"VERIFIED={_HEX}" in r.stdout, "установщику из поставки (труба) совпавший sha256 не передан"
    assert "DUMMY/awg-bot/releases/tags/v3.4.0" in (tmp_path / "calls").read_text(encoding="utf-8")


def test_installer_refuses_a_foreign_archive_and_writes_nothing(tmp_path):
    r, text, _ = _installer(tmp_path, curl="other")
    assert r.returncode == 1 and "DIE sha256 архива не совпал" in r.stdout
    assert text is None


def test_installer_without_github_goes_on_without_the_file(tmp_path):
    """GitHub не ответил (лимит, нет сети) — установка продолжается: отказ curl
    под pipefail раньше молча обрывал установщик."""
    r, text, _ = _installer(tmp_path, curl="down")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "GitHub не ответил — ставлю без сверки" in r.stdout
    assert text is None


def test_own_build_with_skip_verify_has_no_file(tmp_path):
    r, text, _ = _installer(tmp_path, skip=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "сверка отключена (--skip-verify)" in r.stdout and text is None
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "", "своя сборка — без запроса к GitHub"


@pytest.mark.parametrize("given, written", [(_HEX, True), ("f" * 64, False)])
def test_the_piped_installer_passes_the_verified_sum_on(tmp_path, given, written):
    """Труба сверила sha256 и зовёт установщик из поставки с --skip-verify: тот
    второй раз GitHub не спрашивает, но выпуск знает — если архив тот самый."""
    r, text, _ = _installer(tmp_path, skip=True, env={"AWG_BOT_VERIFIED_SHA256": given})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (text == f"3.4.0 {_HEX}\n") is written
    assert ("sha256 уже сверен с релизом v3.4.0" in r.stdout) is written
    assert (tmp_path / "calls").read_text(encoding="utf-8") == ""


def test_the_installer_writes_the_file_after_laying_out_the_tree():
    src = INSTALLER.read_text(encoding="utf-8")
    body = src[src.index('log "создаю каталоги'):]
    assert body.index('mkdir -p "$INSTALL_DIR" "$ETC_DIR" "$DATA_DIR"') < body.index("note_installed_digest")
    assert body.index("verify_archive") < body.index("note_installed_digest") < body.index("reconfigure --first-run")


# ── awg-bot update ──────────────────────────────────────────────────────────

def _update(tmp: Path, curl: str, *, stale: bool = False, env: dict | None = None):
    src = flat()
    s, tgz = _tree(tmp)
    data = tmp / "data"; data.mkdir()
    conf = tmp / "conf"; conf.mkdir()
    (conf / "updates.yaml").write_text('repo: "DUMMY/awg-bot"\n', encoding="utf-8")
    f = data / "installed-digest"
    if stale:
        f.write_text(f"3.4.0 {'e' * 64}\n", encoding="utf-8")
    prog = "\n".join([
        "set -euo pipefail", 'log() { echo "LOG $*"; }',
        f'DATA_DIR="{data}"; CONF_DIR="{conf}"; INSTALL_DIR="{tmp / "opt"}"',
        f'INSTALLED_DIGEST_FILE="{f}"',
        _func(src, "yaml_get"), _func(src, "release_sha256s"), _func(src, "note_installed_digest"),
        f'note_installed_digest "{tgz}" 3.4.0', "echo DONE",
    ])
    r = _run(prog, tmp, curl, env)
    assert r.returncode == 0 and "DONE" in r.stdout, r.stdout + r.stderr
    return r, (f.read_text(encoding="utf-8") if f.exists() else None), f


def test_update_writes_the_file_when_the_archive_matches(tmp_path):
    r, text, f = _update(tmp_path, "match")
    assert text == f"3.4.0 {_HEX}\n" and f.stat().st_mode & 0o777 == 0o600
    assert "sha256 архива совпал с выпуском v3.4.0" in r.stdout


@pytest.mark.parametrize("curl", ["other", "down"])
def test_update_without_a_match_leaves_no_file_not_even_a_stale_one(tmp_path, curl):
    """Своя сборка или нет ответа — файла нет; прежний (прерванное обновление
    той же версии) — прочь: чужую сборку он назвал бы выпуском."""
    r, text, _ = _update(tmp_path, curl, stale=True)
    assert text is None
    assert "архив не сверен с выпуском v3.4.0" in r.stdout


def test_update_from_the_piped_installer_does_not_ask_github_again(tmp_path):
    """Установщик из трубы уже сверил этот архив (AWG_BOT_VERIFIED_SHA256
    доходит до `awg-bot update` через exec): второго запроса к API нет."""
    r, text, _ = _update(tmp_path, "down", env={"AWG_BOT_VERIFIED_SHA256": _HEX})
    assert text == f"3.4.0 {_HEX}\n"
    assert (tmp_path / "calls").read_text(encoding="utf-8") == ""


def test_a_verified_sum_of_another_archive_is_not_trusted(tmp_path):
    """Сумма от трубы — чужого архива (пересобран): сверка с GitHub как обычно."""
    r, text, _ = _update(tmp_path, "match", env={"AWG_BOT_VERIFIED_SHA256": "f" * 64})
    assert text == f"3.4.0 {_HEX}\n"
    assert "DUMMY/awg-bot/releases/tags/v3.4.0" in (tmp_path / "calls").read_text(encoding="utf-8")


def test_the_installer_hands_the_verified_sum_to_the_update():
    src = INSTALLER.read_text(encoding="utf-8")
    assert 'export AWG_BOT_VERIFIED_SHA256="$have"' in _func(src, "verify_archive")
    upd = src[src.index('AWG_UPDATE_KEEP=1 AWG_UPDATE_CLEANUP="$cleanup" exec "$BOT"'):].splitlines()[0]
    assert 'exec "$BOT" update "$TGZ"' in upd and "env -i" not in upd


def test_update_checks_before_touching_the_service():
    src = flat()
    upd = _func(src, "cmd_update")
    assert 'note_installed_digest "$tgz" "$newver"' in upd
    assert upd.index("откат ниже $floor невозможен") < upd.index("note_installed_digest") \
        < upd.index('systemctl stop "$SERVICE"')


# ── старт бота: перенос в базу ──────────────────────────────────────────────

def test_start_moves_the_file_into_the_state_and_the_backup_label_carries_it(services, monkeypatch, tmp_path):
    from awgbot.core import config
    from awgbot.domain.backup import meta
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    f = tmp_path / "installed-digest"
    f.write_text(f"3.4.0 {_HEX}\n", encoding="utf-8")
    assert services.adopt_installed_digest() is True
    assert services.db.get_state("update_installed_digest") == f"3.4.0 {_HEX}"
    assert not f.exists(), "файл остался — следующий старт перенёс бы его снова"
    assert meta.installed_digest(services.db) == f"sha256:{_HEX}"
    assert services.adopt_installed_digest() is False, "нет файла — нечего переносить"
    assert services.db.get_state("update_installed_digest") == f"3.4.0 {_HEX}"


@pytest.mark.parametrize("raw", [f"3.4.1 {_HEX}", "3.4.0 not-a-sum", "", "3.4.0"])
def test_a_file_of_another_install_is_dropped_and_the_state_kept(services, monkeypatch, tmp_path, raw):
    """Обновление прервалось до старта или откатилось — файл чужой версии; битый
    — тоже не наш: база не трогается, файл прочь."""
    from awgbot.core import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    services.db.set_state("update_installed_digest", "3.4.0 " + "a" * 64)
    f = tmp_path / "installed-digest"
    f.write_text(raw + "\n", encoding="utf-8")
    assert services.adopt_installed_digest() is False
    assert services.db.get_state("update_installed_digest") == "3.4.0 " + "a" * 64
    assert not f.exists()


def test_both_roles_move_the_file_before_the_update_report():
    """Итог самообновления кнопкой пишет свой digest поверх — перенос раньше
    него; обе роли."""
    from awgbot.runtime.main import client, gateway
    for mod in (client, gateway):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        assert text.index("services.adopt_installed_digest()") < text.index("report_update_result"), mod.__name__


def test_the_previous_version_keeps_its_digest_across_a_rollback(services, monkeypatch, tmp_path):
    """Новая версия переписывает digest своим; сторож откатил — прежняя
    версия получает свой digest назад, бэкапы снова со ссылкой на выпуск."""
    from awgbot.core import config
    from awgbot.domain.backup import meta
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")
    old, new = "3.4.0 " + "a" * 64, f"3.4.1 {_HEX}"
    services.db.set_state("update_installed_digest", old)
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")             # первый старт новой
    (tmp_path / "installed-digest").write_text(new + "\n", encoding="utf-8")
    assert services.adopt_installed_digest() is True
    assert services.db.get_state("update_installed_digest") == new
    assert services.db.get_state("update_installed_digest_prev") == old
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")             # сторож откатил
    services.adopt_installed_digest()
    assert services.db.get_state("update_installed_digest") == old
    assert meta.installed_digest(services.db) == "sha256:" + "a" * 64
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")             # повтор того же обновления
    services.adopt_installed_digest()
    assert services.db.get_state("update_installed_digest") == new, "digest того же выпуска не вернулся"
    services.adopt_installed_digest()                                    # обычный перезапуск — без перестановок
    assert (services.db.get_state("update_installed_digest"),
            services.db.get_state("update_installed_digest_prev")) == (new, old)
