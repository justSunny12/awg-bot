#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-link-setup.sh — линк-туннель ВПС ↔ шлюз под условную маршрутизацию.
# Запускается НА ВПС.
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
#   4) пишет конфиг шлюзовой стороны ($GW_CONF_OUT, 0600). Файла конфигурации
#      для шлюза больше нет: шлюз подключается командой из бота сервера AWG
#      (токен строит бот из этого конфига, режим --join-stage ниже), прежний
#      файл /root/awg-gw-bundle*.sh снимается вместе с линком;
#   5) обфускация линка — S2 переизбирается, пока S1 + 56 = S2 (длины init и
#      response совпали бы, рукопожатие не разбиралось).
#
# ОКРУЖЕНИЕ: LINK_IF (awglink; у второго слота — awglink2; остаются только
# [A-Za-z0-9_.-], не длиннее 15 символов), LINK_PORT (443),
# LINK_CIDR — /30 линка, из него выводятся LINK_VPS_ADDR и LINK_GW_ADDR,
# ENDPOINT_HOST (бот передаёт network.server_host в --apply и --join-stage: адрес
# ВПС сменился или на интерфейсе приватный адрес за 1:1 NAT — строка Endpoint
# конфига шлюзовой стороны правится при сборке, как порт), PEER_LAN_NETS (подсети других шлюзов — их
# префиксы в туннеле, /16 на слот, только слоту с VPN-транзитом — в AllowedIPs
# пира ВПС), CONF_DIR, GW_CONF_OUT. Из конфига бота (_APP_YAML,
# /etc/awg-bot/conf/app.yaml) и перечитываются при каждой сборке конфига шлюза:
# CLIENT_SUBNET (network.subnet_cidr) и LINK_KEEPALIVE
# (client_config.keepalive_seconds) — PersistentKeepalive линка идёт в ритме
# клиентских пиров, «25-35» или одиночное число; непонятное значение
# откатывается к «25-35». Любую переменную можно переопределить окружением;
# бот так и делает с LINK_KEEPALIVE — читает YAML целиком, а awk здесь видит
# только значение в двойных кавычках.
# GW_CONF_OUT (/root/gw-<линк>.conf) — источник и для документа конфигурации,
# который основной бот шлёт агенту по каналу конфигурации (domain/services/gwchannel/
# cfg.py): бот читает его сам и делает те же поправки Endpoint, AllowedIPs и
# PersistentKeepalive, что и здесь, — этот скрипт для документа не зовётся
# (зовётся только для смены ключей линка, режимы --rekey-* ниже).
#
# ЗАПУСК:
#   sudo sh routing-link-setup.sh              # показать план
#   sudo sh routing-link-setup.sh --apply      # поднять
#   sudo sh routing-link-setup.sh --rekey      # новые ключи линка (смена/снятие шлюза)
#   sudo sh routing-link-setup.sh --rollback   # снять
# Два режима зовёт юнит awg-link@<if>, руками они не нужны:
#   --reassert   довести обвязку и переподнять линк после загрузки (ExecStart)
#   --down       опустить линк (ExecStop; через скрипт, а не путём к awg-quick)
# Один режим зовут восстановление бэкапа и старт бота:
#   --adopt      линк с конфигом и без юнита (бэкап на пустом сервере): шаблон
#                юнита, enable --now экземпляра, исключение из MASQUERADE
# Четыре режима зовёт основной бот при смене ключей линка тем же устройством
# по каналу конфигурации («🔑 Ключи линка», domain/services/gateway_link/rekey.py),
# руками они не нужны. Меняются ключи обеих сторон, PSK и обфускация; порт
# (живой ListenPort из $CONF) и адреса /30 — те же; хост Endpoint — из
# ENDPOINT_HOST (network.server_host), без него — из $GW_CONF_OUT.
# Окружение — как у --apply (AllowedIPs шлюзовой стороны собираются заново).
# Шлюз из $GW_CONF_OUT.next сверяет только Address и берёт ключи, PSK и
# обфускацию; Endpoint, AllowedIPs и keepalive при переключении остаются из
# стоящего у него конфига (их ведёт документ конфигурации).
# Итог — последней строкой «rekey: staged|committed|reverted|dropped»:
#   --rekey-stage   новые конфиги в $CONF.next и $GW_CONF_OUT.next, живые не тронуты;
#                   LINK_I1 (бот: строка I1 клиентского интерфейса сервера AWG как
#                   есть) — строкой I1 только в шлюзовую сторону: сигнатурный пакет
#                   шлёт инициатор рукопожатия; сторона ВПС I1 не получает никогда,
#                   прочие режимы (--apply, --rekey) — тоже без I1
#   --rekey-commit  исключение из MASQUERADE (отказ — ключи не тронуты, код 1),
#                   живые → .prev, .next → живые, awg-quick down/up линка;
#                   не поднялся — .prev назад, код 1
#   --rekey-revert  .next снять; был коммит — .prev назад и переподнять линк
#   --rekey-drop    убрать .next и .prev (переход зафиксирован или снят)
# Три режима зовёт бот при подключении шлюза командой (domain/services/gwjoin):
#   --join-stage    линк слота на ключах токена: новые ключи и обфускация Jc…H4,
#                   без PSK и без I1 (их ставит смена ключей после кода), в
#                   конфиге стороны ВПС — штамп «# awg-bot: сопряжение <JOIN_ID>»
#                   (по нему файервол держит карантин); JOIN_PREV=1 (замена
#                   устройства) — сначала живые конфиги обеих сторон в .join-prev
#                   (если их там ещё нет); линк поднимается сразу. Итог — «join: staged»
#   --join-revert   .join-prev на место и линк на прежних ключах (замена отменена,
#                   срок вышел, код не подтверждён) — «join: reverted»; нечего — «join: nothing»
#   --join-drop     убрать .join-prev (верный код) — «join: dropped»
# ─────────────────────────────────────────────────────────────────────────────

