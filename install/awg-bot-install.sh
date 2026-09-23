#!/usr/bin/env bash
#
# awg-bot-install.sh — внешний установщик-bootstrap хоста бота awg-bot.
#
# Главная задача — greenfield-подготовка «в одно окно»: развернуть код и
# передать управление внутреннему инструменту, который всё настроит. Сам НЕ
# настраивает и НЕ дублирует логику: конфигурация/venv/юнит/валидация живут в
# awg-bot.sh (внутри архива → /opt/awg-bot/awg-bot.sh).
#
# Флоу: root → взять код (распакованная поставка вокруг себя ИЛИ awg-bot.tgz
# рядом) → mkdir FHS → разложить код в /opt/awg-bot → симлинк awg-bot →
# exec awg-bot.sh reconfigure --first-run, передав на самоочистку всё
# временное: архив и каталог распаковки.
#
# ПОВЕРХ РАБОЧЕЙ УСТАНОВКИ (есть /opt/awg-bot/venv) — обеим ролям одно меню:
#   1) обновить до версии поставки, сохранив данные и настройки — только если
#      версия на хосте МЕНЬШЕ версии в поставке, иначе пункт говорит «обновление
#      не нужно». Зовёт `awg-bot update <архив>` с AWG_UPDATE_KEEP=1 (вопроса
#      об удалении данных нет); шлюзу — ещё AWG_UPDATE_THEN_BUNDLE=<файл
#      конфигурации из --bundle>, и после обновления файл применяется новым
#      кодом (`awg-bot reconfigure --role gateway --bundle`);
#   2) снести прошлую установку вместе с данными и настройками и поставить
#      заново (второе подтверждение): код, /etc/awg-bot, /var/lib/awg-bot; у
#      шлюза — ещё `routing-gw-setup.sh --rollback`, /etc/awg-gw, /var/lib/awg-gw,
#      /opt/awg-gw и скрипты в /usr/local/sbin (аплинк НЕ трогается — это связь
#      агента с Telegram); у ВПС — ВСЁ, что поставил бот: линки до шлюзов и
#      обвязка условной маршрутизации (их же --rollback), резолвер клиентов,
#      интерфейсы awg со всеми пирами, контейнер docker-режима, таблица
#      inet awg_bot_guard — клиенты остаются без доступа, поэтому ВПС
#      подтверждает это ещё и словом «УДАЛИТЬ». Дальше — обычная установка с
#      чистого листа;
#   3) отмена.
# Молча применять файл конфигурации на уже установленном агенте установщик НЕ
# будет: новую конфигурацию туда применяют из чата бота шлюза (переслать файл)
# или `sudo sh <файл>` без --install.
#
# ОСНОВНОЙ СПОСОБ — ОДНА КОМАНДА. По ссылке едет не архив, а этот же скрипт:
# curl отдаёт его в sudo bash, он качает поставку, распаковывает во временный
# каталог и ПЕРЕДАЁТ УПРАВЛЕНИЕ установщику ИЗ АРХИВА.
#
#   curl -fsSL https://raw.githubusercontent.com/<repo>/main/install/awg-bot-install.sh | sudo bash
#
# Передача управления — не формальность. Логика установки принадлежит ПОСТАВКЕ
# и обязана ехать вместе с ней: иначе на хосте выполнялся бы установщик из
# ветки main, а код ставился бы из релиза, и эти двое разъезжались бы молча.
# В режиме трубы этот файл делает ровно три вещи: качает, проверяет sha256 и
# запускает установщик из распакованного архива.
#
# Аргументы после `-s --`:
#   … | sudo bash -s -- --role gateway
#
# ТРЕБОВАНИЕ К ХОСТУ, КОТОРОЕ ЗДЕСЬ НЕ ПРОВЕРЯЕТСЯ: синхронизированные часы
# (systemd-timesyncd или chrony; `timedatectl show -p NTPSynchronized --value`
# → yes). Оба бота верят часам своего хоста: TLS к GitHub, PyPI и Telegram,
# расписания, сроки подписок. Особенно малина — часов реального времени у неё
# нет. Канал ВПС ↔ шлюз от часов не зависит и показывает их расхождение в
# карточке слота. Подробности — README §2, «Время хоста».
#
# ВАЖНОЕ СЛЕДСТВИЕ: при запуске из трубы stdin занят самим скриптом, поэтому
# ВСЕ вопросы задаются в /dev/tty (здесь и дальше, в awg-bot.sh). Иначе визард
# «прочитал» бы собственный текст вместо ответа и молча ушёл по умолчаниям.
#
# Прочие способы (когда архив уже скачан):
#   sudo bash install/awg-bot-install.sh   (из распакованной поставки)
#   sudo ./awg-bot-install.sh              (архив awg-bot.tgz рядом со скриптом)
#   sudo ./awg-bot-install.sh <path.tgz>
#   sudo ./awg-bot-install.sh --role gateway [<path.tgz>]   # агент на шлюзе
#   sudo ./awg-bot-install.sh --port 51820 [--subnet 10.8.1]  # порт/подсеть создаваемого
#                                                            # awg-сервера (иначе порт случайный)
#   sudo ./awg-bot-install.sh --advanced      # спросить всё, как в прежних версиях
#   sudo ./awg-bot-install.sh --skip-verify   # своя сборка: не сверять sha256 с релизом
#   … --role gateway [--bundle <файл>]        # агент шлюза; файл ищется сам в /root
#
set -euo pipefail

