"""
routing-gw-setup.sh: форвардинг, юнит обвязки, nft-таблица защиты, SSH снаружи
(цепочка ssh_in) и строки канала в юните.
"""
from __future__ import annotations

import re
import subprocess

import pytest
from tests.conftest import SH, fake_cmd
from tests.unit import _gw_setup_common as _c
from tests.unit._gw_setup_common import _sh

script = _c.script


def test_ip_forward_is_set_explicitly(script):
    """Без форвардинга правила NAT стоят и не работают — без единой ошибки.

    Раньше это держалось побочным эффектом docker: он выставляет ip_forward при
    старте. То есть шлюз работал на удаче — не запустился docker или его убрали,
    и он молча переставал быть шлюзом.
    """
    assert "net.ipv4.ip_forward" in script
    assert "sysctl -w net.ipv4.ip_forward=1" in script


def test_ip_forward_survives_reboot(script):
    """Значение sysctl живёт до перезагрузки.

    Вернувшийся из ребута шлюз выглядел бы исправным и не пропускал ни пакета.
    """
    assert re.search(r'^SYSCTL_CONF="/etc/sysctl\.d/[^"]+\.conf"$', script, re.M)
    apply_part = script.split('step "1a.', 1)[1]
    assert "net.ipv4.ip_forward = 1" in apply_part and "> $SYSCTL_CONF" in apply_part, \
        "форвардинг не записывается в drop-in — до ребута"


def test_rollback_removes_the_sysctl_drop_in(script):
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "SYSCTL_CONF" in rollback


