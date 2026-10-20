#!/bin/bash
#
# reserve-zone-setup.sh — гостевая зона резерва (netns awg-guest) на ВПС.
#
# Зона — сетевое пространство, в котором живут стороны постоянных линков
# шлюзов основы (всегда, пока есть пара — «оболочка») и, только на время
# переезда, клон клиентского интерфейса основы, veth к хосту, dnsmasq зоны и
# бот основы в гостевом режиме. UDP-сокеты интерфейсов AmneziaWG остаются у
# хоста: интерфейс создаётся в пространстве хоста, переносится в зону и там
# переименовывается; порт слушает хост, расшифрованный пакет выходит в зоне.
# На этом стоит вся схема, поэтому --selftest проверяет это на модуле ядра
# хоста.
#
# Запуск: root (кроме --plan); секретное — только через stdin и в хранилище в
# памяти, в argv и на постоянный диск не попадает. Окружение:
#   RSV_ZONE=awg-guest        имя пространства (и пространства журнала гостя)
#   ZONE_CIDR=169.254.77.0/30 veth бота основы (хост .1, зона .2)
#   ZONE_RF_CIDR=169.254.77.4/30  veth РФ-трафика к шлюзам резерва (хост .5, зона .6)
#   INSTALL_DIR=/opt/awg-bot  где код (ExecStart юнита гостя)
#   RSV_RUN=/run/awg-bot-guest  хранилище гостя в памяти; RSV_STORE_SIZE=32M — его размер
#   RSV_VAR=/var/lib/awg-bot-guest  на диске только rep.bin (шифротекст под K)
#   RSV_STATE_DIR=/run/awg-bot/rsv  state.json, open.status, guest.sock
#   RSV_LINKS_DIR=/var/lib/awg-bot/reserve/links  конфиги сторон постоянных линков
#   RSV_SELFTEST_PORT=45999   порт пробы --selftest
# и по режимам (их кладёт обёртка awgbot/infra/rsvzone.py): --take — GUEST_IF
# (имя клона, awg0), GUEST_ADDR (адрес клона cidr, обязателен), GUEST_DNS
# (адреса резолвера зоны через пробел), GUEST_UPSTREAMS, GUEST_ADMIN (адреса
# устройств админа), GUEST_CHANNEL_PORT (гостевой канал с линков шлюзов в
# зону, 8787; бот передаёт его и при --shell — routing.link_channel_port
# резерва), RF_TABLE (таблица РФ-доступа хоста для трафика из awggr-h; 100 —
# умолчание routing.table, обёртка его не передаёт); --serve — WEBHOOK_PORT; --relay — RELAY_TO (адрес:порт основы).
#
# Таблица nft зоны — inet awg_guest (input/forward с политикой drop, цепочка
# zone_post — маскарад зоны наружу через veth). Файервол ХОСТА (порт клона,
# маскарад veth, пересылка из awggr-h в линки слотов, DNAT вебхука с набора
# tg_pool4 и обучение пула набором tg_seen4, пересылка хвоста в relay) этот
# скрипт НЕ пишет: его рисует бот по состоянию зоны (state.json) в таблице
# awg_bot_guard. Скрипт также НЕ переносит данные основы (хранилище гостя
# заполняет tools/rsvopen.py между --store и --take), НЕ меняет DNS и НЕ ставит
# вебхук Telegram — это делают процесс открытия и бот основы на резерве.
#
# Режимы:
#   --plan        без root: store=tmpfs-noswap|ramfs, cake=yes|no, netns=, awg=,
#                 webhook_port=<свободный из 443/80/88/8443|none>, zone=present|absent
#   --selftest    «сокет у хоста» и диапазон keepalive на установленном модуле;
#                 код 0 — обе гипотезы подтверждены, строки RESULT_*
#   --shell       зона-оболочка: пространство, lo, таблица nft зоны, стороны
#                 постоянных линков шлюзов основы из $LINKS_DIR/<имя>.conf
#                 (интерфейс создаётся у хоста, awg setconf, перенос в зону,
#                 имя awglink<N>, адрес из Address; уже стоящую сторону не
#                 трогает). Конфиги пишет бот резерва по `slots` основы и
#                 зовёт этот режим; порты сторон во вход хоста открывает
#                 файервол хоста (бот), не скрипт
#   --store       хранилище гостя в памяти ($RUN: env, conf, data, awg) — его
#                 заполняет процесс открытия до --take
#   --take        переезд: хранилище гостя в памяти ($RUN), veth ×2 (awggv —
#                 бот основы и клиенты наружу; awggr — РФ-трафик клиентов
#                 основы к шлюзам резерва: на awggr-h proxy ARP и
#                 `ip rule iif awggr-h lookup $RF_TABLE pref 150`; маршрут в
#                 awggr-n в зоне ставит сам бот основы (запасной слот),
#                 пересылку только в линки слотов и маскарад — файервол хоста),
#                 клон из stdin (GUEST_IF, GUEST_ADDR cidr, GUEST_DNS «адреса
#                 через пробел», GUEST_UPSTREAMS, GUEST_CHANNEL_PORT), dnsmasq
#                 зоны, юнит бота основы с песочницей и пространством журнала;
#                 бот стартует без права отвечать (его даёт бот резерва)
#   --serve       обслуживание: состояние serving (порт гостя для файервола
#                 хоста — его рисует бот), CAKE на внешнем интерфейсе
#   --restart-clone  клон вниз/вверх по конфигу в хранилище
#   --relay       возврат: бот основы и dnsmasq остановлены, клон, veth и
#                 правило iif awggr-h сняты, состояние relay (RELAY_TO=адрес:порт основы — правило
#                 пересылки рисует бот)
#   --teardown    остановить бота основы и dnsmasq зоны, снять клон, veth и
#                 правило iif awggr-h (без
#                 пересылки) — состояние idle; хранилище и rep.bin остаются
#   --wipe        хранилище гостя, rep.bin, журнал гостя — прочь; CAKE —
#                 прежняя дисциплина; зона остаётся оболочкой
#   --status      состояние строками key=value
set -u

