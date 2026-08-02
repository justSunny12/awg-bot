#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-lists-update.sh — наполнение базового набора «кому нужна заграница».
# Запускается НА ВПС по таймеру. См. docs/conditional-routing.md.
#
# ЗАЧЕМ. Правило маркировки работает ОТ ОБРАТНОГО: всё, чего нет в наборе
# vpn_base, уходит на шлюз и выходит с российского адреса. Значит пустой набор
# означает «на шлюз уходит АБСОЛЮТНО всё» — включая заблокированные сайты,
# которые с российского адреса не откроются. Наполнение набора не украшение,
# а условие работоспособности.
#
# ДВА ИСТОЧНИКА, и это важно:
#   • подсети — грузятся в ipset НАПРЯМУЮ, без участия DNS. Это основной путь:
#     он не зависит ни от резолвера, ни от кэша, ни от DoH у клиента;
#   • домены — через директивы dnsmasq, наполняются по мере резолва. Дополняют
#     подсети там, где адреса меняются.
#
# ЗАПУСК:
#   sudo sh routing-lists-update.sh            # обновить сейчас
#   sudo sh routing-lists-update.sh --install  # + поставить таймер (раз в 6 ч)
# ─────────────────────────────────────────────────────────────────────────────

set -e

SET_BASE="${SET_BASE:-vpn_base}"
DNSMASQ_CONF="${DNSMASQ_CONF:-/etc/dnsmasq.d/awgbot-vpn-domains.conf}"
DNSMASQ_SERVICE="${DNSMASQ_SERVICE:-dnsmasq}"
DOMAINS_URL="${DOMAINS_URL:-https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Russia/inside-dnsmasq-ipset.lst}"
SUBNET_SVCS="${SUBNET_SVCS:-telegram meta twitter cloudflare discord}"
SUBNET_URL_TPL="${SUBNET_URL_TPL:-https://raw.githubusercontent.com/itdoginfo/allow-domains/main/Subnets/IPv4/%s.lst}"
GOOGLE_URL="${GOOGLE_URL:-https://www.gstatic.com/ipranges/goog.json}"

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }
log() { printf '[lists] %s\n' "$*"; }

if [ "${1:-}" = "--install" ]; then
    SELF="$(readlink -f "$0")"
    cat > /etc/systemd/system/awg-bot-lists.service <<UNITEOF
[Unit]
Description=awg-bot: обновление списков условной маршрутизации
# Wants, а не только After: без него цель в транзакцию не втягивается и
# упорядочивание превращается в no-op — сервис может стартовать до сети.
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=$SELF
UNITEOF
    cat > /etc/systemd/system/awg-bot-lists.timer <<'TIMEREOF'
[Unit]
Description=awg-bot: обновление списков раз в 6 часов

[Timer]
OnBootSec=2min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
TIMEREOF
    systemctl daemon-reload
    systemctl enable --now awg-bot-lists.timer
    log "таймер поставлен: systemctl list-timers | grep awg-bot-lists"
fi

ipset create "$SET_BASE" hash:net -exist

# ── домены → dnsmasq ─────────────────────────────────────────────────────────
TMP_CONF="$(mktemp)"
if curl -sf --max-time 60 "$DOMAINS_URL" -o "$TMP_CONF" && [ -s "$TMP_CONF" ]; then
    # список отдаётся с чужим именем набора — подставляем своё
    sed -E "s#^ipset=(/[^/]+/).*#ipset=\1$SET_BASE#" "$TMP_CONF" > "$TMP_CONF.fixed"
    if ! cmp -s "$TMP_CONF.fixed" "$DNSMASQ_CONF" 2>/dev/null; then
        mv "$TMP_CONF.fixed" "$DNSMASQ_CONF"
        # именно restart: SIGHUP перечитывает hosts и чистит кэш, но НЕ конфиг —
        # новые директивы ipset= через reload не подхватываются
        systemctl restart "$DNSMASQ_SERVICE"
        log "домены обновлены ($(grep -c '^ipset=' "$DNSMASQ_CONF") директив), dnsmasq перезапущен"
    else
        rm -f "$TMP_CONF.fixed"
        log "домены не изменились — dnsmasq не трогаем"
    fi
else
    log "ВНИМАНИЕ: список доменов не скачался, прежний оставлен"
fi
rm -f "$TMP_CONF"

# ── подсети → ipset напрямую (без DNS) ───────────────────────────────────────
TMP_NETS="$(mktemp)"
for svc in $SUBNET_SVCS; do
    url="$(printf "$SUBNET_URL_TPL" "$svc")"
    curl -sf --max-time 60 "$url" >> "$TMP_NETS" 2>/dev/null || true
    echo >> "$TMP_NETS"
done
curl -sf --max-time 60 "$GOOGLE_URL" 2>/dev/null \
    | grep -oE '"ipv4Prefix":[[:space:]]*"[0-9./]+"' \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' >> "$TMP_NETS" || true

COUNT=0
grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' "$TMP_NETS" | sort -u | while read -r net; do
    ipset add "$SET_BASE" "$net" -exist 2>/dev/null || true
done
COUNT="$(grep -cE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' "$TMP_NETS" || true)"
rm -f "$TMP_NETS"
log "подсетей залито: $COUNT"

log "в наборе $SET_BASE записей: $(ipset list "$SET_BASE" | grep -c '^[0-9]' || true)"
log "готово"
