"""
Проверки routing-gw-setup.sh — обвяза шлюза (малины).

Шлюз стоит в чужом доме, за NAT, и попасть на него сложнее, чем на ВПС. Его
отказы видны только со стороны сервера и выглядят одинаково — «интернета за
шлюзом нет», — поэтому цена молчаливой поломки здесь особенно высока.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


def _sh(prog: str, *, path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess:
    return subprocess.run(["sh", "-c", prog], capture_output=True, text=True,
                          env={"PATH": path})


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


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

def test_container_detection_always_returns_zero(script):
    """Функция обязана завершаться успехом, даже не найдя контейнер.

    Без явного `return 0` она отдаёт статус последней команды цикла — неудачного
    `docker exec` на последнем контейнере. Присваивание из подстановки получает
    ненулевой статус, и при set -e скрипт умирает МОЛЧА, не дойдя даже до
    сообщения об ошибке. Ровно так он и «отработал», не поставив ни правила.
    """
    fn = script.split("detect_container() {", 1)[1].split("\n}", 1)[0]
    assert fn.rstrip().endswith("return 0"), "detect_container может вернуть ненулевой статус"


def test_missing_container_is_not_an_error(script, tmp_path):
    """Контейнер шлюзу больше не нужен: линк поднимают хостовые утилиты.

    Требование его наличия делало скрипт неработоспособным ровно там, куда мы и
    идём — на шлюзе без Amnezia: detect_container без docker молча отдаёт пусто.
    """
    fn = re.search(r"^detect_container\(\) \{.*?^\}$", script, re.S | re.M).group(0)
    r = _sh(fn + "\nset -e\nc=$(detect_container)\necho \"[$c]\"")     # docker в PATH нет
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stderr
    r = _sh(fn + "\nCONTAINER=given\ndetect_container")                  # явный — без поиска
    assert r.stdout == "given"


def test_container_commands_are_guarded(script):
    """`docker exec` зовётся только когда контейнер найден."""
    for i, line in enumerate(script.splitlines()):
        if "docker exec $CONTAINER" in line:
            preceding = "\n".join(script.splitlines()[max(0, i - 3):i])
            assert '[ -n "$CONTAINER" ]' in preceding, \
                f"незащищённый docker exec в строке {i + 1}"


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
    assert "policy accept" in body.split("chain input", 1)[1].split("}", 1)[0], "домашняя сеть не запирается"


def test_admin_devices_reach_the_home_lan_others_do_not(script):
    """Устройствам админа с туннеля открыта домашняя сеть, прочим — изоляция:
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
    маскарада улетает в аплинк с домашним адресом — ВПС его отбрасывает. Маскарад
    в аплинк ставит сам скрипт, а не чужое правило домашней схемы."""
    assert 'UPLINK_MASQ="        oifname \\"$UPLINK_IF\\" masquerade"' in script
    body = script.split("GUARDEOF", 1)[1]
    post = body.split("chain postrouting", 1)[1].split("}", 1)[0]
    assert 'ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade' in post
    assert "$UPLINK_MASQ" in post, "маскарад в аплинк — в той же nat-цепочке"
    assert script.index('UPLINK_MASQ=""') < script.index("cat <<GUARDEOF"), "переменная считается до heredoc"


# ── чей это шлюз: ключ УСТРОЙСТВА, а не линка ────────────────────────────────

def _uplink_fns(script: str) -> str:
    """Функции поиска аплинка из скрипта — гоняем их как есть."""
    return script.split("# Публичный ключ интерфейса", 1)[1].split("# Прежняя обвязка в iptables", 1)[0]


def _fake_awg(tmp_path, ifaces: str, keys: dict) -> Path:
    """`awg` для прогона: список интерфейсов, ключ интерфейса, pubkey из stdin
    (PRIV-X → PUB-X)."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text("".join(f"{k} {v}\n" for k, v in keys.items()), encoding="utf-8")
    awg = bin_dir / "awg"
    awg.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = pubkey ] && { sed 's/^PRIV/PUB/'; exit 0; }\n"
        f"[ \"$1\" = show ] && [ \"$2\" = interfaces ] && {{ printf '%s\\n' '{ifaces}'; exit 0; }}\n"
        f"[ \"$1\" = show ] && [ \"$3\" = public-key ] && {{ grep \"^$2 \" '{keys_file}' | cut -d' ' -f2; exit 0; }}\n"
        "exit 1\n", encoding="utf-8")
    awg.chmod(0o755)
    return awg


