"""
routing-gw-setup.sh, раздел 5: сервисы соседних сетей, avahi-utils, показ
плана, режим локальной сети и фильтры фидов.
"""
from __future__ import annotations

import re
import subprocess

import pytest
from tests.conftest import SH, fake_cmd
from tests.unit import _gw_setup_common as _c
from tests.unit._gw_setup_common import _helpers, _sh

script = _c.script


# ── сервисы соседних сетей ───────────

def _svc_host(tmp_path):
    """Шлюз для кусков раздела 5: каталоги во временной папке, systemctl и
    apt-get записывают вызовы. PATH — только подставной каталог (с sh и rm),
    чтобы настоящий avahi-browse хоста не решал за тест."""
    import os
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "log"; log.write_text("", encoding="utf-8")
    for tool in ("sh", "rm", "cat", "printf"):
        real = next((p for p in (f"/bin/{tool}", f"/usr/bin/{tool}") if os.path.exists(p)), None)
        if real:
            (bin_dir / tool).symlink_to(real)

    def fake(name, body):
        fake_cmd(bin_dir / name, body)
    fake("systemctl", f'echo "systemctl $*" >> {log}\n'
                      'case "$*" in *avahi-daemon*) exit "${AVAHI_RC:-0}" ;; esac\nexit 0\n')
    fake("apt-get", f'echo "apt-get $*" >> {log}\n'
                    'case "$1" in update) exit "${APT_UPDATE_RC:-0}" ;; esac\nexit "${APT_RC:-0}"\n')
    fake("nft", "exit 1\n")
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    return bin_dir, log, fake, dns_d


def _peer_block(script: str) -> str:
    sec = script.split("    write_lan_scripts\n", 1)[1]
    return sec.split('    if [ "$_dn_changed" = "1" ]', 1)[0]


def _run_peer_block(script, tmp_path, *, peers: str, avahi_rc="0", browse=False, apt_rc="0",
                    apt_update_rc="0"):
    bin_dir, log, fake, dns_d = _svc_host(tmp_path)
    if browse:
        fake("avahi-browse", "exit 0\n")
    conf = dns_d / "awg-gw-peer-services.conf"
    conf.write_text("local=/awg.internal/\n", encoding="utf-8")
    prog = (_helpers(script) + f'\nMODE=apply\nPEER_SVC_CONF="{conf}"\nPEER_LAN_NETS="{peers}"\n'
            + "_dn_changed=0\n" + _peer_block(script) + '\necho "dn_changed=$_dn_changed"\n')
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": str(bin_dir), "AVAHI_RC": avahi_rc, "APT_RC": apt_rc,
                            "APT_UPDATE_RC": apt_update_rc})
    return r, log.read_text(), conf


def test_without_peer_subnets_the_services_file_is_removed_and_dnsmasq_restarted(script, tmp_path):
    """Доступ между подсетями выключили и перевыпустили конфигурацию: записи
    соседей снимаются обвязкой, а не висят в Finder до следующего канала."""
    r, log, conf = _run_peer_block(script, tmp_path, peers="")
    assert r.returncode == 0, r.stderr
    assert not conf.exists(), "записи соседей пережили выключение доступа между подсетями"
    assert "dn_changed=1" in r.stdout, "файл снят, а dnsmasq не перезапускается"
    assert "apt-get" not in log, "без соседей avahi-utils не нужен"


def test_with_peer_subnets_the_services_file_stays_and_the_block_installs_nothing(script, tmp_path):
    """Подсети соседей есть — записи соседей не трогаются и dnsmasq не
    перезапускается; avahi-utils этот блок больше не ставит (он — ниже, по
    своим подсетям)."""
    r, log, conf = _run_peer_block(script, tmp_path, peers="192.168.1.0/24")
    assert r.returncode == 0, r.stderr
    assert conf.exists() and "dn_changed=0" in r.stdout
    assert "apt-get" not in log, log