set -e

LINK_IF="$(printf '%s' "${LINK_IF:-awglink}" | tr -cd 'A-Za-z0-9_.-' | cut -c1-15)"
# 443 ПО УМОЛЧАНИЮ, а не характерный для туннелей порт: UDP на 443 неотличим от
# QUIC и теряется в общем потоке, тогда как 51830 сам себя объявляет VPN. Порт —
# первое, что видит DPI, и менять его дороже, чем выбрать сразу. Занят чем-то
# другим — переопредели: LINK_PORT=... routing-link-setup.sh --apply
LINK_PORT="${LINK_PORT:-443}"
# /30 линка: адреса сторон выводятся из него (первый — ВПС, второй — шлюз),
# чтобы второй слот задавался одной переменной.
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
CONF_DIR="${CONF_DIR:-/etc/amnezia/amneziawg}"
CONF="$CONF_DIR/$LINK_IF.conf"
GW_CONF_OUT="${GW_CONF_OUT:-/root/gw-$LINK_IF.conf}"
# Подсети за другими шлюзами: в AllowedIPs
# пира ВПС в конфиге шлюза — тогда awg-quick сам ставит маршруты в них через
# линк. Приходит от бота при сборке; пусто — как раньше.
PEER_LAN_NETS="$(printf '%s' "${PEER_LAN_NETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"
gw_allowed_ips() {             # клиентская подсеть, /30 линка, чужие подсети — через запятую
    _a="$CLIENT_SUBNET, $LINK_CIDR"
    for _n in $PEER_LAN_NETS; do _a="$_a, $_n"; done
    printf '%s' "$_a"
}
# Прежний файл конфигурации шлюза (выпуски до 3.4.0) — только чтобы снять его
# вместе с линком: внутри ключи. Новых файлов скрипт не собирает.
if [ "$LINK_IF" = "awglink" ]; then
    GW_BUNDLE_OUT="${GW_BUNDLE_OUT:-/root/awg-gw-bundle.sh}"
else
    GW_BUNDLE_OUT="${GW_BUNDLE_OUT:-/root/awg-gw-bundle-$LINK_IF.sh}"
fi
# Юнит — ШАБЛОН по интерфейсу (awg-link@awglink, awg-link@awglink2): линков
# может быть несколько, по одному на слот шлюза. Прежний awg-link.service
# переезжает на шаблон при первом --reassert (см. migrate_unit).
UNIT_TEMPLATE="${UNIT_TEMPLATE:-/etc/systemd/system/awg-link@.service}"
UNIT_INSTANCE="awg-link@$LINK_IF.service"
LEGACY_UNIT="/etc/systemd/system/awg-link.service"

# Версия КОНТРАКТА ЛИНКА: формат конфига шлюза плюс набор обфускации, который
# обе стороны обязаны понимать одинаково. Бампается, когда меняется генерация
# конфига здесь, — тогда шлюзы переходят на него сменой ключей линка.
#
# Сверять её автоматически некому и не нужно: несовпадающие H1..H4/S1..S4/I1..I5
# ломают хендшейк, линк не встаёт, и бот сообщает об этом сам своим тиком
# живости. Штамп нужен человеку — чтобы на шлюзе было видно, чем его ставили.
LINK_CONTRACT="1"

MODE="plan"
REKEY=0
JOIN=0
JOIN_ID="$(printf '%s' "${JOIN_ID:-}" | tr -cd '0-9a-f' | cut -c1-16)"
case "${1:-}" in
    --apply)    MODE="apply" ;;
    # Новые ключи линка при живом линке: смена или снятие шлюза. Прежняя
    # машина теряет линк по построению — ей ничего не нужно сообщать.
    --rekey)    MODE="apply"; REKEY=1 ;;
    --reassert) MODE="reassert" ;;
    # Линк, конфиг которого уже лежит (восстановление бэкапа на пустом сервере
    # разложило его файлы), а юнита нет: поставить шаблон, включить экземпляр
    # и поднять — тем же --reassert. Ключи и файлы не трогает. Идемпотентно.
    --adopt)    MODE="adopt" ;;
    # ExecStop юнита: опустить линк. Через скрипт, а не абсолютным путём к
    # awg-quick — тулзы ставятся туда, куда собрал make, и захардкоженный
    # /usr/bin однажды не сошёлся: ExecStop падал, интерфейс оставался жить.
    --down)     MODE="down" ;;
    --rollback) MODE="rollback" ;;
    # Смена ключей линка каналом (бот на ВПС ведёт переход, шлюз подтверждает):
    # stage — новые ключи и обфускация в .next обеих сторон, живые не тронуты;
    # commit — живые → .prev, .next → живые, линк переподнят; revert — .prev
    # назад (или просто снять .next, если коммита не было); drop — убрать .prev
    # и .next после фиксации.
    --rekey-stage)  MODE="rekey-stage" ;;
    --rekey-commit) MODE="rekey-commit" ;;
    --rekey-revert) MODE="rekey-revert" ;;
    --rekey-drop)   MODE="rekey-drop" ;;
    --join-stage)   MODE="apply"; JOIN=1 ;;
    --join-revert)  MODE="join-revert" ;;
    --join-drop)    MODE="join-drop" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  awk 'NR>1 && !/^#/{exit} NR>1' "$0"; exit 0 ;;
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

