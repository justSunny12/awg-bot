"""
Состав поставки: скрипты install обязаны доезжать до сервера.

Без них админ получает код фичи и не может её развернуть. Проверка дешёвая, а
пропажа обнаруживается уже на боевом сервере, посреди миграции: ровно так
awg-host-migrate.sh не доехал, и обновление вдобавок снесло копию, положенную
руками.
"""
from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build_release.sh"
INSTALL = ROOT / "install"

# Бутстрап едет РЯДОМ с архивом, а не внутри: им архив и разворачивают.
BOOTSTRAP = "awg-bot-install.sh"


def test_packaging_is_not_a_whitelist():
    """Скрипты берутся маской по каталогу, а не перечислением.

    Перечисление молча пропускает новый файл, и узнаёшь об этом на сервере.
    """
    src = BUILD.read_text(encoding="utf-8")
    assert '"$ROOT"/install/*.sh' in src, "install-скрипты должны браться целиком"
    assert '"$ROOT"/install/routing-*.sh' not in src, "перечисление по маскам вернулось"


@pytest.mark.smoke
def test_built_package_contains_every_install_script(tmp_path):
    """Сборка целиком: в архиве есть каждый install/*.sh, кроме бутстрапа."""
    if not BUILD.exists():
        pytest.skip("build_release.sh отсутствует")
    proc = subprocess.run(["bash", str(BUILD)], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-2000:]

    tgz = ROOT / "dist" / "awg-bot.tgz"
    with tarfile.open(tgz) as tf:
        shipped = {Path(n).name for n in tf.getnames() if "/install/" in n}

    expected = {p.name for p in INSTALL.glob("*.sh")} - {BOOTSTRAP}
    missing = expected - shipped
    assert not missing, f"не доехали до поставки: {sorted(missing)}"
