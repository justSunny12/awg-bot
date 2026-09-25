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


# ── списки из канала линка (концепт «канал линка», этап 3) ──────────────────

@pytest.fixture()
def channel_env(lists_env, tmp_path):
    """Те же списки, но фиды привёз канал: каталог с domains.lst и nets.lst, а
    curl записывает каждый вызов — адрес квартиры не должен ходить за фидами."""
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "curl", f'echo "curl $*" >> {log}\nexit 22\n')
    feed = tmp_path / "channel-feed"; feed.mkdir()
    (feed / "domains.lst").write_text(
        "".join(f"ipset=/chan{i}.org/vpn_domains\n" for i in range(12))
        + "ipset=/shop.ru/vpn_domains\n<html>мусор</html>\n", encoding="utf-8")
    (feed / "nets.lst").write_text("91.108.4.0/22\n8.8.8.0/24\nне подсеть\n", encoding="utf-8")
    return tool, dns_d, dump, log, {**env, "AWG_LAN_FROM": str(feed)}, feed


def test_channel_feeds_are_used_without_a_single_download(channel_env):
    """Смысл этапа 3: фиды привёз сервер, и адрес квартиры за ними не ходит ни
    на GitHub, ни в Google. Проверки те же, что для скачанного."""
    tool, dns_d, dump, log, env, _feed = channel_env
    (dns_d / "awg-gw-ru-user.conf").write_text("nftset=/shop.ru/inet#awg_home#lan_ru4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    text = log.read_text()
    assert "curl" not in text, f"при фидах из канала скрипт ходил в сеть: {text}"
    feed = (dns_d / "awg-gw-vpn-feed.conf").read_text()
    assert "nftset=/chan0.org/inet#awg_home#lan_vpn4" in feed
    assert "shop.ru" not in feed and "<html>" not in feed, "исключения и мусор — как у скачанного"
    assert "91.108.4.0/22" in text and "8.8.8.0/24" in text and "не подсеть" not in text
    status = (dump / "lists.status").read_text()
    assert "source=channel\n" in status and "rc=0" in status


def test_a_broken_channel_feed_is_refused_like_a_downloaded_one(channel_env):
    """Сервер прислал заглушку вместо фида (или его подменили). dnsmasq квартиры
    получает её не больше, чем получил бы скачанную: короткий фид отвергнут,
    прежний файл на месте."""
    tool, dns_d, dump, log, env, feed = channel_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    (feed / "domains.lst").write_text("<html>blocked</html>\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "подозрительно короткий" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_home#lan_vpn4\n"
    assert "curl" not in log.read_text(), "отказ фида из канала не повод идти за ним в сеть"


def test_a_channel_feed_that_dnsmasq_rejects_is_rolled_back(channel_env):
    tool, dns_d, dump, log, env, _feed = channel_env
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env={**env, "DNSMASQ_TEST_RC": "1"})
    assert r.returncode == 1 and "откатываю" in r.stderr
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_home#lan_vpn4\n"
    assert "systemctl restart dnsmasq" not in log.read_text()


def test_a_missing_channel_feed_is_named_as_such(channel_env):
    """Каталог пуст — сообщение говорит про канал, а не «фид не скачался»:
    иначе человек пошёл бы чинить сеть малины, которая тут ни при чём."""
    tool, dns_d, dump, log, env, feed = channel_env
    (feed / "domains.lst").unlink()
    (feed / "nets.lst").unlink()
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert "фида нет в" in r.stderr and "привозит канал" in r.stderr
    assert "не скачался" not in r.stderr
    assert "curl" not in log.read_text()


def test_without_the_channel_the_script_downloads_and_says_so(lists_env):
    tool, dns_d, dump, log, env = lists_env
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "source=net\n" in (dump / "lists.status").read_text()


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
    # DNSMASQ_EXCEPT=lo init-скрипт Debian превращает в except-interface=lo, а тот
    # глушит listen-address=127.0.0.1: скрипт эту строку больше не пишет, только
    # убирает свою прежнюю (поведение — в test_old_dnsmasq_except_line_is_removed…)
    assert "DNSMASQ_EXCEPT=lo\\n' >>" not in sec, "строка, глушащая 127.0.0.1, снова дописывается"
    apt = "apt-get install -y -q -o DPkg::Lock::Timeout=120"
    assert apt in sec, "apt без ожидания блокировки dpkg падает, пока OMV держит свой apt"
    apt_line = sec[sec.index(apt):].split("\n", 1)[0]
    assert "--force-confdef" in apt_line and "--force-confold" in apt_line, (
        "без confdef/confold dpkg в юните без терминала застревает на вопросе о конфиге")
    # конфиг пишется ДО установки пакета: первый старт демона сразу с listen-address
    assert sec.index("install -m 0644 $_tmp $DNSMASQ_D/awg-gw-base.conf") < sec.index(apt)
    # оверрайд юнита тоже до apt: первый старт после установки уже после аплинка
    assert sec.index("install -m 0644 $_ovr_want $DNSMASQ_OVR") < sec.index(apt)


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
    assert "for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf" in fn \
        and 'mv -f $DNSMASQ_D/$_f $LAN_DUMP/restore/$_f' in fn, \
        "личные списки — данные человека: в restore/, откуда их вернёт следующее включение, не rm"
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


# ── списки: блокировка, проверка конфигурации, отказ рестарта ───────────────

def test_lists_wait_for_the_lock_and_say_busy_with_code_75(lists_env):
    """Ручное «Обновить» и фиды из канала совпали по времени. Раньше второй
    запуск молча выходил с нулём — агент считал фиды из канала применёнными,
    хотя они не легли. Теперь ждём блокировку до двух минут, а не дождались —
    код 75 («занято»), и ничего не трогаем."""
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "flock", f'echo "flock $*" >> {log}\nexit 1\n')       # блокировку держит другой
    (dns_d / "awg-gw-vpn-feed.conf").write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 75, f"занятая блокировка выдана за {r.returncode}: {r.stderr}"
    assert "обновление уже идёт" in r.stderr
    text = log.read_text()
    assert "flock -w 120 9" in text, f"ждать блокировку надо до двух минут, а не сдаваться сразу: {text}"
    assert "systemctl" not in text and "nft" not in text, "без блокировки скрипт что-то применил"
    assert (dns_d / "awg-gw-vpn-feed.conf").read_text() == "nftset=/old.org/inet#awg_home#lan_vpn4\n"
    assert not (dump / "lists.status").exists(), "статус переписан запуском, который ничего не сделал"