ZONE="${RSV_ZONE:-awg-guest}"
NS="/run/netns/$ZONE"
RUN="${RSV_RUN:-/run/awg-bot-guest}"
VAR="${RSV_VAR:-/var/lib/awg-bot-guest}"
STATE_DIR="${RSV_STATE_DIR:-/run/awg-bot/rsv}"
STATE="$STATE_DIR/state.json"
LINKS_DIR="${RSV_LINKS_DIR:-/var/lib/awg-bot/reserve/links}"
INSTALL_DIR="${INSTALL_DIR:-/opt/awg-bot}"
ZONE_CIDR="${ZONE_CIDR:-169.254.77.0/30}"
ZONE_RF_CIDR="${ZONE_RF_CIDR:-169.254.77.4/30}"
VH="awggv-h"; VN="awggv-n"; RH="awggr-h"; RN="awggr-n"
UNIT="/etc/systemd/system/awg-bot-guest.service"
DNS_UNIT="/etc/systemd/system/awg-bot-guest-dns.service"
JOURNAL_CONF="/etc/systemd/journald@$ZONE.conf"
WEBHOOK_PORTS="443 80 88 8443"
STORE_SIZE="${RSV_STORE_SIZE:-32M}"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }
nsx() { ip netns exec "$ZONE" "$@"; }

cidr_host() {   # cidr_host CIDR N — N-й адрес /30 (1 или 2)
    local base="${1%/*}" last; last="${base##*.}"; printf '%s.%s' "${base%.*}" "$(( last + $2 ))"
}
HOST_V="$(cidr_host "$ZONE_CIDR" 1)"; ZONE_V="$(cidr_host "$ZONE_CIDR" 2)"
HOST_R="$(cidr_host "$ZONE_RF_CIDR" 1)"; ZONE_R="$(cidr_host "$ZONE_RF_CIDR" 2)"

kernel_ge() {
    local k; k="$(uname -r | cut -d- -f1)"
    local maj min; maj="${k%%.*}"; min="${k#*.}"; min="${min%%.*}"
    [ "$maj" -gt "$1" ] 2>/dev/null || { [ "$maj" -eq "$1" ] && [ "$min" -ge "$2" ]; } 2>/dev/null
}
port_free() { if command -v ss >/dev/null 2>&1; then ! ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; else return 0; fi; }
store_kind() { if kernel_ge 6 4; then printf 'tmpfs-noswap'; else printf 'ramfs'; fi; }
has_cake() { modinfo sch_cake >/dev/null 2>&1 || grep -qw sch_cake /proc/modules 2>/dev/null; }