def _uplinks(script, tmp_path, *, ifaces, keys, confs, link_if="awglink2"):
    """(вывод uplink_list, найденный по ключу PUB-A аплинк)."""
    conf_dir = tmp_path / "awg"; conf_dir.mkdir(exist_ok=True)
    for name, priv in confs.items():
        (conf_dir / f"{name}.conf").write_text(f"[Interface]\nPrivateKey = {priv}\nAddress = 10.9.0.2/32\n",
                                               encoding="utf-8")
    awg = _fake_awg(tmp_path, ifaces, keys)
    prelude = f'set -e\nAWG_BIN="{awg}"\nLINK_IF="{link_if}"\nHOST_CONF_DIR="{conf_dir}"\n'
    r = _sh(prelude + _uplink_fns(script) + '\nuplink_list\necho "|$(iface_by_pubkey PUB-A)|"')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    return [ln for ln in lines[:-1] if ln.strip()], lines[-1].strip("|")


def test_marked_gateway_is_recognised_before_its_uplink_comes_up(script, tmp_path):
    """Юнит обвязки стартует раньше awg-quick@ аплинка: живых интерфейсов, кроме
    линка, ещё нет. По одним живым интерфейсам машина выглядела чужой — линк
    ложился, юнит гасил сам себя, и шлюз не вставал после ребута. Ключ
    устройства лежит в конфиге и до подъёма интерфейса."""
    lst, found = _uplinks(script, tmp_path, ifaces="awglink2", keys={},
                          confs={"awg0": "PRIV-A", "awglink2": "PRIV-L"})
    assert found == "awg0", "помеченное устройство узнано по конфигу аплинка"
    assert lst == ["awg0 PUB-A"]


def test_live_uplink_key_decides_for_a_foreign_machine(script, tmp_path):
    """Аплинк поднят и ключ ЧУЖОЙ — машина шлюзом слота не является."""
    lst, found = _uplinks(script, tmp_path, ifaces="awg0 awglink2", keys={"awg0": "PUB-B"},
                          confs={"awg0": "PRIV-B", "awglink2": "PRIV-L"})
    assert found == "" and lst == ["awg0 PUB-B"]


def test_link_of_the_neighbour_slot_is_not_an_uplink(script, tmp_path):
    """Ключ линка принадлежит паре ВПС↔шлюз, а не устройству: сравнивать с ним
    пометку слота нельзя — ни со своим линком, ни с линком соседнего слота."""
    lst, found = _uplinks(script, tmp_path, ifaces="awglink awglink2",
                          keys={"awglink": "PUB-A", "awglink2": "PUB-L"},
                          confs={"awglink": "PRIV-A", "awglink2": "PRIV-L"})
    assert lst == [] and found == "", "линк аплинком не считается"


