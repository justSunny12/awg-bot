"""
awg-lan-services.sh — записи SMB подсетей других шлюзов: белый список строк
(в Python и в sh — одинаково), проверка и откат, копия для отката вне conf-dir.
"""
from __future__ import annotations

import subprocess

import pytest
from tests.conftest import SH
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import (
    OLD_SVC, _failing_restart, _heredoc, _no_copies, _snapshot_restart, _snapshots, _svc_file,
)

script, svc_env = _c.script, _c.svc_env


# ── сервисы соседних сетей: awg-lan-services.sh ─────────────────────────────
# файл записей собирает агент из
# данных, пришедших каналом с ВПС; помощник — последний рубеж на шлюзе.


def test_services_file_is_checked_installed_and_dnsmasq_restarted(svc_env):
    go, conf, log = svc_env
    text = _svc_file()
    r = go(text)
    assert r.returncode == 0, r.stderr
    assert conf.read_text(encoding="utf-8") == text
    assert r.stdout.strip() == "записи SMB подсетей других шлюзов применены: 2", r.stdout
    out = log.read_text()
    assert "dnsmasq --test --conf-dir=" in out and out.index("dnsmasq --test") < out.index("systemctl restart dnsmasq"), \
        "конфиг проверен до рестарта"


def test_a_single_foreign_line_refuses_the_whole_file(svc_env):
    """Скомпрометированный ВПС не должен получить каналом ни server=, ни
    address=: одна строка вне белого списка — отказ целиком (rc=2), прежний
    файл на месте, dnsmasq не тронут."""
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go(_svc_file() + "server=/awg.internal/203.0.113.5\n")
    assert r.returncode == 2 and "вне белого списка" in r.stderr, (r.returncode, r.stderr)
    assert conf.read_text(encoding="utf-8") == OLD_SVC
    assert "systemctl" not in log.read_text() and "dnsmasq --test" not in log.read_text()


def test_a_file_dnsmasq_test_rejects_is_rolled_back(svc_env):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go(_svc_file(), DNSMASQ_TEST_RC="1")
    assert r.returncode == 1 and "dnsmasq --test отверг записи SMB — откатываю" in r.stderr, r.stderr
    assert conf.read_text(encoding="utf-8") == OLD_SVC, "прежний файл не вернулся"
    assert not (conf.parent / "awg-gw-peer-services.conf.prev.awg").exists(), "копия отката в conf-dir"


def test_a_first_file_dnsmasq_cannot_start_with_is_removed(svc_env, tmp_path):
    go, conf, log = svc_env
    _failing_restart(tmp_path / "bin", log)
    r = go(_svc_file())
    assert r.returncode == 1 and "dnsmasq не поднялся с записями SMB — откатываю" in r.stderr, r.stderr
    assert not conf.exists(), "файл, с которым dnsmasq не встал, остался"


def test_the_same_file_does_not_restart_dnsmasq(svc_env):
    """Рестарт роняет кэш DNS всей сети — на тот же файл его не делаем."""
    go, conf, log = svc_env
    go(_svc_file())
    n = log.read_text().count("systemctl restart dnsmasq")
    r = go(_svc_file())
    assert r.returncode == 0 and r.stdout.strip() == "записи SMB подсетей других шлюзов без изменений", r.stdout
    assert log.read_text().count("systemctl restart dnsmasq") == n, "тот же файл — а рестарт был"


def test_no_argument_removes_the_file_once(svc_env):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go(None)
    assert r.returncode == 0 and not conf.exists()
    assert r.stdout.strip() == "записи SMB подсетей других шлюзов сняты", r.stdout
    assert log.read_text().count("systemctl restart dnsmasq") == 1
    r = go(None)
    assert r.returncode == 0
    assert log.read_text().count("systemctl restart dnsmasq") == 1, "снимать нечего — а рестарт был"


def test_an_empty_file_and_a_non_root_run_are_refused(svc_env):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    r = go("")
    assert r.returncode == 2 and conf.read_text(encoding="utf-8") == OLD_SVC, "пустой файл снёс записи"
    r = go(_svc_file(), FAKE_UID="1000")
    assert r.returncode == 1 and "нужен root" in r.stderr
    assert conf.read_text(encoding="utf-8") == OLD_SVC


