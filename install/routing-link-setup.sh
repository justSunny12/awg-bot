#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-link-setup.sh — линк-туннель ВПС ↔ шлюз под условную маршрутизацию.
# Запускается НА ВПС. См. docs/conditional-routing.md, §11.
#
# ЗАЧЕМ ОТДЕЛЬНЫЙ ТУННЕЛЬ. У шлюза уже есть туннель к клиентскому интерфейсу
# ВПС — им пользуется домашняя схема малинки. Трогать его нельзя: перенос сломал
# бы работающее ради ещё не запущенного. Поэтому поднимаем ВТОРОЙ туннель,
# только под фичу. Откат — погасить интерфейс, домашняя схема не заметит.
#
# ПОЧЕМУ Table = off. У пира шлюза AllowedIPs = 0.0.0.0/0 — иначе нельзя слать
# туда произвольные адреса назначения. Увидев такое, awg-quick пропишет маршрут
# ПО УМОЛЧАНИЮ, и весь трафик ВПС уйдёт в домашнюю малинку вместе с этой
# SSH-сессией. Маршрутами занимается таблица бота, поэтому Table = off.
#
# ЧТО ДЕЛАЕТ:
#   1) генерирует ключи обеих сторон и собственную обфускацию (отличную от
#      клиентского профиля — одинаковая связала бы их в один отпечаток);
#   2) поднимает интерфейс на ВПС и включает автозапуск;
#   3) выводит трафик в линк из-под MASQUERADE (иначе шлюз увидел бы адрес ВПС
#      вместо адреса клиента);
#   4) печатает готовый конфиг для малинки.
#
# ЗАПУСК:
#   sudo sh routing-link-setup.sh              # показать план
#   sudo sh routing-link-setup.sh --apply      # поднять
#   sudo sh routing-link-setup.sh --rollback   # снять
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="${LINK_IF:-awglink}"
LINK_PORT="${LINK_PORT:-51830}"
LINK_VPS_ADDR="${LINK_VPS_ADDR:-10.99.99.1}"
LINK_GW_ADDR="${LINK_GW_ADDR:-10.99.99.2}"
LINK_CIDR="${LINK_CIDR:-10.99.99.0/30}"
CLIENT_SUBNET="${CLIENT_SUBNET:-10.8.1.0/24}"
CONF_DIR="${CONF_DIR:-/etc/amnezia/amneziawg}"
CONF="$CONF_DIR/$LINK_IF.conf"
GW_CONF_OUT="${GW_CONF_OUT:-/root/gw-$LINK_IF.conf}"
UNIT="/etc/systemd/system/awg-link.service"

MODE="plan"
case "${1:-}" in
    --apply)    MODE="apply" ;;
    --reassert) MODE="reassert" ;;
    --rollback) MODE="rollback" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }

say()  { printf '%s\n' "$*"; }
step() { printf '\n── %s\n' "$*"; }
run()  {
    if [ "$MODE" = "plan" ]; then printf '  would: %s\n' "$*"
    else printf '  $ %s\n' "$*"; sh -c "$*"; fi
}

# Исключение линка из MASQUERADE. Вынесено в функцию, потому что нужно не только
# при установке: правила iptables эфемерны, и после ребута это правило исчезает,
# а обвяз хоста свой MASQUERADE восстанавливает. Тогда шлюз увидит адрес ВПС
# вместо адреса клиента, его собственный MASQUERADE по -s 10.8.1.0/24 не
# сработает, и трафик молча перестанет ходить. Поэтому юнит зовёт --reassert.
assert_nat_exempt() {
    if iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$LINK_IF" -j ACCEPT 2>/dev/null; then
        say "  исключение из MASQUERADE уже есть"
    else
        run "iptables -t nat -I POSTROUTING -s $CLIENT_SUBNET -o $LINK_IF -j ACCEPT"
    fi
}

if [ "$MODE" = "reassert" ]; then
    [ -f "$CONF" ] || { say "линк не настроен ($CONF нет) — нечего поднимать"; exit 0; }
    ip link show "$LINK_IF" >/dev/null 2>&1 || run "awg-quick up $LINK_IF"
    assert_nat_exempt
    exit 0
fi

# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Снятие линка"
    run "systemctl disable --now awg-link.service 2>/dev/null || true"
    run "awg-quick down $LINK_IF 2>/dev/null || true"
    run "rm -f $UNIT $CONF $GW_CONF_OUT"
    run "systemctl daemon-reload"
    while iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$LINK_IF" \
          -j ACCEPT 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $CLIENT_SUBNET -o $LINK_IF -j ACCEPT"
    done
    say ""
    say "Готово. Домашний туннель малинки и обвяз хоста не тронуты."
    exit 0
