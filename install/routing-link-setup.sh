#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-link-setup.sh — линк-туннель ВПС ↔ шлюз под условную маршрутизацию.
# Запускается НА ВПС. См. концепт «условная маршрутизация», §11.
#
# ЗАЧЕМ ОТДЕЛЬНЫЙ ТУННЕЛЬ. У шлюза уже есть туннель к клиентскому интерфейсу
# ВПС — им пользуется прежняя ручная схема на шлюзе. Трогать его нельзя: перенос
# сломал бы работающее ради ещё не запущенного. Поэтому поднимаем ВТОРОЙ туннель,
# только под фичу. Откат — погасить интерфейс, прежняя схема не заметит.
#
# ПОЧЕМУ Table = off. У пира шлюза AllowedIPs = 0.0.0.0/0 — иначе нельзя слать
# туда произвольные адреса назначения. Увидев такое, awg-quick пропишет маршрут
# ПО УМОЛЧАНИЮ, и весь трафик ВПС уйдёт в туннель до шлюза вместе с этой
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
# ОКРУЖЕНИЕ: LINK_IF (awglink; у второго слота — awglink2), LINK_PORT (443),
# LINK_CIDR — /30 линка, из него выводятся LINK_VPS_ADDR и LINK_GW_ADDR,
# ENDPOINT_HOST, PEER_HOME_NETS (подсети за другими шлюзами — в AllowedIPs
# пира ВПС), CONF_DIR, GW_CONF_OUT, GW_BUNDLE_OUT. Из конфига бота (_APP_YAML,
# /etc/awg-bot/conf/app.yaml) и перечитываются при каждой сборке бандла:
# CLIENT_SUBNET (network.subnet_cidr) и LINK_KEEPALIVE
# (client_config.keepalive_seconds) — PersistentKeepalive линка идёт в ритме
# клиентских пиров, «25-35» или одиночное число; непонятное значение
# откатывается к «25-35». Любую переменную можно переопределить окружением;
# бот при сборке бандла так и делает с LINK_KEEPALIVE — читает YAML целиком,
# а awk здесь видит только значение в двойных кавычках (число без кавычек у
# ручного `awg-bot gw-bundle` превратится в «25-35»).
# В БАНДЛ дополнительно вшиваются LINK_CHANNEL (1 по умолчанию — агент держит
# канал состояния до ВПС внутри линка, концепт «канал линка»; 0 — собрать
# конфигурацию без канала) и LINK_CHANNEL_PORT (routing.link_channel_port из
# app.yaml, 8787 по умолчанию — тот же ключ, на котором ВПС слушает). Обе
# уезжают в юнит обвязки на шлюзе строками Environment=, читает их агент;
# перевыпуск конфигурации шлюза и есть рубильник функции.
#
# ЗАПУСК:
#   sudo sh routing-link-setup.sh              # показать план
#   sudo sh routing-link-setup.sh --apply      # поднять
#   sudo sh routing-link-setup.sh --bundle     # пересобрать бандл (ключи те же)
#   sudo sh routing-link-setup.sh --rekey      # новые ключи линка (смена/снятие шлюза)
#   sudo sh routing-link-setup.sh --rollback   # снять
# Два режима зовёт юнит awg-link@<if>, руками они не нужны:
#   --reassert   довести обвязку и переподнять линк после загрузки (ExecStart)
#   --down       опустить линк (ExecStop; через скрипт, а не путём к awg-quick)
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="${LINK_IF:-awglink}"
# 443 ПО УМОЛЧАНИЮ, а не характерный для туннелей порт: UDP на 443 неотличим от
# QUIC и теряется в общем потоке, тогда как 51830 сам себя объявляет VPN. Порт —
# первое, что видит DPI, и менять его дороже, чем выбрать сразу. Занят чем-то
# другим — переопредели: LINK_PORT=... routing-link-setup.sh --apply
LINK_PORT="${LINK_PORT:-443}"
# /30 линка: адреса сторон выводятся из него (первый — ВПС, второй — шлюз),
# чтобы второй слот (концепт «резервный шлюз») задавался одной переменной.
LINK_CIDR="${LINK_CIDR:-10.99.99.0/30}"
_cidr_base="${LINK_CIDR%/*}"
_cidr_last="${_cidr_base##*.}"
_cidr_head="${_cidr_base%.*}"
LINK_VPS_ADDR="${LINK_VPS_ADDR:-$_cidr_head.$(( _cidr_last + 1 ))}"
LINK_GW_ADDR="${LINK_GW_ADDR:-$_cidr_head.$(( _cidr_last + 2 ))}"
# Из конфига бота, не из хардкода: юнит зовёт --reassert без окружения, и после
# смены клиентской подсети исключение из MASQUERADE обязано реассертиться для
# новой — иначе шлюз после ребута видит клиентов чужим адресом.
_APP_YAML="${_APP_YAML:-/etc/awg-bot/conf/app.yaml}"
_cfg_subnet="$(awk -F'"' '/^  subnet_cidr:/{print $2; exit}' "$_APP_YAML" 2>/dev/null || true)"
CLIENT_SUBNET="${CLIENT_SUBNET:-${_cfg_subnet:-10.8.1.0/24}}"
# PersistentKeepalive линка — ИЗ ТОГО ЖЕ КЛЮЧА, что у клиентских пиров
# (client_config.keepalive_seconds). Прибитые 25 секунд — метроном ванильного
# WireGuard: на простаивающем туннеле он виден без всякой расшифровки, а линк
# простаивает ровно тогда, когда за ним никто не ходит. Диапазон разбирают
# тулзы AmneziaWG 3.x (u16_range_from_string) — их поставка и ставит обеим
# сторонам. Одиночное число тут законно: его ставят ради клиентов
# дореформенного поколения, и линк тогда честно повторяет их ритм.
_cfg_keepalive="$(awk -F'"' '/^  keepalive_seconds:/{print $2; exit}' "$_APP_YAML" 2>/dev/null || true)"
LINK_KEEPALIVE="${LINK_KEEPALIVE:-${_cfg_keepalive:-25-35}}"
case "$LINK_KEEPALIVE" in
    ''|*[!0-9-]*|-*|*-|*-*-*) LINK_KEEPALIVE="25-35" ;;
