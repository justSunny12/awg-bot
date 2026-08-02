#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-host-setup.sh — базовый обвяз ВПС под условную маршрутизацию.
# См. docs/conditional-routing.md, §11 (этап 0).
#
# СТАВИТСЯ ОДИН РАЗ. Бот этот обвяз не трогает и на горячую не меняет: править
# базовый NAT боевого сервера из бота ради фичи неоправданно. Бот лишь проверяет
# наличие обвяза (routing.self_check) и отказывается подниматься без него.
#
# ЧТО ДЕЛАЕТ:
#   1) dummy-интерфейс под dnsmasq (свой адрес, не зависит от докер-сетей);
#   2) конфиг dnsmasq: слушать ТОЛЬКО на нём (0.0.0.0 перекрыл бы 127.0.0.53
#      systemd-resolved и сломал резолвинг самого сервера);
#   3) DNAT :53 клиентской подсети на dnsmasq — чтобы выданные конфиги с
#      DNS=1.1.1.1 продолжали работать без перевыпуска ссылок;
#   4) MASQUERADE клиентской подсети и разрешения FORWARD — трафик, выпущенный
#      из контейнера немаскараженным, должен выйти наружу и вернуться;
#   5) маршрут до клиентской подсети через контейнер — для обратного трафика.
#
# ЧЕГО НЕ ДЕЛАЕТ: не поднимает линк-туннель до шлюза (отдельный шаг, требует
# настройки на обеих сторонах) и не трогает контейнер Amnezia.
#
# ЗАПУСК:
#   sudo sh routing-host-setup.sh                 # показать план, ничего не менять
#   sudo sh routing-host-setup.sh --apply         # применить
#   sudo sh routing-host-setup.sh --install-unit  # закрепить от ребута (systemd)
#   sudo sh routing-host-setup.sh --rollback      # откатить всё, что поставил
#
# Идемпотентно: повторный --apply ничего не дублирует.
# ─────────────────────────────────────────────────────────────────────────────

set -e

# ── параметры (сверить с conf/app.yaml → routing:) ───────────────────────────
CLIENT_SUBNET="${CLIENT_SUBNET:-10.8.1.0/24}"
DNS_IF="${DNS_IF:-awgdns0}"          # dummy-интерфейс под dnsmasq
DNS_ADDR="${DNS_ADDR:-10.255.53.1}"  # адрес на нём; DNAT ведёт сюда
DNSMASQ_CONF="/etc/dnsmasq.d/awgbot-base.conf"
DNSMASQ_SERVICE="${DNSMASQ_SERVICE:-dnsmasq}"
UPSTREAM1="${UPSTREAM1:-1.1.1.1}"
UPSTREAM2="${UPSTREAM2:-9.9.9.9}"
CONTAINER="${CONTAINER:-amnezia-awg2}"

MODE="plan"
case "${1:-}" in
    --apply)        MODE="apply" ;;
    --rollback)     MODE="rollback" ;;
    --install-unit) MODE="unit" ;;
    ""|--plan)      MODE="plan" ;;
    -h|--help)      sed -n '2,31p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n── %s\n' "$*"; }
run()  {
    if [ "$MODE" = "apply" ] || [ "$MODE" = "rollback" ]; then
        printf '  $ %s\n' "$*"
        sh -c "$*"
    else
        printf '  would: %s\n' "$*"
    fi
}
# правило ставим, только если его ещё нет: иначе повторный запуск размножит их
ensure_rule() {   # $1 = таблица, $2... = спецификация правила
    _t="$1"; shift
    if iptables -t "$_t" -C "$@" 2>/dev/null; then
        say "  уже есть: iptables -t $_t $*"
    else
        run "iptables -t $_t -I $*"
    fi
}
drop_rule() {
    _t="$1"; shift
    while iptables -t "$_t" -C "$@" 2>/dev/null; do
        run "iptables -t $_t -D $*"
    done
}

# ── адрес контейнера: куда маршрутизировать обратный трафик ──────────────────
container_ip() {
    docker inspect -f \
        '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$CONTAINER" \
        2>/dev/null | awk '{print $1}'
}

# Ретраи нужны для запуска из systemd на старте хоста: docker.service уже поднят,
# а контейнер ещё стартует, и адреса у него пока нет. При ручном запуске первая
# же попытка успешна и задержки не будет.
CONT_IP=""
_try=0
while [ -z "$CONT_IP" ] && [ "$_try" -lt 15 ]; do
    CONT_IP="$(container_ip || true)"
    [ -n "$CONT_IP" ] && break
    _try=$((_try + 1))
    [ "$_try" = 1 ] && say "жду контейнер $CONTAINER..."
    sleep 2
done
if [ -z "$CONT_IP" ]; then
    say "ОШИБКА: не удалось узнать адрес контейнера $CONTAINER."
    say "Проверь: docker inspect $CONTAINER"
    exit 1
fi

say "Параметры:"
say "  клиентская подсеть : $CLIENT_SUBNET"
say "  адрес контейнера   : $CONT_IP"
say "  dnsmasq            : $DNS_ADDR на $DNS_IF"
say "  апстримы DNS       : $UPSTREAM1, $UPSTREAM2"
[ "$MODE" = "plan" ] && { say ""; say "(режим показа: ничего не меняется, добавь --apply)"; }

