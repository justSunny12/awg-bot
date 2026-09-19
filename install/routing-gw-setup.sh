#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-gw-setup.sh — сторона ШЛЮЗА (малинки) для условной маршрутизации.
# Запускается НА МАЛИНКЕ. См. docs/conditional-routing.md, §12.
#
# КОНТЕКСТ. Линк поднимается ХОСТОВЫМИ awg/awg-quick — модуль ядра amneziawg
# живёт на хосте, и версия утилит обязана совпадать с ним. Контейнер Amnezia
# шлюзу не нужен вовсе: в образе были только бинарники. Если он ещё жив, скрипт
# лишь снимет линк, поднятый прежней схемой.
#
# Интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ сами — переопределяются
# переменными WAN_IF / HOST_CONF_DIR (и CONTAINER, если автопоиск ошибся).
#
# ЧТО ДЕЛАЕТ:
#   1) кладёт конфиг линка и поднимает интерфейс;
#   2) ставит ОДНУ nft-таблицу inet awg_gw_guard — всю обвязку разом:
#      MASQUERADE клиентов в eth0 (ради этого всё и затевалось: российские
#      сервисы увидят домашний адрес); изоляцию клиентов от домашней сети
#      (нужны НОВЫЕ соединения из туннеля, а это открывает путь к NAS, роутеру,
#      торрент-клиенту — закрываем); метки Telegram для самого агента; и защиту
#      САМОЙ МАШИНЫ от туннеля: с адресов туннеля на неё пускается только ВПС
#      по линку (SSH, ICMP), всё прочее дропается. Исключение — УСТРОЙСТВА
#      АДМИНА (ADMIN_IPS, приезжают в бандле): им открыто всё, и машина, и
#      домашняя сеть через неё;
#   3) снимает прежнюю обвязку в iptables (AWGLINK_FWD, MASQUERADE, метки);
#   4) автозапуск: юнит зовёт этот же скрипт, таблица ставится ДО подъёма линка.
#
# ЧЕГО НЕ ДЕЛАЕТ: не трогает существующие интерфейсы, домашнюю схему
# маршрутизации и чужие правила iptables (docker и т.п.) — они работают как
# работали. Политика INPUT для домашней сети остаётся accept.
#
# ОКРУЖЕНИЕ (из юнита/бандла): CLIENT_SUBNET, LINK_IF, ADMIN_IPS (устройства
# админа — полный доступ с туннеля), ADMIN_IPS_EXTRA (добавленное на самом
# шлюзе: /etc/awg-gw/firewall.env), SSH_PORT (22), TG_MARK (0x1).
#
# ЗАПУСК:
#   sudo sh routing-gw-setup.sh                    # показать план
#   sudo sh routing-gw-setup.sh --apply gw-awglink.conf
#   sudo sh routing-gw-setup.sh --rollback
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="${LINK_IF:-awglink}"
# Боевое значение приезжает из БАНДЛА (export перед запуском) и закрепляется в
# юните строкой Environment: на шлюзе нет app.yaml, и после ребута юнит обязан
# реассертить ту подсеть, с которой бандл собирали, а не хардкод-дефолт.
CLIENT_SUBNET="${CLIENT_SUBNET:-10.8.1.0/24}"
FWD_CHAIN="AWGLINK_FWD"                  # прежняя цепочка iptables — только снятие
UNIT="/etc/systemd/system/awg-link-gw.service"
SYSCTL_CONF="/etc/sysctl.d/99-awgbot-gw.conf"
NETWORKD_UNMANAGED="/etc/systemd/network/99-awg-gw-unmanaged.network"   # networkd не трогает awg*
# локальная сеть без VPN (docs/gateway-lan.md): вторая таблица nft, dnsmasq, списки
HOME_TABLE="inet awg_home"
HOME_FILE="$GW_ETC/home.nft"
LAN_SYSCTL="/etc/sysctl.d/98-awg-gw-lan.conf"
DNSMASQ_D="/etc/dnsmasq.d"
DNSMASQ_OVR="/etc/systemd/system/dnsmasq.service.d/awg-gw.conf"
LAN_DUMP="/var/lib/awg-gw"                # слепки наборов: грузятся при старте до фидов
LAN_LISTS="/usr/local/sbin/awg-lan-lists.sh"
LAN_DOMAIN="/usr/local/sbin/awg-lan-domain.sh"
LAN_MODE="${LAN_MODE:-0}"
HOME_SUBNETS="${HOME_SUBNETS:-}"
RESOLVER="${RESOLVER:-}"
GW_ETC="/etc/awg-gw"
GUARD_FILE="$GW_ETC/guard.nft"           # таблица — источник для nft -f при каждом старте
FW_ENV="$GW_ETC/firewall.env"            # ADMIN_IPS_EXTRA, правится на шлюзе (awg-bot firewall)
GUARD_TABLE="inet awg_gw_guard"
SSH_PORT="${SSH_PORT:-22}"
TG_MARK="${TG_MARK:-0x1}"
# Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами; тот же
# список знает агент (domain/gateway.py TG_RANGES) и сверяет с таблицей.
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
# GitHub — той же меткой в аплинк: агент обновляется с releases, а в юрисдикции
# шлюза GitHub без туннеля недоступен. Четыре сети из api.github.com/meta
# (web/api/git), в них github.com, api.github.com и *.githubusercontent.com
# (release-assets, objects, raw); региональные /32 Azure оттуда же — не для
# нас. Тот же список знает агент (domain/gateway.py GH_RANGES).
GH_NETS="140.82.112.0/20 143.55.64.0/20 185.199.108.0/22 192.30.252.0/22"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"
# Устройства админа (из бандла с ВПС) — им с туннеля открыто ВСЁ: сама машина и
# домашняя сеть через неё. Прочим клиентам — изоляция и drop. Старое имя
# переменной (SSH_ALLOW) принимается для бандлов прежнего выпуска.
ADMIN_IPS="${ADMIN_IPS:-${SSH_ALLOW:-}}"
ADMIN_IPS_EXTRA="${ADMIN_IPS_EXTRA:-}"
[ -f "$FW_ENV" ] && . "$FW_ENV"
# Шлюзовое устройство (пометка в основном боте): ключ аплинка помеченного
# шлюза, старый ключ в окне переезда, конфиг аплинка в base64. Пусто — шлюз
# в основном боте не помечен.
GATEWAY_PUBKEY="${GATEWAY_PUBKEY:-}"
GATEWAY_PREV_PUBKEY="${GATEWAY_PREV_PUBKEY:-}"
UPLINK_B64="${UPLINK_B64:-}"
UPLINK_TABLE="${UPLINK_TABLE:-100}"          # таблица политики «метка → аплинк»
UPLINK_IF_DEFAULT="${UPLINK_IF:-awg0}"        # имя аплинка на чистой машине
GW_FOREIGN=0                                  # 1 = слот помечен другому устройству, линк не поднимаем
GW_UNCONFIRMED=0                              # 1 = аплинка не видно, шлюз не подтверждён — линк не трогаем
GW_STATUS_FILE="$GW_ETC/gateway.status"       # что решил скрипт — читает агент