# ── ключи, обфускация и конфиги обеих сторон: общее для --apply и перехода ────
rnd() {   # $1=min $2=max
    _r="$(od -An -N4 -tu4 /dev/urandom | tr -d ' ')"
    echo $(( _r % ($2 - $1 + 1) + $1 ))
}
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
gen_keys() {   # новые ключи обеих сторон, PSK и обфускация — в переменные
    VPS_PRIV="$(awg genkey)"
    VPS_PUB="$(printf '%s' "$VPS_PRIV" | awg pubkey)"
    GW_PRIV="$(awg genkey)"
    GW_PUB="$(printf '%s' "$GW_PRIV" | awg pubkey)"
    PSK="$(awg genpsk)"
    JC=$(rnd 3 10);    JMIN=$(rnd 8 15);   JMAX=$(rnd 40 70)
    S1=$(rnd 15 150);  S2=$(rnd 15 150);   S3=$(rnd 15 150);  S4=$(rnd 15 150)
    # S1 + 56 == S2 ломает разбор рукопожатия (длина init совпадает с response) —
    # переизбираем S2, как делает awg-server-init.sh; иначе ~0,4 % линков не встают
    while [ $((S1 + 56)) -eq "$S2" ]; do S2=$(rnd 15 150); done
    H1=$(h_range 5 500000000)
    H2=$(h_range 500000001 1000000000)
    H3=$(h_range 1000000001 1500000000)
    H4=$(h_range 1500000001 2000000000)
}
write_vps_conf() {   # $1 = куда: конфиг стороны ВПС, 0600
    cat > "$1" <<CONFEOF
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
    chmod 600 "$1"
    join_conf "$1" stamp
}
join_conf() {   # $1 = конфиг, $2 = stamp|plain: на ключах токена — без PSK; стороне ВПС — штамп сопряжения
    [ "$JOIN" = "1" ] || return 0
    _stamp=""
    [ "${2:-}" = "stamp" ] && _stamp="# awg-bot: сопряжение $JOIN_ID"
    STAMP="$_stamp" awk 'NR == 1 && ENVIRON["STAMP"] != "" { print ENVIRON["STAMP"] } !/^PresharedKey = / { print }' \
        "$1" > "$1.join" && cat "$1.join" > "$1" && rm -f "$1.join"
    chmod 600 "$1"
}
write_gw_conf() {   # $1 = куда, $2 = Endpoint (хост:порт): конфиг стороны шлюза, 0600
    cat > "$1" <<GWEOF
# Линк до ВПС под условную маршрутизацию. Положить на шлюз как ОТДЕЛЬНЫЙ
# интерфейс (например /etc/amnezia/amneziawg/awglink.conf) — существующий awg0
# с прежней ручной схемой НЕ трогать.
#
# AllowedIPs узкие намеренно: 0.0.0.0/0 увёл бы весь трафик шлюза в туннель.
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
Endpoint = $2
AllowedIPs = $(gw_allowed_ips)
PersistentKeepalive = $LINK_KEEPALIVE
GWEOF
    chmod 600 "$1"
    gw_conf_i1 "$1"
    join_conf "$1" plain
}
gw_conf_i1() {   # $1 = конфиг стороны шлюза: строка I1 после H4 — только в --rekey-stage и только с LINK_I1
    [ "$MODE" = "rekey-stage" ] || return 0
    _i1="$(printf '%s' "${LINK_I1:-}" | tr -d '\r\n')"
    [ -n "$_i1" ] || return 0
    LINK_I1_LINE="I1 = $_i1" awk '{ print } /^H4 = / && !done { print ENVIRON["LINK_I1_LINE"]; done = 1 }' "$1" > "$1.i1"
    cat "$1.i1" > "$1" && rm -f "$1.i1"
    chmod 600 "$1"
}
link_restart() {   # переподнять линк ВПС на том, что лежит в $CONF; 0 — поднялся
    ip link show "$LINK_IF" >/dev/null 2>&1 && { awg-quick down "$LINK_IF" >/dev/null 2>&1 || true; }
    awg-quick up "$LINK_IF"
}

