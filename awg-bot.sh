#!/usr/bin/env bash
#
# awg-bot.sh — единый инструмент управления УЖЕ УСТАНОВЛЕННЫМ ботом awg-bot.
#
# Живёт в каталоге установки (/opt/awg-bot), доступен как `awg-bot` (симлинк в
# /usr/local/bin кладёт внешний установщик). Установку НЕ выполняет — greenfield
# (mkdir FHS + распаковка кода + симлинк) делает внешний bootstrap-установщик,
# который затем передаёт сюда управление: `awg-bot reconfigure --first-run`.
#
# Владеет всей повторно используемой логикой рантайма: сборка venv, генерация
# systemd-юнита, config.validate(), визард топологии/секретов. Их переиспользуют
# и первичная настройка (reconfigure --first-run из bootstrap), и обновление.
#
# Глаголы:
#   reconfigure [--first-run] [--cleanup <inst> <tgz>]
#                          мастер конфигурации (топология + секреты). --first-run —
#                          первичный прогон из установщика (собрать venv, юнит,
#                          enable+start, напечатать карту, подчистить установщик).
#   update [<tgz>]         обновить код/зависимости/юнит из архива (по умолчанию —
#                          awg-bot-update.tgz рядом; conf/env/данные не трогаются,
#                          если явно не согласиться на их удаление).
#   uninstall              снять сервис (код всегда; данные/секреты — по согласию).
#   backup                 снимок состояния (БД + conf + env) → tar.gz.
#   restore [<tgz>]        восстановить состояние из снимка (по умолчанию — свежий).
#   status                 состояние сервиса и пути.
#   logs                   журнал сервиса (follow).

set -euo pipefail

# ── пути (FHS) ───────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/awg-bot"
ETC_DIR="/etc/awg-bot"
CONF_DIR="$ETC_DIR/conf"
ENV_FILE="$ETC_DIR/env"
DATA_DIR="/var/lib/awg-bot"
BACKUP_DIR="$DATA_DIR/backups"
UNIT_PATH="/etc/systemd/system/awg-bot.service"
SERVICE="awg-bot"
SELF_LINK="/usr/local/bin/awg-bot"

# Реальный путь к этому скрипту (для update-по-умолчанию и uninstall self-removal).
SELF_PATH="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)/$(basename "$(readlink -f "${BASH_SOURCE[0]}")")"
SELF_DIR="$(dirname "$SELF_PATH")"

# ── лог/утилиты ──────────────────────────────────────────────────────────────
c_info=$'\033[0;36m'; c_ok=$'\033[0;32m'; c_warn=$'\033[0;33m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log()  { printf '%s[awg-bot]%s %s\n' "$c_info" "$c_off" "$*"; }
ok()   { printf '%s[awg-bot]%s %s\n' "$c_ok"   "$c_off" "$*"; }
warn() { printf '%s[awg-bot:!]%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s[awg-bot:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

ask() {  # ask VAR "prompt" "default"  — пустой ввод (Enter) берёт default
    local __v="$1" __p="$2" __d="${3:-}" __a
    if [[ -n "$__d" ]]; then read -r -p "$__p [$__d]: " __a; __a="${__a:-$__d}"
    else read -r -p "$__p: " __a; fi
    printf -v "$__v" '%s' "$__a"
}
ask_masked() {  # ask_masked VAR "prompt" — ввод с маской: видно ДЛИНУ (****), не символы.
    # Вставка из буфера работает: read -N1 читает вклейку посимвольно, на каждый
    # символ печатаем '*' — сразу видно, что вставилось и сколько. Backspace
    # (\x7f/\x08) стирает. Enter завершает.
    local __v="$1" __p="$2" __s="" __ch
    printf '%s: ' "$__p" > /dev/tty
    while IFS= read -rs -N1 __ch < /dev/tty; do
        case "$__ch" in
            $'\n'|$'\r') break ;;
            $'\x7f'|$'\x08')
                if [[ -n "$__s" ]]; then __s="${__s%?}"; printf '\b \b' > /dev/tty; fi ;;
            *) __s+="$__ch"; printf '*' > /dev/tty ;;
        esac
    done
    printf '\n' > /dev/tty
    printf -v "$__v" '%s' "$__s"
}
confirm() {  # confirm "prompt" "default(y/n)" → 0/1  (Enter = default)
    local p="$1" d="${2:-n}" a hint="[y/N]"
    [[ "$d" == "y" ]] && hint="[Y/n]"
    read -r -p "$p $hint: " a; a="${a:-$d}"
    [[ "${a,,}" == "y" ]]
}
require_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root: sudo awg-bot ${VERB:-}"; }
require_installed() { [[ -x "$INSTALL_DIR/venv/bin/python" ]] || die "awg-bot не установлен в $INSTALL_DIR (сначала внешний установщик)"; }

