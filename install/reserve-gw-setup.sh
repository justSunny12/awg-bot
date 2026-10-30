#!/bin/sh
#
# reserve-gw-setup.sh — постоянный линк шлюза к резерву (awgrsv) и его
# обвязка на шлюзе условной маршрутизации.
#
# Линк живёт всегда, пока у сервера AWG есть пара: по нему идёт только
# гостевой канал (шлюз — клиент TCP к стороне зоны резерва) и, во время
# переезда пользователей на резерв, их РФ-трафик из зоны резерва наружу через
# этот шлюз. Своя таблица nft `inet awg_rsv_guard` (не трогает таблицу
# обвязки awg_gw_guard — она собирается отдельно и целиком):
#   input:    с awgrsv — только ответы на соединения самого шлюза и, при
#             открытой пересылке, SSH админов сервера с их адресов; остальное
#             с awgrsv — drop (срабатывает раньше правил обвязки: приоритет
#             filter - 1)
#   forward:  с awgrsv — только источники из набора rsv_open4 (пока
#             пересылка открыта: агент кладёт туда все AllowedIPs стороны
#             зоны — её адрес и подсети туннеля сервера) и только наружу
#             (не в частные сети); ответы — в awgrsv; пустой набор —
#             пересылка закрыта
#   prerouting (mangle): соединения, пришедшие с awgrsv, помечаются
#             connmark 0x52; ответы по connmark уходят обратно в awgrsv
#             через таблицу маршрутов 152 (ip rule fwmark 0x52 lookup 152)
#   output (route, mangle): то же для ответов самого шлюза (SSH админа
#             через зону) — пакет с connmark 0x52 получает метку
#   postrouting (srcnat): адрес стороны зоны (первый AllowedIPs пира в
#             конфиге) наружу — masquerade: с него идут зонды живости бота
#             основы на резерве, обвязка его не знает
#   при tg=1 в RSV_STATE (--tg on) ещё набор tg_nets4 (диапазоны Telegram,
#             те же, что TG_NETS обвязки), цепочка rsv_tgout (output, type
#             route) с меткой TG_MARK сокетам процесса TG_CGROUP к набору и
#             rsv_tgpost (srcnat) — masquerade в awgrsv по метке; правило
#             ip rule fwmark TG_MARK lookup 152 ставят --tg on и --reassert
# Маскарад подсетей пользователей (@tunnel_nets4) наружу делает таблица обвязки.
# Чужая политика DROP в ip filter FORWARD (iptables) — ACCEPT для awgrsv в обе
# стороны (fwd_allow при --up/--reassert, снимается --down/--remove), как у
# обвязки шлюза.
#
# Режимы (root, кроме --status):
#   --up <conf>     конфиг линка (из документа конфигурации сервера AWG;
#                   проверка: PrivateKey, [Peer], Table = off, без PostUp/
#                   PostDown/PreUp/PreDown/SaveConfig/DNS) →
#                   /etc/awg-gw/awgrsv.conf (0600), линк поднят (awg-quick),
#                   таблица nft, маршруты, копия скрипта в SELF_PATH и юнит
#                   awg-rsv-gw.service (enable); ADMIN_IPS — в состояние
#                   RSV_STATE (admins=); тот же конфиг при поднятом линке —
#                   только таблица и маршруты («up: same»), другой при
#                   поднятом — awg syncconf без пересоздания (сессия с
#                   резервом не рвётся; адрес и MTU стоящего линка не
#                   меняются), не вышло — awg-quick down/up
#   --reassert      линк (живой — syncconf), таблица и маршруты по конфигу на
#                   диске (юнит при загрузке); наборы rsv_open4 и rsv_admin4 —
#                   из состояния RSV_STATE
#   --open <cidr…>  пересылка открыта для источников (через запятую)
#   --close         пересылка закрыта (набор пуст)
#   --down          линк опущен, таблица и маршруты сняты; конфиг и состояние
#                   остаются (юнит при остановке)
#   --remove        линк, таблица, маршруты, конфиг, юнит и состояние — прочь
#                   (пары нет); копия в SELF_PATH остаётся. Зовут агент (раздела
#                   reserve больше нет) и awg-bot uninstall
#   --tg on|off     выход агента в Telegram через awgrsv при мёртвом аплинке
#                   (концепт «кворум» §4.11): цепочка output (type route,
#                   приоритет mangle + 1 — после метки 0x1 обвязки) ставит
#                   метку TG_MARK (0x53) пакетам процесса awg-bot.service
#                   (socket cgroupv2) к набору tg_nets4; ip rule fwmark 0x53
#                   lookup 152 — в awgrsv; postrouting — masquerade с меткой
#                   (адрес сокета выбран до перемаршрутизации). Решает агент
#                   (tgvia.py); off — всё снято; состояние tg=0|1 в RSV_STATE
#   --status        key=value: link, open, src, rx, tx, conf, tg
# Окружение: RSV_IF (awgrsv), RSV_CONF_DIR (/etc/awg-gw), RSV_STATE
# (/var/lib/awg-gw/rsv.status: open=0|1, src=…, admins=…, tg=0|1), RSV_MARK (0x52),
# RSV_TABLE (152), TG_MARK (0x53), TG_CGROUP (system.slice/awg-bot.service), SSH_PORT (иначе SSH_PORT из GW_STATUS —
# /etc/awg-gw/gateway.status — или 22), ADMIN_IPS (адреса устройств админа
# сервера через запятую, набор rsv_admin4; передаёт агент при --up из строк
# юнита обвязки и переподнимает линк при их смене; юнит awg-rsv-gw.service и
# --open их не передают — берут из RSV_STATE), UNIT
# (/etc/systemd/system/awg-rsv-gw.service), SELF_PATH
# (/usr/local/sbin/reserve-gw-setup.sh; её же зовёт awg-bot uninstall).
# Чего НЕ делает: не читает и не пишет документ конфигурации (его ведёт
# агент), не решает, когда открывать пересылку (решает агент на каждом такте
# живости по слову сервера или резерва вместе с фактом записи DNS; --open и
# --close зовёт только при смене решения, а открыта ли пересылка, читает из
# RSV_STATE сам, не этим скриптом), не трогает аплинк.
set -u

