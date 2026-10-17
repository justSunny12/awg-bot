"""
Личные списки локальной сети без VPN (awg-gw-vpn-user.conf, awg-gw-ru-user.conf)
при снятии режима уходят в /var/lib/awg-gw/restore, и следующее включение
берёт их оттуда; нет — пустые заготовки. Списки набраны руками в чате агента
(«в туннель», «напрямую») — выключить и снова включить режим не значит
потерять их.

Прогон фрагмента install/routing-gw-setup.sh (sh) с каталогами во временной
папке. Хост не трогается.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
VPN = "# awg-bot (шлюз): личный список\nnftset=/rutracker.org/4#inet#awg_lan#lan_vpn4\n"
RU = "# awg-bot (шлюз): личный список\nnftset=/gosuslugi.ru/4#inet#awg_lan#lan_ru4\n"


def _lan_lists_loop() -> str:
    src = (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    start = src.index("    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do\n        [ -f \"$DNSMASQ_D/$_f\" ] && continue")
    return src[start:src.index("\n    done\n", start) + len("\n    done\n")]


def _enable(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path, Path]:
    dnsmasq = tmp_path / "dnsmasq.d"; dnsmasq.mkdir(exist_ok=True)
    dump = tmp_path / "var-lib-awg-gw"
    prog = ('set -e\nrun(){ sh -c "$*"; }\n'
            f'DNSMASQ_D="{dnsmasq}"\nLAN_DUMP="{dump}"\n_dn_changed=0\n'
            + _lan_lists_loop() + 'echo "changed=$_dn_changed"\n')
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    return r, dnsmasq, dump


def test_the_next_enable_takes_the_parked_lists_instead_of_empty_ones(tmp_path):
    parked = tmp_path / "var-lib-awg-gw" / "restore"; parked.mkdir(parents=True)
    (parked / "awg-gw-vpn-user.conf").write_text(VPN, encoding="utf-8")
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (dnsmasq / "awg-gw-vpn-user.conf").read_text() == VPN, "отложенный список не подхвачен"
    assert (dnsmasq / "awg-gw-ru-user.conf").read_text().startswith("# awg-bot (шлюз): личный список"), (
        "второго списка не откладывали — нужна обычная заготовка")
    assert not (parked / "awg-gw-vpn-user.conf").exists(), "отложенный список остался и встанет ещё раз"
    assert "changed=1" in r.stdout, "dnsmasq не узнает о новых списках"


def test_enable_over_existing_lists_keeps_them(tmp_path):
    parked = tmp_path / "var-lib-awg-gw" / "restore"; parked.mkdir(parents=True)
    (parked / "awg-gw-vpn-user.conf").write_text(VPN, encoding="utf-8")
    dnsmasq = tmp_path / "dnsmasq.d"; dnsmasq.mkdir()
    mine = "nftset=/свежий.рф/4#inet#awg_lan#lan_vpn4\n"
    (dnsmasq / "awg-gw-vpn-user.conf").write_text(mine, encoding="utf-8")
    (dnsmasq / "awg-gw-ru-user.conf").write_text(RU, encoding="utf-8")
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (dnsmasq / "awg-gw-vpn-user.conf").read_text() == mine, "свежий список перетёрт отложенным"
    assert "changed=0" in r.stdout, "неизменившиеся списки дёргают dnsmasq"


def test_enable_without_anything_parked_writes_empty_lists(tmp_path):
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("awg-gw-vpn-user.conf", "awg-gw-ru-user.conf"):
        assert (dnsmasq / name).read_text() == "# awg-bot (шлюз): личный список — awg-bot lan add/ru/del\n"
    r2, _, _ = _enable(tmp_path)                  # повтор ничего не портит
    assert "changed=0" in r2.stdout
