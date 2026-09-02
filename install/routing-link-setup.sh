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
#   4) собирает БАНДЛ для шлюза: скрипт настройки и конфиг линка одним файлом,
#      плюс печатает готовую строку scp и что запустить на той стороне.
#
# ЗАПУСК:
#   sudo sh routing-link-setup.sh              # показать план
#   sudo sh routing-link-setup.sh --apply      # поднять
#   sudo sh routing-link-setup.sh --bundle     # пересобрать бандл (ключи те же)
#   sudo sh routing-link-setup.sh --rollback   # снять
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="${LINK_IF:-awglink}"
# 443 ПО УМОЛЧАНИЮ, а не характерный для туннелей порт: UDP на 443 неотличим от
# QUIC и теряется в общем потоке, тогда как 51830 сам себя объявляет VPN. Порт —
# первое, что видит DPI, и менять его дороже, чем выбрать сразу. Занят чем-то
# другим — переопредели: LINK_PORT=... routing-link-setup.sh --apply
LINK_PORT="${LINK_PORT:-443}"
LINK_VPS_ADDR="${LINK_VPS_ADDR:-10.99.99.1}"
LINK_GW_ADDR="${LINK_GW_ADDR:-10.99.99.2}"
LINK_CIDR="${LINK_CIDR:-10.99.99.0/30}"
# Из конфига бота, не из хардкода: юнит зовёт --reassert без окружения, и после
# смены клиентской подсети исключение из MASQUERADE обязано реассертиться для
# новой — иначе шлюз после ребута видит клиентов чужим адресом.
_APP_YAML="${_APP_YAML:-/etc/awg-bot/conf/app.yaml}"
_cfg_subnet="$(awk -F'"' '/^  subnet_cidr:/{print $2; exit}' "$_APP_YAML" 2>/dev/null || true)"
CLIENT_SUBNET="${CLIENT_SUBNET:-${_cfg_subnet:-10.8.1.0/24}}"
CONF_DIR="${CONF_DIR:-/etc/amnezia/amneziawg}"
CONF="$CONF_DIR/$LINK_IF.conf"
GW_CONF_OUT="${GW_CONF_OUT:-/root/gw-$LINK_IF.conf}"
GW_BUNDLE_OUT="${GW_BUNDLE_OUT:-/root/awg-gw-bundle.sh}"
UNIT="/etc/systemd/system/awg-link.service"

# Версия КОНТРАКТА ЛИНКА: формат конфига шлюза плюс набор обфускации, который
# обе стороны обязаны понимать одинаково. Бампается, когда меняется генерация
# конфига здесь, — тогда шлюз надо переприслать бандлом.
#
# Сверять её автоматически некому и не нужно: несовпадающие H1..H4/S1..S4/I1..I5
# ломают хендшейк, линк не встаёт, и бот сообщает об этом сам своим тиком
# живости. Штамп нужен человеку — чтобы на шлюзе было видно, чем его ставили.
LINK_CONTRACT="1"

MODE="plan"
case "${1:-}" in
    --apply)    MODE="apply" ;;
    --reassert) MODE="reassert" ;;
    --rollback) MODE="rollback" ;;
    --bundle)   MODE="bundle" ;;
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

# Юнит обязан ссылаться на ПОСТОЯННЫЙ путь, поэтому скрипт сначала ставит себя
# рядом с прочими локальными админскими командами и только потом пишет ExecStart.
#
# Прежде туда уходил `readlink -f "$0"` — каталог, ОТКУДА запустили. Поставка
# распаковывается во временный, а systemd-tmpfiles вычищает его через десять
# дней. Автозапуск умирал молча: интерфейс уже стоял, RemainAfterExit держал
# юнит «активным», и обнаруживалось это только при первой перезагрузке — уже в
# виде отказа без связи с каким-либо действием.
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