RSV_IF="$(printf '%s' "${RSV_IF:-awgrsv}" | tr -cd 'A-Za-z0-9_.-' | cut -c1-15)"
RSV_CONF_DIR="${RSV_CONF_DIR:-/etc/awg-gw}"
CONF="$RSV_CONF_DIR/$RSV_IF.conf"
RSV_STATE="${RSV_STATE:-/var/lib/awg-gw/rsv.status}"
RSV_MARK="${RSV_MARK:-0x52}"
RSV_TABLE="${RSV_TABLE:-152}"
GUARD_TABLE="inet awg_rsv_guard"
TG_MARK="${TG_MARK:-0x53}"
TG_CGROUP="${TG_CGROUP:-system.slice/awg-bot.service}"
# набор Telegram — тот же, что TG_NETS обвязки (routing-gw-setup.sh) и TG_RANGES агента
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
UNIT="${UNIT:-/etc/systemd/system/awg-rsv-gw.service}"
SELF_PATH="${SELF_PATH:-/usr/local/sbin/reserve-gw-setup.sh}"
GW_STATUS="${GW_STATUS:-/etc/awg-gw/gateway.status}"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16"

say() { printf '%s\n' "$*"; }
die() { say "ОШИБКА: $*"; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }

ssh_port() {
    _p="${SSH_PORT:-}"
    [ -n "$_p" ] || _p="$(sed -n 's/^SSH_PORT=//p' "$GW_STATUS" 2>/dev/null | head -n1)"
    case "$_p" in ''|*[!0-9]*) _p=22 ;; esac
    printf '%s' "$_p"
}

