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
#      САМОЙ МАШИНЫ от туннеля: на неё с адресов туннеля пускается только ВПС
#      по линку (SSH, ICMP) и адреса из SSH_ALLOW (устройства админа, приезжают
#      в бандле) — всё прочее на порты шлюза дропается;
#   3) снимает прежнюю обвязку в iptables (AWGLINK_FWD, MASQUERADE, метки);
#   4) автозапуск: юнит зовёт этот же скрипт, таблица ставится ДО подъёма линка.
#
# ЧЕГО НЕ ДЕЛАЕТ: не трогает существующие интерфейсы, домашнюю схему
# маршрутизации и чужие правила iptables (docker и т.п.) — они работают как
# работали. Политика INPUT для домашней сети остаётся accept.
#
# ОКРУЖЕНИЕ (из юнита/бандла): CLIENT_SUBNET, LINK_IF, SSH_ALLOW (адреса, кому
# открыт SSH на шлюз через туннель), SSH_ALLOW_EXTRA (то же, добавленное на
# самом шлюзе: /etc/awg-gw/firewall.env), SSH_PORT (22), TG_MARK (0x1).
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
GW_ETC="/etc/awg-gw"
GUARD_FILE="$GW_ETC/guard.nft"           # таблица — источник для nft -f при каждом старте
FW_ENV="$GW_ETC/firewall.env"            # SSH_ALLOW_EXTRA, правится на шлюзе (awg-bot firewall)
GUARD_TABLE="inet awg_gw_guard"
SSH_PORT="${SSH_PORT:-22}"
TG_MARK="${TG_MARK:-0x1}"
# Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами; тот же
# список знает агент (domain/gateway.py TG_RANGES) и сверяет с таблицей.
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"
SSH_ALLOW="${SSH_ALLOW:-}"
SSH_ALLOW_EXTRA="${SSH_ALLOW_EXTRA:-}"
[ -f "$FW_ENV" ] && . "$FW_ENV"

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
    run "nft delete table $GUARD_TABLE 2>/dev/null || true"
    run "rm -f $GUARD_FILE $FW_ENV"
    run "rmdir $GW_ETC 2>/dev/null || true"
    run "rm -f $HOST_CONF_DIR/$LINK_IF.conf $UNIT $SYSCTL_CONF"
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
    say "     клиентов от приватных сетей, метки Telegram, защита шлюза от туннеля"
    say "     (SSH через туннель: ВПС по линку + SSH_ALLOW=${SSH_ALLOW:-—})"
    say "  3. снятие прежних правил iptables ($FWD_CHAIN, MASQUERADE, метки)"
    say "  4. юнит awg-link-gw.service"
    exit 0
fi

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
run "printf 'net.ipv4.ip_forward = 1\\n' > $SYSCTL_CONF"

# ── 2. таблица nft — вся обвязка одним атомарным файлом ──────────────────────
step "2. Таблица $GUARD_TABLE"
say "  MASQUERADE $CLIENT_SUBNET и $LINK_CIDR → $WAN_IF: российские сервисы увидят"
say "  домашний адрес; с адреса линка ходит зонд живости с ВПС."
say "  Изоляция: клиентам из туннеля закрыты все приватные сети (NAS, роутер,"
say "  docker, link-local), остальное — транзит наружу."
say "  Защита шлюза: с адресов туннеля на саму машину пускаем только ВПС по"
say "  линку ($LINK_PEER: SSH, ICMP) и SSH с адресов SSH_ALLOW; прочее дропается."
say "  Метки Telegram ($TG_MARK): агенту нужен Telegram через ВПС."
command -v nft >/dev/null 2>&1 || { say "ОШИБКА: нет nft — apt install nftables"; exit 1; }
SSH_ELEMS="$(ipv4_list $SSH_ALLOW $SSH_ALLOW_EXTRA)"
say "  SSH через туннель разрешён: $LINK_PEER${SSH_ELEMS:+, $SSH_ELEMS}"
if [ "$MODE" = "plan" ]; then
    say "  would: записать $GUARD_FILE и применить: nft -f $GUARD_FILE"
else
mkdir -p "$GW_ETC"
{
cat <<GUARDEOF
#!/usr/sbin/nft -f
# awg-bot (шлюз): обвязка условной маршрутизации и защита машины от туннеля.
# Генерирует routing-gw-setup.sh при каждом старте юнита awg-link-gw.service —
# правки руками перезапишутся. SSH через туннель: SSH_ALLOW из бандла с ВПС
# плюс SSH_ALLOW_EXTRA из $FW_ENV (awg-bot firewall allow/deny).
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
    set ssh_allow4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$SSH_ELEMS" ] && printf '        elements = { %s }\n' "$SSH_ELEMS"
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
        ip saddr $LINK_PEER tcp dport $SSH_PORT accept
        ip saddr @ssh_allow4 tcp dport $SSH_PORT accept
        drop
    }

    chain forward {
        type filter hook forward priority filter; policy accept;
        oifname "$LINK_IF" ct state established,related accept
        iifname "$LINK_IF" ip daddr @private4 drop
        iifname "$LINK_IF" accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade
    }

    chain output {
        type route hook output priority mangle; policy accept;
        ip daddr @tg_nets4 meta mark set $TG_MARK
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
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый бандл с ВПС.
Environment=CLIENT_SUBNET=$CLIENT_SUBNET
Environment=LINK_IF=$LINK_IF
# Кому открыт SSH на шлюз через туннель: устройства админа, из бандла с ВПС.
# Добавленное на самом шлюзе (awg-bot firewall allow) — в $FW_ENV.
Environment="SSH_ALLOW=$SSH_ALLOW"
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

step "Проверка"
say "  awg show $LINK_IF                            # есть ли хендшейк"
say "  ip -br addr show $LINK_IF"
say "  nft list table $GUARD_TABLE                  # вся обвязка одним взглядом"
say ""
say "Хендшейка не будет, пока на ВПС не поднят ответный конец."
say ""
say "Затем на ВПС в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
say "и перезапустить бота."