# Публичный ключ интерфейса: `awg show <if> public-key`.
iface_pubkey() { "$AWG_BIN" show "$1" public-key 2>/dev/null || true; }
conf_pubkey() {                # $1 = конфиг; публичный ключ из PrivateKey или ничего
    _pk="$(awk -F'= *' '/^PrivateKey/{print $2; exit}' "$1" 2>/dev/null | tr -d ' \r')"
    [ -n "$_pk" ] || return 0
    printf '%s' "$_pk" | "$AWG_BIN" pubkey 2>/dev/null || true
}
# Аплинки ЭТОЙ машины строками «имя ключ»: сначала живые интерфейсы, затем
# конфиги тех, что ещё не поднялись. Линки до ВПС (свой слот и слот соседа —
# awglink*) аплинками не считаются: их ключ принадлежит паре ВПС↔шлюз, а не
# устройству, и сравнивать пометку слота с ним нельзя.
uplink_list() {
    _live=" $("$AWG_BIN" show interfaces 2>/dev/null | tr '\n' ' ')"
    for _i in $_live; do
        case "$_i" in "$LINK_IF"|awglink*) continue ;; esac
        printf '%s %s\n' "$_i" "$(iface_pubkey "$_i")"
    done
    # Конфиги: при загрузке юнит обвязки стартует РАНЬШЕ awg-quick@ аплинка, и
    # по одним живым интерфейсам машина выглядит чужой — линк ложится, юнит
    # гасит сам себя (наступили при ребуте второго шлюза 19.09.2026). Ключ
    # устройства лежит в конфиге и до подъёма интерфейса.
    for _c in "$HOST_CONF_DIR"/*.conf; do
        [ -f "$_c" ] || continue
        _n="$(basename "$_c" .conf)"
        case "$_n" in "$LINK_IF"|awglink*) continue ;; esac
        case "$_live" in *" $_n "*) continue ;; esac
        printf '%s %s\n' "$_n" "$(conf_pubkey "$_c")"
    done
}
iface_by_pubkey() {            # $1 = ключ УСТРОЙСТВА; печатает имя аплинка или ничего
    [ -n "$1" ] || return 0
    uplink_list | while read -r _n _k; do
        [ -n "$_k" ] && [ "$_k" = "$1" ] && { printf '%s' "$_n"; break; }
    done
}

# Прежняя обвязка в iptables: снимаем идемпотентно и при --apply (переезд на
# таблицу), и при --rollback. Чужих правил (docker, домашняя схема) не касаемся.
legacy_cleanup() {
    # в режиме показа циклы «пока правило есть — снимай» не сходятся: -D не
    # исполняется. Показ — одной строкой.
    if [ "$MODE" = "plan" ]; then
        printf '  would: снять прежние правила iptables (%s, MASQUERADE, метки Telegram), если есть\n' "$FWD_CHAIN"
        return 0
    fi
    while iptables -C FORWARD -i "$LINK_IF" -j "$FWD_CHAIN" 2>/dev/null; do
        run "iptables -D FORWARD -i $LINK_IF -j $FWD_CHAIN"
    done
    while iptables -C FORWARD -o "$LINK_IF" -m state --state RELATED,ESTABLISHED \
          -j ACCEPT 2>/dev/null; do
        run "iptables -D FORWARD -o $LINK_IF -m state --state RELATED,ESTABLISHED -j ACCEPT"
    done
    while iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $CLIENT_SUBNET -o $WAN_IF -j MASQUERADE"
    done
    while [ -n "${LINK_CIDR:-}" ] && iptables -t nat -C POSTROUTING -s "$LINK_CIDR" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $LINK_CIDR -o $WAN_IF -j MASQUERADE"
    done
    if iptables -S "$FWD_CHAIN" >/dev/null 2>&1; then
        run "iptables -F $FWD_CHAIN && iptables -X $FWD_CHAIN"
    fi
    for n in $TG_NETS; do
        while iptables -t mangle -C OUTPUT -d "$n" -j MARK --set-mark "$TG_MARK" 2>/dev/null; do
            run "iptables -t mangle -D OUTPUT -d $n -j MARK --set-mark $TG_MARK"
        done
    done
}

# ── локальная сеть без VPN (docs/gateway-lan.md, функция A): помощники ────────
LAN_IF=""; LAN_ADDR=""
dn_set_elsewhere() {           # $1 = regex: ключ dnsmasq уже задан в ДРУГОМ файле?
    for _f in /etc/dnsmasq.conf "$DNSMASQ_D"/*.conf; do
        [ -f "$_f" ] || continue
        case "$_f" in */awg-gw-base.conf) continue ;; esac
        grep -qsE "$1" "$_f" && return 0
    done
    return 1
}
lan_iface_for() {              # $1 = подсеть a.b.c.d/n → «iface addr» или ничего
    ip -4 -o addr show 2>/dev/null | awk -v net="$1" '
        function ip2n(s, a) { split(s, a, "."); return ((a[1]*256+a[2])*256+a[3])*256+a[4] }
        BEGIN { split(net, p, "/"); h = 2^(32-(p[2]+0)); want = int(ip2n(p[1])/h) }
        $2 != "lo" { split($4, q, "/"); if (int(ip2n(q[1])/h) == want) { print $2, q[1]; exit } }'
}
lan_remove() {                 # снять всё своё; персональные списки — в .bak
    _changed=0
    for _f in awg-gw-base.conf awg-gw-doh.conf awg-gw-vpn-feed.conf; do
        [ -f "$DNSMASQ_D/$_f" ] && { run "rm -f $DNSMASQ_D/$_f"; _changed=1; }
    done
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] && { run "mv -f $DNSMASQ_D/$_f $DNSMASQ_D/$_f.bak"; _changed=1; }
    done
    [ -f "$DNSMASQ_OVR" ] && { run "rm -f $DNSMASQ_OVR"; run "systemctl daemon-reload"; _changed=1; }
    if [ "$_changed" = "1" ] && systemctl is-active --quiet dnsmasq 2>/dev/null; then
        run "systemctl restart dnsmasq"
    fi
    nft list table $HOME_TABLE >/dev/null 2>&1 && run "nft delete table $HOME_TABLE"
    [ -f "$HOME_FILE" ] && run "rm -f $HOME_FILE"
    [ -f "$LAN_SYSCTL" ] && run "rm -f $LAN_SYSCTL"
    [ -f "$LAN_LISTS" ] && run "rm -f $LAN_LISTS"
    [ -f "$LAN_DOMAIN" ] && run "rm -f $LAN_DOMAIN"
    return 0
}
lan_migrate_manual() {         # ручной слой (docs/gateway-lan.md §7): переезжает, не ломается
    _moved=""
    for _u in home-split.service awg-lists.timer awg-lists.service; do
        if [ -f "/etc/systemd/system/$_u" ]; then
            run "systemctl disable --now $_u 2>/dev/null || true"
            run "mv -f /etc/systemd/system/$_u /etc/systemd/system/$_u.bak"
            _moved="$_moved $_u"
        fi
    done
    if ip rule list 2>/dev/null | grep -q "fwmark 0x10 lookup 100"; then
        run "ip rule del fwmark 0x10 lookup 100"; _moved="$_moved ip-rule-0x10"
    fi
    nft list table inet home_split >/dev/null 2>&1 && { run "nft delete table inet home_split"; _moved="$_moved home_split"; }
    for _f in /etc/home-split.nft /etc/sysctl.d/99-home-split.conf /usr/local/sbin/home-split.sh \
              /usr/local/sbin/home-subnets.sh /usr/local/sbin/home-lists-update.sh /usr/local/bin/awg-add \
              /etc/systemd/network/99-awg-unmanaged.network /etc/systemd/system/dnsmasq.service.d/restart.conf \
              /etc/systemd/system/awg-quick@${UPLINK_IF:-awg0}.service.d/retry.conf \
              "$DNSMASQ_D/awg-base.conf" "$DNSMASQ_D/vpn-domains.conf"; do
        [ -f "$_f" ] && { run "mv -f $_f $_f.bak"; _moved="$_moved $(basename "$_f")"; }
    done
    # персональные списки — данные человека: переносятся с заменой имени набора
    if [ -f "$DNSMASQ_D/ru-force.conf" ]; then
        run "sed 's|inet#home_split#ru4|inet#awg_home#lan_ru4|' $DNSMASQ_D/ru-force.conf > $DNSMASQ_D/awg-gw-ru-user.conf && mv -f $DNSMASQ_D/ru-force.conf $DNSMASQ_D/ru-force.conf.bak"
        _moved="$_moved ru-force.conf→awg-gw-ru-user.conf"
    fi
    if [ -f "$DNSMASQ_D/user-managed.conf" ]; then
        run "sed 's|inet#home_split#vpn4|inet#awg_home#lan_vpn4|' $DNSMASQ_D/user-managed.conf > $DNSMASQ_D/awg-gw-vpn-user.conf && mv -f $DNSMASQ_D/user-managed.conf $DNSMASQ_D/user-managed.conf.bak"
        _moved="$_moved user-managed.conf→awg-gw-vpn-user.conf"
    fi
    [ -n "$_moved" ] && say "  ручной слой перенесён:$_moved (старые файлы — .bak)"
    return 0
}
write_lan_scripts() {          # скрипты списков — из этого же файла, чтобы бандл был самодостаточен
cat > "$LAN_LISTS" <<'LISTSEOF'
#!/bin/sh
# awg-lan-lists.sh — списки локальной сети без VPN (docs/gateway-lan.md §3.3).
# Зовёт агент по расписанию (с джиттером) и `awg-bot lan update`. Идемпотентно.
#   фид доменов  → /etc/dnsmasq.d/awg-gw-vpn-feed.conf (nftset= в lan_vpn4), минус исключения
#   фиды подсетей → набор lan_vpn_nets4 (атомарно: flush + add)
#   слепки наборов → /var/lib/awg-gw/*.nft (грузятся при старте до фидов)
set -u
TABLE="inet awg_home"
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
FEED="$D/awg-gw-vpn-feed.conf"; RU="$D/awg-gw-ru-user.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"
ITDOG="https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
DOMAINS_URL="${AWG_LAN_DOMAINS_URL:-$ITDOG/Russia/inside-dnsmasq-ipset.lst}"
SUBNET_SERVICES="${AWG_LAN_SUBNET_SERVICES:-telegram meta twitter cloudflare discord}"
GOOG_URL="https://www.gstatic.com/ipranges/goog.json"
TMP="$(mktemp)"; TMP2="$(mktemp)"; NETS="$(mktemp)"
trap 'rm -f "$TMP" "$TMP2" "$NETS"' EXIT
rc=0

# ── домены: во временный файл (недокачанный .new в conf-dir читался бы демоном)
if curl -sf --max-time 60 "$DOMAINS_URL" -o "$TMP" && [ -s "$TMP" ]; then
    # sed -i без суффикса — GNU-изм: правим через временный файл
    sed 's|^ipset=\(/.*/\)vpn_domains$|nftset=\1inet#awg_home#lan_vpn4|' "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    # исключения побеждают: dnsmasq применяет одну директиву на домен, и какая
    # из двух победит, зависело бы от порядка чтения каталога — вычитаем
    if [ -s "$RU" ]; then
        awk -F/ '
            NR==FNR { if ($0 ~ /^nftset=/) for (i=2; i<NF; i++) skip[$i]=1; next }
            $0 !~ /^nftset=/ { print; next }
            { out=$1; n=0; for (i=2; i<NF; i++) if (!($i in skip)) { out=out "/" $i; n++ }
              if (n) print out "/" $(NF) }' "$RU" "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    fi
    # дифф-скип: рестарт роняет кэш всей сети, а список меняется не каждые 6 ч
    if ! cmp -s "$TMP" "$FEED"; then
        install -m 644 "$TMP" "$FEED"
        systemctl restart dnsmasq          # именно restart: SIGHUP конфиги не перечитывает
    fi
else
    echo "домены: фид не скачался ($DOMAINS_URL)" >&2; rc=1
fi

# ── подсети: itdoginfo по сервисам + официальный фид Google
: > "$NETS"
for svc in $SUBNET_SERVICES; do
    curl -sf --max-time 60 "$ITDOG/Subnets/IPv4/${svc}.lst" >> "$NETS" 2>/dev/null \
        || { echo "подсети: $svc не скачался" >&2; rc=1; }
    echo >> "$NETS"
done
curl -sf --max-time 60 "$GOOG_URL" 2>/dev/null \
    | grep -oE '"ipv4Prefix":[[:space:]]*"[0-9./]+"' \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' >> "$NETS" \
    || { echo "подсети: goog.json не скачался" >&2; rc=1; }
elems="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' "$NETS" | sort -u | paste -sd, -)"
if [ -n "$elems" ]; then
    # одной транзакцией: между flush и add окна нет
    printf 'flush set %s lan_vpn_nets4\nadd element %s lan_vpn_nets4 { %s }\n' "$TABLE" "$TABLE" "$elems" \
        | nft -f - || { echo "подсети: nft отклонил набор" >&2; rc=1; }
fi

# ── слепки: при старте таблица пуста, пока агент не сходит за фидами
mkdir -p "$DUMP"
for s in lan_vpn4 lan_vpn_nets4; do
    nft list set $TABLE $s > "$DUMP/$s.nft.tmp" 2>/dev/null && mv -f "$DUMP/$s.nft.tmp" "$DUMP/$s.nft"
done
printf 'updated_at=%s\ndomains=%s\nnets=%s\nrc=%s\n' "$(date -Iseconds)" \
    "$(grep -c '^nftset=' "$FEED" 2>/dev/null || echo 0)" \
    "$(printf '%s' "$elems" | tr ',' '\n' | grep -c . )" "$rc" > "$DUMP/lists.status"
exit $rc
LISTSEOF
chmod 0755 "$LAN_LISTS"
cat > "$LAN_DOMAIN" <<'DOMEOF'
#!/bin/sh
# awg-lan-domain.sh — персональные списки локальной сети без VPN.
#   add <домен…>   — в туннель (awg-gw-vpn-user.conf, набор lan_vpn4)
#   ru  <домен…>   — напрямую, российский адрес (awg-gw-ru-user.conf, набор lan_ru4)
#   del <домен…>   — убрать из обоих
#   list           — показать: «vpn <домен>» / «ru <домен>»
# Домен накрывает поддомены. Схема и www. отбрасываются. Хост ВПС (Endpoint
# аплинка) добавить нельзя: увести туннель в туннель — запереть себя.
set -u
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
VPN="$D/awg-gw-vpn-user.conf"; RU="$D/awg-gw-ru-user.conf"
TABLE="inet awg_home"
UPLINK_CONF="${AWG_UPLINK_CONF:-/etc/amnezia/amneziawg/awg0.conf}"
cmd="${1:-}"; [ $# -gt 0 ] && shift
[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }
touch "$VPN" "$RU"
drop_line() {                  # $1 = домен, $2 = файл; sed -i без суффикса — GNU-изм
    grep -v "^nftset=/$1/" "$2" > "$2.tmp"; mv -f "$2.tmp" "$2"
}
case "$cmd" in
    list)
        sed -n 's|^nftset=/\([^/]*\)/.*|vpn \1|p' "$VPN"
        sed -n 's|^nftset=/\([^/]*\)/.*|ru \1|p' "$RU"
        exit 0 ;;
    add|ru|del) [ $# -gt 0 ] || { echo "usage: $0 $cmd <домен…>"; exit 1; } ;;
    *) echo "usage: $0 add|ru|del <домен…> | list"; exit 1 ;;
