"""
routing-gw-setup.sh, раздел 5 как текст и как функции: интерфейс шлюза по
локальной подсети, таблица awg_lan и переход с прежней awg_home, резолвер,
снятие режима, оверрайд dnsmasq, подмена помощников через mv, ожидание агента.
"""
from __future__ import annotations

import re
import subprocess

import pytest
from tests.conftest import SH
from tests.unit import _gw_lan_common as _c
from tests.unit._gw_lan_common import _fake, _heredoc, _sh

script = _c.script


# ── интерфейс и адрес шлюза — из локальной подсети ──────────────────────────

def test_lan_interface_is_found_by_home_subnet(script, tmp_path):
    """По подсети из бота шлюз находит свой LAN-интерфейс и адрес: их не
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


# ── раздел 5 скрипта: структура ──────────────────────────────────────────────

def test_home_table_keeps_its_sets_across_reasserts(script):
    """awg_gw_guard перевыставляется через delete table, а наборы awg_lan
    наполняет dnsmasq на лету — их терять нельзя: таблица без delete, цепочки
    flush + add."""
    home = script.split("cat <<LANEOF", 1)[1].split("LANEOF", 1)[0]
    assert not any("delete table" in ln for ln in home.splitlines() if not ln.lstrip().startswith("#"))
    assert 'set lan_vpn4' in home and "set lan_vpn_nets4" in home and "set lan_ru4" in home
    for ch in ("prerouting", "input", "forward", "postrouting"):
        assert f"flush chain $LAN_TABLE {ch}" in home
    rules = [ln for ln in home.splitlines() if ln.startswith("add rule $LAN_TABLE prerouting")]
    assert rules[0].endswith('iifname != "$LAN_IF" accept'), "из линка и аплинка — мимо маркировки"
    assert "meta pkttype host" in rules[1] and "!= $_net counter" in rules[1], \
        "счётчик заворота — только unicast на MAC шлюза, до вердиктов: бродкасты фонят всегда"
    assert "@lan_ru4 accept" in rules[2], "исключения ВЫШЕ метки"
    assert "@lan_vpn_nets4 meta mark set $TG_MARK" in rules[3] and "@lan_vpn4 meta mark set $TG_MARK" in rules[4], \
        "та же метка, что у агента — правило и маршрут уже есть"
    assert 'ip saddr $_net oifname "$LAN_IF" masquerade' in home
    assert 'iifname "$LAN_IF" tcp dport 853 drop' in home
    assert 'iifname "$LAN_IF" udp dport 53 counter' in home


def test_resolver_goes_upstream_through_the_uplink(script):
    sec = script.split('step "5. VPN-транзит"', 1)[1]
    assert "printf 'server=%s@%s\\n' \"$_upstream\" \"$UPLINK_IF\"" in sec, "без @iface запрос ушёл бы линком"
    assert '_upstream="${RESOLVER:-1.1.1.1}"' in sec, "без резолвера ВПС — запасной, но тоже через аплинк"
    assert "bind-dynamic" in sec and "listen-address=127.0.0.1,%s" in sec and "no-hosts" in sec
    assert "Restart=on-failure" in sec and "AmbientCapabilities" not in sec, "ambient-набор ядро чистит при setuid"
    assert "address=/use-application-dns.net/" in sec and "address=/dns.google/" in sec
    assert "filter-AAAA" in sec, "IPv6 у клиентов режется ответами резолвера, не sysctl на шлюзе"
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
    sec = script.split('step "5. VPN-транзит"', 1)[1].split('write_status "up"', 1)[0]
    assert "exit 1" not in sec and "if lan_apply; then" in sec
    assert 'LAN_ERROR=%s' in script and "lan_fail" in sec
    assert 'port53_busy' in sec and "Pi-hole" in sec, "проверка :53 до установки, с честным отказом"


def test_lan_mode_off_removes_its_own_and_rollback_too(script):
    sec = script.split('step "5. VPN-транзит"', 1)[1]
    off = sec.split('if [ "$LAN_MODE" = "1" ]; then', 1)[1].split("\nelse\n", 1)[1].split("\nfi\n", 1)[0]
    assert "lan_remove" in off and "lan_apply" not in off
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "lan_remove" in rollback
    wrapper = script.split("lan_remove() {", 1)[1].split("\n}", 1)[0]
    assert "lists.lock" in wrapper and "_lan_remove_locked" in wrapper, "снятие режима — не под блокировкой списков"
    fn = script.split("_lan_remove_locked() {", 1)[1].split("\n}", 1)[0]
    assert "for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf" in fn \
        and 'mv -f $DNSMASQ_D/$_f $LAN_DUMP/restore/$_f' in fn, \
        "личные списки — данные человека: в restore/, откуда их вернёт следующее включение, не rm"
    assert "nft delete table $LAN_TABLE" in fn
    assert "lan_table_old_drop" in fn, "снятие режима сразу после обновления оставило бы прежнюю таблицу"
    assert "systemctl disable --now dnsmasq" in fn and "$DNSMASQ_MARK" in fn, "ставили сами — снимаем"
    # dnsmasq читает в conf-dir всё, кроме .dpkg-*: .bak рядом класть нельзя
    assert ".bak" not in fn and 'mv -f $_f $_f.bak' not in script


def test_unit_and_status_carry_the_lan_variables(script):
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    for line in ("Environment=LAN_MODE=$LAN_MODE", 'Environment="LAN_SUBNETS=$LAN_SUBNETS"', "Environment=RESOLVER=$RESOLVER"):
        assert line in unit, line
    assert "LAN=%s\\nLAN_IF=%s\\nLAN_ADDR=%s" in script
    plan = script.split('if [ "$MODE" = "plan" ]; then\n    say ""', 1)[1].split("exit 0", 1)[0]
    assert "5. VPN-транзит" in plan


def test_embedded_scripts_parse(script, tmp_path):
    for var, tag in (("LAN_LISTS", "LISTSEOF"), ("LAN_DOMAIN", "DOMEOF")):
        f = tmp_path / f"{var}.sh"; f.write_text(_heredoc(script, var, tag), encoding="utf-8")
        r = subprocess.run([SH, "-n", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_paths_resolve_after_their_prefixes(script):
    """Файл таблицы вычислялся до GW_ETC и раскрывался в /home.nft — таблица ложилась
    в корень ФС. Шапку скрипта исполняем до первой функции и смотрим на значения."""
    head = script.split("\niface_pubkey()", 1)[0].split("set -e\n", 1)[1]
    r = _sh(head + '\necho "$LAN_FILE|$GUARD_FILE|$LAN_TABLE"', env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "/etc/awg-gw/lan.nft|/etc/awg-gw/guard.nft|inet awg_lan"


def test_port53_check_ignores_dnsmasq_and_the_resolved_stub(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    fn = script.split("port53_busy() {", 1)[1].split("\n}", 1)[0]
    prog = "port53_busy() {" + fn + "\n}\nLAN_ADDR=192.168.68.222\nport53_busy"
    _fake(bin_dir, "ss", 'cat <<EOF\nudp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=1,fd=1))\n'
                         'udp UNCONN 0 0 192.168.68.222:53 0.0.0.0:* users:(("dnsmasq",pid=2,fd=1))\nEOF\n')
    assert _sh(prog, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}).stdout.strip() == "", "stub resolved и наш dnsmasq — не помеха"
    _fake(bin_dir, "ss", 'echo \'udp UNCONN 0 0 0.0.0.0:53 0.0.0.0:* users:(("pihole-FTL",pid=3,fd=1))\'\n')
    assert "pihole-FTL" in _sh(prog, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}).stdout


# ── раздел 5: оверрайд dnsmasq и /etc/default/dnsmasq ───────────────────────

def _ovr_fragment(script: str) -> str:
    """Кусок lan_apply от уборки /etc/default/dnsmasq до сборки оверрайда
    включительно — то, что уедет на шлюз, без правки."""
    sec = script.split('step "5. VPN-транзит"', 1)[1]
    start = sec.index("    # /etc/default/dnsmasq НЕ трогаем: DNSMASQ_EXCEPT init-скрипт Debian")
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


def test_the_dnsmasq_override_has_no_ordering_against_the_uplink_and_drops_the_resolvconf_hook(ovr_env):
    """After=/Wants=awg-quick@аплинк в оверрайде dnsmasq давали цикл
    упорядочивания (dnsmasq Before=nss-lookup.target, awg-quick@ After=него):
    systemd выкидывал из цикла произвольную единицу — в худшем случае сам
    аплинк, и устройство оставалось без Telegram. Перечитывание после подъёма
    аплинка — ExecStartPost в оверрайде самого аплинка. Хук resolvconf Debian
    вписал бы 127.0.0.1 системным резолвером — её DNS зависел бы от аплинка."""
    go, ovr, default, unit, log = ovr_env
    r = go(UPLINK_IF="awg0")
    assert r.returncode == 0, r.stderr
    text = ovr.read_text()
    assert "After=" not in text and "Wants=" not in text and "[Unit]" not in text, text
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
    дёргать daemon-reload без изменений — рестарт dnsmasq всей локальной сети на
    ровном месте."""
    go, ovr, default, unit, log = ovr_env
    go(UPLINK_IF="awg0")
    log.write_text("", encoding="utf-8")
    r = go(UPLINK_IF="awg0")
    assert "changed=0" in r.stdout, "неизменный оверрайд посчитан изменением"
    assert "daemon-reload" not in log.read_text()
    r = go(UPLINK_IF="awg1")                      # аплинка в оверрайде нет — смена его имени ничего не меняет
    assert "awg-quick@" not in ovr.read_text() and "changed=0" in r.stdout


