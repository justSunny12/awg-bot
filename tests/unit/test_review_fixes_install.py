"""Установка после перепроверки выпуска: digest выпуска после перехода со
старого скрипта и права расшифрованного вручную бэкапа.

- переход 3.3.1 → 3.4.0 делает cmd_update прежнего скрипта, и
  installed-digest не пишется: digest установки берётся из списка выпусков
  (ассет своей опубликованной версии); digest, данный архивом, не трогается;
  своя сборка — без digest; digest прежней версии уходит в отложенные;
- tools.restore_backup пишет расшифрованный архив только владельцу (0600),
  а не по umask (0644 под sudo), и права уже стоящего файла тоже 0600.

Цена ошибки: все бэкапы 3.4.0 до следующего обновления — без digest, на 3.4.1
и новее они не восстановятся с переходом на свой выпуск; база с секретами,
которую читает любой пользователь хоста.
"""
from __future__ import annotations

import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.domain.backup import meta
from awgbot.domain.selfupdate import marks
from awgbot.util import secrets_util as su
from tests.unit._fleet_common import rel

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


# ── upd-1: digest установки из списка выпусков ──────────────────────────────

@pytest.fixture()
def installed(services, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "bot.db")       # каталог меток — пустой
    return services


def test_an_empty_digest_of_a_published_release_is_taken_from_the_list(installed):
    s = installed
    own = rel("v3.4.0", both=True)
    s._update_store([own, rel("v3.4.1", both=True)], {})
    assert s.db.get_state(marks.DIGEST_KEY) == f"3.4.0 {own.sha256}"
    assert meta.installed_digest(s.db) == f"sha256:{own.sha256}", "бэкапы этой установки — без digest выпуска"


def test_a_digest_given_by_the_archive_is_not_replaced(installed):
    s = installed
    s.db.set_state(marks.DIGEST_KEY, "3.4.0 " + "a" * 64)
    s._update_store([rel("v3.4.0", both=True)], {})
    assert s.db.get_state(marks.DIGEST_KEY) == "3.4.0 " + "a" * 64


def test_an_own_build_stays_without_a_digest(installed, monkeypatch):
    """Установленной версии в списке выпусков нет (своя сборка следующего
    номера) — digest не выдумывается из чужого ассета."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.2")
    installed._update_store([rel("v3.4.0", both=True), rel("v3.4.1", both=True)], {})
    assert not installed.db.get_state(marks.DIGEST_KEY)
    assert meta.installed_digest(installed.db) == ""


def test_the_digest_of_the_previous_version_is_kept_for_a_way_back(installed):
    """В базе digest 3.3.1 (её ставило самообновление кнопкой): старт 3.4.0
    откладывает его, список выпусков даёт свой — откат на 3.3.1 вернёт его."""
    s = installed
    s.db.set_state(marks.DIGEST_KEY, "3.3.1 " + "b" * 64)
    marks.adopt_installed_digest(s.db)                    # старт: installed-digest не писался
    own = rel("v3.4.0", both=True)
    s._update_store([own], {})
    assert s.db.get_state(marks.DIGEST_KEY) == f"3.4.0 {own.sha256}"
    assert ("3.3.1 " + "b" * 64) in (s.db.get_state(marks.DIGEST_PREV_KEY) or "").splitlines()


def test_a_broken_asset_digest_is_not_written(installed):
    assert marks.adopt_release_digest(installed.db, "3.4.0", "не-hex") is False
    assert not installed.db.get_state(marks.DIGEST_KEY)


# ── sec-3: расшифрованный вручную бэкап — только владельцу ──────────────────

@pytest.mark.parametrize("existing", [False, True])
def test_the_decrypted_backup_is_readable_only_by_its_owner(tmp_path, existing):
    key = os.urandom(su.KEY_SIZE)
    src = tmp_path / "awg-bot-backup-main-x.tgz.enc"
    src.write_bytes(su.encrypt(b"DUMMY backup with secrets", key=key))
    dst = tmp_path / "plain.tgz"
    if existing:
        dst.write_bytes(b"old")
        dst.chmod(0o644)
    r = subprocess.run([sys.executable, "-m", "tools.restore_backup", "--key", base64.b64encode(key).decode(),
                        "--out", str(dst), str(src)], cwd=ROOT, capture_output=True, text=True, timeout=60,
                       preexec_fn=lambda: os.umask(0o022))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dst.read_bytes() == b"DUMMY backup with secrets"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600, oct(dst.stat().st_mode)