esac
deny="$(sed -n 's/^Endpoint *= *\([^:]*\):.*/\1/p' "$UPLINK_CONF" 2>/dev/null | head -n1 | tr 'A-Z' 'a-z')"
changed=0; added=""
for raw in "$@"; do
    d="$(printf '%s' "$raw" | sed -E 's|^[a-zA-Z]+://||; s|/.*$||; s|^www\.||' | tr 'A-Z' 'a-z')"
    printf '%s' "$d" | grep -Eq '^[a-z0-9.-]+\.[a-z]{2,}$' || { echo "$d: не похоже на домен, пропущен"; continue; }
    if [ "$cmd" = "del" ]; then
        if grep -q "^nftset=/$d/" "$VPN" "$RU" 2>/dev/null; then
            drop_line "$d" "$VPN"; drop_line "$d" "$RU"; echo "$d: убран"; changed=1
        else
            echo "$d: в списках нет"
        fi
        continue
    fi
    [ -n "$deny" ] && [ "$d" = "$deny" ] && { echo "$d: это хост сервера — его добавить нельзя"; continue; }
    if [ "$cmd" = "add" ]; then f="$VPN"; set_="lan_vpn4"; other="$RU"; else f="$RU"; set_="lan_ru4"; other="$VPN"; fi
    grep -q "^nftset=/$d/" "$f" && { echo "$d: уже в списке"; continue; }
    drop_line "$d" "$other"                    # из противоположного списка — убрать
    echo "nftset=/$d/inet#awg_home#$set_" >> "$f"
    added="$added $d:$set_"; changed=1
    echo "$d: добавлен"
done
[ "$changed" = "1" ] || exit 0
systemctl restart dnsmasq && sleep 1
for pair in $added; do
    d="${pair%%:*}"; set_="${pair##*:}"; n=0
    for ip in $(dig +short +time=3 @127.0.0.1 "$d" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'); do
        nft add element $TABLE $set_ "{ $ip }" 2>/dev/null && n=$((n+1))
    done
    echo "  $d → $n адрес(а) в наборе $set_"
done
DOMEOF
chmod 0755 "$LAN_DOMAIN"
}