def test_foreign_machine_does_not_switch_its_own_unit_off(script):
    """Выключенный юнит означал ручное вмешательство даже там, где машина
    шлюзом осталась: после ребута она решала «шлюз чужой» и гасила себя. Юнит
    идемпотентен и перепроверяет пометку при каждом старте."""
    assert "systemctl disable awg-link-gw" not in script
    foreign = script.split('if [ "$GW_FOREIGN" = "1" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "down $LINK_IF" in foreign and "exit 0" in foreign


def test_unresolved_uplink_leaves_the_link_alone(script):
    """Аплинка не видно вовсе — сказать «шлюз чужой» не по чему: линк не трогаем
    (раньше эта ветка сливалась с чужим шлюзом и линк ложился)."""
    unconf = script.split('if [ "$GW_UNCONFIRMED" = "1" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "down $LINK_IF" not in unconf and 'write_status "unconfirmed"' in unconf
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split("# ── 1. конфиг", 1)[0]
    assert 'GW_STATUS="unconfirmed"' in step0 and 'elif [ -n "$_others" ]' in step0


def test_unit_starts_after_the_uplink(script):
    """awg-link-gw без упорядочивания стартует раньше awg-quick@ аплинка."""
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    for d in ("After", "Wants"):
        assert f"{d}=network-online.target${{UPLINK_IF:+ awg-quick@$UPLINK_IF.service}}" in unit, d


def test_github_goes_into_the_uplink_like_telegram(script):
    """Агент обновляется с GitHub, а в юрисдикции шлюза он без туннеля
    недоступен: та же метка → та же политика → аплинк. Список — тот же, что у
    агента (domain/gateway.py GH_RANGES)."""
    import re
    from awgbot.domain.gateway import GatewayServices
    m = re.search(r'^GH_NETS="([^"]+)"$', script, re.M)
    assert m and set(m.group(1).split()) == set(GatewayServices.GH_RANGES)
    body = script.split("GUARDEOF", 1)[1]
    assert "set gh_nets4 {" in body and "elements = { $(ipv4_list $GH_NETS) }" in body
    out = body.split("chain output", 1)[1].split("}", 1)[0]
    assert "ip daddr @gh_nets4 meta mark set $TG_MARK" in out, "той же меткой, что Telegram"


# ── база аплинка (v3.0, этап 1): грабли ручного слоя, снятые в поставке ───────

def test_uplink_unit_retries_until_it_wins(script):
    """awg-quick@<аплинк> на загрузке падает, пока DNS не резолвит Endpoint; пять
    отказов за десять секунд — и systemd сдаётся навсегда: малина без Telegram и
    без списков. Лимит снимаем, перезапускаем до победы."""
    step0 = script.split('step "0. Шлюзовое устройство"', 1)[1].split("# ── 1. конфиг", 1)[0]
    assert "awg-quick@$UPLINK_IF.service.d" in step0
    assert "StartLimitIntervalSec=0" in step0 and "Restart=on-failure" in step0
    assert step0.index('GW_STATUS="confirmed"') < step0.index("StartLimitIntervalSec=0"), \
        "оверрайд — только помеченному шлюзу, аплинк которого известен"
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "awg-quick@*.service.d/awg-gw.conf" in rollback


def test_networkd_leaves_awg_interfaces_alone(script):
    """OMV переводит сеть на networkd, тот подхватывает awg0 и awglink как
    обычные интерфейсы, и любой Apply останавливал оба туннеля молча — снаружи
    это выглядело отказом ВПС."""
    assert re.search(r'^NETWORKD_UNMANAGED="/etc/systemd/network/[^"]+\.network"', script, re.M)
    part = script.split('step "1b.', 1)[1].split('step "2.', 1)[0]
    assert "Name=awg*" in part and "Unmanaged=yes" in part
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "$NETWORKD_UNMANAGED" in rollback


def test_rp_filter_is_loose_everywhere_the_uplink_answers(script):
    """Ответы из интернета приходят в аплинк, а обратный путь до источника по
    main лежит через домашний интерфейс — строгий rp_filter такое роняет.
    Loose (2), не 0: проверка остаётся, выключать её незачем. Аплинк — из PostUp:
    в sysctl-файл его не вписать, интерфейса на момент применения ещё нет."""
    apply_part = script.split('step "1a.', 1)[1]
    assert "net.ipv4.conf.all.rp_filter = 2" in apply_part and "net.ipv4.conf.default.rp_filter = 2" in apply_part
    assert "> $SYSCTL_CONF" in apply_part
    awk = script.split("awk -v mark=\"$TG_MARK\" -v tbl=\"$UPLINK_TABLE\" '", 1)[1].split("' \"$_tmp\"", 1)[0]
    assert "PostUp = sysctl -qw net.ipv4.conf.%%i.rp_filter=2" in awk


def test_uplink_gets_mss_clamp_next_to_its_masquerade(script):
    """Транзитный TCP из локальной сети в туннель без клампа виснет на больших
    ответах. Свойство аплинка, не домашнего слоя."""
    assert 'UPLINK_MSS="        oifname \\"$UPLINK_IF\\" tcp flags syn / syn,rst tcp option maxseg size set rt mtu"' in script
    body = script.split("GUARDEOF", 1)[1]
    fwd = body.split("chain forward", 1)[1].split("}", 1)[0]
    assert "$UPLINK_MSS" in fwd
    assert script.index('UPLINK_MSS=""') < script.index("cat <<GUARDEOF")


def test_peer_subnets_are_let_in_from_the_link_above_the_private_drop(script):
    """Доступ между подсетями за шлюзами: чужие подсети — по источнику, выше
    drop по приватным; набор из PEER_HOME_NETS бандла, закреплён в юните."""
    assert re.search(r'^PEER_HOME_NETS="\$\(printf .*tr -cd \'0-9\./ \'', script, re.M)
    assert 'PEER_ELEMS="$(ipv4_list $PEER_HOME_NETS)"' in script
    body = script.split("GUARDEOF", 1)[1]
    assert "set peer_nets4 {" in body
    fwd = script.split("chain forward {\n        type filter hook forward priority filter; policy accept;", 1)[1].split("}", 1)[0]
    assert fwd.index('ip saddr @peer_nets4 accept') < fwd.index('ip daddr @private4 drop')
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    assert 'Environment="PEER_HOME_NETS=$PEER_HOME_NETS"' in unit


def test_foreign_forward_drop_gets_accepts_for_our_interfaces(script):
    """docker ставит политику FORWARD DROP в ip filter; accept в нашей inet-таблице
    её не отменяет — транзит квартиры умирал бы молча. Правила в его цепочке,
    только когда политика DROP, снимаются при откате."""
    sec = script.split('step "3a. Чужая политика FORWARD"', 1)[1].split('step "4.', 1)[0]
    assert "grep -q '^-P FORWARD DROP'" in sec
    assert 'iptables -w -C FORWARD "$1" "$2" -j ACCEPT' in sec and "iptables -w -I FORWARD 1 $1 $2 -j ACCEPT" in sec
    assert '"$LINK_IF" "${UPLINK_IF:-}" "${LAN_IF_PRE:-}"' in sec
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "iptables -w -D FORWARD" in rollback


# ── SSH снаружи: цепочка ssh_in, порт как факт, локальное состояние ──────────

def test_ssh_filter_chain_is_always_there_but_jumped_only_when_enabled(script):
    """Цепочка и наборы создаются всегда (по ним агент отличает обвязку нового
    образца); переход на неё в input — только при SSH_FILTER=1: выключенный
    фильтр не должен оставлять в input ни одного правила про SSH."""
    body = script.split("GUARDEOF", 1)[1]
    assert "chain ssh_in {" in body and "set ssh_allow4 {" in body and "set server4 {" in body
    assert '[ "$SSH_FILTER" = 1 ] && SSH_JUMP="        meta nfproto ipv4 tcp dport $SSH_PORT jump ssh_in"' \
        in script, "без nfproto v6-пакет из квартиры доходил бы до drop"
    inp = body.split("chain input", 1)[1].split("}", 1)[0]
    assert "$SSH_JUMP" in inp and inp.index("jump tunnel_in") < inp.index("$SSH_JUMP"), \
        "туннель разбирается своей цепочкой раньше"
    sin = body.split("chain ssh_in", 1)[1].split("}", 1)[0]
    assert "ip saddr @lan4 accept" in sin, "локальная сеть открыта всегда — запереться из квартиры нельзя"
    assert "@private4" not in sin, "RFC1918 целиком пускал бы соседей по CGNAT (100.64/10) как своих"
    assert "set lan4 {" in body and "elements = { $LAN_ELEMS }" in body
    assert "ip saddr @server4 accept" in sin and "ip saddr @ssh_allow4 accept" in sin
    assert sin.strip().endswith("drop")


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
    fake = tmp_path / "getent"
    fake.write_text("#!/bin/sh\n[ \"$1\" = ahostsv4 ] && echo '198.51.100.7 STREAM vpn.example.org'\n")
    fake.chmod(0o755)
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 {conf}", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == "198.51.100.7"
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 /nonexistent")
    assert out.stdout.strip() == "" and out.returncode == 0, "нет конфига — пустой набор, не ошибка"
    conf.write_text("[Peer]\nEndpoint = [2001:db8::1]:51820\n")
    out = _sh(f"server_ipv4() {{{fns}\n}}\nserver_ipv4 {conf}", path=f"{tmp_path}:/usr/bin:/bin")
    assert out.stdout.strip() == "", "v6-эндпоинт снаружи не поддерживается — пусто, без getent"


def test_lan_ipv4_takes_link_routes_of_the_lan_interface(script, tmp_path):
    fns = script.split("lan_ipv4() {", 1)[1].split("\n}\n", 1)[0]
    fake = tmp_path / "ip"
    fake.write_text("#!/bin/sh\n[ \"$5\" = end0 ] && printf '%s\\n' "
                    "'192.168.1.0/24 proto kernel scope link src 192.168.1.111' "
                    "'192.168.50.0/24 proto kernel scope link src 192.168.50.2' "
                    "'192.168.1.0/24 proto kernel scope link src 192.168.1.111'\nexit 0\n")
    fake.chmod(0o755)
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


# ── канал до ВПС (концепт «канал линка»): рубильник в юните ──────────────────

def _channel_vars(script: str, **env) -> tuple[str, str]:
    """Прогнать настоящие строки скрипта, которые разбирают значения канала."""
    lines = [ln for ln in script.splitlines()
             if ln.startswith("LINK_CHANNEL") or ln.startswith('case "$LINK_CHANNEL_PORT"')]
    assert len(lines) == 3, f"строки разбора канала изменились: {lines}"
    prog = "\n".join(lines) + '\nprintf "%s|%s" "$LINK_CHANNEL" "$LINK_CHANNEL_PORT"'
    out = subprocess.run(["sh", "-c", prog], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", **env})
    assert out.returncode == 0, out.stderr
    return tuple(out.stdout.split("|"))


def test_a_bundle_without_the_channel_lines_leaves_the_channel_off(script):
    """Старые бандлы этих строк не несут вовсе. Молчаливое «включено по
    умолчанию» означало бы, что агент после обновления сам пошёл бы на ВПС —
    функцию включает перевыпуск конфигурации, и только он."""
    assert _channel_vars(script) == ("0", "8787")


def test_the_channel_switch_and_port_are_sanitised_before_they_reach_the_unit(script):
    """Значения приезжают из бандла, то есть из окружения. Мусор, доехавший до
    юнита, сломал бы разбор у агента (порт читается как число) или подменил бы
    команду в строке Environment=."""
    assert _channel_vars(script, LINK_CHANNEL="1", LINK_CHANNEL_PORT="9099") == ("1", "9099")
    assert _channel_vars(script, LINK_CHANNEL="1; rm -rf /")[0] == "1"
    assert _channel_vars(script, LINK_CHANNEL="да") == ("", "8787"), "непонятное — не «включено»"
    assert _channel_vars(script, LINK_CHANNEL_PORT="порт")[1] == "8787"
    assert _channel_vars(script, LINK_CHANNEL_PORT="0")[1] == "8787", "нулевой порт — не порт"
    assert _channel_vars(script, LINK_CHANNEL_PORT="123456789")[1] == "12345", "порт не длиннее пяти цифр"


def test_the_unit_carries_the_channel_lines_so_the_agent_can_read_them(script):
    """Единственный канал настроек на малину — бандл: агент читает рубильник из
    юнита обвязки, а не угадывает его. Пропадут строки — канал не поднимется
    ни на одном шлюзе, и понять это будет неоткуда."""
    unit = script.split('cat > "$UNIT" <<UNITEOF', 1)[1].split("UNITEOF", 1)[0]
    assert "Environment=LINK_CHANNEL=$LINK_CHANNEL\n" in unit
    assert "Environment=LINK_CHANNEL_PORT=$LINK_CHANNEL_PORT\n" in unit
    assert unit.index("Environment=LINK_CHANNEL=") < unit.index("EnvironmentFile=-$FW_ENV"), (
        "локальный файл читается после строк бандла — иначе он не перекроет их")