def _avahi_tail(script: str) -> str:
    """Хвост раздела 5: ветка режима (VPN-транзит включён/выключен) и обзор
    своей сети после неё — до записи статуса."""
    start = script.index('\nif [ "$LAN_MODE" = "1" ]; then\n') + 1
    return script[start:script.index('\nwrite_status "up"\n', start)]


def _run_avahi(script, tmp_path, *, lan: str, lan_mode="0", peers="", avahi_rc="0", browse=False,
               apt_rc="0", apt_update_rc="0"):
    """Ветка режима с подставными lan_apply/lan_remove и блок avahi-utils;
    последней строкой — «tail=ok»: раздел дошёл до конца."""
    bin_dir, log, fake, dns_d = _svc_host(tmp_path)
    if browse:
        fake("avahi-browse", "exit 0\n")
    prog = (_helpers(script)
            + f'\nMODE=apply\nLAN_MODE="{lan_mode}"\nLAN_SUBNETS="{lan}"\nPEER_LAN_NETS="{peers}"\n'
            + f'LAN_FILE="{tmp_path}/none.nft"\nDNSMASQ_D="{dns_d}"\n'
            + 'lan_apply() { echo "lan_apply"; return 0; }\nlan_remove() { echo "lan_remove"; }\n'
            + _avahi_tail(script) + '\necho "tail=ok"\n')
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": str(bin_dir), "AVAHI_RC": avahi_rc, "APT_RC": apt_rc,
                            "APT_UPDATE_RC": apt_update_rc})
    return r, [ln for ln in log.read_text().splitlines() if ln.startswith("apt-get")]


@pytest.mark.parametrize("lan_mode", ["0", "1"])
def test_avahi_utils_is_installed_with_own_subnets_in_either_mode(script, tmp_path, lan_mode):
    """Обзор своей сети — при заданных своих подсетях, с VPN-транзитом и без:
    имена SMB уходят серверу и для устройств админа в туннеле («SMB в
    туннеле»), а не только соседним шлюзам."""
    r, apt = _run_avahi(script, tmp_path, lan="192.168.1.0/24", lan_mode=lan_mode)
    assert r.returncode == 0 and "tail=ok" in r.stdout, r.stdout + r.stderr
    # списки apt на шлюзе бывают старыми (404 на зеркале) — сначала update, потом install
    assert len(apt) == 2 and apt[0].startswith("apt-get update") and apt[1].endswith(" avahi-utils"), apt
    assert "avahi-daemon" not in apt[1], "демон не ставим: он начал бы объявлять сам шлюз"
    assert ("ставлю avahi-utils (обзор SMB-серверов этой подсети: для подсетей других шлюзов и устройств "
            "админа в туннеле)") in r.stdout, r.stdout


def test_without_own_subnets_avahi_utils_is_not_installed_even_with_peers(script, tmp_path):
    """Обзирать нечего — свои подсети не заданы; подсети соседей без своих
    сами по себе обзора не требуют."""
    r, apt = _run_avahi(script, tmp_path, lan="", lan_mode="1", peers="192.168.68.0/24")
    assert r.returncode == 0 and "tail=ok" in r.stdout, r.stderr
    assert apt == [], apt


@pytest.mark.parametrize("avahi_rc,browse", [("3", False), ("0", True)])
def test_avahi_utils_is_not_installed_without_the_daemon_or_when_present(script, tmp_path, avahi_rc, browse):
    """Демона нет — ставить утилиту незачем (в мониторе ⚪); утилита уже есть —
    apt не трогаем: OMV держит свой apt, и лишний вызов ждёт его блокировку."""
    r, apt = _run_avahi(script, tmp_path, lan="192.168.1.0/24", avahi_rc=avahi_rc, browse=browse)
    assert r.returncode == 0, r.stderr
    assert apt == [], apt