def test_unit_does_not_depend_on_docker(script):
    """Линк поднимает хостовой awg-quick, а не утилиты из образа Amnezia.

    С Requires=docker.service не стартовавший или снесённый docker уносил за
    собой весь обвяз шлюза — молча, и обнаруживалось это со стороны ВПС как
    «интернета за шлюзом нет».
    """
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    # только директивы: в комментариях docker поминается намеренно — там
    # объясняется, почему зависимости больше нет
    directives = [ln for ln in unit.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("docker" in ln for ln in directives), \
        "юнит шлюза не должен зависеть от docker"
    assert any("network-online.target" in ln for ln in directives)


def test_link_subnet_is_masqueraded_too(script):
    """Зонд живости идёт с адреса линка, а не клиента.

    Без маскарада линк-подсети его пакеты уходят наружу немаскараженными и не
    возвращаются — бот считает исправный шлюз непроходимым.
    """
    assert "LINK_CIDR" in script
    assert "elements = { $CLIENT_SUBNET, $LINK_CIDR }" in script
    assert 'ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade' in script


# ── скрипт не должен умирать молча ───────────────────────────────────────────

def test_same_or_unchanged_config_is_not_reinstalled(script, tmp_path):
    """Повторный прогон «поверх» уже установленного конфига — обычное дело.

    `install` с совпадающими путями падает с «are the same file» и при set -e
    уносит весь остальной обвяз, который как раз и надо доставить. Тот же
    конфиг по другому пути — тоже не копируем: рестарт линка рвёт РФ-доступ
    у всех, а ради того же самого конфига рвать нечего.
    """
    block = script.split("LINK_SAME=0\n", 1)[1]
    first_fi = block.index("\nfi\n") + 4
    block = "LINK_SAME=0\n" + block[:block.index("\nfi\n", first_fi) + 4]
    conf_dir = tmp_path / "awg"; conf_dir.mkdir()
    (conf_dir / "awglink.conf").write_text("[Interface]\n", encoding="utf-8")
    other = tmp_path / "other.conf"; other.write_text("[Interface]\n", encoding="utf-8")
    changed = tmp_path / "changed.conf"; changed.write_text("[Interface]\nMTU = 1\n", encoding="utf-8")
    prelude = ('set -e\nsay(){ printf "%s\\n" "$*"; }\nrun(){ printf "RUN %s\\n" "$*"; }\n'
               f'HOST_CONF_DIR="{conf_dir}"\nLINK_IF="awglink"\n')
    same = _sh(prelude + f'SRC_CONF="{conf_dir}/awglink.conf"\n' + block)
    assert same.returncode == 0 and "уже на месте" in same.stdout and "RUN" not in same.stdout
    equal = _sh(prelude + f'SRC_CONF="{other}"\n' + block)
    assert "не изменился" in equal.stdout and "RUN" not in equal.stdout
    new = _sh(prelude + f'SRC_CONF="{changed}"\n' + block)
    assert "RUN install -m 600" in new.stdout


def test_install_self_is_idempotent_and_quiet_in_place(script):
    """Запуск уже из постоянного места не копирует сам в себя: `install a a`
    затёр бы файл, который в этот момент исполняется. И путь обязан уходить в
    stdout ОДИН — его подхватывает подстановка, любая лишняя строка уехала бы
    в ExecStart."""
    body = script.split("install_self() {", 1)[1].split("\n}", 1)[0]
    assert '[ "$_src" != "$_dst" ]' in body, "нет защиты от копирования в себя"
    assert body.count("printf '%s' \"$_dst\"") == 1
    assert ">&2" in body, "сообщение об установке уйдёт в stdout вместе с путём"


# ── единственная точка: nft-таблица вместо россыпи iptables ──────────────────

def test_plumbing_is_one_nft_table(script):
    """MASQUERADE, изоляция, метки Telegram и защита машины — в одной таблице,
    атомарно (объявить → удалить → создать), с проверкой синтаксиса до записи."""
    assert "table $GUARD_TABLE\ndelete table $GUARD_TABLE\ntable $GUARD_TABLE {" in script
    assert "nft -c -f" in script and 'run "nft -f $GUARD_FILE"' in script
    body = script.split("GUARDEOF", 1)[1]
    assert 'iifname "$LINK_IF" ip daddr @private4 drop' in body
    assert 'iifname "$LINK_IF" accept' in body
    assert body.index("@private4 drop") < body.index('iifname "$LINK_IF" accept'), "DROP выше ACCEPT"
    assert "ip daddr @tg_nets4 meta mark set $TG_MARK" in body


def test_gateway_itself_is_closed_to_tunnel_clients(script):
    """Изоляция в FORWARD не защищала саму машину: пакет клиента на адрес шлюза
    идёт в INPUT. С адресов туннеля на шлюз пускаем ВПС по линку (SSH) и
    устройства админа (всё), остальное drop."""
    body = script.split("GUARDEOF", 1)[1]
    assert "ip saddr @tunnel_nets4 jump tunnel_in" in body
    tin = body.split("chain tunnel_in", 1)[1].split("}", 1)[0]
    assert "ip saddr @admin4 accept" in tin
    assert "ip saddr $LINK_PEER tcp dport $SSH_PORT accept" in tin
    assert tin.strip().endswith("drop")
    assert "policy accept" in body.split("chain input", 1)[1].split("}", 1)[0], "локальная сеть не запирается"


def test_admin_devices_reach_the_home_lan_others_do_not(script):
    """Устройствам админа с туннеля открыта локальная сеть, прочим — изоляция:
    исключение стоит ВЫШЕ drop по приватным диапазонам."""
    fwd = script.split("chain forward", 1)[1].split("}", 1)[0]
    assert fwd.index('ip saddr @admin4 accept') < fwd.index("@private4 drop")


def test_admin_ips_come_from_bundle_and_local_env(script):
    assert 'Environment="ADMIN_IPS=$ADMIN_IPS"' in script
    assert "EnvironmentFile=-$FW_ENV" in script
    assert "ipv4_list $ADMIN_IPS $ADMIN_IPS_EXTRA" in script
    assert 'ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"' in script, "бандл прежнего выпуска принимается"
    # чужие символы в файл nft не попадают
    assert '*[!0-9./]*|"") ;;' in script


def test_legacy_iptables_is_removed_on_apply_and_rollback(script):
    apply_part = script.split('step "3. Снятие прежних правил iptables"', 1)[1]
    assert "legacy_cleanup" in apply_part
    rb = script.split('step "Снятие"', 1)[1].split("exit 0", 1)[0]
    assert "legacy_cleanup" in rb and "nft delete table $GUARD_TABLE" in rb
    cleanup = script.split("legacy_cleanup() {", 1)[1].split("\n}\n", 1)[0]
    for frag in ("-D FORWARD -i $LINK_IF -j $FWD_CHAIN", "-t nat -D POSTROUTING -s $CLIENT_SUBNET",
                 "iptables -X $FWD_CHAIN", "-t mangle -D OUTPUT -d $n -j MARK"):
        assert frag in cleanup, frag
    assert 'if [ "$MODE" = "plan" ]' in cleanup, "в режиме показа циклы -C/-D не сходятся"


def test_networkd_is_told_to_keep_foreign_rules_and_policy_is_reasserted(script):
    """systemd-networkd при перезапуске сносит чужие ip rule и маршруты; drop-in
    запрещает ему это, а сам скрипт перевыставляет правило и маршрут аплинка —
    реассерт юнита чинит и их. Откат drop-in убирает."""
    part = script.split('step "1b.', 1)[1].split('step "2.', 1)[0]
    assert "ManageForeignRoutes=no" in part and "ManageForeignRoutingPolicyRules=no" in part
    assert "/etc/systemd/networkd.conf.d/awg-gw.conf" in part
    assert 'ip rule add fwmark $TG_MARK lookup $UPLINK_TABLE' in part
    assert 'ip route replace default dev $UPLINK_IF table $UPLINK_TABLE' in part
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "networkd.conf.d/awg-gw.conf" in rollback


def test_guard_masquerades_the_agent_into_the_uplink(script):
    """Локальный пакет агента выбирает исходный адрес до метки в output и без
    маскарада улетает в аплинк с локальным адресом — ВПС его отбрасывает. Маскарад
    в аплинк ставит сам скрипт, а не чужое правило локальной схемы."""
    assert 'UPLINK_MASQ="        oifname \\"$UPLINK_IF\\" masquerade"' in script
    body = script.split("GUARDEOF", 1)[1]
    post = body.split("chain postrouting", 1)[1].split("}", 1)[0]
    assert 'ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade' in post
    assert "$UPLINK_MASQ" in post, "маскарад в аплинк — в той же nat-цепочке"
    assert script.index('UPLINK_MASQ=""') < script.index("cat <<GUARDEOF"), "переменная считается до heredoc"


# ── SSH снаружи: цепочка ssh_in, порт как факт, локальное состояние ──────────

def test_ssh_filter_chain_is_always_there_but_jumped_only_when_enabled(script):
    """Цепочка и наборы создаются всегда (по ним агент отличает обвязку нового
    образца); переход на неё в input — только при SSH_FILTER=1: выключенный
    фильтр не должен оставлять в input ни одного правила про SSH."""
    body = script.split("GUARDEOF", 1)[1]
    assert "chain ssh_in {" in body and "set ssh_allow4 {" in body and "set server4 {" in body
    assert '[ "$SSH_FILTER" = 1 ] && SSH_JUMP="        meta nfproto ipv4 tcp dport $SSH_PORT jump ssh_in"' \
        in script, "без nfproto v6-пакет из локальной сети доходил бы до drop"
    inp = body.split("chain input", 1)[1].split("}", 1)[0]
    assert "$SSH_JUMP" in inp and inp.index("jump tunnel_in") < inp.index("$SSH_JUMP"), \
        "туннель разбирается своей цепочкой раньше"
    sin = body.split("chain ssh_in", 1)[1].split("}", 1)[0]
    assert "ip saddr @lan4 accept" in sin, "локальная сеть открыта всегда — запереться из локальной сети нельзя"
    assert "@private4" not in sin, "RFC1918 целиком пускал бы соседей по CGNAT (100.64/10) как своих"
    assert "set lan4 {" in body and "elements = { $LAN_ELEMS }" in body
    assert "ip saddr @server4 accept" in sin and "ip saddr @ssh_allow4 accept" in sin
    assert sin.strip().endswith("drop")


def _guard_table(script: str, **env) -> str:
    """Отрисовать таблицу обвязки так, как её пишет скрипт: блок
    `{ cat <<GUARDEOF … } > "$GUARD_FILE.tmp"` с настоящей ipv4_list под sh,
    вывод — в stdout вместо файла. Переменные — минимальная сцена шлюза."""
    fn = script[script.index("ipv4_list() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    start = script.index("{\ncat <<GUARDEOF\n#!/usr/sbin/nft -f")
    end = script.index('} > "$GUARD_FILE.tmp"', start)
    block = script[start:end] + "}\n"
    base = {"GUARD_TABLE": "inet awg_gw_guard", "FW_ENV": "/etc/awg-bot/firewall.env",
            "CLIENT_SUBNET": "10.8.0.0/24", "LINK_CIDR": "10.99.99.4/30", "LINK_PEER": "10.99.99.5",
            "PRIVATE_NETS": "10.0.0.0/8 192.168.0.0/16", "TG_NETS": "91.108.4.0/22", "GH_NETS": "140.82.112.0/20",
            "ADMIN_ELEMS": "10.8.0.2", "PEER_ELEMS": "192.168.50.0/24", "SSH_ALLOW_ELEMS": "",
            "SERVER_ELEMS": "203.0.113.10", "LAN_ELEMS": "192.168.1.0/24", "SSH_PORT": "22",
            "SSH_JUMP": "        meta nfproto ipv4 tcp dport 22 jump ssh_in", "LINK_IF": "awglink2",
            "WAN_IF": "eth0", "UPLINK_IF": "", "UPLINK_MSS": "", "UPLINK_MASQ": ""}
    base.update(env)
    prog = "".join(f"{k}='{v}'\n" for k, v in base.items()) + fn + block
    r = _sh(prog)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _chain(table: str, name: str) -> list[str]:
    body = table.split(f"chain {name} {{", 1)[1].split("}", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


@pytest.mark.parametrize("peer", ["192.168.50.0/24", ""])
def test_ssh_in_lets_in_the_lan_behind_the_other_gateway(script, peer):
    """Доступ между подсетями за шлюзами включён — админ из локальной сети
    другого шлюза заходит на эту шлюз по SSH, как из своей. Правило стоит
    после established и своей сети и до drop: ниже drop оно не работало бы,
    а человек, для которого фильтр SSH включён, терял бы вход, ради которого
    подсети соединяли. Набор пуст (доступ выключен) — правило то же и ничего
    не пускает."""
    table = _guard_table(script, PEER_ELEMS=peer)
    assert _chain(table, "ssh_in") == [
        "ct state established,related accept",
        "ip saddr @lan4 accept",
        "ip saddr @peer_nets4 accept",
        "ip saddr @server4 accept",
        "ip saddr @ssh_allow4 accept",
        "drop",
    ], table.split("chain ssh_in", 1)[1][:400]


def test_every_set_used_by_ssh_in_is_declared_in_the_same_table(script):
    """`nft -c` отвергает всю таблицу, если правило ссылается на набор,
    которого в ней нет: обвязка шлюза не применилась бы вовсе — ни защиты от
    туннеля, ни маскарада. Набор peer_nets4 объявлен в той же таблице, что и
    ssh_in, до цепочек."""
    table = _guard_table(script)
    assert table.count("table inet awg_gw_guard {") == 1, table[:400]
    body = table.split("table inet awg_gw_guard {", 1)[1]
    declared = set(re.findall(r"^\s*set (\w+) \{", body, re.M))
    used = set(re.findall(r"@(\w+)", " ".join(_chain(table, "ssh_in"))))
    assert "peer_nets4" in used, "правила для подсетей другого шлюза в ssh_in нет"
    assert used <= declared, f"ssh_in ссылается на необъявленные наборы: {sorted(used - declared)}"
    assert body.index("set peer_nets4 {") < body.index("chain ssh_in {")
    peer_set = body.split("set peer_nets4 {", 1)[1].split("\n    }", 1)[0]
    assert "elements = { 192.168.50.0/24 }" in peer_set, peer_set


def test_ssh_port_comes_from_firewall_env_as_a_number(script):
    """Порт — факт от sshd, который агент пишет в firewall.env; не число — 22,
    иначе `nft -c` отказал бы всей таблице после ребута."""
    assert script.index('[ -f "$FW_ENV" ] && . "$FW_ENV"') > script.index('SSH_PORT="${SSH_PORT:-22}"')
    assert "case \"$SSH_PORT\" in ''|*[!0-9]*) SSH_PORT=22 ;; esac" in script
    assert '[ "$SSH_FILTER" = "1" ] || SSH_FILTER=0' in script
    # старое имя SSH_ALLOW из бандла — это ADMIN_IPS, но только когда ADMIN_IPS
    # НЕ ЗАДАН: юнит задаёт его пустым, а SSH_ALLOW из firewall.env — список снаружи
    assert 'ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"' in script
    assert script.index('SSH_ALLOW=""; SSH_ALLOW_RESOLVED=""; SSH_FILTER=""') \
        > script.index('ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"')
    out = _sh('ADMIN_IPS=""; SSH_ALLOW="203.0.113.7"; ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"; echo "[$ADMIN_IPS]"')
    assert out.stdout.strip() == "[]", "пустой ADMIN_IPS из юнита не подменяется списком снаружи"
    out = _sh('unset ADMIN_IPS; SSH_ALLOW="10.9.1.2"; ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"; echo "[$ADMIN_IPS]"')
    assert out.stdout.strip() == "[10.9.1.2]", "старый бандл без ADMIN_IPS принимается"
    assert "ipv4_list $SSH_ALLOW $SSH_ALLOW_RESOLVED" in script, "имена в nft не попадают — их отсеет ipv4_list"


def test_server_ipv4_takes_endpoint_host_and_resolves_names(script, tmp_path):
    fns = script.split("server_ipv4() {", 1)[1].split("\n}\n", 1)[0]
    conf = tmp_path / "awglink.conf"
    conf.write_text("[Peer]\nPublicKey = x\nEndpoint = 203.0.113.10:51820\n")
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 {conf}")
    assert out.stdout.strip() == "203.0.113.10"
    conf.write_text("[Peer]\nEndpoint=vpn.example.org:51820\n")
    fake_cmd(tmp_path / "getent",
             "#!/bin/sh\n[ \"$1\" = ahostsv4 ] && echo '198.51.100.7 STREAM vpn.example.org'\n")
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 {conf}", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == "198.51.100.7"
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 /nonexistent")
    assert out.stdout.strip() == "" and out.returncode == 0, "нет конфига — пустой набор, не ошибка"
    conf.write_text("[Peer]\nEndpoint = [2001:db8::1]:51820\n")
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 {conf}", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == "", "v6-эндпоинт снаружи не поддерживается — пусто, без getent"


def test_lan_ipv4_takes_link_routes_of_the_lan_interface(script, tmp_path):
    fns = script.split("lan_ipv4() {", 1)[1].split("\n}\n", 1)[0]
    fake_cmd(tmp_path / "ip",
             "#!/bin/sh\n[ \"$5\" = end0 ] && printf '%s\\n' "
             "'192.168.1.0/24 proto kernel scope link src 192.168.1.111' "
             "'192.168.50.0/24 proto kernel scope link src 192.168.50.2' "
             "'192.168.1.0/24 proto kernel scope link src 192.168.1.111'\nexit 0\n")
    out = _sh(f"lan_ipv4() {{{fns}\n}}\nlan_ipv4 end0", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == "192.168.1.0/24, 192.168.50.0/24"
    out = _sh(f"lan_ipv4() {{{fns}\n}}\nlan_ipv4 nope", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == ""
    assert 'LAN_ELEMS="10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"' in script, "запас — RFC1918 без CGNAT"


def test_rollback_keeps_the_local_firewall_env(script):
    """Адреса и порт в firewall.env — данные человека: --rollback откладывает
    файл в .bak, а не удаляет вместе с обвязкой."""
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert 'mv -f $FW_ENV $FW_ENV.bak' in rollback
    assert "$FW_ENV $GW_STATUS_FILE" not in rollback


def test_unit_gets_no_new_environment_lines_for_ssh(script):
    """Локальное состояние читается через EnvironmentFile — юнит без новых
    Environment=: бандл ничего про SSH снаружи не знает."""
    unit = script.split("cat > \"$UNIT\" <<UNITEOF", 1)[1].split("UNITEOF", 1)[0]
    assert "SSH_" not in unit and "EnvironmentFile=-$FW_ENV" in unit


# ── канал до ВПС: строки в юните ─────────────────

def _channel_vars(script: str, **env) -> tuple[str, str]:
    """Прогнать настоящие строки скрипта, которые задают значения канала."""
    lines = [ln for ln in script.splitlines()
             if ln.startswith("LINK_CHANNEL") or ln.startswith('case "$LINK_CHANNEL_PORT"')]
    assert len(lines) == 3, f"строки канала изменились: {lines}"
    prog = "\n".join(lines) + '\nprintf "%s|%s" "$LINK_CHANNEL" "$LINK_CHANNEL_PORT"'
    out = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", **env})
    assert out.returncode == 0, out.stderr
    return tuple(out.stdout.split("|"))


@pytest.mark.parametrize("env", [{}, {"LINK_CHANNEL": "0"}, {"LINK_CHANNEL": ""},
                                 {"LINK_CHANNEL": "да"}, {"LINK_CHANNEL": "1; rm -rf /"}])
def test_the_channel_line_is_always_on_whatever_the_bundle_says(script, env):
    """Канал обязателен: в юнит всегда уходит LINK_CHANNEL=1. Агенты прежних
    версий без этой строки (или с «0» из старого файла конфигурации) канал
    гасят — шлюз замолчал бы для ВПС до следующего обновления агента."""
    assert _channel_vars(script, **env)[0] == "1", f"окружение {env} повлияло на строку канала"


def test_the_channel_port_is_sanitised_before_it_reaches_the_unit(script):
    """Порт приезжает из файла конфигурации, то есть из окружения. Мусор,
    доехавший до юнита, сломал бы разбор у агента (порт читается как число)
    или подменил бы команду в строке Environment=."""
    assert _channel_vars(script) == ("1", "8787"), "без порта в файле конфигурации — порт по умолчанию"
    assert _channel_vars(script, LINK_CHANNEL_PORT="9099") == ("1", "9099")
    assert _channel_vars(script, LINK_CHANNEL_PORT="порт")[1] == "8787"
    assert _channel_vars(script, LINK_CHANNEL_PORT="0")[1] == "8787", "нулевой порт — не порт"
    assert _channel_vars(script, LINK_CHANNEL_PORT="123456789")[1] == "12345", "порт не длиннее пяти цифр"
    assert _channel_vars(script, LINK_CHANNEL_PORT="90$(reboot)99")[1] == "9099", "в порт проходит не только цифра"


def test_the_unit_carries_the_channel_lines_so_the_agent_can_read_them(script):
    """Порт канала агент читает из юнита обвязки, а строку LINK_CHANNEL —
    агенты прежних версий: пропадёт она, и каждый не обновлённый шлюз
    погасит канал, а понять это будет неоткуда."""
    unit = script.split('cat > "$UNIT" <<UNITEOF', 1)[1].split("UNITEOF", 1)[0]
    assert "Environment=LINK_CHANNEL=$LINK_CHANNEL\n" in unit
    assert "Environment=LINK_CHANNEL_PORT=$LINK_CHANNEL_PORT\n" in unit
    assert unit.index("Environment=LINK_CHANNEL=") < unit.index("EnvironmentFile=-$FW_ENV"), (
        "локальный файл читается после строк бандла — иначе он не перекроет их")