# Подсеть линка — У ЯДРА (/30 ⇒ сеть считается из адреса), сосед по /30 — ВПС.
link_cidr_of() {
    ip -4 -o addr show dev "$1" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}' \
        | awk -F'[./]' '$5==30{ printf "%d.%d.%d.%d/%d\n", $1, $2, $3, int($4/4)*4, $5 }'
}
link_peer_of() {
    ip -4 -o addr show dev "$1" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}' \
        | awk -F'[./]' '$5==30{ b=int($4/4)*4; p=(($4-b)==1)?b+2:b+1; printf "%d.%d.%d.%d\n", $1, $2, $3, p }'
}

# Элементы set из строки: только адреса и CIDR IPv4 — чужое в файл nft не
# попадает (значение приезжает из бандла и env, доверять форме нельзя).
ipv4_list() {
    for t in "$@"; do
        case "$t" in
            *[!0-9./]*|"") ;;
            *) printf '%s\n' "$t" ;;
        esac
    done | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g'
}

# Контейнер, интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ, а не задаются
# дефолтом: чужие имена в поставке — источник тихих ошибок «скрипт отработал, но
# не там». Любое можно переопределить переменной окружения.
# Контейнер шлюзу БОЛЬШЕ НЕ НУЖЕН: линк поднимает хостовой awg-quick, а в образе
# Amnezia были только бинарники. Ищем его исключительно чтобы подчистить линк,
# поднятый прежней, контейнерной схемой. Не нашли — не беда.
#
# `return 0` в конце обязателен. Без него функция отдаёт статус последней команды
# цикла, а это неудачный `docker exec` на последнем контейнере. Присваивание
# CONTAINER="$(detect_container)" получает ненулевой статус, и при set -e скрипт
# умирает МОЛЧА — не дойдя даже до строки с сообщением об ошибке. Ровно так он и
# отработал «успешно», не поставив ни одного правила.
detect_container() {
    if [ -n "${CONTAINER:-}" ]; then printf '%s' "$CONTAINER"; return 0; fi
    for n in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        if docker exec "$n" sh -c 'command -v awg' >/dev/null 2>&1; then
            printf '%s' "$n"; return 0
        fi
    done
    return 0
}
detect_wan() {
    [ -n "${WAN_IF:-}" ] && { printf '%s' "$WAN_IF"; return; }
    ip route show default 2>/dev/null | awk '/^default/{print $5; exit}'
}

MODE="plan"; SRC_CONF=""
case "${1:-}" in
    --apply)    MODE="apply"; SRC_CONF="${2:-}" ;;
    --rollback) MODE="rollback" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n── %s\n' "$*"; }
run()  {
    if [ "$MODE" = "plan" ]; then printf '  would: %s\n' "$*"
    else printf '  $ %s\n' "$*"; sh -c "$*"; fi
}

# Юнит обязан ссылаться на ПОСТОЯННЫЙ путь, поэтому скрипт сначала ставит себя
# рядом с прочими локальными админскими командами и только потом пишет ExecStart.
#
# Прежде туда уходил `readlink -f "$0"` — каталог, ОТКУДА запустили. Бандл
# шлюза распаковывается во временный, а systemd-tmpfiles вычищает его через
# десять дней. Автозапуск умирал молча: интерфейс уже стоял, RemainAfterExit
# держал юнит «активным», и обнаруживалось это только при первой перезагрузке —
# уже в виде «за шлюзом нет интернета», без связи с каким-либо действием.
install_self() {
    _src="$(readlink -f "$0")"
    _dst="/usr/local/sbin/$(basename "$_src")"
    if [ "$_src" != "$_dst" ]; then
        mkdir -p /usr/local/sbin
        install -m 0755 "$_src" "$_dst" || return 1
        printf '  $ install -m 0755 %s %s\n' "$_src" "$_dst" >&2
    fi
    printf '%s' "$_dst"          # только путь в stdout — его подхватит SELF
}

# Линк поднимаем ХОСТОВЫМИ утилитами, а не через контейнер. Причина не в
# красоте: модуль ядра amneziawg живёт на хосте, и его протокол netlink обязан
# совпадать с версией awg. Утилиты внутри образа Amnezia живут своей жизнью и
# однажды разъезжаются с модулем — тогда `awg setconf` падает с Invalid
# argument, и линк не поднимается ВОВСЕ (откатиться в userspace он уже не может,
# раз модуль есть). Контейнер для линка не нужен: там были только бинарники.
AWG_QUICK="$(command -v awg-quick || true)"
AWG_BIN="$(command -v awg || true)"
# Отсутствие контейнера — НЕ ошибка: он тут только для подчистки прежней схемы.
CONTAINER="$(detect_container)"
WAN_IF="$(detect_wan)"
[ -n "$WAN_IF" ] || { say "ОШИБКА: не определил интерфейс выхода. Укажи: WAN_IF=eth0 $0 ..."; exit 1; }
HOST_CONF_DIR="${HOST_CONF_DIR:-/etc/amnezia/amneziawg}"

say "Параметры (определены автоматически, переопределяются переменными):"
say "  интерфейс линка   : $LINK_IF"
say "  контейнер         : ${CONTAINER:-нет (и не нужен)}"
say "  конфиг на хосте   : $HOST_CONF_DIR/$LINK_IF.conf"
say "  клиенты ВПС       : $CLIENT_SUBNET"
say "  выход в интернет  : $WAN_IF"
say "  локальные сети    : будут ЗАКРЫТЫ для клиентов (все приватные диапазоны)"

# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Снятие"
    LINK_CIDR_PRE="$(link_cidr_of "$LINK_IF")"
    run "systemctl disable --now awg-link-gw.service 2>/dev/null || true"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    if [ -n "$CONTAINER" ]; then
        run "docker exec $CONTAINER awg-quick down $LINK_IF 2>/dev/null || true"
    fi
    # подсеть линка — пока интерфейс ещё жив (до down он выше уже снят, но
    # адрес мог остаться в старом iptables-правиле — снимаем по нему)
    LINK_CIDR="${LINK_CIDR_PRE:-$(link_cidr_of "$LINK_IF")}"
    legacy_cleanup
    lan_remove
    run "nft delete table $GUARD_TABLE 2>/dev/null || true"
    run "rm -f $GUARD_FILE $FW_ENV $GW_STATUS_FILE"
    run "rmdir $GW_ETC 2>/dev/null || true"
    run "rm -f $HOST_CONF_DIR/$LINK_IF.conf $UNIT $SYSCTL_CONF /etc/systemd/networkd.conf.d/awg-gw.conf $NETWORKD_UNMANAGED"
    run "rm -f /etc/systemd/system/awg-quick@*.service.d/awg-gw.conf"
    run "systemctl daemon-reload"
    say ""
    say "Готово. Существующие интерфейсы и маршрутизация шлюза не тронуты."
    exit 0
fi

if [ "$MODE" = "plan" ]; then
    say ""
    say "(режим показа — добавь: --apply <файл-конфига-с-ВПС>)"
    say ""
    say "Будет сделано:"
    say "  1. конфиг → $HOST_CONF_DIR/$LINK_IF.conf, awg-quick up хостовыми утилитами"
    say "  2. таблица nft $GUARD_TABLE: MASQUERADE $CLIENT_SUBNET → $WAN_IF, изоляция"
    say "     клиентов от приватных сетей, метки Telegram и GitHub → аплинк, защита шлюза от туннеля"
    say "     (с туннеля на шлюз: ВПС по линку; полный доступ — ADMIN_IPS=${ADMIN_IPS:-—})"
    say "  3. снятие прежних правил iptables ($FWD_CHAIN, MASQUERADE, метки)"
    say "  0. шлюзовое устройство: ${GATEWAY_PUBKEY:+помечен, конфиг аплинка ставится машине с тем же ключом}${GATEWAY_PUBKEY:-не помечен}"
    say "  4. юнит awg-link-gw.service"
    say "  5. локальная сеть без VPN: ${LAN_MODE:-0} (подсети: ${HOME_SUBNETS:-—}; резолвер: ${RESOLVER:-запасной через аплинк})"
    exit 0