# ─────────────────────────────────────────────────────────────────────────────
if [ "$MODE" = "unit" ]; then
    SELF="$(readlink -f "$0")"
    UNIT=/etc/systemd/system/awg-bot-routing.service
    step "Юнит автоприменения → $UNIT"
    say "  Правила iptables, адрес на dummy и sysctl эфемерны и ребут не переживают."
    say "  Вместо дублирования их в netfilter-persistent юнит просто зовёт ЭТОТ же"
    say "  скрипт: он идемпотентен, и источник истины остаётся один."
    cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: обвяз условной маршрутизации (хост)
# Требуется docker: адрес контейнера и маршрут к нему вычисляются из него же.
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SELF --apply
# Контейнер может стартовать дольше docker.service — скрипт его ждёт сам,
# но при совсем долгом старте даём повторить.
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
    systemctl enable awg-bot-routing.service
    say ""
    say "Готово. Проверить: systemctl status awg-bot-routing"
    exit 0
fi

if [ "$MODE" = "rollback" ]; then
    step "Откат"
    drop_rule nat PREROUTING -s "$CLIENT_SUBNET" -p udp --dport 53 \
              -j DNAT --to-destination "$DNS_ADDR:53"
    drop_rule nat PREROUTING -s "$CLIENT_SUBNET" -p tcp --dport 53 \
              -j DNAT --to-destination "$DNS_ADDR:53"
    drop_rule nat POSTROUTING -s "$CLIENT_SUBNET" -j MASQUERADE
    drop_rule filter FORWARD -s "$CLIENT_SUBNET" -j ACCEPT
    drop_rule filter FORWARD -d "$CLIENT_SUBNET" -j ACCEPT
    run "ip route del $CLIENT_SUBNET via $CONT_IP 2>/dev/null || true"
    run "rm -f $DNSMASQ_CONF"
    run "rm -rf /etc/systemd/system/${DNSMASQ_SERVICE}.service.d/ipset.conf"
    run "systemctl daemon-reload"
    run "systemctl restart ${DNSMASQ_SERVICE} 2>/dev/null || true"
    run "ip link del $DNS_IF 2>/dev/null || true"
    run "systemctl disable --now awg-bot-routing.service 2>/dev/null || true"
    run "rm -f /etc/systemd/system/awg-bot-routing.service"
    run "systemctl daemon-reload"
    say ""
    say "Готово. Обвяз снят (линк-туннель и контейнер не трогались)."
    exit 0
fi

# 1) dummy-интерфейс под dnsmasq
step "1. Интерфейс $DNS_IF ($DNS_ADDR)"
if ip link show "$DNS_IF" >/dev/null 2>&1; then
    say "  уже есть"
else
    run "ip link add $DNS_IF type dummy"
fi
if ip addr show "$DNS_IF" 2>/dev/null | grep -q "$DNS_ADDR"; then
    say "  адрес уже назначен"
else
    run "ip addr add $DNS_ADDR/32 dev $DNS_IF"
fi
run "ip link set $DNS_IF up"

# 2) конфиг dnsmasq
step "2. Конфиг dnsmasq → $DNSMASQ_CONF"
say "  listen-address только на $DNS_ADDR: 0.0.0.0 перекрыл бы 127.0.0.53"
say "  systemd-resolved и сломал бы резолвинг самого сервера"
say "  stop-dns-rebind ОБЯЗАТЕЛЕН: без него пользователь добавит домен,"
say "  указывающий на приватный адрес, и уедет в чужую сеть через туннель"
if [ "$MODE" = "apply" ]; then
    cat > "$DNSMASQ_CONF" <<CONF
# Сгенерировано routing-host-setup.sh. Базовая часть; списки доменов бот пишет
# отдельным файлом (см. conf/app.yaml → routing.dnsmasq_conf).
bind-interfaces
listen-address=$DNS_ADDR
no-resolv
server=$UPSTREAM1
server=$UPSTREAM2
# защита от DNS rebinding: ответ с приватным адресом отбрасывается. Личные
# списки позволяют пользователю добавить свой домен, поэтому это не паранойя.
stop-dns-rebind
# канареечный домен Firefox: NXDOMAIN отключает у него DoH, иначе он резолвит
# мимо нас и наборы не наполняются
address=/use-application-dns.net/
cache-size=10000
CONF
    printf '  записан\n'
else
    printf '  would: записать %s\n' "$DNSMASQ_CONF"
fi

# Пакет dnsmasq-base даёт только бинарь (его тянут libvirt/lxd/NetworkManager),
# systemd-юнита в нём нет. Проверяем ПОСЛЕ записи конфига намеренно: полный
# пакет при установке сразу стартует сервис, и если конфига ещё нет — займёт
# :53 на всех адресах и перебьёт systemd-resolved. С уже лежащим конфигом
# (bind-interfaces + listen-address) он поднимется только на нашем адресе.
if ! systemctl list-unit-files 2>/dev/null | grep -q "^${DNSMASQ_SERVICE}\.service"; then
    say ""
    say "СТОП: юнита ${DNSMASQ_SERVICE}.service нет — установлен только dnsmasq-base."
    say ""
    say "Конфиг и интерфейс уже на месте, поэтому ставить пакет теперь безопасно:"
    say "    apt install -y dnsmasq"
    say "и запустить этот скрипт ещё раз — он доделает шаги 3-6 (идемпотентен)."
    exit 0