fi

# ── предполётные проверки ────────────────────────────────────────────────────
# Диапазонные H1..H4 понимают только утилиты v3+. Со старыми конфиг не
# разберётся, и линк не поднимется — проверяем ДО генерации, а не после.
if awg --version 2>/dev/null | grep -q 'v1\.'; then
    say "ОШИБКА: amneziawg-tools первого поколения ($(awg --version 2>/dev/null))."
    say "  Нужны v3+: диапазонные H1..H4 и совместимость с модулем ядра."
    say "  Собери из github.com/amnezia-vpn/amneziawg-tools и повтори."
    exit 1
fi
for t in awg awg-quick; do
    command -v "$t" >/dev/null 2>&1 || {
        say "ОШИБКА: $t не найден. Собери amneziawg-tools из исходников."
        exit 1; }
done
# Порядок проверок важен: свой же поднятый линк держит порт, и проверка порта
# первой сообщала бы «порт занят» вместо «уже настроено» — диагноз, уводящий в
# сторону ровно после успешного запуска.
if [ -f "$CONF" ] && [ "$MODE" = "apply" ]; then
    say ""
    say "Линк уже настроен: $CONF существует."
    if ip link show "$LINK_IF" >/dev/null 2>&1; then
        say "Интерфейс $LINK_IF поднят — делать нечего."
        say "Состояние:  awg show $LINK_IF"
        say "Конфиг для шлюза: $GW_CONF_OUT"
    else
        say "Интерфейс $LINK_IF НЕ поднят. Поднять из существующего конфига:"
        say "    awg-quick up $LINK_IF"
    fi
    say ""
    say "Пересоздать с нуля: сначала $0 --rollback — сменятся ключи, и малинку"
    say "придётся перенастроить заново."
    exit 0
fi
if ss -lnup 2>/dev/null | grep -q ":$LINK_PORT "; then
    say "ОШИБКА: порт $LINK_PORT занят кем-то другим (конфига $CONF нет)."
    say "Кто держит:  ss -lnup | grep :$LINK_PORT"
    say "Взять другой:  LINK_PORT=51834 $0 --apply"
    exit 1
fi

ENDPOINT_HOST="${ENDPOINT_HOST:-$(ip -4 addr show scope global 2>/dev/null \
    | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)}"
[ -n "$ENDPOINT_HOST" ] || { say "ОШИБКА: не определён внешний адрес. Задай ENDPOINT_HOST=..."; exit 1; }

say "Параметры:"
say "  интерфейс      : $LINK_IF ($LINK_VPS_ADDR ↔ $LINK_GW_ADDR)"
say "  порт           : $LINK_PORT"
say "  эндпоинт для ГВ: $ENDPOINT_HOST:$LINK_PORT"
say "  конфиг ВПС     : $CONF"
say "  конфиг для ГВ  : $GW_CONF_OUT"
if [ "$MODE" = "plan" ]; then
    say ""
    say "(режим показа: ключи не генерируются, ничего не меняется — добавь --apply)"
    say ""
    say "Будет сделано:"
    say "  1. awg genkey ×2 + genpsk, случайная обфускация"
    say "  2. записать $CONF (Table = off!) и поднять awg-quick up $LINK_IF"
    say "  3. iptables -t nat -I POSTROUTING -s $CLIENT_SUBNET -o $LINK_IF -j ACCEPT"
    say "  4. юнит awg-link.service + enable"
    say "  5. записать конфиг малинки в $GW_CONF_OUT"
    exit 0
fi

# ── 1. ключи и обфускация ────────────────────────────────────────────────────
step "1. Ключи и обфускация"
umask 077
VPS_PRIV="$(awg genkey)"
VPS_PUB="$(printf '%s' "$VPS_PRIV" | awg pubkey)"
GW_PRIV="$(awg genkey)"
GW_PUB="$(printf '%s' "$GW_PRIV" | awg pubkey)"
PSK="$(awg genpsk)"

