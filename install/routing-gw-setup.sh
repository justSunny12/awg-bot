#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-gw-setup.sh — сторона ШЛЮЗА (малинки) для условной маршрутизации.
# Запускается НА МАЛИНКЕ. См. концепт «условная маршрутизация», §12.
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
#      сервисы увидят российский адрес шлюза); изоляцию клиентов от локальной
#      сети (нужны НОВЫЕ соединения из туннеля, а это открывает путь к NAS,
#      роутеру, торрент-клиенту — закрываем); метки Telegram для самого агента; и защиту
#      САМОЙ МАШИНЫ от туннеля: с адресов туннеля на неё пускается только ВПС
#      по линку (SSH, ICMP), всё прочее дропается. Исключение — УСТРОЙСТВА
#      АДМИНА (ADMIN_IPS, приезжают в бандле): им открыто всё, и машина, и
#      локальная сеть через неё;
#   3) снимает прежнюю обвязку в iptables (AWGLINK_FWD, MASQUERADE, метки);
#      при политике DROP в чужой ip filter FORWARD (docker) вставляет в неё
#      ACCEPT своим интерфейсам (линк, аплинк, локальная сеть) — п.3a; по ним
#      агент и судит, что красной проверки «политика FORWARD» нет;
#   4) автозапуск: юнит зовёт этот же скрипт, таблица ставится ДО подъёма линка;
#      юнит переписывается целиком на каждом запуске и ставится с правами 0600;
#   5) при LAN_MODE=1 — «за шлюзом без VPN» (концепт «локальная сеть»,
#      функция A): dnsmasq на адресе шлюза в квартире (bind-dynamic, апстрим
#      через АПЛИНК, NXDOMAIN на DoH-эндпоинты), ВТОРАЯ таблица inet awg_home
#      (наборы lan_vpn4/lan_vpn_nets4/lan_ru4, метка в туннель, маскарад в
#      локальную сеть) и скрипты списков в /usr/local/sbin — awg-lan-lists.sh
#      (фиды; исходник до вычитания исключений — /var/lib/awg-gw/vpn-feed.src),
#      awg-lan-domain.sh (свои списки: add|ru|del|list — их же зовёт awg-bot lan;
#      sync <файл> — полный список, его зовёт только агент; одна блокировка
#      lists.lock с фидами, исключения «напрямую» вычитаются из фида заново,
#      наборы чистятся) и
#      awg-lan-services.sh (записи сервисов соседних сетей для dnsmasq —
#      концепт «сервисы соседних сетей»: SMB-серверы сети другого шлюза видны
#      в Finder через домен обзора awg.internal; при PEER_HOME_NETS,
#      LINK_CHANNEL=1 и живом avahi-daemon ставится avahi-utils для обзора
#      своей сети — после apt-get update; PEER_HOME_NETS пуст — файл
#      awg-gw-peer-services.conf снимается).
#      awg-lan-services.sh <файл> ставит записи, собранные агентом из данных
#      канала: каждая строка — по белому списку шаблонов (LINE_RES в
#      awgbot/domain/gwservices.py), dnsmasq --test, рестарт с откатом, та же
#      блокировка lists.lock; без аргумента — снять; rc=2 — строка вне списка.
#      awg-lan-lists.sh с AWG_LAN_FROM=<каталог> фиды не качает, а берёт
#      готовыми (domains.lst, nets.lst) — их привозит канал линка в
#      /var/lib/awg-gw/feed; в lists.status тогда source=channel, иначе net.
#      Скрипты awg-lan-*.sh пишутся во временный файл и подменяются mv:
#      запущенный экземпляр дочитывает свой прежний inode. Копии для отката
#      конфигов dnsmasq все три держат в /var/lib/awg-gw/rollback, НЕ в
#      conf-dir: Debian читает оттуда всё, кроме .dpkg-*, и копия грузилась бы
#      вместе с новым файлом.
#      Таблица пишется БЕЗ delete table: наборы наполняет dnsmasq на лету, и
#      реассерт обязан их сохранить — пересобираются только цепочки. Ручной
#      слой прежней схемы переезжает в /var/lib/awg-gw/migrated — туда же
#      хвосты conf-dir *.bak, *.tmp и *.prev.awg прежних версий. Личные
#      списки при снятии уходят в /var/lib/awg-gw/restore (туда же их
#      откладывает awg-bot restore, пока режим не применён), и следующее
#      включение берёт их оттуда; нет — пустые заготовки. Занят :53
#      не-dnsmasq — честный отказ ДО
#      apt, причина в статусе; остальная обвязка при этом стоит. Юниту
#      dnsmasq — оверрайд dnsmasq.service.d/awg-gw.conf: After/Wants
#      awg-quick@<аплинк> (апстрим привязан к интерфейсу), Restart=on-failure,
#      выключенный хук resolvconf Debian (если он в юните). apt — с
#      DPkg::Lock::Timeout=120 (OMV может держать dpkg) и force-confdef/confold
#      (терминала у юнита нет). awg-lan-lists.sh — один запуск за раз: ждёт
#      блокировку до 120 с, не дождался — код 75 «занято» (агент ждёт скрипты
#      списков и записей SMB 150 с — LAN_SCRIPT_TIMEOUT в gwguard.py).
#
# ЧЕГО НЕ ДЕЛАЕТ: не трогает существующие интерфейсы, прежнюю ручную схему
# маршрутизации и чужие правила iptables (docker и т.п.) — они работают как
# работали (исключение — ACCEPT из п.3a). Политика INPUT для локальной сети
# остаётся accept. avahi-daemon не ставит (он начал бы объявлять малину) —
# только avahi-utils рядом с уже запущенным демоном и только при канале линка.
# /etc/default/dnsmasq не трогает: прежняя строка
# DNSMASQ_EXCEPT=lo глушила 127.0.0.1 (except-interface=lo), а файл, созданный
# до пакета, ронял dpkg; свою строку прежних выпусков — убирает.
#
# ОКРУЖЕНИЕ. Первую группу вшивает в себя бандл и закрепляет юнит awg-link-gw
# строками Environment= — значения приезжают с ВПС и меняются перевыпуском
# конфигурации шлюза. Четыре из них (ADMIN_IPS, HOME_SUBNETS, LAN_MODE,
# RESOLVER — gwlink.SETTINGS_KEYS) при живом канале доставляет
# сервер: агент переписывает эти строки прямо в юните и перезапускает его
# (концепт «канал линка», этап 2). Отдельного файла для них нет намеренно —
# юнит единственный источник, и следующее применение бандла перепишет его
# целиком:
#   CLIENT_SUBNET   подсеть клиентов ВПС (MASQUERADE, изоляция)
#   LINK_IF         интерфейс линка (awglink, у второго слота awglink2)
#   ADMIN_IPS       устройства админа — полный доступ с туннеля
#   GATEWAY_PUBKEY / GATEWAY_PREV_PUBKEY
#                   ключ аплинка помеченного шлюза (и прежний ключ пары в окне
#                   переезда): по нему находится аплинк и проверяется, та ли
#                   это машина — с чужим аплинком линк не поднимается
#   UPLINK_B64      конфиг аплинка из бандла (base64) — ТОЛЬКО на время
#                   применения: в юнит не закрепляется (внутри приватный
#                   ключ, а поставленный конфиг и так лежит в HOST_CONF_DIR с
#                   0600). Реассерт из юнита аплинк не переставляет
#   LAN_MODE        1 — поднять «за шлюзом без VPN» (см. п.5), 0 — снять своё
#   HOME_SUBNETS    локальные подсети шлюза: по первой он находит свой
#                   интерфейс и адрес в квартире
#   RESOLVER        адрес резолвера ВПС для апстрима; пусто — запасной 1.1.1.1
#   PEER_HOME_NETS  подсети за другими шлюзами: им из линка открыт транзит.
#                   Только бандлом (gwlink.BUNDLE_ONLY_KEYS): те же подсети
#                   стоят в AllowedIPs конфига линка, канал их не везёт.
#                   Непуст — действуют и сервисы соседних сетей (п.5)
#   LINK_CHANNEL    1 — агент держит канал состояния до ВПС внутри линка
#                   (концепт «канал линка»); 0 или нет строки — не держит.
#                   Сам скрипт канала не касается: он только закрепляет обе
#                   строки в юните, читает их агент
#   LINK_CHANNEL_PORT  порт канала на адресе ВПС в /30 линка (8787)
# В юните их нет — дефолт в скрипте, переопределяются окружением при ручном
# запуске: TG_MARK (метка трафика в туннель, 0x1), UPLINK_TABLE (таблица
# политики «метка → аплинк», 100), UPLINK_IF (имя аплинка на чистой машине,
# пока его не найти по ключу, awg0), WAN_IF, HOST_CONF_DIR, CONTAINER.
# Вторую группу юнит читает строкой EnvironmentFile=-/etc/awg-gw/firewall.env —
# это локальное состояние шлюза, его пишут агент и awg-bot (разделы файервола и
# доступа по SSH), перевыпуск бандла его не трогает: ADMIN_IPS_EXTRA
# (доверенные из туннеля сверх бандла), SSH_PORT (факт: порт, который слушает
# sshd; 22), SSH_FILTER (1 — фильтр снаружи включён), SSH_ALLOW (адреса
# снаружи: IP/CIDR/имена), SSH_ALLOW_RESOLVED (последний резолв имён — агент).
# Фильтр снаружи (цепочка ssh_in) касается только порта SSH и только
# не-туннельных источников: локальная сеть, сервер и подсети других шлюзов
# (peer_nets4 — они приходят линком, но адрес у них не из tunnel_nets4)
# открыты всегда.
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
NETWORKD_UNMANAGED="/etc/systemd/network/99-awg-gw-unmanaged.network"   # networkd не трогает awg*
GW_ETC="/etc/awg-gw"
GUARD_FILE="$GW_ETC/guard.nft"           # таблица — источник для nft -f при каждом старте
FW_ENV="$GW_ETC/firewall.env"            # ADMIN_IPS_EXTRA, правится на шлюзе (awg-bot firewall)
GUARD_TABLE="inet awg_gw_guard"
SSH_PORT="${SSH_PORT:-22}"
TG_MARK="${TG_MARK:-0x1}"
# Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами; тот же
# список знает агент (domain/gateway.py TG_RANGES) и сверяет с таблицей.
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
# GitHub — той же меткой в аплинк: агент обновляется с releases, а в юрисдикции
# шлюза GitHub без туннеля недоступен. Четыре сети из api.github.com/meta
# (web/api/git), в них github.com, api.github.com и *.githubusercontent.com
# (release-assets, objects, raw); региональные /32 Azure оттуда же — не для
# нас. Тот же список знает агент (domain/gateway.py GH_RANGES).
GH_NETS="140.82.112.0/20 143.55.64.0/20 185.199.108.0/22 192.30.252.0/22"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"
# Устройства админа (из бандла с ВПС) — им с туннеля открыто ВСЁ: сама машина и
# локальная сеть через неё. Прочим клиентам — изоляция и drop. Старое имя
# переменной (SSH_ALLOW) принимается для бандлов прежнего выпуска.
# Без двоеточия: запасное имя — только когда ADMIN_IPS НЕ ЗАДАН (старый
# бандл); юнит всегда задаёт его, пусть и пустым, а SSH_ALLOW из firewall.env
# к этому моменту уже в окружении (EnvironmentFile) — это список снаружи.
ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"
ADMIN_IPS_EXTRA="${ADMIN_IPS_EXTRA:-}"
# Старое имя SSH_ALLOW отработало выше как ADMIN_IPS; дальше SSH_ALLOW — список
# адресов СНАРУЖИ из firewall.env, и бандловое значение ему не должно достаться.
SSH_ALLOW=""; SSH_ALLOW_RESOLVED=""; SSH_FILTER=""
[ -f "$FW_ENV" ] && . "$FW_ENV"
case "$SSH_PORT" in ''|*[!0-9]*) SSH_PORT=22 ;; esac
[ "$SSH_FILTER" = "1" ] || SSH_FILTER=0
# Шлюзовое устройство (пометка в основном боте): ключ аплинка помеченного
# шлюза, старый ключ в окне переезда, конфиг аплинка в base64. Пусто — шлюз
# в основном боте не помечен.
GATEWAY_PUBKEY="${GATEWAY_PUBKEY:-}"
GATEWAY_PREV_PUBKEY="${GATEWAY_PREV_PUBKEY:-}"
UPLINK_B64="${UPLINK_B64:-}"
UPLINK_TABLE="${UPLINK_TABLE:-100}"          # таблица политики «метка → аплинк»
UPLINK_IF_DEFAULT="${UPLINK_IF:-awg0}"        # имя аплинка на чистой машине
GW_FOREIGN=0                                  # 1 = слот помечен другому устройству, линк не поднимаем
GW_UNCONFIRMED=0                              # 1 = аплинка не видно, шлюз не подтверждён — линк не трогаем
GW_STATUS_FILE="$GW_ETC/gateway.status"       # что решил скрипт — читает агент
# локальная сеть без VPN (концепт «локальная сеть»): вторая таблица nft, dnsmasq, списки
HOME_TABLE="inet awg_home"
HOME_FILE="$GW_ETC/home.nft"            # после GW_ETC — иначе уедет в корень ФС
LAN_SYSCTL="/etc/sysctl.d/98-awg-gw-lan.conf"
DNSMASQ_D="/etc/dnsmasq.d"
DNSMASQ_OVR="/etc/systemd/system/dnsmasq.service.d/awg-gw.conf"
LAN_DUMP="/var/lib/awg-gw"                # слепки наборов: грузятся при старте до фидов
LAN_OLD="$LAN_DUMP/migrated"              # снятые файлы ручного слоя и личные списки —
                                          # ВНЕ conf-dir: dnsmasq читает там всё, кроме .dpkg-*
