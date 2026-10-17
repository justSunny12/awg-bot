"""
Личные списки локальной сети без VPN (awg-gw-vpn-user.conf, awg-gw-ru-user.conf)
в резервной копии шлюза: копия их кладёт, скрипт обвязки подхватывает
отложенные при первом включении режима. `awg-bot restore` на шлюзе бэкапов не
разворачивает (настройки шлюза — в бэкапе сервера AWG, канон своих списков —
на сервере).

Списки набраны руками в чате агента («в туннель», «напрямую») — бандл их не
везёт, и замена шлюза без копии теряла бы их молча: локальная сеть снова ходит
«как было», а человек не помнит, что туда добавлял.

Копия — боевой make_backup агента с подменённым путём /etc/dnsmasq.d; подхват
— прогон фрагмента install/routing-gw-setup.sh (sh) с каталогами во временной
папке и подставным systemctl. Хост не трогается.
"""
from __future__ import annotations

import builtins
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database
from tests.conftest import SH

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
VPN = "# awg-bot (шлюз): личный список\nnftset=/rutracker.org/4#inet#awg_lan#lan_vpn4\n"
RU = "# awg-bot (шлюз): личный список\nnftset=/gosuslugi.ru/4#inet#awg_lan#lan_ru4\n"
PHRASE = "correct horse battery"


# ── копия ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def backup(tmp_path, monkeypatch):
    """Агент шлюза с копией во временной папке; /etc/dnsmasq.d — тоже там."""
    from awgbot.core import config
    from awgbot.infra import gwguard
    d = Database(tmp_path / "gw.db"); d.init_schema()
    svc = GatewayServices(d)
    svc.backup_set_passphrase(PHRASE)
    gwd = tmp_path / "awg"; gwd.mkdir()
    (gwd / "awglink.conf").write_text("[Interface]\nPrivateKey = B\n")
    confd = tmp_path / "conf"; confd.mkdir()
    dnsmasq = tmp_path / "dnsmasq.d"; dnsmasq.mkdir()
    monkeypatch.setattr(config, "GW_CONF_DIR", str(gwd))
    monkeypatch.setattr(config, "CONF_DIR", confd)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(config, "ENV_PATH", None)
    monkeypatch.setattr(gwguard, "FW_ENV", str(tmp_path / "no-firewall.env"))
    real_open = builtins.open

    def _open(path, *a, **kw):
        p = str(path)
        if p.startswith("/etc/dnsmasq.d/"):
            path = dnsmasq / p[len("/etc/dnsmasq.d/"):]
        return real_open(path, *a, **kw)
    from awgbot.domain.backup import archive as bc        # состав копии живёт там
    monkeypatch.setattr(bc, "open", _open, raising=False)

    def members() -> dict[str, bytes]:
        from awgbot.util import secrets_util
        paths = svc.make_backup()
        raw = secrets_util.decrypt(open(paths[0], "rb").read(), passphrase=PHRASE)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
    yield dnsmasq, members
    d.close()


def test_the_backup_carries_both_personal_lists_byte_for_byte(backup):
    """Оба списка едут в копию как есть, под awg-gw/lan/: именно оттуда их
    берёт `awg-bot restore`."""
    dnsmasq, members = backup
    (dnsmasq / "awg-gw-vpn-user.conf").write_text(VPN, encoding="utf-8")
    (dnsmasq / "awg-gw-ru-user.conf").write_text(RU, encoding="utf-8")
    (dnsmasq / "awg-gw-vpn-feed.conf").write_text("nftset=/x/4#inet#awg_lan#lan_vpn4\n")
    got = members()
    assert got.get("awg-gw/lan/awg-gw-vpn-user.conf") == VPN.encode(), "список «в туннель» не попал в копию"
    assert got.get("awg-gw/lan/awg-gw-ru-user.conf") == RU.encode(), "список «напрямую» не попал в копию"
    assert not any("feed" in n for n in got), "фид — не данные человека, он приедет с ВПС сам"


def test_a_missing_list_does_not_break_the_backup(backup):
    """Режим без VPN не включали или один из списков не заводили — копия
    обязана собраться с тем, что есть, а не упасть на отсутствующем файле."""
    dnsmasq, members = backup
    (dnsmasq / "awg-gw-ru-user.conf").write_text(RU, encoding="utf-8")
    got = members()
    assert "awg-gw/lan/awg-gw-ru-user.conf" in got
    assert "awg-gw/lan/awg-gw-vpn-user.conf" not in got, "пустое место выдано за список"
    assert "awg/awglink.conf" in got, "без одного из списков копия потеряла остальное"


# ── awg-bot restore ──────────────────────────────────────────────────────────

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


def test_first_enable_takes_the_parked_lists_instead_of_empty_ones(tmp_path):
    """Восстановили копию на новую шлюз, потом включили режим без VPN:
    личные списки обязаны встать из копии, а не пустыми заготовками — иначе
    восстановление было бы видимостью."""
    parked = tmp_path / "var-lib-awg-gw" / "restore"; parked.mkdir(parents=True)
    (parked / "awg-gw-vpn-user.conf").write_text(VPN, encoding="utf-8")
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (dnsmasq / "awg-gw-vpn-user.conf").read_text() == VPN, "отложенный список не подхвачен"
    assert (dnsmasq / "awg-gw-ru-user.conf").read_text().startswith("# awg-bot (шлюз): личный список"), (
        "второго списка в копии не было — нужна обычная заготовка")
    assert not (parked / "awg-gw-vpn-user.conf").exists(), "отложенный список остался и встанет ещё раз"
    assert "changed=1" in r.stdout, "dnsmasq не узнает о новых списках"


def test_enable_over_existing_lists_keeps_them(tmp_path):
    """Списки уже стоят (человек пополнял их после восстановления) — отложенная
    копия их не перетирает, и перезапуск dnsmasq ради них не нужен."""
    parked = tmp_path / "var-lib-awg-gw" / "restore"; parked.mkdir(parents=True)
    (parked / "awg-gw-vpn-user.conf").write_text(VPN, encoding="utf-8")
    dnsmasq = tmp_path / "dnsmasq.d"; dnsmasq.mkdir()
    mine = "nftset=/свежий.рф/4#inet#awg_lan#lan_vpn4\n"
    (dnsmasq / "awg-gw-vpn-user.conf").write_text(mine, encoding="utf-8")
    (dnsmasq / "awg-gw-ru-user.conf").write_text(RU, encoding="utf-8")
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (dnsmasq / "awg-gw-vpn-user.conf").read_text() == mine, "свежий список перетёрт старой копией"
    assert "changed=0" in r.stdout, "неизменившиеся списки дёргают dnsmasq"


def test_enable_without_anything_parked_writes_empty_lists(tmp_path):
    """Обычная первая установка: отложенного нет (каталога тоже) — две пустые
    заготовки, как было всегда."""
    r, dnsmasq, _ = _enable(tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("awg-gw-vpn-user.conf", "awg-gw-ru-user.conf"):
        assert (dnsmasq / name).read_text() == "# awg-bot (шлюз): личный список — awg-bot lan add/ru/del\n"
    r2, _, _ = _enable(tmp_path)                  # повтор ничего не портит
    assert "changed=0" in r2.stdout
