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
# ОДНОКРАТНЫЕ КЛЮЧИ. dnsmasq принимает bind-interfaces и cache-size один раз
# на ВСЕ файлы конфигурации (/etc/dnsmasq.conf + conf-dir), иначе «illegal
# repeated keyword» и демон не стартует. Ubuntu кладёт bind-interfaces в
# /etc/dnsmasq.d/ubuntu-fan (файл дистрибутива, без .conf — но он читается),
# а bind-dynamic с ним несовместим вовсе. Поэтому: bind-interfaces и cache-size
# пишем только если их нет в других файлах; из конфига обвязки (наш) — снимаем.
# Адрес интерфейса awg появляется позже старта dnsmasq — юнит перезапускается
# сам без лимита попыток, пока не поднимется.
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
# Прежний конфиг обвязки: однократные ключи и наши адреса из него уходят при
# усыновлении, остальное (адрес перехвата) не трогаем.
ROUTING_BASE_CONF="${ROUTING_BASE_CONF:-/etc/dnsmasq.d/awgbot-base.conf}"
DNSMASQ_MAIN_CONF="${DNSMASQ_MAIN_CONF:-/etc/dnsmasq.conf}"
DNSMASQ_CONF_DIR="${DNSMASQ_CONF_DIR:-/etc/dnsmasq.d}"

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