DNSMASQ_DEFAULT="/etc/default/dnsmasq"
DNSMASQ_MARK="$GW_ETC/dnsmasq.installed"  # dnsmasq ставили мы — при снятии выключаем
# Локальные подсети и резолвер — те же фильтры, что у чужих подсетей ниже
HOME_SUBNETS="$(printf '%s' "${HOME_SUBNETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"
RESOLVER="$(printf '%s' "${RESOLVER:-}" | tr -cd '0-9.' | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || true)"
LAN_LISTS="/usr/local/sbin/awg-lan-lists.sh"
LAN_DOMAIN="/usr/local/sbin/awg-lan-domain.sh"
LAN_SERVICES="/usr/local/sbin/awg-lan-services.sh"      # записи сервисов соседних сетей → dnsmasq
PEER_SVC_CONF="$DNSMASQ_D/awg-gw-peer-services.conf"
LAN_MODE="${LAN_MODE:-0}"
# Канал до ВПС: 1 — агент держит сессию внутри линка. Значения из бандла, у
# старых бандлов их нет — тогда канала нет, и это рабочее состояние.
LINK_CHANNEL="$(printf '%s' "${LINK_CHANNEL:-0}" | tr -cd '01' | cut -c1)"
LINK_CHANNEL_PORT="$(printf '%s' "${LINK_CHANNEL_PORT:-8787}" | tr -cd '0-9' | cut -c1-5)"
case "$LINK_CHANNEL_PORT" in ''|0) LINK_CHANNEL_PORT=8787 ;; esac
# Подсети за другими шлюзами (концепт «локальная сеть», функция B): им из линка
# открыт транзит в локальную сеть — по источнику, выше drop по приватным.
PEER_HOME_NETS="$(printf '%s' "${PEER_HOME_NETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"

# Публичный ключ интерфейса: `awg show <if> public-key`.
iface_pubkey() { "$AWG_BIN" show "$1" public-key 2>/dev/null || true; }
conf_pubkey() {                # $1 = конфиг; публичный ключ из PrivateKey или ничего
    _pk="$(awk -F'= *' '/^PrivateKey/{print $2; exit}' "$1" 2>/dev/null | tr -d ' \r')"
    [ -n "$_pk" ] || return 0
    printf '%s' "$_pk" | "$AWG_BIN" pubkey 2>/dev/null || true
}
# Аплинки ЭТОЙ машины строками «имя ключ»: сначала живые интерфейсы, затем
# конфиги тех, что ещё не поднялись. Линки до ВПС (свой слот и слот соседа —
# awglink*) аплинками не считаются: их ключ принадлежит паре ВПС↔шлюз, а не
# устройству, и сравнивать пометку слота с ним нельзя.
uplink_list() {
    _live=" $("$AWG_BIN" show interfaces 2>/dev/null | tr '\n' ' ')"
    for _i in $_live; do
        case "$_i" in "$LINK_IF"|awglink*) continue ;; esac
        printf '%s %s\n' "$_i" "$(iface_pubkey "$_i")"
    done
    # Конфиги: при загрузке юнит обвязки стартует РАНЬШЕ awg-quick@ аплинка, и
    # по одним живым интерфейсам машина выглядит чужой — линк ложится, юнит
    # гасит сам себя (наступили при ребуте второго шлюза 19.09.2026). Ключ
    # устройства лежит в конфиге и до подъёма интерфейса.
    for _c in "$HOST_CONF_DIR"/*.conf; do
        [ -f "$_c" ] || continue
        _n="$(basename "$_c" .conf)"
        case "$_n" in "$LINK_IF"|awglink*) continue ;; esac
        case "$_live" in *" $_n "*) continue ;; esac
        printf '%s %s\n' "$_n" "$(conf_pubkey "$_c")"
    done
}
iface_by_pubkey() {            # $1 = ключ УСТРОЙСТВА; печатает имя аплинка или ничего
    [ -n "$1" ] || return 0
    uplink_list | while read -r _n _k; do
        [ -n "$_k" ] && [ "$_k" = "$1" ] && { printf '%s' "$_n"; break; }
    done
}

# Прежняя обвязка в iptables: снимаем идемпотентно и при --apply (переезд на
# таблицу), и при --rollback. Чужих правил (docker, прежняя схема) не касаемся.
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

# ── локальная сеть без VPN (концепт «локальная сеть», функция A): помощники ────────
LAN_IF=""; LAN_ADDR=""; LAN_ERROR=""
dn_set_elsewhere() {           # $1 = regex: ключ dnsmasq уже задан в ДРУГОМ файле?
    # Debian запускает демон с conf-dir=/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,
    # .dpkg-new: читается ВСЁ, кроме этих суффиксов — и .bak, и .tmp тоже.
    for _f in /etc/dnsmasq.conf "$DNSMASQ_D"/*; do
        [ -f "$_f" ] || continue
        case "$_f" in */awg-gw-base.conf|*.dpkg-dist|*.dpkg-old|*.dpkg-new) continue ;; esac
        grep -qsE "$1" "$_f" && return 0
    done
    return 1
}
park() {                       # $1 = файл → $LAN_OLD, с датой: ничего не теряем, conf-dir чист
    [ -f "$1" ] || return 0
    run "mkdir -p $LAN_OLD"
    run "mv -f $1 $LAN_OLD/$(basename "$1").$(date +%Y%m%d%H%M%S)"
}
port53_busy() {                # слушатель :53, который не dnsmasq и мешает нам → строка или пусто
    ss -Hlnup 'sport = :53' 2>/dev/null | grep -v '"dnsmasq"' \
        | grep -E "(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]|127\.0\.0\.1|${LAN_ADDR:-NOADDR}):53([[:space:]]|$)" | head -n1
}
lan_iface_for() {              # $1 = подсеть a.b.c.d/n → «iface addr» или ничего
    ip -4 -o addr show 2>/dev/null | awk -v net="$1" '
        function ip2n(s, a) { split(s, a, "."); return ((a[1]*256+a[2])*256+a[3])*256+a[4] }
        BEGIN { split(net, p, "/"); h = 2^(32-(p[2]+0)); want = int(ip2n(p[1])/h) }
        $2 != "lo" && $2 !~ /^(awg|docker|veth|br-)/ { split($4, q, "/"); if (int(ip2n(q[1])/h) == want) { print $2, q[1]; exit } }'
}
lan_remove() {                 # снять всё своё; личные списки — в $LAN_DUMP/restore (данные человека)
    _changed=0
    for _f in "$DNSMASQ_D/awg-gw-base.conf" "$DNSMASQ_D/awg-gw-doh.conf" "$DNSMASQ_D/awg-gw-vpn-feed.conf" "$PEER_SVC_CONF"; do
        [ -f "$_f" ] && { run "rm -f $_f"; _changed=1; }
    done
    # личные списки — данные человека: не в архив с датой, а в restore/, откуда
    # следующее включение режима (или awg-bot restore) вернёт их на место
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] || continue
        run "mkdir -p $LAN_DUMP/restore"
        run "mv -f $DNSMASQ_D/$_f $LAN_DUMP/restore/$_f"; _changed=1
    done
    [ -f "$DNSMASQ_OVR" ] && { run "rm -f $DNSMASQ_OVR"; run "systemctl daemon-reload"; _changed=1; }
    if [ -f "$DNSMASQ_MARK" ]; then
        # ставили мы — снимаем: иначе на всех интерфейсах остаётся резолвер с дефолтами Debian
        run "systemctl disable --now dnsmasq 2>/dev/null || true"
        run "rm -f $DNSMASQ_MARK"
    elif [ "$_changed" = "1" ] && systemctl is-active --quiet dnsmasq 2>/dev/null; then
        run "systemctl restart dnsmasq"
    fi
    nft list table $HOME_TABLE >/dev/null 2>&1 && run "nft delete table $HOME_TABLE"
    [ -f "$HOME_FILE" ] && run "rm -f $HOME_FILE"
    [ -f "$LAN_SYSCTL" ] && run "rm -f $LAN_SYSCTL"
    [ -f "$LAN_LISTS" ] && run "rm -f $LAN_LISTS"
    [ -f "$LAN_DOMAIN" ] && run "rm -f $LAN_DOMAIN"
    [ -f "$LAN_SERVICES" ] && run "rm -f $LAN_SERVICES"
    return 0
}
lan_migrate_manual() {         # ручной слой (концепт «локальная сеть» §7): переезжает, не ломается
    _moved=""
    for _u in home-split.service awg-lists.timer awg-lists.service; do
        if [ -f "/etc/systemd/system/$_u" ]; then
            run "systemctl disable --now $_u 2>/dev/null || true"
            park "/etc/systemd/system/$_u"
            _moved="$_moved $_u"
        fi
    done
    if ip rule list 2>/dev/null | grep -q "fwmark 0x10 lookup 100"; then
        run "ip rule del fwmark 0x10 lookup 100"; _moved="$_moved ip-rule-0x10"
    fi
    nft list table inet home_split >/dev/null 2>&1 && { run "nft delete table inet home_split"; _moved="$_moved home_split"; }
    for _f in /etc/home-split.nft /etc/sysctl.d/99-home-split.conf /usr/local/sbin/home-*.sh /usr/local/bin/awg-add \
              /etc/systemd/network/99-awg-unmanaged.network \
              /etc/systemd/system/dnsmasq.service.d/*.conf /etc/systemd/system/awg-quick@${UPLINK_IF:-awg0}.service.d/*.conf \
              "$DNSMASQ_D/awg-base.conf" "$DNSMASQ_D/vpn-domains.conf"; do
        [ -f "$_f" ] || continue
        case "$_f" in */awg-gw.conf) continue ;; esac              # наши оверрайды
        park "$_f"; _moved="$_moved $(basename "$_f")"
    done
    # личные списки — данные человека: переносятся с заменой имени набора; оригинал — в $LAN_OLD
    if [ -f "$DNSMASQ_D/ru-force.conf" ]; then
        run "sed 's|inet#home_split#ru4|inet#awg_home#lan_ru4|' $DNSMASQ_D/ru-force.conf > $DNSMASQ_D/awg-gw-ru-user.conf"
        park "$DNSMASQ_D/ru-force.conf"
        _moved="$_moved ru-force.conf→awg-gw-ru-user.conf"
    fi
    if [ -f "$DNSMASQ_D/user-managed.conf" ]; then
        run "sed 's|inet#home_split#vpn4|inet#awg_home#lan_vpn4|' $DNSMASQ_D/user-managed.conf > $DNSMASQ_D/awg-gw-vpn-user.conf"
        park "$DNSMASQ_D/user-managed.conf"
        _moved="$_moved user-managed.conf→awg-gw-vpn-user.conf"
    fi
    # хвосты в conf-dir, которые dnsmasq тоже прочитает: .bak/.tmp прежних версий
    for _f in "$DNSMASQ_D"/*.bak "$DNSMASQ_D"/*.tmp "$DNSMASQ_D"/*.prev.awg; do
        [ -f "$_f" ] && { park "$_f"; _moved="$_moved $(basename "$_f")"; }
    done
    [ -n "$_moved" ] && say "  ручной слой перенесён:$_moved (снятые файлы — в $LAN_OLD)"
    return 0
}
write_lan_scripts() {          # скрипты списков — из этого же файла, чтобы бандл был самодостаточен
    # каждый — во временный файл и mv: работающий экземпляр (dash читает по
    # 8 КБ, скрипты длиннее) дочитывает свой прежний inode, а не новый текст
cat > "$LAN_LISTS.new" <<'LISTSEOF'
#!/bin/sh
# awg-lan-lists.sh — списки локальной сети без VPN (концепт «локальная сеть» §3.3).
# Зовёт агент по расписанию (с джиттером), `awg-bot lan update` и агент же, когда
# фиды привёз канал (с AWG_LAN_FROM). Идемпотентно.
# AWG_LAN_FROM=<каталог> — фиды не качать, а взять готовыми из domains.lst и
# nets.lst в этом каталоге: их привозит сервер по каналу линка (концепт «канал
# линка», этап 3), и адрес квартиры тогда не ходит за ними ни на GitHub, ни в
# Google. Проверки те же, что для скачанного: формат, длина, dnsmasq --test
# (с conf-dir, как у init-скрипта Debian); отказ --test или рестарта dnsmasq —
# откат прежнего фида и rc=1. Копия для отката — $DUMP/rollback/vpn-feed.conf.prev.
# Один запуск за раз: ждём блокировку lists.lock до 120 с, не дождались —
# выход 75 («занято»), не успех: иначе фиды из канала пропали бы молча.
#   фид доменов  → /etc/dnsmasq.d/awg-gw-vpn-feed.conf (nftset= в lan_vpn4), минус исключения
#   фиды подсетей → набор lan_vpn_nets4 (атомарно: flush + add)
#   слепки наборов → /var/lib/awg-gw/*.nft (грузятся при старте до фидов)
#   итог → /var/lib/awg-gw/lists.status: updated_at, domains, nets, rc,
#          source (net — скачано, channel — привёз канал)
set -u
TABLE="inet awg_home"
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
FEED="$D/awg-gw-vpn-feed.conf"; RU="$D/awg-gw-ru-user.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; RB="$DUMP/rollback"
mkdir -p "$DUMP"
# один запуск за раз: кнопка «Обновить» и задача агента могут совпасть, а два
# параллельных — двойная запись фида и двойной рестарт dnsmasq
exec 9>"$DUMP/lists.lock"
# Ждём до двух минут, а не выходим сразу: фиды из канала, пришедшие во время
# ручного «Обновить», иначе молча не применились бы, а агент считал бы их
# применёнными. Не дождались — код 75 («занято»), не успех.
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление уже идёт" >&2; exit 75; fi
ITDOG="https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
DOMAINS_URL="${AWG_LAN_DOMAINS_URL:-$ITDOG/Russia/inside-dnsmasq-ipset.lst}"
SUBNET_SERVICES="${AWG_LAN_SUBNET_SERVICES:-telegram meta twitter cloudflare discord}"
GOOG_URL="https://www.gstatic.com/ipranges/goog.json"
TMP="$(mktemp)"; TMP2="$(mktemp)"; NETS="$(mktemp)"
trap 'rm -f "$TMP" "$TMP2" "$NETS"' EXIT
rc=0

FROM="${AWG_LAN_FROM:-}"
get_domains() {
    if [ -n "$FROM" ]; then cp "$FROM/domains.lst" "$TMP" 2>/dev/null
    else curl -sf --max-time 60 "$DOMAINS_URL" -o "$TMP"; fi
}
# ── домены: во временный файл (недокачанный .new в conf-dir читался бы демоном)
if get_domains && [ -s "$TMP" ]; then
    # sed -i без суффикса — GNU-изм: правим через временный файл
    sed 's|^ipset=\(/.*/\)vpn_domains$|nftset=\1inet#awg_home#lan_vpn4|' "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    # исходник до вычитания — awg-lan-domain.sh пересобирает фид по нему, когда
    # меняются исключения «напрямую» (иначе новое исключение ждало бы новых фидов);
    # кладётся только после проверки длины ниже, чтобы обрезанный фид не стал исходником
    cp "$TMP" "$DUMP/vpn-feed.src.tmp"
    # исключения побеждают: dnsmasq применяет одну директиву на домен, и какая
    # из двух победит, зависело бы от порядка чтения каталога — вычитаем
    if [ -s "$RU" ]; then
        awk -F/ '
            NR==FNR { if ($0 ~ /^nftset=/) for (i=2; i<NF; i++) skip[$i]=1; next }
            $0 !~ /^nftset=/ { print; next }
            { out=$1; n=0; for (i=2; i<NF; i++) if (!($i in skip)) { out=out "/" $i; n++ }
              if (n) print out "/" $(NF) }' "$RU" "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    fi
    # Только директивы нашего набора и комментарии: HTTP 200 с HTML (заглушка
    # провайдера, страница блокировки) иначе уехал бы в conf-dir и уронил бы
    # dnsmasq на «bad option» — квартира без DNS до следующего удачного фида.
    grep -E '^(#.*|nftset=/[^/[:space:]]+(/[^/[:space:]]+)*/inet#awg_home#lan_vpn4)$' "$TMP" > "$TMP2"
    mv "$TMP2" "$TMP"
    feed_ok=0
    if [ "$(grep -c '^nftset=' "$TMP")" -lt 10 ]; then
        echo "домены: фид подозрительно короткий — не применяю" >&2; rc=1
    # дифф-скип: рестарт роняет кэш всей сети, а список меняется не каждые 6 ч
    elif ! cmp -s "$TMP" "$FEED"; then
        # копия для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
        mkdir -p "$RB"; [ -f "$FEED" ] && cp -p "$FEED" "$RB/vpn-feed.conf.prev" 2>/dev/null || true
        install -m 644 "$TMP" "$FEED"
        # conf-dir Debian подключает ключом из init-скрипта (CONFIG_DIR в
        # /etc/default/dnsmasq), и голый --test наш фид не видел вовсе
        if dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; then
            # именно restart: SIGHUP конфиги не перечитывает; отказ — откат фида
            if systemctl restart dnsmasq; then
                rm -f "$RB/vpn-feed.conf.prev"; feed_ok=1
            else
                echo "домены: dnsmasq не поднялся с новым фидом — откатываю" >&2; rc=1
                if [ -f "$RB/vpn-feed.conf.prev" ]; then mv -f "$RB/vpn-feed.conf.prev" "$FEED"; else rm -f "$FEED"; fi
                systemctl restart dnsmasq || true
            fi
        else
            echo "домены: dnsmasq --test отверг новый фид — откатываю" >&2; rc=1
            if [ -f "$RB/vpn-feed.conf.prev" ]; then mv -f "$RB/vpn-feed.conf.prev" "$FEED"; else rm -f "$FEED"; fi
        fi
    else
        feed_ok=1
    fi
    # исходник — только от принятого (или не изменившегося) фида: отвергнутый
    # исходником не становится, иначе каждая правка «напрямую» падала бы на нём
    if [ "$feed_ok" = 1 ]; then mv -f "$DUMP/vpn-feed.src.tmp" "$DUMP/vpn-feed.src"; else rm -f "$DUMP/vpn-feed.src.tmp"; fi
