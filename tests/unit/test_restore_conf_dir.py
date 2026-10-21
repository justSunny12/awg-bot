"""install/lib/restore.sh `awg_conf_dir`: каталог конфигов awg хоста — ключ
`conf_dir` раздела `awg:` app.yaml; раздела нет — первое вхождение ключа
(прежнее поведение, у шлюза — `gateway.conf_dir`); ничего нет — умолчание
/etc/amnezia/amneziawg. Функция гоняется bash'ем из настоящего скрипта.

Цена ошибки: ключ `conf_dir` есть и в разделе gateway; взятый оттуда на ВПС
каталог — восстановление раскладывает конфиги интерфейсов и линка пары не
туда, awg-quick поднимает прежние (или никакие), клиенты без VPN.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


def _conf_dir(script: str, tmp: Path, app: str | None) -> str:
    conf = tmp / "conf"
    conf.mkdir(exist_ok=True)
    if app is not None:
        (conf / "app.yaml").write_text(app, encoding="utf-8")
    prog = "\n".join(["set -euo pipefail", f'CONF_DIR="{conf}"', _func(script, "yaml_get"),
                      _func(script, "awg_conf_dir"),
                      # как зовёт restore_apply: подстановкой под set -e
                      'awgdir="$(awg_conf_dir)"', 'printf "%s" "$awgdir"'])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return r.stdout


GW_FIRST = ('role: "client"\n'
            'gateway:\n'
            '  link_interface: "awglink"\n'
            '  conf_dir: "/srv/gw-awg"           # каталог шлюза\n'
            '\n'
            'awg:\n'
            '  protocol_id: "amnezia-awg2"\n'
            '  conf_dir: "/srv/vps-awg"   # каталог awg-quick\n'
            '  interface: "awg0"\n'
            'network:\n'
            '  conf_dir: "/srv/not-this"\n')


@pytest.mark.parametrize("app, want", [
    pytest.param(GW_FIRST, "/srv/vps-awg", id="awg-section-wins-over-an-earlier-gateway-key"),
    pytest.param(GW_FIRST.replace('  conf_dir: "/srv/vps-awg"   # каталог awg-quick\n', '  conf_dir: /srv/vps-awg\n'),
                 "/srv/vps-awg", id="unquoted-value"),
    pytest.param('gateway:\n  conf_dir: "/srv/gw-awg"\n', "/srv/gw-awg", id="no-awg-section-first-key-as-before"),
    pytest.param('awg:\n  interface: "awg0"\nother:\n  conf_dir: "/srv/other"\n', "/srv/other",
                 id="awg-section-without-the-key-falls-back"),
    pytest.param('awg:\n  interface: "awg0"\n', "/etc/amnezia/amneziawg", id="no-key-anywhere"),
    pytest.param(None, "/etc/amnezia/amneziawg", id="no-app-yaml"),
])
def test_the_conf_dir_comes_from_the_awg_section(script, tmp_path, app, want):
    assert _conf_dir(script, tmp_path, app) == want


def test_the_shipped_template_gives_the_default_dir(script, tmp_path):
    tpl = (Path(__file__).resolve().parents[2] / "conf" / "app.yaml").read_text(encoding="utf-8")
    assert _conf_dir(script, tmp_path, tpl) == "/etc/amnezia/amneziawg"