REPO="${AWG_BOT_REPO:-justSunny12/awg-bot}"       # откуда качать поставку
TGZ_URL="${AWG_BOT_TGZ_URL:-https://github.com/$REPO/releases/latest/download/awg-bot.tgz}"

INSTALL_DIR="/opt/awg-bot"
ETC_DIR="/etc/awg-bot"
DATA_DIR="/var/lib/awg-bot"
SELF_LINK="/usr/local/bin/awg-bot"

c_info=$'\033[0;36m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log() { printf '%s[install]%s %s\n' "$c_info" "$c_off" "$*"; }
die() { printf '%s[install:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

verify_archive() {  # verify_archive TGZ SRC_ROOT — сверить sha256 с релизом GitHub
    # Не обязательный шаг: у GitHub есть лимит на анонимные запросы, а сети
    # может не быть вовсе. Ответил — сверяем и на расхождении отказываемся
    # ставить; не ответил — говорим об этом одной строкой и продолжаем, потому
    # что целостность загрузки и так держит TLS.
    local tgz="$1" root="$2" ver repo want have
    [[ "$SKIP_VERIFY" != "1" ]] || { log "целостность: сверка отключена (--skip-verify)"; return 0; }
    [[ -f "$tgz" ]] || return 0
    command -v sha256sum >/dev/null 2>&1 || return 0
    ver="$(sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' "$root/awgbot/__version__.py" 2>/dev/null | head -n1)"
    repo="$(sed -nE 's/^ *repo: *"?([^"#]+)"?.*/\1/p' "$root/conf/updates.yaml" 2>/dev/null | head -n1 | tr -d ' ')"
    [[ -n "$ver" && -n "$repo" ]] || return 0
    want="$(curl -fsS --max-time 10 "https://api.github.com/repos/$repo/releases/tags/v$ver" 2>/dev/null \
            | sed -nE 's/.*"digest" *: *"sha256:([0-9a-f]{64})".*/\1/p' | head -n1)"
    if [[ -z "$want" ]]; then
        log "целостность: GitHub не ответил — ставлю без сверки (загрузку прикрывает TLS)"
        return 0
    fi
    have="$(sha256sum "$tgz" | awk '{print $1}')"
    [[ "$have" == "$want" ]] || die "sha256 архива не совпал с релизом v$ver — ставить не буду. Скачай архив заново; если это своя сборка — повтори с --skip-verify"
    log "целостность: sha256 совпал с релизом v$ver"
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    # Из трубы «$0» — это «bash», и совет «sudo bash» выглядел бы издевательством.
    if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
        die "нужен root: sudo $0 $*"
    fi
    die "нужен root. Повтори команду целиком:
  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install/awg-bot-install.sh | sudo bash"
fi

# Из трубы (`curl … | sudo bash`) файла у нас нет: BASH_SOURCE указывает на
# «bash», а не на скрипт. Тогда путь к себе не вычисляем и поставку качаем.
SELF_PATH=""; SELF_DIR=""; PIPED=1
if [[ -f "${BASH_SOURCE[0]:-}" ]]; then
    SELF_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
    SELF_DIR="$(dirname "$SELF_PATH")"
    PIPED=0
fi

# ── ключи: --role gateway (или AWG_BOT_ROLE=gateway), --port N, --subnet X.Y.Z ──
ROLE="${AWG_BOT_ROLE:-client}"
SKIP_VERIFY=0                  # своя сборка: sha256 с релизом сверять не с чем
ORIG_ARGS=("$@")               # что передать установщику из поставки как есть
EXTRA=()                       # что уходит дальше в reconfigure --first-run
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --role)   ROLE="${2:-client}"; shift 2 ;;
        --port)   [[ "${2:-}" =~ ^[0-9]+$ ]] || die "--port: число"; EXTRA+=(--port "$2"); shift 2 ;;
        --subnet) [[ "${2:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--subnet: три октета вида 10.8.1"; EXTRA+=(--subnet "$2"); shift 2 ;;
        --advanced) EXTRA+=(--advanced); shift ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        --bundle) [[ -n "${2:-}" ]] || die "--bundle: нужен путь к файлу"; EXTRA+=(--bundle "$2"); shift 2 ;;
        *) die "неизвестный ключ: $1" ;;
    esac