def test_lists_run_normally_once_the_lock_is_taken(lists_env):
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "flock", f'echo "flock $*" >> {log}\nexit 0\n')
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "systemctl restart dnsmasq" in log.read_text()


def test_lists_check_the_feed_with_the_same_conf_dir_as_debian(lists_env):
    """init-скрипт Debian подключает conf-dir ключом, а не строкой в
    dnsmasq.conf: голый `dnsmasq --test` наш фид не видел вовсе и одобрял
    любой мусор, после которого рестарт оставлял квартиру без DNS."""
    tool, dns_d, dump, log, env = lists_env
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    tests = [ln for ln in log.read_text().splitlines() if ln.startswith("dnsmasq --test")]
    assert tests == [f"dnsmasq --test --conf-dir={dns_d},.dpkg-dist,.dpkg-old,.dpkg-new"], (
        f"проверка конфигурации не видит каталог с фидом: {tests}")


def _failing_restart(bin_dir: Path, log: Path, fails: int = 1) -> None:
    """systemctl, у которого первые `fails` рестартов dnsmasq не проходят."""
    cnt = log.parent / "restarts"
    _fake(bin_dir, "systemctl",
          f'echo "systemctl $*" >> {log}\n'
          f'if [ "$1" = restart ]; then n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}\n'
          f'  [ "$n" -le {fails} ] && exit 1; fi\nexit 0\n')


def test_a_feed_dnsmasq_cannot_start_with_is_rolled_back(lists_env):
    """--test пропустил, а демон с новым фидом не поднялся (память, дубли с
    чужим файлом). Раньше код был 0 и файл оставался — dnsmasq лежал, квартира
    без DNS, агент думал, что всё применено. Теперь прежний фид возвращается,
    демон поднимается с ним, код 1."""
    tool, dns_d, dump, log, env = lists_env
    feed = dns_d / "awg-gw-vpn-feed.conf"
    feed.write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    _failing_restart(Path(env["PATH"].split(":", 1)[0]), log)
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1, f"отказ рестарта выдан за успех: rc={r.returncode}"
    assert "не поднялся с новым фидом" in r.stderr and "откатываю" in r.stderr
    assert feed.read_text() == "nftset=/old.org/inet#awg_home#lan_vpn4\n", "прежний фид не вернулся"
    assert not (dns_d / "awg-gw-vpn-feed.conf.prev.awg").exists(), "копия отката осталась в conf-dir"
    assert log.read_text().count("systemctl restart dnsmasq") == 2, (
        "после отката демон надо поднять с прежним фидом")
    assert "rc=1" in (dump / "lists.status").read_text()


