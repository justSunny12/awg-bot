#!/bin/bash
# rtlab.sh — РФ-доступ на паре серверов стенда для s08_rf_transit; подключается сценарием после
# pairlab.sh и quolab.sh (`. rtlab.sh`), сам не запускается.
#
# У каждого сервера — слот 1 с настоящим линком AWG `awglink` (10.99.99.0/30, сторона сервера с
# `Table = off`, порт 443) до своего шлюза: A («Alpha») ↔ G1, B («Beta») ↔ GR. Шлюз стенда — не
# агент: сторона линка, маршрут до подсети пользователей сервера в линк и маскарад в свой аплинк
# (`table ip labgw`), как обвязка шлюза. «РФ-сайт» (tests/lab/rfsite.py в lab-rf) — на 192.0.2.10
# (адрес в наборе пользователей вместо домена списка: dnsmasq стенд не ведёт) и 192.0.2.20 (не в
# наборе — мимо шлюза); адреса не из частных сетей: транзит и учёт частные назначения не пропускают.
# Маршрутизатор стенда их не маскарадит — сайт видит адрес того, кто пришёл: G1 (10.1.1.2), GR
# (10.1.3.2) или сервер A (203.0.113.5). Пользователи — cA (10.8.1.5 на awg0 A) и cB (10.8.2.5 на
# awg0 B; пир — в конфиге awg0 сервера с PSK устройства из базы): интерфейс `awgc`, в туннель — подсеть сервера, 192.0.2.0/24, линк пары, сеть GR.
# Цифры живости сжаты: зонд 5 с без простоя, окно 6 (недоступен с 3-й неудачи), резервный зонд —
# каждый такт, 1 мин между автоматическими переключениями.
#
#   rt_init                       pl_init с РФ-доступом обоих узлов (до pl_start)
#   rt_up                         rt_init, оба узла с поллингом, пара на связи
#   rt_q <узел> <выражение>       pl_q над блоком rt статуса узла (r — s["rt"])
#   rt_wait <узел> <выражение> <срок> <что>   wait_t над r; секунд с T0 — в WT
#   rt_get <пользователь> <адрес> [путь]      HTTP от пользователя к сайту, на stdout — тело (таймаут 5 с)
#   rt_seen <адрес> <с ts>        какие src видел сайт на адресе с момента ts (через запятую)
#   rt_cb <узел> <действие RsvCB> нажатие кнопки «🛟 Резерв» админом узла (через макет Bot API)
#   rt_cmd <узел> <команда> [json]           pl_cmd
#   rt_cleanup                    снять всё РФ-доступа (зовёт pl_cleanup_quiet)
RT_RF_SITE=192.0.2.10
RT_RF_OTHER=192.0.2.20
declare -A RT_GW=([A]=G1 [B]=GR)
declare -A RT_GW_ADDR=([A]=10.1.1.2 [B]=10.1.3.2)
declare -A RT_PUB=([A]=203.0.113.5 [B]=198.51.100.7)
declare -A RT_SUB=([A]=10.8.1 [B]=10.8.2)
declare -A RT_USER=([A]=cA [B]=cB)
declare -A RT_PORT=([A]=45871 [B]=45872)
declare -A RT_TOKEN=([A]="$TOKEN_A" [B]="$TOKEN_B")
declare -A RT_ADMIN=([A]="$ADMIN_A" [B]="$ADMIN_B")
RT_OBF='Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 66
H1 = 5-500
H2 = 600-700
H3 = 800-900
H4 = 1000-1100'
PL_RF_PID=""

rt_conf() {
    local n="$1"
    pl_conf "$n" routing.enabled true
    pl_conf "$n" routing.gw_interface '"awglink"'
    pl_conf "$n" routing.probe_targets "[\"$RT_RF_SITE\"]"
    pl_conf "$n" routing.probe_port 80
    pl_conf "$n" routing.probe_seconds 5
    pl_conf "$n" routing.probe_idle_multiplier 0
    pl_conf "$n" routing.failover.window_samples 6
    pl_conf "$n" routing.failover.min_interval_minutes 1
    pl_conf "$n" routing.failover.standby_probe_minutes 0
    pl_conf "$n" reserve.rf_via_mine true
}

