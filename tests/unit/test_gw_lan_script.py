"""
Локальная сеть без VPN на шлюзе (концепт «локальная сеть», функция A) — раздел 5
routing-gw-setup.sh и два вшитых в него скрипта: списки и персональные домены.

Гоняем настоящий код скриптов с подменёнными ip/curl/nft/systemctl/dig: то,
что уедет на малину, а не грепаем исходник там, где можно исполнить.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _heredoc(script: str, var: str, tag: str) -> str:
    """Тело `cat > "$VAR" <<'TAG' … TAG`."""
    return script.split(f'cat > "${var}" <<\'{tag}\'\n', 1)[1].split(f"\n{tag}\n", 1)[0] + "\n"


def _fake(bin_dir: Path, name: str, body: str) -> None:
    f = bin_dir / name
    f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    f.chmod(0o755)


def _sh(prog: str, *, env: dict, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", prog], capture_output=True, text=True, env=env, cwd=cwd)


# ── интерфейс и адрес малины — из локальной подсети ──────────────────────────

def test_lan_interface_is_found_by_home_subnet(script, tmp_path):
    """По подсети из бота малина находит свой LAN-интерфейс и адрес: их не
    спрашивают и не вшивают — они выводятся из того, что уже задано."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    _fake(bin_dir, "ip", "cat <<'EOF'\n"
          "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
          "2: end0    inet 192.168.68.222/24 brd 192.168.68.255 scope global end0\\       valid_lft forever\n"
          "5: awg0    inet 10.9.1.15/32 scope global awg0\\       valid_lft forever\n"
          "EOF\n")
    fn = re.search(r"^lan_iface_for\(\) \{.*?^\}$", script, re.S | re.M).group(0)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin"}
    assert _sh(fn + '\nlan_iface_for 192.168.68.0/24', env=env).stdout.strip() == "end0 192.168.68.222"
    assert _sh(fn + '\nlan_iface_for 10.9.1.0/24', env=env).stdout.strip() == "", "туннели и docker — не локальная сеть"
    assert _sh(fn + '\nlan_iface_for 192.168.1.0/24', env=env).stdout.strip() == "", "чужая подсеть — пусто"
    assert _sh(fn + '\nlan_iface_for 192.168.0.0/16', env=env).stdout.strip() == "end0 192.168.68.222", "вложенность"


# ── персональные списки: awg-lan-domain.sh ───────────────────────────────────

@pytest.fixture()
def domain_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    log = tmp_path / "log"
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "sleep", "")
    _fake(bin_dir, "id", "echo 0\n")
    _fake(bin_dir, "dig", "case \"$*\" in *example.com*) echo 93.184.216.34; echo 93.184.216.35 ;; esac\n")
    _fake(bin_dir, "nft", f'echo "nft $*" >> {log}\n')
    conf = tmp_path / "awg0.conf"; conf.write_text("[Peer]\nEndpoint = vps.example.net:51820\n", encoding="utf-8")
    tool = tmp_path / "awg-lan-domain.sh"
    tool.write_text(_heredoc(script, "LAN_DOMAIN", "DOMEOF"), encoding="utf-8"); tool.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_UPLINK_CONF": str(conf)}
    return tool, dns_d, log, env


def _run(tool, env, *args):
    return subprocess.run(["sh", str(tool), *args], capture_output=True, text=True, env=env)


def test_domain_tool_adds_resolves_and_fills_the_set(domain_env):
    tool, dns_d, log, env = domain_env
    r = _run(tool, env, "add", "https://www.Example.com/path", "not a domain")
    assert r.returncode == 0, r.stderr
    assert "example.com: добавлен" in r.stdout and "не похоже на домен" in r.stdout
    assert (dns_d / "awg-gw-vpn-user.conf").read_text() == "nftset=/example.com/inet#awg_home#lan_vpn4\n"
    text = log.read_text()
    assert "systemctl restart dnsmasq" in text, "именно restart: SIGHUP конфиги не перечитывает"
    assert "nft add element inet awg_home lan_vpn4 { 93.184.216.34 }" in text
    assert "2 адрес(а) в наборе lan_vpn4" in r.stdout
    assert "example.com: уже в списке" in _run(tool, env, "add", "example.com").stdout


def test_domain_tool_moves_between_lists_and_deletes(domain_env):
    tool, dns_d, log, env = domain_env
    _run(tool, env, "add", "shop.ru")
    r = _run(tool, env, "ru", "shop.ru")                      # напрямую → уходит из туннельного
    assert "shop.ru: добавлен" in r.stdout
    assert "shop.ru" not in (dns_d / "awg-gw-vpn-user.conf").read_text()
    assert (dns_d / "awg-gw-ru-user.conf").read_text().strip() == "nftset=/shop.ru/inet#awg_home#lan_ru4"
    assert _run(tool, env, "list").stdout.strip() == "ru shop.ru"
    assert "shop.ru: убран" in _run(tool, env, "del", "shop.ru").stdout
    assert _run(tool, env, "list").stdout.strip() == ""
    assert "в списках нет" in _run(tool, env, "del", "shop.ru").stdout