# ── смена ключей линка: режимы перехода ──────────────────────────────────────
# Порт, адреса /30 и Endpoint — те же: меняются ключи обеих сторон, PSK и
# обфускация. Строка «rekey: …» последней — итог для бота.
if [ "$MODE" = "rekey-stage" ]; then
    [ -f "$CONF" ] && [ -f "$GW_CONF_OUT" ] || { say "ОШИБКА: линк $LINK_IF не настроен — менять нечего"; exit 1; }
    umask 077
    _port="$(awk '/^ListenPort/{print $3; exit}' "$CONF")"
    # хост — из настроек бота (ENDPOINT_HOST), как у --apply и документа:
    # /root/gw-*.conf после «✏️ Домен» помнит прежний
    if [ -n "${ENDPOINT_HOST:-}" ] && [ -n "$_port" ]; then
        _ep="$ENDPOINT_HOST:$_port"
    else
        _ep="$(awk '/^Endpoint/{print $3; exit}' "$GW_CONF_OUT")"
    fi
    [ -n "$_ep" ] && [ -n "$_port" ] || { say "ОШИБКА: в конфигах линка нет Endpoint или ListenPort"; exit 1; }
    LINK_PORT="$_port"
    gen_keys
    write_vps_conf "$CONF.next"
    write_gw_conf "$GW_CONF_OUT.next" "$_ep"
    say "rekey: staged"
    exit 0
