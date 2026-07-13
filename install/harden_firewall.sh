#!/usr/bin/env bash
#
# harden_firewall.sh — точечный firewall для ХОСТА БОТА (nftables).
#
# Философия (пересмотрена):
#   • Ограничиваем ТОЛЬКО одно — SSH: доступ к хосту разрешён лишь с ВАЙТЛИСТА
#     ВАШИХ IP (постоянные белые адреса; нет статики — DynamicDNS/аналог) плюс
#     из собственной VPN-подсети (вход «через свой туннель»).
#   • ВЕСЬ остальной трафик хоста НЕ трогаем: политика input — accept, мы лишь
#     добавляем адресные drop'ы для двух портов выше. Так авария firewall не
#     отрежет хост от сети целиком.
#
# ⚠️  ВАЖНО — НЕ сводите SSH-вайтлист к адресу VPN-туннеля. Это создаёт ДЕДЛОК:
#     развёртывание/управление/подключение сервера из приложения AmneziaVPN
#     (full-access профиль) требует доступности SSH СНАЧАЛА — а туннель, которым
#     вы хотели бы дать этот SSH-доступ, сам не поднимется, пока SSH недоступен.
#     (VPN-подсеть в вайтлисте полезна для обычного захода, но не спасает от
#     этого дедлока — нужны и внешние белые IP.)
#     Замкнутый круг. Разворачивание контейнера Amnezia вообще идёт с IP, с
#     которого вы это делаете (SSH с вашего адреса). Поэтому:
#       • вносите в вайтлист СВОИ постоянные IP (дом/офис/впн-выход);
#       • нет статики — DynamicDNS или аналог (обновляемая A-запись);
#       • в идеале эти IP — ЗА ПРЕДЕЛАМИ РФ и маршрутизируются через ваши же
#         туннели в те страны — так природа управляющего трафика не выдаёт себя;
#       • держите SSH на НЕСТАНДАРТНОМ порту (меньше шума/сканов).
#
# Идемпотентно: пересоздаёт СВОЮ таблицу inet awg_bot_guard, чужие правила
# (в т.ч. Docker/Amnezia на совмещённом хосте) не трогает.
#
# ВНИМАНИЕ: не закрывайте текущую SSH-сессию, пока не проверите вход по новой
# из разрешённой сети. Established-соединения не рвутся, но новый вход с
# невайтлистенного адреса будет отклонён.

set -euo pipefail

RULES_DIR="/etc/nftables.d"
RULES_FILE="$RULES_DIR/awg-bot-guard.nft"
MAIN_CONF="/etc/nftables.conf"
TABLE="inet awg_bot_guard"