rt_link() {        # линк слота 1: сторона сервера (Table = off) и сторона шлюза с маскарадом в аплинк
    local n="$1" g="${RT_GW[$1]}" sp gp su gu ns="${PL_NS[$1]}"
    sp="$(awg genkey)"; gp="$(awg genkey)"; su="$(printf '%s' "$sp" | awg pubkey)"; gu="$(printf '%s' "$gp" | awg pubkey)"
    cat > "$PL/$n/awg/awglink.conf" <<EOF
# Линк до шлюза (слот 1) — стенд
[Interface]
Address = 10.99.99.1/30
ListenPort = 443
PrivateKey = $sp
Table = off
$RT_OBF

[Peer]
PublicKey = $gu
AllowedIPs = 0.0.0.0/0
EOF
    chmod 600 "$PL/$n/awg/awglink.conf"
    ip -n "$ns" link del awglink 2>/dev/null
    ip -n "$ns" link add awglink type amneziawg || pl_fail "awglink в $ns не создан"
    ip netns exec "$ns" awg setconf awglink <(pl_awg_conf "$PL/$n/awg/awglink.conf") || pl_fail "awg setconf awglink в $ns"
    ip -n "$ns" addr add 10.99.99.1/30 dev awglink
    ip -n "$ns" link set awglink up
    # обвязка хоста (routing-host-setup.sh): rp_filter не strict — ответы из линков приходят с чужими адресами
    ip netns exec "$ns" sysctl -qw net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2
    mkdir -p "$PL/$g"
    cat > "$PL/$g/awglink.conf" <<EOF
[Interface]
PrivateKey = $gp
$RT_OBF

[Peer]
PublicKey = $su
AllowedIPs = 10.99.99.1/32, ${RT_SUB[$n]}.0/24
Endpoint = ${RT_PUB[$n]}:443
PersistentKeepalive = 5
EOF
    ip -n "lab-$g" link del awglink 2>/dev/null
    ip -n "lab-$g" link add awglink type amneziawg || pl_fail "awglink в lab-$g не создан"
    ip netns exec "lab-$g" awg setconf awglink "$PL/$g/awglink.conf" || pl_fail "awg setconf awglink в lab-$g"
    ip -n "lab-$g" addr add 10.99.99.2/30 dev awglink
    ip -n "lab-$g" link set awglink up
    ip -n "lab-$g" route replace "${RT_SUB[$n]}.0/24" dev awglink
    ip netns exec "lab-$g" sysctl -qw net.ipv4.ip_forward=1
    ip netns exec "lab-$g" nft -f - <<EOF
table ip labgw
delete table ip labgw
table ip labgw {
    chain post { type nat hook postrouting priority srcnat; policy accept; oifname "v-$g" masquerade; }
}
EOF
}

