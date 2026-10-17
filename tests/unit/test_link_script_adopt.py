"""
install/routing-link-setup.sh --adopt: линк, конфиг которого уже лежит
(бэкап восстановлен на пустом сервере), а юнита нет, — шаблон юнита,
экземпляр awg-link@<линк> включён и поднят (тем же --reassert), исключение
из MASQUERADE. Ключи и файлы не трогает; идемпотентен. Подставные команды
пишут вызовы в журнал, хост не трогается.
"""
from __future__ import annotations

import subprocess

import pytest

from tests.conftest import SH, fake_cmd
from tests.unit.test_link_script_rekey import SCRIPT, VPS_LIVE

pytestmark = pytest.mark.unit


class Vps:
    def __init__(self, tmp):
        self.tmp = tmp
        self.bin = tmp / "bin"; self.bin.mkdir()
        self.conf_dir = tmp / "amneziawg"; self.conf_dir.mkdir()
        self.unit = tmp / "awg-link@.service"
        self.log = tmp / "calls.log"
        fake_cmd(self.bin / "id", "echo 0\n")
        for name in ("systemctl", "install", "mkdir", "awg-quick", "ip", "awg"):
            fake_cmd(self.bin / name, f'echo "{name} $*" >> {self.log}\nexit 0\n')
        nat = tmp / "nat"; nat.write_text("", encoding="utf-8")
        # -C отвечает «есть», только когда исключение уже вставлено
        fake_cmd(self.bin / "iptables",
                 f'echo "iptables $*" >> {self.log}\n'
                 f'case " $* " in *" -C "*) grep -q x {nat} && exit 0 || exit 1 ;; '
                 f'*" -I "*) echo x > {nat} ;; esac\nexit 0\n')

    def run(self) -> subprocess.CompletedProcess:
        self.log.write_text("", encoding="utf-8")
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "LINK_IF": "awglink2", "LINK_CIDR": "10.99.99.4/30",
               "LINK_PORT": "8443", "CONF_DIR": str(self.conf_dir), "UNIT_TEMPLATE": str(self.unit),
               "GW_CONF_OUT": str(self.tmp / "gw-awglink2.conf"), "_APP_YAML": str(self.tmp / "нет.yaml"),
               "CLIENT_SUBNET": "10.8.1.0/24", "LINK_KEEPALIVE": "25-35"}
        return subprocess.run([SH, str(SCRIPT), "--adopt"], capture_output=True, text=True, env=env, timeout=30)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


def test_adopt_installs_the_template_enables_the_instance_and_exempts_nat(tmp_path):
    vps = Vps(tmp_path)
    conf = vps.conf_dir / "awglink2.conf"
    conf.write_text(VPS_LIVE, encoding="utf-8")
    r = vps.run()
    assert r.returncode == 0, r.stdout + r.stderr
    unit = vps.unit.read_text(encoding="utf-8")
    assert "ExecStart=" in unit and "--reassert" in unit and "--down" in unit
    c = vps.calls()
    assert "systemctl daemon-reload" in c and "systemctl enable --now awg-link@awglink2.service" in c, c
    assert any(x.startswith("iptables -t nat -I POSTROUTING -s 10.8.1.0/24 -o awglink2 -j ACCEPT") for x in c), c
    assert conf.read_text(encoding="utf-8") == VPS_LIVE, "ключи линка тронуты"


def test_adopt_is_idempotent(tmp_path):
    vps = Vps(tmp_path)
    (vps.conf_dir / "awglink2.conf").write_text(VPS_LIVE, encoding="utf-8")
    assert vps.run().returncode == 0
    r = vps.run()
    assert r.returncode == 0
    c = vps.calls()
    assert "systemctl daemon-reload" not in c, "шаблон переписан второй раз"
    assert not any(x.startswith("iptables -t nat -I") for x in c), "исключение из MASQUERADE вставлено дважды"
    assert "systemctl enable --now awg-link@awglink2.service" in c


def test_adopt_without_a_link_config_refuses(tmp_path):
    vps = Vps(tmp_path)
    r = vps.run()
    assert r.returncode == 1 and "ОШИБКА: конфига линка" in r.stdout
    assert not vps.unit.exists()