# Заменить значение КОНКРЕТНОГО ключа yaml, сохранив отступ и комментарии.
yaml_set() {  # yaml_set FILE KEY VALUE
    local file="$1" key="$2" val="$3"
    grep -qE "^[[:space:]]*${key}:" "$file" || die "в $file нет ключа '$key' — шаблон конфига не тот?"
    local esc="${val//\\/\\\\}"; esc="${esc//&/\\&}"
    sed -i -E "s|^([[:space:]]*)${key}:.*|\1${key}: ${esc}|" "$file"
}
yaml_get() {  # yaml_get FILE KEY → печатает значение (снимает кавычки и хвостовой # коммент)
    local file="$1" key="$2" line
    line="$(grep -E "^[[:space:]]*${key}:" "$file" 2>/dev/null | head -n1)" || return 0
    line="${line#*:}"                       # после ключа
    line="${line%%#*}"                       # срезать хвостовой комментарий
    line="${line#"${line%%[![:space:]]*}"}"  # ltrim
    line="${line%"${line##*[![:space:]]}"}"  # rtrim
    line="${line#\"}"; line="${line%\"}"     # снять кавычки
    printf '%s' "$line"
}
env_get() { [[ -f "$ENV_FILE" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$ENV_FILE" | head -n1 || true; }
env_set() {  # env_set KEY VALUE (сохраняет прочие строки/комментарии)
    local key="$1" val="$2" tmp; mkdir -p "$ETC_DIR"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    tmp="$(mktemp)"; local seen=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^${key}= ]]; then echo "${key}=${val}" >> "$tmp"; seen=1
        else echo "$line" >> "$tmp"; fi
    done < "$ENV_FILE"
    [[ "$seen" -eq 0 ]] && echo "${key}=${val}" >> "$tmp"
    mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

# ── Python (нужен 3.12+: StrEnum) ────────────────────────────────────────────
PYBIN=""
detect_python() {
    local cand
    for cand in python3.12 python3.13 python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)' 2>/dev/null; then
            PYBIN="$(command -v "$cand")"; return 0
        fi
    done
    return 1
}
ensure_python() {
    if detect_python; then log "Python: $PYBIN ($("$PYBIN" -V 2>&1))"; return; fi
    warn "не найден Python 3.12+ (нужен для StrEnum)."
    if command -v apt-get >/dev/null 2>&1 && confirm "Установить python3.12 через apt?" y; then
        apt-get update
        if ! apt-get install -y python3.12 python3.12-venv 2>/dev/null; then
            log "нет в штатных репозиториях — подключаю deadsnakes PPA…"
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa; apt-get update
            apt-get install -y python3.12 python3.12-venv
        fi
        detect_python || die "python3.12 так и не появился — поставьте вручную."
        log "Python: $PYBIN"
    else
        die "поставьте Python 3.12+ вручную и повторите."
    fi
}

# ── apply-runtime: venv + юнит + валидация (общее для first-run и update) ─────
build_venv() {
    if [[ ! -x "$INSTALL_DIR/venv/bin/python" ]]; then
        log "создаю venv ($PYBIN)…"; "$PYBIN" -m venv "$INSTALL_DIR/venv"
    fi
    log "ставлю зависимости (pip install -r requirements.txt)…"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$INSTALL_DIR/requirements.txt"
}
validate_config() {
    log "проверяю конфигурацию (config.validate)…"
    if ( cd "$INSTALL_DIR" && AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
         ./venv/bin/python -c "import awgbot.core.config as c; c.validate()" ); then
        ok "конфигурация валидна."
    else
        die "config.validate() не прошёл — исправьте значения в $CONF_DIR / $ENV_FILE и повторите."
    fi
}
install_unit() {
    log "ставлю systemd-юнит $UNIT_PATH…"
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=AmneziaWG Telegram bot
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
# Бот работает от root: нужен docker exec + управление iptables в контейнере.
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment=AWG_BOT_CONF_DIR=$CONF_DIR
Environment=AWG_BOT_DATA_DIR=$DATA_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m awgbot
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$UNIT_PATH"; systemctl daemon-reload
}

# ── визард: конфиг (топология) + секреты ─────────────────────────────────────
seed_conf() {
    mkdir -p "$CONF_DIR"; local f base copied=0
    for f in "$INSTALL_DIR"/conf/*.yaml; do
        base="$(basename "$f")"
        if [[ ! -f "$CONF_DIR/$base" ]]; then
            cp "$f" "$CONF_DIR/$base"; copied=1
        fi
    done
    [[ "$copied" -eq 1 ]] && log "шаблоны конфига скопированы в $CONF_DIR" \
                          || log "конфиг в $CONF_DIR уже есть — существующие файлы не трогаю"
}
_detect_awg_container() {  # печатает имена, ВНУТРИ которых отвечает `awg show`
    local names ordered n
    names="$(docker ps --format '{{.Names}}' 2>/dev/null)" || return 0
    ordered="$( { printf '%s\n' "$names" | grep -E '^amnezia-awg';
                  printf '%s\n' "$names" | grep -vE '^amnezia-awg'; } 2>/dev/null )"
    for n in $ordered; do
        docker exec "$n" awg show >/dev/null 2>&1 && printf '%s\n' "$n"
    done
}

configure_topology() {
    local app="$CONF_DIR/app.yaml"
    [[ -f "$app" ]] || die "нет $app — сначала seed_conf"
    echo; log "─── Топология (пишу в $CONF_DIR, потом можно править руками) ───"
    # Бот co-located с awg → внешний IP этого хоста можно определить самим:
    # адрес, с которого хост выходит в интернет (ip route get) — как подсказку.
    # Юзер подтверждает Enter'ом, правит (хост за NAT) или вводит доменное имя:
    # в ссылку идёт как есть (hostName), WireGuard резолвит DNS при коннекте.
    # Приоритет дефолта: текущее значение из конфига (reconfigure не должен молча
    # затирать заданный домен/IP на автодетект) → автодетект IP (первичная установка).
    local detected host port cur_host
    cur_host="$(yaml_get "$app" server_host)"
    detected="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' || true)"
    [[ -n "$cur_host" ]] && detected="$cur_host"
    while :; do
        ask host "Внешний IP ИЛИ доменное имя этого сервера (server_host)" "${detected}"
        [[ -n "$host" ]] && break; warn "обязателен"
    done
    yaml_set "$app" server_host "\"$host\""

    # Имя сервера — это description в vpn:// (видно в приложении Amnezia у клиента).
    # Одностороннее: попадает в НОВЫЕ ссылки; уже импортированные у клиентов не
    # меняются. При reconfigure текущее имя предлагается как дефолт (Enter — оставить).
    local srv_name cur_name
    cur_name="$(yaml_get "$app" server_name)"; [[ -n "$cur_name" ]] || cur_name="Сервер 1"
    ask srv_name "Имя сервера в ссылках (видно клиенту в приложении)" "$cur_name"
    yaml_set "$app" server_name "\"$srv_name\""

    # Порт awg и VPN-подсеть почти всегда УЖЕ настроены в живом контейнере awg —
    # подсасываем их из awg0.conf (detect_topology), юзер подтверждает Enter'ом.
    # Контейнер ищем по факту живости `awg show` (имя amnezia-awg* лишь первым).
    # Приоритет дефолта: текущее значение из конфига → автодетект из живого awg0.conf
    # → хардкод (только первичная установка без контейнера). Так reconfigure с Enter
    # сохраняет уже настроенные порт/подсеть, не сбрасывая их.
    # Порт и подсеть: если живой awg-контейнер дал их автодетектом — берём молча
    # (детект точен, лишний Enter не нужен; поправить можно потом в reconfigure).
    # Спрашиваем ТОЛЬКО то, что детект не дал: нет контейнера / не прочитался конфиг.
    # Порт у докерной Amnezia случайный — хардкод-дефолта не бывает; подсеть штатно
    # 10.8.1. При reconfigure текущее значение из конфига в приоритете над детектом.
    local cont det="" cur_port cur_prefix d_port="" d_prefix="" d_cidr=""
    cur_port="$(yaml_get "$app" server_port)"
    cur_prefix="$(yaml_get "$app" subnet_prefix)"
    cont="$(_detect_awg_container | head -n1)"
    if [[ -n "$cont" ]]; then
        det="$(cd "$INSTALL_DIR" && AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
            ./venv/bin/python -c "
from awgbot.infra import awg
t = awg.detect_topology('$cont')
print('%s|%s|%s' % (t['listen_port'] or '', t['subnet_prefix'] or '', t['subnet_cidr'] or ''))
" 2>/dev/null || true)"
        d_port="${det%%|*}"; local d_rest="${det#*|}"
        d_prefix="${d_rest%%|*}"; d_cidr="${d_rest#*|}"
    else
        warn "живой awg-контейнер не найден — бот ставится до awg? Укажу порт/подсеть вручную."
    fi

    # ── Порт ──────────────────────────────────────────────────────────────────
    local port
    if [[ -n "$cur_port" ]]; then
        port="$cur_port"                     # reconfigure: конфиг в приоритете
        [[ -n "$d_port" && "$d_port" != "$cur_port" ]] && \
            warn "в контейнере порт $d_port, в конфиге $cur_port — оставляю конфиг (меняй тут, если надо)."
    elif [[ -n "$d_port" ]]; then
        port="$d_port"; ok "порт из контейнера: $port"
    else
        while :; do
            ask port "Порт awg-сервера (server_port) — у докерной Amnezia он случайный, см. awg0.conf/маппинг"
            [[ "$port" =~ ^[0-9]+$ ]] && break; warn "порт — число"
        done
    fi
    yaml_set "$app" server_port "$port"

    # ── Подсеть (первые три октета) ───────────────────────────────────────────
    local prefix
    if [[ -n "$cur_prefix" ]]; then
        prefix="$cur_prefix"
        [[ -n "$d_prefix" && "$d_prefix" != "$cur_prefix" ]] && \
            warn "в контейнере подсеть ${d_prefix}.0, в конфиге ${cur_prefix}.0 — оставляю конфиг."
    elif [[ -n "$d_prefix" ]]; then
        prefix="$d_prefix"; ok "подсеть из контейнера: ${d_cidr:-${prefix}.0/24}"
    else
        while :; do
            ask prefix "VPN-подсеть — первые три октета (subnet_prefix)" "10.8.1"
            [[ "$prefix" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && break; warn "префикс вида X.Y.Z (три октета)"
        done
    fi
    yaml_set "$app" subnet_prefix "\"$prefix\""
    yaml_set "$app" subnet_cidr "\"${prefix}.0/24\""
    ok "конфиг записан (порт $port, подсеть ${prefix}.0/24)."
}

setup_secrets() {
    local cur_token cur_admin token admin
    mkdir -p "$ETC_DIR"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    cur_token="$(env_get BOT_TOKEN)"; cur_admin="$(env_get ADMIN_ID)"
    if [[ -n "$cur_token" ]] && ! confirm "BOT_TOKEN уже задан — заменить?" n; then token="$cur_token"
    else while :; do ask_masked token "Токен бота (от @BotFather)"; [[ -n "$token" ]] && break; warn "пусто"; done; fi
    if [[ -n "$cur_admin" ]] && ! confirm "ADMIN_ID уже задан ($cur_admin) — заменить?" n; then admin="$cur_admin"
    else while :; do ask admin "Telegram ID администратора (число, у @userinfobot)"; [[ "$admin" =~ ^[0-9]+$ ]] && break; warn "должно быть числом"; done; fi
    env_set BOT_TOKEN "$token"; env_set ADMIN_ID "$admin"
    ok "секреты записаны в $ENV_FILE (600)."
    if confirm "Настроить шифрование резервных копий (рекомендуется)?" y; then
        ( cd "$INSTALL_DIR" \
            && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
            && ./venv/bin/python -m tools.manage_secrets ) || warn "manage_secrets прерван — можно запустить позже."
    fi
}

setup_email_resume() {
    # Аварийный email-выход из приостановки. Клиент, заперевшийся в паузе
    # (Telegram только через этот VPN), шлёт одноразовый код письмом — бот
    # опрашивает IMAP исходяще (портов не открываем) и снимает паузу.
    # Всё опционально; выключение стирает настройки (фича засыпает).
    local app="$CONF_DIR/email.yaml"
    local cur_login imap smtp imap_port smtp_port alias login pass poll domain
    cur_login="$(env_get EMAIL_RESUME_LOGIN)"
    echo
    if [[ -n "$cur_login" ]]; then
        log "Email-выход уже настроен (ящик: $cur_login)."
        if confirm "Отключить email-выход и стереть настройки?" n; then
            env_set EMAIL_RESUME_LOGIN ""; env_set EMAIL_RESUME_PASSWORD ""
            yaml_set "$app" imap_host "\"\""; yaml_set "$app" smtp_host "\"\""
            yaml_set "$app" resume_address "\"\""
            ok "email-выход отключён."
            return
        fi
        confirm "Изменить параметры email-выхода?" n || return
    else
        confirm "Настроить аварийный email-выход из приостановки?" n || return
    fi

    while :; do ask login "Адрес ящика (напр. box@icloud.com)"; [[ "$login" == *@*.* ]] && break; warn "нужен корректный e-mail"; done
    # IMAP/SMTP выводим из домена для известных провайдеров; иначе спросим вручную.
    domain="${login##*@}"
    imap=""; smtp=""; imap_port=993; smtp_port=587
    case "$domain" in
        icloud.com) imap="imap.mail.me.com"; smtp="smtp.mail.me.com" ;;
    esac
    if [[ -n "$imap" ]]; then
        log "Провайдер распознан ($domain): IMAP $imap:$imap_port, SMTP $smtp:$smtp_port."
    else
        log "Домен $domain незнаком — укажи серверы вручную."
        while :; do ask imap "IMAP-сервер (приём)"; [[ -n "$imap" ]] && break; warn "пусто"; done
        ask imap_port "Порт IMAP (SSL/TLS)" "993"
        while :; do ask smtp "SMTP-сервер (ответ об успехе)"; [[ -n "$smtp" ]] && break; warn "пусто"; done
        ask smtp_port "Порт SMTP (STARTTLS)" "587"
    fi
    log "Пароль ящика: для iCloud это app-specific password (account.apple.com → App-Specific Passwords)."
    while :; do ask_masked pass "Пароль ящика"; [[ -n "$pass" ]] && break; warn "пусто"; done
    # Алиас опционален: если у ящика есть алиас-адрес для писем от клиентов —
    # укажи его; пусто (Enter) → клиенты шлют на сам адрес ящика ($login).
    ask alias "Адрес-алиас для писем от клиентов (Enter — сам адрес ящика)" ""
    [[ -z "$alias" ]] && alias="$login"
    ask poll "Интервал опроса, сек (не менее 60)" "60"
    [[ "$poll" =~ ^[0-9]+$ ]] && (( poll >= 60 )) || { warn "интервал <60 или не число — ставлю 60"; poll=60; }

    yaml_set "$app" imap_host "\"$imap\""
    yaml_set "$app" imap_port "$imap_port"
    yaml_set "$app" smtp_host "\"$smtp\""
    yaml_set "$app" smtp_port "$smtp_port"
    yaml_set "$app" resume_address "\"$alias\""
    yaml_set "$app" poll_interval_sec "$poll"
    env_set EMAIL_RESUME_LOGIN "$login"
    env_set EMAIL_RESUME_PASSWORD "$pass"
    ok "email-выход настроен (ящик $login, письма на $alias, опрос ${poll}с)."
}
optional_steps() {
    echo; log "─── Опционально ───"
    if [[ -f "$INSTALL_DIR/install/harden_firewall.sh" ]] \
       && confirm "Настроить firewall хоста бота (harden_firewall.sh)?" n; then
        bash "$INSTALL_DIR/install/harden_firewall.sh" || warn "harden_firewall.sh прерван."
    fi
}

print_map() {  # print_map "active"|"failed"
    echo
    ok "══════════════════════════════════════════════════════════════════"
    if [[ "${1:-}" == "active" ]]; then
        ok " awg-bot установлен и запущен."
    else
        warn " awg-bot установлен, но сервис НЕ ЗАПУСТИЛСЯ."
        echo "   Диагностика:  awg-bot logs   (частая причина — неверный токен/конфиг)"
        echo "   Исправить:    sudo awg-bot reconfigure"
    fi
    echo "   Код и venv:     $INSTALL_DIR"
    echo "   Конфиг (yaml):  $CONF_DIR         (правится руками, переживает update)"
    echo "   Секреты (600):  $ENV_FILE"
    echo "   Данные (БД):    $DATA_DIR"
    echo "   systemd-юнит:   $UNIT_PATH"
    echo
    echo "   Управление — команда awg-bot:"
    echo "     awg-bot status              состояние сервиса"
    echo "     awg-bot logs                журнал (follow)"
    echo "     awg-bot reconfigure         перенастроить топологию/секреты"
    echo "     awg-bot update [tgz]        обновить код из архива"
    echo "     awg-bot backup              снимок БД + конфига + секретов"
    echo "     awg-bot restore [tgz]       восстановить из снимка"
    echo "     awg-bot uninstall           снять сервис"
    ok "══════════════════════════════════════════════════════════════════"
}

# ── reconfigure ──────────────────────────────────────────────────────────────
cmd_reconfigure() {
    local first_run=0 cleanup_inst="" cleanup_tgz=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --first-run) first_run=1; shift ;;
            --cleanup)   cleanup_inst="${2:-}"; cleanup_tgz="${3:-}"; shift 3 ;;
            *) die "reconfigure: неизвестный аргумент '$1'" ;;
        esac
    done
    require_root
    [[ -f "$INSTALL_DIR/awgbot/__main__.py" ]] || die "код не распакован в $INSTALL_DIR (запусти внешний установщик)"

    if [[ "$first_run" -eq 1 ]]; then
        ensure_python
        build_venv
        mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR"
        seed_conf
        configure_topology                 # свежая установка — визард обязателен
        setup_secrets
        setup_email_resume
        validate_config
        install_unit
        log "включаю и запускаю сервис…"
        systemctl enable --now "$SERVICE"; sleep 1
        local svc_state="failed"
        systemctl is-active --quiet "$SERVICE" && { svc_state="active"; ok "$SERVICE запущен."; } \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e"
        optional_steps
        print_map "$svc_state"
        # подчистить внешний установщик и архив (переданы двумя явными путями).
        # Мы — уже exec'нутый процесс, файл установщика никем не держится → безопасно.
        [[ -n "$cleanup_inst" && -f "$cleanup_inst" ]] && { rm -f "$cleanup_inst" && log "удалён установщик: $cleanup_inst"; }
        [[ -n "$cleanup_tgz"  && -f "$cleanup_tgz"  ]] && { rm -f "$cleanup_tgz"  && log "удалён архив: $cleanup_tgz"; }
    else
        require_installed
        seed_conf                          # доложить недостающие шаблоны, существующие не трогать
        configure_topology                 # перенастройка поверх существующего
        setup_secrets
        setup_email_resume
        validate_config
        log "перезапускаю $SERVICE…"; systemctl restart "$SERVICE" 2>/dev/null || true
        systemctl is-active --quiet "$SERVICE" && ok "$SERVICE перезапущен." \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e"
        ok "Переконфигурация завершена."
    fi
}

# ── update ───────────────────────────────────────────────────────────────────
locate_tgz() {  # locate_tgz DEFAULT_NAME EXPLICIT → печатает путь или пусто
    local name="$1" explicit="${2:-}"
    if [[ -n "$explicit" ]]; then [[ -f "$explicit" ]] && { echo "$explicit"; return; }; die "архив не найден: $explicit"; fi
    [[ -f "./$name"          ]] && { echo "$(pwd)/$name"; return; }   # рядом с оператором (CWD)
    [[ -f "$SELF_DIR/$name"  ]] && { echo "$SELF_DIR/$name"; return; } # рядом со скриптом
    return 0
}
cmd_update() {
    require_root; require_installed
    local tgz; tgz="$(locate_tgz "awg-bot-update.tgz" "${1:-}")"
    [[ -n "$tgz" ]] || die "не найден awg-bot-update.tgz (в текущем каталоге или рядом со скриптом); укажи путь: awg-bot update <tgz>"
    log "обновление из: $tgz"

    local wipe=0
    if confirm "Удалить пользовательские данные (БД в $DATA_DIR + секреты $ENV_FILE + конфиг)?" n; then
        confirm "Точно удалить ВСЕ данные и настройки? Это НЕОБРАТИМО." n && wipe=1 || log "данные оставлены."
    fi

    ensure_python
    log "останавливаю $SERVICE…"; systemctl stop "$SERVICE" 2>/dev/null || true

    local tmp; tmp="$(mktemp -d)"
    log "распаковываю новый код…"
    tar xzf "$tgz" -C "$tmp"
    local main; main="$(find "$tmp" -maxdepth 3 -type f -path '*/awgbot/__main__.py' | head -n1)"
    [[ -n "$main" ]] || { rm -rf "$tmp"; die "в архиве нет awgbot/ — не та поставка?"; }
    local src; src="$(dirname "$(dirname "$main")")"

    # заменить код, сохранив venv (данные/конфиг живут в /etc и /var — их не касаемся)
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv -exec rm -rf {} +
    cp -a "$src"/. "$INSTALL_DIR"/
    rm -rf "$tmp"
    chmod +x "$INSTALL_DIR/awg-bot.sh" 2>/dev/null || true

    build_venv
    install_unit
    seed_conf                              # досеять НОВЫЕ conf-файлы этой версии
                                           # (существующие не трогаем — idempotent)
    validate_config

    if [[ "$wipe" -eq 1 ]]; then
        warn "удаляю данные и настройки по твоему запросу…"
        rm -rf "$DATA_DIR"/*.db "$DATA_DIR"/*.db-* 2>/dev/null || true
        rm -f "$ENV_FILE" 2>/dev/null || true
        rm -rf "$CONF_DIR" 2>/dev/null || true
        warn "данные удалены — потребуется reconfigure перед стартом."
        die "запусти: sudo awg-bot reconfigure"
    fi

    systemctl start "$SERVICE"; sleep 1
    systemctl is-active --quiet "$SERVICE" && ok "$SERVICE перезапущен." \
        || warn "$SERVICE не активен — journalctl -u $SERVICE -e"
    ok "Обновление завершено."
}

# ── backup / restore (снимок состояния: БД + conf + env) ─────────────────────
cmd_backup() {
    require_root
    mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
    local ts out; ts="$(date +%Y%m%d-%H%M%S)"; out="$BACKUP_DIR/awg-bot-state-$ts.tgz"
    local tmp; tmp="$(mktemp -d)"; mkdir -p "$tmp/state"
    [[ -d "$DATA_DIR" ]] && find "$DATA_DIR" -maxdepth 1 -name '*.db' -exec cp -a {} "$tmp/state/" \; 2>/dev/null || true
    [[ -d "$CONF_DIR" ]] && cp -a "$CONF_DIR" "$tmp/state/conf" 2>/dev/null || true
    [[ -f "$ENV_FILE" ]] && cp -a "$ENV_FILE" "$tmp/state/env" 2>/dev/null || true
    ( cd "$tmp/state" && tar czf "$out" . ); chmod 600 "$out"; rm -rf "$tmp"
    ok "снимок состояния: $out"
    log "в нём БД, конфиг и секреты — храни как чувствительный (внутри приватные ключи устройств)."
}
cmd_restore() {
    require_root
    local tgz
    if [[ -n "${1:-}" ]]; then tgz="$1"; [[ -f "$tgz" ]] || die "не найден: $tgz"
    else
        [[ -d "$BACKUP_DIR" ]] || die "нет каталога снимков $BACKUP_DIR — укажи путь: awg-bot restore <tgz>"
        tgz="$(ls -1t "$BACKUP_DIR"/awg-bot-state-*.tgz 2>/dev/null | head -n1 || true)"
        [[ -n "$tgz" ]] || die "снимков не найдено в $BACKUP_DIR — укажи путь: awg-bot restore <tgz>"
    fi
    warn "восстановление ПЕРЕЗАПИШЕТ текущие БД/конфиг/секреты содержимым: $(basename "$tgz")"
    confirm "Продолжить?" n || die "отменено"
    local tmp; tmp="$(mktemp -d)"; tar xzf "$tgz" -C "$tmp" || { rm -rf "$tmp"; die "не удалось распаковать снимок"; }
    log "останавливаю $SERVICE…"; systemctl stop "$SERVICE" 2>/dev/null || true
    mkdir -p "$DATA_DIR" "$CONF_DIR"
    find "$tmp" -maxdepth 2 -name '*.db' -exec cp -a {} "$DATA_DIR/" \; 2>/dev/null || true
    [[ -d "$tmp/conf" ]] && { rm -rf "$CONF_DIR"; cp -a "$tmp/conf" "$CONF_DIR"; }
    [[ -f "$tmp/env"  ]] && { cp -a "$tmp/env" "$ENV_FILE"; chmod 600 "$ENV_FILE"; }
    rm -rf "$tmp"
    systemctl start "$SERVICE" 2>/dev/null || true; sleep 1
    systemctl is-active --quiet "$SERVICE" && ok "восстановлено, $SERVICE запущен." || warn "$SERVICE не активен — journalctl -u $SERVICE -e"
}

# ── uninstall (self-removal через отсоединённый пост-хук) ─────────────────────
cmd_uninstall() {
    require_root
    warn "Снятие сервиса awg-bot: будут удалены сервис, код и команда awg-bot."
    confirm "Точно снять awg-bot?" n || { log "отменено."; return 0; }
    # ПОРЯДОК КРИТИЧЕН: сперва БЕЗУСЛОВНО и СИНХРОННО убираем
    # то, из-за чего установка считается установкой (код + симлинк), чтобы
    # прерывание на любом дальнейшем вопросе оставляло чистое поле, а не тупик.
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_PATH"; systemctl daemon-reload 2>/dev/null || true
    rm -f "$SELF_LINK"
    local tmp_self; tmp_self="$(mktemp)"; cp "$SELF_PATH" "$tmp_self"; chmod +x "$tmp_self"
    rm -rf "$INSTALL_DIR"
    ok "сервис снят, код и команда удалены — хост чист для переустановки."
    exec "$tmp_self" __post_uninstall
}
# Хвост: опциональная зачистка данных/секретов (из /tmp-копии, прерывание тут
# уже безопасно — установка полностью снята).
cmd_post_uninstall() {
    warn "ДАННЫЕ: в $DATA_DIR — БД с приватными ключами устройств; в $ETC_DIR — секреты."
    confirm "Удалить $ETC_DIR (секреты + конфиг)?" n && { rm -rf "$ETC_DIR"; ok "удалён $ETC_DIR"; }
    confirm "Удалить $DATA_DIR (БД + бэкапы — НЕОБРАТИМО)?" n && { rm -rf "$DATA_DIR"; ok "удалён $DATA_DIR"; }
    # Firewall: снимаем ТОЛЬКО свою таблицу/файл (harden_firewall.sh их создал).
    # Снятие адресных drop'ов делает SSH снова открытым для всех — доступ к хосту
    # при этом НЕ теряется (мы только убираем ограничение, а не рвём established).
    local fw_rules="/etc/nftables.d/awg-bot-guard.nft"
    local fw_table="inet awg_bot_guard"
    if [[ -f "$fw_rules" ]] || nft list table $fw_table >/dev/null 2>&1; then
        warn "Найдены firewall-правила awg-bot (таблица awg_bot_guard, SSH-вайтлист)."
        if confirm "Снять их? (SSH снова станет открыт для всех адресов)" n; then
            nft delete table $fw_table 2>/dev/null && ok "таблица awg_bot_guard снята" \
                || warn "таблицы awg_bot_guard не было в рантайме"
            rm -f "$fw_rules" && ok "удалён $fw_rules" || true
            # include-строку в /etc/nftables.conf убираем ТОЛЬКО если каталог
            # /etc/nftables.d опустел (иначе там могут быть чужие правила).
            if [[ -d /etc/nftables.d ]] && [[ -z "$(ls -A /etc/nftables.d 2>/dev/null)" ]]; then
                rmdir /etc/nftables.d 2>/dev/null || true
                if [[ -f /etc/nftables.conf ]]; then
                    sed -i '\#include "/etc/nftables.d/\*.nft"#d' /etc/nftables.conf 2>/dev/null || true
                    ok "убрал include пустого /etc/nftables.d из /etc/nftables.conf"
                fi
            else
                log "в /etc/nftables.d остались другие правила — include не трогаю."
            fi
        else
            log "firewall-правила оставлены. Снять вручную: nft delete table $fw_table"
        fi
    fi
    ok "Готово."
    setsid sh -c "sleep 1; rm -f '$SELF_PATH'" >/dev/null 2>&1 < /dev/null &
}

# ── status / logs ────────────────────────────────────────────────────────────
cmd_status() {
    echo "Пути: код=$INSTALL_DIR conf=$CONF_DIR env=$ENV_FILE data=$DATA_DIR"
    systemctl status "$SERVICE" --no-pager -l 2>/dev/null | head -n 12 || warn "юнит $SERVICE не найден"
}
cmd_logs() { exec journalctl -u "$SERVICE" -n 200 -f; }

cmd_start()   { require_root; systemctl start "$SERVICE";   _svc_feedback "запущен"; }
cmd_stop()    { require_root; systemctl stop "$SERVICE";    _svc_feedback "остановлен"; }
cmd_restart() { require_root; systemctl restart "$SERVICE"; _svc_feedback "перезапущен"; }
_svc_feedback() {  # короткий итог после start/stop/restart
    sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE $1 (active)."
    else
        [[ "$1" == "остановлен" ]] && ok "$SERVICE $1." \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e"
    fi
}

usage() {
    cat <<EOF
awg-bot — управление установленным ботом.

  awg-bot status             состояние сервиса
  awg-bot start              запустить сервис
  awg-bot stop               остановить сервис
  awg-bot restart            перезапустить сервис
  awg-bot reconfigure        перенастроить топологию/секреты (wizard)
  awg-bot update [tgz]       обновить код из архива (по умолч. awg-bot-update.tgz)
  awg-bot backup             снимок БД + конфига + секретов
  awg-bot restore [tgz]      восстановить из снимка (по умолч. — самый свежий)
  awg-bot logs               журнал сервиса (follow)
  awg-bot uninstall          удалить приложение (опционально: данные приложения)
EOF
}

VERB="${1:-}"; shift || true
case "$VERB" in
    reconfigure) cmd_reconfigure "$@" ;;
    update)      cmd_update "${1:-}" ;;
    backup)      cmd_backup ;;
    restore)     cmd_restore "${1:-}" ;;
    uninstall)   cmd_uninstall ;;
    __post_uninstall) cmd_post_uninstall ;;
    status)      cmd_status ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    logs)        cmd_logs ;;
    -h|--help|help|"") usage ;;
    *) usage; die "неизвестная команда: $VERB" ;;
esac