def test_domain_tool_refuses_the_vps_host(domain_env):
    """Увести хост ВПС в туннель — запереть себя: Endpoint аплинка под запретом."""
    tool, dns_d, log, env = domain_env
    r = _run(tool, env, "add", "VPS.example.net")
    assert "это хост сервера" in r.stdout
    assert "vps.example.net" not in (dns_d / "awg-gw-vpn-user.conf").read_text()


# ── списки: awg-lan-lists.sh ─────────────────────────────────────────────────

@pytest.fixture()
def lists_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    dump = tmp_path / "dump"
    log = tmp_path / "log"
    feeds = tmp_path / "feeds"; feeds.mkdir()
    lines = ["ipset=/youtube.com/googlevideo.com/vpn_domains", "ipset=/shop.ru/vpn_domains",
             "ipset=/x.com/vpn_domains", "<html>заглушка провайдера</html>", "# comment"]
    lines += [f"ipset=/site{i}.org/vpn_domains" for i in range(12)]
    (feeds / "domains").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (feeds / "telegram").write_text("91.108.4.0/22\n149.154.160.0/20\n", encoding="utf-8")
    (feeds / "goog").write_text('{"prefixes":[{"ipv4Prefix": "8.8.8.0/24"},{"ipv6Prefix":"2001::/32"}]}', encoding="utf-8")
    # curl: URL → файл фикстуры; неизвестный — отказ
    _fake(bin_dir, "curl",
          'out=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift; done\n'
          f'case "$url" in *inside-dnsmasq-ipset*) f={feeds}/domains ;; *Subnets/IPv4/telegram*) f={feeds}/telegram ;;'
          f' *goog.json*) f={feeds}/goog ;; *) exit 22 ;; esac\n'
          'if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi\n')
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "nft", f'if [ "$1" = "-f" ]; then echo "nft -f: $(cat)" >> {log}; else echo "nft $*" >> {log}; echo "elements = {{ 1.2.3.0/24 }}"; fi\n')
    _fake(bin_dir, "dnsmasq", f'echo "dnsmasq $*" >> {log}; exit "${{DNSMASQ_TEST_RC:-0}}"\n')
    tool = tmp_path / "awg-lan-lists.sh"
    tool.write_text(_heredoc(script, "LAN_LISTS", "LISTSEOF"), encoding="utf-8"); tool.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_LAN_DUMP": str(dump),
           "AWG_LAN_SUBNET_SERVICES": "telegram"}
    return tool, dns_d, dump, log, env


def test_lists_convert_feed_subtract_exceptions_and_load_subnets(lists_env):
    tool, dns_d, dump, log, env = lists_env
    (dns_d / "awg-gw-ru-user.conf").write_text("nftset=/shop.ru/inet#awg_home#lan_ru4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    feed = (dns_d / "awg-gw-vpn-feed.conf").read_text()
    assert "nftset=/youtube.com/googlevideo.com/inet#awg_home#lan_vpn4" in feed, "ipset= → nftset= в наш набор"
    assert "shop.ru" not in feed, "исключение вычтено: dnsmasq применяет одну директиву на домен"
    assert "nftset=/x.com/inet#awg_home#lan_vpn4" in feed
    assert "<html>" not in feed, "мусор HTTP 200 в conf-dir не попадает"
    text = log.read_text()
    assert "dnsmasq --test" in text and text.index("dnsmasq --test") < text.index("systemctl restart dnsmasq"), \
        "конфиг проверен до рестарта"
    assert "systemctl restart dnsmasq" in text
    assert "flush set inet awg_home lan_vpn_nets4" in text and "8.8.8.0/24" in text and "91.108.4.0/22" in text
    assert text.index("flush set") < text.index("add element"), "одной транзакцией: flush, затем add"
    assert (dump / "lan_vpn4.nft").exists() and (dump / "lan_vpn_nets4.nft").exists(), "слепки для старта"
    status = (dump / "lists.status").read_text()
    assert "domains=14\n" in status and "nets=3\n" in status and "rc=0" in status


def test_lists_roll_back_a_feed_that_dnsmasq_rejects(lists_env):
    """dnsmasq --test отверг новый фид — прежний файл на месте, рестарта нет,
    квартира не остаётся без DNS."""
    tool, dns_d, dump, log, env = lists_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env={**env, "DNSMASQ_TEST_RC": "1"})
    assert r.returncode == 1 and "откатываю" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_home#lan_vpn4\n"
    assert "systemctl restart dnsmasq" not in log.read_text()


