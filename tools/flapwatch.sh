#!/usr/bin/env bash
#
# flapwatch.sh — фоновый сторож «рвётся связь»: раз в N секунд снимает всё,
# что под подозрением, и при первом же отклонении пишет снимок в журнал и
# ВЫХОДИТ с однозначным кодом. Запуск на ВПС:
#
#   nohup bash tools/flapwatch.sh 10.9.1.15 > /var/log/flapwatch.out 2>&1 &
#   tail -f /var/log/flapwatch.log            # что видит
#   cat /var/log/flapwatch.exit               # код и причина после выхода
#
# Аргумент — адрес НАБЛЮДАЕМОГО устройства в туннеле (на котором рвётся).
# Переменные: INTERVAL (сек, 5), LINK_IF (awglink), CLIENT_IF (awg1),
# LINK_PEER (10.99.99.2), USER_SET (vpn_u2), LOG (/var/log/flapwatch.log).
#
# КОДЫ ВЫХОДА (что случилось первым):
#   10  линк: эндпоинт пира сменился
#   11  линк: хендшейк старше 5 минут при идущем трафике
#   12  линк: потери в пинге до шлюза ≥ 20% за окно
#   13  линк: перестал считать байты (нет трафика 60 с при живом устройстве)
#   20  awg клиентов: TX dropped растёт (пакеты к клиенту нечем шифровать)
#   21  устройство: хендшейк старше 5 минут при идущем трафике
#   22  устройство: эндпоинт сменился (роуминг/переподключение)
#   30  conntrack ≥ 90% лимита
#   40  dnsmasq: перезапуск/падение
#   50  nft: таблица awg_bot_guard переприменена (сменился handle/текст)
#   51  маршрутизация: цепочка AWGBOT_RT пересобрана (счётчики сброшены)
#   52  маршрутизация: ip rule по метке ИСЧЕЗ (был на старте)
#   53  маршрутизация: устройство ВЫПАЛО из своего rt_src-набора (было)
#   60  устройство: шторм новых соединений (реконнекты) — ≥ NEWSTORM (300) за окно;
#       браузер за раз открывает десятки, порог намеренно высокий
#   61  устройство: ≥ MISS_EXIT новых адресов мимо набора за шаг (0 = только
#       писать в журнал: заграница и должна идти мимо, это не отклонение;
#       смотри строки «мимо набора» и проверяй, российские ли там адреса)
#   70  ядро: новые сообщения про awg/conntrack/link
#   71  бот: в журнале restart/syncconf/applied/переезд
#   80  хост: load average выше 2×ядер
#
set -u
ME="${1:?адрес устройства в туннеле, напр. 10.9.1.15}"
INTERVAL="${INTERVAL:-5}"
LINK_IF="${LINK_IF:-awglink}"
CLIENT_IF="${CLIENT_IF:-awg1}"
LINK_PEER="${LINK_PEER:-10.99.99.2}"
USER_SET="${USER_SET:-vpn_u2}"
LOG="${LOG:-/var/log/flapwatch.log}"
EXITF="${EXITF:-/var/log/flapwatch.exit}"
NEWSTORM="${NEWSTORM:-300}"
MISS_EXIT="${MISS_EXIT:-0}"

ts()  { date '+%F %T'; }
log() { printf '%s %s\n' "$(ts)" "$*" >> "$LOG"; }

snapshot() {   # полный снимок в журнал — в момент отклонения
    {
        echo "──────── СНИМОК $(ts): $1"
        echo "· awg show $LINK_IF"; awg show "$LINK_IF" 2>&1 | sed 's/^/    /'
        echo "· awg show $CLIENT_IF (пир устройства)"
        awg show "$CLIENT_IF" 2>&1 | awk -v me="$ME" 'BEGIN{RS="";ORS="\n\n"} $0 ~ "allowed ips: "me"/32"' | sed 's/^/    /'
        echo "· ip -s link $CLIENT_IF"; ip -s link show "$CLIENT_IF" 2>&1 | sed 's/^/    /'
        echo "· conntrack"; sysctl -n net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max 2>&1 | tr '\n' ' '; echo
        echo "· соединения устройства (dst, счёт, в наборе?)"
        # только исходный кортеж (первый dst=): во втором у маскарадных потоков
        # стоит публичный адрес самого ВПС, и он считался бы «соединениями с ВПС»
        conntrack -L -s "$ME" 2>/dev/null | grep -oE 'dst=[0-9.]+' | awk 'NR%2==1' | cut -d= -f2 | sort | uniq -c | sort -rn | head -20 \
            | while read -r n ip; do
                inset=мимо; ipset test "$USER_SET" "$ip" >/dev/null 2>&1 && inset=В_НАБОРЕ
                printf '    %5s  %-16s %s\n' "$n" "$ip" "$inset"
              done
        echo "· ip rule / route"; ip rule show 2>&1 | grep -i fwmark | sed 's/^/    /'; ip route show table all 2>/dev/null | grep "$LINK_IF" | head -3 | sed 's/^/    /'
        echo "· AWGBOT_RT"; iptables -t mangle -S AWGBOT_RT -v 2>&1 | sed 's/^/    /'
        echo "· dnsmasq"; systemctl is-active dnsmasq 2>&1 | sed 's/^/    /'
        echo "· журнал бота (5 мин)"; journalctl -u awg-bot --since '-5min' --no-pager 2>/dev/null | tail -15 | sed 's/^/    /'
        echo "· ядро (5 мин)"; journalctl -k --since '-5min' --no-pager 2>/dev/null | tail -10 | sed 's/^/    /'
        echo "────────"
    } >> "$LOG"
}