# ── состояние для бота (без секретов) ───────────────────────────────────────
state_write() {   # state_write KEY=VALUE … — слить с прежним
    mkdir -p "$STATE_DIR"; chmod 0755 "$STATE_DIR"
    local tmp; tmp="$(mktemp)"
    python3 - "$STATE" "$tmp" "$@" <<'EOF' 2>/dev/null || cat > "$tmp" <<EOF2
import json, sys
path, out, *kv = sys.argv[1:]
try:
    data = json.load(open(path))
except Exception:
    data = {}
for item in kv:
    k, _, v = item.partition("=")
    data[k] = v
json.dump(data, open(out, "w"), ensure_ascii=False)
EOF
{"note": "python3 недоступен"}
EOF2
    mv "$tmp" "$STATE"; chmod 0644 "$STATE"
}
state_get() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$STATE" "$1" 2>/dev/null || true; }

# ── --plan ──────────────────────────────────────────────────────────────────
cmd_plan() {
    say "store=$(store_kind)"
    if has_cake; then say "cake=yes"; else say "cake=no"; fi
    if [ -d /proc/self/ns ] && [ -e /proc/self/ns/net ]; then say "netns=yes"; else say "netns=no"; fi
    if modinfo amneziawg >/dev/null 2>&1 || grep -qw amneziawg /proc/modules 2>/dev/null; then say "awg=yes"; else say "awg=no"; fi
    local p chosen=""
    for p in $WEBHOOK_PORTS; do if port_free "$p"; then chosen="$p"; break; fi; done
    say "webhook_port=${chosen:-none}"
    if [ -e "$NS" ]; then say "zone=present"; else say "zone=absent"; fi
}

# ── --selftest ──────────────────────────────────────────────────────────────
ST_ZONE="awgrsvtst-z"; ST_CLI="awgrsvtst-c"; ST_VH="awgrsvtv0"; ST_VC="awgrsvtv1"; ST_IF="awgrsvtst"
ST_PORT="${RSV_SELFTEST_PORT:-45999}"
st_cleanup() { ip netns del "$ST_ZONE" 2>/dev/null; ip netns del "$ST_CLI" 2>/dev/null; ip link del "$ST_VH" 2>/dev/null; ip link del "$ST_IF" 2>/dev/null; }
cmd_selftest() {
    need_root
    command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools (awg)"
    modprobe amneziawg 2>/dev/null || true
    trap st_cleanup EXIT
    st_cleanup
    local sp spub cp cpub ok_ka=0 ok_sock=0 ok_ping=0
    ip netns add "$ST_ZONE" || die "пространство не создалось"
    ip netns add "$ST_CLI" || die "пространство не создалось"
    ip link add "$ST_VH" type veth peer name "$ST_VC" || die "veth не создался"
    ip link set "$ST_VC" netns "$ST_CLI"
    ip addr add 192.0.2.1/30 dev "$ST_VH"; ip link set "$ST_VH" up
    ip netns exec "$ST_CLI" ip addr add 192.0.2.2/30 dev "$ST_VC"
    ip netns exec "$ST_CLI" ip link set "$ST_VC" up
    ip netns exec "$ST_CLI" ip link set lo up
    sp="$(awg genkey)"; spub="$(printf '%s' "$sp" | awg pubkey)"
    cp="$(awg genkey)"; cpub="$(printf '%s' "$cp" | awg pubkey)"
    ip link add "$ST_IF" type amneziawg || die "интерфейс amneziawg не создался"
    printf '%s' "$sp" | awg set "$ST_IF" private-key /dev/stdin listen-port "$ST_PORT" peer "$cpub" allowed-ips 10.255.77.2/32 \
        || die "awg set на интерфейсе хоста не прошёл"
    ip link set "$ST_IF" netns "$ST_ZONE" || die "перенос интерфейса в пространство не прошёл"
    ip netns exec "$ST_ZONE" ip link set "$ST_IF" name awg0
    ip netns exec "$ST_ZONE" ip addr add 10.255.77.1/30 dev awg0
    ip netns exec "$ST_ZONE" ip link set awg0 up
    ip netns exec "$ST_CLI" ip link add awgc type amneziawg
    if printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25-35 2>/dev/null; then ok_ka=1
    else
        printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25
    fi
    ip netns exec "$ST_CLI" ip addr add 10.255.77.2/30 dev awgc
    ip netns exec "$ST_CLI" ip link set awgc up
    ip netns exec "$ST_CLI" ping -c1 -W3 10.255.77.1 >/dev/null 2>&1 && ok_ping=1
    if ip netns exec "$ST_ZONE" awg show awg0 latest-handshakes 2>/dev/null | awk '{ if ($2 > 0) found = 1 } END { exit !found }'; then ok_sock=1; fi
    say "RESULT_SOCKET_AT_HOST=$ok_sock"; say "RESULT_KEEPALIVE_RANGE=$ok_ka"; say "RESULT_TUNNEL_PING=$ok_ping"
    [ "$ok_sock" -eq 1 ] && [ "$ok_ka" -eq 1 ]
}