def test_lists_refuse_a_suspiciously_short_feed(lists_env, tmp_path):
    tool, dns_d, dump, log, env = lists_env
    (tmp_path / "feeds" / "domains").write_text("<html>blocked</html>\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "подозрительно короткий" in r.stderr
    assert not (dns_d / "awg-gw-vpn-feed.conf").exists()


def test_lists_restart_dnsmasq_only_when_the_feed_changed(lists_env):
    """Рестарт роняет кэш всей сети — вхолостую его не делаем."""
    tool, dns_d, dump, log, env = lists_env
    subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    n = log.read_text().count("systemctl restart dnsmasq")
    subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert log.read_text().count("systemctl restart dnsmasq") == n, "фид тот же — рестарта нет"


def test_lists_report_a_failed_feed_but_keep_going(lists_env):
    tool, dns_d, dump, log, env = lists_env
    env = {**env, "AWG_LAN_DOMAINS_URL": "https://nowhere.invalid/x.lst"}
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "фид не скачался" in r.stderr
    assert "flush set" in log.read_text(), "подсети залиты, несмотря на отказ доменов"
    assert "rc=1" in (dump / "lists.status").read_text()


# ── раздел 5 скрипта: структура ──────────────────────────────────────────────

def test_home_table_keeps_its_sets_across_reasserts(script):
    """awg_gw_guard перевыставляется через delete table, а наборы awg_home
    наполняет dnsmasq на лету — их терять нельзя: таблица без delete, цепочки
    flush + add."""
    home = script.split("cat <<HOMEEOF", 1)[1].split("HOMEEOF", 1)[0]
    assert not any("delete table" in ln for ln in home.splitlines() if not ln.lstrip().startswith("#"))
    assert 'set lan_vpn4' in home and "set lan_vpn_nets4" in home and "set lan_ru4" in home
    for ch in ("prerouting", "input", "forward", "postrouting"):
        assert f"flush chain $HOME_TABLE {ch}" in home
    rules = [ln for ln in home.splitlines() if ln.startswith("add rule $HOME_TABLE prerouting")]
    assert rules[0].endswith('iifname != "$LAN_IF" accept'), "из линка и аплинка — мимо маркировки"
    assert "meta pkttype host" in rules[1] and "!= $_net counter" in rules[1], \
        "счётчик заворота — только unicast на MAC малины, до вердиктов: бродкасты фонят всегда"
    assert "@lan_ru4 accept" in rules[2], "исключения ВЫШЕ метки"
    assert "@lan_vpn_nets4 meta mark set $TG_MARK" in rules[3] and "@lan_vpn4 meta mark set $TG_MARK" in rules[4], \
        "та же метка, что у агента — правило и маршрут уже есть"
    assert 'ip saddr $_net oifname "$LAN_IF" masquerade' in home
    assert 'iifname "$LAN_IF" tcp dport 853 drop' in home
    assert 'iifname "$LAN_IF" udp dport 53 counter' in home


def test_resolver_goes_upstream_through_the_uplink(script):
    sec = script.split('step "5. Локальная сеть без VPN"', 1)[1]
    assert "printf 'server=%s@%s\\n' \"$_upstream\" \"$UPLINK_IF\"" in sec, "без @iface запрос ушёл бы линком"
    assert '_upstream="${RESOLVER:-1.1.1.1}"' in sec, "без резолвера ВПС — запасной, но тоже через аплинк"
    assert "bind-dynamic" in sec and "listen-address=127.0.0.1,%s" in sec and "no-hosts" in sec
    assert "Restart=on-failure" in sec and "AmbientCapabilities" not in sec, "ambient-набор ядро чистит при setuid"
    assert "address=/use-application-dns.net/" in sec and "address=/dns.google/" in sec
    assert "filter-AAAA" in sec, "IPv6 у клиентов режется ответами резолвера, не sysctl на малине"
    assert "disable_ipv6" not in sec and "rp_filter = 2" in sec
    assert "DNSMASQ_EXCEPT=lo" in sec, "системный резолвер малины dnsmasq не трогает"
    # конфиг пишется ДО установки пакета: первый старт демона сразу с listen-address
    assert sec.index("install -m 0644 $_tmp $DNSMASQ_D/awg-gw-base.conf") < sec.index("apt-get install -y -q dnsmasq")


def test_lan_section_never_kills_the_script_after_the_unit_is_enabled(script):
    """Юнит уже включён (раздел 4) и перезапускается до победы: exit 1 в разделе 5
    крутил бы его в цикле каждые 10 с с daemon-reload и пересборкой guard.
    Отказ — в LAN_ERROR и в статус, его покажет агент."""
    sec = script.split('step "5. Локальная сеть без VPN"', 1)[1].split('write_status "up"', 1)[0]
    assert "exit 1" not in sec and "if lan_apply; then" in sec
    assert 'LAN_ERROR=%s' in script and "lan_fail" in sec
    assert 'port53_busy' in sec and "Pi-hole" in sec, "проверка :53 до установки, с честным отказом"


def test_lan_mode_off_removes_its_own_and_rollback_too(script):
    sec = script.split('step "5. Локальная сеть без VPN"', 1)[1]
    off = sec.split('if [ "$LAN_MODE" = "1" ]; then', 1)[1].split("\nelse\n", 1)[1].split("\nfi\n", 1)[0]
    assert "lan_remove" in off and "lan_apply" not in off
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "lan_remove" in rollback
    fn = script.split("lan_remove() {", 1)[1].split("\n}", 1)[0]
    assert "for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf" in fn and 'park "$DNSMASQ_D/$_f"' in fn, \
        "личные списки — данные человека: паркуются, не rm"
    assert "nft delete table $HOME_TABLE" in fn
    assert "systemctl disable --now dnsmasq" in fn and "$DNSMASQ_MARK" in fn, "ставили сами — снимаем"
    # dnsmasq читает в conf-dir всё, кроме .dpkg-*: .bak рядом класть нельзя
    assert ".bak" not in fn and 'mv -f $_f $_f.bak' not in script
    park = script.split("park() {", 1)[1].split("\n}", 1)[0]
    assert "$LAN_OLD" in park and "mv -f" in park


def test_manual_layer_is_migrated_not_broken(script):
    fn = script.split("lan_migrate_manual() {", 1)[1].split("\n}", 1)[0]
    for what in ("home-split.service", "awg-lists.timer", "fwmark 0x10 lookup 100", "inet home_split",
                 "/etc/home-split.nft", "/usr/local/bin/awg-add", "ru-force.conf", "user-managed.conf"):
        assert what in fn, what
    assert "inet#home_split#ru4|inet#awg_home#lan_ru4" in fn and "inet#home_split#vpn4|inet#awg_home#lan_vpn4" in fn


def test_unit_and_status_carry_the_lan_variables(script):
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    for line in ("Environment=LAN_MODE=$LAN_MODE", 'Environment="HOME_SUBNETS=$HOME_SUBNETS"', "Environment=RESOLVER=$RESOLVER"):
        assert line in unit, line
    assert "LAN=%s\\nLAN_IF=%s\\nLAN_ADDR=%s" in script
    plan = script.split('if [ "$MODE" = "plan" ]; then\n    say ""', 1)[1].split("exit 0", 1)[0]
    assert "5. локальная сеть без VPN" in plan


def test_embedded_scripts_parse(script, tmp_path):
    for var, tag in (("LAN_LISTS", "LISTSEOF"), ("LAN_DOMAIN", "DOMEOF")):
        f = tmp_path / f"{var}.sh"; f.write_text(_heredoc(script, var, tag), encoding="utf-8")
        r = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_paths_resolve_after_their_prefixes(script):
    """HOME_FILE вычислялся до GW_ETC и раскрывался в /home.nft — таблица ложилась
    в корень ФС. Шапку скрипта исполняем до первой функции и смотрим на значения."""
    head = script.split("\niface_pubkey()", 1)[0].split("set -e\n", 1)[1]
    r = _sh(head + '\necho "$HOME_FILE|$GUARD_FILE|$LAN_OLD"', env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/etc/awg-gw/home.nft|/etc/awg-gw/guard.nft|/var/lib/awg-gw/migrated"


def test_port53_check_ignores_dnsmasq_and_the_resolved_stub(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    fn = script.split("port53_busy() {", 1)[1].split("\n}", 1)[0]
    prog = "port53_busy() {" + fn + "\n}\nLAN_ADDR=192.168.68.222\nport53_busy"
    _fake(bin_dir, "ss", 'cat <<EOF\nudp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=1,fd=1))\n'
                         'udp UNCONN 0 0 192.168.68.222:53 0.0.0.0:* users:(("dnsmasq",pid=2,fd=1))\nEOF\n')
    assert _sh(prog, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}).stdout.strip() == "", "stub resolved и наш dnsmasq — не помеха"
    _fake(bin_dir, "ss", 'echo \'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("pihole-FTL",pid=3,fd=1))\'\n')
    assert "pihole-FTL" in _sh(prog, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}).stdout