fi

# ── 0. шлюзовое устройство: аплинк и чей это шлюз ───────────────────────────
# Аплинк — клиентский туннель этой машины к ВПС, которым она является
# устройством админа. Помеченный шлюз приезжает в бандле ключом аплинка и его
# конфигом. Конфиг ставим ТОЛЬКО машине с тем же ключом (или со старым ключом
# пары в окне переезда): чужая машина шлюзом не становится, пока основной бот
# её не пометит, — она попросит переслать сообщение и линк не поднимет.
step "0. Шлюзовое устройство"
mkdir -p "$GW_ETC"
UPLINK_STATE="none"
if [ -z "$GATEWAY_PUBKEY" ]; then
    say "  в основном боте шлюз не назначен — после применения агент попросит переслать сообщение"
    GW_STATUS="unmarked"
else
    UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PUBKEY")"
    [ -z "$UPLINK_IF" ] && [ -n "$GATEWAY_PREV_PUBKEY" ] && UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PREV_PUBKEY")"
    # ЧИСТАЯ машина: аплинка нет вовсе. Бандл выпущен основным ботом под это
    # устройство и несёт его ключи — кто применил бандл, тот и шлюз. Ставим
    # аплинк под именем UPLINK_IF (по умолчанию awg0). Машина с ЧУЖИМ аплинком
    # шлюзом не становится — это ветка «foreign» ниже.
    _others="$(uplink_list | cut -d' ' -f1)"
    if [ -z "$UPLINK_IF" ] && [ -z "$_others" ] && [ -n "$UPLINK_B64" ] \
       && [ ! -f "$HOST_CONF_DIR/${UPLINK_IF_DEFAULT}.conf" ]; then
        UPLINK_IF="$UPLINK_IF_DEFAULT"
        say "  чистая машина: аплинк $UPLINK_IF ставится из бандла"
    fi
    if [ -n "$UPLINK_IF" ]; then
        say "  это помеченный шлюз: аплинк $UPLINK_IF"
        GW_STATUS="confirmed"
        # Аплинк на загрузке поднимается дольше, чем systemd готов ждать: пять
        # отказов за десять секунд (DNS ещё не резолвит Endpoint) — и юнит
        # сдаётся навсегда, малина остаётся без Telegram и без списков.
        # Снимаем лимит и перезапускаем до победы.
        _ovr="/etc/systemd/system/awg-quick@$UPLINK_IF.service.d"
        if [ ! -f "$_ovr/awg-gw.conf" ]; then
            run "mkdir -p $_ovr"
            run "printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=on-failure\nRestartSec=10\n' > $_ovr/awg-gw.conf"
            run "systemctl daemon-reload"
        else
            say "  оверрайд awg-quick@$UPLINK_IF (перезапуск до победы) уже есть"
        fi
        if [ -n "$UPLINK_B64" ]; then
            # временный каталог тут ни к чему: файл живёт рядом с
            # прочим состоянием шлюза и сразу удаляется
            _tmp="$GW_ETC/uplink.new"
            printf '%s' "$UPLINK_B64" | base64 -d > "$_tmp" 2>/dev/null || : > "$_tmp"
            if grep -q '^PrivateKey' "$_tmp"; then
                # PostUp/PostDown — ВНУТРИ [Interface], сразу за Table = off:
                # awg-quick вырезает их только из этой секции, в [Peer] они
                # уходят в setconf и роняют подъём («Line unrecognized»)
                awk -v mark="$TG_MARK" -v tbl="$UPLINK_TABLE" '
                    { print }
                    /^Table = off$/ {
                        printf "PostUp = ip rule list | grep -q \"fwmark %s lookup %s\" || ip rule add fwmark %s lookup %s\n", mark, tbl, mark, tbl
                        printf "PostUp = ip route replace default dev %%i table %s\n", tbl
                        printf "PostUp = sysctl -qw net.ipv4.conf.%%i.rp_filter=2\n"
                        printf "PostDown = ip route del default dev %%i table %s 2>/dev/null || true\n", tbl
                    }' "$_tmp" > "$_tmp.conf"
                _dst="$HOST_CONF_DIR/$UPLINK_IF.conf"
                if [ -f "$_dst" ] && cmp -s "$_tmp.conf" "$_dst"; then
                    say "  конфиг аплинка не изменился"
                    UPLINK_STATE="unchanged"
                else
                    UPLINK_STATE="installed"
                    say "  конфиг аплинка обновлён из бандла (Table = off, политика по метке $TG_MARK → таблица $UPLINK_TABLE)"
                    _bak=""
                    if [ -f "$_dst" ]; then
                        _bak="$_dst.bak-$(date +%Y%m%d%H%M%S)"
                        run "cp -p $_dst $_bak"
                    fi
                    run "install -m 600 $_tmp.conf $_dst"
                    run "$AWG_QUICK down $UPLINK_IF 2>/dev/null || true"
                    # Аплинк — связь самого агента с Telegram. Не поднялся —
                    # немедленно назад на прежний конфиг, иначе шлюз режет себе
                    # связь и чинить его придётся руками.
                    if ! "$AWG_QUICK" up "$UPLINK_IF"; then
                        say "  ОШИБКА: аплинк с новым конфигом не поднялся — откатываю на прежний"
                        if [ -n "$_bak" ]; then
                            run "cp -p $_bak $_dst"
                            run "$AWG_QUICK up $UPLINK_IF || true"
                        fi
                        rm -f "$_tmp" "$_tmp.conf"
                        exit 1
                    fi
                    run "systemctl enable awg-quick@$UPLINK_IF 2>/dev/null || true"
                fi
            else
                say "  конфиг аплинка в бандле не разобрался — не трогаю"
            fi
            rm -f "$_tmp" "$_tmp.conf"
        fi
    elif [ -n "$_others" ]; then
        say "  ВНИМАНИЕ: конфигурация слота ($LINK_IF) выпущена ДРУГОМУ устройству"
        say "  (ключ ${GATEWAY_PUBKEY%%????????????????????????????????}…), у этой машины аплинк: $(printf '%s' "$_others" | tr '\n' ' ')."
        say "  Линк слота НЕ поднимаю: трафик пошёл бы мимо помеченного шлюза."
        say "  Агент попросит переслать сообщение основному боту; после пометки"
        say "  перевыпусти конфигурацию слота и примени её ещё раз."
        GW_FOREIGN=1
        GW_STATUS="foreign"
    else
        # Аплинка нет ни живого, ни в конфигах — сказать «шлюз чужой» не по чему.
        # Раньше эта ветка сливалась с «чужим»: линк опускался, юнит выключал
        # сам себя, и машина не вставала обратно даже когда аплинк поднимался.
        say "  ВНИМАНИЕ: аплинка этой машины не видно — ни интерфейса, ни конфига в $HOST_CONF_DIR."
        say "  Подтвердить, что шлюз слота ($LINK_IF) — эта машина, нечем: линк не трогаю."
        say "  Подними аплинк (awg-quick up <имя>) и примени конфигурацию слота ещё раз."
        GW_UNCONFIRMED=1
        GW_STATUS="unconfirmed"
    fi
fi
write_status() {   # $1 = состояние линка
    [ "$MODE" = "plan" ] && return 0
    printf 'GW_STATUS=%s\nGATEWAY_PUBKEY=%s\nUPLINK=%s\nUPLINK_IF=%s\nLINK=%s\nLAN=%s\nLAN_IF=%s\nLAN_ADDR=%s\n' \
        "$GW_STATUS" "$GATEWAY_PUBKEY" "$UPLINK_STATE" "${UPLINK_IF:-}" "$1" \
        "${LAN_MODE:-0}" "${LAN_IF:-}" "${LAN_ADDR:-}" > "$GW_STATUS_FILE"
}
write_status "pending"