else
    if [ -n "$FROM" ]; then echo "домены: фида нет в $FROM (привозит канал)" >&2
    else echo "домены: фид не скачался ($DOMAINS_URL)" >&2; fi
    rc=1
fi

# ── подсети: itdoginfo по сервисам + официальный фид Google
: > "$NETS"
if [ -n "$FROM" ]; then
    cat "$FROM/nets.lst" >> "$NETS" 2>/dev/null || { echo "подсети: нет $FROM/nets.lst" >&2; rc=1; }
else
    for svc in $SUBNET_SERVICES; do
        curl -sf --max-time 60 "$ITDOG/Subnets/IPv4/${svc}.lst" >> "$NETS" 2>/dev/null \
            || { echo "подсети: $svc не скачался" >&2; rc=1; }
        echo >> "$NETS"
    done
    curl -sf --max-time 60 "$GOOG_URL" 2>/dev/null \
        | grep -oE '"ipv4Prefix":[[:space:]]*"[0-9./]+"' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' >> "$NETS" \
        || { echo "подсети: goog.json не скачался" >&2; rc=1; }
fi
elems="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$' "$NETS" | sort -u | paste -sd, -)"
if [ -n "$elems" ]; then
    # одной транзакцией: между flush и add окна нет
    printf 'flush set %s lan_vpn_nets4\nadd element %s lan_vpn_nets4 { %s }\n' "$TABLE" "$TABLE" "$elems" \
        | nft -f - || { echo "подсети: nft отклонил набор" >&2; rc=1; }
fi

# ── слепки: при старте таблица пуста, пока агент не сходит за фидами
mkdir -p "$DUMP"
for s in lan_vpn4 lan_vpn_nets4; do
    nft list set $TABLE $s > "$DUMP/$s.nft.tmp" 2>/dev/null && mv -f "$DUMP/$s.nft.tmp" "$DUMP/$s.nft"
done
_dom="$(grep -c '^nftset=' "$FEED" 2>/dev/null)"; _dom="${_dom:-0}"
_nets="$(printf '%s' "$elems" | tr ',' '\n' | grep -c .)"; _nets="${_nets:-0}"
printf 'updated_at=%s\ndomains=%s\nnets=%s\nrc=%s\nsource=%s\n' "$(date -Iseconds)" "$_dom" "$_nets" "$rc" \
    "$([ -n "$FROM" ] && echo channel || echo net)" > "$DUMP/lists.status"
