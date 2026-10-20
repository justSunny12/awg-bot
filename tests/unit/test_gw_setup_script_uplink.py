"""
routing-gw-setup.sh: чей это шлюз (ключ устройства, а не линка) и база
аплинка — грабли ручного слоя, снятые в поставке.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import fake_cmd
from tests.unit import _gw_setup_common as _c
from tests.unit._gw_setup_common import _sh

script = _c.script


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
    fake_cmd(awg,
        "#!/bin/sh\n"
        "[ \"$1\" = pubkey ] && { sed 's/^PRIV/PUB/'; exit 0; }\n"
        f"[ \"$1\" = show ] && [ \"$2\" = interfaces ] && {{ printf '%s\\n' '{ifaces}'; exit 0; }}\n"
        f"[ \"$1\" = show ] && [ \"$3\" = public-key ] && {{ grep \"^$2 \" '{keys_file}' | cut -d' ' -f2; exit 0; }}\n"
        "exit 1\n")
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
    агента (domain/gateway/tgmark.py GH_RANGES)."""
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
    отказов за десять секунд — и systemd сдаётся навсегда: шлюз без Telegram и
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
    main лежит через локальный интерфейс — строгий rp_filter такое роняет.
    Loose (2), не 0: проверка остаётся, выключать её незачем. Аплинк — из PostUp:
    в sysctl-файл его не вписать, интерфейса на момент применения ещё нет."""
    apply_part = script.split('step "1a.', 1)[1]
    assert "net.ipv4.conf.all.rp_filter = 2" in apply_part and "net.ipv4.conf.default.rp_filter = 2" in apply_part
    assert "> $SYSCTL_CONF" in apply_part
    awk = script.split("awk -v mark=\"$TG_MARK\" -v tbl=\"$UPLINK_TABLE\" '", 1)[1].split("' \"$_tmp\"", 1)[0]
    assert "PostUp = sysctl -qw net.ipv4.conf.%%i.rp_filter=2" in awk


def test_uplink_gets_mss_clamp_next_to_its_masquerade(script):
    """Транзитный TCP из локальной сети в туннель без клампа виснет на больших
    ответах. Свойство аплинка, не локального слоя."""
    assert 'UPLINK_MSS="        oifname \\"$UPLINK_IF\\" tcp flags syn / syn,rst tcp option maxseg size set rt mtu"' in script
    body = script.split("GUARDEOF", 1)[1]
    fwd = body.split("chain forward", 1)[1].split("}", 1)[0]
    assert "$UPLINK_MSS" in fwd
    assert script.index('UPLINK_MSS=""') < script.index("cat <<GUARDEOF")


def test_peer_subnets_are_let_in_from_the_link_above_the_private_drop(script):
    """Доступ между подсетями за шлюзами: чужие подсети — по источнику, выше
    drop по приватным; набор из PEER_LAN_NETS бандла, закреплён в юните."""
    assert re.search(r'^PEER_LAN_NETS="\$\(printf .*tr -cd \'0-9\./ \'', script, re.M)
    assert 'PEER_ELEMS="$(ipv4_list $PEER_LAN_NETS)"' in script
    body = script.split("GUARDEOF", 1)[1]
    assert "set peer_nets4 {" in body
    fwd = script.split("chain forward {\n        type filter hook forward priority filter; policy accept;", 1)[1].split("}", 1)[0]
    assert fwd.index('ip saddr @peer_nets4 accept') < fwd.index('ip daddr @private4 drop')
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    assert 'Environment="PEER_LAN_NETS=$PEER_LAN_NETS"' in unit


def test_foreign_forward_drop_gets_accepts_for_our_interfaces(script):
    """Чужой файервол ставит политику FORWARD DROP в ip filter; accept в нашей inet-таблице
    её не отменяет — транзит локальной сети умирал бы молча. Правила в его цепочке,
    только когда политика DROP, снимаются при откате."""
    sec = script.split('step "3a. Чужая политика FORWARD"', 1)[1].split('step "4.', 1)[0]
    assert "grep -q '^-P FORWARD DROP'" in sec
    assert 'iptables -w -C FORWARD "$1" "$2" -j ACCEPT' in sec and "iptables -w -I FORWARD 1 $1 $2 -j ACCEPT" in sec
    assert '"$LINK_IF" "${UPLINK_IF:-}" "${LAN_IF_PRE:-}"' in sec
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "iptables -w -D FORWARD" in rollback