rt_user() {        # пользователь сервера: свой AWG до awg0, пир — в конфиге awg0 и в ядре
    local n="$1" c="${RT_USER[$1]}" cp cu spub
    cp="$(awg genkey)"; cu="$(printf '%s' "$cp" | awg pubkey)"
    spub="$(sed -n 's/^PrivateKey *= *//p' "$PL/$n/awg/awg0.conf" | head -n1 | awg pubkey)"
    mkdir -p "$PL/$c"
    printf '%s' "$cu" > "$PL/$c/pub"
    awg genpsk > "$PL/$c/psk"
    # пир устройства — в конфиге awg0 сервера, как его пишет бот (правки конфига ботом — syncconf: пир только
    # в ядре снялся бы первой же записью пира-зонда кворума)
    printf '\n[Peer]\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s.5/32\n' "$cu" "$(cat "$PL/$c/psk")" "${RT_SUB[$n]}" \
        >> "$PL/$n/awg/awg0.conf"
    ip netns exec "${PL_NS[$n]}" awg set awg0 peer "$cu" preshared-key "$PL/$c/psk" allowed-ips "${RT_SUB[$n]}.5/32" \
        || pl_fail "пир пользователя на awg0 $n не заведён"
    cat > "$PL/$c/awgc.conf" <<EOF
[Interface]
PrivateKey = $cp
$RT_OBF

[Peer]
PublicKey = $spub
PresharedKey = $(cat "$PL/$c/psk")
AllowedIPs = ${RT_SUB[$n]}.0/24, 192.0.2.0/24, 10.99.98.0/30, 10.1.3.0/24
Endpoint = ${RT_PUB[$n]}:${RT_PORT[$n]}
PersistentKeepalive = 5
EOF
    ip -n "lab-$c" link del awgc 2>/dev/null
    ip -n "lab-$c" link add awgc type amneziawg || pl_fail "awgc в lab-$c не создан"
    ip netns exec "lab-$c" awg setconf awgc "$PL/$c/awgc.conf" || pl_fail "awg setconf awgc в lab-$c"
    ip -n "lab-$c" addr add "${RT_SUB[$n]}.5/32" dev awgc
    ip -n "lab-$c" link set awgc up
    local r
    for r in "${RT_SUB[$n]}.0/24" 192.0.2.0/24 10.99.98.0/30 10.1.3.0/24; do ip -n "lab-$c" route replace "$r" dev awgc; done
}

rt_init() {
    export LAB_RT=1 LAB_RF_SET="$RT_RF_SITE"
    pl_init
    local n
    for n in A B; do
        rt_conf "$n"
        rt_link "$n"
        rt_user "$n"
        pl_py "$n" -m tests.lab.pairnode rtseed --user-pub "$(cat "$PL/${RT_USER[$n]}/pub")" --user-addr "${RT_SUB[$n]}.5" --user-psk "$(cat "$PL/${RT_USER[$n]}/psk")" \
            >"$PL/$n/rtseed.log" 2>&1 || pl_fail "РФ-доступ узла $n не засеян: $(tail -n5 "$PL/$n/rtseed.log")"
    done
    ip -n lab-rf addr replace "$RT_RF_SITE/32" dev lo
    ip -n lab-rf addr replace "$RT_RF_OTHER/32" dev lo
    ip -n lab-inet route replace 192.0.2.0/24 via 10.9.0.2
    ip netns exec lab-inet nft list chain ip lab post 2>/dev/null | grep -q 'ip daddr 192.0.2.0/24 return' \
        || ip netns exec lab-inet nft insert rule ip lab post ip daddr 192.0.2.0/24 return
    : > "$PL/rf.jsonl"
    ip netns exec lab-rf env PYTHONPATH="$PL_ROOT" "$PY" -m tests.lab.rfsite --port 80 --journal "$PL/rf.jsonl" \
        >"$PL/rf.log" 2>&1 & PL_RF_PID=$!
    disown "$PL_RF_PID"
}

rt_up() {
    rt_init
    pl_start A --poll
    pl_start B --poll
    pl_wait A 's.get("jrn_on") and s.get("online")' 120 "пара на связи" || exit 1
}

rt_q() { pl_q "$1" "(lambda r: $2)(s.get('rt') or {})"; }

rt_wait() {
    local i
    for i in $(seq 1 "$3"); do
        if [ "$(rt_q "$1" "bool($2)")" = "true" ]; then WT=$(since "$T0"); return 0; fi
        sleep 1
    done
    printf 'не дождался за %s с на %s: %s\n' "$3" "$1" "$4"
    printf 'rt %s: %s\n' "$1" "$(pl_q "$1" 's.get("rt")' | cut -c1-2500)"
    printf 'журнал %s:\n%s\n' "$1" "$(grep -E 'routing|транзит|РФ-доступ|rfpeer' "$PL/$1/node.log" | tail -n 25)"
    return 1
}

rt_get() {         # rt_get <пользователь> <адрес> [путь] — тело ответа сайта или пусто
    ip netns exec "lab-$1" curl -s -m 5 "http://$2${3:-/ip}" 2>/dev/null
}