def test_a_failed_apt_update_still_tries_the_install(script, tmp_path):
    """update упал (зеркало недоступно, сети нет) — install всё равно
    пробуется: пакет может найтись и по старым спискам, а раздел не падает."""
    r, apt = _run_avahi(script, tmp_path, lan="192.168.1.0/24", apt_update_rc="100")
    assert r.returncode == 0 and "tail=ok" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert len(apt) == 2 and apt[1].endswith(" avahi-utils"), f"после отказа update install не пробовался: {apt}"
    assert "avahi-utils не установился" not in r.stdout, "отказ update выдан за отказ установки"


def test_a_failed_avahi_utils_install_does_not_fail_the_section(script, tmp_path):
    """apt упал — остальное работает: раздел 5 не роняет юнит в цикл рестартов."""
    r, apt = _run_avahi(script, tmp_path, lan="192.168.1.0/24", apt_rc="100")
    assert r.returncode == 0 and "tail=ok" in r.stdout, (r.returncode, r.stdout, r.stderr)
    assert ("avahi-utils не установился — SMB-серверы этой подсети по именам не видны, "
            "остальное работает") in r.stdout, r.stdout


def test_the_daemon_itself_is_never_installed(script):
    installs = [ln for ln in script.splitlines() if "apt-get install" in ln]
    assert installs and not any("avahi-daemon" in ln for ln in installs), installs


def test_lan_remove_takes_the_services_file_and_helper_with_it(script, tmp_path):
    """Выключение режима и --rollback снимают всё своё: файл записей соседей
    остался бы в conf-dir, и dnsmasq продолжил бы отдавать соседские серверы."""
    bin_dir, log, fake, dns_d = _svc_host(tmp_path)
    for f in ("awg-gw-base.conf", "awg-gw-peer-services.conf"):
        (dns_d / f).write_text("x\n", encoding="utf-8")
    sbin = tmp_path / "sbin"; sbin.mkdir()
    helper = sbin / "awg-lan-services.sh"; helper.write_text("#!/bin/sh\n", encoding="utf-8")
    fn = "\n".join(re.search(rf"^{name}\(\) \{{.*?^\}}$", script, re.S | re.M).group(0)
                   for name in ("lan_remove", "_lan_remove_locked", "lan_table_old_drop"))
    t = tmp_path
    prog = (_helpers(script) + f'\nMODE=apply\nDNSMASQ_D="{dns_d}"\nLAN_DUMP="{t}/dump"\n'
            f'DNSMASQ_OVR="{t}/none.conf"\nDNSMASQ_MARK="{t}/none.mark"\nLAN_TABLE="inet awg_lan"\n'
            f'LAN_FILE="{t}/lan.nft"\nLAN_TABLE_OLD="inet awg_home"\nLAN_FILE_OLD="{t}/home.nft"\n'
            f'LAN_SYSCTL="{t}/sysctl.conf"\nLAN_LISTS="{sbin}/awg-lan-lists.sh"\n'
            f'LAN_DOMAIN="{sbin}/awg-lan-domain.sh"\nLAN_SERVICES="{helper}"\n'
            f'PEER_SVC_CONF="{dns_d}/awg-gw-peer-services.conf"\n' + fn + "\nlan_remove\n")
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, env={"PATH": str(bin_dir)})
    assert r.returncode == 0, r.stderr
    assert not (dns_d / "awg-gw-peer-services.conf").exists(), "файл записей соседей пережил снятие"
    assert not helper.exists(), "помощник сервисов пережил снятие"
    assert "systemctl restart dnsmasq" in (tmp_path / "log").read_text()