exit $rc
LISTSEOF
chmod 0755 "$LAN_LISTS.new" && mv -f "$LAN_LISTS.new" "$LAN_LISTS"
cat > "$LAN_DOMAIN.new" <<'DOMEOF'
#!/bin/sh
# awg-lan-domain.sh — свои списки локальной сети без VPN (концепт «локальная
# сеть» §3.3; концепт «синхронизация своих списков», этап 1).
#   add <домен…>   — в туннель (awg-gw-vpn-user.conf, набор lan_vpn4)
#   ru  <домен…>   — напрямую, российский адрес (awg-gw-ru-user.conf, набор lan_ru4)
#   del <домен…>   — убрать из обоих
#   list           — показать: «vpn <домен>» / «ru <домен>»
#   sync <файл>    — полный список строками «vpn <домен>» / «ru <домен>» (его
#                    собирает агент из канона сервера AWG): оба файла целиком,
#                    один рестарт dnsmasq; чужая строка — отказ целиком, rc=2
# Домен накрывает поддомены. Схема и www. отбрасываются. Хост сервера AWG
# (Endpoint аплинка) добавить нельзя: увести туннель в туннель — запереть себя.
# Одна блокировка со скриптом фидов (lists.lock): два писателя одних файлов и
# два рестарта dnsmasq разом не бывает; занято дольше 120 с — код 75. list —
# без блокировки. Исключения «напрямую» вычитаются из фида доменов заново по
# сохранённому исходнику vpn-feed.src (его кладёт awg-lan-lists.sh; нет
# исходника — фид выправит ближайшая сборка). Домен, ушедший из «напрямую»,
# уходит и из набора lan_ru4 (flush + dig оставшихся: набор маленький и
# наполняется только отсюда); ушедший из «в туннель» вынимается из lan_vpn4 по
# адресам dig, ошибки глушатся (адрес мог слиться в интервал или принадлежать
# домену фида). Копии для отката обоих файлов и фида — $DUMP/rollback/<имя>.prev.
# Правило домена — DOMAIN_RE (зона буквами или punycode «xn--»);
# то же правило применит сервер AWG к общему списку (этап 2 синхронизации).
# awg-lan-domain: sync
set -u
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
VPN="$D/awg-gw-vpn-user.conf"; RU="$D/awg-gw-ru-user.conf"; FEED="$D/awg-gw-vpn-feed.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; SRC="$DUMP/vpn-feed.src"; RB="$DUMP/rollback"
TABLE="inet awg_home"
DOMAIN_RE='([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})'
# Аплинк — тот, что нашёл скрипт обвязки (UPLINK_IF в статусе): на машине с
# аплинком не awg0 иначе хост ВПС не был бы под запретом
_up="$(sed -n 's/^UPLINK_IF=//p' /etc/awg-gw/gateway.status 2>/dev/null | head -n1 | tr -cd 'A-Za-z0-9_.-')"
UPLINK_CONF="${AWG_UPLINK_CONF:-/etc/amnezia/amneziawg/${_up:-awg0}.conf}"
cmd="${1:-}"; [ $# -gt 0 ] && shift
[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }
mkdir -p "$DUMP"
touch "$VPN" "$RU"
list_of() {                    # $1 = файл → домены по одному на строку, по алфавиту
    # строка может нести несколько доменов (nftset=/a/b/набор — мигрированный ручной слой)
    sed -n 's|^nftset=\(/.*\)/inet#.*|\1|p' "$1" | tr '/' '\n' | grep . | sort -u
}
case "$cmd" in
    list)
        list_of "$VPN" | sed 's|^|vpn |'
        list_of "$RU" | sed 's|^|ru |'
        exit 0 ;;
    add|ru|del) [ $# -gt 0 ] || { echo "usage: $0 $cmd <домен…>"; exit 1; } ;;
    sync) [ $# -eq 1 ] && [ -f "${1:-}" ] || { echo "usage: $0 sync <файл>"; exit 1; } ;;
    *) echo "usage: $0 add|ru|del <домен…> | list | sync <файл>"; exit 1 ;;
esac
# одна блокировка со скриптом фидов: он читает ru-user.conf и тоже перезапускает dnsmasq
exec 9>"$DUMP/lists.lock"
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление списков ещё идёт" >&2; exit 75; fi
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
deny="$(sed -n 's/^Endpoint *= *\([^:]*\):.*/\1/p' "$UPLINK_CONF" 2>/dev/null | head -n1 | tr 'A-Z' 'a-z')"
valid() { [ "${#1}" -le 253 ] && [ "$(printf '%s' "$1" | grep -c '')" -le 1 ] && printf '%s' "$1" | grep -Eq "^$DOMAIN_RE$"; }
in_list() { grep -qxF "$1" "$TMPD/want_$2"; }             # $1 домен, $2 vpn|ru
list_of "$VPN" > "$TMPD/before_vpn"; list_of "$RU" > "$TMPD/before_ru"
cp "$TMPD/before_vpn" "$TMPD/want_vpn"; cp "$TMPD/before_ru" "$TMPD/want_ru"
drop_from() { grep -vxF "$1" "$TMPD/want_$2" > "$TMPD/x" || true; mv "$TMPD/x" "$TMPD/want_$2"; }
add_to() { printf '%s\n' "$1" >> "$TMPD/want_$2"; }
added=""                       # «домен:набор» — наполнить после рестарта
# «добавлен»/«убран» — только после удачного рестарта: при откате человек видел
# бы «добавлен» рядом с «откатываю»
say() { printf '%s\n' "$1" >> "$TMPD/said"; }
: > "$TMPD/said"
if [ "$cmd" = "sync" ]; then
    if grep -Ev "^(vpn|ru) $DOMAIN_RE\$" "$1" | grep -q . || ! awk 'length($2) > 253 { bad=1 } END { exit bad }' "$1"; then
        echo "в файле есть строка не вида «vpn <домен>» / «ru <домен>» — не применяю" >&2; exit 2
    fi
    sed -n 's/^ru //p' "$1" | sort -u > "$TMPD/want_ru"
    # домен в обоих видах — «напрямую», как решает nft; хост сервера — пропущен с пометкой
    sed -n 's/^vpn //p' "$1" | sort -u | grep -vxF -f "$TMPD/want_ru" > "$TMPD/want_vpn" || true
    if [ -n "$deny" ]; then
        for f in want_vpn want_ru; do
            if grep -qxF "$deny" "$TMPD/$f"; then echo "$deny: это хост сервера — пропущен"; drop_from "$deny" "${f#want_}"; fi
        done
    fi
else
    for raw in "$@"; do
        d="$(printf '%s' "$raw" | sed -E 's|^[a-zA-Z]+://||; s|/.*$||; s|^www\.||' | tr 'A-Z' 'a-z')"
        valid "$d" || { echo "$d: не похоже на домен, пропущен"; continue; }
        if [ "$cmd" = "del" ]; then
            if in_list "$d" vpn || in_list "$d" ru; then
                drop_from "$d" vpn; drop_from "$d" ru; say "$d: убран"
            else
                echo "$d: в списках нет"
            fi
            continue
        fi
        [ -n "$deny" ] && [ "$d" = "$deny" ] && { echo "$d: это хост сервера — его добавить нельзя"; continue; }
        if [ "$cmd" = "add" ]; then k=vpn; other=ru; set_="lan_vpn4"; else k=ru; other=vpn; set_="lan_ru4"; fi
        in_list "$d" "$k" && { echo "$d: уже в списке"; continue; }
        drop_from "$d" "$other"; add_to "$d" "$k"
        added="$added $d:$set_"
        say "$d: добавлен"
    done
fi
sort -u -o "$TMPD/want_vpn" "$TMPD/want_vpn"; sort -u -o "$TMPD/want_ru" "$TMPD/want_ru"
if cmp -s "$TMPD/want_vpn" "$TMPD/before_vpn" && cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    [ "$cmd" = "sync" ] && echo "свои списки без изменений"
    exit 0
fi
# ── файлы: по домену на строку, по алфавиту (sync и кнопка пишут одинаково)
sed 's|.*|nftset=/&/inet#awg_home#lan_vpn4|' "$TMPD/want_vpn" > "$TMPD/vpn.conf"
sed 's|.*|nftset=/&/inet#awg_home#lan_ru4|' "$TMPD/want_ru" > "$TMPD/ru.conf"
changed="$VPN $RU"
# копии для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
mkdir -p "$RB"
prev() { printf '%s/%s.prev' "$RB" "$(basename "$1")"; }
for f in "$VPN" "$RU"; do cp -p "$f" "$(prev "$f")" 2>/dev/null || : > "$(prev "$f")"; done
install -m 644 "$TMPD/vpn.conf" "$VPN"; install -m 644 "$TMPD/ru.conf" "$RU"
# ── фид доменов: исключения «напрямую» вычитаются заново по исходнику (тот же
# awk, что в awg-lan-lists.sh); исходника нет — фид выправит ближайшая сборка
if [ -s "$SRC" ] && ! cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    # пустой список «напрямую» — первому файлу awk нужна хоть одна строка, иначе
    # NR==FNR сработает уже на исходнике и вычтет из фида всё
    { echo "# ru"; cat "$RU"; } > "$TMPD/ru_awk"
    awk -F/ '
        NR==FNR { if ($0 ~ /^nftset=/) for (i=2; i<NF; i++) skip[$i]=1; next }
        $0 !~ /^nftset=/ { print; next }
        { out=$1; n=0; for (i=2; i<NF; i++) if (!($i in skip)) { out=out "/" $i; n++ }
          if (n) print out "/" $(NF) }' "$TMPD/ru_awk" "$SRC" \
        | grep -E '^(#.*|nftset=/[^/[:space:]]+(/[^/[:space:]]+)*/inet#awg_home#lan_vpn4)$' > "$TMPD/feed.conf" || true
    if [ -s "$TMPD/feed.conf" ] && ! cmp -s "$TMPD/feed.conf" "$FEED"; then
        cp -p "$FEED" "$(prev "$FEED")" 2>/dev/null || : > "$(prev "$FEED")"
        install -m 644 "$TMPD/feed.conf" "$FEED"; changed="$changed $FEED"
    fi
fi
rollback() { for f in $changed; do mv -f "$(prev "$f")" "$f"; done; systemctl restart dnsmasq || true; }
# conf-dir Debian подключает ключом из init-скрипта — голый --test файлы не видит
if command -v dnsmasq >/dev/null 2>&1 && ! dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; then
    echo "dnsmasq --test отверг списки — откатываю" >&2; rollback; exit 1
fi
if ! systemctl restart dnsmasq; then
    echo "dnsmasq не поднялся со своими списками — откатываю" >&2; rollback; exit 1
fi
for f in $changed; do rm -f "$(prev "$f")"; done
cat "$TMPD/said"
sleep 1
# ── наборы nft
resolve() { dig +short +time=3 +tries=1 @127.0.0.1 "$1" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; }
set_op() {                     # set_op add|delete <набор> <домен> — адреса домена в набор / из набора
    for ip in $(resolve "$3"); do nft "$1" element $TABLE "$2" "{ $ip }" 2>/dev/null || true; done
}
# ушедшие из «в туннель» — вынуть адреса (ошибки глушатся: интервал auto-merge, домен фида)
grep -vxF -f "$TMPD/want_vpn" "$TMPD/before_vpn" 2>/dev/null | while read -r d; do
    [ -n "$d" ] && set_op delete lan_vpn4 "$d"
done
# «напрямую» изменилось — набор целиком заново: он маленький и наполняется только отсюда
if ! cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    nft flush set $TABLE lan_ru4 2>/dev/null || true
    while read -r d; do
        [ -n "$d" ] && set_op add lan_ru4 "$d"
    done < "$TMPD/want_ru"
fi
snapshot_vpn() {               # слепок набора грузится при старте: без него снятый адрес вернулся бы с загрузкой
    nft list set $TABLE lan_vpn4 > "$DUMP/lan_vpn4.nft.tmp" 2>/dev/null && mv -f "$DUMP/lan_vpn4.nft.tmp" "$DUMP/lan_vpn4.nft" || rm -f "$DUMP/lan_vpn4.nft.tmp"
}
if [ "$cmd" = "sync" ]; then
    grep -vxF -f "$TMPD/before_vpn" "$TMPD/want_vpn" 2>/dev/null | sed 's|^|+ |; s|$| (в туннель)|'
    grep -vxF -f "$TMPD/before_ru" "$TMPD/want_ru" 2>/dev/null | sed 's|^|+ |; s|$| (напрямую)|'
    # «−» — только ушедшие из обоих видов; сменившие вид уже названы строкой «+»
    grep -vxF -f "$TMPD/want_ru" "$TMPD/before_ru" 2>/dev/null | grep -vxF -f "$TMPD/want_vpn" | sed 's|^|− |'
    grep -vxF -f "$TMPD/want_vpn" "$TMPD/before_vpn" 2>/dev/null | grep -vxF -f "$TMPD/want_ru" | sed 's|^|− |'
    grep -vxF -f "$TMPD/before_vpn" "$TMPD/want_vpn" 2>/dev/null | while read -r d; do
        [ -n "$d" ] && set_op add lan_vpn4 "$d"
    done
    snapshot_vpn
    echo "свои списки: $(grep -c . "$TMPD/want_vpn") в туннель, $(grep -c . "$TMPD/want_ru") напрямую"
    exit 0
fi
for pair in $added; do
    d="${pair%%:*}"; set_="${pair##*:}"; n=0
    for ip in $(resolve "$d"); do
        # «напрямую» уже наполнен целиком выше — только считаем
        if [ "$set_" = "lan_ru4" ] || nft add element $TABLE $set_ "{ $ip }" 2>/dev/null; then n=$((n+1)); fi
    done
    echo "  $d → $n адрес(а) в наборе $set_"
done
snapshot_vpn
DOMEOF
chmod 0755 "$LAN_DOMAIN.new" && mv -f "$LAN_DOMAIN.new" "$LAN_DOMAIN"
cat > "$LAN_SERVICES.new" <<'SVCEOF'
#!/bin/sh
# awg-lan-services.sh — записи сервисов соседних сетей для dnsmasq (концепт
# «сервисы соседних сетей»). Зовёт агент: с путём к файлу — установить, без
# аргумента — снять. Содержимое агент собирает из данных, пришедших каналом
# линка с сервера AWG, поэтому файл проверяется построчно по белому списку
# шаблонов (тот же список — LINE_RES в awgbot/domain/gwservices.py): ни
# server=, ни address=, ни conf-file= сюда не пролезут. Дальше как у списков:
# тот же файл — выход без рестарта; dnsmasq --test с conf-dir; рестарт; отказ —
# откат прежнего файла (копия — $DUMP/rollback/peer-services.conf.prev) и
# rc=1. Блокировка общая с awg-lan-lists.sh: двух
# рестартов dnsmasq разом не бывает. rc=2 — файл не прошёл проверку.
set -u
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
CONF="$D/awg-gw-peer-services.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; RB="$DUMP/rollback"; PREV="$RB/peer-services.conf.prev"
DOMAIN='awg\.internal'
[ "$(id -u)" = "0" ] || { echo "нужен root" >&2; exit 1; }
mkdir -p "$DUMP" "$RB"
exec 9>"$DUMP/lists.lock"
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление списков или записей SMB ещё идёт" >&2; exit 75; fi
# conf-dir Debian подключает ключом из init-скрипта — голый --test файл не видит
test_conf() { dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; }
restart_dnsmasq() { test_conf && systemctl restart dnsmasq; }
rollback() { if [ -f "$PREV" ]; then mv -f "$PREV" "$CONF"; else rm -f "$CONF"; fi; }
if [ $# -eq 0 ]; then
    [ -f "$CONF" ] || exit 0
    rm -f "$CONF"
    restart_dnsmasq || { echo "dnsmasq не поднялся после снятия записей SMB: journalctl -u dnsmasq -e" >&2; exit 1; }
    echo "записи SMB подсетей других шлюзов сняты"
    exit 0
fi
SRC="$1"
[ -s "$SRC" ] || { echo "файл записей пуст или не найден: $SRC" >&2; exit 2; }
if grep -Ev -e '^#.*$' -e '^$' \
    -e "^local=/$DOMAIN/\$" \
    -e "^ptr-record=l?b\\._dns-sd\\._udp\\.([0-9]{1,3}\\.){4}in-addr\\.arpa,$DOMAIN\$" \
    -e "^ptr-record=l?b\\._dns-sd\\._udp\\.$DOMAIN,$DOMAIN\$" \
    -e "^ptr-record=_services\\._dns-sd\\._udp\\.$DOMAIN,_smb\\._tcp\\.$DOMAIN\$" \
    -e "^ptr-record=_smb\\._tcp\\.$DOMAIN,\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\"\$" \
    -e "^srv-host=\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\",[A-Za-z0-9-]{1,63}\\.$DOMAIN,[0-9]{1,5}\$" \
    -e "^txt-record=\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\",\"\"\$" \
    -e "^host-record=[A-Za-z0-9-]{1,63}\\.$DOMAIN,[0-9]{1,3}(\\.[0-9]{1,3}){3}\$" \
    "$SRC" >/dev/null; then
    echo "в файле записей есть строка вне белого списка — не применяю" >&2; exit 2
fi
if cmp -s "$SRC" "$CONF" 2>/dev/null; then echo "записи SMB подсетей других шлюзов без изменений"; exit 0; fi
# копия для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
if [ -f "$CONF" ]; then cp -p "$CONF" "$PREV" 2>/dev/null || true; fi
install -m 0644 "$SRC" "$CONF"
if ! test_conf; then
    # демон с плохим файлом не перезапускался — откат без рестарта, кэш сети цел
    echo "dnsmasq --test отверг записи SMB — откатываю" >&2; rollback; exit 1
fi
if systemctl restart dnsmasq; then
    rm -f "$PREV"
    echo "записи SMB подсетей других шлюзов применены: $(grep -c '^srv-host=' "$CONF")"
    exit 0
fi
echo "dnsmasq не поднялся с записями SMB — откатываю" >&2
rollback
systemctl restart dnsmasq || true
exit 1
SVCEOF
chmod 0755 "$LAN_SERVICES.new" && mv -f "$LAN_SERVICES.new" "$LAN_SERVICES"
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

# Адрес сервера снаружи — хост Endpoint из конфига линка: IP как есть, имя —
# один резолв (при отказе пусто: набор server4 наполнит агент по тику).
# v6-литерал ([…]:порт) снаружи не поддерживается — пусто.
server_ipv4() {
    _h="$(awk '/^[[:space:]]*Endpoint[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/, ""); print; exit}' \
        "${1:-/dev/null}" 2>/dev/null | sed 's/:[0-9]*$//')"
    case "$_h" in
        ""|\[*) return 0 ;;
        *[!0-9.]*) getent ahostsv4 "$_h" 2>/dev/null | awk '{print $1; exit}' ;;
        *) printf '%s\n' "$_h" ;;
    esac
}