def test_a_first_feed_dnsmasq_cannot_start_with_is_removed(lists_env):
    """Прежнего фида не было — откатывать не к чему, новый убирается целиком:
    dnsmasq без фида лучше, чем лежащий dnsmasq с фидом."""
    tool, dns_d, dump, log, env = lists_env
    _failing_restart(Path(env["PATH"].split(":", 1)[0]), log)
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    assert not (dns_d / "awg-gw-vpn-feed.conf").exists(), "фид, с которым демон не встал, остался"


def test_the_rollback_copy_is_removed_only_after_a_successful_restart(lists_env):
    tool, dns_d, dump, log, env = lists_env
    feed = dns_d / "awg-gw-vpn-feed.conf"
    feed.write_text("nftset=/old.org/inet#awg_home#lan_vpn4\n", encoding="utf-8")
    r = subprocess.run(["sh", str(tool)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "nftset=/x.com/inet#awg_home#lan_vpn4" in feed.read_text()
    assert not (dns_d / "awg-gw-vpn-feed.conf.prev.awg").exists(), (
        "копия отката осталась в conf-dir: dnsmasq читает там всё, кроме .dpkg-*")


# ── раздел 5: оверрайд dnsmasq и /etc/default/dnsmasq ───────────────────────

def _ovr_fragment(script: str) -> str:
    """Кусок lan_apply от уборки /etc/default/dnsmasq до сборки оверрайда
    включительно — то, что уедет на малину, без правки."""
    sec = script.split('step "5. Локальная сеть без VPN"', 1)[1]
    start = sec.index("    if grep -qs '^# awg-bot: резолвер только для локальной сети'")
    end = sec.index('    rm -f "$_ovr_want"\n', start) + len('    rm -f "$_ovr_want"\n')
    return sec[start:end]


@pytest.fixture()
def ovr_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "log"
    unit = tmp_path / "dnsmasq.service"
    unit.write_text("[Service]\nExecStart=/usr/share/dnsmasq/systemd-helper exec\n"
                    "ExecStartPost=/usr/share/dnsmasq/systemd-helper start-resolvconf\n"
                    "ExecStop=/usr/share/dnsmasq/systemd-helper stop-resolvconf\n", encoding="utf-8")
    _fake(bin_dir, "systemctl",
          f'echo "systemctl $*" >> {log}\n[ "$1" = cat ] && cat {unit}\nexit 0\n')
    default = tmp_path / "default-dnsmasq"
    ovr = tmp_path / "dropin" / "awg-gw.conf"
    prog = ("MODE=apply\nrun() { sh -c \"$*\"; }\n_dn_changed=0\n"
            f"DNSMASQ_DEFAULT={default}\nDNSMASQ_OVR={ovr}\n"
            + _ovr_fragment(script) + 'echo "changed=$_dn_changed"\n')

    def go(**extra):
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp_path), **extra}
        return _sh(prog, env=env)
    return go, ovr, default, unit, log


def test_the_dnsmasq_override_starts_after_the_uplink_and_drops_the_resolvconf_hook(ovr_env):
    """dnsmasq, стартовавший раньше awg0, оставался без апстрима
    `server=…@awg0` до ручного рестарта (так было на живых малинах). Хук
    resolvconf Debian вписал бы 127.0.0.1 системным резолвером малины — её
    собственный DNS зависел бы от аплинка."""
    go, ovr, default, unit, log = ovr_env
    r = go(UPLINK_IF="awg0")
    assert r.returncode == 0, r.stderr
    text = ovr.read_text()
    assert "[Unit]\nAfter=awg-quick@awg0.service\nWants=awg-quick@awg0.service\n" in text, text
    assert "[Service]\nRestart=on-failure\nRestartSec=5\n" in text
    assert "ExecStartPost=\nExecStop=\n" in text, "хук resolvconf из юнита не снят"
    assert "systemctl daemon-reload" in log.read_text() and "changed=1" in r.stdout


def test_the_resolvconf_hook_is_cleared_only_where_it_exists(ovr_env):
    """Пустые ExecStartPost=/ExecStop= на юните, где их не было, сносят чужие
    строки — ровно те, что пакет мог добавить позже. Нет хука — нет и сброса."""
    go, ovr, default, unit, log = ovr_env
    unit.write_text("[Service]\nExecStart=/usr/sbin/dnsmasq -k\n", encoding="utf-8")
    assert go(UPLINK_IF="awg0").returncode == 0
    assert "ExecStartPost" not in ovr.read_text() and "ExecStop" not in ovr.read_text()