set_elsewhere() {  # set_elsewhere REGEX — ключ уже задан в ДРУГОМ файле dnsmasq?
    # Наш файл и конфиг обвязки не в счёт: их мы переписываем сами.
    local f
    for f in "$DNSMASQ_MAIN_CONF" "$DNSMASQ_CONF_DIR"/*; do
        [[ -f "$f" ]] || continue
        [[ "$f" == "$RESOLVER_CONF" || "$f" == "$ROUTING_BASE_CONF" ]] && continue
        grep -qE "$1" "$f" && return 0
    done
    return 1
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
        # без bind-interfaces dnsmasq сел бы на 0.0.0.0 и перекрыл 127.0.0.53
        # systemd-resolved; ключ однократный — пишем, только если его нет в
        # других файлах (Ubuntu: /etc/dnsmasq.d/ubuntu-fan)
        set_elsewhere '^bind-interfaces$' || echo "bind-interfaces"
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
EOF
        set_elsewhere '^cache-size=' || echo "cache-size=10000"
    } > "$tmp"
    if [[ "$PLAN" -eq 1 ]]; then
        printf '  would: записать %s (listen: %s)\n' "$RESOLVER_CONF" "$*"; rm -f "$tmp"
    else
        mkdir -p "$(dirname "$RESOLVER_CONF")"
        # прежний файл — в сторону: не поднялся демон — вернём его, а не оставим
        # клиентов без DNS с нашим же битым конфигом
        PREV_CONF=""
        if [[ -f "$RESOLVER_CONF" ]]; then
            PREV_CONF="$(mktemp)"; cp "$RESOLVER_CONF" "$PREV_CONF"
        fi
        install -m 0644 "$tmp" "$RESOLVER_CONF"; rm -f "$tmp"
    fi
}

rollback_conf() {  # вернуть прежний конфиг (или снять новый) и поднять демон
    if [[ -n "${PREV_CONF:-}" && -f "$PREV_CONF" ]]; then
        cp "$PREV_CONF" "$RESOLVER_CONF"; rm -f "$PREV_CONF"
        log "конфиг резолвера возвращён к прежнему"
    else
        rm -f "$RESOLVER_CONF"
        log "новый конфиг резолвера снят"
    fi
    systemctl restart "$DNSMASQ_SERVICE" 2>/dev/null || true
}

adopt_routing_conf() {  # adopt_routing_conf ADDR… — конфиг обвязки под наш режим
    # Однократные ключи (bind-interfaces, cache-size) из файла обвязки уходят:
    # их место решает set_elsewhere при записи нашего; адреса, которые слушаем
    # мы, из её файла тоже уходят — один адрес в одном файле.
    [[ -f "$ROUTING_BASE_CONF" ]] || return 0
    local a changed=""
    if grep -q '^bind-interfaces$' "$ROUTING_BASE_CONF"; then
        run "sed -i '/^bind-interfaces$/d' '$ROUTING_BASE_CONF'"; changed=1
    fi
    if grep -q '^cache-size=' "$ROUTING_BASE_CONF"; then
        run "sed -i '/^cache-size=/d' '$ROUTING_BASE_CONF'"; changed=1
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

# Drop-in пишется при КАЖДОМ запуске с правами root, а не только при install:
# правка самого drop-in'а (например, снятие лимита попыток) иначе не доезжает
# до хостов, поставленных раньше, — обновление бота его не трогает.
# Переписываем только при расхождении: лишняя запись на флеш ни к чему.
write_dropin() {
    if [[ "$PLAN" -eq 1 ]]; then printf '  would: записать %s\n' "$DROPIN"; return 0; fi
    mkdir -p "$DROPIN_DIR"
    local tmp; tmp="$(mktemp)"
    cat > "$tmp" <<'EOF'
# Поставлено awg-resolver-setup.sh. Резолвер клиентов — единственный DNS у
# всех, кто получил конфиг с приватным адресом: упал — люди без DNS. Поэтому
# поднимаем сам и не ждём ручного вмешательства. Без лимита попыток: адрес
# интерфейса awg появляется позже старта dnsmasq, и с bind-interfaces первые
# запуски после ребута честно падают, пока awg-quick не поднял интерфейс.
[Unit]
StartLimitIntervalSec=0
[Service]
Restart=on-failure
RestartSec=5
EOF
    if cmp -s "$tmp" "$DROPIN"; then rm -f "$tmp"; return 0; fi
    mv "$tmp" "$DROPIN"; chmod 0644 "$DROPIN"
    log "обновлён $DROPIN"
    return 10                     # «изменился» — вызывающий решает, нужен ли daemon-reload
}

restart_service() {
    # Синтаксис — ДО рестарта: dnsmasq падает на любом повторе однократного
    # ключа между файлами, а упавший демон = все клиенты без DNS.
    if [[ "$PLAN" -eq 0 ]] && command -v dnsmasq >/dev/null 2>&1 \
            && ! out="$(dnsmasq --test 2>&1)"; then
        log "конфиг dnsmasq не прошёл проверку: $out"
        rollback_conf
        die "конфиг резолвера отклонён dnsmasq — см. выше; прежний возвращён"
    fi
    run "systemctl daemon-reload"
    run "systemctl enable --now ${DNSMASQ_SERVICE} >/dev/null 2>&1 || true"
    if ! run "systemctl restart ${DNSMASQ_SERVICE}"; then
        rollback_conf
        die "dnsmasq не перезапустился — journalctl -u ${DNSMASQ_SERVICE} -e; прежний конфиг возвращён"
    fi
    if [[ "$PLAN" -eq 0 ]] && ! systemctl is-active --quiet "$DNSMASQ_SERVICE"; then
        rollback_conf
        die "dnsmasq не активен после запуска — journalctl -u ${DNSMASQ_SERVICE} -e; прежний конфиг возвращён"
    fi
    [[ -n "${PREV_CONF:-}" ]] && rm -f "$PREV_CONF"
    return 0
}

verify() {  # verify ADDR — отвечает ли адрес (если есть чем спросить)
    [[ "$PLAN" -eq 1 ]] && return 0
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
        write_dropin || true          # daemon-reload сделает restart_service
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
        write_dropin || true          # daemon-reload сделает restart_service
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
    dropin)
        # Только drop-in: зовёт бот на старте, чтобы правка доехала до хостов,
        # поставленных прежними версиями (install их не переустанавливает).
        # Демон не трогаем: override действует со следующего его запуска, а
        # рестарт резолвера ради этого — обрыв DNS у всех на ровном месте.
        [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
        [[ -f "$RESOLVER_CONF" ]] || { log "резолвера нет — drop-in не нужен"; exit 0; }
        write_dropin || { run "systemctl daemon-reload"; }
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