# ── бандл для шлюза: скрипт настройки + конфиг линка одним файлом ────────────
# Оператор несёт на малинку ОДИН файл вместо двух. Два — это лишний способ
# ошибиться: скопировал скрипт, забыл конфиг, и «почему-то не работает».
emit_gw_bundle() {
    _gwsrc="$(dirname "$(readlink -f "$0")")/routing-gw-setup.sh"
    [ -f "$_gwsrc" ] || {
        say "ОШИБКА: рядом нет routing-gw-setup.sh (искал $_gwsrc)"; exit 1; }
    [ -f "$GW_CONF_OUT" ] || {
        say "ОШИБКА: нет $GW_CONF_OUT — линк ещё не поднят. Сначала: $0 --apply"
        exit 1; }

    {
        cat <<HDREOF
#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# awg-gw-bundle.sh — сторона ШЛЮЗА условной маршрутизации, одним файлом.
#
# Собран на ВПС: $(date '+%Y-%m-%d %H:%M %z')
# Контракт линка: $LINK_CONTRACT
#
# ВНУТРИ ПРИВАТНЫЙ КЛЮЧ И PSK. Файл секретный: права 600, после установки удалить.
#
# ЗАПУСК НА ШЛЮЗЕ:
#   sudo sh awg-gw-bundle.sh              # применить
#   sudo sh awg-gw-bundle.sh --rollback   # снять всё, что поставил
# ─────────────────────────────────────────────────────────────────────────────
HDREOF
        # Подсеть ВШИВАЕТСЯ в бандл: на шлюзе нет app.yaml, взять её там неоткуда,
        # а env-дефолт скрипта вернул бы после ребута правила для чужой подсети.
        printf 'CLIENT_SUBNET="${CLIENT_SUBNET:-%s}"\nexport CLIENT_SUBNET\n' "$CLIENT_SUBNET"
        cat <<'BODYEOF'
set -e
[ "$(id -u)" = "0" ] || { echo "нужен root: sudo sh $0"; exit 1; }

# Раскладываем в ПОСТОЯННЫЙ каталог, а не во временный. Скрипт настройки
# прописывает СЕБЯ в systemd-юнит по собственному пути: запущенный из /tmp, он
# оставил бы юнит, указывающий на удалённый файл. Обнаружилось бы это только
# после ребута и выглядело бы как «шлюз сам отвалился».
DEST="/opt/awg-gw"
mkdir -p "$DEST"

sed -n '/^#__GW_SETUP_BELOW__$/,$p' "$0" | tail -n +2 > "$DEST/routing-gw-setup.sh"
chmod 0755 "$DEST/routing-gw-setup.sh"

BODYEOF
        printf '%s\n' "cat > \"\$DEST/link.conf\" <<'__LINK_CONF_EOF__'"
        printf '# awg-bot: контракт линка %s\n' "$LINK_CONTRACT"
        cat "$GW_CONF_OUT"
        printf '%s\n' "__LINK_CONF_EOF__"
        cat <<'TAILEOF'
chmod 0600 "$DEST/link.conf"

exec "$DEST/routing-gw-setup.sh" "${1:---apply}" "$DEST/link.conf"
TAILEOF
        printf '#__GW_SETUP_BELOW__\n'
        cat "$_gwsrc"
    } > "$GW_BUNDLE_OUT"
    chmod 600 "$GW_BUNDLE_OUT"
}

# Инструкция печатается и после --apply, и после --bundle: человек читает её в
# момент, когда идёт к шлюзу, а не когда поднимал линк.
print_gw_instructions() {
    say ""
    say "Бандл для шлюза: $GW_BUNDLE_OUT   (контракт линка: $LINK_CONTRACT)"
    say ""
    say "Скопировать на шлюз:"
    say "    scp $GW_BUNDLE_OUT <юзер>@<адрес-шлюза>:~/"
    say ""
    say "И там:"
    say "    sudo sh ~/awg-gw-bundle.sh"
    say "    rm ~/awg-gw-bundle.sh          # внутри приватный ключ"
    say ""
    say "Пересобрать бандл позже (ключи НЕ меняются): $0 --bundle"
}

if [ "$MODE" = "bundle" ]; then
    emit_gw_bundle
    print_gw_instructions
    exit 0
fi

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
    run "rm -f $UNIT $CONF $GW_CONF_OUT $GW_BUNDLE_OUT"
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
        say "Пересобрать бандл:  $0 --bundle"
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
    say "  6. собрать бандл для шлюза в $GW_BUNDLE_OUT"
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
SELF="$(install_self)"
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

step "6. Бандл для шлюза"
emit_gw_bundle
say "  собран (права 600 — внутри приватный ключ и psk)"
print_gw_instructions
say ""
say "Затем на ВПС в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
say "и перезапустить бота. До этого фича спит."