def _plan_line(script: str, **env) -> str:
    """Режим показа: блок `if [ "$MODE" = "plan" ]` раздела плана, прогнанный
    под sh; строка про SMB подсетей других шлюзов."""
    start = script.index('\nif [ "$MODE" = "plan" ]; then\n    say ""\n    say "(режим показа') + 1
    block = script[start:script.index("\n    exit 0\nfi\n", start) + len("\n    exit 0\nfi\n")]
    r = subprocess.run([SH, "-c", _helpers(script) + "\nMODE=plan\n" + block],
                       capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env})
    assert r.returncode == 0, r.stderr
    return next(ln.strip() for ln in r.stdout.splitlines() if "SMB подсетей других шлюзов" in ln)


@pytest.mark.parametrize("env,state", [
    ({"PEER_LAN_NETS": "192.168.1.0/24", "LAN_MODE": "1"}, "включены"),
    ({"PEER_LAN_NETS": "", "LAN_MODE": "1"}, "нет"),
    ({"PEER_LAN_NETS": "192.168.1.0/24", "LAN_MODE": "0"}, "нет"),
])
def test_the_plan_says_whether_peer_smb_will_be_served(script, env, state):
    """Показ плана до применения: записи SMB соседей возможны при соседях и
    режиме локальной сети — иначе «нет», чтобы человек не искал в Finder то,
    что эта конфигурация не даст."""
    assert _plan_line(script, **env) == f"SMB подсетей других шлюзов (в Finder: «Сеть» → awg.internal): {state}"


def test_previous_guard_table_is_loaded_before_the_link_comes_up(script):
    """Между awg-quick up и nft -f свежей таблицы клиенты сервера AWG доходили
    бы до самого устройства; прежняя таблица держит окно закрытым."""
    step1 = script.split('step "1. Конфиг и подъём $LINK_IF"', 1)[1].split('step "1a.', 1)[0]
    assert step1.index('nft -f $GUARD_FILE') < step1.index('run "$AWG_QUICK up $LINK_IF"')
    assert 'if [ -f "$GUARD_FILE" ]' in step1


