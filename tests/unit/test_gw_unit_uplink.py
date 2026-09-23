"""
Конфиг аплинка шлюза (UPLINK_B64) — только на время применения бандла.

Внутри UPLINK_B64 — конфиг клиентского туннеля малины с приватным ключом.
Прежние выпуски закрепляли его в юните awg-link-gw.service строкой
Environment и оставляли юнит 0644: любой локальный пользователь малины (OMV,
шары) читал ключ. Теперь переменная живёт только в окружении применения, юнит
пишется без неё и с правами 0600, а реассерт из юнита (загрузка, рестарт)
аплинк не переставляет — ему нечем.

Прогоняются сами блоки install/routing-gw-setup.sh под sh: раздел
«Автозапуск» (запись юнита) и шаг «0. Шлюзовое устройство» с подставными
iface_by_pubkey/uplink_list, run и awg-quick. Хост не трогается.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"
PUB = "DUMMY"
SECRET = "UPLINK-PRIVATE-KEY-DO-NOT-LEAK"
UPLINK_CONF = f"[Interface]\nPrivateKey = {SECRET}\nAddress = 10.8.1.15/32\nTable = off\n\n[Peer]\nPublicKey = S==\n"
UPLINK_B64 = base64.b64encode(UPLINK_CONF.encode()).decode()


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _unit_block(script: str) -> str:
    """Раздел «Автозапуск»: от записи юнита до выставления прав."""
    start = script.index('cat > "$UNIT" <<UNITEOF')
    end = script.index('chmod 0600 "$UNIT"', start)
    return script[start:script.index("\n", end) + 1]


def _step0(script: str) -> str:
    """Шаг «0. Шлюзовое устройство» целиком, до записи статуса."""
    start = script.index('step "0. Шлюзовое устройство"')
    return script[start:script.index("write_status() {", start)]


def _sh(prog: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", prog], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", **env}, timeout=30)


# ── юнит ─────────────────────────────────────────────────────────────────────

class _Unit:
    """Юнит во временном каталоге и окружение, с которым его пишет скрипт."""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "awg-link-gw.service"
        self.env = {
            "UNIT": str(self.path), "SELF": "/usr/local/sbin/routing-gw-setup.sh",
            "CLIENT_SUBNET": "10.8.1.0/24", "LINK_IF": "awglink", "ADMIN_IPS": "10.8.1.2",
            "GATEWAY_PUBKEY": PUB, "GATEWAY_PREV_PUBKEY": "", "UPLINK_B64": UPLINK_B64,
            "UPLINK_IF": "awg0", "LAN_MODE": "0", "HOME_SUBNETS": "", "RESOLVER": "",
            "PEER_HOME_NETS": "", "LINK_CHANNEL": "1", "LINK_CHANNEL_PORT": "8787",
            "FW_ENV": "/etc/awg-gw/firewall.env", "HOST_CONF_DIR": "/etc/amnezia/amneziawg",
        }

    def write(self, script: str) -> subprocess.CompletedProcess:
        return _sh("set -e\n" + _unit_block(script), self.env)

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def mode(self) -> int:
        return self.path.stat().st_mode & 0o777


def test_the_unit_keeps_the_gateway_keys_but_not_the_uplink_config(script, tmp_path):
    """Применение бандла с конфигом аплинка в окружении: юнит получает ключи
    шлюза (по ним реассерт узнаёт свою машину), но не сам конфиг с приватным
    ключом — ни в base64, ни как-то ещё."""
    unit = _Unit(tmp_path)
    r = unit.write(script)
    assert r.returncode == 0, r.stderr
    text = unit.text()
    assert f"Environment=GATEWAY_PUBKEY={PUB}" in text, "ключ шлюза не закреплён — реассерт не узнает машину"
    assert "UPLINK_B64" not in text, "конфиг аплинка снова закрепляется в юните"
    assert UPLINK_B64 not in text and SECRET not in text, "приватный ключ аплинка попал в юнит"


def test_the_unit_is_readable_only_by_root_even_over_an_old_0644_one(script, tmp_path):
    """Юнит прежнего выпуска лежал с 0644 и с конфигом аплинка внутри.
    Перезапись поверх обязана и убрать секрет, и закрыть права: cat > файл
    права существующего файла не меняет, их выставляет только chmod."""
    unit = _Unit(tmp_path)
    unit.path.write_text(f"[Service]\nEnvironment=UPLINK_B64={UPLINK_B64}\n", encoding="utf-8")
    unit.path.chmod(0o644)
    r = unit.write(script)
    assert r.returncode == 0, r.stderr
    assert unit.mode() == 0o600, f"права юнита {oct(unit.mode())} — читает любой пользователь малины"
    assert SECRET not in unit.text() and UPLINK_B64 not in unit.text(), "секрет прежнего юнита пережил перезапись"
    r = unit.write(script)                        # повторное применение ничего не портит
    assert r.returncode == 0 and unit.mode() == 0o600


# ── шаг 0: аплинк ставится только из бандла ──────────────────────────────────

class _Machine:
    """Малина для шага 0: каталог конфигов, аплинк по ключу, журнал команд."""

    def __init__(self, tmp_path: Path, *, uplink: str = "awg0"):
        self.conf_dir = tmp_path / "amneziawg"
        self.conf_dir.mkdir()
        self.etc = tmp_path / "awg-gw"
        self.log = tmp_path / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.uplink = uplink
        awg = tmp_path / "awg-quick"
        awg.write_text(f'#!/bin/sh\necho "awg-quick $*" >> "{self.log}"\n', encoding="utf-8")
        awg.chmod(0o755)
        self.awg = awg
        if uplink:
            (self.conf_dir / f"{uplink}.conf").write_text("[Interface]\nPrivateKey = OLD==\n", encoding="utf-8")

    def run(self, script: str, *, uplink_b64: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        prelude = f'''
set -e
say()  {{ printf '%s\\n' "$*"; }}
step() {{ :; }}
run()  {{ echo "RUN $*" >> "{self.log}"; }}
iface_by_pubkey() {{ [ "$1" = "{PUB}" ] && printf '%s' "{self.uplink}"; return 0; }}
uplink_list() {{ [ -n "{self.uplink}" ] && printf '%s pub\\n' "{self.uplink}"; return 0; }}
GW_ETC="{self.etc}"; HOST_CONF_DIR="{self.conf_dir}"; AWG_QUICK="{self.awg}"
TG_MARK=0x1; UPLINK_TABLE=100; UPLINK_IF_DEFAULT=awg0; UPLINK_IF=""
GATEWAY_PUBKEY="{PUB}"; GATEWAY_PREV_PUBKEY=""; UPLINK_B64="{uplink_b64}"
GW_FOREIGN=0; GW_UNCONFIRMED=0
'''
        r = _sh(prelude + _step0(script) + '\necho "UPLINK_STATE=$UPLINK_STATE UPLINK_IF=$UPLINK_IF"\n', {})
        return r, [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln]


def _touches_uplink(calls: list[str]) -> list[str]:
    """Вызовы, трогающие сам аплинк: подъём/спуск интерфейса и установка его
    конфига. Оверрайд юнита awg-quick@ (перезапуск до победы) сюда не входит."""
    return [c for c in calls if c.startswith("awg-quick ") or c.startswith("RUN install")
            or c.startswith("RUN cp") or "/awg-quick " in c]


def test_a_reassert_without_the_uplink_config_leaves_the_uplink_alone(script, tmp_path):
    """Загрузка малины: юнит зовёт скрипт с тем, что в нём закреплено, —
    UPLINK_B64 там больше нет. Аплинк — связь агента с Telegram; реассерт не
    должен ни переставлять его конфиг, ни дёргать интерфейс."""
    pi = _Machine(tmp_path)
    before = (pi.conf_dir / "awg0.conf").read_text(encoding="utf-8")
    r, calls = pi.run(script, uplink_b64="")
    assert r.returncode == 0, r.stderr
    assert "UPLINK_STATE=none UPLINK_IF=awg0" in r.stdout, r.stdout
    assert not _touches_uplink(calls), f"реассерт тронул аплинк: {calls}"
    assert (pi.conf_dir / "awg0.conf").read_text(encoding="utf-8") == before, "конфиг аплинка переписан"


def test_the_bundle_still_installs_a_changed_uplink_config(script, tmp_path):
    """Обратная сторона: при применении бандла конфиг аплинка из окружения
    по-прежнему ставится — иначе смена ключей аплинка не доезжала бы вовсе."""
    pi = _Machine(tmp_path)
    r, calls = pi.run(script, uplink_b64=UPLINK_B64)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "UPLINK_STATE=installed" in r.stdout, r.stdout
    assert any(c.startswith("RUN install -m 600") and c.endswith("/awg0.conf") for c in calls), calls
    assert "awg-quick up awg0" in calls, f"аплинк не переподнят с новым конфигом: {calls}"
    assert not list(pi.etc.glob("uplink.new*")), "временный конфиг аплинка с ключом остался на диске"


def test_a_fresh_machine_without_the_uplink_config_does_not_invent_an_uplink(script, tmp_path):
    """Чистая машина и реассерт без UPLINK_B64: ставить аплинк не из чего.
    Скрипт не назначает имя по умолчанию и ничего не поднимает, а говорит,
    что подтвердить машину нечем."""
    pi = _Machine(tmp_path, uplink="")
    r, calls = pi.run(script, uplink_b64="")
    assert r.returncode == 0, r.stderr
    assert "UPLINK_STATE=none UPLINK_IF=" in r.stdout and "UPLINK_IF=awg0" not in r.stdout, r.stdout
    assert not _touches_uplink(calls), calls
    assert "аплинка этой машины не видно" in r.stdout
