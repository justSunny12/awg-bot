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
#   2) MASQUERADE трафика клиентов в eth0 — ради этого всё и затевалось:
#      российские сервисы увидят домашний адрес;
#   3) ЗАКРЫВАЕТ клиентам доступ в домашнюю сеть. Обязательный шаг: чтобы фича
#      заработала, нужно разрешить НОВЫЕ соединения из туннеля, а это открывает
#      и путь к NAS, роутеру и торрент-клиенту;
#   4) автозапуск.
#
# ЧЕГО НЕ ДЕЛАЕТ: не трогает существующие интерфейсы и уже настроенную на шлюзе
# маршрутизацию — они продолжают работать как работали.
#
# ЗАПУСК:
#   sudo sh routing-gw-setup.sh                    # показать план
#   sudo sh routing-gw-setup.sh --apply gw-awglink.conf
#   sudo sh routing-gw-setup.sh --rollback
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="${LINK_IF:-awglink}"
CLIENT_SUBNET="${CLIENT_SUBNET:-10.8.1.0/24}"
FWD_CHAIN="AWGLINK_FWD"
UNIT="/etc/systemd/system/awg-link-gw.service"
SYSCTL_CONF="/etc/sysctl.d/99-awgbot-gw.conf"

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
    run "systemctl disable --now awg-link-gw.service 2>/dev/null || true"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    if [ -n "$CONTAINER" ]; then
        run "docker exec $CONTAINER awg-quick down $LINK_IF 2>/dev/null || true"
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
    LINK_CIDR="$(ip -4 -o addr show dev "$LINK_IF" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}')"
    LINK_CIDR="$(printf '%s' "$LINK_CIDR" | awk -F'[./]' '$5==30{
        printf "%d.%d.%d.%d/%d\n", $1, $2, $3, int($4/4)*4, $5 }')"
    while [ -n "$LINK_CIDR" ] && iptables -t nat -C POSTROUTING -s "$LINK_CIDR" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $LINK_CIDR -o $WAN_IF -j MASQUERADE"
    done
    run "iptables -F $FWD_CHAIN 2>/dev/null || true"
    run "iptables -X $FWD_CHAIN 2>/dev/null || true"
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
    say "  2. iptables -t nat -A POSTROUTING -s $CLIENT_SUBNET -o $WAN_IF -j MASQUERADE"
    say "  3. цепочка $FWD_CHAIN: DROP во все приватные сети, затем ACCEPT"
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
if [ "$(readlink -f "$SRC_CONF")" = "$(readlink -f "$HOST_CONF_DIR/$LINK_IF.conf")" ]; then
    say "  конфиг уже на месте — копировать не нужно"
else
    run "install -m 600 '$SRC_CONF' $HOST_CONF_DIR/$LINK_IF.conf"
fi
if ip link show "$LINK_IF" >/dev/null 2>&1; then
    say "  интерфейс уже поднят — перезапускаю, чтобы подхватить конфиг"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    # и в контейнере тоже: линк мог быть поднят прежней версией скрипта
    if [ -n "$CONTAINER" ]; then
        run "docker exec $CONTAINER awg-quick down $LINK_IF 2>/dev/null || true"
    fi
fi
[ -n "$AWG_QUICK" ] || { say "ОШИБКА: awg-quick не найден на ХОСТЕ."; \
    say "  Собери amneziawg-tools той же версии, что и модуль ядра."; exit 1; }
run "$AWG_QUICK up $LINK_IF"

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
LINK_CIDR="$(ip -4 -o addr show dev "$LINK_IF" 2>/dev/null \
    | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}')"
LINK_CIDR="$(printf '%s' "$LINK_CIDR" | awk -F'[./]' '$5==30{
    printf "%d.%d.%d.%d/%d\n", $1, $2, $3, int($4/4)*4, $5 }')"
[ -n "$LINK_CIDR" ] || { say "ОШИБКА: не удалось определить подсеть $LINK_IF"; exit 1; }
say "  Подсеть линка: $LINK_CIDR"

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

# ── 2. MASQUERADE: ради этого всё и затевалось ───────────────────────────────
step "2. MASQUERADE клиентов в $WAN_IF"
say "  Трафик приходит из туннеля с адресом клиента (10.8.1.x) — подменяем его"
say "  домашним, чтобы российские сервисы увидели российский адрес."
if iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$WAN_IF" -j MASQUERADE 2>/dev/null; then
    say "  уже есть"
else
    run "iptables -t nat -A POSTROUTING -s $CLIENT_SUBNET -o $WAN_IF -j MASQUERADE"
fi
# Линк-подсеть маскарадим ТОЖЕ: с адреса линка ходит зонд живости с ВПС. Без
# этого его пакеты уходили бы в интернет немаскараженными и не возвращались —
# бот считал бы исправный шлюз непроходимым и держал режим выключенным.
if iptables -t nat -C POSTROUTING -s "$LINK_CIDR" -o "$WAN_IF" -j MASQUERADE 2>/dev/null; then
    say "  уже есть (линк)"
else
    run "iptables -t nat -A POSTROUTING -s $LINK_CIDR -o $WAN_IF -j MASQUERADE"
fi

# ── 3. закрыть домашнюю сеть ─────────────────────────────────────────────────
step "3. Изоляция клиентов от домашней сети"
say "  ОБЯЗАТЕЛЬНО. Чтобы фича работала, из туннеля должны проходить НОВЫЕ"
say "  соединения (сейчас правила пускают только RELATED,ESTABLISHED). Это же"
say "  открывает путь во все локальные сети шлюза — закрываем."
say "  Отдельная цепочка, а не вставки в FORWARD: она пересобирается целиком,"
say "  и порядок DROP-перед-ACCEPT не зависит от того, что уже лежит в FORWARD."
run "iptables -N $FWD_CHAIN 2>/dev/null || true"
run "iptables -F $FWD_CHAIN"
# Перечислены ВСЕ приватные диапазоны, а не конкретная домашняя подсеть: так
# скрипт не несёт в себе чужую топологию и закрывает заодно докеровские сети и
# link-local, о которых легко забыть.
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
    run "iptables -A $FWD_CHAIN -d $net -j DROP"
done
run "iptables -A $FWD_CHAIN -j ACCEPT"
if iptables -C FORWARD -i "$LINK_IF" -j "$FWD_CHAIN" 2>/dev/null; then
    say "  хук уже есть"
else
    run "iptables -I FORWARD -i $LINK_IF -j $FWD_CHAIN"
fi
if iptables -C FORWARD -o "$LINK_IF" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; then
    say "  обратный путь уже разрешён"
else
    run "iptables -I FORWARD -o $LINK_IF -m state --state RELATED,ESTABLISHED -j ACCEPT"
fi

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
say "  iptables -L $FWD_CHAIN -n --line-numbers     # DROP выше ACCEPT?"
say ""
say "Хендшейка не будет, пока на ВПС не поднят ответный конец."
say ""
say "Затем на ВПС в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
say "и перезапустить бота."