done

# ── откуда брать код ─────────────────────────────────────────────────────────
# Поставка теперь ОДИН архив, и установщик едет внутри него: обычный путь —
# «распаковали, запустили отсюда». Тогда код лежит вокруг нас, и распаковывать
# нечего. Архив рядом остаётся для случая, когда скрипт вытащили отдельно.
TGZ=""
SRC_ROOT=""
UNPACK_ROOT=""
[[ -n "$SELF_DIR" ]] && UNPACK_ROOT="$(cd "$SELF_DIR/.." && pwd)"
if [[ "$PIPED" -eq 1 && -z "${1:-}" ]]; then
    # Скрипт пришёл из трубы — качаем поставку сами, во временный каталог,
    # который сами же и уберём. Это и есть «установка одной командой»: снаружи
    # ничего не остаётся, ни архива, ни распакованного дерева.
    command -v curl >/dev/null 2>&1 || die "нужен curl"
    command -v tar  >/dev/null 2>&1 || die "нужен tar"
    SRC_ROOT="$(mktemp -d /tmp/awg-bot-install.XXXXXX)"
    log "качаю поставку: $TGZ_URL"
    curl -fsSL --retry 3 -o "$SRC_ROOT/awg-bot.tgz" "$TGZ_URL" \
        || { rm -rf "$SRC_ROOT"; die "не скачалась поставка ($TGZ_URL)"; }
    tar xzf "$SRC_ROOT/awg-bot.tgz" -C "$SRC_ROOT" \
        || { rm -rf "$SRC_ROOT"; die "архив не распаковался — скачан не тот файл?"; }
    [[ -f "$SRC_ROOT/awgbot/__main__.py" && -f "$SRC_ROOT/awg-bot.sh" ]] \
        || { rm -rf "$SRC_ROOT"; die "в архиве нет ожидаемого дерева — не та поставка?"; }
    TGZ="$SRC_ROOT/awg-bot.tgz"
    verify_archive "$TGZ" "$SRC_ROOT"
    [[ -f "$SRC_ROOT/install/awg-bot-install.sh" ]] \
        || { rm -rf "$SRC_ROOT"; die "в поставке нет install/awg-bot-install.sh — не та поставка?"; }
    log "передаю управление установщику из поставки"
    # Дочерним процессом, а не exec: каталог создали здесь — здесь и убираем,
    # если установка сорвалась. Через exec ловушку не унести, а отказ бывает
    # ДО того, как установщик успеет что-то о себе понять (не root, не та
    # система) — и тогда временный каталог оставался бы на диске навсегда.
    # --skip-verify: sha256 уже сверен здесь, второй запрос к API ни к чему.
    set +e
    bash "$SRC_ROOT/install/awg-bot-install.sh" --skip-verify ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
    __rc=$?
    set -e
    if [[ "$__rc" -ne 0 ]]; then
        case "$SRC_ROOT" in
            /tmp/awg-bot-install.*) rm -rf "$SRC_ROOT" && log "убрал временный каталог: $SRC_ROOT" ;;
        esac
    fi
    exit "$__rc"