state_write() {   # state_write open=0|1 src=… — tg= сохраняется, если не передан
    mkdir -p "$(dirname "$RSV_STATE")"
    _tg="$(state_get tg)"; _has_tg=0
    { for kv in "$@"; do case "$kv" in tg=*) _has_tg=1 ;; esac; say "$kv"; done; [ $_has_tg -eq 1 ] || say "tg=${_tg:-0}"; } > "$RSV_STATE.tmp"
    mv "$RSV_STATE.tmp" "$RSV_STATE"; chmod 0644 "$RSV_STATE"
}
state_get() { sed -n "s/^$1=//p" "$RSV_STATE" 2>/dev/null | head -n1; }

ipv4_list() { _first=1; for n in "$@"; do if [ $_first -eq 1 ]; then printf '%s' "$n"; _first=0; else printf ', %s' "$n"; fi; done; }

conf_check() {   # конфиг линка к резерву: только Interface/Peer с известными ключами, без PostUp
    [ -f "$1" ] || return 1
    grep -q '^PrivateKey' "$1" || return 1
    grep -q '^\[Peer\]' "$1" || return 1
    ! grep -Eqi '^(PostUp|PostDown|PreUp|PreDown|SaveConfig|DNS)' "$1" || return 1
    grep -q '^Table *= *off' "$1" || return 1
    return 0
}

zone_addr() {   # адрес стороны зоны в /30 — первый AllowedIPs пира (/32): зонды живости бота основы идут с него
    sed -n 's/^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*\([0-9.]*\).*/\1/p' "$CONF" 2>/dev/null | head -n1
}

guard_write() {   # таблица nft: вход, пересылка, connmark; набор rsv_open4 — из состояния
    _open="$(state_get src | tr ',' ' ')"
    _admins="$(printf '%s' "${ADMIN_IPS:-$(state_get admins)}" | tr ',' ' ')"
    _zone="$(zone_addr)"
    nft -f - <<EOF
table $GUARD_TABLE
delete table $GUARD_TABLE
table $GUARD_TABLE {
    set rsv_open4 {
        type ipv4_addr
        flags interval
$( [ -n "$_open" ] && printf '        elements = { %s }\n' "$(ipv4_list $_open)" )
    }
    set rsv_admin4 {
        type ipv4_addr
        flags interval
$( [ -n "$_admins" ] && printf '        elements = { %s }\n' "$(ipv4_list $_admins)" )
    }
    set rsv_private4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $PRIVATE_NETS) }
    }
    chain rsv_input {
        type filter hook input priority filter - 1; policy accept;
        iifname "$RSV_IF" ct state established,related accept
        iifname "$RSV_IF" ip saddr @rsv_open4 ip saddr @rsv_admin4 tcp dport $(ssh_port) accept
        iifname "$RSV_IF" drop
    }
    chain rsv_forward {
        type filter hook forward priority filter - 1; policy accept;
        oifname "$RSV_IF" ct state established,related accept
        iifname "$RSV_IF" ip saddr @rsv_open4 ip daddr != @rsv_private4 accept
        iifname "$RSV_IF" drop
        oifname "$RSV_IF" drop
    }
    chain rsv_mark {
        type filter hook prerouting priority mangle; policy accept;
        iifname "$RSV_IF" ct state new ct mark set $RSV_MARK
        iifname != "$RSV_IF" ct mark $RSV_MARK ct direction reply meta mark set $RSV_MARK
    }
    chain rsv_output {
        type route hook output priority mangle; policy accept;
        ct mark $RSV_MARK ct direction reply meta mark set $RSV_MARK
    }
$( [ -n "$_zone" ] && printf '    chain rsv_post {\n        type nat hook postrouting priority srcnat; policy accept;\n        oifname != "%s" ip saddr %s masquerade\n    }\n' "$RSV_IF" "$_zone" )
$( [ "$(state_get tg)" = "1" ] && cat <<EOF2
    set tg_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $TG_NETS) }
    }
    chain rsv_tgout {
        type route hook output priority mangle + 1; policy accept;
        ip daddr @tg_nets4 socket cgroupv2 level 2 "$TG_CGROUP" meta mark set $TG_MARK
    }
    chain rsv_tgpost {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "$RSV_IF" meta mark $TG_MARK masquerade
    }
