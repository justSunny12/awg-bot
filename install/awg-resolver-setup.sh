#!/usr/bin/env bash
#
# awg-resolver-setup.sh — резолвер клиентов на самом сервере: dnsmasq слушает
# собственный адрес интерфейса в туннеле (<подсеть>.1), клиенты получают его в
# поле DNS обоими номерами.
#
# ЗАЧЕМ. Публичный адрес в поле DNS (1.1.1.1) — это Chrome/Edge, которые молча
# уходят на DoH мимо любого перехвата :53, и условная маршрутизация у такого
# человека работает «через раз»: набор адресов наполняет только наш резолвер.
# Приватному адресу браузеру не за что подхватить DoH. Заодно запросы клиентов
# не уходят третьей стороне открытым текстом, есть кэш и защита от DNS
# rebinding и DoH-эндпоинтов — с первого дня, а не только со шлюзом.
#
# ОДИН ВЛАДЕЛЕЦ. Файл /etc/dnsmasq.d/awgbot-resolver.conf ведёт этот скрипт:
# адреса, апстримы, защиты. Обвязка условной маршрутизации (routing-host-setup)
# при наличии этого файла пишет в свой awgbot-base.conf только собственный
# адрес перехвата; списки доменов бот пишет отдельным файлом.
#
# bind-dynamic, а не bind-interfaces: адрес интерфейса awg появляется вместе с
# ним, и при старте dnsmasq раньше awg-quick его ещё нет — с bind-interfaces
# демон не поднялся бы вовсе, а с bind-dynamic подхватит адрес, когда тот
# появится.
#
# РЕЖИМЫ:
#   install ADDR   поставить dnsmasq (если нет юнита), добавить ADDR, включить
#   add ADDR       добавить адрес к слушаемым (второй интерфейс переезда)
#   remove ADDR    снять адрес (старый интерфейс после финала переезда)
#   status         ADDRS=… ACTIVE=0|1 в stdout; код 0 — конфиг есть и демон жив
#   plan ADDR      что сделал бы install, ничего не меняя (без root)
#
# ОКРУЖЕНИЕ: RESOLVER_CONF, DNSMASQ_SERVICE, UPSTREAMS ("1.1.1.1 1.0.0.1"),
# DROPIN_DIR (каталог override юнита), ROUTING_BASE_CONF.
# Идемпотентно. Коды: 0 — сделано, 1 — ошибка, 2 — неверный вызов.

set -euo pipefail

RESOLVER_CONF="${RESOLVER_CONF:-/etc/dnsmasq.d/awgbot-resolver.conf}"
DNSMASQ_SERVICE="${DNSMASQ_SERVICE:-dnsmasq}"
UPSTREAMS="${UPSTREAMS:-1.1.1.1 1.0.0.1}"
DROPIN_DIR="${DROPIN_DIR:-/etc/systemd/system/${DNSMASQ_SERVICE}.service.d}"
DROPIN="$DROPIN_DIR/awgbot-resolver.conf"
# Прежний конфиг обвязки: bind-interfaces в нём несовместим с bind-dynamic —
# при усыновлении строку снимаем, остальное (адрес перехвата) не трогаем.
ROUTING_BASE_CONF="${ROUTING_BASE_CONF:-/etc/dnsmasq.d/awgbot-base.conf}"

MODE="${1:-}"; ADDR="${2:-}"
log()  { printf '[resolver] %s\n' "$*"; }
die()  { printf '[resolver:ОШИБКА] %s\n' "$*" >&2; exit 1; }
PLAN=0
run()  { if [[ "$PLAN" -eq 1 ]]; then printf '  would: %s\n' "$*"; else eval "$@"; fi; }

valid_addr() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

listen_addrs() {  # адреса из конфига, через пробел (bash 3.2: без mapfile)
    [[ -f "$RESOLVER_CONF" ]] || return 0
    sed -nE 's/^listen-address=([0-9.]+)$/\1/p' "$RESOLVER_CONF" | tr '\n' ' '
}

has_addr() {  # has_addr ADDR LIST…
    local want="$1"; shift
    local a; for a in "$@"; do [[ "$a" == "$want" ]] && return 0; done
    return 1
}