esac
# Порт канала — тот же ключ, на котором ВПС слушает (routing.link_channel_port).
# Бандл, собранный с другим числом, увёз бы шлюз стучаться в порт, где никого
# нет, и канал молча не поднялся бы. Бот передаёт значение и окружением; отсюда
# оно нужно CLI `awg-bot gw-bundle`, которому окружение никто не готовит.
_cfg_chport="$(awk '/^  link_channel_port:/{print $2; exit}' "$_APP_YAML" 2>/dev/null | tr -cd '0-9' || true)"
LINK_CHANNEL_PORT="${LINK_CHANNEL_PORT:-${_cfg_chport:-8787}}"
CONF_DIR="${CONF_DIR:-/etc/amnezia/amneziawg}"
CONF="$CONF_DIR/$LINK_IF.conf"
GW_CONF_OUT="${GW_CONF_OUT:-/root/gw-$LINK_IF.conf}"
# Подсети за другими шлюзами (концепт «локальная сеть», функция B): в AllowedIPs
# пира ВПС в конфиге шлюза — тогда awg-quick сам ставит маршруты в них через
# линк. Приходит от бота при сборке; пусто — как раньше.
PEER_HOME_NETS="$(printf '%s' "${PEER_HOME_NETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"
gw_allowed_ips() {             # клиентская подсеть, /30 линка, чужие подсети — через запятую
    _a="$CLIENT_SUBNET, $LINK_CIDR"
    for _n in $PEER_HOME_NETS; do _a="$_a, $_n"; done
    printf '%s' "$_a"
}
# Бандл первого линка — под прежним именем: его ждут инструкции и установщик
# на шлюзе; у остальных слотов имя с интерфейсом, чтобы файлы не перетирались.
if [ "$LINK_IF" = "awglink" ]; then
    GW_BUNDLE_OUT="${GW_BUNDLE_OUT:-/root/awg-gw-bundle.sh}"
else
    GW_BUNDLE_OUT="${GW_BUNDLE_OUT:-/root/awg-gw-bundle-$LINK_IF.sh}"
fi
# Юнит — ШАБЛОН по интерфейсу (awg-link@awglink, awg-link@awglink2): линков
# может быть несколько, по одному на слот шлюза. Прежний awg-link.service
# переезжает на шаблон при первом --reassert (см. migrate_unit).
UNIT_TEMPLATE="/etc/systemd/system/awg-link@.service"
UNIT_INSTANCE="awg-link@$LINK_IF.service"
LEGACY_UNIT="/etc/systemd/system/awg-link.service"

# Версия КОНТРАКТА ЛИНКА: формат конфига шлюза плюс набор обфускации, который
# обе стороны обязаны понимать одинаково. Бампается, когда меняется генерация
# конфига здесь, — тогда шлюз надо переприслать бандлом.
#
# Сверять её автоматически некому и не нужно: несовпадающие H1..H4/S1..S4/I1..I5
# ломают хендшейк, линк не встаёт, и бот сообщает об этом сам своим тиком
# живости. Штамп нужен человеку — чтобы на шлюзе было видно, чем его ставили.
LINK_CONTRACT="1"

MODE="plan"
REKEY=0
case "${1:-}" in
    --apply)    MODE="apply" ;;
    # Новые ключи линка при живом линке: смена или снятие шлюза. Прежняя
    # машина теряет линк по построению — ей ничего не нужно сообщать.
    --rekey)    MODE="apply"; REKEY=1 ;;
    --reassert) MODE="reassert" ;;
    # ExecStop юнита: опустить линк. Через скрипт, а не абсолютным путём к
    # awg-quick — тулзы ставятся туда, куда собрал make, и захардкоженный
    # /usr/bin однажды не сошёлся: ExecStop падал, интерфейс оставался жить.
    --down)     MODE="down" ;;
    --rollback) MODE="rollback" ;;
    --bundle)   MODE="bundle" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  sed -n '2,50p' "$0"; exit 0 ;;
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

