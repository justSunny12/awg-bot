"""
Режимы сопряжения в install/routing-link-setup.sh на ВПС: --join-stage,
--join-revert, --join-drop (§3.6–3.8 концепта «единые обновления»).

--join-stage ставит линк слота на ключи токена: новые ключи и обфускация,
без PSK (его ставит смена ключей после кода), штамп «сопряжение <id>» в
конфиге стороны ВПС — по нему файервол держит карантин; при замене
устройства (JOIN_PREV=1) живые конфиги обеих сторон — сначала в .join-prev.
--join-revert возвращает прежние ключи и линк на них; --join-drop убирает
.join-prev после верного кода.

Цена ошибки: прежнее устройство без линка после отмены замены; линк
сопряжения без штампа (без карантина); PSK, уехавший в токен.

Скрипт гоняется целиком под sh в копии, где /usr/local/sbin — временный
каталог; awg, awg-quick, ip, iptables, ss, systemctl — подставные.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd
from tests.unit.test_link_script_rekey import GW_LIVE, VPS_LIVE, _kv

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-link-setup.sh"
JID = "0123456789abcdef"


class _Vps:
    def __init__(self, tmp_path, *, live: bool):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.sbin = tmp_path / "sbin"; self.sbin.mkdir()
        self.conf_dir = tmp_path / "amneziawg"; self.conf_dir.mkdir()
        self.root = tmp_path / "root"; self.root.mkdir()
        self.log = tmp_path / "calls.log"; self.log.write_text("", encoding="utf-8")
        self.conf = self.conf_dir / "awglink.conf"
        self.gw = self.root / "gw-awglink.conf"
        if live:
            self.conf.write_text(VPS_LIVE, encoding="utf-8"); self.conf.chmod(0o600)
            self.gw.write_text(GW_LIVE, encoding="utf-8"); self.gw.chmod(0o600)
        n = tmp_path / "keyno"; n.write_text("0", encoding="utf-8")
        fake_cmd(self.bin / "id", "echo 0\n")
        fake_cmd(self.bin / "awg",
                 f'echo "awg $*" >> {self.log}\n'
                 f'case "$1" in\n'
                 f'  --version) echo "awg v3.1.20260812" ;;\n'
                 f'  genkey|genpsk) i=$(( $(cat {n}) + 1 )); echo $i > {n}; echo "DUMMY-NEW-$1-$i=" ;;\n'
                 f'  pubkey) read k; echo "DUMMY-PUB-OF-$k" ;;\n'
                 f'esac\nexit 0\n')
        for name in ("awg-quick", "ip", "iptables", "systemctl"):
            fake_cmd(self.bin / name, f'echo "{name} $*" >> {self.log}\nexit 0\n')
        fake_cmd(self.bin / "ss", "exit 0\n")
        src = SCRIPT.read_text(encoding="utf-8").replace("/usr/local/sbin", str(self.sbin))
        self.script = tmp_path / "routing-link-setup.sh"
        self.script.write_text(src, encoding="utf-8")

    def run(self, mode: str, **env) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "LINK_IF": "awglink", "LINK_CIDR": "10.99.99.0/30",
                "LINK_PORT": "51821", "CONF_DIR": str(self.conf_dir), "GW_CONF_OUT": str(self.gw),
                "_APP_YAML": str(self.tmp / "нет.yaml"), "CLIENT_SUBNET": "10.8.1.0/24",
                "LINK_KEEPALIVE": "25-35", "ENDPOINT_HOST": "vpn-server.example.org",
                "UNIT_TEMPLATE": str(self.tmp / "awg-link@.service")}
        base.update(env)
        self.log.write_text("", encoding="utf-8")
        return subprocess.run([SH, str(self.script), mode], capture_output=True, text=True, env=base, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def p(self, path: Path, suffix: str) -> Path:
        return path.with_name(path.name + suffix)


def _last(r) -> str:
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def test_join_stage_of_a_new_slot_puts_the_link_on_token_keys_with_the_stamp(tmp_path):
    vps = _Vps(tmp_path, live=False)
    r = vps.run("--join-stage", JOIN_ID=JID, LINK_PORT="8443")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == "join: staged"
    live = vps.conf.read_text(encoding="utf-8")
    assert live.splitlines()[0] == f"# awg-bot: сопряжение {JID}", live.splitlines()[:2]
    assert "PresharedKey" not in live and "PresharedKey" not in vps.gw.read_text(encoding="utf-8")
    kv = _kv(vps.gw.read_text(encoding="utf-8"))
    assert kv["Endpoint"] == "vpn-server.example.org:8443" and kv["Address"] == "10.99.99.2/30"
    assert "I1" not in kv, "I1 — только сменой ключей после кода"
    assert "# awg-bot: сопряжение" not in vps.gw.read_text(encoding="utf-8"), "штамп уехал бы в токен"
    assert oct(vps.conf.stat().st_mode)[-3:] == "600" and oct(vps.gw.stat().st_mode)[-3:] == "600"
    assert "awg-quick up awglink" in vps.calls()
    assert not list(tmp_path.glob("**/awg-gw-bundle*")), "собран файл конфигурации шлюза"


def test_join_stage_without_an_id_refuses(tmp_path):
    vps = _Vps(tmp_path, live=False)
    r = vps.run("--join-stage")
    assert r.returncode != 0 and "нет JOIN_ID" in r.stdout
    assert not vps.conf.exists()


def test_join_stage_of_a_replacement_keeps_the_old_keys_aside_once(tmp_path):
    vps = _Vps(tmp_path, live=True)
    r = vps.run("--join-stage", JOIN_ID=JID, JOIN_PREV="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert vps.p(vps.conf, ".join-prev").read_text(encoding="utf-8") == VPS_LIVE
    assert vps.p(vps.gw, ".join-prev").read_text(encoding="utf-8") == GW_LIVE
    assert _kv(vps.conf.read_text(encoding="utf-8"))["ListenPort"] == "51821", "порт слота сменился"
    first = vps.conf.read_text(encoding="utf-8")
    # новая команда того же слота: .join-prev — по-прежнему ключи прежнего устройства
    r = vps.run("--join-stage", JOIN_ID="f" * 16, JOIN_PREV="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert vps.p(vps.conf, ".join-prev").read_text(encoding="utf-8") == VPS_LIVE, "ключи токена затёрли прежние"
    assert vps.conf.read_text(encoding="utf-8") != first


def test_join_revert_gives_the_old_keys_back_and_restarts_the_link(tmp_path):
    vps = _Vps(tmp_path, live=True)
    assert vps.run("--join-stage", JOIN_ID=JID, JOIN_PREV="1").returncode == 0
    r = vps.run("--join-revert")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == "join: reverted"
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and vps.gw.read_text(encoding="utf-8") == GW_LIVE
    assert not vps.p(vps.conf, ".join-prev").exists() and not vps.p(vps.gw, ".join-prev").exists()
    assert "awg-quick up awglink" in vps.calls()
    r = vps.run("--join-revert")
    assert r.returncode == 0 and _last(r) == "join: nothing"


def test_join_drop_removes_the_old_keys_only(tmp_path):
    vps = _Vps(tmp_path, live=True)
    assert vps.run("--join-stage", JOIN_ID=JID, JOIN_PREV="1").returncode == 0
    now = vps.conf.read_text(encoding="utf-8")
    r = vps.run("--join-drop")
    assert r.returncode == 0 and _last(r) == "join: dropped"
    assert not vps.p(vps.conf, ".join-prev").exists() and vps.conf.read_text(encoding="utf-8") == now


def test_the_bundle_mode_is_gone(tmp_path):
    vps = _Vps(tmp_path, live=True)
    r = vps.run("--bundle")
    assert r.returncode == 2 and "неизвестный аргумент: --bundle" in r.stderr