# ── 1. конфиг и подъём ───────────────────────────────────────────────────────
[ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || {
    say ""
    say "ОШИБКА: укажи файл конфига, полученный с ВПС:"
    say "  $0 --apply gw-awglink.conf"
    exit 1; }

step "1. Конфиг и подъём $LINK_IF"
mkdir -p "$HOST_CONF_DIR"
# Источник может СОВПАДАТЬ с назначением: так бывает при повторном прогоне
# «поверх» уже установленного конфига. `install` в этом случае падает с «are the
# same file», а при set -e уносит с собой весь остальной обвяз — который как раз
# и надо доставить.
# Конфиг не изменился и линк поднят — НЕ трогаем: рестарт линка рвёт РФ-доступ
# у всех, а ради того же самого конфига рвать нечего (правила ниже идемпотентны).
LINK_SAME=0
if [ -f "$HOST_CONF_DIR/$LINK_IF.conf" ] && cmp -s "$SRC_CONF" "$HOST_CONF_DIR/$LINK_IF.conf"; then
    LINK_SAME=1
fi
if [ "$(readlink -f "$SRC_CONF")" = "$(readlink -f "$HOST_CONF_DIR/$LINK_IF.conf")" ]; then
    say "  конфиг уже на месте — копировать не нужно"
elif [ "$LINK_SAME" = "1" ]; then
    say "  конфиг не изменился — копировать не нужно"
else
    run "install -m 600 '$SRC_CONF' $HOST_CONF_DIR/$LINK_IF.conf"
fi
[ -n "$AWG_QUICK" ] || { say "ОШИБКА: awg-quick не найден на ХОСТЕ."; \
    say "  Собери amneziawg-tools той же версии, что и модуль ядра."; exit 1; }
if [ "$GW_FOREIGN" = "1" ]; then
    # Юнит обвязки НЕ выключаем: он идемпотентен и при следующем старте
    # перепроверит пометку сам. Выключенный юнит означал ручное вмешательство
    # даже там, где машина шлюзом осталась.
    say "  линк не поднимаю (слот помечен другому устройству); если был поднят — опускаю"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    write_status "foreign"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк лежит до пометки этой машины шлюзом слота."
    exit 0
fi
if [ "$GW_UNCONFIRMED" = "1" ]; then
    say "  линк не трогаю (аплинк не найден — шлюз слота не подтверждён)"
    write_status "unconfirmed"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк — как был, до подъёма аплинка."
    exit 0
fi
if ip link show "$LINK_IF" >/dev/null 2>&1 && [ "$LINK_SAME" = "1" ]; then
    say "  интерфейс поднят, конфиг тот же — линк не перезапускаю"
else
    if ip link show "$LINK_IF" >/dev/null 2>&1; then
        say "  интерфейс уже поднят — перезапускаю, чтобы подхватить конфиг"
        run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
        # и в контейнере тоже: линк мог быть поднят прежней версией скрипта
        if [ -n "$CONTAINER" ]; then
            run "docker exec $CONTAINER awg-quick down $LINK_IF 2>/dev/null || true"
        fi
    fi
    run "$AWG_QUICK up $LINK_IF"
fi

# Проверяем ДЕЛОМ, а не по коду возврата: при расхождении версий awg-quick
# создаёт интерфейс, спотыкается на setconf и молча удаляет его обратно —
# завершаясь успешно. Снаружи это выглядит как «скрипт отработал», а линка нет.
if ! ip link show "$LINK_IF" >/dev/null 2>&1; then
    say ""
    say "ОШИБКА: интерфейс $LINK_IF не поднялся."
    say "  Почти всегда это РАСХОЖДЕНИЕ ВЕРСИЙ: модуль ядра и amneziawg-tools"
    say "  из разных поколений. Начиная с v3 параметры H1..H4 передаются как"
    say "  64-битные диапазоны, утилиты v1 шлют 32 бита — netlink отвергает."
    say "  Проверь:  awg --version   и   modinfo amneziawg | head -3"
    exit 1
fi
say "  Интерфейс поднят: $($AWG_BIN show "$LINK_IF" 2>/dev/null | head -1)"

# Подсеть линка берём У ЯДРА, а не из конфига: конфиг мог быть не применён, а
# нам нужно то, что реально назначено. /30 ⇒ сеть считается из адреса.
LINK_CIDR="$(link_cidr_of "$LINK_IF")"
LINK_PEER="$(link_peer_of "$LINK_IF")"
[ -n "$LINK_CIDR" ] && [ -n "$LINK_PEER" ] || { say "ОШИБКА: не удалось определить подсеть $LINK_IF"; exit 1; }
say "  Подсеть линка: $LINK_CIDR, ВПС на линке: $LINK_PEER"

# ── 1a. форвардинг в ядре ────────────────────────────────────────────────────
# Без него правила ниже стоят и не работают: пакет не выйдет из шлюза наружу, а
# ошибки не будет ни одной. Раньше это держалось побочным эффектом docker (он
# выставляет ip_forward при старте) — то есть на удаче: не запустился docker,
# или его вовсе убрали, и шлюз молча перестаёт быть шлюзом.
step "1a. net.ipv4.ip_forward"
if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ]; then
    say "  уже 1"
else
    run "sysctl -w net.ipv4.ip_forward=1"
fi
# Отдельным файлом — иначе значение живёт до перезагрузки, а вернувшийся из
# ребута шлюз выглядит исправным и не пропускает ни пакета.
# rp_filter=2 (loose): ответы из интернета приходят в аплинк, а обратный путь
# до их источника по main лежит через домашний интерфейс — строгая проверка
# такое роняет. Loose пропускает, если маршрут к источнику есть хоть где-то;
# выключать проверку совсем незачем. Аплинк — PostUp его конфига (интерфейса
# на момент применения sysctl ещё нет).
run "printf 'net.ipv4.ip_forward = 1\\nnet.ipv4.conf.all.rp_filter = 2\\nnet.ipv4.conf.default.rp_filter = 2\\n' > $SYSCTL_CONF"
run "sysctl -qw net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2"

# ── 1b. политика «Telegram → аплинк»: правило по метке и таблица ─────────────
# Ставит PostUp аплинка при подъёме, но systemd-networkd при (пере)запуске по
# умолчанию сносит чужие ip rule и маршруты: аплинк жив, метка стоит, а
# Telegram агента уходит домашнему провайдеру. Запрещаем networkd их трогать
# (drop-in читается при его следующем старте — ровно тогда, когда он и снёс
# бы) и перевыставляем политику здесь: реассерт юнита её тоже чинит.
step "1b. Политика Telegram → аплинк"
NETWORKD_DROPIN="/etc/systemd/networkd.conf.d/awg-gw.conf"
if [ -d /etc/systemd ] && command -v networkctl >/dev/null 2>&1; then
    if [ -f "$NETWORKD_DROPIN" ]; then
        say "  drop-in networkd уже есть"
    else
        run "mkdir -p /etc/systemd/networkd.conf.d"
        run "printf '[Network]\nManageForeignRoutes=no\nManageForeignRoutingPolicyRules=no\n' > $NETWORKD_DROPIN"
    fi
    # Сами интерфейсы awg* networkd тоже не должен подхватывать: с ним на
    # каждом перезапуске (любой Apply в OMV) оба туннеля переставали слать
    # пакеты — хендшейк застывал, sent не рос, юниты active. Снаружи это
    # выглядело отказом ВПС.
    if [ -f "$NETWORKD_UNMANAGED" ]; then
        say "  awg* для networkd unmanaged — уже"
    else
        run "mkdir -p /etc/systemd/network"
        run "printf '[Match]\nName=awg*\n\n[Link]\nUnmanaged=yes\n' > $NETWORKD_UNMANAGED"
    fi
else
    say "  systemd-networkd не используется — drop-in не нужен"
fi
if [ -n "${UPLINK_IF:-}" ] && [ "$GW_STATUS" = "confirmed" ] && ip link show "$UPLINK_IF" >/dev/null 2>&1; then
    if ip rule list | grep -q "fwmark $TG_MARK lookup $UPLINK_TABLE"; then
        say "  правило по метке есть"
    else
        run "ip rule add fwmark $TG_MARK lookup $UPLINK_TABLE"
    fi
    run "ip route replace default dev $UPLINK_IF table $UPLINK_TABLE"
else
    say "  аплинк не поднят или шлюз не помечен — политику ставит PostUp при подъёме"
fi