# Локальная сеть — подсети интерфейса квартиры (маршруты scope link): именно
# они, а не RFC1918 целиком — за CGNAT провайдера (100.64/10) соседи по пулу
# приходили бы через проброс как «свои». Пусто — RFC1918 (запас).
lan_ipv4() {
    ip -4 route show dev "$1" scope link 2>/dev/null | awk '$1 ~ /\// {print $1}' \
        | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g'
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
    -h|--help)  sed -n '2,80p' "$0"; exit 0 ;;
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
say "  интерфейс линка    : $LINK_IF"
say "  контейнер          : ${CONTAINER:-нет (и не нужен)}"
say "  конфиг на хосте    : $HOST_CONF_DIR/$LINK_IF.conf"
say "  клиенты сервера AWG: $CLIENT_SUBNET"
say "  выход в интернет   : $WAN_IF"
say "  локальные сети     : будут ЗАКРЫТЫ для клиентов (все приватные диапазоны)"

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
    lan_remove
    if command -v iptables >/dev/null 2>&1; then
        for _i in "$LINK_IF" $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^(awg|end|eth|br|wl)'); do
            for _d in -i -o; do
                while iptables -w -C FORWARD $_d "$_i" -j ACCEPT 2>/dev/null; do
                    run "iptables -w -D FORWARD $_d $_i -j ACCEPT"
                done
            done
        done
    fi
    run "nft delete table $GUARD_TABLE 2>/dev/null || true"
    # firewall.env — данные человека (адреса, порт): не удаляем, а откладываем.
    [ -f "$FW_ENV" ] && run "mv -f $FW_ENV $FW_ENV.bak"
    run "rm -f $GUARD_FILE $GW_STATUS_FILE"
    run "rmdir $GW_ETC 2>/dev/null || true"
    run "rm -f $HOST_CONF_DIR/$LINK_IF.conf $UNIT $SYSCTL_CONF /etc/systemd/networkd.conf.d/awg-gw.conf $NETWORKD_UNMANAGED"
    run "rm -f /etc/systemd/system/awg-quick@*.service.d/awg-gw.conf"
    run "systemctl daemon-reload"
    say ""
    say "Готово. Существующие интерфейсы и маршрутизация шлюза не тронуты."
    exit 0
fi

if [ "$MODE" = "plan" ]; then
    say ""
    say "(режим показа — добавь: --apply <файл-конфига-с-сервера-AWG>)"
    say ""
    say "Будет сделано:"
    say "  1. конфиг → $HOST_CONF_DIR/$LINK_IF.conf, awg-quick up хостовыми утилитами"
    say "  2. таблица nft $GUARD_TABLE: MASQUERADE $CLIENT_SUBNET → $WAN_IF, изоляция"
    say "     клиентов от приватных сетей, метки Telegram и GitHub → аплинк, защита шлюза от туннеля"
    say "     (с туннеля на шлюз: сервер AWG по линку; полный доступ — ADMIN_IPS=${ADMIN_IPS:-—})"
    say "     SSH снаружи (порт $SSH_PORT): фильтр $([ "$SSH_FILTER" = 1 ] && echo включён || echo выключен); адреса: ${SSH_ALLOW:-—}"
    say "  3. снятие прежних правил iptables ($FWD_CHAIN, MASQUERADE, метки)"
    say "  0. шлюзовое устройство: ${GATEWAY_PUBKEY:+помечен, конфиг аплинка ставится машине с тем же ключом}${GATEWAY_PUBKEY:-не помечен}"
    say "  4. юнит awg-link-gw.service"
    say "  5. локальная сеть без VPN: ${LAN_MODE:-0} (подсети: ${HOME_SUBNETS:-—}; резолвер: ${RESOLVER:-запасной через аплинк})"
    say "     SMB подсетей других шлюзов (в Finder: «Сеть» → awg.internal): $([ -n "${PEER_HOME_NETS:-}" ] && [ "${LAN_MODE:-0}" = "1" ] && [ "${LINK_CHANNEL:-0}" = "1" ] && echo включены || echo нет)"
    say "     локальные подсети других шлюзов (транзит из линка): ${PEER_HOME_NETS:-—}"
    exit 0
fi

# ── 0. шлюзовое устройство: аплинк и чей это шлюз ───────────────────────────
# Аплинк — клиентский туннель этой машины к ВПС, которым она является
# устройством админа. Помеченный шлюз приезжает в бандле ключом аплинка и его
# конфигом. Конфиг ставим ТОЛЬКО машине с тем же ключом (или со старым ключом
# пары в окне переезда): чужая машина шлюзом не становится, пока основной бот
# её не пометит, — она попросит переслать сообщение и линк не поднимет.
step "0. Шлюзовое устройство"
mkdir -p "$GW_ETC"
UPLINK_STATE="none"
if [ -z "$GATEWAY_PUBKEY" ]; then
    say "  в основном боте шлюз не назначен — после применения агент попросит переслать сообщение"
    GW_STATUS="unmarked"