EOF2
)
}
EOF
}

fwd_allow() {   # чужой файервол с политикой DROP в ip filter FORWARD (iptables): транзит линку — как обвязка шлюза
    command -v iptables >/dev/null 2>&1 || return 0
    iptables -w -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP' || return 0
    for d in -i -o; do
        iptables -w -C FORWARD "$d" "$RSV_IF" -j ACCEPT 2>/dev/null || iptables -w -I FORWARD 1 "$d" "$RSV_IF" -j ACCEPT 2>/dev/null || true
    done
}
fwd_disallow() {
    command -v iptables >/dev/null 2>&1 || return 0
    for d in -i -o; do
        while iptables -w -C FORWARD "$d" "$RSV_IF" -j ACCEPT 2>/dev/null; do iptables -w -D FORWARD "$d" "$RSV_IF" -j ACCEPT 2>/dev/null || break; done
    done
}

routes_up() {
    ip rule list | grep -q "fwmark $RSV_MARK lookup $RSV_TABLE" || ip rule add fwmark "$RSV_MARK" lookup "$RSV_TABLE"
    ip route replace default dev "$RSV_IF" table "$RSV_TABLE"
    if [ "$(state_get tg)" = "1" ]; then tg_routes_up; else tg_routes_down; fi
}
routes_down() {
    tg_routes_down
    while ip rule list | grep -q "fwmark $RSV_MARK lookup $RSV_TABLE"; do ip rule del fwmark "$RSV_MARK" lookup "$RSV_TABLE" || break; done
    ip route flush table "$RSV_TABLE" 2>/dev/null || true
}
tg_routes_up() {   # сокеты Bot API с меткой TG_MARK — в awgrsv той же таблицей 152
    ip rule list | grep -q "fwmark $TG_MARK lookup $RSV_TABLE" || ip rule add fwmark "$TG_MARK" lookup "$RSV_TABLE"
}
tg_routes_down() {
    while ip rule list | grep -q "fwmark $TG_MARK lookup $RSV_TABLE"; do ip rule del fwmark "$TG_MARK" lookup "$RSV_TABLE" || break; done
}

link_up() {
    # линк уже поднят — ключи и пир подтягиваются без пересоздания (сессия с
    # резервом не рвётся); адрес и MTU у стоящего линка не меняем
    if ip link show "$RSV_IF" >/dev/null 2>&1; then
        _t="$(mktemp)"; awg-quick strip "$CONF" > "$_t" 2>/dev/null || { rm -f "$_t"; return 1; }
        awg syncconf "$RSV_IF" "$_t"; _rc=$?; rm -f "$_t"
        [ "$_rc" -eq 0 ] && return 0
        awg-quick down "$CONF" >/dev/null 2>&1 || true
    fi
    awg-quick up "$CONF"
}
link_down() { ip link show "$RSV_IF" >/dev/null 2>&1 && awg-quick down "$CONF"; return 0; }

install_self() {
    [ -f "$SELF_PATH" ] && cmp -s "$0" "$SELF_PATH" && return 0
    install -m 0755 "$0" "$SELF_PATH" 2>/dev/null || return 1
}

write_unit() {
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: постоянный линк шлюза к резерву
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=RSV_IF=$RSV_IF
ExecStart=$SELF_PATH --reassert
ExecStop=$SELF_PATH --down

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
}

ensure_unit() {
    install_self || die "скрипт линка к резерву не встал в /usr/local/sbin"
    [ -f "$UNIT" ] && grep -q -- '--reassert' "$UNIT" || write_unit
    systemctl enable "$(basename "$UNIT")" >/dev/null 2>&1 || true
}

counters() {   # rx tx интерфейса (сумма по пирам)
    awg show "$RSV_IF" transfer 2>/dev/null | awk '{rx += $2; tx += $3} END {printf "%d %d", rx, tx}'
}

