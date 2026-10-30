#!/bin/bash
#
# reserve-zone-setup.sh — гостевая зона резерва (netns awg-guest) на ВПС.
#
# Зона — сетевое пространство, в котором живут стороны постоянных линков
# шлюзов основы (всегда, пока есть пара — «оболочка») и, только на время
# переезда, клоны интерфейсов основы (основного и, если он есть, окна
# переезда профилей), veth к хосту, dnsmasq зоны и бот основы в гостевом
# режиме. UDP-сокеты интерфейсов AmneziaWG остаются у
# хоста: интерфейс создаётся в пространстве хоста, переносится в зону и там
# переименовывается; порт слушает хост, расшифрованный пакет выходит в зоне.
# На этом стоит вся схема, поэтому --selftest проверяет это на модуле ядра
# хоста.
#
# Запуск: root (кроме --plan); секретное — только через stdin и в хранилище в
# памяти, в argv и на постоянный диск не попадает. Окружение:
#   RSV_ZONE=awg-guest        имя пространства (и пространства журнала гостя)
#   ZONE_CIDR=169.254.77.0/30 veth бота основы (хост .1, зона .2)
#   ZONE_RF_CIDR=169.254.77.4/30  veth РФ-трафика к шлюзам резерва (хост .5, зона .6)
#   ZONE_TG_CIDR=169.254.77.8/30  veth выхода агентов шлюзов основы в Telegram при мёртвом
#                             аплинке (хост .9, зона .10): постоянный, в оболочке; из зоны
#                             через него — только tcp 443 на набор Telegram с адресов
#                             шлюзов в /30 линков, ≤ 8 соединений с источника (таблица
#                             маршрутов TG_TABLE=153 по источнику, не main зоны);
#                             awggx-h у хоста, awggx-n в зоне
#   ZONE_POOL=169.254.96.0/19 пул NETMAP: подсеть каждого клона 1:1 на свой блок той же
#                             длины (хост видит устройства основы поодиночке — CAKE
#                             делит загрузку по ним, а не по сайтам назначения)
#   INSTALL_DIR=/opt/awg-bot  где код (ExecStart юнита гостя)
#   RSV_RUN=/run/awg-bot-guest  хранилище гостя в памяти; RSV_STORE_SIZE=32M — его размер
#   RSV_VAR=/var/lib/awg-bot-guest  на диске только шифротекст под K: rep.bin
#                             (снимок базы гостя) или jrn/ (журнал данных гостя)
#   RSV_STATE_DIR=/run/awg-bot/rsv  state.json, open.status, guest.sock
#   RSV_LINKS_DIR=/var/lib/awg-bot/reserve/links  конфиги сторон постоянных линков
#   RSV_SELFTEST_PORT=45999   порт пробы --selftest
# Пути (RSV_ZONE, RSV_RUN, RSV_VAR, RSV_STATE_DIR, RSV_LINKS_DIR) и подсети
# (ZONE_CIDR, ZONE_RF_CIDR, ZONE_TG_CIDR, ZONE_POOL, RF_TABLE) обёртка awgbot/infra/rsvzone.py передаёт
# при КАЖДОМ вызове — из тех же констант и настроек, что у бота (стороны линков
# — по текущему каталогу данных); умолчания выше — для ручного запуска.
# Состояние зоны и разбор JSON — python бота ($INSTALL_DIR/venv/bin/python),
# запасной — системный python3; нет ни того, ни другого — режимы, которые
# пишут состояние, отказывают (состояние не затирается).
# и по режимам (их кладёт обёртка awgbot/infra/rsvzone.py): --take — GUEST_IF
# (имя клона, awg0), GUEST_ADDR (адрес клона cidr, обязателен), GUEST_DNS
# (адреса резолвера зоны через пробел), GUEST_UPSTREAMS, GUEST_ADMIN (адреса
# устройств админа), GUEST_EXTRA («имя=cidr …» — дополнительные клоны: интерфейсы
# окна переезда профилей, их конфиги процесс открытия уже положил в
# $RUN/awg/<имя>.conf), GUEST_CHANNEL_PORT (гостевой канал с линков шлюзов в
# зону, 8787; бот передаёт его и при --shell — routing.link_channel_port
# резерва), RF_TABLE (таблица РФ-доступа хоста для трафика из awggr-h; 100 —
# умолчание routing.table, обёртка его не передаёт); --serve — WEBHOOK_PORT; --relay — RELAY_TO (адрес:порт основы).
#
# Таблица nft зоны — inet awg_guest (input/forward с политикой drop, цепочка
# zone_post — наружу через veth: подсети клонов NETMAP 1:1 на блоки ZONE_POOL,
# прочее (бот основы, резолвер зоны), РФ-veth и veth awggx — маскарад; набор
# tg_out4 — диапазоны Telegram для выхода агентов шлюзов основы; вход к
# ответчику кворума на втором адресе клона — только с подблока линии,
# с блока зондов 169.254.88.0/24 прочее и пересылка — drop). Таблицу с NETMAP
# ядро ниже 5.8 отвергает — тогда маскарад для всех и строка ВНИМАНИЕ (netmap
# в state.json пуст, netmap_why=kernel). Рисуется целиком при каждом
# вызове (delete + create: повторные --shell/--take правил не дописывают); вход
# вебхука с veth (iifname awggv-n ip daddr <адрес зоны> tcp dport <порт>) — с
# --serve, когда порт известен (webhook_port из state.json). Файервол ХОСТА
# (порты клонов, маскарад veth и блоков пула, пересылка из awggv-h только во внешние интерфейсы
# хоста, из awggr-h — в линки слотов, из awggx-h — только во внешние интерфейсы
# на tcp 443 набора tg_out4, вход к ответчику кворума на awg0, DNAT вебхука с набора
# tg_pool4 и обучение пула набором tg_seen4, пересылка хвоста в relay по всем
# портам клонов — каждый на тот же порт основы) этот
# скрипт НЕ пишет: его рисует бот по состоянию зоны (state.json) в таблице
# awg_bot_guard. Скрипт также НЕ переносит данные основы (хранилище гостя
# заполняет tools/rsvopen.py между --store и --take), НЕ меняет DNS и НЕ ставит
# вебхук Telegram — это делают процесс открытия и бот основы на резерве.
#
# Режимы:
#   --plan        без root: store=tmpfs-noswap|ramfs, cake=yes|no, netns=, awg=,
#                 webhook_port=<свободный из 443/80/88/8443|none>, zone=present|absent,
#                 dnsmasq=yes|no (без dnsmasq резолвер зоны не поднимется),
#                 conntrack=yes|no (без него --relay-flush не переведёт живые
#                 потоки), python=yes|no (для состояния зоны); dnsmasq, python и
#                 webhook_port бот читает в готовность направления «к себе»
#                 (поле zone в st). Пакеты dnsmasq и conntrack скрипт НЕ ставит:
#                 по строкам «=no» их ставит бот (apt-get) при включении
#                 готовности, сопряжении и раз в 6 часов, пока чего-то нет
#   --selftest    «сокет у хоста» и диапазон keepalive на установленном модуле;
#                 код 0 — обе гипотезы подтверждены, строки RESULT_*
#   --shell       зона-оболочка: пространство, lo, таблица nft зоны, стороны
#                 постоянных линков шлюзов основы из $LINKS_DIR/<имя>.conf
#                 (интерфейс создаётся у хоста, awg setconf без строк
#                 Address/MTU/DNS/Table — их setconf не знает, — перенос в
#                 зону, имя awglink<N>, адрес из Address; уже стоящую сторону не
#                 трогает); постоянный veth awggx (ZONE_TG_CIDR) и правила
#                 `ip rule from <адреса шлюзов /30> lookup TG_TABLE` — выход агентов
#                 шлюзов основы в Telegram (в состоянии tg=up; не создался —
#                 строка ВНИМАНИЕ, оболочка встаёт без него; --take, --relay,
#                 --teardown и --wipe его не трогают). Конфиги пишет бот резерва по `slots` основы и
#                 зовёт этот режим; порты сторон во вход хоста открывает
#                 файервол хоста (бот), не скрипт
#   --store       хранилище гостя в памяти ($RUN: env, conf, data, awg): tmpfs
#                 с noswap (ядро 6.4+), иначе — и если noswap не принят — ramfs,
#                 свопируемого tmpfs не бывает. Перед процессом открытия его
#                 монтирует бот резерва в пространстве хоста (из песочницы
#                 юнита открытия смонтированное хосту не видно); заполняет
#                 процесс открытия до --take
#   --take        переезд (GUEST_PROBE_ADDR — второй адрес клона cidr для ответчика кворума,
#                 GUEST_PROBE_NET — подблок линии, GUEST_PROBE_PORT — порт ответчика, 8789:
#                 в таблицу зоны — вход к ответчику только с подблока, пересылки с блока зондов
#                 169.254.88.0/24 нет; состояние — probe_addr/probe_net/probe_port):
#                 хранилище гостя в памяти ($RUN), veth ×2 (awggv —
#                 бот основы и пользователи наружу; awggr — РФ-трафик пользователей
#                 основы к шлюзам резерва: на awggr-h proxy ARP и
#                 `ip rule iif awggr-h lookup $RF_TABLE pref 150`; маршрут в
#                 awggr-n в зоне ставит сам бот основы (запасной слот),
#                 пересылку только в линки слотов и маскарад — файервол хоста),
#                 клон из stdin (GUEST_IF, GUEST_ADDR cidr, GUEST_DNS «адреса
#                 через пробел», GUEST_UPSTREAMS, GUEST_CHANNEL_PORT) и
#                 клоны GUEST_EXTRA из хранилища (нет конфига или клон не
#                 встал — строка ВНИМАНИЕ, переезд идёт без него); в
#                 state.json — iface/port первого клона и ifaces/ports
#                 всех (через пробел; правила таблицы зоны — на все), dnsmasq
#                 зоны и $RUN/resolv.conf на него (юнит гостя видит его как
#                 /etc/resolv.conf: заглушка хоста 127.0.0.53 в зоне не
#                 отвечает), юнит бота основы с песочницей и пространством журнала;
#                 бот стартует без права отвечать (его даёт бот резерва).
#                 Подсети клонов — на блоки ZONE_POOL (NETMAP в таблице зоны,
#                 маршрут блока через awggv-h у хоста); в state.json —
#                 netmap «подсеть=блок …» (пусто — NETMAP не встал, маскарад;
#                 причина — netmap_why: kernel — nft отверг, pool — в ZONE_POOL
#                 нет свободного блока или подсети не влезают, bad_pool — пул
#                 не разобран), netmap_miss — клоны без блока при частичном
#                 плане (ВНИМАНИЕ; CAKE тогда dual-dsthost)
#                 Без оболочки отказывает («сначала --shell»); GUEST_ADMIN —
#                 адреса устройств админа основы: в state.json (admin), из
#                 него — в набор admin4 при каждой перерисовке таблицы зоны
#   --serve       обслуживание: состояние serving (порт гостя для файервола
#                 хоста — его рисует бот), таблица зоны заново со входом
#                 вебхука с veth, CAKE на внешнем интерфейсе
#                 (triple-isolate nat — при NETMAP на всех клонах, иначе dual-dsthost nat), если
#                 есть модуль sch_cake (иначе cake=absent, дисциплина не
#                 тронута; CAKE, стоявший там до зоны, — с параметрами админа —
#                 не трогается: cake=kept); без клона отказывает («сначала --take»)
#   --restart-clone  клоны (все из ifaces) вниз, setconf из текущих конфигов
#                 хранилища, вверх — только для оператора: бот основы на
#                 резерве перезапускает клон сам изнутри зоны (ip netns exec в
#                 его песочнице недоступен)
#   --relay       возврат: бот основы и dnsmasq остановлены, клоны, veth и
#                 правило iif awggr-h сняты, состояние relay (RELAY_TO=IP:порт
#                 основы — правило пересылки рисует бот, со счётчиком: по нему
#                 он снимает пересылку после тишины; ports остаются —
#                 пересылаются все)
#   --relay-flush записи conntrack по портам клонов (state.json ports) — прочь:
#                 NAT привязан к первому пакету потока, и живые потоки пользователей
#                 встают под DNAT хвоста только после сброса. Бот зовёт ПОСЛЕ
#                 перерисовки файервола хоста с DNAT (иначе поток, пришедший
#                 между --relay и перерисовкой, завёл бы запись без NAT); без
#                 утилиты conntrack — строка ВНИМАНИЕ
#   --teardown    остановить бота основы и dnsmasq зоны, снять клоны, veth и
#                 правило iif awggr-h (без
#                 пересылки) — состояние idle; хранилище, rep.bin и jrn/ остаются
#   --wipe        хранилище гостя, $VAR целиком (rep.bin, журнал данных гостя
#                 jrn/), журнал гостя в journald — прочь; CAKE —
#                 прежняя дисциплина; зона остаётся оболочкой. В фазе relay
#                 фаза и адрес основы остаются (пересылка хвоста живёт до
#                 конца relay), сбрасывается только порт вебхука
#   (`awg-bot reserve teardown` — аварийно --teardown и затем --wipe; так же
#   зону снимают awg-bot uninstall — копией скрипта, сделанной до удаления
#   кода, — переустановка со сносом данных и удаление пары)
#   --status      состояние строками key=value (zone, tg=up|down — veth awggx в зоне,
#                 phase, iface, ifaces, netmap, netmap_why, netmap_miss, store, guest, rep)
# Таблица зоны, отвергнутая nft, — отказ режима (--shell, --take, --serve):
# прежняя таблица остаётся, звать следующий шаг нельзя. Таблицу с NETMAP
# отвергнутой не считают, пока nft принимает её же без NETMAP (маскарад,
# строка ВНИМАНИЕ).
set -u