bail() {   # bail КОД ПРИЧИНА
    log "!!! ОТКЛОНЕНИЕ [$1]: $2"
    snapshot "$2"
    printf '%s\n%s %s\n' "$1" "$(ts)" "$2" > "$EXITF"
    exit "$1"
}

# ── снятие показателей ───────────────────────────────────────────────────────
link_peer_line() { awg show "$LINK_IF" endpoints 2>/dev/null | head -1; }
link_hs_age()    { local t; t="$(awg show "$LINK_IF" latest-handshakes 2>/dev/null | awk '{print $2}' | head -1)"; [[ -n "$t" && "$t" != 0 ]] && echo $(( $(date +%s) - t )) || echo 999999; }
link_bytes()     { awg show "$LINK_IF" transfer 2>/dev/null | awk '{print $2+$3}' | head -1; }
dev_pub()        { awg show "$CLIENT_IF" allowed-ips 2>/dev/null | awk -v me="$ME/32" '$2==me{print $1}' | head -1; }
dev_hs_age()     { local t; t="$(awg show "$CLIENT_IF" latest-handshakes 2>/dev/null | awk -v k="$1" '$1==k{print $2}')"; [[ -n "$t" && "$t" != 0 ]] && echo $(( $(date +%s) - t )) || echo 999999; }
dev_endpoint()   { awg show "$CLIENT_IF" endpoints 2>/dev/null | awk -v k="$1" '$1==k{print $2}'; }
dev_bytes()      { awg show "$CLIENT_IF" transfer 2>/dev/null | awk -v k="$1" '$1==k{print $2+$3}'; }
tx_dropped()     { ip -s link show "$CLIENT_IF" 2>/dev/null | awk '/TX:/{getline; print $4}'; }
ct_pct()         { local c m; c="$(sysctl -n net.netfilter.nf_conntrack_count 2>/dev/null || echo 0)"; m="$(sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 1)"; echo $(( c * 100 / m )); }
dnsmasq_pid()    { systemctl show -p MainPID --value dnsmasq 2>/dev/null; }
guard_handle()   { nft -a list table inet awg_bot_guard 2>/dev/null | md5sum | cut -c1-12; }
rt_rules()       { iptables -t mangle -S AWGBOT_RT 2>/dev/null | grep -c '^-A'; }
rt_counters()    { iptables -t mangle -L AWGBOT_RT -v -n -x 2>/dev/null | awk 'NR>2{s+=$1} END{print s+0}'; }
fw_rule()        { ip rule show 2>/dev/null | grep -c fwmark; }
in_srcset()      { local s; for s in $(ipset list -n 2>/dev/null | grep '^rt_src_'); do ipset test "$s" "$ME" >/dev/null 2>&1 && { echo 1; return; }; done; echo 0; }
kmsgs()          { journalctl -k --since "-$((INTERVAL + 2))sec" --no-pager 2>/dev/null | grep -ciE 'amnezia|awg|conntrack|link (is )?(up|down)' ; }
botmsgs()        { journalctl -u awg-bot --since "-$((INTERVAL + 2))sec" --no-pager 2>/dev/null | grep -iE 'restart|syncconf|awg_bot_guard — (applied|restored|resynced)|переезд|rebuild' | head -3; }
load_hi()        { local l n; l="$(cut -d' ' -f1 /proc/loadavg)"; n="$(nproc)"; awk -v l="$l" -v n="$n" 'BEGIN{exit !(l > 2*n)}'; }
new_flows()      { timeout "$INTERVAL" conntrack -E -e NEW -s "$ME" 2>/dev/null | grep -oE 'dst=[0-9.]+' | awk 'NR%2==1' | cut -d= -f2; }
is_private()     { [[ "$1" =~ ^10\. || "$1" =~ ^192\.168\. || "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || "$1" =~ ^127\. ]]; }

PUB="$(dev_pub)"
[[ -n "$PUB" ]] || { echo "устройство $ME не найдено среди пиров $CLIENT_IF" >&2; exit 2; }
VPS_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')"

log "старт: устройство $ME ($PUB), линк $LINK_IF → $LINK_PEER, набор $USER_SET, шаг ${INTERVAL}с"
p_ep="$(link_peer_line)"; p_lb="$(link_bytes)"; p_lb_t=$(date +%s)
p_dep="$(dev_endpoint "$PUB")"; p_dbytes="$(dev_bytes "$PUB")"
p_txd="$(tx_dropped)"; p_dns="$(dnsmasq_pid)"; p_guard="$(guard_handle)"
p_rtn="$(rt_rules)"; p_rtc="$(rt_counters)"
p_fw="$(fw_rule)"; p_src="$(in_srcset)"
log "старт: ip rule fwmark=$p_fw, устройство в rt_src=$p_src, dnsmasq pid=$p_dns, TX dropped=$p_txd"

while :; do
    # пинг линка — фоном на длину шага, одновременно с ловлей NEW-соединений
    ping -i 0.5 -w "$INTERVAL" -q "$LINK_PEER" > /tmp/flapwatch.ping 2>&1 &
    PPID_=$!
    mapfile -t news < <(new_flows)
    wait "$PPID_" 2>/dev/null
    loss="$(grep -oE '[0-9]+% packet loss' /tmp/flapwatch.ping | grep -oE '^[0-9]+')"

    # ── линк ────────────────────────────────────────────────────────────────
    ep="$(link_peer_line)"
    [[ "$ep" == "$p_ep" ]] || bail 10 "эндпоинт линка: '$p_ep' → '$ep'"
    age="$(link_hs_age)"
    [[ -n "$loss" && "$loss" -ge 20 ]] && bail 12 "потери до $LINK_PEER: ${loss}% за ${INTERVAL}с"
    lb="$(link_bytes)"
    if [[ "$lb" == "$p_lb" ]]; then
        (( $(date +%s) - p_lb_t > 60 )) && [[ "$(dev_bytes "$PUB")" != "$p_dbytes" ]] \
            && bail 13 "линк не считает байты 60с, а устройство шлёт"
    else
        # трафик по линку идёт — хендшейк обязан обновляться (~2 мин)
        (( age > 300 )) && bail 11 "хендшейк линка ${age}с назад при идущем трафике"
        p_lb="$lb"; p_lb_t=$(date +%s)
    fi

    # ── устройство и awg клиентов ───────────────────────────────────────────
    txd="$(tx_dropped)"; (( txd > p_txd )) && bail 20 "TX dropped $CLIENT_IF: $p_txd → $txd"
    dbytes="$(dev_bytes "$PUB")"
    dage="$(dev_hs_age "$PUB")"
    (( dage > 300 )) && [[ "$dbytes" != "$p_dbytes" ]] && bail 21 "хендшейк устройства ${dage}с назад при идущем трафике"
    dep="$(dev_endpoint "$PUB")"
    [[ "$dep" == "$p_dep" ]] || bail 22 "эндпоинт устройства: '$p_dep' → '$dep'"
    p_dbytes="$dbytes"

    # ── хост ────────────────────────────────────────────────────────────────
    pct="$(ct_pct)"; (( pct >= 90 )) && bail 30 "conntrack ${pct}% лимита"
    dns="$(dnsmasq_pid)"; [[ "$dns" == "$p_dns" ]] || bail 40 "dnsmasq перезапущен: pid $p_dns → $dns"
    g="$(guard_handle)"; [[ "$g" == "$p_guard" ]] || bail 50 "таблица awg_bot_guard изменилась ($p_guard → $g)"
    rtn="$(rt_rules)"; rtc="$(rt_counters)"
    { [[ "$rtn" != "$p_rtn" ]] || (( rtc < p_rtc )); } && bail 51 "AWGBOT_RT пересобрана: правил $p_rtn→$rtn, счётчик $p_rtc→$rtc"
    p_rtc="$rtc"
    fw="$(fw_rule)"; [[ "$p_fw" != 0 && "$fw" == 0 ]] && bail 52 "ip rule по fwmark исчез"
    src="$(in_srcset)"; [[ "$p_src" == 1 && "$src" == 0 ]] && bail 53 "$ME выпало из rt_src-набора — не маркируется"
    p_fw="$fw"; p_src="$src"
    load_hi && bail 80 "load average выше 2×ядер: $(cut -d' ' -f1-3 /proc/loadavg)"

    # ── соединения устройства ───────────────────────────────────────────────
    n=${#news[@]}
    (( n >= NEWSTORM )) && bail 60 "шторм соединений: $n NEW за ${INTERVAL}с"
    miss=()
    for ip in $(printf '%s\n' "${news[@]}" | sort -u); do
        is_private "$ip" && continue
        [[ "$ip" == "$VPS_IP" ]] && continue
        ipset test "$USER_SET" "$ip" >/dev/null 2>&1 || miss+=("$ip")
    done
    if (( ${#miss[@]} )); then
        log "мимо набора: ${miss[*]}"
        (( MISS_EXIT > 0 && ${#miss[@]} >= MISS_EXIT )) && bail 61 "≥$MISS_EXIT новых адресов мимо набора за шаг: ${miss[*]}"
    fi

    # ── журналы ─────────────────────────────────────────────────────────────
    k="$(kmsgs)"; (( k > 0 )) && bail 70 "ядро: $k сообщений про awg/conntrack/link"
    b="$(botmsgs)"; [[ -n "$b" ]] && bail 71 "бот: $b"

    log "ok: линк hs=${age}с ep=${ep##* } loss=${loss:-?}% · устр hs=${dage}с new=$n · ct=${pct}% txd=$txd"
done
