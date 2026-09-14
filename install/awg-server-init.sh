#!/usr/bin/env bash
#
# awg-server-init.sh — сервер AmneziaWG с нуля на чистом хосте: конфиг
# интерфейса с ключами, общий PSK, ip_forward, автозагрузка awg-quick@.
#
# Раньше сервер поднимала докерная Amnezia, а бот лишь подсасывал из неё порт
# и подсеть. Теперь сервер создаёт установщик: параметры генерируются здесь,
# бот читает их живьём из конфига, как и прежде (второго источника истины
# не появляется). Файервол и NAT клиентов — не здесь: их ведёт бот в таблице
# awg_bot_guard (README §6b).
#
# РАСКЛАДКА: сервер — .1, клиенты с .2 (ip_host_start: 2 в app.yaml). Так у
# приватного резолвера появляется адрес, на котором кто-то отвечает: dnsmasq
# слушает прямо на адресе интерфейса. Докерный квирк «сервер на .0» — только у
# старых установок, их конфиги не трогаются.
#
# ОБФУСКАЦИЯ — классический скалярный набор Jc/Jmin/Jmax/S1/S2/H1..H4 в
# диапазонах приложения Amnezia: его понимает любой клиент поколения 3.x.
# Диапазонные H и S3/S4 — дело переезда профилей, а не первой установки.
#
# ИДЕМПОТЕНТНО: конфиг есть — ничего не трогает, печатает его топологию.
#
# Использование (root):
#   awg-server-init.sh [--plan]           # создать, если нет; печатает KEY=VALUE
# Окружение: AWG_IF (awg0), AWG_QUICK_DIR (каталог awg-quick, /etc/amnezia/amneziawg),
#            SUBNET_PREFIX (10.8.1), LISTEN_PORT (случайный 20000-60000).
set -euo pipefail

AWG_IF="${AWG_IF:-awg0}"
AWG_QUICK_DIR="${AWG_QUICK_DIR:-/etc/amnezia/amneziawg}"
SUBNET_PREFIX="${SUBNET_PREFIX:-10.8.1}"
LISTEN_PORT="${LISTEN_PORT:-}"
CONF="$AWG_QUICK_DIR/$AWG_IF.conf"
PSK_FILE="$AWG_QUICK_DIR/wireguard_psk.key"
SYSCTL_FILE="/etc/sysctl.d/99-awg-bot.conf"