fi
if [ "$MODE" = "rekey-commit" ]; then
    [ -f "$CONF.next" ] && [ -f "$GW_CONF_OUT.next" ] || { say "ОШИБКА: новых ключей ($CONF.next) нет — сначала --rekey-stage"; exit 1; }
    umask 077
    # исключение из MASQUERADE — до подмены: его отказ не должен случиться
    # после того, как новые ключи уже живые (бот счёл бы это возвратом)
    assert_nat_exempt || { say "ОШИБКА: исключение линка из MASQUERADE не поставлено — ключи не тронуты"; exit 1; }
    cp -p "$CONF" "$CONF.prev"
    cp -p "$GW_CONF_OUT" "$GW_CONF_OUT.prev"
    install -m 600 "$CONF.next" "$CONF"
    install -m 600 "$GW_CONF_OUT.next" "$GW_CONF_OUT"
    rm -f "$CONF.next" "$GW_CONF_OUT.next"
    if ! link_restart; then
        # новые ключи не подняли интерфейс — прежние назад, интерфейс на них
        install -m 600 "$CONF.prev" "$CONF"; install -m 600 "$GW_CONF_OUT.prev" "$GW_CONF_OUT"
        rm -f "$CONF.prev" "$GW_CONF_OUT.prev"
        link_restart || true
        say "ОШИБКА: линк на новых ключах не поднялся — прежние ключи на месте"
        exit 1
    fi
    say "rekey: committed"
    exit 0
fi
if [ "$MODE" = "rekey-revert" ]; then
    umask 077
    rm -f "$CONF.next" "$GW_CONF_OUT.next"
    if [ -f "$CONF.prev" ]; then
        install -m 600 "$CONF.prev" "$CONF"
        [ -f "$GW_CONF_OUT.prev" ] && install -m 600 "$GW_CONF_OUT.prev" "$GW_CONF_OUT"
        rm -f "$CONF.prev" "$GW_CONF_OUT.prev"
        link_restart || { say "ОШИБКА: линк на прежних ключах не поднялся"; exit 1; }
        assert_nat_exempt || say "ВНИМАНИЕ: исключение линка из MASQUERADE не поставлено — поставит --reassert"
    fi
    say "rekey: reverted"
    exit 0
fi
if [ "$MODE" = "rekey-drop" ]; then
    rm -f "$CONF.next" "$GW_CONF_OUT.next" "$CONF.prev" "$GW_CONF_OUT.prev"
    say "rekey: dropped"
    exit 0
fi