def test_a_missing_default_file_is_not_created(ovr_env):
    """На чистой шлюзе файл, созданный до установки пакета, становится его
    conffile: dpkg без терминала падает на вопросе о нём."""
    go, ovr, default, unit, log = ovr_env
    assert go(UPLINK_IF="awg0").returncode == 0
    assert not default.exists(), "создан /etc/default/dnsmasq до установки пакета"


# ── агент ↔ скрипт: «занято» доходит до человека ─────────────────────────────

def test_the_agent_waits_longer_than_the_script_waits_for_the_lock(script):
    """Скрипт ждёт блокировку до N секунд и лишь потом говорит «занято». Сдайся
    агент раньше — вместо внятного «обновление списков ещё идёт» человек
    увидел бы таймаут, а применение могло бы пройти уже без него."""
    import inspect
    from awgbot.infra import gwguard
    m = re.search(r"flock -w (\d+) 9", _heredoc(script, "LAN_DOMAIN", "DOMEOF"))
    assert m, "скрипт своих списков не ждёт блокировку"
    timeout = inspect.signature(gwguard.run_lan_domain).parameters["timeout"].default
    assert timeout > int(m.group(1)), f"агент ждёт {timeout} с, скрипт — {m.group(1)} с"


def test_the_busy_answer_reaches_the_agent_as_a_refusal(monkeypatch, tmp_path):
    from awgbot.infra import gwguard
    stub = tmp_path / "awg-lan-domain.sh"
    stub.write_text("#!/bin/sh\necho 'обновление списков ещё идёт' >&2\nexit 75\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(gwguard, "LAN_DOMAIN_SCRIPT", str(stub))
    assert gwguard.run_lan_domain("ru", ["shop.ru"]) == (False, "обновление списков ещё идёт")


def test_helpers_are_replaced_by_mv_not_rewritten_in_place(script, tmp_path):
    """Работающий экземпляр (dash читает скрипт кусками) должен дочитать свой
    прежний inode: write_lan_scripts пишет во временный файл и подменяет mv."""
    from tests.unit._gw_setup_common import _helpers
    # тело функции — heredoc'и со своими «}» в начале строки: конец — после SVCEOF
    start = script.index("\nwrite_lan_scripts() {") + 1
    end = script.index("\n}\n", script.index("\nSVCEOF\n", start)) + 3
    fn = script[start:end]
    sbin = tmp_path / "sbin"; sbin.mkdir()
    olds = {}
    for name in ("awg-lan-lists.sh", "awg-lan-domain.sh", "awg-lan-services.sh"):
        (sbin / name).write_text("#!/bin/sh\n# старый\n", encoding="utf-8")
        olds[name] = (sbin / name).stat().st_ino
    prog = (_helpers(script) + f'\nLAN_LISTS="{sbin}/awg-lan-lists.sh"\nLAN_DOMAIN="{sbin}/awg-lan-domain.sh"\n'
            f'LAN_SERVICES="{sbin}/awg-lan-services.sh"\n' + fn + "\nwrite_lan_scripts\n")
    r = _sh(prog, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in sbin.iterdir()) == sorted(olds), "временные .new остались рядом"
    for name, ino in olds.items():
        f = sbin / name
        assert f.stat().st_ino != ino, f"{name} переписан на месте — работающий экземпляр дочитает новый текст"
        assert f.stat().st_mode & 0o777 == 0o755 and "# старый" not in f.read_text(), name


# ── переход таблицы awg_home → awg_lan (снимается со следующим сдвигом минимума) ──

def _move_env(script, tmp_path, *, old_table: bool):
    """Шлюз после обновления до 3.3.1: прежняя таблица есть (или нет), свои
    списки, фид, слепки и копии для отката — со строками под прежним именем."""
    from tests.unit._gw_setup_common import _helpers
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "nft.log"
    sets = {"lan_vpn4": "1.2.3.4", "lan_vpn_nets4": "5.6.0.0/16", "lan_ru4": "7.7.7.7"}
    listing = "\n".join(
        f'[ "$5" = "{name}" ] && {{ printf \'table inet awg_home {{\\n\\tset {name} {{\\n\\t\\telements = {{ {el} }}\\n\\t}}\\n}}\\n\'; exit 0; }}'
        for name, el in sets.items())
    _fake(bin_dir, "nft", f'echo "nft $*" >> {log}\n'
          f'if [ "$1 $2 $3 $4" = "list table inet awg_home" ]; then exit {0 if old_table else 1}; fi\n'
          f'if [ "$1 $2 $3" = "list set inet" ]; then {listing}\n exit 1; fi\n'
          'exit 0\n')
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    dump = tmp_path / "dump"; (dump / "restore").mkdir(parents=True); (dump / "rollback").mkdir()
    gw_etc = tmp_path / "etc"; gw_etc.mkdir()
    (gw_etc / "home.nft").write_text("table inet awg_home {}\n", encoding="utf-8")
    old_line = "nftset=/example.org/inet#awg_home#lan_vpn4\n"
    files = {dns_d / "awg-gw-vpn-user.conf": old_line, dns_d / "awg-gw-ru-user.conf": old_line.replace("vpn4", "ru4"),
             dns_d / "awg-gw-vpn-feed.conf": old_line, dump / "restore" / "awg-gw-vpn-user.conf": old_line,
             dump / "rollback" / "vpn-feed.conf.prev": old_line, dump / "vpn-feed.src": old_line,
             dump / "lan_vpn4.nft": "table inet awg_home {\n\tset lan_vpn4 {\n\t}\n}\n"}
    for f, text in files.items():
        f.write_text(text, encoding="utf-8")
    move = re.search(r'^LAN_TABLE_OLD=.*?^lan_table_old_drop\(\) \{.*?^\}$', script, re.S | re.M).group(0)
    head = (_helpers(script) + f'\nMODE=apply\nDNSMASQ_D="{dns_d}"\nLAN_DUMP="{dump}"\nGW_ETC="{gw_etc}"\n'
            'LAN_TABLE="inet awg_lan"\n_dn_changed=0\n' + move + "\n")
    return {"bin": bin_dir, "log": log, "files": files, "dump": dump, "gw_etc": gw_etc, "head": head}


def test_the_old_table_moves_with_its_sets_and_own_lists(script, tmp_path):
    """Первый прогон 3.3.1 на шлюзе 3.3.0: наборы прежней таблицы — слепками
    под новым именем, все строки nftset= и слепки — на awg_lan, dnsmasq будет
    перезапущен (_dn_changed), прежняя таблица и её файл сняты после новой."""
    env = _move_env(script, tmp_path, old_table=True)
    r = _sh(env["head"] + 'lan_table_move_prepare\necho "changed=$_dn_changed"\nlan_table_move_finish\n',
            env={"PATH": f"{env['bin']}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "changed=1" in r.stdout and "переезжает" in r.stdout
    for f in env["files"]:
        text = f.read_text(encoding="utf-8")
        assert "awg_home" not in text and "awg_lan" in text, f"{f.name}: {text!r}"
    log = env["log"].read_text(encoding="utf-8")
    loads = [ln for ln in log.splitlines() if ln.startswith("nft -f ") and ".move.nft" in ln]
    assert len(loads) == 3, f"наборы прежней таблицы не загружены в новую: {log}"
    assert "nft delete table inet awg_home" in log
    assert log.index("nft -f") < log.index("nft delete table inet awg_home"), "прежняя снята раньше, чем легли наборы"
    assert not list(env["dump"].glob("*.move.nft")), "слепки переезда остались"
    assert not (env["gw_etc"] / "home.nft").exists(), "файл прежней таблицы остался"


def test_the_moved_set_dump_names_the_new_table(script, tmp_path):
    env = _move_env(script, tmp_path, old_table=True)
    r = _sh(env["head"] + "lan_table_move_prepare\n", env={"PATH": f"{env['bin']}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    dumped = (env["dump"] / "lan_ru4.move.nft").read_text(encoding="utf-8")
    assert dumped.startswith("table inet awg_lan {") and "7.7.7.7" in dumped, dumped


def test_without_the_old_table_nothing_moves(script, tmp_path):
    """Шлюз уже на 3.3.1 (или режим впервые): переписывать и перезапускать нечего."""
    env = _move_env(script, tmp_path, old_table=False)
    r = _sh(env["head"] + 'lan_table_move_prepare\necho "changed=$_dn_changed"\n',
            env={"PATH": f"{env['bin']}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "changed=0" in r.stdout
    assert all("awg_home" in f.read_text(encoding="utf-8") for f in env["files"]), "файлы тронуты без перехода"


def test_the_move_runs_inside_the_locked_apply_before_the_table(script):
    # тело с heredoc таблицы (в нём «}» с начала строки) — до развилки режима
    fn = script.split("_lan_apply_locked() {", 1)[1].split('\nif [ "$LAN_MODE" = "1" ]; then', 1)[0]
    assert fn.index("_dn_changed=0") < fn.index("lan_table_move_prepare") < fn.index('nft -f $LAN_FILE"')
    assert fn.index('nft -f $LAN_FILE"') < fn.index("lan_table_move_finish")
