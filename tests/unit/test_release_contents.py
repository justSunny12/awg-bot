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

# Бутстрап едет ВНУТРИ архива: поставка — один артефакт (ROADMAP п.8).
BOOTSTRAP = "awg-bot-install.sh"


@pytest.mark.smoke
def test_built_package_contains_every_install_script(tmp_path):
    """Сборка целиком, во временный каталог: поставка — один архив, и в нём
    есть каждый install/*.sh вместе с бутстрапом и манифестом ядра. Скрипты
    берутся маской по каталогу: перечисление молча пропускало бы новый файл."""
    if not BUILD.exists():
        pytest.skip("build_release.sh отсутствует")
    out = tmp_path / "dist"
    proc = subprocess.run(["bash", str(BUILD), str(out)], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-2000:]

    assert (out / "awg-bot.tgz").exists(), "продукт — один архив awg-bot.tgz"
    with tarfile.open(out / "awg-bot.tgz") as tf:
        names = tf.getnames()
    shipped = {Path(n).name for n in names if "/install/" in n}
    assert not any("/tests/" in n for n in names), "тесты в продуктовую поставку не едут"

    expected = {p.name for p in INSTALL.glob("*.sh")} | {BOOTSTRAP}
    expected |= {"awg.lock"}          # версия awg прибита к поставке (ROADMAP п.8)
    missing = expected - shipped
    assert not missing, f"не доехали до поставки: {sorted(missing)}"