# ── сопряжение: возврат прежнего устройства и уборка ─────────────────────────
if [ "$MODE" = "join-revert" ]; then
    umask 077
    [ -f "$CONF.join-prev" ] || { say "join: nothing"; exit 0; }
    install -m 600 "$CONF.join-prev" "$CONF"
    [ -f "$GW_CONF_OUT.join-prev" ] && install -m 600 "$GW_CONF_OUT.join-prev" "$GW_CONF_OUT"
    rm -f "$CONF.join-prev" "$GW_CONF_OUT.join-prev"
    link_restart || { say "ОШИБКА: линк на прежних ключах не поднялся"; exit 1; }
    assert_nat_exempt || say "ВНИМАНИЕ: исключение линка из MASQUERADE не поставлено — поставит --reassert"
    say "join: reverted"
    exit 0
fi
if [ "$MODE" = "join-drop" ]; then
    rm -f "$CONF.join-prev" "$GW_CONF_OUT.join-prev"
    say "join: dropped"
    exit 0
fi

if [ "$MODE" = "down" ]; then
    ip link show "$LINK_IF" >/dev/null 2>&1 && run "awg-quick down $LINK_IF"
    exit 0
fi

if [ "$MODE" = "adopt" ]; then
    [ -f "$CONF" ] || { say "ОШИБКА: конфига линка $CONF нет — принимать нечего"; exit 1; }
    SELF="$(install_self)" || { say "ОШИБКА: скрипт линка не встал в /usr/local/sbin"; exit 1; }
    if [ ! -f "$UNIT_TEMPLATE" ] || ! grep -q -- '--down' "$UNIT_TEMPLATE"; then
        write_unit_template
        run "systemctl daemon-reload"
    fi
    run "systemctl enable --now $UNIT_INSTANCE"
    assert_nat_exempt
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
if [ "$JOIN" = "1" ]; then
    [ -n "$JOIN_ID" ] || { say "ОШИБКА: нет JOIN_ID — сопряжение без id не ставлю"; exit 1; }
    step "0. Линк слота на ключах токена"
    umask 077
    if [ "${JOIN_PREV:-0}" = "1" ] && [ -f "$CONF" ] && [ ! -f "$CONF.join-prev" ]; then
        # замена устройства: прежние ключи — на случай отмены, только раз за сопряжение
        cp -p "$CONF" "$CONF.join-prev"
        [ -f "$GW_CONF_OUT" ] && cp -p "$GW_CONF_OUT" "$GW_CONF_OUT.join-prev"
    fi
    if [ -f "$CONF" ]; then
        _jport="$(awk '/^ListenPort/{print $3; exit}' "$CONF")"
        [ -n "$_jport" ] && LINK_PORT="$_jport"
        run "awg-quick down $LINK_IF 2>/dev/null || true"
        run "rm -f $CONF"
    fi
fi
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
    exit 0
fi

# ── 1. ключи и обфускация ────────────────────────────────────────────────────
step "1. Ключи и обфускация"
umask 077
gen_keys
say "  ключи сгенерированы, обфускация своя (не совпадает с клиентским профилем)"

# ── 2. конфиг и подъём ───────────────────────────────────────────────────────
step "2. Конфиг $CONF и подъём интерфейса"
say "  Table = off — иначе AllowedIPs=0.0.0.0/0 у пира увёл бы весь трафик сервера AWG"
say "  в шлюз вместе с этой SSH-сессией"
mkdir -p "$CONF_DIR"
write_vps_conf "$CONF"
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

# ── 5. конфиг для шлюза ────────────────────────────────────────────────────
step "5. Конфиг для шлюза → $GW_CONF_OUT"
write_gw_conf "$GW_CONF_OUT" "$ENDPOINT_HOST:$LINK_PORT"
say "  записан (права 600 — внутри приватный ключ и psk)"

step "Проверка"
say "  awg show $LINK_IF"
say "  ip -br addr show $LINK_IF"

if [ "$JOIN" = "1" ]; then
    say "join: staged"
    exit 0
fi
say ""
if [ "$LINK_IF" = "awglink" ]; then
    say "Затем на сервере AWG в conf/app.yaml:  routing.gw_interface: \"$LINK_IF\""
    say "и перезапустить бота. До этого фича спит."
fi