else
    UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PUBKEY")"
    [ -z "$UPLINK_IF" ] && [ -n "$GATEWAY_PREV_PUBKEY" ] && UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PREV_PUBKEY")"
    # ЧИСТАЯ машина: аплинка нет вовсе. Бандл выпущен основным ботом под это
    # устройство и несёт его ключи — кто применил бандл, тот и шлюз. Ставим
    # аплинк под именем UPLINK_IF (по умолчанию awg0). Машина с ЧУЖИМ аплинком
    # шлюзом не становится — это ветка «foreign» ниже.
    _others="$(uplink_list | cut -d' ' -f1)"
    if [ -z "$UPLINK_IF" ] && [ -z "$_others" ] && [ -n "$UPLINK_B64" ] \
       && [ ! -f "$HOST_CONF_DIR/${UPLINK_IF_DEFAULT}.conf" ]; then
        UPLINK_IF="$UPLINK_IF_DEFAULT"
        say "  чистая машина: аплинк $UPLINK_IF ставится из бандла"
    fi
    if [ -n "$UPLINK_IF" ]; then
        say "  это помеченный шлюз: аплинк $UPLINK_IF"
        GW_STATUS="confirmed"
        # Аплинк на загрузке поднимается дольше, чем systemd готов ждать: пять
        # отказов за десять секунд (DNS ещё не резолвит Endpoint) — и юнит
        # сдаётся навсегда, малина остаётся без Telegram и без списков.
        # Снимаем лимит и перезапускаем до победы.
        _ovr="/etc/systemd/system/awg-quick@$UPLINK_IF.service.d"
        if [ ! -f "$_ovr/awg-gw.conf" ]; then
            run "mkdir -p $_ovr"
            run "printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=on-failure\nRestartSec=10\n' > $_ovr/awg-gw.conf"
            run "systemctl daemon-reload"
        else
            say "  оверрайд awg-quick@$UPLINK_IF (перезапуск до победы) уже есть"
        fi
        if [ -n "$UPLINK_B64" ]; then
            # временный каталог тут ни к чему: файл живёт рядом с
            # прочим состоянием шлюза и сразу удаляется
            _tmp="$GW_ETC/uplink.new"
            printf '%s' "$UPLINK_B64" | base64 -d > "$_tmp" 2>/dev/null || : > "$_tmp"
            if grep -q '^PrivateKey' "$_tmp"; then
                # PostUp/PostDown — ВНУТРИ [Interface], сразу за Table = off:
                # awg-quick вырезает их только из этой секции, в [Peer] они
                # уходят в setconf и роняют подъём («Line unrecognized»)
                awk -v mark="$TG_MARK" -v tbl="$UPLINK_TABLE" '
                    { print }
                    /^Table = off$/ {
                        printf "PostUp = ip rule list | grep -q \"fwmark %s lookup %s\" || ip rule add fwmark %s lookup %s\n", mark, tbl, mark, tbl
                        printf "PostUp = ip route replace default dev %%i table %s\n", tbl
                        printf "PostUp = sysctl -qw net.ipv4.conf.%%i.rp_filter=2\n"
                        printf "PostDown = ip route del default dev %%i table %s 2>/dev/null || true\n", tbl
                    }' "$_tmp" > "$_tmp.conf"
                _dst="$HOST_CONF_DIR/$UPLINK_IF.conf"
                if [ -f "$_dst" ] && cmp -s "$_tmp.conf" "$_dst"; then
                    say "  конфиг аплинка не изменился"
                    UPLINK_STATE="unchanged"
                else
                    UPLINK_STATE="installed"
                    say "  конфиг аплинка обновлён из бандла (Table = off, политика по метке $TG_MARK → таблица $UPLINK_TABLE)"
                    _bak=""
                    if [ -f "$_dst" ]; then
                        _bak="$_dst.bak-$(date +%Y%m%d%H%M%S)"
                        run "cp -p $_dst $_bak"
                    fi
                    run "install -m 600 $_tmp.conf $_dst"
                    run "$AWG_QUICK down $UPLINK_IF 2>/dev/null || true"
                    # Аплинк — связь самого агента с Telegram. Не поднялся —
                    # немедленно назад на прежний конфиг, иначе шлюз режет себе
                    # связь и чинить его придётся руками.
                    if ! "$AWG_QUICK" up "$UPLINK_IF"; then
                        say "  ОШИБКА: аплинк с новым конфигом не поднялся — откатываю на прежний"
                        if [ -n "$_bak" ]; then
                            run "cp -p $_bak $_dst"
                            run "$AWG_QUICK up $UPLINK_IF || true"
                        fi
                        rm -f "$_tmp" "$_tmp.conf"
                        exit 1
                    fi
                    run "systemctl enable awg-quick@$UPLINK_IF 2>/dev/null || true"
                fi
            else
                say "  конфиг аплинка в бандле не разобрался — не трогаю"
            fi
            rm -f "$_tmp" "$_tmp.conf"
        fi
    elif [ -n "$_others" ]; then
        say "  ВНИМАНИЕ: конфигурация слота ($LINK_IF) выпущена ДРУГОМУ устройству"
        say "  (ключ ${GATEWAY_PUBKEY%%????????????????????????????????}…), у этой машины аплинк: $(printf '%s' "$_others" | tr '\n' ' ')."
        say "  Линк слота НЕ поднимаю: трафик пошёл бы мимо помеченного шлюза."
        say "  Агент попросит переслать сообщение основному боту; после пометки"
        say "  перевыпусти конфигурацию слота и примени её ещё раз."
        GW_FOREIGN=1
        GW_STATUS="foreign"
    else
        # Аплинка нет ни живого, ни в конфигах — сказать «шлюз чужой» не по чему.
        # Раньше эта ветка сливалась с «чужим»: линк опускался, юнит выключал
        # сам себя, и машина не вставала обратно даже когда аплинк поднимался.
        say "  ВНИМАНИЕ: аплинка этой машины не видно — ни интерфейса, ни конфига в $HOST_CONF_DIR."
        say "  Подтвердить, что шлюз слота ($LINK_IF) — эта машина, нечем: линк не трогаю."
        say "  Подними аплинк (awg-quick up <имя>) и примени конфигурацию слота ещё раз."
        GW_UNCONFIRMED=1
        GW_STATUS="unconfirmed"
    fi
fi
write_status() {   # $1 = состояние линка
    [ "$MODE" = "plan" ] && return 0
    printf 'GW_STATUS=%s\nGATEWAY_PUBKEY=%s\nUPLINK=%s\nUPLINK_IF=%s\nLINK=%s\nLAN=%s\nLAN_IF=%s\nLAN_ADDR=%s\nLAN_ERROR=%s\nSSH_PORT=%s\nSSH_FILTER=%s\nSSH_ALLOW_COUNT=%s\n' \
        "$GW_STATUS" "$GATEWAY_PUBKEY" "$UPLINK_STATE" "${UPLINK_IF:-}" "$1" \
        "${LAN_MODE:-0}" "${LAN_IF:-}" "${LAN_ADDR:-}" "${LAN_ERROR:-}" \
        "$SSH_PORT" "$SSH_FILTER" "$(set -- $SSH_ALLOW; echo $#)" > "$GW_STATUS_FILE"
}
write_status "pending"

# ── 1. конфиг и подъём ───────────────────────────────────────────────────────
[ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || {
    say ""
    say "ОШИБКА: укажи файл конфига, полученный с сервера AWG:"
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
if [ "$GW_FOREIGN" = "1" ]; then
    # Юнит обвязки НЕ выключаем: он идемпотентен и при следующем старте
    # перепроверит пометку сам. Выключенный юнит означал ручное вмешательство
    # даже там, где машина шлюзом осталась.
    say "  линк не поднимаю (слот помечен другому устройству); если был поднят — опускаю"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    write_status "foreign"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк лежит до пометки этой машины шлюзом слота."
    exit 0
fi
if [ "$GW_UNCONFIRMED" = "1" ]; then
    say "  линк не трогаю (аплинк не найден — шлюз слота не подтверждён)"
    write_status "unconfirmed"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк — как был, до подъёма аплинка."
    exit 0
fi
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
say "  Подсеть линка: $LINK_CIDR, сервер AWG на линке: $LINK_PEER"

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
# rp_filter=2 (loose): ответы из интернета приходят в аплинк, а обратный путь
# до их источника по main лежит через локальный интерфейс — строгая проверка
# такое роняет. Loose пропускает, если маршрут к источнику есть хоть где-то;
# выключать проверку совсем незачем. Аплинк — PostUp его конфига (интерфейса
# на момент применения sysctl ещё нет).
run "printf 'net.ipv4.ip_forward = 1\\nnet.ipv4.conf.all.rp_filter = 2\\nnet.ipv4.conf.default.rp_filter = 2\\n' > $SYSCTL_CONF"
run "sysctl -qw net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2"

# ── 1b. политика «Telegram → аплинк»: правило по метке и таблица ─────────────
# Ставит PostUp аплинка при подъёме, но systemd-networkd при (пере)запуске по
# умолчанию сносит чужие ip rule и маршруты: аплинк жив, метка стоит, а
# Telegram агента уходит провайдеру квартиры. Запрещаем networkd их трогать
# (drop-in читается при его следующем старте — ровно тогда, когда он и снёс
# бы) и перевыставляем политику здесь: реассерт юнита её тоже чинит.
step "1b. Политика Telegram → аплинк"
NETWORKD_DROPIN="/etc/systemd/networkd.conf.d/awg-gw.conf"
if [ -d /etc/systemd ] && command -v networkctl >/dev/null 2>&1; then
    if [ -f "$NETWORKD_DROPIN" ]; then
        say "  drop-in networkd уже есть"
    else
        run "mkdir -p /etc/systemd/networkd.conf.d"
        run "printf '[Network]\nManageForeignRoutes=no\nManageForeignRoutingPolicyRules=no\n' > $NETWORKD_DROPIN"
    fi
    # Сами интерфейсы awg* networkd тоже не должен подхватывать: с ним на
    # каждом перезапуске (любой Apply в OMV) оба туннеля переставали слать
    # пакеты — хендшейк застывал, sent не рос, юниты active. Снаружи это
    # выглядело отказом ВПС.
    if [ -f "$NETWORKD_UNMANAGED" ]; then
        say "  awg* для networkd unmanaged — уже"
    else
        run "mkdir -p /etc/systemd/network"
        run "printf '[Match]\nName=awg*\n\n[Link]\nUnmanaged=yes\n' > $NETWORKD_UNMANAGED"
        # .network читается при старте networkd и по reload; рестарт делать нельзя —
        # уронит LAN. reload безопасен: конфиг локального интерфейса не менялся.
        run "networkctl reload 2>/dev/null || true"
    fi
else
    say "  systemd-networkd не используется — drop-in не нужен"
fi
if [ -n "${UPLINK_IF:-}" ] && [ "$GW_STATUS" = "confirmed" ] && ip link show "$UPLINK_IF" >/dev/null 2>&1; then
    if ip rule list | grep -q "fwmark $TG_MARK lookup $UPLINK_TABLE"; then
        say "  правило по метке есть"
    else
        run "ip rule add fwmark $TG_MARK lookup $UPLINK_TABLE"
    fi
    run "ip route replace default dev $UPLINK_IF table $UPLINK_TABLE"
else
    say "  аплинк не поднят или шлюз не помечен — политику ставит PostUp при подъёме"
fi

# ── 2. таблица nft — вся обвязка одним атомарным файлом ──────────────────────
step "2. Таблица $GUARD_TABLE"
say "  MASQUERADE $CLIENT_SUBNET и $LINK_CIDR → $WAN_IF: российские сервисы увидят"
say "  локальный адрес; с адреса линка ходит зонд живости с сервера AWG."
say "  Изоляция: клиентам из туннеля закрыты все приватные сети (NAS, роутер,"
say "  docker, link-local), остальное — транзит наружу."
say "  Защита шлюза: с адресов туннеля на саму машину пускаем только сервер AWG по"
say "  линку ($LINK_PEER: SSH, ICMP); устройствам админа открыто всё, включая"
say "  локальную сеть; прочее дропается."
say "  Метки Telegram ($TG_MARK): агенту нужен Telegram через сервер AWG."
command -v nft >/dev/null 2>&1 || { say "ОШИБКА: нет nft — apt install nftables"; exit 1; }
ADMIN_ELEMS="$(ipv4_list $ADMIN_IPS $ADMIN_IPS_EXTRA)"
PEER_ELEMS="$(ipv4_list $PEER_HOME_NETS)"
say "  Устройства админа: ${ADMIN_ELEMS:-— (никому, кроме сервера AWG по линку)}"
# SSH снаружи (через проброс на роутере): при SSH_FILTER=1 на порт sshd пускаются
# только локальная сеть, подсети других шлюзов, сервер и адреса из SSH_ALLOW (имена — по последнему
# резолву агента, SSH_ALLOW_RESOLVED; ipv4_list имена отсеивает сам).
SSH_ALLOW_ELEMS="$(ipv4_list $SSH_ALLOW $SSH_ALLOW_RESOLVED)"
SERVER_ELEMS="$(server_ipv4 "$SRC_CONF")"
say "  SSH снаружи (порт $SSH_PORT): фильтр $([ "$SSH_FILTER" = 1 ] && echo включён || echo выключен);"
say "  адреса: ${SSH_ALLOW_ELEMS:-—}; сервер снаружи: ${SERVER_ELEMS:-— (адрес не определён)}"
SSH_JUMP=""
# Только IPv4: таблица inet, а accept'ы в ssh_in — по ip saddr; без nfproto
# v6-пакет доходил бы до drop, и «из локальной сети всегда» ломалось бы.
[ "$SSH_FILTER" = 1 ] && SSH_JUMP="        meta nfproto ipv4 tcp dport $SSH_PORT jump ssh_in"
LAN_ELEMS="$(lan_ipv4 "$WAN_IF")"
say "  локальная сеть (SSH снаружи не фильтруется): ${LAN_ELEMS:-RFC1918 целиком — подсеть $WAN_IF не определена}"
[ -n "$LAN_ELEMS" ] || LAN_ELEMS="10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"
if [ "$MODE" = "plan" ]; then
    say "  would: записать $GUARD_FILE и применить: nft -f $GUARD_FILE"
else
mkdir -p "$GW_ETC"
# Маскарад в АПЛИНК — для самого агента. Локально рождённый пакет выбирает
# исходный адрес до метки в output: берёт адрес локального интерфейса, а после
# перемаршрутизации по метке улетает в аплинк с чужим src — ВПС его отбросит
# (у пира разрешён только адрес аплинка). Маскарад подменяет src на адрес
# аплинка. Без него агент на чистой машине нем: на прежней малине это делало
# чужое правило прежней схемы, и отсутствие своего не было видно.
UPLINK_MASQ=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MASQ="        oifname \"$UPLINK_IF\" masquerade"
# MSS-кламп в аплинк: транзитный TCP из локальной сети в туннель без него
# упирается в MTU туннеля и виснет на больших ответах. Свойство аплинка, не
# прежнего ручного слоя: агенту с его Telegram он тоже полезен.
UPLINK_MSS=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MSS="        oifname \"$UPLINK_IF\" tcp flags syn / syn,rst tcp option maxseg size set rt mtu"
{
cat <<GUARDEOF
#!/usr/sbin/nft -f
# awg-bot (шлюз): обвязка условной маршрутизации и защита машины от туннеля.
# Генерирует routing-gw-setup.sh при каждом старте юнита awg-link-gw.service —
# правки руками перезапишутся. Устройства админа (полный доступ с туннеля):
# ADMIN_IPS из бандла с ВПС плюс ADMIN_IPS_EXTRA из $FW_ENV (awg-bot firewall).
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
    set gh_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $GH_NETS) }
    }
    set admin4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$ADMIN_ELEMS" ] && printf '        elements = { %s }\n' "$ADMIN_ELEMS"