fi
# CAP_NET_ADMIN: без него директивы ipset= выполняются ВХОЛОСТУЮ. Демон
# стартует от root и сбрасывает привилегии до пользователя dnsmasq; capability
# для записи в наборы при этом теряется. Ошибки нет ни одной — dnsmasq исправно
# резолвит, наборы просто остаются пустыми, маркировать нечего, и трафик молча
# идёт мимо шлюза. Диагностируется только сравнением «резолв есть, набор пуст».
step "2a. CAP_NET_ADMIN для dnsmasq"
say "  Возвращаем ровно одну привилегию, а не запускаем демон от root."
if [ "$MODE" = "apply" ]; then
    mkdir -p /etc/systemd/system/${DNSMASQ_SERVICE}.service.d
    cat > /etc/systemd/system/${DNSMASQ_SERVICE}.service.d/ipset.conf <<'CAPEOF'
# Поставлено routing-host-setup.sh. Без CAP_NET_ADMIN dnsmasq не может писать в
# ipset, и условная маршрутизация не работает БЕЗ сообщений об ошибке.
[Service]
AmbientCapabilities=CAP_NET_ADMIN
CAPEOF
    printf '  записан override\n'
else
    printf '  would: записать override с AmbientCapabilities=CAP_NET_ADMIN\n'
fi
run "systemctl daemon-reload"
run "systemctl enable --now ${DNSMASQ_SERVICE}"
run "systemctl restart ${DNSMASQ_SERVICE}"

# 3) DNAT :53 — чтобы выданные конфиги с DNS=1.1.1.1 работали без перевыпуска
step "3. Перехват DNS клиентов на dnsmasq"
ensure_rule nat PREROUTING -s "$CLIENT_SUBNET" -p udp --dport 53 \
            -j DNAT --to-destination "$DNS_ADDR:53"
ensure_rule nat PREROUTING -s "$CLIENT_SUBNET" -p tcp --dport 53 \
            -j DNAT --to-destination "$DNS_ADDR:53"

# 4) выход наружу для немаскараженного трафика включённых устройств
step "4. MASQUERADE и FORWARD для $CLIENT_SUBNET"
ensure_rule nat POSTROUTING -s "$CLIENT_SUBNET" -j MASQUERADE
ensure_rule filter FORWARD -s "$CLIENT_SUBNET" -j ACCEPT
ensure_rule filter FORWARD -d "$CLIENT_SUBNET" -j ACCEPT

# 5) обратный маршрут в контейнер
step "5. Маршрут $CLIENT_SUBNET → $CONT_IP"
if ip route show "$CLIENT_SUBNET" | grep -q "$CONT_IP"; then
    say "  уже есть"
else
    run "ip route replace $CLIENT_SUBNET via $CONT_IP"
fi

# 6) параметры ядра
step "6. sysctl"
say "  ip_forward — иначе хост не пропустит трафик клиентов вовсе"
say "  rp_filter=strict отбросил бы пакеты с 10.8.1.x, пришедшие с докерного"
say "  интерфейса, если обратный маршрут покажется ядру несимметричным"
if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" != "1" ]; then
    run "sysctl -w net.ipv4.ip_forward=1"
else
    say "  ip_forward: уже 1"
fi
for _p in all default; do
    _cur="$(cat "/proc/sys/net/ipv4/conf/$_p/rp_filter" 2>/dev/null || echo 0)"
    if [ "$_cur" = "1" ]; then
        run "sysctl -w net.ipv4.conf.$_p.rp_filter=2"
    else
        say "  rp_filter.$_p: $_cur (strict не включён)"
    fi
done

step "Проверка"
say "  ip route get 10.8.1.1"
say "  iptables -t nat -C POSTROUTING -s $CLIENT_SUBNET -j MASQUERADE && echo ok"
say "  dig +short @$DNS_ADDR example.com"
say ""
say "  ГЛАВНАЯ проверка — наполняется ли набор (иначе маркировать нечего):"
say "    dig +short @$DNS_ADDR sberbank.ru >/dev/null; ipset list ru_base | grep -c '^[0-9]'"
say "  Ноль при живом резолве = dnsmasq не может писать в ipset (см. шаг 2a)."
say ""
say "ВАЖНО: правила iptables и адрес на $DNS_IF эфемерны — переживут перезапуск"
say "бота, но не ребут хоста. Закрепи (netfilter-persistent / systemd-юнит),"
say "иначе после ребута у включённых пользователей пропадёт интернет."
say ""
say "Дальше: линк-туннель до шлюза + routing.gw_interface в conf/app.yaml."
say "Пока ключ пуст, бот фичу не поднимает."