write_conf() {  # write_conf ADDR… — переписать конфиг с этим списком адресов
    local a tmp
    [[ $# -gt 0 ]] || die "нечего слушать: список адресов пуст"
    tmp="$(mktemp)"
    {
        echo "# Сгенерировано awg-resolver-setup.sh — резолвер клиентов awg-bot."
        echo "# Руками не править: файл переписывается при переезде профилей."
        echo "bind-dynamic"
        for a in "$@"; do echo "listen-address=$a"; done
        echo "no-resolv"
        for a in $UPSTREAMS; do echo "server=$a"; done
        cat <<'EOF'
# Ответ с приватным адресом отбрасывается: личные списки маршрутизации позволяют
# пользователю добавить свой домен, и без этого он увёл бы чужую сеть в туннель.
stop-dns-rebind
# Канареечный домен Firefox: NXDOMAIN выключает у него DoH.
address=/use-application-dns.net/
# DoH-эндпоинты: Chrome/Edge делают auto-upgrade на известные резолверы; имя
# эндпоинта клиент обязан отрезолвить обычным DNS — у нас, и NXDOMAIN здесь
# обрывает попытку в самом начале. По адресам ловить бесполезно: эндпоинт
# живёт на CDN, а не на анкасте резолвера.
address=/cloudflare-dns.com/
address=/chrome.cloudflare-dns.com/
address=/mozilla.cloudflare-dns.com/
address=/one.one.one.one/
address=/dns.google/
address=/dns.quad9.net/
address=/dns.adguard-dns.com/
address=/doh.opendns.com/
cache-size=10000
EOF
    } > "$tmp"
    if [[ "$PLAN" -eq 1 ]]; then
        printf '  would: записать %s (listen: %s)\n' "$RESOLVER_CONF" "$*"; rm -f "$tmp"
    else
        mkdir -p "$(dirname "$RESOLVER_CONF")"
        install -m 0644 "$tmp" "$RESOLVER_CONF"; rm -f "$tmp"
    fi
}

adopt_routing_conf() {  # adopt_routing_conf ADDR… — конфиг обвязки под наш режим
    # bind-interfaces обвязки несовместим с bind-dynamic; адреса, которые
    # слушаем мы, из её файла уходят — один адрес в одном файле.
    [[ -f "$ROUTING_BASE_CONF" ]] || return 0
    local a changed=""
    if grep -q '^bind-interfaces$' "$ROUTING_BASE_CONF"; then
        run "sed -i '/^bind-interfaces$/d' '$ROUTING_BASE_CONF'"; changed=1
    fi
    for a in "$@"; do
        if grep -qx "listen-address=$a" "$ROUTING_BASE_CONF"; then
            run "sed -i '/^listen-address=${a//./\\.}$/d' '$ROUTING_BASE_CONF'"; changed=1
        fi
    done
    [[ -n "$changed" ]] && log "конфиг обвязки $ROUTING_BASE_CONF приведён под резолвер бота"
    return 0
}

ensure_package() {
    if systemctl list-unit-files 2>/dev/null | grep -q "^${DNSMASQ_SERVICE}\.service"; then
        return 0
    fi
    # Конфиг уже лежит — полный пакет при установке стартует сервис, и с нашим
    # listen-address он поднимется только на наших адресах, не перебив
    # systemd-resolved на 127.0.0.53.
    log "ставлю пакет dnsmasq…"
    run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q dnsmasq >/dev/null" \
        || die "пакет dnsmasq не установился — apt-get install -y dnsmasq и повтори"
}

write_dropin() {
    if [[ "$PLAN" -eq 1 ]]; then printf '  would: записать %s\n' "$DROPIN"; return 0; fi
    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN" <<'EOF'
# Поставлено awg-resolver-setup.sh. Резолвер клиентов — единственный DNS у
# всех, кто получил конфиг с приватным адресом: упал — люди без DNS. Поэтому
# поднимаем сам и не ждём ручного вмешательства.
[Service]
Restart=on-failure
RestartSec=5
EOF
}

restart_service() {
    run "systemctl daemon-reload"
    run "systemctl enable --now ${DNSMASQ_SERVICE} >/dev/null 2>&1 || true"
    run "systemctl restart ${DNSMASQ_SERVICE}" \
        || die "dnsmasq не перезапустился — journalctl -u ${DNSMASQ_SERVICE} -e"
}

verify() {  # verify ADDR — демон жив и отвечает на адресе (если есть чем спросить)
    [[ "$PLAN" -eq 1 ]] && return 0
    systemctl is-active --quiet "$DNSMASQ_SERVICE" || die "dnsmasq не активен после запуска"
    if command -v dig >/dev/null 2>&1; then
        dig +time=3 +tries=1 +short "@$1" example.com >/dev/null 2>&1 \
            || log "предупреждение: dig @$1 не ответил — адрес ещё не поднят или апстрим недоступен"
    fi
}

case "$MODE" in
    install|plan)
        [[ "$MODE" == "plan" ]] && PLAN=1
        valid_addr "$ADDR" || { echo "нужен адрес: $0 $MODE 10.8.1.1" >&2; exit 2; }
        [[ "$PLAN" -eq 1 || "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        have="$(listen_addrs)"
        # shellcheck disable=SC2086 — список адресов через пробел намеренно
        has_addr "$ADDR" $have || have="$have $ADDR"
        write_conf $have
        adopt_routing_conf $have
        ensure_package
        write_dropin
        restart_service
        verify "$ADDR"
        log "резолвер слушает: ${have# }"
        ;;
    add)
        valid_addr "$ADDR" || { echo "нужен адрес: $0 add 10.9.1.1" >&2; exit 2; }
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        [[ -f "$RESOLVER_CONF" ]] || exec "$0" install "$ADDR"
        have="$(listen_addrs)"
        has_addr "$ADDR" $have && { log "уже слушаем $ADDR"; exit 0; }
        write_conf $have "$ADDR"
        adopt_routing_conf "$ADDR"
        restart_service
        verify "$ADDR"
        log "добавлен $ADDR; слушаем: ${have% }$ADDR"
        ;;
    remove)
        valid_addr "$ADDR" || { echo "нужен адрес: $0 remove 10.8.1.1" >&2; exit 2; }
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        [[ -f "$RESOLVER_CONF" ]] || { log "конфига нет — снимать нечего"; exit 0; }
        have="$(listen_addrs)"
        has_addr "$ADDR" $have || { log "$ADDR и так не слушали"; exit 0; }
        keep=""
        for a in $have; do [[ "$a" == "$ADDR" ]] || keep="$keep $a"; done
        [[ -n "${keep// /}" ]] || die "$ADDR — последний адрес; снять его значит оставить клиентов без DNS"
        write_conf $keep
        restart_service
        log "снят $ADDR; слушаем:$keep"
        ;;
    status)
        have="$(listen_addrs)"
        active=0; systemctl is-active --quiet "$DNSMASQ_SERVICE" 2>/dev/null && active=1
        printf 'ADDRS=%s\nACTIVE=%s\n' "${have% }" "$active"
        [[ -f "$RESOLVER_CONF" && "$active" -eq 1 ]]
        ;;
    *)
        sed -n '2,32p' "$0"; exit 2 ;;
esac