def test_wan_interface_is_the_token_after_dev(script, tmp_path):
    """«default via X dev Y», «default dev Y», с nhid — колонка плавает, имя нет."""
    fn = script.split("detect_wan() {", 1)[1].split("\n}", 1)[0]
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    for line in ("default via 192.168.1.1 dev br0 proto dhcp metric 100",
                 "default dev ppp0 scope link",
                 "default nhid 12 via 10.0.0.1 dev bond0 proto static"):
        fake_cmd(bin_dir / "ip", f'#!/bin/sh\necho "{line}"\n')
        r = subprocess.run([SH, "-c", "detect_wan() {" + fn + "\n}\ndetect_wan"], capture_output=True,
                           text=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
        assert r.stdout.strip() == line.split(" dev ")[1].split()[0], (line, r.stdout)


def test_the_uplink_unit_override_restarts_dnsmasq_and_is_rewritten_when_it_differs(script):
    """Оверрайд awg-quick@аплинк: перезапуск до победы плюс перечитывание dnsmasq
    после подъёма (вместо After=/Wants= в drop-in dnsmasq, дававших цикл);
    файл сверяется по содержимому, а не «уже есть» — прежние хосты получают новый."""
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split("write_status() {", 1)[0]
    assert "ExecStartPost=-/bin/systemctl --no-block try-restart dnsmasq.service" in step0
    assert '"$(cat "$_ovr/awg-gw.conf" 2>/dev/null)" != "$_ovr_txt"' in step0
    assert 'if [ ! -f "$_ovr/awg-gw.conf" ]' not in step0, "оверрайд по-прежнему пишется только раз"


def test_lan_mode_takes_the_lists_lock_and_marks_dnsmasq_only_when_it_installed_it(script):
    """Выключение режима посреди скачивания фидов возвращало фид и nftset в
    снятую таблицу — режим включается и снимается под lists.lock; метка
    «dnsmasq ставили мы» — только если его не было до apt (ради dig ставили и
    dnsutils, и выключение режима гасило чужой dnsmasq)."""
    apply_wrap = script.split("lan_apply() {", 1)[1].split("\n}", 1)[0]
    assert "lists.lock" in apply_wrap and "_lan_apply_locked" in apply_wrap
    body = script.split("_lan_apply_locked() {", 1)[1].split('# ── sysctl: rp_filter loose на LAN', 1)[0]
    assert '_had_dnsmasq=0; command -v dnsmasq >/dev/null 2>&1 && _had_dnsmasq=1' in body
    assert '[ "$_had_dnsmasq" = "1" ] || run "touch $DNSMASQ_MARK"' in body


def test_endpoint_names_resolve_through_the_router_not_the_tunnel(script):
    """DNS устройства = оно само (рецепт роутера), апстрим dnsmasq — через
    аплинк: имя Endpoint резолвилось бы только через ещё не поднятый туннель —
    взаимная блокировка на загрузке. Имена — через DNS роутера."""
    body = script.split("_lan_apply_locked() {", 1)[1].split("filter-AAAA", 1)[0]
    assert '_ep_dns="$VIA4"' in body, "шлюз маршрута по умолчанию — тот же разбор, что у адреса роутера"
    assert "_ep_names=" in body and "Endpoint" in body
    assert "printf 'server=/%s/%s\\n' \"$_n\" \"$_ep_dns\"" in body


# прежний разбор адреса DNS роутера — до VIA4 (строка из _lan_apply_locked)
_EP_DNS_OLD = ("ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) "
               "if($i==\"via\"){print $(i+1); exit}}'")


@pytest.mark.parametrize("route", [
    "default via 192.168.1.1 dev eth0 proto dhcp src 192.168.1.10 metric 100",
    "default via 10.0.0.254 dev br0\ndefault via 10.0.0.1 dev wlan0 metric 600",
    "default proto static metric 100\n\tnexthop via 172.16.0.1 dev eth0 weight 1",
    "default dev ppp0 scope link",
])
def test_endpoint_dns_lines_are_the_same_with_via4(script, tmp_path, route):
    """_ep_dns берёт VIA4 (detect_via) — строки server=/<имя>/<адрес> для
    IPv4-шлюза маршрута те же, что давал прежний разбор `via`, и не зависят
    от того, внутри ли он своих подсетей; маршрут без via — строк нет."""
    fn = script[script.index("detect_via() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    loop = ("for _n in vpn.example.org link.example.org; do "
            "[ -n \"$_ep_dns\" ] && printf 'server=/%s/%s\\n' \"$_n\" \"$_ep_dns\"; done; :")
    fake_cmd(tmp_path / "ip", "#!/bin/sh\nprintf '%b\\n' '" + route + "'\n")
    path = f"{tmp_path}:/usr/bin:/bin"
    new = _sh(fn + 'VIA4="$(detect_via)"\n_ep_dns="$VIA4"\n' + loop, path=path)
    old = _sh(f'_ep_dns="$({_EP_DNS_OLD})"\n' + loop, path=path)
    assert new.returncode == 0 and old.returncode == 0, (new.stderr, old.stderr)
    assert new.stdout == old.stdout, (new.stdout, old.stdout)
    assert ("server=/vpn.example.org/" in new.stdout) == ("via" in route)


def test_subnet_feeds_are_only_added_when_one_source_failed(script):
    """Сбой одного фида подсетей давал rc=1, но flush set + add из скачавшегося —
    подсети сервиса пропадали до следующего похода. Неполное скачивание — только add."""
    lists = script.split("cat > \"$LAN_LISTS.new\" <<'LISTSEOF'", 1)[1].split("LISTSEOF", 1)[0]
    assert "nets_rc=1" in lists
    assert lists.index('if [ "${nets_rc:-0}" = "0" ]; then') < lists.index("flush set %s lan_vpn_nets4")
    branch = lists.split("else\n        # один из фидов не скачался", 1)[1].split("\n    fi\n", 1)[0]
    assert "flush set" not in branch and "add element" in branch


def test_the_link_unit_has_a_start_timeout_and_a_calmer_restart(script):
    unit = script.split("cat > \"$UNIT\" <<UNITEOF", 1)[1].split("UNITEOF", 1)[0]
    assert "TimeoutStartSec=15min" in unit and "RestartSec=60" in unit
    assert '_unit_old="$(cat "$UNIT" 2>/dev/null || true)"' in script
    assert 'if [ "$_unit_old" = "$(cat "$UNIT")" ]; then' in script, "daemon-reload на каждом прогоне"
    assert 'systemctl is-enabled -q awg-link-gw.service 2>/dev/null || run "systemctl enable awg-link-gw.service"' in script


def test_rollback_clears_accepts_on_every_interface(script):
    rb = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "sed 's/@.*//' | grep -v '^lo$'" in rb and "grep -E '^(awg|end|eth|br|wl)'" not in rb


def test_bundle_values_are_taken_by_form(script):
    """Значения из бандла и env идут в nft, юнит и sh -c: подсеть клиентов,
    адреса админа и имя линка проходят фильтр формы, LAN_MODE — только 0/1;
    поиска сервера прежней схемы больше нет."""
    assert "detect_container" not in script
    assert "CLIENT_SUBNET=\"$(printf '%s' \"${CLIENT_SUBNET:-10.8.1.0/24}\" | tr -cd '0-9./')\"" in script
    assert "ADMIN_IPS=\"$(printf '%s' \"$ADMIN_IPS\" | tr -cd '0-9./ ')\"" in script
    assert "LINK_IF=\"$(printf '%s' \"${LINK_IF:-awglink}\" | tr -cd 'A-Za-z0-9_.-' | cut -c1-15)\"" in script
    assert '[ "${LAN_MODE:-0}" = "1" ] && LAN_MODE=1 || LAN_MODE=0' in script
    out = _sh('LAN_MODE=yes; [ "${LAN_MODE:-0}" = "1" ] && LAN_MODE=1 || LAN_MODE=0; echo "$LAN_MODE"')
    assert out.stdout.strip() == "0"


def test_the_feed_filter_takes_domains_by_segment_and_subnets_not_shorter_than_8(script):
    """`nftset=/#/…` у dnsmasq значит «все домены», «0.0.0.0/1» — полсети: битый
    или подменённый фид не должен увести в туннель всё."""
    lists = script.split("<<'LISTSEOF'", 1)[1].split("LISTSEOF", 1)[0]
    m = re.search(r"grep -Ei '(\^\(#\.\*\|nftset=.*?)' \"\$TMP\"", lists)
    assert m, "фильтр фида не найден"
    rx = re.compile(m.group(1).replace("\\\\", "\\"), re.I)
    for good in ("nftset=/gosuslugi.ru/inet#awg_lan#lan_vpn4", "nftset=/a.b.xn--p1ai/c.ru/inet#awg_lan#lan_vpn4",
                 "# comment"):
        assert rx.match(good), good
    for bad in ("nftset=/#/inet#awg_lan#lan_vpn4", "nftset=/./inet#awg_lan#lan_vpn4",
                "nftset=/ru/inet#awg_lan#lan_vpn4", "nftset=/-a.ru/inet#awg_lan#lan_vpn4",
                "server=/x.ru/1.1.1.1"):
        assert not rx.match(bad), bad
    m = re.search(r"grep -E '(\^\[0-9\]\+.*?)' \"\$NETS\"", lists)
    assert m, "фильтр подсетей не найден"
    rx = re.compile(m.group(1).replace("\\\\", "\\"))
    assert rx.match("10.0.0.0/8") and rx.match("203.0.113.0/24") and rx.match("1.2.3.4/32")
    assert not rx.match("0.0.0.0/1") and not rx.match("128.0.0.0/7") and not rx.match("1.2.3.4/33")