log() { printf '\033[0;36m[firewall]\033[0m %s\n' "$*"; }
err() { printf '\033[0;31m[firewall:ОШИБКА]\033[0m %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root (sudo $0)"

# ── nftables на месте? ───────────────────────────────────────────────────────
if ! command -v nft >/dev/null 2>&1; then
    err "не найден nft (nftables)."
    read -r -p "Установить пакет nftables сейчас (apt)? [y/N]: " a
    [[ "${a,,}" == "y" ]] || die "поставьте nftables вручную: apt install nftables"
    apt-get update && apt-get install -y nftables
fi

# ── опрос параметров ─────────────────────────────────────────────────────────
echo "Рекомендуется НЕСТАНДАРТНЫЙ порт SSH (меньше сканов). Должен совпадать с"
echo "портом в /etc/ssh/sshd_config (менять его — отдельно от этого скрипта)."
read -r -p "Порт SSH [22]: " SSH_PORT;   SSH_PORT="${SSH_PORT:-22}"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "порт SSH должен быть числом"
[[ "$SSH_PORT" == "22" ]] && echo "[!] порт 22 — стандартный; смена на нестандартный рекомендуется."

echo
echo "VPN-подсеть этого сервера попадёт в SSH-вайтлист: зайти по SSH можно будет,"
echo "подключившись к собственному VPN (подсеть из conf/app.yaml, network.subnet_cidr)."
# дефолт берём из уже настроенного app.yaml (визард положил туда subnet_cidr из
# живого контейнера — подсеть может быть нестандартной); иначе 10.8.1.0/24.
_def_net="10.8.1.0/24"
for _cfg in /etc/awg-bot/conf/app.yaml "$(dirname "$0")/../conf/app.yaml"; do
    if [[ -r "$_cfg" ]]; then
        _v="$(grep -oP '^\s*subnet_cidr:\s*"?\K[0-9./]+' "$_cfg" 2>/dev/null | head -n1)"
        [[ -n "$_v" ]] && { _def_net="$_v"; break; }
    fi
done
read -r -p "VPN-подсеть (Enter — ${_def_net}; '-' — не добавлять): " VPN_NET
VPN_NET="${VPN_NET:-$_def_net}"

# ── валидатор IP/CIDR (v4 и v6). Печатает 4|6, код 0; иначе код 1 ─────────────
validate_ip() {
    local t="$1"
    [[ -z "$t" ]] && return 1
    if [[ "$t" == *:* ]]; then
        local addr="${t%%/*}" pfx
        if [[ "$t" == */* ]]; then pfx="${t##*/}"; [[ "$pfx" =~ ^[0-9]+$ ]] && ((pfx>=0 && pfx<=128)) || return 1; fi
        [[ "$addr" =~ ^[0-9A-Fa-f:]+$ ]] || return 1
        [[ "$addr" == *::*::* ]] && return 1
        [[ "$addr" == "::" || "$addr" =~ [0-9A-Fa-f] ]] || return 1
        echo 6; return 0
    else
        local addr="${t%%/*}" pfx o; local IFS=.
        if [[ "$t" == */* ]]; then pfx="${t##*/}"; [[ "$pfx" =~ ^[0-9]+$ ]] && ((pfx>=0 && pfx<=32)) || return 1; fi
        [[ "$addr" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
        local -a oct; IFS=. read -r -a oct <<< "$addr"; for o in "${oct[@]}"; do ((o>=0 && o<=255)) || return 1; done
        echo 4; return 0
    fi
}

echo
echo "ВАЙТЛИСТ SSH — ВАШИ постоянные IP, с которых будете заходить и управлять"
echo "сервером из приложения Amnezia (дом/офис/выход вашего VPN). v4 и v6, можно CIDR."
echo "  • нет статики → DynamicDNS/аналог; в идеале IP — за пределами РФ."
echo "  • Enter (пусто) → SSH останется ОТКРЫТ ДЛЯ ВСЕХ (небезопасно, но не заблокирует)."
echo "Формат: через запятую ИЛИ пробел. Пример: 203.0.113.7, 198.51.100.0/24, 2001:db8::/48"
read -r -p "Whitelist для SSH (ваши IP): " USER_LIST_RAW

# awg-IP — в вайтлисте по умолчанию: агент едет на awg-хост, это наш законный
# управляющий адрес (вход через туннель). Плюс список пользователя.
declare -a SSH_V4=("127.0.0.1") SSH_V6=("::1")
vpn_in_list=0
if [[ "$VPN_NET" != "-" && -n "$VPN_NET" ]]; then
    if fam="$(validate_ip "$VPN_NET")"; then
        [[ "$fam" == 6 ]] && SSH_V6+=("$VPN_NET") || SSH_V4+=("$VPN_NET"); vpn_in_list=1
    else
        err "'$VPN_NET' не похож на IP/CIDR — VPN-подсеть в вайтлист не добавлена."
    fi
fi

# разобрать ввод пользователя: разделители — запятые И пробелы
USER_LIST_RAW="${USER_LIST_RAW//,/ }"
read -r -a USER_ELEMS <<< "$USER_LIST_RAW"
user_count=0
for n in "${USER_ELEMS[@]:-}"; do
    [[ -z "$n" ]] && continue
    if fam="$(validate_ip "$n")"; then
        [[ "$fam" == 6 ]] && SSH_V6+=("$n") || SSH_V4+=("$n"); ((user_count++)) || true
    else
        die "адрес '$n' не является корректным IPv4/IPv6/CIDR — исправьте и повторите."
    fi
done

# Решение о правиле SSH:
#   есть адреса пользователя → вайтлист (v4+v6);
#   пусто, но есть только awg-IP → вайтлист + предупреждение о ДЕДЛОКЕ;
#   совсем пусто → SSH ОТКРЫТ ДЛЯ ВСЕХ (по вашему выбору), правило drop не ставим.
SSH_OPEN_ALL=0
if [[ "$user_count" -eq 0 ]]; then
    if [[ "$vpn_in_list" -eq 1 ]]; then
        err "Вы не задали своих IP — в вайтлисте только собственная VPN-подсеть."
        err "РИСК ДЕДЛОКА: управление/подключение сервера из приложения Amnezia"
        err "(full-access) требует SSH ДО поднятия туннеля — а туннель без SSH не"
        err "встанет. Рекомендуется добавить СВОЙ независимый IP."
        read -r -p "Всё равно оставить только VPN-подсеть? [y/N]: " a
        [[ "${a,,}" == "y" ]] || die "отменено — перезапустите и укажите свои IP"
    else
        err "Вайтлист пуст. По умолчанию SSH останется ОТКРЫТ ДЛЯ ВСЕХ адресов."
        err "Это небезопасно (брутфорс/сканы). Настоятельно рекомендуем вайтлист"
        err "своих IP; если сейчас негде взять постоянный адрес — настройте DynamicDNS."
        read -r -p "Оставить SSH открытым для всех? [y/N]: " a
        [[ "${a,,}" == "y" ]] || die "отменено — перезапустите и укажите свои IP"
        SSH_OPEN_ALL=1
    fi
fi

join() { local IFS=", "; echo "$*"; }

if [[ "$SSH_OPEN_ALL" -eq 1 ]]; then
    log "SSH ($SSH_PORT): ОТКРЫТ ДЛЯ ВСЕХ адресов (по вашему выбору)"
else
    log "SSH ($SSH_PORT) разрешён с: v4[$(join "${SSH_V4[@]}")] v6[$(join "${SSH_V6[@]}")]"
fi
read -r -p "Применить эти правила? [y/N]: " a; [[ "${a,,}" == "y" ]] || die "отменено"

# ── SSH-правила: либо вайтлист (v4+v6), либо ничего (открыто для всех) ────────
if [[ "$SSH_OPEN_ALL" -eq 1 ]]; then
    SSH_RULES="        # SSH открыт для всех — адресных ограничений не ставим (ваш выбор)"
else
    SSH_RULES="        tcp dport $SSH_PORT ip  saddr @ssh_allow4 accept
        tcp dport $SSH_PORT ip6 saddr @ssh_allow6 accept
        tcp dport $SSH_PORT drop"
fi

# ── сгенерировать ruleset ────────────────────────────────────────────────────
mkdir -p "$RULES_DIR"
cat > "$RULES_FILE" <<EOF
#!/usr/sbin/nft -f
# Сгенерировано harden_firewall.sh — правит ТОЛЬКО таблицу awg_bot_guard.
# Идемпотентный пере-создатель: объявить → удалить → создать заново.
table $TABLE
delete table $TABLE
table $TABLE {
    set ssh_allow4 {
        type ipv4_addr
        flags interval
        elements = { $(join "${SSH_V4[@]}") }
    }
    set ssh_allow6 {
        type ipv6_addr
        flags interval
        elements = { $(join "${SSH_V6[@]}") }
    }
    chain input {
        # policy accept: хост в целом НЕ запираем, добавляем лишь адресные drop'ы
        type filter hook input priority filter; policy accept;

        ct state established,related accept
        iif lo accept

        # SSH: пускаем вайтлист (v4+v6), остальным — от ворот поворот
$SSH_RULES
    }
}
EOF
chmod 0644 "$RULES_FILE"

# ── применить сейчас (только нашу таблицу) ───────────────────────────────────
if nft -f "$RULES_FILE"; then
    log "правила применены (таблица awg_bot_guard)"
else
    die "nft отклонил ruleset — проверьте $RULES_FILE"
fi

# ── персистентность через include в /etc/nftables.conf ───────────────────────
INCLUDE_LINE="include \"$RULES_DIR/*.nft\""
if [[ -f "$MAIN_CONF" ]]; then
    if ! grep -qF "$RULES_DIR" "$MAIN_CONF"; then
        printf '\n%s\n' "$INCLUDE_LINE" >> "$MAIN_CONF"
        log "добавлен include в $MAIN_CONF"
    fi
else
    printf '#!/usr/sbin/nft -f\n%s\n' "$INCLUDE_LINE" > "$MAIN_CONF"
    log "создан $MAIN_CONF с include"
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl enable nftables >/dev/null 2>&1 || \
        err "не удалось enable nftables.service — правила могут не пережить ребут"
    log "nftables.service включён (правила переживут перезагрузку)"
fi

echo
log "готово. Проверьте НОВЫМ подключением, что SSH пускает с разрешённой сети,"
log "и только потом закрывайте текущую сессию."
log "Снять наши правила при аварии:  nft delete table $TABLE"