elif [[ -z "${1:-}" && -n "$UNPACK_ROOT" && -f "$UNPACK_ROOT/awgbot/__main__.py" && -f "$UNPACK_ROOT/awg-bot.sh" ]]; then
    SRC_ROOT="$UNPACK_ROOT"
    log "поставка распакована здесь: $SRC_ROOT"
    # Архив, если он остался рядом (скачали файлом, а не потоком) — уберём в конце
    for c in "$SRC_ROOT"/awg-bot.tgz "$SRC_ROOT"/../awg-bot.tgz; do
        [[ -f "$c" ]] && { TGZ="$(cd "$(dirname "$c")" && pwd)/$(basename "$c")"; break; }
    done
else
    if [[ -n "${1:-}" ]]; then
        [[ -f "$1" ]] || die "архив не найден: $1"
        TGZ="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
    else
        for c in "$SELF_DIR"/awg-bot.tgz "$SELF_DIR"/awg-bot-*.tgz ./awg-bot.tgz; do
            [[ -f "$c" ]] && { TGZ="$(cd "$(dirname "$c")" && pwd)/$(basename "$c")"; break; }
        done
        [[ -n "$TGZ" ]] || die "нет ни распакованной поставки рядом, ни awg-bot.tgz — скачай архив и распакуй его: tar xzf awg-bot.tgz && sudo bash install/awg-bot-install.sh"
    fi
    log "архив: $TGZ"
fi

