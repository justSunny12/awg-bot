"""Мелочи скриптов поставки, на которых держатся секреты и конфиг: файлы с
ключами рождаются 0600 (umask 077), а не 0644 до chmod; временный файл
состояния зоны — рядом с ним (mv из /tmp в /run — не атомарная замена);
yaml_set переживает «|» в значении (разделитель sed).

Цена ошибки: окно, в котором env или конфиг awg с приватным ключом читает
любой пользователь хоста; конфиг интерфейса после хвоста uninstall навсегда
0644; полузаписанное state.json зоны; reconfigure с «A|B» в имени рвёт
app.yaml посередине.
"""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_reserve_uninstall import CLI, _func

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RESTORE = (ROOT / "install" / "lib" / "restore.sh").read_text(encoding="utf-8")
ZONE = (ROOT / "install" / "reserve-zone-setup.sh").read_text(encoding="utf-8")


def _bash(prog: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


def test_restore_stage_creates_the_copy_owner_only_before_chmod(tmp_path):
    src = tmp_path / "env.new"
    src.write_text("BOT_TOKEN=DUMMY\n", encoding="utf-8")
    src.chmod(0o644)
    target = tmp_path / "etc" / "env"
    # chmod — пустышка: виден режим, с которым файл родился
    r = _bash("\n".join(["set -euo pipefail", "chmod() { :; }", "RESTORE_TARGETS=()",
                         _func(RESTORE, "restore_stage"), f'restore_stage "{src}" "{target}" 600']))
    assert r.returncode == 0, r.stderr
    mode = stat.S_IMODE((tmp_path / "etc" / "env.restore-new").stat().st_mode)
    assert mode & 0o077 == 0, f"копия секрета родилась {oct(mode)}"


def test_yaml_set_keeps_a_pipe_in_the_value(tmp_path):
    app = tmp_path / "app.yaml"
    app.write_text("network:\n  server_name: old  # имя\n  x: 1\n", encoding="utf-8")
    sed_i = ('sed() { if [ "$1" = -i ]; then shift; if /usr/bin/sed --version >/dev/null 2>&1; '
             'then /usr/bin/sed -i "$@"; else /usr/bin/sed -i "" "$@"; fi; else /usr/bin/sed "$@"; fi; }')
    r = _bash("\n".join(["set -euo pipefail", 'die() { echo "DIE $*" >&2; exit 1; }', sed_i,
                         _func(CLI, "yaml_set"), f"yaml_set '{app}' server_name 'A|B & C'"]))
    assert r.returncode == 0, r.stderr
    assert app.read_text(encoding="utf-8") == "network:\n  server_name: A|B & C\n  x: 1\n"


def test_zone_state_is_written_next_to_itself():
    body = _func(ZONE, "state_write")
    assert 'mktemp "$STATE_DIR/' in body and 'mv "$tmp" "$STATE"' in body, body