# ── 2. таблица nft — вся обвязка одним атомарным файлом ──────────────────────
step "2. Таблица $GUARD_TABLE"
say "  MASQUERADE $CLIENT_SUBNET и $LINK_CIDR → $WAN_IF: российские сервисы увидят"
say "  домашний адрес; с адреса линка ходит зонд живости с ВПС."
say "  Изоляция: клиентам из туннеля закрыты все приватные сети (NAS, роутер,"
say "  docker, link-local), остальное — транзит наружу."
say "  Защита шлюза: с адресов туннеля на саму машину пускаем только ВПС по"
say "  линку ($LINK_PEER: SSH, ICMP); устройствам админа открыто всё, включая"
say "  домашнюю сеть; прочее дропается."
say "  Метки Telegram ($TG_MARK): агенту нужен Telegram через ВПС."
command -v nft >/dev/null 2>&1 || { say "ОШИБКА: нет nft — apt install nftables"; exit 1; }
ADMIN_ELEMS="$(ipv4_list $ADMIN_IPS $ADMIN_IPS_EXTRA)"
say "  Устройства админа: ${ADMIN_ELEMS:-— (никому, кроме ВПС по линку)}"
if [ "$MODE" = "plan" ]; then
    say "  would: записать $GUARD_FILE и применить: nft -f $GUARD_FILE"
else
mkdir -p "$GW_ETC"
# Маскарад в АПЛИНК — для самого агента. Локально рождённый пакет выбирает
# исходный адрес до метки в output: берёт адрес домашнего интерфейса, а после
# перемаршрутизации по метке улетает в аплинк с чужим src — ВПС его отбросит
# (у пира разрешён только адрес аплинка). Маскарад подменяет src на адрес
# аплинка. Без него агент на чистой машине нем: на прежней малине это делало
# чужое правило домашней схемы, и отсутствие своего не было видно.
UPLINK_MASQ=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MASQ="        oifname \"$UPLINK_IF\" masquerade"
# MSS-кламп в аплинк: транзитный TCP из локальной сети в туннель без него
# упирается в MTU туннеля и виснет на больших ответах. Свойство аплинка, не
# домашнего слоя: агенту с его Telegram он тоже полезен.
UPLINK_MSS=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MSS="        oifname \"$UPLINK_IF\" tcp flags syn / syn,rst tcp option maxseg size set rt mtu"
{
cat <<GUARDEOF
#!/usr/sbin/nft -f
# awg-bot (шлюз): обвязка условной маршрутизации и защита машины от туннеля.
# Генерирует routing-gw-setup.sh при каждом старте юнита awg-link-gw.service —
# правки руками перезапишутся. Устройства админа (полный доступ с туннеля):
# ADMIN_IPS из бандла с ВПС плюс ADMIN_IPS_EXTRA из $FW_ENV (awg-bot firewall).
table $GUARD_TABLE
delete table $GUARD_TABLE
table $GUARD_TABLE {
    set tunnel_nets4 {
        type ipv4_addr
        flags interval
        elements = { $CLIENT_SUBNET, $LINK_CIDR }
    }
    set private4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $PRIVATE_NETS) }
    }
    set tg_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $TG_NETS) }
    }
    set gh_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $GH_NETS) }
    }
    set admin4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$ADMIN_ELEMS" ] && printf '        elements = { %s }\n' "$ADMIN_ELEMS"
cat <<GUARDEOF
    }

    chain input {
        type filter hook input priority filter; policy accept;
        iifname "lo" accept
        ip saddr @tunnel_nets4 jump tunnel_in
    }
    chain tunnel_in {
        ct state established,related accept
        ip protocol icmp accept
        ip saddr @admin4 accept
        ip saddr $LINK_PEER tcp dport $SSH_PORT accept
        drop
    }

    chain forward {
        type filter hook forward priority filter; policy accept;
$UPLINK_MSS
        oifname "$LINK_IF" ct state established,related accept
        iifname "$LINK_IF" ip saddr @admin4 accept
        iifname "$LINK_IF" ip daddr @private4 drop
        iifname "$LINK_IF" accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade
$UPLINK_MASQ
    }

    chain output {
        type route hook output priority mangle; policy accept;
        ip daddr @tg_nets4 meta mark set $TG_MARK
        ip daddr @gh_nets4 meta mark set $TG_MARK
    }
}
GUARDEOF
} > "$GUARD_FILE.tmp"
chmod 0644 "$GUARD_FILE.tmp"
nft -c -f "$GUARD_FILE.tmp" || { say "ОШИБКА: nft отклонил таблицу — $GUARD_FILE.tmp"; exit 1; }
mv "$GUARD_FILE.tmp" "$GUARD_FILE"
run "nft -f $GUARD_FILE"
fi

# ── 3. прежняя обвязка в iptables — снять ────────────────────────────────────
step "3. Снятие прежних правил iptables"
say "  Таблица держит то же самое; два владельца одних правил не нужны."
legacy_cleanup

# ── 4. автозапуск ────────────────────────────────────────────────────────────
step "4. Автозапуск"
SELF="$(install_self)"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк до ВПС и изоляция клиентов (шлюз)
# Зависимости от docker БОЛЬШЕ НЕТ, и это не уборка. Она осталась с тех пор,
# когда линк поднимался утилитами из образа Amnezia; сейчас его поднимает
# хостовой awg-quick. С Requires=docker.service не стартовавший (или снесённый)
# docker уносил за собой весь обвяз шлюза — молча, и обнаруживалось это как
# «интернета за шлюзом нет» уже со стороны ВПС.
After=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}
Wants=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый бандл с ВПС.
Environment=CLIENT_SUBNET=$CLIENT_SUBNET
Environment=LINK_IF=$LINK_IF
# Устройства админа (полный доступ с туннеля) — из бандла с ВПС. Добавленное
# на самом шлюзе (awg-bot firewall allow) — в $FW_ENV.
Environment="ADMIN_IPS=$ADMIN_IPS"
Environment=GATEWAY_PUBKEY=$GATEWAY_PUBKEY
Environment=GATEWAY_PREV_PUBKEY=$GATEWAY_PREV_PUBKEY
Environment=UPLINK_B64=$UPLINK_B64
# Локальная сеть без VPN (docs/gateway-lan.md): флаг, подсети, резолвер ВПС.
Environment=LAN_MODE=$LAN_MODE
Environment="HOME_SUBNETS=$HOME_SUBNETS"
Environment=RESOLVER=$RESOLVER
EnvironmentFile=-$FW_ENV
# Зовём этот же скрипт: он идемпотентен, источник истины один.
ExecStart=$SELF --apply $HOST_CONF_DIR/$LINK_IF.conf
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITEOF
run "systemctl daemon-reload"
run "systemctl enable awg-link-gw.service"
# ── 5. локальная сеть: «за шлюзом — без VPN» (docs/gateway-lan.md, функция A) ─
# Роутер заворачивает весь трафик локальной сети на малину, малина делит его
# сама: заблокированное — по метке аплинка в туннель, остальное — напрямую.
# Резолвер dnsmasq на LAN-адресе с апстримом через аплинк наполняет наборы
# доменами, скрипт списков — подсетями из фидов. Владелец всего ниже — этот
# скрипт: любая ручная правка перезапишется при следующем применении.
#
# Две таблицы nft — не случайность: awg_gw_guard перевыставляется атомарно
# (delete table), а наборы awg_home наполняет dnsmasq на лету, и терять их при
# каждом реассерте нельзя — таблица создаётся БЕЗ delete, меняются только
# цепочки. Метка — та же, что у агента: смысл один («в аплинк»), правило и
# маршрут уже ставит PostUp аплинка, а чинит агент.
step "5. Локальная сеть без VPN"
if [ "$LAN_MODE" = "1" ]; then
    _net="${HOME_SUBNETS%% *}"
    [ -n "$_net" ] || { say "ОШИБКА: LAN_MODE=1 без HOME_SUBNETS — задай локальную подсеть шлюза в боте"; exit 1; }
    _r="$(lan_iface_for "$_net")"; LAN_IF="${_r%% *}"; LAN_ADDR="${_r#* }"; [ "$LAN_ADDR" = "$_r" ] && LAN_ADDR=""
    [ -n "$LAN_IF" ] || { say "ОШИБКА: на этой машине нет адреса из подсети $_net — не та подсеть или не та машина"; exit 1; }
    say "  локальная сеть $_net: интерфейс $LAN_IF, адрес малины $LAN_ADDR"
    [ -n "$UPLINK_IF" ] || { say "ОШИБКА: аплинк не известен — режим без VPN без него не работает"; exit 1; }
    lan_migrate_manual
    # dnsmasq и dig (наполнение набора после ручного добавления домена)
    if ! command -v dnsmasq >/dev/null 2>&1 || ! command -v dig >/dev/null 2>&1; then
        say "  ставлю dnsmasq и dnsutils"
        run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q dnsmasq dnsutils"
    fi
    _upstream="${RESOLVER:-1.1.1.1}"
    [ -n "$RESOLVER" ] || say "  резолвера ВПС нет — апстрим 1.1.1.1 через аплинк (без защиты от DoH и общего кэша)"
    _dn_changed=0
    # ── конфиги dnsmasq: базовый, DoH, персональные (создаются пустыми, не перезаписываются)
    _tmp="$DNSMASQ_D/awg-gw-base.conf.tmp"
    {
        printf '# awg-bot (шлюз): резолвер локальной сети без VPN. Владелец — routing-gw-setup.sh.\n'
        # bind-dynamic: с bind-interfaces демон падает, пока аплинк не поднялся.
        # Однократные ключи (bind-*, cache-size) — только если их нет в других файлах.
        dn_set_elsewhere '^(bind-interfaces|bind-dynamic)$' || printf 'bind-dynamic\n'
        printf 'listen-address=127.0.0.1,%s\n' "$LAN_ADDR"
        printf 'no-resolv\n'
        # апстрим ЧЕРЕЗ АПЛИНК: без @iface запрос ушёл бы линком с адресом линка
        printf 'server=%s@%s\n' "$_upstream" "$UPLINK_IF"
        dn_set_elsewhere '^cache-size=' || printf 'cache-size=10000\n'
        printf 'stop-dns-rebind\nrebind-localhost-ok\n'
    } > "$_tmp"
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-base.conf"; then rm -f "$_tmp"; else
        run "mv -f $_tmp $DNSMASQ_D/awg-gw-base.conf"; _dn_changed=1; fi
    _tmp="$DNSMASQ_D/awg-gw-doh.conf.tmp"
    cat > "$_tmp" <<'DOHEOF'
