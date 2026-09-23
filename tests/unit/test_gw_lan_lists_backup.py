"""
Личные списки локальной сети без VPN (awg-gw-vpn-user.conf, awg-gw-ru-user.conf)
в резервной копии шлюза: копия их кладёт, `awg-bot restore` возвращает,
скрипт обвязки подхватывает отложенные при первом включении режима.

Списки набраны руками в чате агента («в туннель», «напрямую») — бандл их не
везёт, и замена малины без копии теряла бы их молча: квартира снова ходит
«как было», а человек не помнит, что туда добавлял.

Копия — боевой make_backup агента с подменённым путём /etc/dnsmasq.d;
восстановление и подхват — прогон самих фрагментов awg-bot.sh (bash) и
install/routing-gw-setup.sh (sh) с каталогами во временной папке и
подставным systemctl. Хост не трогается.
"""
from __future__ import annotations

import builtins
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
VPN = "# awg-bot (шлюз): личный список\nnftset=/rutracker.org/4#inet#awg_home#lan_vpn4\n"
RU = "# awg-bot (шлюз): личный список\nnftset=/gosuslugi.ru/4#inet#awg_home#lan_ru4\n"
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
    monkeypatch.setattr(gw, "open", _open, raising=False)

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
    (dnsmasq / "awg-gw-vpn-feed.conf").write_text("nftset=/x/4#inet#awg_home#lan_vpn4\n")
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

def _restore_fragment() -> str:
    src = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    body = src.split("cmd_restore()", 1)[1].split("\ncmd_uninstall()", 1)[0]
    start = body.index("    # Личные списки локальной сети без VPN")
    return body[start:body.index("    # маркер для бота", start)]


class _Pi:
    """Малина для восстановления: распакованная копия, conf-dir dnsmasq,
    каталог отложенного, systemctl, который записывает вызовы."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path / "unpacked"
        self.lan = self.tmp / "awg-gw" / "lan"
        self.dnsmasq = tmp_path / "dnsmasq.d"
        self.state = tmp_path / "var-lib-awg-gw"
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.log = tmp_path / "systemctl.log"; self.log.write_text("")
        self.restart_rc = 0

    def backup_has(self, **files: str) -> None:
        self.lan.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (self.lan / name).write_text(text, encoding="utf-8")

    def lan_mode_applied(self) -> None:
        self.dnsmasq.mkdir(exist_ok=True)
        (self.dnsmasq / "awg-gw-base.conf").write_text("listen-address=192.168.68.2\n")

    def restore(self) -> subprocess.CompletedProcess:
        (self.bin / "systemctl").write_text(
            f'#!/bin/sh\necho "$*" >> "{self.log}"\nexit {self.restart_rc}\n', encoding="utf-8")
        (self.bin / "systemctl").chmod(0o755)
        frag = (_restore_fragment()
                .replace("/etc/dnsmasq.d", str(self.dnsmasq))
                .replace("/var/lib/awg-gw", str(self.state)))
        prog = ('ok(){ echo "OK $*"; }\nwarn(){ echo "WARN $*"; }\nlog(){ echo "LOG $*"; }\n'
                f'tmp="{self.tmp}"\nset -e\n' + frag + 'echo DONE\n')
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin"})

    def systemctl(self) -> list[str]:
        return [ln for ln in self.log.read_text().splitlines() if ln]


def test_restore_puts_the_lists_in_place_and_restarts_dnsmasq_once(tmp_path):
    """Режим без VPN на машине уже применён: списки встают в conf-dir и dnsmasq
    перечитывается — иначе домены из списков не наполнят наборы до ребута."""
    pi = _Pi(tmp_path)
    pi.lan_mode_applied()
    (pi.dnsmasq / "awg-gw-vpn-user.conf").write_text("# пустая заготовка\n")
    pi.backup_has(**{"awg-gw-vpn-user.conf": VPN, "awg-gw-ru-user.conf": RU})
    r = pi.restore()
    assert r.returncode == 0 and "DONE" in r.stdout, r.stderr + r.stdout
    assert (pi.dnsmasq / "awg-gw-vpn-user.conf").read_text() == VPN, "список из копии не лёг поверх заготовки"
    assert (pi.dnsmasq / "awg-gw-ru-user.conf").read_text() == RU
    for name in ("awg-gw-vpn-user.conf", "awg-gw-ru-user.conf"):
        assert (pi.dnsmasq / name).stat().st_mode & 0o777 == 0o644, f"{name}: dnsmasq под своим пользователем не прочтёт"
    assert pi.systemctl() == ["restart dnsmasq"], f"dnsmasq перезапущен не один раз: {pi.systemctl()}"
    assert "OK личные списки локальной сети восстановлены" in r.stdout
    assert not pi.state.exists(), "при применённом режиме списки ещё и отложены"


def test_restore_before_the_mode_is_applied_parks_the_lists_and_leaves_dnsmasq_alone(tmp_path):
    """Малина новая, режим без VPN ещё не включали: dnsmasq может быть чужим
    (OMV) или не стоять вовсе. Списки откладываются, dnsmasq не трогается."""
    pi = _Pi(tmp_path)
    pi.backup_has(**{"awg-gw-vpn-user.conf": VPN})
    r = pi.restore()
    assert r.returncode == 0 and "DONE" in r.stdout, r.stderr + r.stdout
    assert (pi.state / "restore" / "awg-gw-vpn-user.conf").read_text() == VPN
    assert not pi.dnsmasq.exists(), "в conf-dir чужого dnsmasq положены наши файлы"
    assert pi.systemctl() == [], f"dnsmasq тронут до включения режима: {pi.systemctl()}"
    assert "отложены" in r.stdout


def test_restore_says_so_when_dnsmasq_does_not_come_back(tmp_path):
    """Списки легли, а dnsmasq не перезапустился — человек должен узнать это
    сразу и куда смотреть, а восстановление — дойти до конца."""
    pi = _Pi(tmp_path)
    pi.lan_mode_applied()
    pi.backup_has(**{"awg-gw-ru-user.conf": RU})
    pi.restart_rc = 1
    r = pi.restore()
    assert r.returncode == 0 and "DONE" in r.stdout, "отказ dnsmasq оборвал восстановление"
    assert (pi.dnsmasq / "awg-gw-ru-user.conf").read_text() == RU
    assert "WARN" in r.stdout and "journalctl -u dnsmasq" in r.stdout, r.stdout
    assert "OK личные списки" not in r.stdout


def test_a_backup_without_lists_changes_nothing(tmp_path):
    """Копия прежнего выпуска или шлюз без режима — списков в ней нет. Ничего
    не создаётся и dnsmasq не перезапускается."""
    pi = _Pi(tmp_path)
    pi.lan_mode_applied()
    pi.tmp.mkdir()
    r = pi.restore()
    assert r.returncode == 0 and "DONE" in r.stdout, r.stderr + r.stdout
    assert pi.systemctl() == [] and not pi.state.exists()
    assert sorted(p.name for p in pi.dnsmasq.iterdir()) == ["awg-gw-base.conf"]


# ── подхват отложенного при включении режима ─────────────────────────────────

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
    r = subprocess.run(["sh", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    return r, dnsmasq, dump


def test_first_enable_takes_the_parked_lists_instead_of_empty_ones(tmp_path):
    """Восстановили копию на новую малину, потом включили режим без VPN:
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
    mine = "nftset=/свежий.рф/4#inet#awg_home#lan_vpn4\n"
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