# Переезд первого линка с awg-link.service на шаблон: интерфейс не трогаем (он
# поднят), меняется только то, что его поднимает после ребута. Идемпотентно.
migrate_unit() {
    # Шаблон с прежним ExecStop (абсолютный путь к awg-quick) — переписать:
    # иначе перезапуск юнита не опускает линк, и смена порта не применяется.
    if [ -f "$UNIT_TEMPLATE" ] && ! grep -q -- '--down' "$UNIT_TEMPLATE"; then
        SELF="$(install_self)"
        write_unit_template
        run "systemctl daemon-reload"
        say "  шаблон юнита обновлён: остановка через $SELF --down"
    fi
    [ -f "$LEGACY_UNIT" ] || return 0
    [ "$LINK_IF" = "awglink" ] || return 0
    SELF="$(install_self)"
    [ -f "$UNIT_TEMPLATE" ] || write_unit_template
    run "systemctl disable awg-link.service 2>/dev/null || true"
    run "rm -f $LEGACY_UNIT"
    run "systemctl daemon-reload"
    run "systemctl enable $UNIT_INSTANCE"
    say "  юнит переведён на шаблон: $UNIT_INSTANCE"
}

# Шаблон юнита: один файл на все линки, экземпляр — по имени интерфейса.
# Реассерт зовёт этот же скрипт с LINK_IF из имени экземпляра. $SELF —
# постоянный путь скрипта (install_self), выставляется до вызова.
write_unit_template() {
    cat > "$UNIT_TEMPLATE" <<UNITEOF
[Unit]
Description=awg-bot: линк-туннель до шлюза условной маршрутизации (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=LINK_IF=%i
# --reassert, а не только awg-quick: правила iptables эфемерны, и исключение
# линка из MASQUERADE после ребута пришлось бы ставить заново вручную.
ExecStart=$SELF --reassert
ExecStop=$SELF --down

[Install]
WantedBy=multi-user.target
UNITEOF
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
    # Соседа ищем рядом, затем в поставке: юниты зовут КОПИЮ этого скрипта из
    # /usr/local/sbin, где gw-скрипт не живёт, — без фолбэка запуск --bundle
    # оттуда падал, хотя поставка лежит на месте.
    _gwsrc="$(dirname "$(readlink -f "$0")")/routing-gw-setup.sh"
    [ -f "$_gwsrc" ] || _gwsrc="/opt/awg-bot/install/routing-gw-setup.sh"
    [ -f "$_gwsrc" ] || {
        say "ОШИБКА: routing-gw-setup.sh не найден ни рядом, ни в /opt/awg-bot/install"
        exit 1; }
    [ -f "$GW_CONF_OUT" ] || {
        say "ОШИБКА: нет $GW_CONF_OUT — линк ещё не поднят. Сначала: $0 --apply"
        exit 1; }
    # Endpoint в конфиге шлюза ЗАМЕРЗАЕТ при генерации, а порт линка — факт из
    # живого $CONF. Разошлись — бандл увёз бы шлюз в закрытый порт, и линк умер
    # бы до ручной правки (наступили: перенос 443 → 47231). Правим источник до
    # упаковки; сам конфиг линка не трогаем.
    if [ -f "$CONF" ]; then
        _live_port="$(awk '/^ListenPort/{print $3; exit}' "$CONF" 2>/dev/null || true)"
        if [ -n "$_live_port" ] && ! grep -q ":$_live_port\$" "$GW_CONF_OUT"; then
            say "Endpoint в $GW_CONF_OUT отстал от ListenPort=$_live_port — правлю"
            # sed -i без суффикса — GNU-изм; скрипт POSIX, BSD sed съедает
            # выражение как суффикс. Временный файл переносим везде.
            sed "s/^\(Endpoint = .*\):[0-9][0-9]*\$/\1:$_live_port/" "$GW_CONF_OUT" > "$GW_CONF_OUT.tmp" \
                && mv "$GW_CONF_OUT.tmp" "$GW_CONF_OUT"
        fi
    fi

    # AllowedIPs пира ВПС зависит от подсетей за другими шлюзами, а те меняются
    # после --apply: правим строку в источнике при каждой сборке, как порт.
    _want="AllowedIPs = $(gw_allowed_ips)"
    if [ -f "$GW_CONF_OUT" ] && ! grep -qxF "$_want" "$GW_CONF_OUT"; then
        say "AllowedIPs в $GW_CONF_OUT отстал (локальные подсети других шлюзов) — правлю"
        sed "s|^AllowedIPs = .*\$|$_want|" "$GW_CONF_OUT" > "$GW_CONF_OUT.tmp" \
            && mv "$GW_CONF_OUT.tmp" "$GW_CONF_OUT"
    fi

    # PersistentKeepalive — тоже из конфига бота, и тоже замерзает при
    # генерации: шлюзы, выданные до того, как значение стало диапазоном,
    # остались бы с прибитыми 25 навсегда. Правим строку при каждой сборке.
    _want_ka="PersistentKeepalive = $LINK_KEEPALIVE"
    if [ -f "$GW_CONF_OUT" ] && grep -q '^PersistentKeepalive = ' "$GW_CONF_OUT" \
        && ! grep -qxF "$_want_ka" "$GW_CONF_OUT"; then
        say "PersistentKeepalive в $GW_CONF_OUT отстал — правлю на $LINK_KEEPALIVE"
        sed "s|^PersistentKeepalive = .*\$|$_want_ka|" "$GW_CONF_OUT" > "$GW_CONF_OUT.tmp" \
            && mv "$GW_CONF_OUT.tmp" "$GW_CONF_OUT"
    fi
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
#   sudo sh awg-gw-bundle.sh --install    # чистая машина: поставить агента из
#                                         # поставки внутри файла и применить
#   sudo sh awg-gw-bundle.sh              # применить (обвязка, линк, аплинк)
#   sudo sh awg-gw-bundle.sh --rollback   # снять всё, что поставил
# ─────────────────────────────────────────────────────────────────────────────
HDREOF
        # Подсеть ВШИВАЕТСЯ в бандл: на шлюзе нет app.yaml, взять её там неоткуда,
        # а env-дефолт скрипта вернул бы после ребута правила для чужой подсети.
        printf 'CLIENT_SUBNET="${CLIENT_SUBNET:-%s}"\nexport CLIENT_SUBNET\n' "$CLIENT_SUBNET"
        # Устройства админа — им с туннеля открыт сам шлюз и локальная сеть за
        # ним. Список знает только бот ВПС: он передаёт его окружением при
        # сборке, бандл вшивает, юнит на шлюзе закрепляет. Сменился состав —
        # новый бандл (бот сам напомнит).
        printf 'ADMIN_IPS="${ADMIN_IPS:-%s}"\nexport ADMIN_IPS\n' "$(printf '%s' "${ADMIN_IPS:-}" | tr -cd '0-9./ ')"
        # Помеченный шлюз: ключ его аплинка (в окне переезда — ключ двойника и
        # старый ключ отдельно) и конфиг аплинка в base64. По ключу агент поймёт,
        # он ли шлюз; конфиг ставится только машине с тем же ключом.
        printf 'GATEWAY_PUBKEY="%s"\nexport GATEWAY_PUBKEY\n' "$(printf '%s' "${GATEWAY_PUBKEY:-}" | tr -cd 'A-Za-z0-9+/=')"
        printf 'GATEWAY_PREV_PUBKEY="%s"\nexport GATEWAY_PREV_PUBKEY\n' "$(printf '%s' "${GATEWAY_PREV_PUBKEY:-}" | tr -cd 'A-Za-z0-9+/=')"
        printf 'UPLINK_B64="%s"\nexport UPLINK_B64\n' "$(printf '%s' "${UPLINK_B64:-}" | tr -cd 'A-Za-z0-9+/=')"
        # «За шлюзом — без VPN» (концепт «локальная сеть»): включена ли функция у
        # слота, локальные подсети (первая даёт LAN-интерфейс и адрес
        # резолвера), апстрим резолвера — свой резолвер ВПС через аплинк.
        printf 'LAN_MODE="%s"\nexport LAN_MODE\n' "$(printf '%s' "${LAN_MODE:-0}" | tr -cd '01' | cut -c1)"
        printf 'HOME_SUBNETS="%s"\nexport HOME_SUBNETS\n' "$(printf '%s' "${HOME_SUBNETS:-}" | tr -cd '0-9./ ')"
        printf 'RESOLVER="%s"\nexport RESOLVER\n' "$(printf '%s' "${RESOLVER:-}" | tr -cd '0-9.')"
        printf 'PEER_HOME_NETS="%s"\nexport PEER_HOME_NETS\n' "$PEER_HOME_NETS"
        # Имя ВПС — для панели агента («Линк до …»): на шлюзе взять его неоткуда.
        printf 'SERVER_NAME="%s"\n' "$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-' | cut -c1-64)"
        # Канал ВПС ↔ шлюз внутри линка (концепт «канал линка»): включается
        # бандлом и только им. Агент, не получивший этих строк, никуда не
        # ходит — перевыпуск конфигурации и есть рубильник функции.
        printf 'LINK_CHANNEL="%s"\nexport LINK_CHANNEL\n' "$(printf '%s' "${LINK_CHANNEL:-1}" | tr -cd '01' | cut -c1)"
        printf 'LINK_CHANNEL_PORT="%s"\nexport LINK_CHANNEL_PORT\n' "$(printf '%s' "${LINK_CHANNEL_PORT:-8787}" | tr -cd '0-9' | cut -c1-5)"
        cat <<'BODYEOF'
set -e
[ "$(id -u)" = "0" ] || { echo "нужен root: sudo sh $0"; exit 1; }

# --install: поставить агента ИЗ ЭТОГО ЖЕ файла. Поставка вшита ниже базой64
# между маркерами: шлюз стоит в России, GitHub там без туннеля недоступен, а
# туннель как раз и ставим — качать с шлюза неоткуда. Распаковываем во
# временный каталог (установщик уберёт его сам) и передаём управление
# установщику из поставки; он применит этот файл как --bundle, и после
# успеха файл удалит себя. --skip-verify: sha256 с релизом такая сборка не
# совпадает (собрана из установки ВПС), доверие держит scp с машины админа.
if [ "${1:-}" = "--install" ]; then
    _self="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    _src="$(mktemp -d /tmp/awg-bot-install.XXXXXX)"
    sed -n '/^#__AWG_BOT_TGZ_BELOW__$/,/^#__AWG_BOT_TGZ_END__$/p' "$_self" | sed '1d;$d' \
        | base64 -d > "$_src/awg-bot.tgz" 2>/dev/null || true
    [ -s "$_src/awg-bot.tgz" ] || { rm -rf "$_src"; \
        echo "в этом файле нет поставки: он собран без неё (awg-bot gw-bundle). Выпусти конфигурацию из основного бота."; exit 1; }
    tar xzf "$_src/awg-bot.tgz" -C "$_src" || { rm -rf "$_src"; echo "поставка внутри файла не распаковалась"; exit 1; }
    [ -f "$_src/install/awg-bot-install.sh" ] || { rm -rf "$_src"; echo "в поставке нет установщика — не та поставка?"; exit 1; }
    exec bash "$_src/install/awg-bot-install.sh" --skip-verify --role gateway --bundle "$_self"
fi

# Раскладываем в ПОСТОЯННЫЙ каталог, а не во временный. Скрипт настройки
# прописывает СЕБЯ в systemd-юнит по собственному пути: запущенный из /tmp, он
# оставил бы юнит, указывающий на удалённый файл. Обнаружилось бы это только
# после ребута и выглядело бы как «шлюз сам отвалился».
DEST="/opt/awg-gw"
mkdir -p "$DEST"

sed -n '/^#__GW_SETUP_BELOW__$/,$p' "$0" | tail -n +2 > "$DEST/routing-gw-setup.sh"
# Конвейер прячет отказ sed за кодом tail: пустой скрипт обвязки затем
# исполнился бы «успешно», а юнит реассерта остался бы с пустым файлом.
[ -s "$DEST/routing-gw-setup.sh" ] || { echo "бандл повреждён: скрипт обвязки не извлёкся"; exit 1; }
chmod 0755 "$DEST/routing-gw-setup.sh"

BODYEOF
        printf '%s\n' "cat > \"\$DEST/link.conf\" <<'__LINK_CONF_EOF__'"
        printf '# awg-bot: контракт линка %s\n' "$LINK_CONTRACT"
        cat "$GW_CONF_OUT"
        printf '%s\n' "__LINK_CONF_EOF__"
        cat <<'TAILEOF'
chmod 0600 "$DEST/link.conf"

# Внутри приватный ключ: после успешного применения файл удаляет себя сам,
# чтобы не полагаться на память человека. Отказ — файл остаётся для повтора.
"$DEST/routing-gw-setup.sh" "${1:---apply}" "$DEST/link.conf"
_rc=$?
[ "$_rc" -eq 0 ] && rm -f -- "$0" 2>/dev/null
exit "$_rc"
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
    say "    sudo sh ~/$(basename "$GW_BUNDLE_OUT")"
    say "    rm ~/$(basename "$GW_BUNDLE_OUT")          # внутри приватный ключ"
    say "(агента этот бандл не ставит: поставку везёт только файл из основного бота)"
    say ""
    say "Пересобрать бандл позже (ключи НЕ меняются): LINK_IF=$LINK_IF $0 --bundle"
}

if [ "$MODE" = "bundle" ]; then
    emit_gw_bundle
    print_gw_instructions
    exit 0
fi

if [ "$MODE" = "down" ]; then
    ip link show "$LINK_IF" >/dev/null 2>&1 && run "awg-quick down $LINK_IF"
    exit 0
fi

if [ "$MODE" = "reassert" ]; then
    [ -f "$CONF" ] || { say "линк не настроен ($CONF нет) — нечего поднимать"; exit 0; }
    migrate_unit
    if ip link show "$LINK_IF" >/dev/null 2>&1; then
        # Поднятый линк переподнимаем только если ядро разошлось с конфигом:
        # порт сменили руками в conf — перезапуск юнита обязан это применить,
        # а не молча оставить старый.
        _want="$(awk '/^ListenPort/{print $3; exit}' "$CONF" 2>/dev/null)"
        _live="$(awg show "$LINK_IF" listen-port 2>/dev/null)"
        if [ -n "$_want" ] && [ -n "$_live" ] && [ "$_want" != "$_live" ]; then
            say "  порт линка в конфиге $_want, в ядре $_live — переподнимаю"
            run "awg-quick down $LINK_IF"
            run "awg-quick up $LINK_IF"
        fi
    else
        run "awg-quick up $LINK_IF"
    fi
    assert_nat_exempt
    exit 0
fi

# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Снятие линка $LINK_IF"
    run "systemctl disable --now $UNIT_INSTANCE 2>/dev/null || true"
    if [ "$LINK_IF" = "awglink" ]; then
        run "systemctl disable --now awg-link.service 2>/dev/null || true"
        run "rm -f $LEGACY_UNIT"
    fi
    run "awg-quick down $LINK_IF 2>/dev/null || true"
    run "rm -f $CONF $GW_CONF_OUT $GW_BUNDLE_OUT"
    # шаблон юнита общий на все линки: снимаем, только если экземпляров не осталось
    if ! ls /etc/systemd/system/multi-user.target.wants/awg-link@*.service >/dev/null 2>&1; then
        run "rm -f $UNIT_TEMPLATE"
    fi
    run "systemctl daemon-reload"
    while iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$LINK_IF" \
          -j ACCEPT 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $CLIENT_SUBNET -o $LINK_IF -j ACCEPT"
    done
    say ""
    say "Готово. Туннель шлюза и обвязка хоста не тронуты."
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
if [ "$REKEY" = "1" ] && [ -f "$CONF" ]; then
    step "0. Смена ключей линка"
    say "  прежний конфиг линка снимается, ключи генерируются заново"
    run "awg-quick down $LINK_IF 2>/dev/null || true"
    run "rm -f $CONF"
fi
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
    say "Пересоздать с нуля: сначала $0 --rollback — сменятся ключи, и шлюз"
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
say "  интерфейс         : $LINK_IF ($LINK_VPS_ADDR ↔ $LINK_GW_ADDR)"
say "  порт              : $LINK_PORT"
say "  эндпоинт для шлюза: $ENDPOINT_HOST:$LINK_PORT"
say "  конфиг сервера AWG: $CONF"
say "  конфиг для шлюза  : $GW_CONF_OUT"
if [ "$MODE" = "plan" ]; then
    say ""
    say "(режим показа: ключи не генерируются, ничего не меняется — добавь --apply)"
    say ""
    say "Будет сделано:"
    say "  1. awg genkey ×2 + genpsk, случайная обфускация"
    say "  2. записать $CONF (Table = off!) и поднять awg-quick up $LINK_IF"
    say "  3. iptables -t nat -I POSTROUTING -s $CLIENT_SUBNET -o $LINK_IF -j ACCEPT"
    say "  4. юнит awg-link.service + enable"
    say "  5. записать конфигурацию шлюза в $GW_CONF_OUT"
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
say "  Table = off — иначе AllowedIPs=0.0.0.0/0 у пира увёл бы весь трафик сервера AWG"
say "  в шлюз вместе с этой SSH-сессией"
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
say "  адрес сервера AWG вместо адреса клиента — и различать клиентов стало бы нечем."
assert_nat_exempt

# ── 4. автозапуск ────────────────────────────────────────────────────────────
step "4. Автозапуск"
SELF="$(install_self)"
write_unit_template
if [ -f "$LEGACY_UNIT" ] && [ "$LINK_IF" = "awglink" ]; then
    run "systemctl disable awg-link.service 2>/dev/null || true"
    run "rm -f $LEGACY_UNIT"
fi
run "systemctl daemon-reload"
run "systemctl enable $UNIT_INSTANCE"

# ── 5. конфиг для малинки ────────────────────────────────────────────────────
step "5. Конфиг для шлюза → $GW_CONF_OUT"
cat > "$GW_CONF_OUT" <<GWEOF
# Линк до ВПС под условную маршрутизацию. Положить на шлюз как ОТДЕЛЬНЫЙ
# интерфейс (например /etc/amnezia/amneziawg/awglink.conf) — существующий awg0
# с прежней ручной схемой НЕ трогать.
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
AllowedIPs = $(gw_allowed_ips)
PersistentKeepalive = $LINK_KEEPALIVE
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
if [ "$LINK_IF" = "awglink" ]; then
    say "Затем на сервере AWG в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
    say "и перезапустить бота. До этого фича спит."
fi