# awg-bot (шлюз): DoH мимо резолвера — иначе адрес не попадает в набор и
# заблокированное идёт напрямую («часть сайтов через раз»). Тот же список,
# что у резолвера ВПС. Канареечный домен Firefox: NXDOMAIN выключает DoH.
address=/use-application-dns.net/
address=/cloudflare-dns.com/
address=/chrome.cloudflare-dns.com/
address=/mozilla.cloudflare-dns.com/
address=/one.one.one.one/
address=/dns.google/
address=/dns.quad9.net/
address=/dns.adguard-dns.com/
address=/doh.opendns.com/
address=/dns.nextdns.io/
address=/firefox.dns.nextdns.io/
DOHEOF
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-doh.conf"; then rm -f "$_tmp"; else
        run "mv -f $_tmp $DNSMASQ_D/awg-gw-doh.conf"; _dn_changed=1; fi
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] || { run "printf '# awg-bot (шлюз): персональный список — awg-bot lan add/ru/del\\n' > $DNSMASQ_D/$_f"; _dn_changed=1; }
    done
    # Restart и CAP_NET_ADMIN сразу: демон сбрасывает привилегии и без права
    # на nft резолвит, а наборы остаются пустыми — молча.
    if [ ! -f "$DNSMASQ_OVR" ]; then
        run "mkdir -p $(dirname "$DNSMASQ_OVR")"
        run "printf '[Service]\\nRestart=on-failure\\nRestartSec=5\\nAmbientCapabilities=CAP_NET_ADMIN\\n' > $DNSMASQ_OVR"
        run "systemctl daemon-reload"; _dn_changed=1
    fi
    # ── sysctl: rp_filter loose и без IPv6 на LAN (AAAA увёл бы трафик мимо туннеля)
    run "printf 'net.ipv4.conf.$LAN_IF.rp_filter = 2\\nnet.ipv6.conf.$LAN_IF.disable_ipv6 = 1\\n' > $LAN_SYSCTL"
    run "sysctl -qw net.ipv4.conf.$LAN_IF.rp_filter=2 net.ipv6.conf.$LAN_IF.disable_ipv6=1"
    # ── таблица awg_home: наборы без delete (переживают реассерт), цепочки заново
    {
        cat <<HOMEEOF
# awg-bot (шлюз): локальная сеть без VPN. Владелец — routing-gw-setup.sh.
# Без «delete table»: наборы наполняет dnsmasq на лету, их нельзя терять при реассерте.
table $HOME_TABLE {
    set lan_vpn4      { type ipv4_addr; flags interval; auto-merge; }
    set lan_vpn_nets4 { type ipv4_addr; flags interval; auto-merge; }
    set lan_ru4       { type ipv4_addr; flags interval; auto-merge; }
    chain prerouting  { type filter hook prerouting priority mangle; policy accept; }
    chain input       { type filter hook input priority filter; policy accept; }
    chain forward     { type filter hook forward priority filter; policy accept; }
    chain postrouting { type nat hook postrouting priority srcnat; policy accept; }
}
flush chain $HOME_TABLE prerouting
flush chain $HOME_TABLE input
flush chain $HOME_TABLE forward
flush chain $HOME_TABLE postrouting
# из линка и аплинка — мимо маркировки; исключения ВЫШЕ метки (accept обрывает обход)
add rule $HOME_TABLE prerouting iifname != "$LAN_IF" accept
# счётчик для агента: заворот с роутера (всё из LAN наружу, до любых вердиктов)
add rule $HOME_TABLE prerouting ip daddr != $_net counter
add rule $HOME_TABLE prerouting ip daddr @lan_ru4 accept
add rule $HOME_TABLE prerouting ip daddr @lan_vpn_nets4 meta mark set $TG_MARK
add rule $HOME_TABLE prerouting ip daddr @lan_vpn4 meta mark set $TG_MARK
# счётчик для агента: DNS с роутера (раздаёт ли DHCP адрес малины)
add rule $HOME_TABLE input iifname "$LAN_IF" udp dport 53 counter
# DoT мимо резолвера — как на ВПС для клиентов
add rule $HOME_TABLE forward iifname "$LAN_IF" tcp dport 853 drop
# ответ на прямой трафик должен вернуться на малину, а не на клиента мимо неё
add rule $HOME_TABLE postrouting ip saddr $_net oifname "$LAN_IF" masquerade
HOMEEOF
    } > "$HOME_FILE.tmp"
    chmod 0644 "$HOME_FILE.tmp"
    nft -c -f "$HOME_FILE.tmp" || { say "ОШИБКА: nft отклонил таблицу локальной сети — $HOME_FILE.tmp"; exit 1; }
    mv "$HOME_FILE.tmp" "$HOME_FILE"
    run "nft -f $HOME_FILE"
    # слепки наборов — до фидов: первая минута после загрузки не остаётся без списков
    for _s in lan_vpn4 lan_vpn_nets4; do
        [ -s "$LAN_DUMP/$_s.nft" ] && run "nft -f $LAN_DUMP/$_s.nft 2>/dev/null || true"
    done
    write_lan_scripts
    if [ "$_dn_changed" = "1" ] || ! systemctl is-active --quiet dnsmasq; then
        run "systemctl enable dnsmasq 2>/dev/null || true"
        run "systemctl restart dnsmasq"
    else
        say "  dnsmasq: конфиги не изменились"
    fi
    say "  списки — обновляет агент (первый раз через несколько минут); руками: $LAN_LISTS"
else
    if [ -f "$HOME_FILE" ] || [ -f "$DNSMASQ_D/awg-gw-base.conf" ]; then
        say "  выключено — снимаю своё"
        lan_remove
    else
        say "  выключено"
    fi
fi

write_status "up"
step "Проверка"
say "  awg show $LINK_IF                            # есть ли хендшейк"
say "  ip -br addr show $LINK_IF"
say "  nft list table $GUARD_TABLE                  # вся обвязка одним взглядом"
say ""
say "Хендшейк появится, когда ВПС ответит; статус — в панели агента."
