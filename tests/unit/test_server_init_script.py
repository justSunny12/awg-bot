"""
install/awg-server-init.sh на хосте, где сервер уже есть: топология читается
из живого конфига интерфейса и печатается установщику (KEY=VALUE), конфиг не
трогается. Прогоном скрипта на временном каталоге awg-quick, а не чтением
исходника.

Выпуск с кворумом резерва добавляет awg0 второй адрес из блока пиров-зондов
169.254.88.0/24. Прими скрипт его за подсеть пользователей — установщик
запишет в app.yaml префикс 169.254.88, и новые устройства получат адреса вне
awg0, то есть останутся без сети; а сервер «.1» блока станет адресом
резолвера, на котором никто не отвечает.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install" / "awg-server-init.sh"


def _run(tmp_path, conf: str | None):
    """Прогон с --plan (root не нужен) на каталоге tmp_path; conf — текст awg0.conf."""
    quick = tmp_path / "amneziawg"
    quick.mkdir()
    if conf is not None:
        (quick / "awg0.conf").write_text(conf, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "AWG_IF": "awg0", "AWG_QUICK_DIR": str(quick)}
    cp = subprocess.run(["bash", str(SCRIPT), "--plan"], env=env, capture_output=True, text=True, timeout=30)
    kv = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
    return cp, kv


def _conf(address: str) -> str:
    return f"[Interface]\n{address}\nListenPort = 43125\nPrivateKey = DUMMY\n\n[Peer]\nPublicKey = DUMMY\n" \
           f"AllowedIPs = 10.8.1.2/32\n"


@pytest.mark.parametrize("address", [
    "Address = 10.8.1.1/24",
    "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26",
    "Address = 169.254.88.1/26\nAddress = 10.8.1.1/24",
    "Address = 169.254.88.65/26 # кворум\nAddress = 10.8.1.1/24",
])
def test_an_existing_server_reports_the_users_subnet_not_the_probe_block(tmp_path, address):
    cp, kv = _run(tmp_path, _conf(address))
    assert cp.returncode == 0, cp.stderr
    assert kv.get("CREATED") == "0", f"скрипт не признал готовый сервер: {cp.stdout}"
    assert (kv.get("SUBNET_PREFIX"), kv.get("SUBNET_CIDR"), kv.get("SERVER_ADDR")) == \
        ("10.8.1", "10.8.1.0/24", "10.8.1.1"), f"подсеть пользователей взята из блока зондов:\n{cp.stdout}"
    assert kv.get("LISTEN_PORT") == "43125"


def test_a_server_whose_only_address_is_in_the_block_keeps_it(tmp_path):
    """Запасной путь: других адресов нет — это и есть подсеть пользователей.
    Под `set -euo pipefail` пустой вывод grep не должен ронять скрипт:
    установщик остался бы без топологии и упал на полпути обновления."""
    cp, kv = _run(tmp_path, _conf("Address = 169.254.88.1/24"))
    assert cp.returncode == 0, f"скрипт упал на сервере с подсетью в блоке:\n{cp.stderr}"
    assert (kv.get("SUBNET_PREFIX"), kv.get("SERVER_ADDR")) == ("169.254.88", "169.254.88.1"), cp.stdout


def test_a_conf_without_address_lines_still_reports_the_server(tmp_path):
    """Конфиг без Address (ручная правка): grep без строк не валит скрипт,
    топология — умолчание, конфиг не перезаписан."""
    conf = "[Interface]\nListenPort = 43125\nPrivateKey = DUMMY\n"
    cp, kv = _run(tmp_path, conf)
    assert cp.returncode == 0, f"скрипт упал на конфиге без Address:\n{cp.stderr}"
    assert kv.get("CREATED") == "0" and kv.get("SUBNET_PREFIX") == "10.8.1" and kv.get("LISTEN_PORT") == "43125"
    assert (tmp_path / "amneziawg" / "awg0.conf").read_text(encoding="utf-8") == conf, "готовый конфиг тронут"