# ── зона-оболочка ───────────────────────────────────────────────────────────
zone_nft() {   # таблица зоны: вход — только канал с линков, DNS и ICMP с клона; пересылка — см. §3.2
    local chan="${GUEST_CHANNEL_PORT:-8787}" iface="${1:-}"
    nsx nft -f - <<EOF
table inet awg_guest {
    set admin4 { type ipv4_addr; }
    set private4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } }
    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop
        ip protocol icmp accept
        iifname "awglink*" tcp dport $chan accept
        iifname "awglink*" drop
$( [ -n "$iface" ] && printf '        iifname "%s" udp dport 53 accept\n        iifname "%s" tcp dport 53 accept\n' "$iface" "$iface" )
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        ct state invalid drop
$( [ -n "$iface" ] && cat <<EOF2
        iifname "$iface" oifname "$iface" ip saddr @admin4 accept
        iifname "$iface" oifname "$iface" drop
        iifname "$iface" ip daddr @private4 oifname "$VN" drop
        iifname "$iface" oifname { "$VN", "$RN", "awglink*" } accept
EOF2
)
$( [ -n "$iface" ] && printf '        iifname "awglink*" oifname "%s" ct state established,related accept\n' "$iface" )
        iifname "awglink*" drop
    }
    chain zone_post {
        type nat hook postrouting priority srcnat; policy accept;
        oifname { "$VN", "$RN" } masquerade
    }
}
EOF
}