def test_embedded_services_helper_parses(script, tmp_path):
    f = tmp_path / "svc.sh"
    f.write_text(_heredoc(script, "LAN_SERVICES", "SVCEOF"), encoding="utf-8")
    r = subprocess.run([SH, "-n", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# Строки вне шаблона: что пытался бы протащить чужой ВПС, и чем ломается
# шаблон на краях (длина, кавычка, пробел по краю, лишнее поле).
_FOREIGN_LINES = [
    "server=/awg.internal/203.0.113.5",
    "address=/awg.internal/203.0.113.5",
    "conf-file=/tmp/evil.conf",
    "nftset=/awg.internal/inet#awg_home#lan_vpn4",
    "local=/awg.internal/x/",
    " local=/awg.internal/",
    "local=/awg.internal/ ",
    "local=/evil.com/",
    "ptr-record=b._dns-sd._udp.1.0.68.168.192.in-addr.arpa,awg.internal",
    "ptr-record=b._dns-sd._udp.0.68.168.192.in-addr.arpa,evil.com",
    "ptr-record=db._dns-sd._udp.awg.internal,awg.internal",
    "ptr-record=_services._dns-sd._udp.awg.internal,_http._tcp.awg.internal",
    'ptr-record=_smb._tcp.awg.internal,"na"s._smb._tcp.awg.internal"',
    'ptr-record=_smb._tcp.awg.internal,"' + "x" * 64 + '._smb._tcp.awg.internal"',
    'ptr-record=_smb._tcp.awg.internal,"сервер._smb._tcp.awg.internal"',
    'ptr-record=_smb._tcp.awg.internal,"nas,x._smb._tcp.awg.internal"',
    'srv-host="nas._smb._tcp.awg.internal",nas.awg.internal,445,0,0',
    'srv-host="nas._smb._tcp.awg.internal",nas.awg.internal,445445',
    'srv-host="nas._smb._tcp.awg.internal",nas.evil.com,445',
    'txt-record="nas._smb._tcp.awg.internal","evil"',
    "host-record=nas.awg.internal,192.168.1.10,fe80::1",
    "host-record=nas.awg.internal,fe80::1",
    "host-record=nas.awg.internal,192.168.1",
    "host-record=nas.awg.internal.evil.com,192.168.1.10",
    "host-record=nas.awg.internal,192.168.1.10\tserver=/x/1.1.1.1",
]
# Края, которые обязаны пройти: самые длинные допустимые поля.
_EDGE_OK_LINES = [
    "# любой комментарий, даже server=/x/1.1.1.1",
    'ptr-record=_smb._tcp.awg.internal,"' + "x" * 63 + '._smb._tcp.awg.internal"',
    'srv-host="A b_c-d._smb._tcp.awg.internal",' + "h" * 63 + ".awg.internal,65535",
    "host-record=NAS-1.awg.internal,999.999.999.999",
]


@pytest.mark.parametrize("locale", ["C", "C.UTF-8", "en_US.UTF-8"])
def test_shell_whitelist_and_python_line_res_agree(svc_env, locale):
    """Белый список живёт в двух местах: LINE_RES в Python (агент) и grep в
    помощнике на шлюзе. Разойдись они — либо агент соберёт файл, который
    помощник отвергнет навсегда (записи соседей не лягут), либо помощник
    пропустит строку, которую Python считает запрещённой. Гоняем каждую строку
    собранного Python-ом файла и каждую подмешанную запрещённую через оба и
    требуем одного вердикта. Локаль — как у юнита: диапазоны [A-Za-z] под
    UTF-8 не должны пускать кириллицу."""
    from awgbot.domain import gwservices as gs
    go, conf, log = svc_env
    rendered = [ln for ln in _svc_file().splitlines() if ln]
    disagree = []
    for line in rendered + _EDGE_OK_LINES + _FOREIGN_LINES:
        py_ok = any(p.fullmatch(line) for p in gs.LINE_RES)
        r = go("# проверка\n" + line + "\n", LC_ALL=locale)
        assert r.returncode in (0, 2), (line, r.returncode, r.stderr)
        if py_ok != (r.returncode == 0):
            disagree.append((line, "python" if py_ok else "sh"))
    assert disagree == [], f"вердикты разошлись (кто пропустил): {disagree}"
    assert all(any(p.fullmatch(ln) for p in gs.LINE_RES) for ln in rendered + _EDGE_OK_LINES)
    assert not any(any(p.fullmatch(ln) for p in gs.LINE_RES) for ln in _FOREIGN_LINES), \
        "запрещённая строка проходит Python-шаблоны"
    # и файл целиком: собранный — проходит, с подмешанной строкой — нет
    assert go(_svc_file()).returncode == 0 and gs.lines_ok(_svc_file())
    mixed = _svc_file() + _FOREIGN_LINES[0] + "\n"
    assert go(mixed).returncode == 2 and not gs.lines_ok(mixed)


# ── копии для отката — вне conf-dir ──────────────────────────────────────────

def test_the_services_rollback_copy_is_outside_the_conf_dir(svc_env, tmp_path):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}
    snap = _snapshot_restart(env, log)
    r = go(_svc_file(), AWG_DNSMASQ_D=str(conf.parent), AWG_LAN_DUMP=str(tmp_path / "dump"))
    assert r.returncode == 0, r.stderr
    (confdir, rb), = _snapshots(snap)
    assert confdir == {"awg-gw-peer-services.conf"}, f"при рестарте в conf-dir: {confdir}"
    assert rb == {"peer-services.conf.prev"}, f"копии для отката нет в rollback/: {rb}"
    assert not (tmp_path / "dump" / "rollback" / "peer-services.conf.prev").exists()


def test_a_failed_services_restart_restores_the_file_from_rollback(svc_env, tmp_path):
    go, conf, log = svc_env
    conf.write_text(OLD_SVC, encoding="utf-8")
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}
    snap = _snapshot_restart(env, log, fails=1)
    r = go(_svc_file(), AWG_DNSMASQ_D=str(conf.parent), AWG_LAN_DUMP=str(tmp_path / "dump"))
    assert r.returncode == 1 and "откатываю" in r.stderr, r.stderr
    assert conf.read_text(encoding="utf-8") == OLD_SVC, "прежние записи не вернулись из rollback/"
    shots = _snapshots(snap)
    assert len(shots) == 2 and all(_no_copies(c) for c, _ in shots), shots
    assert not (tmp_path / "dump" / "rollback" / "peer-services.conf.prev").exists()


def test_a_non_root_services_run_creates_nothing_and_takes_no_lock(svc_env, tmp_path):
    """Проверка root — до mkdir и flock: запуск не от root не заводит каталог
    состояния с чужим владельцем и не держит блокировку списков."""
    go, conf, log = svc_env
    r = go(_svc_file(), FAKE_UID="1000")
    assert r.returncode == 1 and "нужен root" in r.stderr, r.stderr
    assert not (tmp_path / "dump").exists(), "не-root успел создать каталог состояния"