# ── прошлая установка: обновить, снести и поставить заново, отмена ───────────
ver_of() { sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' "$1" 2>/dev/null | head -n1; }
ver_lt() {   # $1 < $2 по X.Y.Z.P
    [[ -n "$1" && -n "$2" && "$1" != "$2" ]] \
        && [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}
wipe_previous() {
    # Снять прошлую установку целиком, вместе с данными и настройками — то же,
    # что awg-bot uninstall с ответами «да» на все вопросы, плюс всё, что бот
    # поставил на хост: у шлюза — линк, юнит реассерта и таблицы (аплинк —
    # связь агента с Telegram — не трогается), у ВПС — линки, обвязка
    # маршрутизации, резолвер, интерфейсы awg с пирами, таблица файервола.
    # Чистое поле для установки ниже.
    local role; role="$(sed -nE 's/^ *role: *"?([a-z]+)"?.*/\1/p' "$ETC_DIR/conf/app.yaml" 2>/dev/null | head -n1)"
    log "снимаю сервис, удаляю код, настройки и данные прошлой установки…"
    systemctl disable --now awg-bot 2>/dev/null || true
    rm -f /etc/systemd/system/awg-bot.service; systemctl daemon-reload 2>/dev/null || true
    rm -f "$SELF_LINK"
    if [[ "$role" == "gateway" ]]; then
        [[ -x /usr/local/sbin/routing-gw-setup.sh ]] \
            && sh /usr/local/sbin/routing-gw-setup.sh --rollback >/dev/null 2>&1 || true
        # /opt/awg-gw — куда файл конфигурации кладёт скрипт обвязки и link.conf
        # с приватным ключом линка: без него «снесено с настройками» было бы неправдой
        rm -rf /etc/awg-gw /var/lib/awg-gw /opt/awg-gw
        rm -f /usr/local/sbin/routing-gw-setup.sh /usr/local/sbin/awg-lan-lists.sh /usr/local/sbin/awg-lan-domain.sh
    else
        # ВПС — всё, что поставил бот: линки до шлюзов и обвязка условной
        # маршрутизации (своими скриптами), резолвер клиентов, интерфейсы awg
        # с пирами клиентов (host-режим: awg-quick@*, конфиги в
        # /etc/amnezia/amneziawg; docker-рудимент: контейнер), таблица файервола.
        # Клиенты после этого без доступа — об этом спрошено выше, отдельно.
        local f name
        for f in /etc/amnezia/amneziawg/awglink*.conf; do
            [[ -f "$f" ]] || continue
            name="$(basename "$f" .conf)"
            [[ -x /usr/local/sbin/routing-link-setup.sh ]] \
                && LINK_IF="$name" sh /usr/local/sbin/routing-link-setup.sh --rollback >/dev/null 2>&1 || true
        done
        [[ -x /usr/local/sbin/routing-host-setup.sh ]] \
            && sh /usr/local/sbin/routing-host-setup.sh --rollback >/dev/null 2>&1 || true
        rm -f /usr/local/sbin/routing-link-setup.sh /usr/local/sbin/routing-host-setup.sh
        rm -f /etc/dnsmasq.d/awgbot-resolver.conf /etc/systemd/system/dnsmasq.service.d/awgbot-resolver.conf
        systemctl daemon-reload 2>/dev/null || true
        systemctl try-restart dnsmasq 2>/dev/null || true
        for f in /etc/amnezia/amneziawg/*.conf; do
            [[ -f "$f" ]] || continue
            name="$(basename "$f" .conf)"
            systemctl disable --now "awg-quick@$name" 2>/dev/null || true
            awg-quick down "$name" >/dev/null 2>&1 || true
        done
        rm -rf /etc/amnezia/amneziawg /root/gw-awglink*.conf /root/awg-gw-bundle*.sh
        name="$(sed -nE 's/^ *container: *"?([^"#]+)"?.*/\1/p' "$ETC_DIR/conf/app.yaml" 2>/dev/null | head -n1 | tr -d ' ')"
        [[ -n "$name" ]] && command -v docker >/dev/null 2>&1 && docker rm -f "$name" >/dev/null 2>&1 || true
        nft delete table inet awg_bot_guard 2>/dev/null || true
        rm -f /etc/nftables.d/awg-bot-guard.nft
    fi
    rm -rf "$INSTALL_DIR" "$ETC_DIR" "$DATA_DIR"
}
if [[ -x "$INSTALL_DIR/venv/bin/python" ]]; then
    # Рабочая установка уже есть — обеим ролям одно и то же меню из трёх
    # пунктов. Обновление — только вверх и с сохранением данных: вопрос
    # «удалить ли данные» здесь не задаётся (AWG_UPDATE_KEEP), для этого есть
    # второй пункт. Шлюзу после обновления применяется файл конфигурации, с
    # которым пришли (AWG_UPDATE_THEN_BUNDLE).
    BOT="$INSTALL_DIR/awg-bot.sh"
    # Роль — та, что установлена (app.yaml), а не флаг запуска: команду для
    # малины могли скопировать на ВПС, и диалог обязан говорить о том хосте,
    # который будет снесён. Так же выбирает wipe_previous.
    inst_role="$(sed -nE 's/^ *role: *"?([a-z]+)"?.*/\1/p' "$ETC_DIR/conf/app.yaml" 2>/dev/null | head -n1)"
    inst_role="${inst_role:-client}"
    have_ver="$(ver_of "$INSTALL_DIR/awgbot/__version__.py")"
    if [[ -n "$SRC_ROOT" ]]; then
        new_ver="$(ver_of "$SRC_ROOT/awgbot/__version__.py")"
    else
        new_ver="$(tar -xzOf "$TGZ" --wildcards '*awgbot/__version__.py' 2>/dev/null \
                   | sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' | head -n1)"
    fi
    newer=0; ver_lt "$have_ver" "$new_ver" && newer=1
    printf '\n%s[install]%s awg-bot уже установлен в %s: версия %s, в поставке %s. Что делаем?\n' \
        "$c_info" "$c_off" "$INSTALL_DIR" "${have_ver:-?}" "${new_ver:-?}" >&2
    if [[ "$newer" -eq 1 ]]; then
        printf '  1) Обновить до %s, сохранив данные и настройки\n' "$new_ver" >&2
    else
        printf '  1) Обновление не нужно: установлена та же или более новая версия\n' >&2
    fi
    printf '  2) Удалить прошлую установку вместе с данными и настройками и поставить заново\n' >&2
    printf '  3) Отмена\n' >&2
    read -r -p "Выбор [1-3]: " __ch < /dev/tty
    case "${__ch:-3}" in
        1)
            if [[ "$newer" -ne 1 ]]; then
                hint=""
                [[ "$inst_role" == "gateway" ]] && hint=". Конфигурацию на установленном агенте применяют из чата бота шлюза (переслать файл) или так: sudo sh <файл> (без --install)"
                die "обновление не требуется: установлена ${have_ver:-?}, в поставке ${new_ver:-?}$hint"
            fi
            if [[ -z "$TGZ" ]]; then
                # поставка распакована, архива рядом нет — awg-bot update ждёт архив
                TGZ="$(mktemp -d)/awg-bot.tgz"
                tar czf "$TGZ" -C "$SRC_ROOT" .
            fi
            bundle=""
            if [[ "$inst_role" == "gateway" ]]; then
                for ((i = 0; i < ${#EXTRA[@]}; i++)); do
                    [[ "${EXTRA[$i]}" == "--bundle" ]] && bundle="${EXTRA[$((i + 1))]:-}"
                done
            fi
            # временное — прочь: каталог распаковки из трубы и собранный архив
            # никто после exec не уберёт; архив, если он в этом каталоге,
            # переезжает, а убрать его после обновления просим awg-bot
            cleanup=""
            case "${SRC_ROOT:-}" in
                /tmp/awg-bot-install.*)
                    case "$TGZ" in "$SRC_ROOT"/*) cp "$TGZ" "$(mktemp -d)/awg-bot.tgz"; TGZ="$_" ;; esac
                    rm -rf "$SRC_ROOT" ;;
            esac
            case "$TGZ" in /tmp/*) cleanup="$(dirname "$TGZ")" ;; esac
            log "→ обновление ${have_ver:-?} → $new_ver из $TGZ, данные и настройки сохраняются"
            AWG_UPDATE_KEEP=1 AWG_UPDATE_THEN_BUNDLE="$bundle" AWG_UPDATE_CLEANUP="$cleanup" exec "$BOT" update "$TGZ" ;;
        2)
            printf '[install:!] Будут удалены: код, /etc/awg-bot (секреты и конфиг), /var/lib/awg-bot (БД, копии)' >&2
            if [[ "$inst_role" == "gateway" ]]; then
                printf ', линк до ВПС, юнит и таблицы обвязки, /etc/awg-gw, /var/lib/awg-gw, /opt/awg-gw. Аплинк остаётся.\n' >&2
            else
                printf ',\n  ЛИНКИ ДО ШЛЮЗОВ, обвязка условной маршрутизации, резолвер клиентов, ИНТЕРФЕЙСЫ AWG СО ВСЕМИ ПИРАМИ.\n' >&2
                printf '  Клиенты останутся БЕЗ ДОСТУПА. Чтобы вернуть его, придётся заново добавить всех в бота\n' >&2
                printf '  и перевыпустить каждому конфигурацию; шлюзам — новые файлы конфигурации.\n' >&2
            fi
            printf '  Это необратимо. Резервную копию, если она нужна, забери с хоста до ответа.\n' >&2
            read -r -p "Точно снести и поставить заново? [y/N]: " __a < /dev/tty
            [[ "${__a,,}" == "y" ]] || die "отмена — ничего не изменено"
            if [[ "$inst_role" != "gateway" ]]; then
                read -r -p "Напиши слово УДАЛИТЬ, чтобы подтвердить, что клиенты останутся без доступа: " __a < /dev/tty
                [[ "$__a" == "УДАЛИТЬ" ]] || die "отмена — ничего не изменено"
            fi
            wipe_previous
            log "прошлая установка снята — ставлю заново" ;;
        *)
            if [[ "$inst_role" == "gateway" ]]; then
                die "отмена — ничего не изменено. Новую конфигурацию на установленном агенте применяют из чата бота шлюза (переслать файл) или так: sudo sh <файл> (без --install)"
            fi
            die "отмена — ничего не изменено (для действий: sudo awg-bot update|restore|uninstall)" ;;
    esac
fi
# Полу-остаток (каталог есть, но рабочего venv нет) — прерванный uninstall/распаковка.
# Не тупикуем: предлагаем дочистить и продолжить с нуля.
if [[ -e "$INSTALL_DIR" ]]; then
    printf '[install:!] найден остаток прошлой установки в %s (без рабочего venv).\n' "$INSTALL_DIR" >&2
    read -r -p "Удалить его и установить с нуля? [Y/n]: " __a < /dev/tty; __a="${__a:-y}"
    [[ "${__a,,}" == "y" ]] || die "прервано — уберите $INSTALL_DIR вручную и повторите"
    rm -rf "$INSTALL_DIR"; rm -f "$SELF_LINK"
fi
log "создаю каталоги ($INSTALL_DIR, $ETC_DIR, $DATA_DIR)…"
mkdir -p "$INSTALL_DIR" "$ETC_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

if [[ -n "$SRC_ROOT" ]]; then
    # Сорвалась установка на полпути (нет root, не та система, отказ визарда) —
    # временный каталог, скачанный трубой, за собой всё равно убираем. Снимаем
    # ловушку перед передачей управления awg-bot.sh: дальше уборка его.
    trap '[[ $? -eq 0 ]] || case "$SRC_ROOT" in /tmp/awg-bot-install.*) rm -rf "$SRC_ROOT" ;; esac' EXIT
    verify_archive "$TGZ" "$SRC_ROOT"
    log "раскладываю код в ${INSTALL_DIR}…"
    ( shopt -s dotglob; cp -a "$SRC_ROOT"/. "$INSTALL_DIR"/ )
    # Установщик остаётся в /opt вместе с остальной поставкой: основной бот
    # собирает из своей установки поставку для шлюза (файл первого применения
    # везёт её с собой — с шлюза в России GitHub без туннеля не достать), и
    # без установщика внутри та была бы неполной. Обновление и так кладёт его.
else
    log "распаковываю код в ${INSTALL_DIR}…"
    tar xzf "$TGZ" -C "$INSTALL_DIR"
    # архив может содержать один верхний каталог — нормализуем
    if [[ ! -f "$INSTALL_DIR/awgbot/__main__.py" ]]; then
        sub="$(ls -d "$INSTALL_DIR"/*/ 2>/dev/null | head -n1 || true)"
        [[ -n "$sub" && -f "$sub/awgbot/__main__.py" ]] || die "в архиве нет awgbot/ — не та поставка?"
        ( shopt -s dotglob; mv "$sub"* "$INSTALL_DIR"/ ) && rmdir "$sub" 2>/dev/null || true
    fi
fi
[[ -f "$INSTALL_DIR/awg-bot.sh" ]] || die "в архиве нет awg-bot.sh — не та поставка?"
chmod +x "$INSTALL_DIR/awg-bot.sh"

log "симлинк $SELF_LINK → $INSTALL_DIR/awg-bot.sh"
ln -sf "$INSTALL_DIR/awg-bot.sh" "$SELF_LINK"

# ── передать управление внутреннему инструменту (он настроит и подчистит нас) ─
log "запускаю мастер настройки…"
# Убрать за собой: архив и каталог распаковки. Каталог сносим ТОЛЬКО если он
# временный (/tmp, /var/tmp) — человек мог распаковать поставку и к себе в
# домашний каталог, и удалять её там мы не вправе.
CLEANUP_DIR=""
case "${SRC_ROOT:-}" in
    /tmp/*|/var/tmp/*|/private/tmp/*) CLEANUP_DIR="$SRC_ROOT" ;;
esac
trap - EXIT
exec "$INSTALL_DIR/awg-bot.sh" reconfigure --first-run --role "$ROLE" ${EXTRA[@]+"${EXTRA[@]}"} \
     --cleanup "${CLEANUP_DIR:-$SELF_PATH}" "$TGZ"