rt_seen() {        # rt_seen <адрес назначения> <с ts> — источники запросов /ip к сайту (уникальные, по порядку)
    "$PY" -c 'import json,sys
dst, since = sys.argv[2], float(sys.argv[3])
out = []
for l in open(sys.argv[1], encoding="utf-8"):
    try:
        r = json.loads(l)
    except ValueError:
        continue
    if r["dst"] == dst and r["ts"] >= since and r["path"] == "/ip" and r["src"] not in out:
        out.append(r["src"])
print(",".join(out))' "$PL/rf.jsonl" "$1" "$2"
}

rt_cb() {          # rt_cb <узел> <действие> — callback_query от админа узла с данными RsvCB
    local n="$1" data
    data="$(cd "$PL_ROOT" && "$PY" -c 'import sys; from awgbot.bot.callbacks import RsvCB; print(RsvCB(action=sys.argv[1]).pack())' "$2")"
    local adm="${RT_ADMIN[$n]}"
    ip netns exec "${PL_NS[$n]}" curl -s -m5 -X POST "http://${PL_GW[$n]}:8081/inject/${RT_TOKEN[$n]}" \
        -H 'Content-Type: application/json' -d "{\"callback_query\": {\"id\": \"$RANDOM\", \"chat_instance\": \"1\", \"data\": \"$data\",
        \"from\": {\"id\": $adm, \"is_bot\": false, \"first_name\": \"Админ\"},
        \"message\": {\"message_id\": 77, \"date\": 1, \"chat\": {\"id\": $adm, \"type\": \"private\"},
                      \"from\": {\"id\": ${RT_TOKEN[$n]%%:*}, \"is_bot\": true, \"first_name\": \"bot\"}, \"text\": \"🛟 Резерв\"}}}" >/dev/null
}

rt_tg() {          # rt_tg <узел-бот> <подстрока> [с ts] [critical|silent] — сколько sendMessage админу узла
    "$PY" -c 'import json,sys
path, bot, chat, sub = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
since = float(sys.argv[5]); kind = sys.argv[6]
n = 0
for l in open(path, encoding="utf-8"):
    if not l.strip():
        continue
    r = json.loads(l)
    p = r["params"]
    if r["method"] != "sendMessage" or r["bot"] != bot or r["ts"] < since or str(p.get("chat_id")) != chat:
        continue
    if sub not in str(p.get("text")):
        continue
    silent = str(p.get("disable_notification")).lower() in ("true", "1")
    if kind == "critical" and silent or kind == "silent" and not silent:
        continue
    n += 1
print(n)' "$PL/botapi.jsonl" "${RT_TOKEN[$1]%%:*}" "${RT_ADMIN[$1]}" "$2" "${3:-0}" "${4:-any}"
}

rt_cleanup() {
    local n
    [ -n "$PL_RF_PID" ] && kill -9 "$PL_RF_PID" 2>/dev/null; PL_RF_PID=""
    pkill -9 -f "[r]fsite --port" 2>/dev/null
    for n in A B G1 GR; do ip -n "lab-$n" link del awglink 2>/dev/null; done
    for n in cA cB; do ip -n "lab-$n" link del awgc 2>/dev/null; done
    for n in G1 GR; do ip netns exec "lab-$n" nft delete table ip labgw 2>/dev/null; done
    for n in A B; do                                         # таблицы, правила, наборы и цепочка РФ-доступа узла
        ip netns exec "lab-$n" nft delete table inet awg_bot_guard 2>/dev/null
        ip netns exec "lab-$n" nft delete table inet awg_bot_acct 2>/dev/null
        while ip netns exec "lab-$n" ip rule del pref 151 2>/dev/null; do :; done
        ip netns exec "lab-$n" ip rule show | awk -F: '/fwmark/{print $1}' | while read -r p; do
            ip netns exec "lab-$n" ip rule del pref "$p" 2>/dev/null; done
        for t in 100 101 102 129; do ip -n "lab-$n" route flush table "$t" 2>/dev/null; done
        ip netns exec "lab-$n" iptables -t mangle -F 2>/dev/null; ip netns exec "lab-$n" iptables -t mangle -X 2>/dev/null
        ip netns exec "lab-$n" ipset destroy 2>/dev/null
    done
}