def test_an_unchanged_override_is_not_rewritten_and_does_not_reload(ovr_env):
    """Оверрайд собирается при каждом старте юнита обвязки: переписывать его и
    дёргать daemon-reload без изменений — рестарт dnsmasq всей квартиры на
    ровном месте."""
    go, ovr, default, unit, log = ovr_env
    go(UPLINK_IF="awg0")
    log.write_text("", encoding="utf-8")
    r = go(UPLINK_IF="awg0")
    assert "changed=0" in r.stdout, "неизменный оверрайд посчитан изменением"
    assert "daemon-reload" not in log.read_text()
    r = go(UPLINK_IF="awg1")                      # сменился аплинк — оверрайд обязан переехать
    assert "awg-quick@awg1.service" in ovr.read_text() and "changed=1" in r.stdout


def test_the_old_dnsmasq_except_line_is_removed_and_nothing_else(ovr_env):
    """DNSMASQ_EXCEPT=lo init-скрипт Debian превращает в except-interface=lo, а
    тот перекрывает listen-address: dnsmasq переставал слушать 127.0.0.1, и
    проверка апстрима была вечно красной. Свою прежнюю строку убираем, чужие
    строки файла — не трогаем."""
    go, ovr, default, unit, log = ovr_env
    default.write_text("ENABLED=1\nCONFIG_DIR=/etc/dnsmasq.d,.dpkg-dist\n"
                       "\n# awg-bot: резолвер только для локальной сети, системный DNS малины не трогать\n"
                       "DNSMASQ_EXCEPT=lo\n", encoding="utf-8")
    r = go(UPLINK_IF="awg0")
    assert r.returncode == 0, r.stderr
    text = default.read_text()
    assert "DNSMASQ_EXCEPT" not in text and "awg-bot" not in text, text
    assert "ENABLED=1\n" in text and "CONFIG_DIR=/etc/dnsmasq.d,.dpkg-dist\n" in text, "снесли чужое"
    log.write_text("", encoding="utf-8")
    before = default.read_text()
    go(UPLINK_IF="awg0")
    assert default.read_text() == before, "второй проход снова правил файл"


def test_a_foreign_dnsmasq_except_line_is_left_alone(ovr_env):
    """Строку без нашей пометки поставил человек — это его решение."""
    go, ovr, default, unit, log = ovr_env
    default.write_text("DNSMASQ_EXCEPT=lo\n", encoding="utf-8")
    go(UPLINK_IF="awg0")
    assert default.read_text() == "DNSMASQ_EXCEPT=lo\n"


def test_a_missing_default_file_is_not_created(ovr_env):
    """На чистой малине файл, созданный до установки пакета, становится его
    conffile: dpkg без терминала падает на вопросе о нём."""
    go, ovr, default, unit, log = ovr_env
    assert go(UPLINK_IF="awg0").returncode == 0
    assert not default.exists(), "создан /etc/default/dnsmasq до установки пакета"


# ── сервисы соседних сетей: awg-lan-services.sh ─────────────────────────────
# Концепт «сервисы соседних сетей» §4.3, §9: файл записей собирает агент из
# данных, пришедших каналом с ВПС; помощник — последний рубеж на малине.

def _svc_file() -> str:
    """Файл так, как его соберёт агент-получатель: из общего модуля."""
    from awgbot.domain import gwservices as gs
    items = [{"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"},
             {"t": "_smb._tcp", "n": "Time Machine", "h": "naspi5", "p": 10445, "a": "192.168.1.10"}]
    return gs.render_dnsmasq(items, ["192.168.68.0/24"])


@pytest.fixture()
def svc_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    dump = tmp_path / "dump"
    log = tmp_path / "log"; log.write_text("", encoding="utf-8")
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "dnsmasq", f'echo "dnsmasq $*" >> {log}; exit "${{DNSMASQ_TEST_RC:-0}}"\n')
    _fake(bin_dir, "id", 'echo "${FAKE_UID:-0}"\n')
    tool = tmp_path / "awg-lan-services.sh"
    tool.write_text(_heredoc(script, "LAN_SERVICES", "SVCEOF"), encoding="utf-8"); tool.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_LAN_DUMP": str(dump)}
    new = tmp_path / "peer-services.conf.new"

    def go(text: str | None, **extra) -> subprocess.CompletedProcess:
        args = []
        if text is not None:
            new.write_text(text, encoding="utf-8")
            args = [str(new)]
        return subprocess.run(["sh", str(tool), *args], capture_output=True, text=True,
                              env={**env, **extra})
    return go, dns_d / "awg-gw-peer-services.conf", log


OLD_SVC = "# прежние записи\nlocal=/awg.internal/\n"


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
    r = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
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
    помощнике на малине. Разойдись они — либо агент соберёт файл, который
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