ZONE="${RSV_ZONE:-awg-guest}"
NS="/run/netns/$ZONE"
RUN="${RSV_RUN:-/run/awg-bot-guest}"
VAR="${RSV_VAR:-/var/lib/awg-bot-guest}"
STATE_DIR="${RSV_STATE_DIR:-/run/awg-bot/rsv}"
STATE="$STATE_DIR/state.json"
LINKS_DIR="${RSV_LINKS_DIR:-/var/lib/awg-bot/reserve/links}"
INSTALL_DIR="${INSTALL_DIR:-/opt/awg-bot}"
ZONE_CIDR="${ZONE_CIDR:-169.254.77.0/30}"
ZONE_RF_CIDR="${ZONE_RF_CIDR:-169.254.77.4/30}"
ZONE_TG_CIDR="${ZONE_TG_CIDR:-169.254.77.8/30}"
TG_TABLE="${TG_TABLE:-153}"
# набор Telegram — тот же, что TG_NETS обвязки шлюза и TG_RANGES агента
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
ZONE_POOL="${ZONE_POOL:-169.254.96.0/19}"
RF_TABLE="${RF_TABLE:-100}"
VH="awggv-h"; VN="awggv-n"; RH="awggr-h"; RN="awggr-n"; XH="awggx-h"; XN="awggx-n"
UNIT="/etc/systemd/system/awg-bot-guest.service"
DNS_UNIT="/etc/systemd/system/awg-bot-guest-dns.service"
JOURNAL_CONF="/etc/systemd/journald@$ZONE.conf"
WEBHOOK_PORTS="443 80 88 8443"
STORE_SIZE="${RSV_STORE_SIZE:-32M}"