cat <<GUARDEOF
    }
    set peer_nets4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$PEER_ELEMS" ] && printf '        elements = { %s }\n' "$PEER_ELEMS"
cat <<GUARDEOF
    }
    set ssh_allow4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$SSH_ALLOW_ELEMS" ] && printf '        elements = { %s }\n' "$SSH_ALLOW_ELEMS"
cat <<GUARDEOF
    }
    set server4 {
        type ipv4_addr
GUARDEOF
[ -n "$SERVER_ELEMS" ] && printf '        elements = { %s }\n' "$SERVER_ELEMS"
cat <<GUARDEOF
    }
    set lan4 {
        type ipv4_addr
        flags interval
        elements = { $LAN_ELEMS }
    }

    chain input {
        type filter hook input priority filter; policy accept;
        iifname "lo" accept
        ip saddr @tunnel_nets4 jump tunnel_in
$SSH_JUMP
    }
    chain tunnel_in {
        ct state established,related accept
        ip protocol icmp accept
        ip saddr @admin4 accept
        ip saddr $LINK_PEER tcp dport $SSH_PORT accept
        drop
    }
    chain ssh_in {
        ct state established,related accept
        ip saddr @lan4 accept
        ip saddr @peer_nets4 accept
        ip saddr @server4 accept
        ip saddr @ssh_allow4 accept
        drop
    }

    chain forward {
        type filter hook forward priority filter; policy accept;
$UPLINK_MSS
        oifname "$LINK_IF" ct state established,related accept
        iifname "$LINK_IF" ip saddr @admin4 accept
        iifname "$LINK_IF" ip saddr @peer_nets4 accept
        iifname "$LINK_IF" ip daddr @private4 drop
        iifname "$LINK_IF" accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade
$UPLINK_MASQ
    }

    chain output {
        type route hook output priority mangle; policy accept;
        ip daddr @tg_nets4 meta mark set $TG_MARK
        ip daddr @gh_nets4 meta mark set $TG_MARK
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

LAN_IF_PRE=""
if [ "$LAN_MODE" = "1" ] && [ -n "${HOME_SUBNETS%% *}" ]; then
    _r="$(lan_iface_for "${HOME_SUBNETS%% *}")"; LAN_IF_PRE="${_r%% *}"
fi
# ── 3a. чужая политика FORWARD ───────────────────────────────────────────────
# accept в нашей inet-таблице не отменяет DROP в чужой ip filter FORWARD: docker
# (на OMV — норма) ставит её, когда сам включает ip_forward, и транзит из линка,
# из аплинка и из локальной сети умирает молча. Правила — в его же цепочке,
# идемпотентно; снимаются при откате. Только когда политика действительно DROP.
step "3a. Чужая политика FORWARD"
fwd_allow() {                  # $1 = -i|-o, $2 = интерфейс
    [ -n "$2" ] || return 0
    iptables -w -C FORWARD "$1" "$2" -j ACCEPT 2>/dev/null \
        || run "iptables -w -I FORWARD 1 $1 $2 -j ACCEPT"
}
if command -v iptables >/dev/null 2>&1 && iptables -w -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP'; then
    say "  ip filter FORWARD: DROP (docker?) — открываю транзит своим интерфейсам"
    for _i in "$LINK_IF" "${UPLINK_IF:-}" "${LAN_IF_PRE:-}"; do fwd_allow -i "$_i"; fwd_allow -o "$_i"; done
else
    say "  ip filter FORWARD не DROP — ничего не нужно"
fi

# ── 4. автозапуск ────────────────────────────────────────────────────────────
step "4. Автозапуск"
SELF="$(install_self)"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк до сервера AWG и изоляция клиентов (шлюз)
# Зависимости от docker БОЛЬШЕ НЕТ, и это не уборка. Она осталась с тех пор,
# когда линк поднимался утилитами из образа Amnezia; сейчас его поднимает
# хостовой awg-quick. С Requires=docker.service не стартовавший (или снесённый)
# docker уносил за собой весь обвяз шлюза — молча, и обнаруживалось это как
# «интернета за шлюзом нет» уже со стороны ВПС.
After=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}
Wants=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый бандл с ВПС.
Environment=CLIENT_SUBNET=$CLIENT_SUBNET
Environment=LINK_IF=$LINK_IF
# Устройства админа (полный доступ с туннеля) — из бандла с ВПС. Добавленное
# на самом шлюзе (awg-bot firewall allow) — в $FW_ENV.
Environment="ADMIN_IPS=$ADMIN_IPS"
Environment=GATEWAY_PUBKEY=$GATEWAY_PUBKEY
Environment=GATEWAY_PREV_PUBKEY=$GATEWAY_PREV_PUBKEY
# Локальная сеть без VPN (концепт «локальная сеть»): флаг, подсети, резолвер ВПС.
Environment=LAN_MODE=$LAN_MODE
Environment="HOME_SUBNETS=$HOME_SUBNETS"
Environment=RESOLVER=$RESOLVER
Environment="PEER_HOME_NETS=$PEER_HOME_NETS"
# Канал до ВПС внутри линка (концепт «канал линка»): включается бандлом и
# только им. Агент читает эти строки из юнита — без перевыпуска конфигурации
# он никуда не ходит.
Environment=LINK_CHANNEL=$LINK_CHANNEL
Environment=LINK_CHANNEL_PORT=$LINK_CHANNEL_PORT
EnvironmentFile=-$FW_ENV
# Зовём этот же скрипт: он идемпотентен, источник истины один.
ExecStart=$SELF --apply $HOST_CONF_DIR/$LINK_IF.conf
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITEOF
# 0600: прежние выпуски закрепляли в юните UPLINK_B64 с приватным ключом
# аплинка и оставляли 0644 — любой локальный пользователь малины (OMV, шары)
# мог его прочитать. Секрета в юните больше нет, права держим строгими:
# юнит читает только systemd.
chmod 0600 "$UNIT"
run "systemctl daemon-reload"
run "systemctl enable awg-link-gw.service"
# ── 5. локальная сеть: «за шлюзом — без VPN» (концепт «локальная сеть», функция A) ─
# Роутер заворачивает весь трафик локальной сети на малину, малина делит его
# сама: заблокированное — по метке аплинка в туннель, остальное — напрямую.
# Резолвер dnsmasq на LAN-адресе с апстримом через аплинк наполняет наборы
# доменами, скрипт списков — подсетями из фидов. Владелец всего ниже — этот
# скрипт: любая ручная правка перезапишется при следующем применении.
#
# Две таблицы nft — не случайность: awg_gw_guard перевыставляется атомарно
# (delete table), а наборы awg_home наполняет dnsmasq на лету, и терять их при
# каждом реассерте нельзя — таблица создаётся БЕЗ delete, меняются только
# цепочки. Метка — та же, что у агента: смысл один («в аплинк»), правило и
# маршрут уже ставит PostUp аплинка, а чинит агент.
step "5. Локальная сеть без VPN"
LAN_ERROR=""
lan_fail() { LAN_ERROR="$1"; say "  ОШИБКА: $1"; return 1; }
lan_apply() {                  # отказ — return 1 с LAN_ERROR: юнит уже включён, ронять скрипт нельзя
    _net="${HOME_SUBNETS%% *}"
    [ -n "$_net" ] || { lan_fail "LAN_MODE=1 без локальной подсети — задай её в боте и перевыпусти конфигурацию"; return 1; }
    _r="$(lan_iface_for "$_net")"; LAN_IF="${_r%% *}"; LAN_ADDR="${_r#* }"; [ "$LAN_ADDR" = "$_r" ] && LAN_ADDR=""
    [ -n "$LAN_IF" ] || { lan_fail "на этой машине нет адреса из подсети $_net — не та подсеть или не та машина"; return 1; }
    say "  локальная сеть $_net: интерфейс $LAN_IF, адрес шлюза $LAN_ADDR"
    [ -n "$UPLINK_IF" ] || { lan_fail "аплинк не известен — режим без VPN без него не работает"; return 1; }
    # :53 занят кем-то, кроме dnsmasq (systemd-resolved на 0.0.0.0, Pi-hole, второй
    # dnsmasq в контейнере)? Честный отказ ДО установки пакета: postinst dnsmasq
    # стартует демон, тот не сможет забиндить порт, и apt-get упадёт посреди дела.
    _busy="$(port53_busy)"
    [ -z "$_busy" ] || { lan_fail "порт 53 занят: $_busy — освободи его (systemd-resolved на 0.0.0.0, Pi-hole?) и примени ещё раз"; return 1; }
    lan_migrate_manual
    _upstream="${RESOLVER:-1.1.1.1}"
    [ -n "$RESOLVER" ] || say "  резолвера сервера AWG нет — апстрим 1.1.1.1 через аплинк (без защиты от DoH и общего кэша)"
    _dn_changed=0
    run "mkdir -p $DNSMASQ_D $LAN_DUMP"
    # ── конфиги dnsmasq ДО установки пакета: первый старт демона сразу с нашим
    # listen-address, а не wildcard на :53. Временные файлы — вне conf-dir.
    _tmp="$(mktemp "$LAN_DUMP/dnsmasq.XXXXXX")"
    {
        printf '# awg-bot (шлюз): резолвер локальной сети без VPN. Владелец — routing-gw-setup.sh.\n'
        # bind-dynamic: с bind-interfaces демон падает, пока аплинк не поднялся.
        # Однократные ключи (bind-*, cache-size) — только если их нет в других файлах.
        dn_set_elsewhere '^(bind-interfaces|bind-dynamic)$' || printf 'bind-dynamic\n'
        printf 'listen-address=127.0.0.1,%s\n' "$LAN_ADDR"
        printf 'no-resolv\nno-hosts\n'
        # апстрим ЧЕРЕЗ АПЛИНК: без @iface запрос ушёл бы линком с адресом линка
        printf 'server=%s@%s\n' "$_upstream" "$UPLINK_IF"
        dn_set_elsewhere '^cache-size=' || printf 'cache-size=10000\n'
        printf 'stop-dns-rebind\nrebind-localhost-ok\n'
        # IPv6 в квартире выключить с малины нельзя (RA раздаёт роутер, networkd
        # OMV включает v6 на интерфейсе при каждом Apply) — зато AAAA можно не
        # отдавать: без адреса v6 трафик мимо туннеля не уйдёт. Пакета ещё нет
        # (первое применение: конфиг пишется ДО apt) — ключ кладём: apt ставит
        # dnsmasq ≥ 2.89 (Debian 12+), а без него первый же старт раздавал бы
        # AAAA до следующего реассерта — днями
        if ! command -v dnsmasq >/dev/null 2>&1 || dnsmasq --help 2>&1 | grep -q 'filter-AAAA'; then
            printf 'filter-AAAA\n'
        fi
    } > "$_tmp"
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-base.conf"; then rm -f "$_tmp"; else
        run "install -m 0644 $_tmp $DNSMASQ_D/awg-gw-base.conf"; rm -f "$_tmp"; _dn_changed=1; fi
    _tmp="$(mktemp "$LAN_DUMP/dnsmasq.XXXXXX")"
    cat > "$_tmp" <<'DOHEOF'
# awg-bot (шлюз): DoH мимо резолвера — иначе адрес не попадает в набор и
# заблокированное идёт напрямую («часть сайтов через раз»). Тот же список,
# что у резолвера ВПС. Канареечный домен Firefox: NXDOMAIN выключает DoH.
address=/use-application-dns.net/
address=/cloudflare-dns.com/
address=/chrome.cloudflare-dns.com/
address=/mozilla.cloudflare-dns.com/
address=/one.one.one.one/
address=/dns.google/
address=/dns.quad9.net/
address=/dns.adguard-dns.com/
address=/doh.opendns.com/
address=/dns.nextdns.io/
address=/firefox.dns.nextdns.io/
DOHEOF
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-doh.conf"; then rm -f "$_tmp"; else
        run "install -m 0644 $_tmp $DNSMASQ_D/awg-gw-doh.conf"; rm -f "$_tmp"; _dn_changed=1; fi
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] && continue
        # awg-bot restore кладёт личные списки из копии сюда, когда режим на
        # машине ещё не применён: подхватываем их при первом включении
        if [ -f "$LAN_DUMP/restore/$_f" ]; then
            run "mv -f $LAN_DUMP/restore/$_f $DNSMASQ_D/$_f"
        else
            run "printf '# awg-bot (шлюз): личный список — awg-bot lan add/ru/del\\n' > $DNSMASQ_D/$_f"
        fi
        _dn_changed=1
    done
    # /etc/default/dnsmasq НЕ трогаем. Прежняя строка DNSMASQ_EXCEPT=lo init-скрипт
    # Debian превращает в except-interface=lo, а тот по man перекрывает
    # listen-address: dnsmasq переставал слушать 127.0.0.1, и проверка апстрима
    # была вечно красной. А на чистой малине файл, созданный до установки
    # пакета, — его conffile: dpkg без терминала падает на вопросе о нём.
    # Свою прежнюю строку, если осталась, убираем.
    if grep -qs '^# awg-bot: резолвер только для локальной сети' "$DNSMASQ_DEFAULT"; then
        _tmp="$(mktemp)"
        grep -v -e '^# awg-bot: резолвер только для локальной сети' -e '^DNSMASQ_EXCEPT=lo$' \
            "$DNSMASQ_DEFAULT" > "$_tmp" && run "install -m 0644 $_tmp $DNSMASQ_DEFAULT"
        rm -f "$_tmp"; _dn_changed=1
    fi
    # Оверрайд юнита: перезапуск при отказе; старт ПОСЛЕ аплинка — апстрим
    # server=…@<аплинк> привязывается к интерфейсу, и dnsmasq, стартовавший
    # раньше awg0 (так и было на живых малинах), оставался без апстрима до
    # ручного рестарта; хук resolvconf Debian выключен — он вписал бы 127.0.0.1
    # системным резолвером малины, и её собственный DNS (агент, awg-quick, apt)
    # зависел бы от аплинка. Хук снимаем, только если он в юните и есть.
    _ovr_want="$(mktemp)"
    {
        printf '# awg-bot (шлюз): оверрайд dnsmasq под локальную сеть без VPN.\n'
        if [ -n "${UPLINK_IF:-}" ]; then
            printf '[Unit]\nAfter=awg-quick@%s.service\nWants=awg-quick@%s.service\n' "$UPLINK_IF" "$UPLINK_IF"
        fi
        printf '[Service]\nRestart=on-failure\nRestartSec=5\n'
        if systemctl cat dnsmasq.service 2>/dev/null | grep -q 'start-resolvconf'; then
            printf 'ExecStartPost=\nExecStop=\n'
        fi
    } > "$_ovr_want"
    if ! cmp -s "$_ovr_want" "$DNSMASQ_OVR"; then
        run "mkdir -p $(dirname "$DNSMASQ_OVR")"
        run "install -m 0644 $_ovr_want $DNSMASQ_OVR"
        run "systemctl daemon-reload"; _dn_changed=1
    fi
    rm -f "$_ovr_want"
    # dnsmasq и dig (наполнение набора после ручного добавления домена)
    if ! command -v dnsmasq >/dev/null 2>&1 || ! command -v dig >/dev/null 2>&1; then
        say "  ставлю dnsmasq и dnsutils"
        run "apt-get update -q >/dev/null 2>&1 || true"
        # confdef/confold — на вопрос о конфигах отвечать самим, без терминала
        # (юнит его не даёт); Lock::Timeout — OMV мог держать dpkg своим apt
        run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dnsmasq dnsutils" \
            || { lan_fail "dnsmasq не установился — смотри вывод apt выше"; return 1; }
        run "touch $DNSMASQ_MARK"
        _dn_changed=1
    fi
    # ── sysctl: rp_filter loose на LAN
    run "printf 'net.ipv4.conf.$(printf '%s' "$LAN_IF" | tr . /).rp_filter = 2\\n' > $LAN_SYSCTL"
    run "sysctl -qw net.ipv4.conf.$(printf '%s' "$LAN_IF" | tr . /).rp_filter=2 || true"
    # ── таблица awg_home: наборы без delete (переживают реассерт), цепочки заново
    {
        cat <<HOMEEOF
# awg-bot (шлюз): локальная сеть без VPN. Владелец — routing-gw-setup.sh.
# Без «delete table»: наборы наполняет dnsmasq на лету, их нельзя терять при реассерте.
table $HOME_TABLE {
    set lan_vpn4      { type ipv4_addr; flags interval; auto-merge; }
    set lan_vpn_nets4 { type ipv4_addr; flags interval; auto-merge; }
    set lan_ru4       { type ipv4_addr; flags interval; auto-merge; }
    chain prerouting  { type filter hook prerouting priority mangle; policy accept; }
    chain input       { type filter hook input priority filter; policy accept; }
    chain forward     { type filter hook forward priority filter; policy accept; }
    chain postrouting { type nat hook postrouting priority srcnat; policy accept; }
}
flush chain $HOME_TABLE prerouting
flush chain $HOME_TABLE input
flush chain $HOME_TABLE forward
flush chain $HOME_TABLE postrouting
# из линка и аплинка — мимо маркировки; исключения ВЫШЕ метки (accept обрывает обход)
add rule $HOME_TABLE prerouting iifname != "$LAN_IF" accept
# счётчик для агента: заворот с роутера — только unicast на MAC малины (до любых
# вердиктов); mDNS/SSDP/DHCP-бродкасты фонят круглые сутки и считаться не должны
add rule $HOME_TABLE prerouting meta pkttype host ip daddr != $_net counter
add rule $HOME_TABLE prerouting ip daddr @lan_ru4 accept
add rule $HOME_TABLE prerouting ip daddr @lan_vpn_nets4 meta mark set $TG_MARK
add rule $HOME_TABLE prerouting ip daddr @lan_vpn4 meta mark set $TG_MARK
# счётчик для агента: DNS с роутера (раздаёт ли DHCP адрес малины)
add rule $HOME_TABLE input iifname "$LAN_IF" udp dport 53 counter
# DoT мимо резолвера — как на ВПС для клиентов
add rule $HOME_TABLE forward iifname "$LAN_IF" tcp dport 853 drop
# ответ на прямой трафик должен вернуться на малину, а не на клиента мимо неё
add rule $HOME_TABLE postrouting ip saddr $_net oifname "$LAN_IF" masquerade
HOMEEOF
    } > "$HOME_FILE.tmp"
    chmod 0644 "$HOME_FILE.tmp"
    nft -c -f "$HOME_FILE.tmp" || { lan_fail "nft отклонил таблицу локальной сети — $HOME_FILE.tmp"; return 1; }
    mv "$HOME_FILE.tmp" "$HOME_FILE"
    run "nft -f $HOME_FILE" || { lan_fail "таблица локальной сети не применилась"; return 1; }
    # слепки наборов — до фидов: первая минута после загрузки не остаётся без списков
    for _s in lan_vpn4 lan_vpn_nets4; do
        [ -s "$LAN_DUMP/$_s.nft" ] && run "nft -f $LAN_DUMP/$_s.nft 2>/dev/null || true"
    done
    write_lan_scripts
    # ── сервисы соседних сетей (концепт «сервисы соседних сетей»): обзор своей
    # сети — avahi-browse при живом avahi-daemon (сам демон не ставим: он начал
    # бы объявлять малину); соседей нет — записи соседей снять
    if [ -n "${PEER_HOME_NETS:-}" ]; then
        # без канала линка записи никуда не уедут — пакет не нужен; списки apt
        # на малине могут быть старыми (404 на зеркале) — сначала update
        if [ "${LINK_CHANNEL:-0}" = "1" ] && systemctl is-active --quiet avahi-daemon 2>/dev/null \
                && ! command -v avahi-browse >/dev/null 2>&1; then
            say "  ставлю avahi-utils (обзор SMB-серверов этой подсети для подсетей других шлюзов)"
            run "apt-get update -q >/dev/null 2>&1 || true"
            run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold avahi-utils" \
                || say "  avahi-utils не установился — SMB-серверы этой подсети не видны из подсетей других шлюзов, остальное работает"
        fi
    elif [ -f "$PEER_SVC_CONF" ]; then
        run "rm -f $PEER_SVC_CONF"; _dn_changed=1
    fi
    if [ "$_dn_changed" = "1" ] || ! systemctl is-active --quiet dnsmasq; then
        run "systemctl enable dnsmasq 2>/dev/null || true"
        run "systemctl restart dnsmasq" || { lan_fail "dnsmasq не запустился: journalctl -u dnsmasq -e"; return 1; }
    else
        say "  dnsmasq: конфиги не изменились"
    fi
    say "  списки — обновляет агент (первый раз через несколько минут); руками: $LAN_LISTS"
    return 0
}
if [ "$LAN_MODE" = "1" ]; then
    # В условии if set -e внутри функции не действует — потому каждый шаг с
    # последствиями сам решает, продолжать ли (|| return 1). Отказ — в статус,
    # его покажет агент; остальная обвязка стоит, юнит не уходит в цикл рестартов.
    if lan_apply; then :; else
        say "  локальная сеть без VPN НЕ применена: ${LAN_ERROR:-см. выше}"
    fi
else
    if [ -f "$HOME_FILE" ] || [ -f "$DNSMASQ_D/awg-gw-base.conf" ]; then
        say "  выключено — снимаю своё"
        lan_remove
    else
        say "  выключено"
    fi
fi

write_status "up"
step "Проверка"
say "  awg show $LINK_IF                            # есть ли хендшейк"
say "  ip -br addr show $LINK_IF"
say "  nft list table $GUARD_TABLE                  # вся обвязка одним взглядом"
say ""
say "Хендшейк появится, когда сервер AWG ответит; статус — в панели агента."