link_sides_up() {   # стороны постоянных линков шлюзов основы из $LINKS_DIR/<linkname>.conf
    local f name tmp addr
    for f in "$LINKS_DIR"/*.conf; do
        [ -f "$f" ] || continue
        name="$(basename "$f" .conf)"
        nsx ip link show "$name" >/dev/null 2>&1 && continue
        tmp="awgz$$"
        ip link add "$tmp" type amneziawg || { say "ВНИМАНИЕ: сторона линка $name не создалась"; continue; }
        awg setconf "$tmp" "$f" || { ip link del "$tmp"; say "ВНИМАНИЕ: конфиг стороны линка $name не принят"; continue; }
        ip link set "$tmp" netns "$ZONE"
        nsx ip link set "$tmp" name "$name"
        addr="$(awk -F'[ =]+' '/^Address/{print $2; exit}' "$f")"
        [ -n "$addr" ] && nsx ip addr add "$addr" dev "$name" 2>/dev/null
        nsx ip link set "$name" up
    done
}

cmd_shell() {
    need_root
    [ -e "$NS" ] || ip netns add "$ZONE" || die "пространство $ZONE не создалось"
    nsx ip link set lo up
    nsx sysctl -qw net.ipv4.ip_forward=1 2>/dev/null || true
    mkdir -p "$LINKS_DIR"; chmod 0700 "$LINKS_DIR"
    zone_nft "$(state_get iface)"
    link_sides_up
    state_write zone=present
    say "shell: ok"
}

# ── хранилище гостя в памяти ────────────────────────────────────────────────
store_mount() {
    mkdir -p "$RUN"; chmod 0700 "$RUN"
    mountpoint -q "$RUN" && return 0
    if [ "$(store_kind)" = "tmpfs-noswap" ]; then
        mount -t tmpfs -o "size=$STORE_SIZE,mode=0700,noswap" tmpfs "$RUN" 2>/dev/null \
            || mount -t tmpfs -o "size=$STORE_SIZE,mode=0700" tmpfs "$RUN" || die "хранилище гостя не смонтировалось"
    else
        mount -t ramfs -o mode=0700 ramfs "$RUN" || die "хранилище гостя (ramfs) не смонтировалось"
    fi
    mkdir -p "$RUN/data" "$RUN/conf" "$RUN/awg"; chmod 0700 "$RUN/data" "$RUN/conf" "$RUN/awg"
}

# ── юниты гостя ─────────────────────────────────────────────────────────────
write_journal_conf() {
    cat > "$JOURNAL_CONF" <<EOF
# awg-bot: журнал бота основы на резерве — только в памяти
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
}

write_units() {   # write_units GUEST_IF
    cat > "$DNS_UNIT" <<EOF
[Unit]
Description=awg-bot: резолвер гостевой зоны резерва
After=network-online.target

[Service]
Type=simple
NetworkNamespacePath=$NS
ExecStart=/usr/sbin/dnsmasq -k --conf-file=$RUN/dnsmasq.conf
Restart=on-failure
RestartSec=3
EOF
    cat > "$UNIT" <<EOF
[Unit]
Description=awg-bot: бот основы на резерве (гостевой режим)
After=network-online.target awg-bot-guest-dns.service
Wants=awg-bot-guest-dns.service

[Service]
Type=simple
NetworkNamespacePath=$NS
EnvironmentFile=$RUN/env
Environment=AWG_BOT_CONF_DIR=$RUN/conf
Environment=AWG_BOT_DATA_DIR=$RUN/data
Environment=AWG_BOT_ENV=$RUN/env
Environment=AWG_BOT_GUEST=1
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m awgbot
Restart=on-failure
RestartSec=5
TemporaryFileSystem=/etc/awg-bot:ro /var/lib/awg-bot:ro /root:ro /home:ro
InaccessiblePaths=-/etc/amnezia -/etc/awg-gw -/var/backups
ReadWritePaths=$RUN $VAR
BindPaths=-$STATE_DIR/guest.sock
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
LogNamespace=$ZONE
MemorySwapMax=0
MemoryHigh=128M
MemoryMax=160M
LimitCORE=0

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

write_dnsmasq() {   # write_dnsmasq "адреса" "апстримы"
    {
        say "# awg-bot: резолвер гостевой зоны резерва"
        say "no-resolv"; say "no-hosts"; say "bind-interfaces"; say "cache-size=1000"
        local a
        for a in $1; do say "listen-address=$a"; done
        for a in $2; do say "server=$a"; done
        say "conf-file=$RUN/dnsmasq-rf.conf"
    } > "$RUN/dnsmasq.conf"
    [ -f "$RUN/dnsmasq-rf.conf" ] || : > "$RUN/dnsmasq-rf.conf"
}

write_setconf() {   # write_setconf IFACE — конфиг для awg setconf: без расширений awg-quick (Address, MTU, DNS, Table)
    grep -v -E '^[[:space:]]*(Address|MTU|DNS|Table)[[:space:]]*=' "$RUN/awg/$1.conf" > "$RUN/awg/$1.setconf"
    chmod 0600 "$RUN/awg/$1.setconf"
}

# ── --take ──────────────────────────────────────────────────────────────────
cmd_take() {
    need_root
    [ -e "$NS" ] || die "зоны-оболочки нет — сначала --shell"
    local iface="${GUEST_IF:-awg0}" addr="${GUEST_ADDR:-}" dns="${GUEST_DNS:-}" ups="${GUEST_UPSTREAMS:-}"
    [ -n "$addr" ] || die "нет GUEST_ADDR"
    store_mount
    # конфиг клона — только из stdin в хранилище в памяти
    cat > "$RUN/awg/$iface.conf"; chmod 0600 "$RUN/awg/$iface.conf"
    grep -q '^PrivateKey' "$RUN/awg/$iface.conf" || die "в конфиге клона нет ключа"
    # для awg setconf — без расширений awg-quick (Address, MTU, DNS, Table): их setconf не знает
    grep -v -E '^[[:space:]]*(Address|MTU|DNS|Table)[[:space:]]*=' "$RUN/awg/$iface.conf" > "$RUN/awg/$iface.setconf"
    chmod 0600 "$RUN/awg/$iface.setconf"
    # veth: бот основы и клиенты наружу (NAT хоста) и РФ-трафик к шлюзам резерва
    ip link show "$VH" >/dev/null 2>&1 || { ip link add "$VH" type veth peer name "$VN"; ip link set "$VN" netns "$ZONE"; }
    ip link show "$RH" >/dev/null 2>&1 || { ip link add "$RH" type veth peer name "$RN"; ip link set "$RN" netns "$ZONE"; }
    ip addr replace "$HOST_V/30" dev "$VH"; ip link set "$VH" up
    ip addr replace "$HOST_R/30" dev "$RH"; ip link set "$RH" up
    nsx ip addr replace "$ZONE_V/30" dev "$VN"; nsx ip link set "$VN" up
    nsx ip addr replace "$ZONE_R/30" dev "$RN"; nsx ip link set "$RN" up
    nsx ip route replace default via "$HOST_V" dev "$VN"
    # РФ-трафик из зоны: запасной слот бота основы — `default dev awggr-n`; хост
    # отвечает на ARP за любой адрес (proxy ARP) и ведёт пакеты в таблицу своего
    # РФ-доступа (линк активного слота), маскарад — в таблице хоста
    sysctl -qw "net.ipv4.conf.$RH.proxy_arp=1" 2>/dev/null || true
    ip rule show | grep -q "iif $RH lookup ${RF_TABLE:-100}" || ip rule add iif "$RH" lookup "${RF_TABLE:-100}" pref 150
    # клон: создаётся у хоста (сокет — у хоста), переносится в зону под именем основы
    local tmp="awgc$$"
    nsx ip link show "$iface" >/dev/null 2>&1 && { nsx ip link del "$iface" 2>/dev/null || true; }
    ip link add "$tmp" type amneziawg || die "клон не создался"
    awg setconf "$tmp" "$RUN/awg/$iface.setconf" || { ip link del "$tmp"; die "конфиг клона не принят"; }
    ip link set "$tmp" netns "$ZONE" || { ip link del "$tmp"; die "клон не перенёсся в зону"; }
    nsx ip link set "$tmp" name "$iface"
    nsx ip addr replace "$addr" dev "$iface"
    local mtu; mtu="$(awk -F'=' '/^[[:space:]]*MTU[[:space:]]*=/{gsub(/ /,"",$2); print $2; exit}' "$RUN/awg/$iface.conf")"
    [ -n "$mtu" ] && nsx ip link set "$iface" mtu "$mtu"
    nsx ip link set "$iface" up
    zone_nft "$iface"
    local a
    for a in ${GUEST_ADMIN:-}; do nsx nft add element inet awg_guest admin4 "{ $a }" 2>/dev/null || true; done
    [ -n "$dns" ] || dns="${addr%/*}"
    for a in $dns; do [ "$a" = "${addr%/*}" ] || nsx ip addr replace "$a/32" dev lo; done
    write_dnsmasq "$dns" "$ups"
    write_journal_conf
    write_units "$iface"
    mkdir -p "$VAR"; chmod 0700 "$VAR"
    mkdir -p "$STATE_DIR"
    systemctl restart awg-bot-guest-dns.service || say "ВНИМАНИЕ: dnsmasq зоны не поднялся"
    [ -f "$RUN/env" ] && systemctl restart awg-bot-guest.service || say "ВНИМАНИЕ: env бота основы ещё не положен — юнит не запущен"
    state_write zone=present phase=taking iface="$iface" port="$(awk '/^ListenPort/{print $3; exit}' "$RUN/awg/$iface.conf")" \
        zone_cidr="$ZONE_CIDR" zone_rf_cidr="$ZONE_RF_CIDR" taken_at="$(date -Iseconds)"
    say "take: ok"
}

cmd_restart_clone() {
    need_root
    local iface; iface="$(state_get iface)"; [ -n "$iface" ] || die "клона нет"
    nsx ip link set "$iface" down
    write_setconf "$iface"
    nsx awg setconf "$iface" "$RUN/awg/$iface.setconf" || die "конфиг клона не принят"
    nsx ip link set "$iface" up
    say "clone: restarted"
}

# ── --serve ─────────────────────────────────────────────────────────────────
wan_if() { ip -j route show default 2>/dev/null | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["dev"] if r else "")' 2>/dev/null || true; }
cake_on() {
    has_cake || { state_write cake=absent; return 0; }
    local wan; wan="$(wan_if)"; [ -n "$wan" ] || return 0
    local prev; prev="$(tc qdisc show dev "$wan" 2>/dev/null | head -n1 | awk '{print $2}')"
    tc qdisc replace dev "$wan" root cake besteffort dual-dsthost nat 2>/dev/null && state_write cake=on cake_dev="$wan" cake_prev="${prev:-none}"
}
cake_off() {
    local dev prev; dev="$(state_get cake_dev)"; prev="$(state_get cake_prev)"
    [ -n "$dev" ] || return 0
    if [ -n "$prev" ] && [ "$prev" != "none" ] && [ "$prev" != "cake" ]; then tc qdisc replace dev "$dev" root "$prev" 2>/dev/null || tc qdisc del dev "$dev" root 2>/dev/null
    else tc qdisc del dev "$dev" root 2>/dev/null; fi
    state_write cake=off cake_dev= cake_prev=
}
cmd_serve() {
    need_root
    [ -n "$(state_get iface)" ] || die "клона нет — сначала --take"
    cake_on
    state_write phase=serving serving_at="$(date -Iseconds)" webhook_port="${WEBHOOK_PORT:-}"
    say "serve: ok"
}

# ── снятие ──────────────────────────────────────────────────────────────────
guest_stop() {
    systemctl stop awg-bot-guest.service 2>/dev/null || true
    systemctl stop awg-bot-guest-dns.service 2>/dev/null || true
    local iface; iface="$(state_get iface)"
    [ -n "$iface" ] && nsx ip link del "$iface" 2>/dev/null
    while ip rule del iif "$RH" lookup "${RF_TABLE:-100}" 2>/dev/null; do :; done
    ip link del "$VH" 2>/dev/null; ip link del "$RH" 2>/dev/null
    zone_nft ""
}
cmd_relay() {
    need_root
    guest_stop
    state_write phase=relay relay_to="${RELAY_TO:-}" relay_at="$(date -Iseconds)" iface=
    say "relay: ok"
}
cmd_teardown() {
    need_root
    guest_stop
    state_write phase=idle iface= relay_to= webhook_port=
    say "teardown: ok"
}
cmd_wipe() {
    need_root
    guest_stop
    if mountpoint -q "$RUN"; then umount -l "$RUN" 2>/dev/null || true; fi
    rm -rf "$RUN" "$VAR"
    rm -rf /run/log/journal/*."$ZONE" /var/log/journal/*."$ZONE" 2>/dev/null
    rm -f "$UNIT" "$DNS_UNIT"; systemctl daemon-reload 2>/dev/null || true
    cake_off
    state_write phase=idle iface= relay_to= webhook_port= wiped_at="$(date -Iseconds)"
    say "wipe: ok"
}
cmd_status() {
    if [ -e "$NS" ]; then say "zone=present"; else say "zone=absent"; fi
    say "phase=$(state_get phase)"; say "iface=$(state_get iface)"
    if mountpoint -q "$RUN" 2>/dev/null; then say "store=mounted"; else say "store=absent"; fi
    systemctl is-active awg-bot-guest.service >/dev/null 2>&1 && say "guest=active" || say "guest=inactive"
    [ -f "$VAR/rep.bin" ] && say "rep=present" || say "rep=absent"
}

case "${1:-}" in
    --plan) cmd_plan ;;
    --selftest) cmd_selftest ;;
    --shell) cmd_shell ;;
    --store) need_root; store_mount; say "store: $(store_kind)" ;;
    --take) cmd_take ;;
    --serve) cmd_serve ;;
    --restart-clone) cmd_restart_clone ;;
    --relay) cmd_relay ;;
    --teardown) cmd_teardown ;;
    --wipe) cmd_wipe ;;
    --status) cmd_status ;;
    *) say "использование: $0 --plan|--selftest|--shell|--store|--take|--serve|--restart-clone|--relay|--teardown|--wipe|--status"; exit 2 ;;
esac