say() { printf '%s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
# python для состояния и разбора JSON: интерпретатор бота (venv), запасной — системный
PY="$INSTALL_DIR/venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 2>/dev/null || true)"
need_root() { [ "$(id -u)" -eq 0 ] || die "нужен root"; }
nsx() { ip netns exec "$ZONE" "$@"; }

cidr_host() {   # cidr_host CIDR N — N-й адрес /30 (1 или 2)
    local base="${1%/*}" last; last="${base##*.}"; printf '%s.%s' "${base%.*}" "$(( last + $2 ))"
}
HOST_V="$(cidr_host "$ZONE_CIDR" 1)"; ZONE_V="$(cidr_host "$ZONE_CIDR" 2)"
HOST_R="$(cidr_host "$ZONE_RF_CIDR" 1)"; ZONE_R="$(cidr_host "$ZONE_RF_CIDR" 2)"
HOST_X="$(cidr_host "$ZONE_TG_CIDR" 1)"; ZONE_X="$(cidr_host "$ZONE_TG_CIDR" 2)"

kernel_ge() {
    local k; k="$(uname -r | cut -d- -f1)"
    local maj min; maj="${k%%.*}"; min="${k#*.}"; min="${min%%.*}"
    [ "$maj" -gt "$1" ] 2>/dev/null || { [ "$maj" -eq "$1" ] && [ "$min" -ge "$2" ]; } 2>/dev/null
}
port_free() { if command -v ss >/dev/null 2>&1; then ! ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; else return 0; fi; }
store_kind() { if kernel_ge 6 4; then printf 'tmpfs-noswap'; else printf 'ramfs'; fi; }
has_cake() { modinfo sch_cake >/dev/null 2>&1; }   # modinfo знает и загруженный, и ещё не загруженный модуль

# ── состояние для бота (без секретов) ───────────────────────────────────────
state_write() {   # state_write KEY=VALUE … — слить с прежним; без python — отказ, состояние не затирается
    [ -n "$PY" ] || die "нет python для состояния зоны"
    mkdir -p "$STATE_DIR"; chmod 0755 "$STATE_DIR"
    local tmp; tmp="$(mktemp)"
    "$PY" - "$STATE" "$tmp" "$@" <<'EOF' || { rm -f "$tmp"; die "состояние зоны не записано"; }
import json, sys
path, out, *kv = sys.argv[1:]
try:
    data = json.load(open(path))
except Exception:
    data = {}
for item in kv:
    k, _, v = item.partition("=")
    data[k] = v
json.dump(data, open(out, "w"), ensure_ascii=False)
EOF
    mv "$tmp" "$STATE"; chmod 0644 "$STATE"
}
state_get() { [ -n "$PY" ] && "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$STATE" "$1" 2>/dev/null || true; }

# ── --plan ──────────────────────────────────────────────────────────────────
cmd_plan() {
    say "store=$(store_kind)"
    if has_cake; then say "cake=yes"; else say "cake=no"; fi
    if [ -d /proc/self/ns ] && [ -e /proc/self/ns/net ]; then say "netns=yes"; else say "netns=no"; fi
    if modinfo amneziawg >/dev/null 2>&1 || grep -qw amneziawg /proc/modules 2>/dev/null; then say "awg=yes"; else say "awg=no"; fi
    local p chosen=""
    for p in $WEBHOOK_PORTS; do if port_free "$p"; then chosen="$p"; break; fi; done
    say "webhook_port=${chosen:-none}"
    if [ -e "$NS" ]; then say "zone=present"; else say "zone=absent"; fi
    if [ -x /usr/sbin/dnsmasq ] || command -v dnsmasq >/dev/null 2>&1; then say "dnsmasq=yes"; else say "dnsmasq=no"; fi
    if command -v conntrack >/dev/null 2>&1; then say "conntrack=yes"; else say "conntrack=no"; fi
    if [ -n "$PY" ]; then say "python=yes"; else say "python=no"; fi
}

# ── --selftest ──────────────────────────────────────────────────────────────
ST_ZONE="awgrsvtst-z"; ST_CLI="awgrsvtst-c"; ST_VH="awgrsvtv0"; ST_VC="awgrsvtv1"; ST_IF="awgrsvtst"
ST_PORT="${RSV_SELFTEST_PORT:-45999}"
st_cleanup() { ip netns del "$ST_ZONE" 2>/dev/null; ip netns del "$ST_CLI" 2>/dev/null; ip link del "$ST_VH" 2>/dev/null; ip link del "$ST_IF" 2>/dev/null; }
cmd_selftest() {
    need_root
    command -v awg >/dev/null 2>&1 || die "нет amneziawg-tools (awg)"
    modprobe amneziawg 2>/dev/null || true
    trap st_cleanup EXIT
    st_cleanup
    local sp spub cp cpub ok_ka=0 ok_sock=0 ok_ping=0
    ip netns add "$ST_ZONE" || die "пространство не создалось"
    ip netns add "$ST_CLI" || die "пространство не создалось"
    ip link add "$ST_VH" type veth peer name "$ST_VC" || die "veth не создался"
    ip link set "$ST_VC" netns "$ST_CLI"
    ip addr add 192.0.2.1/30 dev "$ST_VH"; ip link set "$ST_VH" up
    ip netns exec "$ST_CLI" ip addr add 192.0.2.2/30 dev "$ST_VC"
    ip netns exec "$ST_CLI" ip link set "$ST_VC" up
    ip netns exec "$ST_CLI" ip link set lo up
    sp="$(awg genkey)"; spub="$(printf '%s' "$sp" | awg pubkey)"
    cp="$(awg genkey)"; cpub="$(printf '%s' "$cp" | awg pubkey)"
    ip link add "$ST_IF" type amneziawg || die "интерфейс amneziawg не создался"
    printf '%s' "$sp" | awg set "$ST_IF" private-key /dev/stdin listen-port "$ST_PORT" peer "$cpub" allowed-ips 10.255.77.2/32 \
        || die "awg set на интерфейсе хоста не прошёл"
    ip link set "$ST_IF" netns "$ST_ZONE" || die "перенос интерфейса в пространство не прошёл"
    ip netns exec "$ST_ZONE" ip link set "$ST_IF" name awg0
    ip netns exec "$ST_ZONE" ip addr add 10.255.77.1/30 dev awg0
    ip netns exec "$ST_ZONE" ip link set awg0 up
    ip netns exec "$ST_CLI" ip link add awgc type amneziawg
    if printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25-35 2>/dev/null; then ok_ka=1
    else
        printf '%s' "$cp" | ip netns exec "$ST_CLI" awg set awgc private-key /dev/stdin peer "$spub" allowed-ips 10.255.77.0/30 \
            endpoint "192.0.2.1:$ST_PORT" persistent-keepalive 25
    fi
    ip netns exec "$ST_CLI" ip addr add 10.255.77.2/30 dev awgc
    ip netns exec "$ST_CLI" ip link set awgc up
    ip netns exec "$ST_CLI" ping -c1 -W3 10.255.77.1 >/dev/null 2>&1 && ok_ping=1
    if ip netns exec "$ST_ZONE" awg show awg0 latest-handshakes 2>/dev/null | awk '{ if ($2 > 0) found = 1 } END { exit !found }'; then ok_sock=1; fi
    say "RESULT_SOCKET_AT_HOST=$ok_sock"; say "RESULT_KEEPALIVE_RANGE=$ok_ka"; say "RESULT_TUNNEL_PING=$ok_ping"
    [ "$ok_sock" -eq 1 ] && [ "$ok_ka" -eq 1 ]
}

# ── зона-оболочка ───────────────────────────────────────────────────────────
zone_nft() {   # zone_nft "клоны" — таблица зоны; NETMAP отвергнут ядром — та же таблица с маскарадом
    local nm=""; [ -n "${1:-}" ] && nm="$(state_get netmap)"
    zone_nft_load "${1:-}" "$nm" && return 0
    [ -n "$nm" ] || return 1
    zone_nft_load "${1:-}" "" || return 1
    say "ВНИМАНИЕ: nft не принял NETMAP подсетей клонов — устройства основы выходят одним адресом зоны"
    state_write netmap= netmap_why=kernel netmap_miss=
}

link_nets() {   # /30 линков шлюзов основы по Address сторон зоны: с этих адресов агенты идут в Telegram
    local f addr base last
    for f in "$LINKS_DIR"/*.conf; do
        [ -f "$f" ] || continue
        addr="$(awk -F'[ =]+' '/^Address/{print $2; exit}' "$f")"; addr="${addr%/*}"
        case "$addr" in *.*.*.*) ;; *) continue ;; esac
        base="${addr%.*}"; last="${addr##*.}"
        printf '%s.%s/30 ' "$base" "$(( last - last % 4 ))"
    done
}

tg_path_up() {   # постоянный veth awggx и маршруты к набору Telegram по источнику (таблица TG_TABLE)
    if ! ip link show "$XH" >/dev/null 2>&1; then
        ip link add "$XH" type veth peer name "$XN" || { say "ВНИМАНИЕ: veth $XH не создался — выхода агентов в Telegram нет"; return 0; }
        ip link set "$XN" netns "$ZONE"
    fi
    ip addr replace "$HOST_X/30" dev "$XH"; ip link set "$XH" up
    nsx ip addr replace "$ZONE_X/30" dev "$XN"; nsx ip link set "$XN" up
    local n
    for n in $TG_NETS; do nsx ip route replace "$n" via "$HOST_X" dev "$XN" table "$TG_TABLE"; done
    for n in $(link_nets); do
        nsx ip rule list | grep -q "from $n lookup $TG_TABLE" || nsx ip rule add from "$n" lookup "$TG_TABLE"
    done
    state_write tg=up zone_tg_cidr="$ZONE_TG_CIDR"
}

zone_nft_load() {   # таблица зоны: вход — только канал с линков, DNS и ICMP с клона, вебхук с veth; пересылка — см. §3.2
    local chan="${GUEST_CHANNEL_PORT:-8787}" ifaces="${1:-}" iface="${1%% *}" nm="${2:-}" whp pair
    # выход агентов в Telegram: только пока veth стоит и есть линки — источники одним набором
    local tgsrc=""; if nsx ip link show "$XN" >/dev/null 2>&1; then set -- $(link_nets); [ $# -gt 0 ] && tgsrc="{ $(printf '%s, ' "$@" | sed 's/, $//') }"; fi
    # выход агентов в Telegram: только пока veth стоит и есть линки — источники одним набором
    local tgsrc=""; if nsx ip link show "$XN" >/dev/null 2>&1; then set -- $(link_nets); [ $# -gt 0 ] && tgsrc="{ $(printf '%s, ' "$@" | sed 's/, $//') }"; fi
    local paddr pnet pport; paddr="$(state_get probe_addr)"; pnet="$(state_get probe_net)"; pport="$(state_get probe_port)"
    case "$pport" in ''|*[!0-9]*) pport=8789 ;; esac
    # «iface» — первый интерфейс (его правила именные), «ifset» — все клоны одним набором nft
    local ifset=""; [ -n "$ifaces" ] && ifset="{ $(printf '"%s", ' $ifaces | sed 's/, $//') }"
    whp="$(state_get webhook_port 2>/dev/null)"; case "$whp" in ''|*[!0-9]*) whp="" ;; esac
    # таблица рисуется целиком (delete + create): повторные --shell/--take не дописывают правила;
    # отвергнутая таблица — отказ (прежняя остаётся в зоне, звать дальше нельзя)
    nsx nft -f - <<EOF || return 1
table inet awg_guest
delete table inet awg_guest
table inet awg_guest {
    set admin4 { type ipv4_addr; }
    set private4 { type ipv4_addr; flags interval; elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } }
    set tg_out4 { type ipv4_addr; flags interval; elements = { $(printf '%s, ' $TG_NETS | sed 's/, $//') } }
    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        ct state invalid drop
$( [ -n "$iface" ] && [ -n "$paddr" ] && [ -n "$pnet" ] && printf '        iifname %s ip saddr %s ip daddr %s udp dport %s accept\n' "$ifset" "$pnet" "${paddr%/*}" "$pport" )
        ip saddr 169.254.88.0/24 drop
        ip protocol icmp accept
        iifname "awglink*" tcp dport $chan accept
        iifname "awglink*" drop
$( [ -n "$iface" ] && [ -n "$whp" ] && printf '        iifname "%s" ip daddr %s tcp dport %s accept\n' "$VN" "$ZONE_V" "$whp" )
$( [ -n "$iface" ] && printf '        iifname %s udp dport 53 accept\n        iifname %s tcp dport 53 accept\n' "$ifset" "$ifset" )
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        ct state invalid drop
        ip saddr 169.254.88.0/24 drop
$( [ -n "$iface" ] && cat <<EOF2
        iifname $ifset oifname $ifset ip saddr @admin4 accept
        iifname $ifset oifname $ifset drop
        iifname $ifset ip daddr @private4 oifname "$VN" drop
        iifname $ifset oifname { "$VN", "$RN", "awglink*" } accept
EOF2
)
$( [ -n "$iface" ] && printf '        iifname "awglink*" oifname %s ct state established,related accept\n' "$ifset" )
$( [ -n "$tgsrc" ] && printf '        iifname "awglink*" ip saddr %s ip daddr @tg_out4 tcp dport 443 ct state new meter tg_src { ip saddr ct count over 8 } drop\n        iifname "awglink*" oifname "%s" ip saddr %s ip daddr @tg_out4 tcp dport 443 accept\n' "$tgsrc" "$XN" "$tgsrc" )
        iifname "awglink*" drop
    }
    chain zone_post {
        type nat hook postrouting priority srcnat; policy accept;
$( for pair in $nm; do printf '        oifname "%s" ip saddr %s snat ip prefix to ip saddr map { %s : %s }\n' "$VN" "${pair%%=*}" "${pair%%=*}" "${pair#*=}"; done )
        oifname { "$VN", "$RN", "$XN" } masquerade
    }
}
EOF
    # адреса админа основы (устройство ↔ устройство в зоне) — из состояния: таблица
    # рисуется заново при --serve и повторном --shell, набор не должен пустеть
    local a
    for a in $(state_get admin); do nsx nft add element inet awg_guest admin4 "{ $a }" 2>/dev/null || true; done
}

link_sides_up() {   # стороны постоянных линков шлюзов основы из $LINKS_DIR/<linkname>.conf
    local f name tmp addr sc
    for f in "$LINKS_DIR"/*.conf; do
        [ -f "$f" ] || continue
        name="$(basename "$f" .conf)"
        nsx ip link show "$name" >/dev/null 2>&1 && continue
        tmp="awgz$$"
        ip link add "$tmp" type amneziawg || { say "ВНИМАНИЕ: сторона линка $name не создалась"; continue; }
        # awg setconf не знает расширений awg-quick (Address и др.) — снять их; срез
        # рядом с конфигом (тот же закрытый каталог), живёт только на время setconf
        sc="$LINKS_DIR/.$name.setconf"
        grep -v -E '^[[:space:]]*(Address|MTU|DNS|Table)[[:space:]]*=' "$f" > "$sc"; chmod 0600 "$sc"
        awg setconf "$tmp" "$sc" \
            || { rm -f "$sc"; ip link del "$tmp"; say "ВНИМАНИЕ: конфиг стороны линка $name не принят"; continue; }
        rm -f "$sc"
        ip link set "$tmp" netns "$ZONE"
        nsx ip link set "$tmp" name "$name"
        addr="$(awk -F'[ =]+' '/^Address/{print $2; exit}' "$f")"
        [ -n "$addr" ] && nsx ip addr add "$addr" dev "$name" 2>/dev/null
        nsx ip link set "$name" up
    done
}

cmd_shell() {
    need_root
    [ -e "$NS" ] || ip netns add "$ZONE" || die "пространство $ZONE не создалось"
    nsx ip link set lo up
    nsx sysctl -qw net.ipv4.ip_forward=1 2>/dev/null || true
    mkdir -p "$LINKS_DIR"; chmod 0700 "$LINKS_DIR"
    link_sides_up
    tg_path_up
    zone_nft "$(state_ifaces)" || die "nft отверг таблицу зоны"   # после сторон линков: их /30 — источники пути к Telegram
    state_write zone=present
    say "shell: ok"
}

# ── хранилище гостя в памяти ────────────────────────────────────────────────
store_mount() {
    mkdir -p "$RUN"; chmod 0700 "$RUN"
    mountpoint -q "$RUN" && return 0
    if [ "$(store_kind)" = "tmpfs-noswap" ]; then
        mount -t tmpfs -o "size=$STORE_SIZE,mode=0700,noswap" tmpfs "$RUN" 2>/dev/null \
            || mount -t ramfs -o mode=0700 ramfs "$RUN" || die "хранилище гостя не смонтировалось"
    else
        mount -t ramfs -o mode=0700 ramfs "$RUN" || die "хранилище гостя (ramfs) не смонтировалось"
    fi
    mkdir -p "$RUN/data" "$RUN/conf" "$RUN/awg"; chmod 0700 "$RUN/data" "$RUN/conf" "$RUN/awg"
}

# ── юниты гостя ─────────────────────────────────────────────────────────────
write_journal_conf() {
    cat > "$JOURNAL_CONF" <<EOF
# awg-bot: журнал бота основы на резерве — только в памяти
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF
}

write_units() {   # write_units GUEST_IF
    cat > "$DNS_UNIT" <<EOF
[Unit]
Description=awg-bot: резолвер гостевой зоны резерва
After=network-online.target

[Service]
Type=simple
NetworkNamespacePath=$NS
ExecStart=/usr/sbin/dnsmasq -k --conf-file=$RUN/dnsmasq.conf
Restart=on-failure
RestartSec=3
EOF
    cat > "$UNIT" <<EOF
[Unit]
Description=awg-bot: бот основы на резерве (гостевой режим)
After=network-online.target awg-bot-guest-dns.service
Wants=awg-bot-guest-dns.service

[Service]
Type=simple
NetworkNamespacePath=$NS
EnvironmentFile=$RUN/env
BindReadOnlyPaths=$RUN/resolv.conf:/etc/resolv.conf
Environment=AWG_BOT_CONF_DIR=$RUN/conf
Environment=AWG_BOT_DATA_DIR=$RUN/data
Environment=AWG_BOT_ENV=$RUN/env
Environment=AWG_BOT_GUEST=1
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m awgbot
Restart=on-failure
RestartSec=5
TemporaryFileSystem=/etc/awg-bot:ro /var/lib/awg-bot:ro /root:ro /home:ro
InaccessiblePaths=-/etc/amnezia -/etc/awg-gw -/var/backups
ReadWritePaths=$RUN $VAR
BindPaths=-$STATE_DIR/guest.sock
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
LogNamespace=$ZONE
MemorySwapMax=0
MemoryHigh=128M
MemoryMax=160M
LimitCORE=0

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
}

write_dnsmasq() {   # write_dnsmasq "адреса" "апстримы"
    # бот основы в зоне резолвит через резолвер зоны, а не через /etc/resolv.conf
    # хоста (stub 127.0.0.53 в зоне — её собственный lo, там никого нет)
    printf 'nameserver %s\n' "${1%% *}" > "$RUN/resolv.conf"
    {
        say "# awg-bot: резолвер гостевой зоны резерва"
        say "no-resolv"; say "no-hosts"; say "bind-interfaces"; say "cache-size=1000"
        local a
        for a in $1; do say "listen-address=$a"; done
        for a in $2; do say "server=$a"; done
        say "conf-file=$RUN/dnsmasq-rf.conf"
    } > "$RUN/dnsmasq.conf"
    [ -f "$RUN/dnsmasq-rf.conf" ] || : > "$RUN/dnsmasq-rf.conf"
}

state_ifaces() {    # все клоны из состояния; старое состояние знало один
    local i; i="$(state_get ifaces)"; [ -n "$i" ] || i="$(state_get iface)"; printf '%s' "$i"
}

netmap_busy() {   # занятое у хоста: подсети veth зоны и маршруты main, кроме default и уже стоящих блоков на awggv-h
    printf '%s %s ' "$ZONE_CIDR" "$ZONE_RF_CIDR"
    # у маршрутов с типом (blackhole, unreachable, prohibit, throw) первое поле — тип, префикс — второе
    ip -4 route show 2>/dev/null \
        | awk -v vh="$VH" '$1 != "default" && $0 !~ (" dev " vh "( |$)") { printf "%s ", ($1 ~ /^[0-9]/) ? $1 : $2 }'
}

netmap_pool_ok() {   # пул ZONE_POOL — сеть IPv4; иначе NETMAP не ставится (netmap_why=bad_pool), а не падает молча
    "$PY" -c 'import ipaddress, sys; sys.exit(0 if ipaddress.ip_network(sys.argv[1], strict=False).version == 4 else 1)' \
        "$ZONE_POOL" 2>/dev/null
}

netmap_miss() {   # netmap_miss "план" имя=cidr… — клоны, чьей подсети в плане нет (частичный план)
    "$PY" - "$@" <<'PYEOF' 2>/dev/null || true
import ipaddress, sys
have = {p.split("=", 1)[0] for p in sys.argv[1].split()}
out = []
for item in sys.argv[2:]:
    name, _, cidr = item.partition("=")
    try:
        net = str(ipaddress.ip_interface(cidr).network)
    except ValueError:
        net = ""
    if net not in have:
        out.append(name)
print(" ".join(out))
PYEOF
}

netmap_plan() {   # netmap_plan "занятое" CIDR… — «подсеть=блок …»: подсети каждого клона — свой свободный блок ZONE_POOL той же длины
    # пул разобран раньше (netmap_pool_ok): здесь глушится только трассировка самого плана
    "$PY" - "$ZONE_POOL" "$@" <<'PYEOF' 2>/dev/null || true
import ipaddress, sys
pool, out = ipaddress.ip_network(sys.argv[1], strict=False), []
# маршрут хоста внутри блока ip route replace перекрыл бы молча; маршрут шире блока
# проигрывает ему по длине префикса и не мешает (link-local /16 у avahi и NetworkManager)
busy = []
for b in sys.argv[2].split():
    try:
        busy.append(ipaddress.ip_network(b, strict=False))
    except ValueError:
        continue
busy = [r for r in busy if r.version == 4]
for c in sys.argv[3:]:
    try:
        net = ipaddress.ip_interface(c).network
    except ValueError:
        continue                                   # битый адрес — только этот клон без NETMAP
    if net.version != 4 or net.prefixlen < pool.prefixlen or any(net.overlaps(n) for n, _ in out):
        continue                                   # не влезает в пул или повтор — этот клон под маскарадом
    blk = next((b for b in pool.subnets(new_prefix=net.prefixlen)
                if not any(r.subnet_of(b) for r in busy if r.overlaps(b)) and not any(b.overlaps(t) for _, t in out)), None)
    if blk is not None:
        out.append((net, blk))
print(" ".join(f"{n}={b}" for n, b in out))
PYEOF
}

write_setconf() {   # write_setconf IFACE — конфиг для awg setconf: без расширений awg-quick (Address, MTU, DNS, Table)
    grep -v -E '^[[:space:]]*(Address|MTU|DNS|Table)[[:space:]]*=' "$RUN/awg/$1.conf" > "$RUN/awg/$1.setconf"
    chmod 0600 "$RUN/awg/$1.setconf"
}

# ── --take ──────────────────────────────────────────────────────────────────
cmd_take() {
    need_root
    [ -e "$NS" ] || die "зоны-оболочки нет — сначала --shell"
    local iface="${GUEST_IF:-awg0}" addr="${GUEST_ADDR:-}" dns="${GUEST_DNS:-}" ups="${GUEST_UPSTREAMS:-}"
    [ -n "$addr" ] || die "нет GUEST_ADDR"
    store_mount
    # конфиг клона — только из stdin в хранилище в памяти; конфиги дополнительных
    # интерфейсов (окно переезда профилей — второй интерфейс основы) процесс
    # открытия кладёт в $RUN/awg/<имя>.conf сам (GUEST_EXTRA="имя=cidr …")
    cat > "$RUN/awg/$iface.conf"; chmod 0600 "$RUN/awg/$iface.conf"
    grep -q '^PrivateKey' "$RUN/awg/$iface.conf" || die "в конфиге клона нет ключа"
    # для awg setconf — без расширений awg-quick (Address, MTU, DNS, Table): их setconf не знает
    write_setconf "$iface"
    # veth: бот основы и пользователи наружу (NAT хоста) и РФ-трафик к шлюзам резерва
    ip link show "$VH" >/dev/null 2>&1 || { ip link add "$VH" type veth peer name "$VN"; ip link set "$VN" netns "$ZONE"; }
    ip link show "$RH" >/dev/null 2>&1 || { ip link add "$RH" type veth peer name "$RN"; ip link set "$RN" netns "$ZONE"; }
    ip addr replace "$HOST_V/30" dev "$VH"; ip link set "$VH" up
    ip addr replace "$HOST_R/30" dev "$RH"; ip link set "$RH" up
    nsx ip addr replace "$ZONE_V/30" dev "$VN"; nsx ip link set "$VN" up
    nsx ip addr replace "$ZONE_R/30" dev "$RN"; nsx ip link set "$RN" up
    nsx ip route replace default via "$HOST_V" dev "$VN"
    # РФ-трафик из зоны: запасной слот бота основы — `default dev awggr-n`; хост
    # отвечает на ARP за любой адрес (proxy ARP) и ведёт пакеты в таблицу своего
    # РФ-доступа (линк активного слота), маскарад — в таблице хоста
    sysctl -qw "net.ipv4.conf.$RH.proxy_arp=1" 2>/dev/null || true
    ip rule show | grep -q "iif $RH lookup $RF_TABLE" || ip rule add iif "$RH" lookup "$RF_TABLE" pref 150
    # клоны: создаются у хоста (сокет — у хоста), переносятся в зону под именами основы;
    # первый — основной интерфейс AWG, остальные — интерфейсы окна переезда профилей
    local ifaces="$iface" ports="" pair extra_if extra_addr cidrs="$addr" named="$iface=$addr"
    clone_up "$iface" "$addr" || die "клон $iface не поднялся"
    for pair in ${GUEST_EXTRA:-}; do
        extra_if="${pair%%=*}"; extra_addr="${pair#*=}"
        [ -n "$extra_if" ] && [ -n "$extra_addr" ] && [ "$extra_if" != "$extra_addr" ] || continue
        [ -f "$RUN/awg/$extra_if.conf" ] || { say "ВНИМАНИЕ: конфига $extra_if нет — интерфейс не поднят"; continue; }
        write_setconf "$extra_if"
        clone_up "$extra_if" "$extra_addr" || { say "ВНИМАНИЕ: клон $extra_if не поднялся"; continue; }
        ifaces="$ifaces $extra_if"; cidrs="$cidrs $extra_addr"; named="$named $extra_if=$extra_addr"
    done
    local i; for i in $ifaces; do ports="$ports $(awk '/^ListenPort/{print $3; exit}' "$RUN/awg/$i.conf")"; done
    ports="${ports# }"
    # план NETMAP — до таблицы зоны: она берёт его из состояния (и при каждой перерисовке).
    # Пустой план — причина в netmap_why (bad_pool — пул не разобран, pool — свободного блока
    # нет или подсети не влезают; kernel ставит zone_nft), клоны без блока — netmap_miss
    local nm="" why="" miss=""
    if netmap_pool_ok; then
        nm="$(netmap_plan "$(netmap_busy)" $cidrs)"
        [ -n "$nm" ] || why=pool
    else
        say "ВНИМАНИЕ: пул $ZONE_POOL (reserve.zone_pool) не разобран — подсети клонов идут маскарадом"
        why=bad_pool
    fi
    miss="$(netmap_miss "$nm" $named)"
    [ -z "$nm" ] || [ -z "$miss" ] || say "ВНИМАНИЕ: NETMAP не на все клоны — без блока: $miss (подсеть не влезает в пул или свободного блока нет)"
    state_write admin="${GUEST_ADMIN:-}" netmap="$nm" netmap_why="$why" netmap_miss="$miss" \
        probe_addr="${GUEST_PROBE_ADDR:-}" probe_net="${GUEST_PROBE_NET:-}" probe_port="${GUEST_PROBE_PORT:-8789}"
    # ответчик кворума бота основы — второй адрес клона (тем же /26, что у основы: маршрут на подблок)
    [ -n "${GUEST_PROBE_ADDR:-}" ] && { nsx ip addr replace "${GUEST_PROBE_ADDR:-}" dev "$iface" || say "ВНИМАНИЕ: второй адрес клона ${GUEST_PROBE_ADDR:-} не встал"; }
    zone_nft "$ifaces" || die "nft отверг таблицу зоны"
    # ответы на блоки пула — через veth в зону (иначе хост увёл бы их маршрутом по умолчанию)
    for pair in $(state_get netmap); do ip route replace "${pair#*=}" via "$ZONE_V" dev "$VH"; done
    [ -n "$dns" ] || dns="${addr%/*}"
    for a in $dns; do [ "$a" = "${addr%/*}" ] || nsx ip addr replace "$a/32" dev lo; done
    write_dnsmasq "$dns" "$ups"
    write_journal_conf
    write_units "$iface"
    mkdir -p "$VAR"; chmod 0700 "$VAR"
    mkdir -p "$STATE_DIR"
    systemctl restart awg-bot-guest-dns.service || say "ВНИМАНИЕ: dnsmasq зоны не поднялся"
    [ -f "$RUN/env" ] && systemctl restart awg-bot-guest.service || say "ВНИМАНИЕ: env бота основы ещё не положен — юнит не запущен"
    state_write zone=present phase=taking iface="$iface" port="${ports%% *}" ifaces="$ifaces" ports="$ports" \
        zone_cidr="$ZONE_CIDR" zone_rf_cidr="$ZONE_RF_CIDR" taken_at="$(date -Iseconds)"
    say "take: ok"
}

clone_up() {   # clone_up IFACE CIDR — клон из $RUN/awg/IFACE.setconf: создать у хоста, перенести в зону, адрес, MTU
    local iface="$1" addr="$2" tmp="awgc$$" mtu
    nsx ip link show "$iface" >/dev/null 2>&1 && { nsx ip link del "$iface" 2>/dev/null || true; }
    ip link add "$tmp" type amneziawg || return 1
    awg setconf "$tmp" "$RUN/awg/$iface.setconf" || { ip link del "$tmp"; return 1; }
    ip link set "$tmp" netns "$ZONE" || { ip link del "$tmp"; return 1; }
    nsx ip link set "$tmp" name "$iface"
    nsx ip addr replace "$addr" dev "$iface"
    mtu="$(awk -F'=' '/^[[:space:]]*MTU[[:space:]]*=/{gsub(/ /,"",$2); print $2; exit}' "$RUN/awg/$iface.conf")"
    [ -n "$mtu" ] && nsx ip link set "$iface" mtu "$mtu"
    nsx ip link set "$iface" up
}

cmd_restart_clone() {
    need_root
    local ifaces; ifaces="$(state_ifaces)"; [ -n "$ifaces" ] || die "клона нет"
    local iface; for iface in $ifaces; do
        nsx ip link set "$iface" down
        write_setconf "$iface"
        nsx awg setconf "$iface" "$RUN/awg/$iface.setconf" || die "конфиг клона $iface не принят"
        nsx ip link set "$iface" up
    done
    say "clone: restarted"
}

# ── --serve ─────────────────────────────────────────────────────────────────
wan_if() { ip -j route show default 2>/dev/null | "$PY" -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["dev"] if r else "")' 2>/dev/null || true; }
cake_on() {
    has_cake || { state_write cake=absent; return 0; }
    local wan; wan="$(wan_if)"; [ -n "$wan" ] || return 0
    local prev; prev="$(tc qdisc show dev "$wan" 2>/dev/null | head -n1 | awk '{print $2}')"
    # повторный --serve: cake уже стоит — прежняя дисциплина записана при первом, не затирать
    if [ "$prev" = "cake" ] && [ "$(state_get cake)" != "on" ]; then
        state_write cake=kept cake_dev= cake_prev=; return 0   # CAKE админа с его параметрами — не трогать
    fi
    [ "$prev" = "cake" ] && [ "$(state_get cake)" = "on" ] && prev="$(state_get cake_prev)"
    # с NETMAP хост видит каждое устройство основы своим адресом — делить и по источнику, и по
    # назначению; без него вся зона — один источник против каждого своего пользователя: только по назначению
    # частичный план (клон без блока — netmap_miss) — его устройства для хоста один адрес: только по назначению
    local iso="dual-dsthost"; [ -n "$(state_get netmap)" ] && [ -z "$(state_get netmap_miss)" ] && iso="triple-isolate"
    tc qdisc replace dev "$wan" root cake besteffort "$iso" nat 2>/dev/null && state_write cake=on cake_dev="$wan" cake_prev="${prev:-none}"
}
cake_off() {
    local dev prev; dev="$(state_get cake_dev)"; prev="$(state_get cake_prev)"
    [ "$(state_get cake)" = "kept" ] && { state_write cake=off; return 0; }
    [ -n "$dev" ] || return 0
    if [ -n "$prev" ] && [ "$prev" != "none" ] && [ "$prev" != "cake" ]; then tc qdisc replace dev "$dev" root "$prev" 2>/dev/null || tc qdisc del dev "$dev" root 2>/dev/null
    else tc qdisc del dev "$dev" root 2>/dev/null; fi
    state_write cake=off cake_dev= cake_prev=
}
cmd_serve() {
    need_root
    [ -n "$(state_get iface)" ] || die "клона нет — сначала --take"
    cake_on
    state_write phase=serving serving_at="$(date -Iseconds)" webhook_port="${WEBHOOK_PORT:-}"
    zone_nft "$(state_ifaces)" || die "nft отверг таблицу зоны"   # вход вебхука с veth — только теперь, когда порт известен
    say "serve: ok"
}

# ── снятие ──────────────────────────────────────────────────────────────────
guest_stop() {
    systemctl stop awg-bot-guest.service 2>/dev/null || true
    systemctl stop awg-bot-guest-dns.service 2>/dev/null || true
    local ifaces; ifaces="$(state_ifaces)"
    local iface; for iface in $ifaces; do nsx ip link del "$iface" 2>/dev/null; done
    while ip rule del iif "$RH" lookup "$RF_TABLE" 2>/dev/null; do :; done
    ip link del "$VH" 2>/dev/null; ip link del "$RH" 2>/dev/null
    zone_nft ""
}
conntrack_flush_ports() {   # записи conntrack по портам клонов: NAT привязан к первому пакету потока —
    local p                  # живые потоки пользователей иначе не попадут под DNAT хвоста
    command -v conntrack >/dev/null 2>&1 || { say "ВНИМАНИЕ: нет conntrack — потоки пользователей, идущие сейчас, пересылки не получат"; return 0; }
    for p in $(state_get ports); do conntrack -D -p udp --orig-port-dst "$p" >/dev/null 2>&1 || true; done
}
cmd_relay() {
    need_root
    guest_stop
    state_write phase=relay relay_to="${RELAY_TO:-}" relay_at="$(date -Iseconds)" iface= ifaces= netmap= netmap_why= netmap_miss=
    say "relay: ok"
}
cmd_teardown() {
    need_root
    guest_stop
    state_write phase=idle iface= ifaces= ports= port= relay_to= webhook_port= netmap= netmap_why= netmap_miss= probe_addr= probe_net=
    say "teardown: ok"
}
cmd_wipe() {
    need_root
    guest_stop
    if mountpoint -q "$RUN"; then umount -l "$RUN" 2>/dev/null || true; fi
    rm -rf "$RUN" "$VAR"
    rm -rf /run/log/journal/*."$ZONE" /var/log/journal/*."$ZONE" 2>/dev/null
    rm -f "$UNIT" "$DNS_UNIT"; systemctl daemon-reload 2>/dev/null || true
    cake_off
    if [ "$(state_get phase)" = "relay" ]; then
        # пересылка хвоста идёт на хосте по state.json: фаза и адрес основы остаются до конца relay
        state_write webhook_port= wiped_at="$(date -Iseconds)"
    else
        state_write phase=idle iface= ifaces= ports= port= relay_to= webhook_port= netmap= netmap_why= netmap_miss= wiped_at="$(date -Iseconds)"
    fi
    say "wipe: ok"
}
cmd_status() {
    if [ -e "$NS" ]; then say "zone=present"; else say "zone=absent"; fi
    if [ -e "$NS" ] && nsx ip link show "$XN" >/dev/null 2>&1; then say "tg=up"; else say "tg=down"; fi
    say "phase=$(state_get phase)"; say "iface=$(state_get iface)"; say "ifaces=$(state_ifaces)"; say "netmap=$(state_get netmap)"
    say "netmap_why=$(state_get netmap_why)"; say "netmap_miss=$(state_get netmap_miss)"
    if mountpoint -q "$RUN" 2>/dev/null; then say "store=mounted"; else say "store=absent"; fi
    systemctl is-active awg-bot-guest.service >/dev/null 2>&1 && say "guest=active" || say "guest=inactive"
    [ -f "$VAR/rep.bin" ] && say "rep=present" || say "rep=absent"
}

case "${1:-}" in
    --plan) cmd_plan ;;
    --selftest) cmd_selftest ;;
    --shell) cmd_shell ;;
    --store) need_root; store_mount; say "store: $(store_kind)" ;;
    --take) cmd_take ;;
    --serve) cmd_serve ;;
    --restart-clone) cmd_restart_clone ;;
    --relay) cmd_relay ;;
    --relay-flush) need_root; conntrack_flush_ports; say "relay-flush: ok" ;;   # после отрисовки DNAT хвоста ботом
    --teardown) cmd_teardown ;;
    --wipe) cmd_wipe ;;
    --status) cmd_status ;;
    *) say "использование: $0 --plan|--selftest|--shell|--store|--take|--serve|--restart-clone|--relay|--relay-flush|--teardown|--wipe|--status"; exit 2 ;;
esac