rnd() {   # $1=min $2=max
    _r="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"
    echo $(( _r % ($2 - $1 + 1) + $1 ))
}
JC=$(rnd 3 10);    JMIN=$(rnd 8 15);   JMAX=$(rnd 40 70)
S1=$(rnd 15 150);  S2=$(rnd 15 150);   S3=$(rnd 15 150);  S4=$(rnd 15 150)
# H1..H4 берём из НЕПЕРЕСЕКАЮЩИХСЯ полос: одинаковые или пересекающиеся
# значения ломают распознавание типов пакетов, а близкие к штатным 1..4 сводят
# смысл обфускации на нет.
#
# ДИАПАЗОНЫ, а не одиночные числа (умеет amneziawg v3+). Одно фиксированное
# значение на тип пакета — это стабильная сигнатура: DPI достаточно заметить,
# что первые четыре байта у потока всегда одни и те же. С диапазоном заголовок
# выбирается заново для каждого пакета, и такой признак пропадает.
h_range() {   # $1=нижняя граница полосы, $2=верхняя
    _lo=$(rnd "$1" $(( $2 - 200000 )))
    printf '%s-%s' "$_lo" "$(( _lo + $(rnd 50000 150000) ))"
}
H1=$(h_range 5 500000000)
H2=$(h_range 500000001 1000000000)
H3=$(h_range 1000000001 1500000000)
H4=$(h_range 1500000001 2000000000)
say "  ключи сгенерированы, обфускация своя (не совпадает с клиентским профилем)"

# ── 2. конфиг и подъём ───────────────────────────────────────────────────────
step "2. Конфиг $CONF и подъём интерфейса"
say "  Table = off — иначе AllowedIPs=0.0.0.0/0 у пира увёл бы весь трафик ВПС"
say "  в малинку вместе с этой SSH-сессией"
mkdir -p "$CONF_DIR"
cat > "$CONF" <<CONFEOF
# Линк ВПС ↔ шлюз для условной маршрутизации. Сгенерировано routing-link-setup.sh.
# Маршрутами занимается бот (таблица $(printf '%s' "${ROUTING_TABLE:-100}")), поэтому Table = off.
[Interface]
Address = $LINK_VPS_ADDR/30
ListenPort = $LINK_PORT
PrivateKey = $VPS_PRIV
Table = off
Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4

[Peer]
PublicKey = $GW_PUB
PresharedKey = $PSK
AllowedIPs = 0.0.0.0/0
CONFEOF
chmod 600 "$CONF"
run "awg-quick up $LINK_IF"

# ── 3. вывести линк из-под MASQUERADE ────────────────────────────────────────
step "3. Исключение линка из MASQUERADE"
say "  Обвяз хоста маскарадит всю $CLIENT_SUBNET. Без исключения шлюз увидел бы"
say "  адрес ВПС вместо адреса клиента — и различать клиентов стало бы нечем."
assert_nat_exempt

# ── 4. автозапуск ────────────────────────────────────────────────────────────
step "4. Автозапуск"
SELF="$(readlink -f "$0")"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк-туннель до шлюза условной маршрутизации
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# --reassert, а не только awg-quick: правила iptables эфемерны, и исключение
# линка из MASQUERADE после ребута пришлось бы ставить заново вручную.
ExecStart=$SELF --reassert
ExecStop=/usr/bin/awg-quick down $LINK_IF

[Install]
WantedBy=multi-user.target
UNITEOF
run "systemctl daemon-reload"
run "systemctl enable awg-link.service"

# ── 5. конфиг для малинки ────────────────────────────────────────────────────
step "5. Конфиг для шлюза → $GW_CONF_OUT"
cat > "$GW_CONF_OUT" <<GWEOF
# Линк до ВПС под условную маршрутизацию. Положить на шлюз как ОТДЕЛЬНЫЙ
# интерфейс (например /etc/amnezia/amneziawg/awglink.conf) — существующий awg0
# с домашней схемой НЕ трогать.
#
# AllowedIPs узкие намеренно: 0.0.0.0/0 увёл бы весь трафик малинки в туннель.
# Нужны только клиентская подсеть (обратный трафик) и сам линк.
[Interface]
Address = $LINK_GW_ADDR/30
PrivateKey = $GW_PRIV
Jc = $JC
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4

[Peer]
PublicKey = $VPS_PUB
PresharedKey = $PSK
Endpoint = $ENDPOINT_HOST:$LINK_PORT
AllowedIPs = $CLIENT_SUBNET, $LINK_CIDR
PersistentKeepalive = 25
GWEOF
chmod 600 "$GW_CONF_OUT"
say "  записан (права 600 — внутри приватный ключ и psk)"

step "Проверка"
say "  awg show $LINK_IF"
say "  ip -br addr show $LINK_IF"
say ""
say "Дальше на МАЛИНКЕ:"
say "  1. скопировать $GW_CONF_OUT → /etc/amnezia/amneziawg/$LINK_IF.conf"
say "  2. поднять интерфейс, включить автозапуск"
say "  3. MASQUERADE из $LINK_IF в eth0 + FORWARD + DROP на RFC1918"
say "     (последнее обязательно: иначе клиенты попадут в домашнюю сеть)"
say ""
say "Затем на ВПС в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
say "и перезапустить бота. До этого фича спит."