cmd_up() {
    need_root
    [ -n "${1:-}" ] && [ -f "$1" ] || die "нет файла конфига"
    conf_check "$1" || die "конфиг линка к резерву не прошёл проверку"
    mkdir -p "$RSV_CONF_DIR"; chmod 0700 "$RSV_CONF_DIR"
    # адреса админов — в состояние: --reassert при загрузке и --open их не получают
    state_write open="$(state_get open)" src="$(state_get src)" admins="${ADMIN_IPS:-}"
    if [ -f "$CONF" ] && cmp -s "$1" "$CONF" && ip link show "$RSV_IF" >/dev/null 2>&1; then
        guard_write; routes_up; fwd_allow; ensure_unit; say "up: same"; return 0
    fi
    install -m 0600 "$1" "$CONF"
    link_up || die "линк к резерву не поднялся"
    guard_write; routes_up; fwd_allow; ensure_unit
    [ -n "$(state_get open)" ] || state_write open=0 src= admins="${ADMIN_IPS:-}"
    say "up: ok"
}

cmd_reassert() {
    need_root
    [ -f "$CONF" ] || { say "reassert: конфига нет"; return 0; }
    link_up || die "линк к резерву не поднялся"
    guard_write; routes_up; fwd_allow
    say "reassert: ok"
}

cmd_open() {
    need_root
    _src="$(printf '%s' "${1:-}" | tr ' ' ',')"
    [ -n "$_src" ] || die "нет подсетей"
    state_write open=1 src="$_src" admins="$(state_get admins)"
    nft flush set $GUARD_TABLE rsv_open4 2>/dev/null || guard_write
    nft add element $GUARD_TABLE rsv_open4 "{ $(ipv4_list $(printf '%s' "$_src" | tr ',' ' ')) }" || { guard_write; }
    say "open: $_src"
}

cmd_close() {
    need_root
    state_write open=0 src= admins="$(state_get admins)"
    nft flush set $GUARD_TABLE rsv_open4 2>/dev/null || true
    say "close: ok"
}

cmd_tg() {   # --tg on|off: метка сокетов Bot API агента и правило маршрута; таблица — заново
    need_root
    case "${1:-}" in on) _v=1 ;; off) _v=0 ;; *) die "tg: on|off" ;; esac
    state_write open="$(state_get open)" src="$(state_get src)" admins="$(state_get admins)" tg="$_v"
    if [ -f "$CONF" ] && ip link show "$RSV_IF" >/dev/null 2>&1; then guard_write; fi
    if [ "$_v" = 1 ]; then tg_routes_up; else tg_routes_down; fi
    say "tg: $1"
}

cmd_down() {
    need_root
    link_down
    nft delete table $GUARD_TABLE 2>/dev/null || true
    routes_down; fwd_disallow
    say "down: ok"
}

cmd_remove() {
    need_root
    cmd_down >/dev/null
    systemctl disable "$(basename "$UNIT")" >/dev/null 2>&1 || true
    rm -f "$UNIT"; systemctl daemon-reload 2>/dev/null || true
    rm -f "$CONF" "$RSV_STATE"
    say "remove: ok"
}

cmd_status() {
    if ip link show "$RSV_IF" >/dev/null 2>&1; then say "link=up"; else say "link=down"; fi
    say "open=$(state_get open)"; say "src=$(state_get src)"
    set -- $(counters); say "rx=${1:-0}"; say "tx=${2:-0}"
    [ -f "$CONF" ] && say "conf=present" || say "conf=absent"
    [ "$(state_get tg)" = "1" ] && say "tg=on" || say "tg=off"
}

case "${1:-}" in
    --up) cmd_up "${2:-}" ;;
    --reassert) cmd_reassert ;;
    --open) cmd_open "${2:-}" ;;
    --close) cmd_close ;;
    --down) cmd_down ;;
    --remove) cmd_remove ;;
    --tg) cmd_tg "${2:-}" ;;
    --status) cmd_status ;;
    *) say "использование: $0 --up <conf>|--reassert|--open <cidr,…>|--close|--down|--remove|--tg on|off|--status"; exit 2 ;;
esac
