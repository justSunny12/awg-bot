#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-gw-setup.sh — сторона ШЛЮЗА (малинки) для условной маршрутизации.
# Запускается НА МАЛИНКЕ. См. docs/conditional-routing.md, §12.
#
# КОНТЕКСТ. Контейнер AmneziaWG на шлюзе работает с network_mode=host, поэтому
# интерфейсы и правила, созданные внутри него, живут в хостовом namespace.
# Инструменты awg есть только в контейнере, iptables — и там, и на хосте (это
# одни и те же правила). Конфиги примонтированы read-only, значит файл кладём на
# хост, а поднимаем через контейнер.
#
# Имя контейнера, интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ сами —
# переопределяются переменными CONTAINER / WAN_IF / HOST_CONF_DIR.
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

# Контейнер, интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ, а не задаются
# дефолтом: чужие имена в поставке — источник тихих ошибок «скрипт отработал, но
# не там». Любое можно переопределить переменной окружения.
detect_container() {
    [ -n "${CONTAINER:-}" ] && { printf '%s' "$CONTAINER"; return; }
    for n in $(docker ps --format '{{.Names}}' 2>/dev/null); do
        docker exec "$n" sh -c 'command -v awg' >/dev/null 2>&1 && { printf '%s' "$n"; return; }
    done
}
detect_wan() {
    [ -n "${WAN_IF:-}" ] && { printf '%s' "$WAN_IF"; return; }
    ip route show default 2>/dev/null | awk '/^default/{print $5; exit}'
}
detect_conf_dir() {   # каталог, примонтированный в /etc/amnezia/amneziawg
    [ -n "${HOST_CONF_DIR:-}" ] && { printf '%s' "$HOST_CONF_DIR"; return; }
    docker inspect -f \
      '{{range .Mounts}}{{if eq .Destination "/etc/amnezia/amneziawg"}}{{.Source}}{{end}}{{end}}' \
      "$1" 2>/dev/null
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
dexec() { docker exec "$CONTAINER" "$@"; }

CONTAINER="$(detect_container)"
[ -n "$CONTAINER" ] || { say "ОШИБКА: не нашёл контейнер с awg. Укажи: CONTAINER=имя $0 ..."; exit 1; }
WAN_IF="$(detect_wan)"
[ -n "$WAN_IF" ] || { say "ОШИБКА: не определил интерфейс выхода. Укажи: WAN_IF=eth0 $0 ..."; exit 1; }
HOST_CONF_DIR="$(detect_conf_dir "$CONTAINER")"
[ -n "$HOST_CONF_DIR" ] || { say "ОШИБКА: не нашёл каталог конфигов контейнера. Укажи: HOST_CONF_DIR=... $0 ..."; exit 1; }

say "Параметры (определены автоматически, переопределяются переменными):"
say "  интерфейс линка   : $LINK_IF"
say "  контейнер         : $CONTAINER"
say "  конфиг на хосте   : $HOST_CONF_DIR/$LINK_IF.conf"
say "  клиенты ВПС       : $CLIENT_SUBNET"
say "  выход в интернет  : $WAN_IF"
say "  локальные сети    : будут ЗАКРЫТЫ для клиентов (все приватные диапазоны)"

# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Снятие"
    run "systemctl disable --now awg-link-gw.service 2>/dev/null || true"
    run "docker exec $CONTAINER awg-quick down $LINK_IF 2>/dev/null || true"
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
    while iptables -t nat -C POSTROUTING -s "$LINK_CIDR" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $LINK_CIDR -o $WAN_IF -j MASQUERADE"
    done
    run "iptables -F $FWD_CHAIN 2>/dev/null || true"
    run "iptables -X $FWD_CHAIN 2>/dev/null || true"
    run "rm -f $HOST_CONF_DIR/$LINK_IF.conf $UNIT"
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
    say "  1. конфиг → $HOST_CONF_DIR/$LINK_IF.conf, awg-quick up через контейнер"
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
run "install -m 600 '$SRC_CONF' $HOST_CONF_DIR/$LINK_IF.conf"
if ip link show "$LINK_IF" >/dev/null 2>&1; then
    say "  интерфейс уже поднят — перезапускаю, чтобы подхватить конфиг"
    run "docker exec $CONTAINER awg-quick down $LINK_IF || true"
fi
run "docker exec $CONTAINER awg-quick up $LINK_IF"

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
SELF="$(readlink -f "$0")"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк до ВПС и изоляция клиентов (шлюз)
After=docker.service network-online.target
Requires=docker.service
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
say "  docker exec $CONTAINER awg show $LINK_IF     # есть ли хендшейк"
say "  ip -br addr show $LINK_IF"
say "  iptables -L $FWD_CHAIN -n --line-numbers     # DROP выше ACCEPT?"
say ""
say "Хендшейка не будет, пока на ВПС не поднят ответный конец."
say ""
say "Затем на ВПС в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
say "и перезапустить бота."