PLAN=0; [[ "${1:-}" == "--plan" ]] && PLAN=1
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { sed -n '2,26p' "$0"; exit 0; }
die() { printf '[awg-server:ОШИБКА] %s\n' "$*" >&2; exit 1; }
say() { printf '[awg-server] %s\n' "$*" >&2; }
[[ "$PLAN" -eq 1 || "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"
[[ "$SUBNET_PREFIX" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "SUBNET_PREFIX вида X.Y.Z, получено '$SUBNET_PREFIX'"

emit() {  # KEY=VALUE для установщика — на stdout, всё прочее на stderr
    printf 'AWG_IF=%s\nCONF=%s\nLISTEN_PORT=%s\nSUBNET_PREFIX=%s\nSUBNET_CIDR=%s.0/24\nSERVER_ADDR=%s.1\nCREATED=%s\n' \
        "$AWG_IF" "$CONF" "$1" "$SUBNET_PREFIX" "$SUBNET_PREFIX" "$SUBNET_PREFIX" "$2"
}

if [[ -f "$CONF" ]]; then
    port="$(sed -nE 's/^ListenPort *= *([0-9]+).*/\1/p' "$CONF" | head -n1)"
    addr="$(sed -nE 's/^Address *= *([0-9.]+)\/.*/\1/p' "$CONF" | head -n1)"
    [[ -n "$addr" ]] && SUBNET_PREFIX="${addr%.*}"
    say "сервер уже есть: $CONF (порт ${port:-?}, подсеть ${SUBNET_PREFIX}.0/24) — не трогаю"
    emit "${port:-}" 0
    exit 0
fi

for t in awg awg-quick; do
    command -v "$t" >/dev/null 2>&1 && continue
    [[ "$PLAN" -eq 1 ]] && { say "would: $t ещё нет — его ставит awg-bot awg install"; continue; }
    die "$t не найден — сначала awg-bot awg install"
done
if ! modprobe amneziawg 2>/dev/null; then
    [[ "$PLAN" -eq 1 ]] || die "модуль amneziawg не грузится — сначала awg-bot awg install"
fi

rnd() {  # rnd MIN MAX — из /dev/urandom: shuf есть не везде, а $RANDOM даёт
         # только 15 бит, тогда как H1..H4 берутся из всего диапазона uint32
    local span=$(( $2 - $1 + 1 )) n
    n="$(od -An -N4 -tu4 < /dev/urandom | tr -d ' \n')"
    printf '%s' "$(( $1 + n % span ))"
}
[[ -n "$LISTEN_PORT" ]] || LISTEN_PORT="$(rnd 20000 60000)"
[[ "$LISTEN_PORT" =~ ^[0-9]+$ ]] || die "LISTEN_PORT — число"

# ── обфускация: диапазоны приложения Amnezia ─────────────────────────────────
JC="$(rnd 3 10)"; JMIN=50; JMAX=1000
S1="$(rnd 15 150)"
S2="$(rnd 15 150)"
# S1 + 56 == S2 ломает разбор рукопожатия: длина init-пакета совпадает с
# response — переизбираем S2, пока не разойдутся
while [[ $((S1 + 56)) -eq "$S2" ]]; do S2="$(rnd 15 150)"; done
# H1..H4 — четыре РАЗНЫХ значения; повтор означает два типа пакетов с одним
# заголовком, и приёмник их не различит
H=""                                  # четыре значения через пробел
while [[ "$(wc -w <<<"$H")" -lt 4 ]]; do
    v="$(rnd 5 2147483647)"
    dup=0
    for x in $H; do [[ "$x" == "$v" ]] && dup=1; done
    if [[ "$dup" -eq 0 ]]; then H="$H $v"; fi
done
read -r H1 H2 H3 H4 <<<"$H"

if [[ "$PLAN" -eq 1 ]]; then
    say "would: создать $CONF (порт $LISTEN_PORT, адрес ${SUBNET_PREFIX}.1/24, Jc=$JC S1=$S1 S2=$S2), $PSK_FILE, $SYSCTL_FILE, enable awg-quick@$AWG_IF"
    emit "$LISTEN_PORT" 0
    exit 0
fi

umask 077
mkdir -p "$AWG_QUICK_DIR"; chmod 700 "$AWG_QUICK_DIR"
priv="$(awg genkey)"
[[ -f "$PSK_FILE" ]] || awg genpsk > "$PSK_FILE"
chmod 600 "$PSK_FILE"

tmp="$CONF.new"
cat > "$tmp" <<CONF_EOF
[Interface]
Address = ${SUBNET_PREFIX}.1/24
ListenPort = $LISTEN_PORT
PrivateKey = $priv
Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
CONF_EOF
chmod 600 "$tmp"; mv -f "$tmp" "$CONF"
say "создан $CONF: порт $LISTEN_PORT, адрес ${SUBNET_PREFIX}.1/24, клиенты с .2"

# ip_forward — сейчас и после перезагрузки, отдельным файлом (rollback его снимает)
printf 'net.ipv4.ip_forward = 1\n' > "$SYSCTL_FILE"; chmod 644 "$SYSCTL_FILE"
sysctl -q -w net.ipv4.ip_forward=1

# автозагрузка: в host-режиме интерфейс поднимает awg-quick@, и его надо включить
systemctl enable --now "awg-quick@$AWG_IF" >/dev/null 2>&1 \
    || { awg-quick down "$AWG_IF" >/dev/null 2>&1 || true; rm -f "$CONF"; die "awg-quick@$AWG_IF не поднялся — конфиг убран, смотри journalctl -u awg-quick@$AWG_IF"; }
say "awg-quick@$AWG_IF включён и поднят"
emit "$LISTEN_PORT" 1
