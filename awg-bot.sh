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
#   reconfigure [--first-run] [--role client|gateway] [--port N] [--subnet X.Y.Z]
#               [--join <токен>] [--cleanup <inst> <tgz>]
#                          мастер конфигурации (топология + секреты). --first-run —
#                          первичный прогон из установщика (ядро awg по манифесту,
#                          сервер с нуля, venv, юнит, enable+start, карта, уборка).
#                          --port/--subnet задают порт и подсеть СОЗДАВАЕМОГО
#                          сервера; без них порт случайный, подсеть 10.8.1.
#                          --advanced возвращает ВСЕ вопросы визарда (адрес, имя
#                          сервера, порт, подсеть); шифрование бэкапов —
#                          только в боте («💾 Бэкапы» → «🔐 Шифрование»).
#                          Шлюз — командой подключения из бота сервера AWG:
#                          --join [<токен>] (токен — строкой AWGJ1.… следом
#                          или окружением AWG_JOIN_TOKEN, как зовёт установщик;
#                          запасное — AWG_BOT_GW_JOIN) — venv, conf, юнит, затем
#                          tools.join с токеном в AWG_JOIN_TOKEN, не в argv
#                          (линк по токену, агент в
#                          режиме сопряжения, код подтверждения — в терминал);
#                          без токена и без стоящего агента — отказ «нет
#                          команды подключения». Python 3.12 и модуль venv
#                          (ensurepip) роль gateway готовит первым делом.
#                          deadsnakes PPA — только на Ubuntu; на
#                          Debian без python3.12 в репозиториях — отказ словами.
#                          AWG_VENV_FRESH=1 (ставит post_update перед этим
#                          прогоном) — venv только что собран, второй раз не
#                          собирается.
#   update [<tgz>]        обновить код/зависимости/юнит из архива (по умолчанию —
#                          awg-bot.tgz рядом; conf/env/данные не трогаются,
#                          если явно не согласиться на их удаление). Распаковка
#                          (--no-same-owner, код — root), проверка состава и сборка
#                          ядра — ДО остановки сервиса; прежний код отодвигается в
#                          $INSTALL_DIR/.prev-code и при отказе подмены возвращается
#                          ловушкой, сервис поднимается. Окружение Python вторая
#                          половина собирает РЯДОМ с рабочим (копия в
#                          $INSTALL_DIR/.venvs/ + pip) и переключает ссылкой venv;
#                          отказ любого шага до старта сервиса возвращает и код
#                          (.prev-code), и окружение (.venvs/.prev). Поставка
#                          старше 3.3.0 без кода адресов в туннеле — таблица
#                          inet awg_lan_map снимается при остановке сервиса.
#                          Из установщика (пункт «обновить» поверх установки):
#                          AWG_UPDATE_KEEP=1 — вопрос об удалении данных не
#                          задаётся; AWG_UPDATE_THEN_JOIN=<токен> — шлюзу после
#                          обновления подключиться командой новым кодом
#                          (`awg-bot join`, токен — в AWG_JOIN_TOKEN).
#                          AWG_UPDATE_CLEANUP=<путь> (установщик и самообновление
#                          из бота) — архив или каталог поставки убрать после
#                          обновления; только из /tmp, чужое не трогается.
#                          Поставка ниже версии совместимости установленного
#                          кода (COMPAT_FLOOR, awgbot/core/compat.py) — отказ до
#                          остановки сервиса и до вопроса об удалении данных
#                          (порядок: распаковка → версия → digest → вопрос →
#                          Python → ядро): откат — только чистой установкой.
#                          Копии обвязки (routing-host-setup.sh или
#                          routing-gw-setup.sh) и скрипта линка в
#                          /usr/local/sbin освежаются, если они там уже есть;
#                          откат неудачного обновления шлюза возвращает и
#                          прежнюю копию routing-gw-setup.sh (gw_plumbing_sync).
#                          Ключ timezone в app.yaml комментируется один раз —
#                          на переходе с выпуска ниже TZ_ADOPT_BELOW (3.4.0;
#                          update_adopts_timezone): у тех ключ писали шаблон и
#                          установщик. С 3.4.0 ключ — переопределение человека,
#                          обновление его не трогает. Снят — пояс бота =
#                          пояс хоста (adopt_timezone); прежнее значение — в
#                          метке $DATA_DIR/.tz-adopted, по ней первый старт бота
#                          скажет админу, в каком поясе теперь время; прервётся
#                          вторая половина — ключ вернётся вместе с прежним
#                          кодом (ловушка; сторож и откат набора ключа не
#                          касаются: переход с выпуска ниже 3.4.0 идёт через
#                          версию совместимости, где сторожа нет).
#                          restore: ключ копии снимается снова,
#                          если в текущем app.yaml его не было.
#                          Ловушка второй половины (post_update_rescue),
#                          вернувшая код, чей скрипт не знает __update_guard
#                          (выпуск ниже 3.4.0), убирает шаг сторожа из юнита
#                          (unit_guard_strip_for_old): юнит уже переписал новый
#                          скрипт. Pid апдейтера под замком — в
#                          $DATA_DIR/update-running (по нему бот видит, что
#                          обновление ещё идёт); файл убирают и конец
#                          обновления, и ловушка.
#                          После старта нового сервиса прежние код, окружение
#                          и сборка ядра остаются до
#                          отметки жизни нового процесса ($DATA_DIR/update-ok:
#                          основной бот — ответ Telegram на getUpdates, агент —
#                          он же или подписанное слово сервера AWG в канале):
#                          бот, поставив её, сразу зовёт фиксацию
#                          (__update_guard --alive, вне своего cgroup); сторож —
#                          таймер systemd awg-bot-update-guard через 10 минут
#                          (__update_guard). Отметка этой версии — прежнее
#                          прочь; нет — откат в той же установке: прежние код и
#                          окружение на место, перезапуск, метка
#                          $DATA_DIR/update-rolled-back для прежнего процесса.
#                          Перезагрузка в окне снимает таймер — его заменяет
#                          ExecStartPre юнита (__update_guard --start): окно не
#                          истекло — сторож заново на остаток, истекло без
#                          отметки — откат до старта сервиса.
#                          Переход через COMPAT_FLOOR (прежняя версия ниже
#                          версии совместимости нового кода) сторожем не
#                          прикрыт: прежнее снимается сразу после старта. Новое
#                          обновление поверх неподтверждённого снимает прежнего
#                          сторожа: откат — на версию, работающую сейчас.
#                          Шаг набора обновлений (AWG_UPDATE_HOLD=<id хода или
#                          команды> — ставит бот: сервер AWG или шлюз по
#                          команде сервера) после
#                          отметки жизни прежнее не снимает, а держит
#                          ($INSTALL_DIR/.hold-code, $DATA_DIR/update-hold) до
#                          фиксации набора — __update_commit (прежнее и
#                          сборки ядра прочь) или его отката — __update_revert
#                          (прежние код и окружение на место переименованием,
#                          без сети; --check — есть ли на что, код 2 — нет).
#                          Цепочка ступеней одной командой держит самое первое
#                          прежнее; шаг другого хода или команды, ручной update,
#                          wipe и переход через COMPAT_FLOOR прежнее удержание
#                          снимают. update, сторож, фиксация и откат набора —
#                          под одним замком ($DATA_DIR/update-guard.lock).
#                          sha256 архива сверяется с ассетами выпуска той же
#                          версии на GitHub (один запрос): совпало — версия и
#                          sha256 в $DATA_DIR/installed-digest, бот на старте
#                          переносит их в базу (метка бэкапа знает выпуск); нет
#                          ответа или своя сборка — файла нет.
#   uninstall              снять сервис (код, таблица учёта РФ-трафика и таблица
#                          адресов в туннеле awg_lan_map — всегда; данные,
#                          секреты, файервол и обвязка роли — по согласию: у шлюза
#                          линк до сервера AWG, юнит и таблицы, /etc/awg-gw,
#                          /var/lib/awg-gw, скрипты списков; у ВПС линки до
#                          шлюзов, таблицы маршрутизации, резолвер клиентов и
#                          резолвер админа).
#                          Что было до бота — аплинк шлюза, интерфейсы awg с
#                          клиентами — остаётся.
#   backup                 бэкап сервера AWG → $BACKUP_DIR/awg-bot-backup-main-*.tgz.enc
#                          тем же составом, что копия из чата: сборщик
#                          tools/snapshot.py (БД через backup API SQLite, conf,
#                          env, конфиги awg-интерфейсов, линки слотов и выданные
#                          шлюзовые стороны root/gw-*.conf, метка схемы 2 с
#                          описью шлюзов). Только шифрованный ключевой фразой из
#                          базы: без фразы — отказ, открытых бэкапов нет. На
#                          шлюзе — отказ: его настройки в бэкапе сервера AWG.
#   restore [--yes] [--release <архив>] [<файл>]
#                          восстановить сервер AWG из шифрованного бэкапа (по
#                          умолчанию — свежий в $BACKUP_DIR); открытый архив —
#                          отказ. --yes — без вопроса (зовёт бот: план человек
#                          видел в чате, его restore-pending.plan.json рядом с
#                          файлом сверяется после остановки — иначе отказ). Безопасность — порядком, снимка «до» нет: всё,
#                          что может отказать (расшифровка — фразой из базы для
#                          файла из чата restore-pending.tgz.enc, иначе вопросом;
#                          распаковка `tar xf`; сторож метки tools/check_backup
#                          --meta: схема 2, версия не ниже COMPAT_FLOOR,
#                          поколение AmneziaWG; версия бэкапа другая — его
#                          выпуск (tools/restore_release: выпуск vV с GitHub,
#                          sha256 = digest метки; --release — архив, уже
#                          скачанный ботом) и сборка ядра vV; план — тот же текст,
#                          что вопрос в чате, до «Продолжить?»), — до остановки
#                          сервиса. После остановки — слияние с текущим
#                          (tools/restore_merge --copy: читает живое, пишет
#                          только в распакованное; отказ — ничего не изменилось,
#                          сервис поднят), подготовка <имя>.restore-new рядом с
#                          каждой целью, DROP заблокированным (--apply-blocks)
#                          до подъёма интерфейсов, замена переименованием через
#                          <имя>.restore-old под ловушкой (отказ посреди замены
#                          — прежнее на место, сервис поднят), подъём
#                          изменившихся интерфейсов (конфиг с другими хуками
#                          Pre/Post кладётся, но не поднимается), линки пустого
#                          сервера — routing-link-setup.sh --adopt (обвязка
#                          РФ-доступа из бэкапа, которой нет на хосте, —
#                          routing-host-setup.sh до линков), DROP ещё раз,
#                          маркер итога $DATA_DIR/restore-done.json, старт.
#                          Фазы ловушки: до остановки — ничего не тронуто;
#                          остановлен — подготовленное прочь, сервис вверх;
#                          замена — прежнее из .restore-old на место, сервис
#                          вверх; после замены (точка невозврата, дубли
#                          .restore-old удалены) — только сервис вверх. Откат
#                          оставляет метку $DATA_DIR/.restore-prune (старт бота
#                          снимет лишние DROP), отказ восстановления из чата —
#                          $DATA_DIR/restore-failed.json (бот скажет админу).
#                          Временный каталог операции и файл из чата удаляются
#                          при любом исходе. На шлюзе — отказ.
#                          Переход на выпуск бэкапа: после остановки — код vV на
#                          место (прежний в .prev-code), exec в скрипт vV
#                          (__restore_after_update): окружение, юнит, conf, затем
#                          слияние и замена кодом vV. Отказ до точки невозврата —
#                          прежние код и окружение назад, сервис поднят
#                          («ничего не изменилось, работает vC»); успех —
#                          прежнее прочь сразу (сторожа нет), installed-digest =
#                          версия и digest метки. Состояние операции скрипт vC
#                          передаёт скрипту vV окружением (AWG_RESTORE_YES/TMP/
#                          PENDING/PLAN_FILE и RESTORE_*) — договор между
#                          версиями, имена не меняются. Восстановление берёт тот
#                          же замок установки, что update и сторож (занят —
#                          отказ до остановки), __restore_after_update наследует
#                          его через exec.
#   Служебные глаголы (руками не нужны): __post_update — вторая половина
#                          update новым скриптом; __update_guard [--alive|--start]
#                          — сторож (таймер awg-bot-update-guard, бот по отметке
#                          жизни, ExecStartPre юнита); __update_commit и
#                          __update_revert [--check] — фиксация и откат набора
#                          обновлений (зовёт бот: сервер AWG — сам, шлюз — по
#                          команде сервера); __restore_after_update — вторая
#                          половина restore с переходом на выпуск бэкапа.
#   join [<токен>] | --status
#                          шлюз: подключение к серверу AWG командой из его бота
#                          (tools/join.py). Токен — строкой руками или
#                          окружением AWG_JOIN_TOKEN (установщик, обновление с
#                          подключением); tools.join получает его только
#                          окружением, не в argv. Линк по токену и карантин
#                          (routing-gw-setup.sh --join), файл сопряжения
#                          /etc/awg-gw/join.json, перезапуск агента в режиме
#                          сопряжения, ход и код — из /run/awg-bot/join.status.
#                          --status — код и ход ещё раз. На сервере AWG — отказ
#                          (кроме --status); первую установку агента ведёт
#                          установщик (--join), не этот глагол.
#   gw-kit --stdout        сервер AWG: поставка для шлюза без доступа к GitHub
#                          (tools/gwkit.py) — код этой установки и исходники
#                          AmneziaWG из кэша сборки ядра, без ключей и токенов;
#                          в stdout, не в терминал. Годится, только когда
#                          команда подключения ставит ту же версию, что у
#                          сервера (иначе отказ). На шлюзе — отказ. Файла
#                          первого применения с 3.4.0 нет: ни глагола
#                          gw-bundle, ни ключа reconfigure и переменных
#                          окружения для него.
#   status                 состояние сервиса и пути.
#   logs                   журнал сервиса (follow, время journald со смещением).

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

ADVANCED="${AWG_BOT_ADVANCED:-0}"       # 1 — спрашивать всё, как раньше
# от установщика при обновлении поверх прошлой установки: не спрашивать про
# удаление данных; шлюзу после обновления — подключение командой
AWG_UPDATE_KEEP="${AWG_UPDATE_KEEP:-0}"
AWG_UPDATE_THEN_JOIN="${AWG_UPDATE_THEN_JOIN:-}"    # шлюз: после обновления — подключение командой (установщик --join)
GW_JOIN="${AWG_BOT_GW_JOIN:-}"
AWG_UPDATE_CLEANUP="${AWG_UPDATE_CLEANUP:-}"        # временный каталог с архивом — убрать после обновления
# шаг набора (бот: apply_update(hold=<id>)): прежние код и окружение держатся до
# фиксации набора (__update_commit) или его отката (__update_revert); значение —
# id хода сервера или команды шлюза («0» или пусто — не шаг набора)
AWG_UPDATE_HOLD="${AWG_UPDATE_HOLD:-0}"
AWG_STATE="$ETC_DIR/awg.state"          # поколение AmneziaWG на этом хосте
AWG_LOCK_FILE="$INSTALL_DIR/install/awg.lock"   # манифест версии из поставки

# Реальный путь к этому скрипту (для update-по-умолчанию и uninstall self-removal).
SELF_PATH="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)/$(basename "$(readlink -f "${BASH_SOURCE[0]}")")"
SELF_DIR="$(dirname "$SELF_PATH")"

# ── лог/утилиты ──────────────────────────────────────────────────────────────
c_info=$'\033[0;36m'; c_ok=$'\033[0;32m'; c_warn=$'\033[0;33m'; c_err=$'\033[0;31m'; c_off=$'\033[0m'
log()  { printf '%s[awg-bot]%s %s\n' "$c_info" "$c_off" "$*"; }
ok()   { printf '%s[awg-bot]%s %s\n' "$c_ok"   "$c_off" "$*"; }
warn() { printf '%s[awg-bot:!]%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s[awg-bot:ОШИБКА]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }

# ВСЕ вопросы читаются из /dev/tty, а не из stdin. Установка запускается одной
# командой через трубу (`curl … | sudo bash`), и там stdin занят текстом
# скрипта: обычный `read` «прочитал» бы его вместо ответа человека и молча
# ушёл по умолчаниям — с токеном бота из случайной строки кода.
ask() {  # ask VAR "prompt" "default"  — пустой ввод (Enter) берёт default
    local __v="$1" __p="$2" __d="${3:-}" __a
    if [[ -n "$__d" ]]; then read -r -p "$__p [$__d]: " __a < /dev/tty; __a="${__a:-$__d}"
    else read -r -p "$__p: " __a < /dev/tty; fi
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
    read -r -p "$p $hint: " a < /dev/tty; a="${a:-$d}"
    [[ "${a,,}" == "y" ]]
}
require_root() { [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root: sudo awg-bot ${VERB:-}"; }
require_installed() { [[ -x "$INSTALL_DIR/venv/bin/python" ]] || die "awg-bot не установлен в $INSTALL_DIR (сначала внешний установщик)"; }

# Заменить значение КОНКРЕТНОГО ключа yaml, сохранив отступ и комментарии.
yaml_set_soft() {  # как yaml_set, но отсутствие ключа — предупреждение, а не смерть
    # Нужно там, где правка идёт ВНУТРИ обновления: конфиг боевого сервера не
    # мигрирует, и ключ, появившийся в шаблоне позже, у него просто
    # отсутствует. Смерть здесь означала бы прерванный post_update — код уже
    # подменён, сервис ещё не поднят.
    local file="$1" key="$2" val="$3"
    if ! grep -qE "^[[:space:]]*${key}:" "$file"; then
        warn "в $file нет ключа '$key' — пропускаю (конфиг старше поставки; задай вручную)"
        return 0
    fi
    yaml_set "$file" "$key" "$val"
}
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
ensure_ping() {
    # «Пинг до шлюза» в карточках слотов — ICMP по линку; iputils-ping на
    # минимальных образах может отсутствовать
    command -v ping >/dev/null 2>&1 && return 0
    log "ставлю iputils-ping (пинг до шлюза по линку)…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q iputils-ping >/dev/null 2>&1 \
        || warn "iputils-ping не установился — «📡 Пинг» в карточке шлюза ответит «шлюз не отвечает»"
}
ensure_python() {
    if detect_python; then log "Python: $PYBIN ($("$PYBIN" -V 2>&1))"; return; fi
    warn "не найден Python 3.12+ (нужен для StrEnum)."
    if command -v apt-get >/dev/null 2>&1 && confirm "Установить python3.12 через apt?" y; then
        apt-get update -q || true
        if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -o DPkg::Lock::Timeout=120 python3.12 python3.12-venv 2>/dev/null; then
            # deadsnakes — PPA Ubuntu: на Debian он писал битый источник в apt
            if [[ "$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")" == "ubuntu" ]]; then
                log "нет в штатных репозиториях — подключаю deadsnakes PPA…"
                apt-get install -y -o DPkg::Lock::Timeout=120 software-properties-common
                add-apt-repository -y ppa:deadsnakes/ppa; apt-get update -q || true
                apt-get install -y -o DPkg::Lock::Timeout=120 python3.12 python3.12-venv
            else
                die "в репозиториях этой системы нет Python 3.12: нужен Debian 13 (trixie) или Ubuntu 22.04+"
            fi
        fi
        detect_python || die "python3.12 так и не появился — поставьте вручную."
        log "Python: $PYBIN"
    else
        die "поставьте Python 3.12+ вручную и повторите."
    fi
}
ensure_venv_ready() {
    # Модуль venv (ensurepip) — ДО применения файла конфигурации: тот удаляет
    # себя после успеха, и отказ venv после него повторить нечем (trixie без
    # python3-venv, /tmp на tmpfs).
    "$PYBIN" -c 'import ensurepip' >/dev/null 2>&1 && return 0
    command -v apt-get >/dev/null 2>&1 || die "у $PYBIN нет модуля venv (ensurepip) — поставь python3-venv и повтори"
    local ver; ver="$("$PYBIN" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
    log "нет модуля venv у $PYBIN — ставлю python${ver}-venv…"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 "python${ver}-venv" >/dev/null 2>&1 \
        || DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 python3-venv >/dev/null 2>&1 \
        || die "python3-venv не установился — без него venv не собрать"
    "$PYBIN" -c 'import ensurepip' >/dev/null 2>&1 || die "ensurepip так и не появился — поставь python3-venv вручную"
}

# ── apply-runtime: venv + юнит + валидация (общее для first-run и update) ─────
# venv — ссылка на каталог в .venvs/. Окружение собирается РЯДОМ с рабочим
# (копия рабочего, если оно есть, — тогда pip докачивает только изменившееся),
# ссылка переключается на него, прежнее живёт до конца обновления: отказ
# pip ничего не трогает, поздний отказ обновления возвращает и код, и
# окружение (venv_rollback). Раньше pip апгрейдил живой venv на месте, и
# упавшая сборка оставляла его полусобранным под новым кодом.
venv_build_next() {  # → VENV_NEXT: каталог собранного окружения; 1 — не собралось (прежнее не тронуто)
    VENV_NEXT="$INSTALL_DIR/.venvs/$(date +%Y%m%d-%H%M%S)-$$"
    mkdir -p "$INSTALL_DIR/.venvs"
    local cur=""; cur="$(readlink -f "$INSTALL_DIR/venv" 2>/dev/null)" || cur=""
    if [[ -n "$cur" && -x "$cur/bin/python" && "$("$cur/bin/python" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)" \
          == "$("$PYBIN" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)" ]]; then
        log "копирую рабочее окружение рядом…"
        cp -a "$cur" "$VENV_NEXT" || { rm -rf "$VENV_NEXT"; return 1; }
    else
        log "создаю окружение ($PYBIN)…"
        "$PYBIN" -m venv "$VENV_NEXT" || { rm -rf "$VENV_NEXT"; return 1; }
    fi
    log "ставлю зависимости (pip install --upgrade -r requirements.txt)…"
    # --upgrade: без него pip оставляет то, что стояло при первой установке, и
    # окружение на хосте застывает во времени — данные таймзон, исправления в
    # библиотеках. Верхние границы в requirements.txt держат мажоры на месте.
    # Через python -m pip: консольные скрипты копии смотрят в прежний каталог.
    if ! { "$VENV_NEXT/bin/python" -m pip install --quiet --no-cache-dir --upgrade pip \
           && "$VENV_NEXT/bin/python" -m pip install --quiet --no-cache-dir --upgrade -r "$INSTALL_DIR/requirements.txt"; }; then
        rm -rf "$VENV_NEXT"; return 1
    fi
}
venv_switch() {  # venv_switch DIR — ссылка venv на DIR; прежняя цель записана в .venvs/.prev
    local dir="$1" link="$INSTALL_DIR/venv" prev=""
    if [[ -L "$link" ]]; then
        prev="$(readlink -f "$link")"
    elif [[ -d "$link" ]]; then
        # venv — каталог (ручная установка, README §12, или установка до 3.3.0).
        # Отодвигается как есть: интерпретатор и пакеты работают и с нового
        # места, откат вернёт его обратно
        prev="$INSTALL_DIR/.venvs/legacy"; rm -rf "$prev"; mv "$link" "$prev"
    fi
    printf '%s\n' "$prev" > "$INSTALL_DIR/.venvs/.prev"
    ln -sfn "$dir" "$link"                 # сервис в этот момент стоит: атомарность не нужна
}
venv_rollback() {  # ссылка venv — обратно на прежнее окружение (.venvs/.prev)
    local prev="" link="$INSTALL_DIR/venv"
    prev="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null)" || return 0
    if [[ -n "$prev" && -d "$prev" ]]; then
        if [[ "$prev" == "$INSTALL_DIR/.venvs/legacy" ]]; then
            rm -f "$link"; mv "$prev" "$link"
        else
            ln -sfn "$prev" "$link"
        fi
    fi
    rm -f "$INSTALL_DIR/.venvs/.prev"
}
venv_prune() {  # оставить окружение, на которое смотрит ссылка (и то, что держат шаг набора и сторож)
    local cur="" keep="" guarded="" d
    cur="$(readlink -f "$INSTALL_DIR/venv" 2>/dev/null)" || return 0
    keep="$(hold_get ENV 2>/dev/null)" || keep=""
    # взведённый сторож откатит на .venvs/.prev — его окружение не трогаем
    [[ -f "${UPDATE_GUARD:-}" ]] && guarded="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null || true)"
    for d in "$INSTALL_DIR/.venvs"/*; do
        [[ -d "$d" && "$d" != "$cur" && "$d" != "$keep" && "$d" != "$guarded" ]] && rm -rf "$d"
    done
    [[ -n "$guarded" ]] || rm -f "$INSTALL_DIR/.venvs/.prev"
}
build_venv() {
    if [[ "${AWG_VENV_FRESH:-}" == "1" && -x "$INSTALL_DIR/venv/bin/python" ]]; then
        log "venv только что собран обновлением — пропускаю"; return 0   # slogic-9: не дважды
    fi
    venv_build_next || die "окружение Python не собралось (pip) — рабочее не тронуто"
    venv_switch "$VENV_NEXT"
    # вне обновления откатывать некуда и незачем: прежнее — сразу долой;
    # обновление чистит его само, когда довело дело до конца
    [[ "${AWG_IN_UPDATE:-}" == "1" ]] || venv_prune
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
    log "ставлю systemd-юнит ${UNIT_PATH}…"
    # Зависимость от docker ставим ТОЛЬКО в докерном режиме. В host-режиме бот к
    # docker не обращается вовсе, а `Requires=docker.service` там вреден дважды:
    # docker нельзя удалить (юнит перестанет стартовать), и падение докера
    # утащило бы за собой бота, который от него не зависит. Юнит переписывается
    # на каждом update — значит режим доедет сюда сам, править руками не нужно.
    local docker_after="" docker_req=""
    if [[ "$(yaml_get "$CONF_DIR/app.yaml" runtime)" != "host" ]]; then
        docker_after=" docker.service"
        docker_req=$'\nRequires=docker.service'
    fi
    # На шлюзе таблицу awg_gw_guard ставит юнит обвязки, и живёт она только в
    # ядре — после ребута её восстанавливает он же. Без упорядочивания агент
    # успевал сделать проверки раньше и первым сообщением после каждой
    # перезагрузки говорил «таблицы нет, шлюз открыт клиентам туннеля».
    # Wants, а не Requires: не отработавшая обвязка не должна уносить агента —
    # через него её и чинят.
    local link_after="" link_want=""
    if [[ "$(yaml_get "$CONF_DIR/app.yaml" role)" == "gateway" ]]; then
        link_after=" awg-link-gw.service"; link_want=" awg-link-gw.service"
    fi
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=AmneziaWG Telegram bot
After=network-online.target${docker_after}${link_after}
Wants=network-online.target${link_want}${docker_req}

[Service]
Type=simple
# Бот работает от root: правит iptables и конфиг awg.
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
Environment=AWG_BOT_CONF_DIR=$CONF_DIR
Environment=AWG_BOT_DATA_DIR=$DATA_DIR
# сторож обновления, потерянный перезагрузкой: окно истекло без отметки
# жизни — откат до старта (awg-bot.sh __update_guard --start); «-» — отказ
# проверки не держит сервис
ExecStartPre=-$INSTALL_DIR/awg-bot.sh __update_guard --start
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

configure_topology_auto() {
    # Свежая установка без вопросов: адрес хоста определяем сами, имя сервера
    # берём дефолтное, порт с подсетью уже записал ensure_awg_server. Всё это
    # правится потом в боте (Настройки → Сервер) и командой reconfigure —
    # спрашивать об этом в первую минуту знакомства незачем.
    local app="$CONF_DIR/app.yaml" host name
    host="$(yaml_get "$app" server_host)"
    if [[ -z "$host" ]]; then
        host="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' || true)"
        # За 1:1 NAT (AWS, GCP, Oracle, Azure) локальный адрес приватный, а в
        # ссылку нужен публичный. Молча вписав приватный, мы выдали бы первому
        # же устройству нерабочий Endpoint — и ровно тому, кто ставит VPN,
        # потому что не может открыть Telegram.
        case "$host" in
            10.*|192.168.*|127.*|169.254.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*)
                local _pub
                _pub="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
                if [[ -n "$_pub" && "$_pub" != "$host" ]]; then
                    log "локальный адрес $host приватный (хост за NAT) — беру внешний $_pub"
                    host="$_pub"
                fi ;;
        esac
        [[ -n "$host" ]] || host="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
        [[ -n "$host" ]] || { warn "внешний адрес не определился — спрошу"; configure_topology; return; }
        yaml_set "$app" server_host "\"$host\""
    fi
    name="$(yaml_get "$app" server_name)"
    [[ -n "$name" ]] || yaml_set "$app" server_name "\"Сервер 1\""
    # Часовой пояс в app.yaml не пишем: без ключа timezone бот берёт пояс
    # хоста и, в отличие от записанного значения, следует за timedatectl.
    ok "адрес сервера: $host (правится в боте: ⚙️ Настройки → 🖥 Сервер AWG → ✏️ Домен)"
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
    # Контейнер ищем только в докерном режиме: на хосте сервер создаём мы сами
    # (ensure_awg_server), и «живого контейнера не найдено» было бы не
    # предупреждением, а недоумением на ровном месте.
    if [[ "$(yaml_get "$app" runtime)" == "docker" ]]; then
        cont="$(_detect_awg_container | head -n1)"
    fi
    if [[ -n "$cont" ]]; then
        det="$(cd "$INSTALL_DIR" && AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
            ./venv/bin/python -c "
from awgbot.infra import awg
t = awg.detect_topology('$cont')
print('%s|%s|%s' % (t['listen_port'] or '', t['subnet_prefix'] or '', t['subnet_cidr'] or ''))
" 2>/dev/null || true)"
        d_port="${det%%|*}"; local d_rest="${det#*|}"
        d_prefix="${d_rest%%|*}"; d_cidr="${d_rest#*|}"
    elif [[ -z "$cur_port" || -z "$cur_prefix" ]]; then
        warn "порт/подсеть awg не определены — спрошу их ниже."
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

pair_admin() {  # pair_admin TOKEN → печатает id админа или пусто
    # Числового Telegram ID у человека под рукой нет: за ним идут к стороннему
    # боту и переписывают цифры. Спрашиваем не человека, а Telegram — кто
    # пришлёт код в чат, тот и админ.
    local out id
    out="$( cd "$INSTALL_DIR" && BOT_TOKEN="$1" ./venv/bin/python -m tools.pair --timeout 600 )" || return 1
    id="$(sed -nE 's/^ADMIN_ID=([0-9]+)$/\1/p' <<<"$out" | head -n1)"
    [[ -n "$id" ]] || return 1
    printf '%s' "$id"
}

setup_secrets() {
    local cur_token cur_admin token admin
    mkdir -p "$ETC_DIR"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
    cur_token="$(env_get BOT_TOKEN)"; cur_admin="$(env_get ADMIN_ID)"
    if [[ -n "$cur_token" ]] && ! confirm "BOT_TOKEN уже задан — заменить?" n; then token="$cur_token"
    else while :; do ask_masked token "Токен бота (от @BotFather)"; [[ -n "$token" ]] && break; warn "пусто"; done; fi
    env_set BOT_TOKEN "$token"

    if [[ -n "$cur_admin" ]] && ! confirm "ADMIN_ID уже задан ($cur_admin) — заменить?" n; then
        admin="$cur_admin"
    else
        admin=""
        if [[ "$ADVANCED" != "1" ]]; then
            admin="$(pair_admin "$token" || true)"
            [[ -n "$admin" ]] || warn "по коду опознать не вышло — спрошу ID вручную."
        fi
        while [[ -z "$admin" ]]; do
            ask admin "Telegram ID администратора (число, у @userinfobot)"
            [[ "$admin" =~ ^[0-9]+$ ]] || { warn "должно быть числом"; admin=""; }
        done
    fi
    env_set ADMIN_ID "$admin"
    ok "секреты записаны в $ENV_FILE (600)."
    # Шифрование бэкапов — экран в боте (⚙️ Настройки → 💾 Бэкапы → 🔐 Шифрование):
    # в визарде это был бы вопрос ровно там, где человек ещё не знает, что
    # такое бэкап этого бота.
}

optional_steps() {
    echo; log "─── Файервол ───"
    # Один вопрос вместо мастера, и ответ на него уже подставлен: адрес, с
    # которого человек сейчас подключён, система знает сама. Третий вариант —
    # свой список: у кого-то вход с нескольких адресов или по DynDNS-имени.
    local here="" ans allow
    # SSH_CONNECTION под sudo не переживает env_reset (его нет в env_keep ни в
    # Debian, ни в Ubuntu), а установка документирована как `curl | sudo bash`.
    # Поэтому второй источник — utmp: `who am i` печатает адрес в скобках.
    here="$(awk '{print $1}' <<<"${SSH_CONNECTION:-}")"
    [[ -n "$here" ]] || here="$(who am i 2>/dev/null | sed -nE 's/.*\(([0-9a-fA-F.:]+)\).*/\1/p' | head -n1)"
    [[ -n "$here" ]] || here="$(awk '{print $1}' <<<"${SSH_CLIENT:-}")"
    local prompt="Ограничить доступ по SSH"
    [[ -n "$here" ]] && prompt="$prompt адресом, с которого вы сейчас подключены ($here)"
    read -r -p "$(printf '%s [y / свой список адресов / N]: ' "$prompt")" ans < /dev/tty || ans=""
    case "${ans,,}" in
        y|yes|д|да)
            [[ -n "$here" ]] || { warn "адрес подключения не определился (не SSH-сессия?) — пропускаю"; return 0; }
            allow="$here" ;;
        ""|n|no|н|нет) log "файервол не трогаю: включить позже — в боте (⚙️ Настройки → 🛡 SSH-доступ → «Фильтр снаружи») или awg-bot firewall setup"; return 0 ;;
        *) allow="$ans" ;;
    esac
    # Таймер отката длиннее штатного: человек ещё в середине установки, и трёх
    # минут на «проверь вход новым подключением» ему мало.
    cmd_firewall setup --allow "$allow" --yes --rollback-seconds 900 \
        || warn "файервол не включён — позже: в боте, ⚙️ Настройки → 🛡 SSH-доступ → «Фильтр снаружи»"
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
    echo "     awg-bot backup              шифрованный бэкап сервера AWG (нужна ключевая фраза)"
    echo "     awg-bot restore [файл]      восстановить из бэкапа со слиянием"
    echo "     awg-bot uninstall           снять сервис"
    echo "     awg-bot awg status          версия AmneziaWG против манифеста поставки"
    echo "     awg-bot first-device        конфигурация первого устройства в терминал"
    ok "══════════════════════════════════════════════════════════════════"
}

# ── reconfigure ──────────────────────────────────────────────────────────────
# ── роль gateway: агент на шлюзе (docs/ROADMAP.md, п.7) ──────────────────────
configure_gateway() {
    # Раскомментировать блок роли из шаблона (yaml_set правит только существующие
    # ключи, а в шаблоне они закомментированы намеренно — свежая установка
    # клиентской роли не должна получить чужую секцию).
    local app="$CONF_DIR/app.yaml"
    [[ -f "$app" ]] || die "нет $app — сначала seed_conf"
    sed -i -E 's/^# (role: "gateway")/\1/; s/^# (gateway:)/\1/' "$app"
    sed -i -E 's/^#   ((link_interface|conf_dir|unit|client_subnet|monitor_minutes|handshake_max_age|link_alert_streak|temp_alert_c|ipv4_only):)/  \1/' "$app"
    yaml_set "$app" runtime '"host"'
    # подсеть клиентов не спрашиваем: она приезжает в конфигурации с ВПС и
    # закрепляется в юните обвязки, агент читает её оттуда
    ok "роль gateway записана в $app"
}

cleanup_delivery() {  # cleanup_delivery <каталог-или-файл> <архив>
    # Поставка едет одним архивом и распаковывается во временный каталог —
    # после установки от неё на диске остаться не должно ничего: внутри и код,
    # и установщик. Каталог сносим только временный: проверку «это /tmp» делает
    # вызывающий (bootstrap), здесь — ещё одна, потому что rm -rf.
    local what="${1:-}" tgz="${2:-}"
    if [[ -n "$what" && -d "$what" ]]; then
        case "$what" in
            /tmp/*|/var/tmp/*|/private/tmp/*)
                rm -rf "$what" && log "убрал каталог распаковки: $what" ;;
            *) log "каталог поставки оставлен (он не временный): $what" ;;
        esac
    elif [[ -n "$what" && -f "$what" ]]; then
        rm -f "$what" && log "удалён установщик: $what"
    fi
    [[ -n "$tgz" && -f "$tgz" ]] && { rm -f "$tgz" && log "удалён архив: $tgz"; }
    return 0
}

cmd_reconfigure() {
    local first_run=0 cleanup_inst="" cleanup_tgz="" role="client"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --first-run) first_run=1; shift ;;
            --advanced)  ADVANCED=1; shift ;;
            --join)      # токен — из окружения (AWG_JOIN_TOKEN, установщик), в argv его нет
                         if [[ "${2:-}" == AWGJ1.* ]]; then GW_JOIN="$2"; shift 2; else GW_JOIN="${AWG_JOIN_TOKEN:-$GW_JOIN}"; shift; fi ;;
            --role)      role="${2:-}"; shift 2 ;;
            --port)      # порт awg для СОЗДАВАЕМОГО сервера — без advanced-режима
                         [[ "${2:-}" =~ ^[0-9]+$ && "$2" -ge 1 && "$2" -le 65535 ]] || die "--port: число 1..65535"
                         export LISTEN_PORT="$2"; shift 2 ;;
            --subnet)    # подсеть создаваемого сервера, первые три октета (X.Y.Z)
                         [[ "${2:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--subnet: три октета вида 10.8.1"
                         export SUBNET_PREFIX="$2"; shift 2 ;;
            --cleanup)   cleanup_inst="${2:-}"; cleanup_tgz="${3:-}"; shift 3 ;;
            *) die "reconfigure: неизвестный аргумент '$1'" ;;
        esac
    done
    require_root
    [[ -f "$INSTALL_DIR/awgbot/__main__.py" ]] || die "код не распакован в $INSTALL_DIR (запусти внешний установщик)"
    [[ "$role" == "client" || "$role" == "gateway" ]] || die "role: client или gateway"

    if [[ "$role" == "gateway" ]]; then
        # Свой путь целиком: ни визарда топологии (шлюз не выдаёт конфигов),
        # ни email-выхода. Общее с клиентской ролью — python, venv, секреты,
        # юнит — то же самое, теми же функциями. Ядро awg — по манифесту
        # поставки, обеим ролям одинаково.
        ensure_awg_kernel
        ensure_python
        ensure_venv_ready
        if [[ -n "${GW_JOIN:-}" ]]; then
            # Подключение командой из бота сервера AWG: токена бота и ADMIN_ID
            # нет — их довезёт канал после кода; агент стартует в режиме
            # сопряжения, ход и код — в терминал (awg-bot join)
            build_venv
            mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR"
            seed_conf
            configure_gateway
            mkdir -p "$ETC_DIR"; touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
            install_unit
            systemctl enable "$SERVICE" >/dev/null 2>&1 || warn "не удалось включить $SERVICE на автозагрузку"
            cleanup_delivery "$cleanup_inst" "$cleanup_tgz"
            AWG_JOIN_TOKEN="$GW_JOIN" bot_tool tools.join
            return
        fi
        # Перенастройка стоящего агента: токен бота и ADMIN_ID уже в env (их
        # довёз канал после кода); нового агента без команды не поставить
        if [[ -z "$(env_get BOT_TOKEN)" ]]; then
            die "нет команды подключения. Выпусти её в боте сервера AWG
  («🛰 Шлюзы» → «🛰 Подключить шлюз») и выполни здесь — одной строкой"
        fi
        build_venv
        mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR"
        seed_conf
        configure_gateway
        setup_secrets
        validate_config
        install_unit
        # Включение — ОТДЕЛЬНО от запуска. Слитое `enable --now || restart`
        # при упавшем первом старте (Telegram через туннель ещё недоступен)
        # уходило в запасную ветку, и автозагрузка терялась молча: ребут
        # шлюза оставил его без агента.
        systemctl enable "$SERVICE" >/dev/null 2>&1 || warn "не удалось включить $SERVICE на автозагрузку"
        systemctl restart "$SERVICE"; sleep 1
        systemctl is-active --quiet "$SERVICE" && ok "$SERVICE (агент шлюза) запущен." \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
        cleanup_delivery "$cleanup_inst" "$cleanup_tgz"
        ok "Готово: бот шлюза напишет сам."
        return
    fi

    if [[ "$first_run" -eq 1 ]]; then
        ensure_awg_kernel
        ensure_python
        ensure_ping
        build_venv
        mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR"
        seed_conf
        ensure_awg_server                  # чистый хост: сервер с нуля; есть — не трогаем
        ensure_resolver                    # свой DNS клиентам: dnsmasq на <подсеть>.1
        if [[ "$ADVANCED" == "1" ]]; then
            configure_topology             # --advanced: все вопросы, как раньше
        else
            configure_topology_auto        # обычный путь: ни одного вопроса
        fi
        setup_secrets
        validate_config
        install_unit
        log "включаю и запускаю сервис…"
        systemctl enable --now "$SERVICE"; sleep 1
        local svc_state="failed"
        systemctl is-active --quiet "$SERVICE" && { svc_state="active"; ok "$SERVICE запущен."; } \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
        optional_steps
        print_map "$svc_state"
        # Конфигурация первого устройства — в терминал: у человека прямо сейчас
        # может не открываться Telegram, он ровно за этим ставит VPN. Бот то же
        # самое уже прислал в чат, и там же оно останется.
        if [[ "$svc_state" == "active" ]]; then
            sleep 3                        # боту нужен тик, чтобы завести устройство
            cmd_first_device || log "конфигурация первого устройства будет в чате бота"
        fi
        # подчистить временное: каталог распаковки (или файл установщика) и
        # архив. Мы — уже exec'нутый процесс, эти файлы никем не держатся.
        cleanup_delivery "$cleanup_inst" "$cleanup_tgz"
    else
        require_installed
        seed_conf                          # доложить недостающие шаблоны, существующие не трогать
        configure_topology                 # перенастройка поверх существующего
        setup_secrets
        validate_config
        log "перезапускаю ${SERVICE}…"; systemctl restart "$SERVICE" 2>/dev/null || true
        systemctl is-active --quiet "$SERVICE" && ok "$SERVICE перезапущен." \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
        ok "Переконфигурация завершена."
    fi
}

# ── update ───────────────────────────────────────────────────────────────────
locate_tgz() {  # locate_tgz DEFAULT_NAME EXPLICIT → печатает путь или пусто
    # Имя по умолчанию — как у ассета релиза (awg-bot.tgz): его человек и
    # скачивает руками; ищем в текущем каталоге и рядом со скриптом.
    local name="$1" explicit="${2:-}" d
    if [[ -n "$explicit" ]]; then [[ -f "$explicit" ]] && { echo "$explicit"; return; }; die "архив не найден: $explicit"; fi
    for d in "$(pwd)" "$SELF_DIR"; do
        [[ -f "$d/$name" ]] && { echo "$d/$name"; return; }
    done
    return 0
}
cmd_update() {
    require_root; require_installed
    # одно обновление за раз: второй апдейтер (повтор шага набора, ручной поверх
    # кнопки) не начинает, пока первый не довёл; замок переходит в __post_update
    update_lock -n || die "уже идёт обновление, фиксация или откат — дождись их конца"
    local tgz; tgz="$(locate_tgz "awg-bot.tgz" "${1:-}")"
    [[ -n "$tgz" ]] || die "не найден awg-bot.tgz (в текущем каталоге или рядом со скриптом); укажи путь: awg-bot update <tgz>"
    log "обновление из: $tgz"

    # Всё, что может отказать, — ДО остановки сервиса: распаковка, проверка
    # состава, сборка ядра. Пока это идёт, работает прежний код, и любой отказ
    # ничего не ломает — сервис даже не трогали. Версия поставки проверяется
    # раньше вопросов об удалении данных: отказ «откат невозможен» не должен
    # идти после «Удалить данные?»
    local tmp; tmp="$(mktemp -d)"
    UPDATE_TMP="$tmp"                           # ловушке нужна глобальная: EXIT срабатывает уже вне функции
    trap 'rm -rf "${UPDATE_TMP:-}"' EXIT        # отказ до остановки сервиса — распакованное прочь
    log "распаковываю новый код…"
    # --no-same-owner: архив несёт uid машины, где его собрали; код в /opt — только root
    tar xzf "$tgz" -C "$tmp" --no-same-owner || { rm -rf "$tmp"; die "архив не распаковался: $tgz"; }
    local main; main="$(find "$tmp" -maxdepth 3 -type f -path '*/awgbot/__main__.py' | head -n1)"
    [[ -n "$main" ]] || { rm -rf "$tmp"; die "в архиве нет awgbot/ — не та поставка?"; }
    local src; src="$(dirname "$(dirname "$main")")"
    chown -R root:root "$src"
    # Версия совместимости — граница, ниже которой этот код не возвращается:
    # копии ниже неё не применяются, а откат — только чистой установкой.
    local floor newver
    floor="$(compat_floor)"; newver="$(tree_version "$src")"
    if [[ -n "$floor" && -n "$newver" ]] && version_lt "$newver" "$floor"; then
        rm -rf "$tmp"
        die "откат ниже $floor невозможен — только чистая установка"
    fi
    note_installed_digest "$tgz" "$newver"     # архив из выпуска — бэкапы будут знать выпуск

    local wipe=0
    # AWG_UPDATE_KEEP=1 — установщик уже выбрал «обновить, сохранив данные»:
    # второй раз про удаление не спрашиваем
    if [[ "${AWG_UPDATE_KEEP:-0}" != "1" ]] \
       && confirm "Удалить пользовательские данные (БД в $DATA_DIR + секреты $ENV_FILE + конфиг)?" n; then
        confirm "Точно удалить ВСЕ данные и настройки? Это НЕОБРАТИМО." n && wipe=1 || log "данные оставлены."
    fi

    ensure_python

    # ЯДРО — ДО подмены кода. Версия AmneziaWG прибита к поставке, значит её
    # надо собрать; а собирать надо ПЕРВЫМ делом, пока на диске рабочая
    # установка: не собралось (нет заголовков, апстрим переименовал цель) —
    # ничего не подменяем и сообщаем причину. Иначе хост остался бы с новым
    # кодом и старым ядром, то есть в состоянии, которого мы нигде не проверяем.
    if [[ -f "$src/install/awg.lock" ]]; then
        log "AmneziaWG: версия из поставки — собираю до подмены кода…"
        if ! AWG_LOCK="$src/install/awg.lock" bash "$src/install/awg-kernel-install.sh" install; then
            rm -rf "$tmp"
            die "ядро AmneziaWG из поставки не собралось — код НЕ подменён, версия прежняя, сервис не трогали"
        fi
    fi

    # Сторож прошлого обновления, если ещё стоит, — прочь: его прежний код
    # уходит ниже, откат теперь — на версию, работающую сейчас
    update_guard_disarm
    log "останавливаю ${SERVICE}…"; systemctl stop "$SERVICE" 2>/dev/null || true
    lanmap_drop_for_older "$src"
    # Старый код — в сторону, а не под нож: отказ копирования (кончилось место,
    # битый архив) возвращает его на место и поднимает сервис. Ловушка — сразу
    # после остановки: с этого момента любой выход не по плану = откат.
    UPDATE_PREV="$INSTALL_DIR/.prev-code"; UPDATE_TMP="$tmp"
    rm -rf "$UPDATE_PREV"; mkdir -p "$UPDATE_PREV"
    trap update_rescue EXIT
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec mv -t "$UPDATE_PREV" {} + \
        || die "прежний код не отодвинулся — обновление отменено"
    cp -a "$src"/. "$INSTALL_DIR"/ || die "новый код не лёг целиком — возвращаю прежний"
    chmod +x "$INSTALL_DIR/awg-bot.sh" 2>/dev/null || true
    [[ -x "$INSTALL_DIR/awg-bot.sh" && -f "$INSTALL_DIR/awgbot/__main__.py" ]] || die "новый код лёг не полностью — возвращаю прежний"
    trap - EXIT
    # прежний код остаётся в .prev-code до конца второй половины: её отказ
    # вернёт и его, и окружение (post_update_rescue)
    rm -rf "$tmp"
    export AWG_UPDATE_PREV="$UPDATE_PREV"

    # Дальше — руками НОВОГО скрипта, а не этого.
    #
    # Bash держит функции в памяти процесса с момента разбора файла. Мы только
    # что подменили awg-bot.sh на диске, но install_unit/seed_conf/build_venv в
    # этом процессе — по-прежнему из СТАРОЙ версии. То есть любая их правка не
    # может применить сама себя: она доезжает до сервера и молчит ещё одно
    # обновление. Ровно так юнит остался с Requires=docker.service на сервере,
    # уже переехавшем на host-режим.
    #
    # exec, а не вызов: старому процессу после подмены кода делать нечего, а
    # продолжать читать с диска файл, который сменился под ним, — отдельный
    # способ получить мусор в разборе оставшегося хвоста.
    exec "$INSTALL_DIR/awg-bot.sh" __post_update "$wipe"
}

INSTALLED_DIGEST_FILE="$DATA_DIR/installed-digest"   # «<версия> <sha256>» архива из выпуска — бот перенесёт в базу

release_sha256s() {  # release_sha256s VER → sha256 ассетов выпуска vVER на GitHub (hex, по строке); пусто — нет сети или выпуска
    local repo
    repo="$(yaml_get "$CONF_DIR/updates.yaml" repo)"
    [[ -n "$repo" ]] || repo="$(yaml_get "$INSTALL_DIR/conf/updates.yaml" repo)"
    [[ -n "$repo" && -n "$1" ]] || return 0
    command -v curl >/dev/null 2>&1 || return 0
    { curl -fsS --max-time 10 "https://api.github.com/repos/${repo// /}/releases/tags/v$1" 2>/dev/null \
        | grep -oE '"digest" *: *"sha256:[0-9a-f]{64}"' | grep -oE '[0-9a-f]{64}'; } || true
}

note_installed_digest() {  # note_installed_digest TGZ VER — архив совпал с выпуском vVER → installed-digest
    # Метка бэкапа берёт digest выпуска из базы (update_installed_digest), и
    # без него переход на версию бэкапа недоступен. Самообновление кнопкой
    # кладёт его само, ручной `update` — этим файлом: один запрос к GitHub,
    # sha256 архива против ассетов выпуска той же версии. Нет сети или не
    # совпало (своя сборка) — файла нет, прежний прочь: чужой версии он не нужен.
    local tgz="$1" ver="$2" have sums
    rm -f "$INSTALLED_DIGEST_FILE"
    [[ -n "$ver" && -f "$tgz" ]] || return 0
    command -v sha256sum >/dev/null 2>&1 || return 0
    have="$(sha256sum "$tgz" | awk '{print $1}')"
    if [[ -n "${AWG_BOT_VERIFIED_SHA256:-}" && "$have" == "${AWG_BOT_VERIFIED_SHA256:-}" ]]; then
        sums="$have"                       # установщик из трубы уже сверил этот архив с выпуском
    else
        sums="$(release_sha256s "$ver")"
    fi
    if [[ -n "$have" ]] && grep -qxF "$have" <<<"$sums"; then
        mkdir -p "$DATA_DIR"
        ( umask 077; printf '%s %s\n' "$ver" "$have" > "$INSTALLED_DIGEST_FILE" )
        log "целостность: sha256 архива совпал с выпуском v$ver"
    else
        log "целостность: архив не сверен с выпуском v$ver (нет ответа GitHub или своя сборка)"
    fi
    return 0
}

compat_floor() {  # compat_floor [DIR] — версия совместимости кода в DIR (по умолчанию — установленного); пусто — не прочитана
    sed -n 's/^COMPAT_FLOOR *= *"\([0-9][0-9.]*\)".*/\1/p' "${1:-$INSTALL_DIR}/awgbot/core/compat.py" 2>/dev/null | head -n1
}

tree_version() {  # tree_version DIR — версия поставки в каталоге (awgbot/__version__.py); пусто — не прочитана
    sed -n 's/^__version__ *= *"\([0-9][0-9.]*\)".*/\1/p' "$1/awgbot/__version__.py" 2>/dev/null | head -n1
}

version_lt() {  # version_lt A B — A строго старше B (X.Y.Z или X.Y.Z.P)
    [[ -n "$1" && -n "$2" && "$1" != "$2" ]] \
        && [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}

lanmap_drop_for_older() {  # lanmap_drop_for_older DIR — поставка без адресов в туннеле: таблицу прочь
    # Таблицу inet awg_lan_map ставит бот 3.3.0 и только он; файла в
    # /etc/nftables.d у неё нет. Код без неё (выпуск старше 3.3.0) о таблице
    # не знает, а оставшись в ядре до ребута, она ломает его трафик: цепочка
    # slot_zone кладёт ответы по реальным адресам сетей шлюзов в другую зону
    # conntrack (invalid → drop файервола), twin_snat переводит источник.
    # Снимаем при остановке сервиса, пока исполняется этот (новый) скрипт.
    # Решает версия поставки ниже 3.3.0 И отсутствие в ней кода таблицы
    # (awgbot/infra/routing/lanmap.py): предрелизная сборка 3.2.x.N с этим
    # кодом — та же функция, таблицу не трогаем. Таблицы и правила слотов
    # (fwmark → lookup 10N) прежний бот ставит сам и держит для зонда резерва
    # — их не трогаем. Маршруты настоящих подсетей прежний бот возвращает сам:
    # при перезагрузке ВПС, на первом такте живости при задействованном
    # РФ-доступе, иначе — при сохранении «🗺 Подсети» слота.
    local ver; ver="$(tree_version "$1")"
    version_lt "$ver" "3.3.0" || return 0
    [[ -f "$1/awgbot/infra/routing/lanmap.py" ]] && return 0
    command -v nft >/dev/null 2>&1 || return 0
    if nft delete table inet awg_lan_map 2>/dev/null; then
        ok "поставка без адресов в туннеле (v$ver): таблица awg_lan_map снята — текущая версия её не поддерживает"
    fi
    return 0
}

update_rescue() {  # ловушка первой половины обновления: прежний код назад, сервис вверх
    local rc=$?
    [[ "$rc" -eq 0 ]] && return 0
    warn "обновление прервалось (код $rc) — возвращаю прежний код и поднимаю сервис"
    if [[ -n "${UPDATE_PREV:-}" && -d "$UPDATE_PREV" ]]; then
        restore_prev_code "$UPDATE_PREV"
    fi
    [[ -n "${UPDATE_TMP:-}" ]] && rm -rf "$UPDATE_TMP"
    systemctl start "$SERVICE" 2>/dev/null || true
}
restore_prev_code() {  # restore_prev_code DIR — прежний код из DIR на место нового (окружение не трогает); 1 — неполон
    local prev="$1" _rc=0
    # текущий код удаляется только под полный прежний: иначе хост остался бы без кода
    if [[ ! -f "$prev/awg-bot.sh" || ! -f "$prev/awgbot/__main__.py" ]]; then
        warn "прежний код в $prev неполон — текущий оставлен на месте"
        return 1
    fi
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec rm -rf {} + 2>/dev/null || true
    ( shopt -s dotglob; mv "$prev"/* "$INSTALL_DIR"/ 2>/dev/null ) || true
    rm -rf "$prev"
    # Копия обвязки шлюза — тоже код: ensure_host_autostart уже могла положить
    # скрипт новой версии, и прежний агент остался бы при чужой обвязке
    gw_plumbing_sync || _rc=$?
    [[ "$_rc" -eq 1 ]] && warn "не удалось вернуть прежнюю копию обвязки в /usr/local/sbin/routing-gw-setup.sh"
    return 0
}
# Копия routing-gw-setup.sh в /usr/local/sbin — по поставке в $INSTALL_DIR,
# если копия уже есть (нет — обвязку тут не разворачивали, класть нечего).
# 0 — копия переписана, 1 — не удалось, 2 — менять нечего.
gw_plumbing_sync() {
    local _rgs="/usr/local/sbin/routing-gw-setup.sh" _src="$INSTALL_DIR/install/routing-gw-setup.sh"
    [[ -f "$_rgs" && -f "$_src" ]] || return 2
    cmp -s "$_src" "$_rgs" && return 2
    install -m 0755 "$_src" "$_rgs" || return 1
}

# ── host-режим: интерфейс на автозагрузке, мусор прежних версий — долой ───────
ensure_host_autostart() {
    # Сам юнит бота — на автозагрузке, обе роли. Ребут шлюза показал, что
    # установка могла оставить его disabled; проверяем на каждом обновлении.
    if [[ "$(systemctl is-enabled "$SERVICE" 2>/dev/null)" != "enabled" ]]; then
        systemctl enable "$SERVICE" >/dev/null 2>&1 \
            && ok "$SERVICE включён на автозагрузку." \
            || warn "не удалось включить $SERVICE на автозагрузку — после ребута бот не поднимется."
    fi
    # Дыра, найденная ребутом ВПС: в докерном режиме интерфейс поднимал
    # контейнер, в host-режиме его поднимает awg-quick@<iface> — и его никто не
    # включал. Ребут оставлял хост без туннелей. Включаем на каждом обновлении:
    # idempotent, и режим доезжает сюда сам, как и в install_unit.
    if [[ "$(yaml_get "$CONF_DIR/app.yaml" role)" == "gateway" ]]; then
        # Та же дыра, что и на ВПС, только на другой стороне: копию
        # routing-gw-setup.sh в /usr/local/sbin кладёт файл конфигурации, её выполняет
        # awg-link-gw.service при каждой загрузке, и обновление агента её не
        # трогало — исправление обвязки, приехавшее в поставке, доезжало до
        # шлюза только новым файлом конфигурации с ВПС. Обновляем, если копия уже есть:
        # нет — значит обвязку тут не разворачивали, и класть нечего.
        # Применится со следующим запуском юнита (реассерт агента или ребут):
        # дёргать обвязку из обновления значит рвать линк без спроса. Откат
        # обновления возвращает и её (restore_prev_code).
        local _rc=0
        gw_plumbing_sync || _rc=$?
        case "$_rc" in
            0) ok "обновлена копия обвязки в /usr/local/sbin/routing-gw-setup.sh (её выполняет юнит при загрузке)" ;;
            1) warn "не удалось обновить /usr/local/sbin/routing-gw-setup.sh — обвязка останется прежней версии" ;;
        esac
        return 0
    fi
    [[ "$(yaml_get "$CONF_DIR/app.yaml" runtime)" == "host" ]] || return 0
    local iface; iface="$(yaml_get "$CONF_DIR/app.yaml" interface)"
    if [[ -n "$iface" ]] && systemctl list-unit-files 'awg-quick@.service' 2>/dev/null | grep -q awg-quick; then
        if [[ "$(systemctl is-enabled "awg-quick@$iface" 2>/dev/null)" != "enabled" ]]; then
            systemctl enable "awg-quick@$iface" >/dev/null 2>&1 \
                && ok "awg-quick@$iface включён на автозагрузку." \
                || warn "не удалось включить awg-quick@$iface — после ребута туннели не поднимутся."
        fi
    fi
    # Копия routing-host-setup.sh в /usr/local/sbin — это то, что юнит
    # awg-bot-routing.service выполняет ПРИ КАЖДОЙ ЗАГРУЗКЕ. Её кладут один раз,
    # при развёртывании обвязки, и обновления её не трогали: исправление,
    # приехавшее в поставке, применялось руками из /opt, а после ребута хост
    # поднимался по версии времён установки.
    local _rhs="/usr/local/sbin/routing-host-setup.sh"
    if [[ -f "$_rhs" && -f "$INSTALL_DIR/install/routing-host-setup.sh" ]] \
       && ! cmp -s "$INSTALL_DIR/install/routing-host-setup.sh" "$_rhs"; then
        install -m 0755 "$INSTALL_DIR/install/routing-host-setup.sh" "$_rhs" \
            && ok "обновлена копия обвязки в $_rhs (её выполняет юнит при загрузке)" \
            || warn "не удалось обновить $_rhs — после ребута обвязка встанет по старой версии"
    fi
    # Та же копия у скрипта линка: её выполняет awg-link@ при загрузке и
    # --reassert/--down из юнита; освежал её только --apply/--rekey, и правки
    # этих режимов не доезжали до ребута.
    local _rls="/usr/local/sbin/routing-link-setup.sh"
    if [[ -f "$_rls" && -f "$INSTALL_DIR/install/routing-link-setup.sh" ]] \
       && ! cmp -s "$INSTALL_DIR/install/routing-link-setup.sh" "$_rls"; then
        install -m 0755 "$INSTALL_DIR/install/routing-link-setup.sh" "$_rls" \
            && ok "обновлена копия скрипта линка в $_rls (её выполняет юнит линка при загрузке)" \
            || warn "не удалось обновить $_rls — после ребута линк встанет по старой версии"
    fi
    return 0
}

awg_state_get() {  # awg_state_get KEY → значение из /etc/awg-bot/awg.state
    [[ -f "$AWG_STATE" ]] || return 0
    sed -nE "s/^$1=(.*)$/\1/p" "$AWG_STATE" | head -n1
}
awg_state_set() {  # awg_state_set KEY VALUE (создаёт файл, сохраняет прочие строки)
    mkdir -p "$(dirname "$AWG_STATE")"
    [[ -f "$AWG_STATE" ]] || printf '%s\n%s\n' \
        "# awg-bot: поколение AmneziaWG на этом хосте. Файл ведут установщик" \
        "# и финал переезда профилей; руками править незачем." > "$AWG_STATE"
    if grep -qE "^$1=" "$AWG_STATE"; then
        sed -i -E "s|^$1=.*|$1=$2|" "$AWG_STATE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$AWG_STATE"
    fi
}
lock_get() { [[ -f "$AWG_LOCK_FILE" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$AWG_LOCK_FILE" | head -n1 || true; }

ensure_awg_module_loaded() {
    # Собранный модуль начинает работать только после подмены работающего:
    # rmmod требует, чтобы ни один интерфейс не был поднят. Делаем это здесь,
    # пока сервис ещё не запущен, — простой туннелей измеряется секундами.
    #
    # Сверяем srcversion (хеш ИСХОДНИКОВ), а не строку версии: апстрим не
    # бампает version.h, и у разных тегов она одинакова — сравнение версий не
    # заметило бы подмены вовсе.
    [[ -f "$AWG_LOCK_FILE" ]] || return 0
    local want have
    want="$(modinfo -F srcversion amneziawg 2>/dev/null | head -n1 || true)"
    have="$(cat /sys/module/amneziawg/srcversion 2>/dev/null || true)"
    [[ -n "$want" && -n "$have" && "$have" != "$want" ]] || return 0
    log "ядро AmneziaWG: работает модуль прежних исходников — подменяю (интерфейсы лягут на секунды)…"
    AWG_LOCK="$AWG_LOCK_FILE" bash "$INSTALL_DIR/install/awg-kernel-install.sh" reload \
        || warn "модуль не подменён — применится после перезагрузки; awg-bot awg reload"
}

ensure_awg_generation() {
    # Поколение — класс совместимости протокола. Растёт только тогда, когда
    # новое ядро не обслужит клиентов прежнего; тогда рядом со старым
    # интерфейсом поднимается второй, на новых параметрах, и бот просит
    # админа начать переезд профилей. Сам переезд — в UI, не здесь.
    local app="$CONF_DIR/app.yaml"
    [[ -f "$AWG_LOCK_FILE" && -f "$app" ]] || return 0
    [[ "$(yaml_get "$app" role)" != "gateway" ]] || return 0     # у шлюза клиентов нет
    local want applied
    want="$(lock_get AWG_GENERATION)"; [[ "$want" =~ ^[0-9]+$ ]] || return 0
    applied="$(awg_state_get AWG_GENERATION_APPLIED)"
    if [[ -z "$applied" ]]; then
        # Файл состояния заводит первая установка (ensure_awg_server), а
        # обновления с версий без него минимум поставки уже не пропускает:
        # пустое значение — повреждённый файл, переезд по нему не объявляем.
        warn "в $AWG_STATE нет AWG_GENERATION_APPLIED — проверка поколения AmneziaWG пропущена"
        return 0
    fi
    [[ "$want" -gt "$applied" ]] || return 0

    local mig_if; mig_if="$(yaml_get "$app" migration_interface)"
    if [[ -n "$mig_if" ]]; then
        # Переезд УЖЕ идёт — и он не наш: двойники на нём рождены под прежнее
        # поколение. Объявить его целью нового значило бы на финале записать
        # хост перешедшим на ядро, которого он не видел. Цель не трогаем;
        # смена поколения ждёт финала, о ней напомнит сам бот.
        warn "поколение $want ждёт: идёт переезд на $mig_if, он обязан завершиться первым"
        log "после финала бот предложит подготовить переезд на новое поколение"
        return 0
    fi

    # Имя и подсеть второго интерфейса выбираем сами: админу тут нечего решать,
    # а ошибиться есть где — занятое имя или пересечение подсетей.
    local base_if base_prefix new_if="" new_prefix="" i
    base_if="$(yaml_get "$app" interface)"; base_if="${base_if:-awg0}"
    base_prefix="$(yaml_get "$app" subnet_prefix)"; base_prefix="${base_prefix:-10.8.1}"
    local conf_dir; conf_dir="$(yaml_get "$app" awg_dir)"; conf_dir="${conf_dir:-/etc/amnezia/amneziawg}"
    for i in 1 2 3 4 5 6 7 8 9; do
        [[ -e "$conf_dir/awg$i.conf" || "awg$i" == "$base_if" ]] && continue
        new_if="awg$i"; break
    done
    local o1 o2 o3; IFS=. read -r o1 o2 o3 <<<"$base_prefix"
    for i in 1 2 3 4 5 6 7 8 9; do
        local cand="$o1.$(( o2 + i )).$o3"
        grep -rqs "$cand\." "$conf_dir" 2>/dev/null && continue
        new_prefix="$cand"; break
    done
    [[ -n "$new_if" && -n "$new_prefix" ]] || { warn "поколение $want: не подобрать имя/подсеть второго интерфейса — переезд придётся настроить вручную"; return 0; }

    log "ядро поколения $want не обслужит клиентов поколения $applied — поднимаю второй интерфейс $new_if ($new_prefix.0/24) под переезд профилей"
    AWG_IF="$new_if" SUBNET_PREFIX="$new_prefix" AWG_QUICK_DIR="$conf_dir" \
        bash "$INSTALL_DIR/install/awg-server-init.sh" >/dev/null \
        || { warn "второй интерфейс не поднят — переезд пока невозможен; см. journalctl -u awg-quick@$new_if -o short-iso"; return 0; }
    # Пул адресов начинается с .2: у созданного нами интерфейса .1 занимает сам
    # сервер, и двойник, выданный на .1, конфликтовал бы с ним.
    [[ "$(yaml_get "$app" ip_host_start)" == "1" ]] && yaml_set_soft "$app" ip_host_start 2
    yaml_set_soft "$app" migration_interface "\"$new_if\""
    yaml_set_soft "$app" migration_subnet_prefix "\"$new_prefix\""
    awg_state_set AWG_GENERATION_TARGET "$want"
    ok "второй интерфейс $new_if готов: бот попросит начать переезд профилей при старте"
}

post_update_rescue() {  # ловушка второй половины обновления
    # cmd_update остановил сервис и передал управление сюда через exec. Любой
    # отказ ниже (нет сети для pip, битый конфиг, systemd) оставлял бы НОВЫЙ код
    # на диске и ОСТАНОВЛЕННЫЙ сервис, причём молча: апдейтер запущен из
    # systemd-run с выводом в /dev/null. Поэтому: поднять сервис на том, что
    # есть, и сказать админу в чат.
    local rc=$? back=0
    [[ "$rc" -eq 0 ]] && return 0
    venv_rollback
    if [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]]; then
        warn "обновление прервалось (код $rc) — возвращаю прежнюю версию и поднимаю сервис"
        restore_prev_code "$AWG_UPDATE_PREV" && back=1
        adopt_timezone_undo                # ключ снял этот прогон — прежний код получает его назад
        unit_guard_strip_for_old           # прежний скрипт сторожа не знает — его шаг из юнита прочь
    else
        warn "обновление прервалось (код $rc) — поднимаю сервис на прежнем состоянии"
    fi
    update_guard_disarm                    # сторож этого прогона, если успел встать: откат уже здесь
    rm -f "$DATA_DIR/update-running"
    systemctl start "$SERVICE" 2>/dev/null || true
    local what='Код уже подменён, но довести установку не удалось. Сервис поднят на том, что есть.'
    [[ "$back" -eq 1 ]] && what='Довести установку не удалось — вернул прежнюю версию: и код, и окружение. Сервис поднят.'
    ( cd "$INSTALL_DIR" 2>/dev/null \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" AWG_WHAT="$what" \
        && ./venv/bin/python -c "
import os
from awgbot.infra import tgsend
tgsend.send('⚠️ <b>Обновление прервалось.</b>\n\n' + os.environ['AWG_WHAT'] + '\n\n'
            'Посмотреть причину: <code>journalctl -u awg-bot -e -o short-iso</code>, '
            'повторить: <code>sudo awg-bot update &lt;архив&gt;</code>.')
" >/dev/null 2>&1 ) || true
}
unit_guard_strip_for_old() {  # возвращённый код не знает __update_guard: ExecStartPre из юнита — прочь
    # Юнит переписал новый скрипт (install_unit) до отказа; прежний скрипт на
    # глагол __update_guard отвечал бы «неизвестная команда» на каждом старте
    # (строка с «-» старт не держит, но сорит в журнал)
    local unit="$UNIT_PATH" tmp
    grep -q '__update_guard' "$INSTALL_DIR/awg-bot.sh" 2>/dev/null && return 0
    [[ -f "$unit" ]] && grep -q '__update_guard' "$unit" || return 0
    tmp="$(mktemp)" || return 0
    grep -v '__update_guard' "$unit" > "$tmp" && cat "$tmp" > "$unit"
    rm -f "$tmp"
    systemctl daemon-reload 2>/dev/null || true
}
update_done() {  # обновление доведено: прежние код и окружение больше не нужны
    rm -f "$DATA_DIR/update-running"       # апдейтер отработал — pid в файле больше ничего не значит
    [[ -n "${AWG_UPDATE_PREV:-}" ]] && rm -rf "$AWG_UPDATE_PREV"
    venv_prune
}

# ── сторож обновления: прежняя версия живёт, пока новая не вышла на связь ─────
# После старта нового сервиса прежние код (.prev-code), окружение (.venvs/.prev)
# и сборка ядра не снимаются сразу: сторож —
# таймер systemd через UPDATE_GUARD_MIN минут — смотрит отметку жизни, которую
# пишет новый процесс бота (основной бот — первый ответ Telegram на getUpdates,
# агент — он же или первое подписанное слово сервера AWG в сессии канала).
# Отметка этой версии — фиксация (update_done, prune ядра); нет — откат в той
# же установке: прежние код и окружение на место, перезапуск, метка отката
# для прежнего процесса. Переход через COMPAT_FLOOR (прежняя версия ниже
# версии совместимости нового кода) сторожем не прикрывается: откат ниже неё
# запрещён тем же правилом, что `awg-bot update`.
UPDATE_GUARD="$DATA_DIR/update-guard"            # сторож взведён: VERSION= и PREV= (пишет post_update)
UPDATE_OK="$DATA_DIR/update-ok"                  # отметка жизни «<версия> <время>» (пишет бот)
UPDATE_ROLLED="$DATA_DIR/update-rolled-back"     # метка отката (JSON) — прочтёт прежний процесс
UPDATE_GUARD_UNIT="awg-bot-update-guard"
UPDATE_GUARD_MIN=10                             # окно сторожа, минут
UPDATE_GUARD_BOOT_MIN=5                         # после загрузки в окне — не меньше стольких минут
UPDATE_LOCK="$UPDATE_GUARD.lock"                 # update, сторож, фиксация и откат набора — по одному

update_lock() {  # update_lock -n | -w СЕК — замок установки на fd 9 (живёт до конца процесса и через exec)
    # нет flock — без замка, как раньше; замок не взят — 1.
    # Взят — pid в update-running: бот видит, что апдейтер ещё жив (шаг набора
    # ждёт его факта, а не запускает второй); exec сохраняет pid
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$UPDATE_LOCK"
        flock "$@" 9 || return 1
    fi
    printf '%s\n' "$$" > "$DATA_DIR/update-running" 2>/dev/null || true
    return 0
}

guard_get() { [[ -f "$UPDATE_GUARD" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$UPDATE_GUARD" | head -n1 || true; }

update_guard_due() {  # 0 — обновление прикрыть сторожем: прежний код на месте и в линии совместимости
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] || return 1
    command -v systemd-run >/dev/null 2>&1 || return 1
    local prev floor
    prev="$(tree_version "$AWG_UPDATE_PREV")"; floor="$(compat_floor)"
    [[ -n "$prev" && -n "$floor" ]] || return 1
    version_lt "$prev" "$floor" && return 1      # переход через COMPAT_FLOOR: назад дороги нет
    return 0
}

update_guard_write() {  # update_guard_write — до старта сервиса: что сторожить
    local ver prev
    ver="$(tree_version "$INSTALL_DIR")"; prev="$(tree_version "$AWG_UPDATE_PREV")"
    rm -f "$UPDATE_OK"                     # отметка прошлых запусков не в счёт — только новый процесс
    mkdir -p "$DATA_DIR"
    # AT — секунды эпохи: по ним проверка на старте сервиса считает, истекло ли окно
    # HOLD — id хода или команды набора (0 — не шаг набора)
    ( umask 077; printf 'VERSION=%s\nPREV=%s\nAT=%s\nHOLD=%s\n' "$ver" "$prev" "$(date +%s)" \
        "$(hold_id)" > "$UPDATE_GUARD" )
}

update_guard_arm() {  # update_guard_arm [МИНУТ] — таймер сторожа; отказ systemd-run — 1
    # RemainAfterElapse=no и --collect: отработавший таймер и его служба
    # выгружаются — следующее обновление взводит сторожа под тем же именем
    systemd-run --quiet --collect --unit="$UPDATE_GUARD_UNIT" --on-active="${1:-$UPDATE_GUARD_MIN}min" \
        --timer-property=AccuracySec=10s --timer-property=RemainAfterElapse=no \
        --description="awg-bot: сторож обновления" \
        "$INSTALL_DIR/awg-bot.sh" __update_guard >/dev/null 2>&1
}

update_guard_disarm() {  # снять сторожа: таймер и его состояние
    systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
    systemctl reset-failed "$UPDATE_GUARD_UNIT.timer" "$UPDATE_GUARD_UNIT.service" 2>/dev/null || true
    rm -f "$UPDATE_GUARD"
}

update_guard_rollback() {  # update_guard_rollback VERSION PREV [nostart] — новая не вышла на связь: прежняя на место
    # nostart — из ExecStartPre юнита: сервис ещё не запущен, а systemctl для
    # него из его же запуска ждал бы сам себя; запустится он сам, уже прежним кодом
    local ver="$1" prev="$2" how="${3:-}"
    if [[ ! -d "$AWG_UPDATE_PREV" ]]; then
        warn "v$ver не вышла на связь за $UPDATE_GUARD_MIN минут, но прежнего кода нет — откатывать некуда"
        rm -f "$UPDATE_GUARD"
        return 1
    fi
    warn "v$ver не вышла на связь за $UPDATE_GUARD_MIN минут — возвращаю v$prev"
    [[ "$how" == "nostart" ]] || systemctl stop "$SERVICE" 2>/dev/null || true
    if ! restore_prev_code "$AWG_UPDATE_PREV"; then
        rm -f "$UPDATE_GUARD"
        [[ "$how" == "nostart" ]] || systemctl start "$SERVICE" 2>/dev/null || true
        return 1
    fi
    venv_rollback
    # Метка отката — сразу за кодом: из ExecStartPre (таймаут 90 с) до
    # удаления окружения дело может не дойти, а прежнему процессу она нужна
    ( umask 077; printf '{"version": %s, "prev": %s, "at": %s}\n' "$(json_str "$ver")" "$(json_str "$prev")" \
        "$(json_str "$(date -Iseconds)")" > "$UPDATE_ROLLED" ) || true
    rm -f "$UPDATE_GUARD" "$UPDATE_OK"
    # окружение новой версии — прочь; из ExecStartPre — потом (следующая уборка)
    [[ "$how" == "nostart" ]] && return 0
    venv_prune
    systemctl start "$SERVICE" || warn "$SERVICE не запустился — journalctl -u $SERVICE -e -o short-iso"
    return 0
}

cmd_update_guard() {  # __update_guard [--alive|--start] — фиксация по отметке жизни или откат
    # без ключа — таймер сторожа: отметка этой версии — фиксация, нет — откат;
    # --alive — бот, поставив отметку: фиксация сразу, без отметки — ничего
    #   (решает таймер);
    # --start — ExecStartPre юнита, перед каждым стартом сервиса: таймера нет
    #   (перезагрузка в окне) — окно не истекло — сторож заново на остаток, истекло
    #   без отметки — откат до старта; таймер есть или сторожа нет — ничего.
    require_root
    local mode="${1:-}"
    [[ -f "$UPDATE_GUARD" ]] || return 0
    # одно решение за раз; откат держит замок и запускает сервис — проверка
    # его старта не ждёт замка (ждала бы сама себя), решает тот, кто держит
    if [[ "$mode" == "--start" ]]; then update_lock -n || return 0; else update_lock -w 120 || return 0; fi
    [[ -f "$UPDATE_GUARD" ]] || return 0
    local ver prev at cur mark left
    ver="$(guard_get VERSION)"; prev="$(guard_get PREV)"; at="$(guard_get AT)"; cur="$(tree_version "$INSTALL_DIR")"
    if [[ -z "$ver" || "$ver" != "$cur" ]]; then
        # установку сменили мимо сторожа (новое обновление, чистая установка) — сторожить нечего
        rm -f "$UPDATE_GUARD"
        return 0
    fi
    AWG_UPDATE_PREV="$INSTALL_DIR/.prev-code"
    mark="$(awk 'NR == 1 { print $1 }' "$UPDATE_OK" 2>/dev/null || true)"
    if [[ "$mark" == "$ver" ]]; then
        systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
        local hold; hold="$(guard_get HOLD)"
        if [[ -n "$hold" && "$hold" != "0" ]]; then
            # шаг набора: прежнее ждёт фиксации или отката всего набора
            log "v$ver вышла на связь — прежняя версия держится до фиксации набора"
            update_hold_take "$hold"
            rm -f "$UPDATE_GUARD" "$UPDATE_OK"
            return 0
        fi
        log "v$ver вышла на связь — прежняя версия v$prev больше не нужна"
        rm -f "$UPDATE_GUARD" "$UPDATE_OK"
        prune_old_kernel_builds
        update_done
        return 0
    fi
    case "$mode" in
        --alive) return 0 ;;
        --start)
            systemctl is-active --quiet "$UPDATE_GUARD_UNIT.timer" && return 0
            [[ "$at" =~ ^[0-9]+$ ]] || at=0
            left=$(( at + UPDATE_GUARD_MIN * 60 - $(date +%s) ))
            # окно по стенным часам включает простой хоста: после загрузки новой
            # версии — не меньше UPDATE_GUARD_BOOT_MIN минут выйти на связь
            (( left < UPDATE_GUARD_BOOT_MIN * 60 )) && left=$(( UPDATE_GUARD_BOOT_MIN * 60 ))
            log "сторож обновления потерян (перезагрузка) — взвожу заново на $(( (left + 59) / 60 )) мин"
            update_guard_arm $(( (left + 59) / 60 )) && return 0
            warn "сторож обновления не взведён заново (systemd-run) — решаю сейчас"
            update_guard_rollback "$ver" "$prev" nostart || true
            return 0 ;;
    esac
    update_guard_rollback "$ver" "$prev"
}

# ── шаг набора: прежнее держится до фиксации или отката всего набора ──────────
# Шаг (сервер или шлюз под командой набора) вышел на связь — прежние код
# (.hold-code) и окружение (ENV в update-hold) не снимаются: откат набора
# (__update_revert) — переименованием на месте, без
# сети. Цепочка ступеней под той же командой держит самое первое прежнее:
# промежуточные ступени снимаются, когда следующая вышла на связь. Фиксация
# набора (__update_commit) — прежнее и сборки ядра прочь.
UPDATE_HOLD_FILE="$DATA_DIR/update-hold"         # PREV= (версия), ENV= (окружение), AT=, RUN= (id хода/команды)
UPDATE_HOLD_CODE="$INSTALL_DIR/.hold-code"

hold_get() { [[ -f "${UPDATE_HOLD_FILE:-}" ]] && sed -nE "s/^$1=(.*)$/\1/p" "$UPDATE_HOLD_FILE" | head -n1 || true; }

hold_id() {  # id шага набора из AWG_UPDATE_HOLD; не шаг набора — 0
    local id="${AWG_UPDATE_HOLD:-0}"
    [[ "$id" =~ ^[0-9A-Za-z]{1,16}$ ]] || id=0
    printf '%s' "$id"
}

update_hold_drop() {  # удержание набора прочь (ручной update, wipe, переход через COMPAT_FLOOR, другой ход)
    [[ -f "$UPDATE_HOLD_FILE" || -d "$UPDATE_HOLD_CODE" ]] || return 0
    log "удержание прежней версии v$(hold_get PREV) снято"
    rm -rf "$UPDATE_HOLD_CODE"
    rm -f "$UPDATE_HOLD_FILE"
}

update_crosses_floor() {  # 0 — прежняя версия (AWG_UPDATE_PREV) ниже версии совместимости нового кода
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] || return 1
    local prev floor
    prev="$(tree_version "$AWG_UPDATE_PREV")"; floor="$(compat_floor)"
    [[ -n "$prev" && -n "$floor" ]] && version_lt "$prev" "$floor"
}

update_hold_take() {  # update_hold_take ID — шаг вышел на связь: его прежнее — в удержание набора (первое — остаётся)
    local run="${1:-}" prev_env=""
    prev_env="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null)" || prev_env=""
    if [[ -f "$UPDATE_HOLD_FILE" && "$(hold_get RUN)" != "$run" ]]; then
        update_hold_drop                   # удержание другого хода или команды — не наше, прочь
    fi
    if [[ -f "$UPDATE_HOLD_FILE" && -d "$UPDATE_HOLD_CODE" ]]; then
        # следующая ступень той же команды: промежуточное прочь, первое прежнее держится
        rm -rf "$INSTALL_DIR/.prev-code"
        [[ -n "$prev_env" && "$prev_env" != "$(hold_get ENV)" ]] && rm -rf "$prev_env"
        rm -f "$INSTALL_DIR/.venvs/.prev"
        return 0
    fi
    [[ -d "$INSTALL_DIR/.prev-code" ]] || return 0
    rm -rf "$UPDATE_HOLD_CODE"
    mv "$INSTALL_DIR/.prev-code" "$UPDATE_HOLD_CODE"
    rm -f "$INSTALL_DIR/.venvs/.prev"
    ( umask 077; printf 'PREV=%s\nENV=%s\nAT=%s\nRUN=%s\n' "$(tree_version "$UPDATE_HOLD_CODE")" "$prev_env" \
        "$(date +%s)" "$run" > "$UPDATE_HOLD_FILE" )
}

cmd_update_commit() {  # __update_commit — набор зафиксирован: удержанное прежнее и сборки ядра прочь
    require_root
    [[ -f "$UPDATE_HOLD_FILE" ]] || return 0
    update_lock -w 120 || { warn "замок установки занят — фиксация набора не выполнена"; return 1; }
    [[ -f "$UPDATE_HOLD_FILE" ]] || return 0
    log "набор зафиксирован — прежняя версия v$(hold_get PREV) больше не нужна"
    rm -rf "$UPDATE_HOLD_CODE"
    rm -f "$UPDATE_HOLD_FILE"
    prune_old_kernel_builds
    venv_prune
}

REV_SRC=""; REV_ENV=""; REV_PREV=""
revert_source() {  # источник отката набора → REV_*; нет (или он ниже версии совместимости) — 1
    # Удержанное шагом набора (.hold-code) или прежнее ещё не подтверждённого
    # шага (.prev-code под сторожем). Ниже COMPAT_FLOOR назад нельзя — как у update.
    REV_SRC=""; REV_ENV=""; REV_PREV=""
    if [[ -f "$UPDATE_HOLD_FILE" && -d "$UPDATE_HOLD_CODE" ]]; then
        REV_SRC="$UPDATE_HOLD_CODE"; REV_ENV="$(hold_get ENV)"; REV_PREV="$(hold_get PREV)"
    elif [[ -f "$UPDATE_GUARD" && -d "$INSTALL_DIR/.prev-code" ]]; then
        REV_SRC="$INSTALL_DIR/.prev-code"; REV_ENV="$(cat "$INSTALL_DIR/.venvs/.prev" 2>/dev/null || true)"
        REV_PREV="$(guard_get PREV)"
    fi
    [[ -n "$REV_SRC" ]] || return 1
    local ver floor
    ver="$(tree_version "$REV_SRC")"; floor="$(compat_floor)"
    if [[ -n "$ver" && -n "$floor" ]] && version_lt "$ver" "$floor"; then
        REV_SRC=""
        return 1
    fi
    return 0
}

cmd_update_revert() {  # __update_revert [--check] — откат набора: прежние код и окружение на место
    # Источника нет — код 2: откатываться не на что, понижение — с GitHub.
    # --check — только ответ. Сам откат — под замком установки, источник
    # выбирается заново после остановки сервиса (сторож или --alive могли
    # решить, пока ждали замок).
    require_root
    if ! revert_source; then
        [[ "${1:-}" == "--check" ]] || warn "прежней версии нет — откатывать некуда"
        return 2
    fi
    [[ "${1:-}" == "--check" ]] && return 0
    update_lock -w 120 || { warn "замок установки занят — откат набора не выполнен"; return 1; }
    systemctl stop "$UPDATE_GUARD_UNIT.timer" 2>/dev/null || true
    systemctl stop "$SERVICE" 2>/dev/null || true
    if ! revert_source; then
        warn "прежней версии нет — откатывать некуда"
        systemctl start "$SERVICE" 2>/dev/null || true
        return 2
    fi
    local src="$REV_SRC" env="$REV_ENV" prev="$REV_PREV"
    warn "откат набора — возвращаю v${prev:-?}"
    if ! restore_prev_code "$src"; then
        systemctl start "$SERVICE" 2>/dev/null || true
        return 1
    fi
    rm -rf "$INSTALL_DIR/.prev-code" "$UPDATE_HOLD_CODE"     # источник перенесён — второй (если был) прочь
    [[ -n "$env" ]] && printf '%s\n' "$env" > "$INSTALL_DIR/.venvs/.prev"
    venv_rollback
    rm -f "$UPDATE_HOLD_FILE"
    venv_prune
    rm -f "$UPDATE_GUARD" "$UPDATE_OK"
    systemctl start "$SERVICE" || warn "$SERVICE не запустился — journalctl -u $SERVICE -e -o short-iso"
    return 0
}

update_env_steps() {  # update_env_steps [restore] — окружение новой версии: вторая половина update и восстановления
    # Одна функция на оба пути: восстановление с переходом на выпуск бэкапа
    # раньше копировало шаги руками и теряло их (ping, резолвер, проверка conf)
    ensure_python
    ensure_ping
    build_venv
    install_unit
    ensure_host_autostart
    ensure_awg_module_loaded
    ensure_awg_generation
    adopt_resolver                         # dns1 уже приватный — резолвер под опеку бота
    # ключ timezone снимается один раз — на переходе с выпуска, где его писали
    # шаблон и установщик (ниже 3.4.0): с 3.4.0 ключ в app.yaml — переопределение
    # человека, обновление его не трогает; восстановление решает пояс после
    # замены conf (restore_apply)
    if [[ "${1:-}" != "restore" ]] && update_adopts_timezone; then
        adopt_timezone
    fi
    seed_conf                              # досеять НОВЫЕ conf-файлы этой версии
                                           # (существующие не трогаем — idempotent)
    validate_config
}

cmd_post_update() {
    # Вторая половина обновления, исполняется УЖЕ НОВЫМ скриптом (см. cmd_update).
    require_root
    trap post_update_rescue EXIT
    local wipe="${1:-0}"
    export AWG_IN_UPDATE=1                 # build_venv держит прежнее окружение до конца
    update_env_steps

    # удержание прошлого хода набора: ручной update, wipe и переход через
    # версию совместимости его снимают (откатываться туда больше нельзя или незачем)
    if [[ "$(hold_id)" == "0" || "$wipe" -eq 1 ]] || update_crosses_floor; then
        update_hold_drop
    fi

    if [[ "$wipe" -eq 1 ]]; then
        warn "удаляю данные и настройки по твоему запросу…"
        rm -rf "$DATA_DIR"/*.db "$DATA_DIR"/*.db-* "$DATA_DIR/.tz-adopted" 2>/dev/null || true
        rm -f "$ENV_FILE" 2>/dev/null || true
        rm -rf "$CONF_DIR" 2>/dev/null || true
        warn "данные удалены — потребуется reconfigure перед стартом."
        update_done
        trap - EXIT                            # это итог по запросу, не отказ: ловушке нечего спасать
        die "запусти: sudo awg-bot reconfigure"
    fi

    # Сторож — до старта: отметка жизни прошлых запусков стирается, таймер
    # взведён раньше, чем новый процесс может её написать. Не взвёлся — как
    # раньше: прежняя версия снимается сразу после старта.
    local guarded=0
    if update_guard_due; then
        update_guard_disarm                # сторож прошлого обновления, если ещё стоит
        update_guard_write
        if update_guard_arm; then
            guarded=1
        else
            warn "сторож обновления не взведён (systemd-run) — прежняя версия снимается сразу"
            rm -f "$UPDATE_GUARD"
        fi
    fi

    systemctl start "$SERVICE"; sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE перезапущен."
        [[ "$guarded" -eq 1 ]] || prune_old_kernel_builds
    else
        warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
    fi
    if [[ "$guarded" -eq 1 ]]; then
        # прежние код, окружение и сборка ядра ждут отметку жизни нового процесса
        ok "прежняя версия хранится, пока новая не выйдет на связь: сторож посмотрит через $UPDATE_GUARD_MIN минут"
    else
        # без сторожа (переход через версию совместимости) назад дороги нет:
        # новый код мог уже тронуть базу
        update_done
    fi
    trap - EXIT
    ok "Обновление завершено."
    # временный каталог с архивом от установщика — только из /tmp, чужое не трогаем
    case "${AWG_UPDATE_CLEANUP:-}" in /tmp/*) rm -rf "$AWG_UPDATE_CLEANUP" ;; esac
    # Шлюз, подключаемый командой поверх стоящего агента другой версии
    # (установщик --join): подключение — теперь, новым кодом
    if [[ -n "${AWG_UPDATE_THEN_JOIN:-}" ]]; then
        AWG_JOIN_TOKEN="$AWG_UPDATE_THEN_JOIN" exec "$SELF_PATH" join
    fi
}

prune_old_kernel_builds() {
    # Прежние сборки ядра — долой, но ровно тогда, когда откат уже не нужен:
    #  • совместимая смена тега — после успешного старта на новом модуле, то
    #    есть здесь;
    #  • смена поколения — только на финале переезда профилей: до него старый
    #    интерфейс ещё обслуживает людей, и путь назад должен оставаться.
    [[ -f "$AWG_LOCK_FILE" ]] || return 0
    local want applied target
    want="$(lock_get AWG_GENERATION)"; applied="$(awg_state_get AWG_GENERATION_APPLIED)"
    target="$(awg_state_get AWG_GENERATION_TARGET)"
    if [[ -n "$target" || ( -n "$want" && -n "$applied" && "$want" -gt "$applied" ) ]]; then
        log "прежняя сборка ядра оставлена до финала переезда профилей"
        return 0
    fi
    AWG_LOCK="$AWG_LOCK_FILE" bash "$INSTALL_DIR/install/awg-kernel-install.sh" prune \
        || log "прежние сборки ядра не убраны — позже: awg-bot awg prune"
}

# ── backup / restore: бэкап сервера AWG — один шифрованный архив ─────────────
GW_NO_BACKUPS="на шлюзе бэкапов нет: настройки шлюза — в бэкапе сервера AWG. Новое устройство подключается командой из бота сервера AWG, остальное довезёт канал конфигурации"
ROOT_GW_DIR="${AWG_ROOT_GW_DIR:-/root}"      # выданные шлюзовые стороны линков: /root/gw-<линк>.conf
SYS_NET="${AWG_SYS_NET:-/sys/class/net}"     # поднятые интерфейсы хоста

is_gateway_role() {  # роль установки — шлюз (role: gateway в app.yaml)
    [[ "$(yaml_get "$CONF_DIR/app.yaml" role 2>/dev/null)" == "gateway" ]]
}

bot_tool() {  # bot_tool MODULE ARGS… — python -m MODULE кодом установки, с путями этой установки
    ( cd "$INSTALL_DIR" \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
        && ./venv/bin/python -m "$@" )
}

cmd_backup() {
    require_root; require_installed
    is_gateway_role && die "$GW_NO_BACKUPS"
    mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
    local ts out err; ts="$(date +%Y%m%d_%H%M%S)"; out="$BACKUP_DIR/awg-bot-backup-main-$ts.tgz.enc"
    # Сборщик один на все случаи (tools/snapshot, тот же состав, что копия из
    # чата): БД — через backup API SQLite; шифрование — фразой из базы. Без
    # фразы бэкапа нет: открытых бэкапов нет, а шифровать без кода установки
    # нечем — при сломанном коде бэкапа нет вовсе.
    err="$(bot_tool tools.snapshot --out "$out" 2>&1 >/dev/null)" || die "${err:-бэкап не снялся}"
    ok "бэкап: $out"
    log "шифрованный ключевой фразой — восстановить: sudo awg-bot restore $out"
}

awg_conf_dir() {
    # где лежат конфиги awg-интерфейсов хоста: /etc/amnezia/amneziawg (awg_dir в
    # app.yaml — докерный рудимент); у шлюза — gateway.conf_dir
    local d; d="$(yaml_get "$CONF_DIR/app.yaml" conf_dir)"
    printf '%s' "${d:-/etc/amnezia/amneziawg}"
}

# Замена файлов живого — переименованием в том же каталоге: <имя>.restore-new
# готовится рядом с целью, заменяемый уходит в <имя>.restore-old, новый встаёт
# на его место (mv в одном каталоге атомарен). Отказ посреди замены — ловушка
# возвращает .restore-old на место и поднимает сервис; после последней замены
# .restore-old удаляются: это не копия и не точка возврата, а временный дубль
# внутри операции.
RESTORE_TARGETS=()        # цели в порядке замены (без .restore-new — только убрать в дубль)
RESTORE_DONE_T=()         # уже тронутые замены
RESTORE_HAD_OLD=()        # 1 — у тронутой цели был прежний файл (.restore-old)
# Фазы операции — от них зависит, что делает ловушка при любом выходе:
#   ""        до остановки сервиса: живое не тронуто, сервис работает;
#   stopped   сервис остановлен, живые файлы прежние (слияние, подготовка,
#             первый DROP): .restore-new прочь, сервис вверх;
#   swapping  идёт замена: заменённое возвращается из .restore-old, сервис вверх;
#   done      точка невозврата пройдена (замена удалась): только сервис вверх.
RESTORE_PHASE=""
RESTORE_ERR=""
RESTORE_FAIL=""           # причина отказа — в маркер для бота (копия из чата)
RESTORE_YES=0
RESTORE_TMP=""
RESTORE_PENDING=""
RESTORE_PLAN_FILE=""

json_str() {  # json_str TEXT → "TEXT" с экранированием для JSON (без переводов строк)
    local s="${1//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="$(printf '%s' "$s" | tr -d '\000-\037')"
    printf '"%s"' "$s"
}

restore_die() {  # restore_die ПРИЧИНА — отказ с причиной для маркера бота
    RESTORE_FAIL="$*"
    die "$*"
}

restore_stage() {  # restore_stage SRC TARGET MODE — подготовить TARGET.restore-new, если отличается
    cmp -s "$1" "$2" 2>/dev/null && return 0
    mkdir -p "$(dirname "$2")" || return 1
    cp "$1" "$2.restore-new" && chmod "$3" "$2.restore-new" || { rm -f "$2.restore-new"; return 1; }
    RESTORE_TARGETS+=("$2")
}

restore_unstage() {  # .restore-new прочь (отказ до замены)
    local t
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-new"; done
}

restore_rollback() {  # вернуть заменённое: .restore-old на место, новое прочь
    local i t
    i=${#RESTORE_DONE_T[@]}
    while [[ "$i" -gt 0 ]]; do
        i=$(( i - 1 ))
        t="${RESTORE_DONE_T[$i]}"
        if [[ "${RESTORE_HAD_OLD[$i]}" == "1" ]]; then
            mv -f -- "$t.restore-old" "$t" 2>/dev/null || warn "не вернулся $t — прежний лежит в $t.restore-old"
        else
            rm -f -- "$t"
        fi
    done
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-new"; done
    RESTORE_DONE_T=(); RESTORE_HAD_OLD=()
}

restore_mv() {  # restore_mv SRC DST — переименование; отказ — причина в RESTORE_ERR
    local errf="${RESTORE_TMP:-/tmp}/.mv-err"
    mv -f -- "$1" "$2" 2>"$errf" && return 0
    RESTORE_ERR="$2: $(cat "$errf" 2>/dev/null)"
    return 1
}

restore_swap() {  # restore_swap → 0 — всё заменено; 1 — отказ (причина в RESTORE_ERR)
    local t had
    RESTORE_PHASE=swapping
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do
        had=0
        if [[ -e "$t" ]]; then
            restore_mv "$t" "$t.restore-old" || return 1
            had=1
        fi
        RESTORE_DONE_T+=("$t"); RESTORE_HAD_OLD+=("$had")
        [[ -e "$t.restore-new" ]] || continue          # только убрать (журналы заменяемой базы)
        restore_mv "$t.restore-new" "$t" || return 1
    done
    return 0
}

restore_finish() {  # замена удалась — точка невозврата: временные дубли прочь
    local t
    for t in ${RESTORE_TARGETS[@]+"${RESTORE_TARGETS[@]}"}; do rm -f -- "$t.restore-old"; done
    RESTORE_PHASE=done
}

restore_prune_mark() {  # DROP первого наложения при откате остаются: старт бота снимет лишние
    : > "$DATA_DIR/.restore-prune" 2>/dev/null || true
}

restore_fail_marker() {  # отказ восстановления из чата — новый процесс бота скажет админу
    [[ "$RESTORE_YES" -eq 1 && -n "$RESTORE_FAIL" ]] || return 0
    mkdir -p "$DATA_DIR" 2>/dev/null || true
    printf '{"failed": %s, "release": %s, "from": %s}\n' "$(json_str "$RESTORE_FAIL")" \
        "$(json_str "${RESTORE_TO:-}")" "$(json_str "${RESTORE_FROM:-}")" > "$DATA_DIR/restore-failed.json" 2>/dev/null || true
}

restore_code_back() {  # переход на выпуск бэкапа не доведён: прежние код и окружение на место
    [[ "${RESTORE_CODE:-0}" -eq 1 ]] || return 0
    venv_rollback
    # ссылка — на окружение, с которым работал прежний код (restore_code_swap)
    if [[ -n "${RESTORE_VENV:-}" && -d "$RESTORE_VENV" ]]; then
        ln -sfn "$RESTORE_VENV" "$INSTALL_DIR/venv"
    fi
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "$AWG_UPDATE_PREV" ]] && restore_prev_code "$AWG_UPDATE_PREV"
    warn "Переход на v${RESTORE_TO:-?} не удался: ${RESTORE_FAIL:-причина не названа} — ничего не изменилось, работает v${RESTORE_FROM:-?}"
    return 0
}

restore_kernel_back() {  # ядро выпуска бэкапа поставлено, а переход не доведён — ядро прежней версии назад
    [[ "${RESTORE_KERNEL:-0}" -eq 1 ]] || return 0
    RESTORE_KERNEL=0
    local lock="$INSTALL_DIR/install/awg.lock"
    if [[ -f "$lock" && -f "$INSTALL_DIR/install/awg-kernel-install.sh" ]] \
       && AWG_LOCK="$lock" bash "$INSTALL_DIR/install/awg-kernel-install.sh" install >/dev/null 2>&1; then
        log "AmneziaWG: ядро прежней версии v${RESTORE_FROM:-?} возвращено"
    else
        warn "AmneziaWG: на хосте осталось ядро выпуска v${RESTORE_TO:-?} — верни прежнее: sudo awg-bot awg install"
    fi
}

restore_exit() {  # ловушка операции: по фазе — откат, сервис вверх, временное прочь
    local rc=$?
    # голый die или errexit после «RESTORE_FAIL=» — причина для маркера всё равно есть
    [[ "$rc" -ne 0 && -z "$RESTORE_FAIL" && "$RESTORE_PHASE" != "done" ]] && RESTORE_FAIL="операция прервана (код $rc)"
    case "$RESTORE_PHASE" in
        stopped)
            restore_unstage
            restore_code_back
            restore_kernel_back
            restore_prune_mark
            restore_fail_marker
            systemctl start "$SERVICE" 2>/dev/null || true ;;
        swapping)
            restore_rollback
            restore_code_back
            restore_kernel_back
            restore_prune_mark
            restore_fail_marker
            systemctl start "$SERVICE" 2>/dev/null || true ;;
        done)
            systemctl is-active --quiet "$SERVICE" 2>/dev/null || systemctl start "$SERVICE" 2>/dev/null || true ;;
        *)
            # сервис не останавливали: бот работает и об отказе не знает —
            # перезапуск, чтобы новый процесс доложил его из маркера
            [[ "$rc" -ne 0 ]] && restore_kernel_back
            if [[ "$rc" -ne 0 && "$RESTORE_YES" -eq 1 && -n "$RESTORE_FAIL" ]]; then
                restore_fail_marker
                systemctl restart "$SERVICE" 2>/dev/null || true
            fi ;;
    esac
    RESTORE_PHASE=""
    [[ -n "$RESTORE_TMP" ]] && rm -rf -- "$RESTORE_TMP"
    [[ -n "$RESTORE_PENDING" ]] && rm -f -- "$RESTORE_PENDING"
    [[ -n "$RESTORE_PLAN_FILE" ]] && rm -f -- "$RESTORE_PLAN_FILE"
    [[ -n "${RESTORE_RELEASE:-}" ]] && rm -f -- "$RESTORE_RELEASE"
    return "$rc"
}

restore_blocks() {  # DROP заблокированным из итога слияния — тем же входом, что у бота
    bot_tool tools.restore_merge --apply-blocks "$1"
}

restore_provision() {  # новый сервер, обвязка в бэкапе развёрнута, на хосте — нет: развернуть до линков
    local base="$INSTALL_DIR/install"
    if ! systemctl list-unit-files dnsmasq.service 2>/dev/null | grep -q dnsmasq.service; then
        apt-get install -y --no-install-recommends dnsmasq >/dev/null 2>&1 \
            || { warn "dnsmasq не поставился — обвязку РФ-доступа разверни из чата бота"; return 1; }
    fi
    sh "$base/routing-host-setup.sh" --apply >/dev/null \
        && sh "$base/routing-host-setup.sh" --install-unit >/dev/null \
        || { warn "обвязка хоста не развернулась — разверни её из чата бота"; return 1; }
}

cmd_restore() {
    require_root
    is_gateway_role && die "$GW_NO_BACKUPS"
    # --yes: без вопросов (из чата: бот уже показал план и спросил админа);
    # итог и отказ — маркером для бота. --release <архив> — выпуск бэкапа,
    # уже скачанный ботом (sha256 = digest метки); без него терминал скачает сам.
    local yes=0 release=""
    while [[ "${1:-}" == --* ]]; do
        case "$1" in
            --yes) yes=1; shift ;;
            --release) release="${2:-}"; shift 2 || shift ;;
            *) break ;;
        esac
    done
    local src="${1:-}"
    if [[ -n "$src" ]]; then
        [[ -f "$src" ]] || die "не найден: $src"
    else
        [[ -d "$BACKUP_DIR" ]] || die "нет каталога бэкапов $BACKUP_DIR — укажи путь: awg-bot restore <файл>"
        src="$(ls -1t "$BACKUP_DIR"/awg-bot-backup-*.tgz.enc 2>/dev/null | head -n1 || true)"
        [[ -n "$src" ]] || die "бэкапов не найдено в $BACKUP_DIR — укажи путь: awg-bot restore <файл>"
    fi
    RESTORE_TARGETS=(); RESTORE_DONE_T=(); RESTORE_HAD_OLD=(); RESTORE_PHASE=""; RESTORE_FAIL=""
    RESTORE_YES="$yes"; RESTORE_CODE=0; RESTORE_KERNEL=0; RESTORE_TO=""; RESTORE_FROM="$(tree_version "$INSTALL_DIR")"
    RESTORE_RELEASE="$release"
    RESTORE_TMP="$(mktemp -d)"; chmod 700 "$RESTORE_TMP"
    # Файл из чата бот кладёт шифрованным (restore-pending.tgz.enc) и рядом —
    # план из вопроса: при любом исходе оба удаляются, расшифрованное живёт
    # только во временном каталоге.
    RESTORE_PENDING=""; RESTORE_PLAN_FILE=""
    if [[ "$(basename "$src")" == "restore-pending.tgz.enc" ]]; then
        RESTORE_PENDING="$src"; RESTORE_PLAN_FILE="$(dirname "$src")/restore-pending.plan.json"
    fi
    trap restore_exit EXIT
    # тот же замок, что у update, сторожа, фиксации и отката набора: переход на
    # выпуск бэкапа меняет код; __restore_after_update наследует замок через exec
    update_lock -n || restore_die "уже идёт обновление, фиксация или откат — ничего не изменилось"
    local tmp="$RESTORE_TMP" copy="$RESTORE_TMP/copy" err expect="$RESTORE_TMP/plan.json" relsrc=""

    # ── всё, что может отказать, — до остановки сервиса ──
    [[ "$(head -c 6 "$src" 2>/dev/null)" == "AWGBK1" ]] \
        || restore_die "бэкап не зашифрован — восстанавливаются только шифрованные бэкапы (.tgz.enc)"
    if [[ -n "$RESTORE_PENDING" ]]; then
        bot_tool tools.restore_backup --phrase-from-db --out "$tmp/backup.tgz" "$src" >/dev/null 2>&1 \
            || restore_die "не расшифровался — ключевая фраза в базе некорректна или файл повреждён"
    else
        log "шифрованный бэкап — нужна ключевая фраза"
        bot_tool tools.restore_backup --out "$tmp/backup.tgz" "$src" >/dev/null \
            || restore_die "не расшифровался — ключевая фраза некорректна или файл повреждён"
    fi
    mkdir -p "$copy"
    tar xf "$tmp/backup.tgz" -C "$copy" --no-same-owner || restore_die "не удалось распаковать бэкап"
    rm -f "$tmp/backup.tgz"
    [[ -f "$copy/state/backup-meta.json" ]] || restore_die "указанный файл не похож на бэкап"
    err="$(bot_tool tools.check_backup --meta "$copy/state/backup-meta.json" 2>&1 >/dev/null)" \
        || restore_die "${err:-метка бэкапа не прошла проверку}"
    # Версия бэкапа другая — переход на его выпуск (domain/fleet/restore.py):
    # выпуск с GitHub, sha256 = digest метки; всё — до остановки сервиса
    local rel_out
    rel_out="$(bot_tool tools.restore_release --meta "$copy/state/backup-meta.json" --out "$tmp/release.tgz" \
               ${release:+--archive "$release"} 2>"$tmp/release.err")" \
        || restore_die "$(cat "$tmp/release.err" 2>/dev/null || echo "выпуск бэкапа не проверен")"
    local rel_first rel_lines=""; rel_first="$(printf '%s\n' "$rel_out" | head -n1)"
    if [[ -n "$rel_first" && "$rel_first" != "same" ]]; then
        RESTORE_TO="$rel_first"
        RESTORE_DIGEST="$(sed -n 's/.*"digest": *"sha256:\([0-9a-f]*\)".*/\1/p' "$copy/state/backup-meta.json" | head -n1)"
        relsrc="$(restore_release_tree "$tmp/release.tgz" "$tmp/release")" || restore_die "${relsrc:-архив выпуска не распаковался}"
        # повышение через версию совместимости выпуска: его код данных этой
        # версии не принимает — сначала обычное обновление по цепочке минимумов
        local vfloor; vfloor="$(compat_floor "$relsrc")"
        if [[ -n "$vfloor" && -n "$RESTORE_FROM" ]] && version_lt "$RESTORE_FROM" "$vfloor"; then
            restore_die "выпуск v$RESTORE_TO принимает переход только с v$vfloor и новее, а здесь v$RESTORE_FROM — сначала обнови сервер AWG до v$vfloor кнопкой «⬆️ Обновить», затем восстанови бэкап"
        fi
        rel_lines="$(printf '%s\n' "$rel_out" | tail -n +2)"    # строка версии — в план, после заголовка
    fi
    local plan
    plan="$(bot_tool tools.restore_merge --plan "$copy" --save "$expect" 2>&1)" || restore_die "$plan"
    # из чата человек видел план в вопросе бота — сверяем с ним
    [[ -n "$RESTORE_PLAN_FILE" && -f "$RESTORE_PLAN_FILE" ]] && cp "$RESTORE_PLAN_FILE" "$expect"
    if [[ -n "$rel_lines" ]]; then
        # как в вопросе бота: заголовок плана, строка версии, остальное
        printf '%s\n%s\n' "$(printf '%s\n' "$plan" | head -n1)" "$rel_lines"
        printf '%s\n\n' "$(printf '%s\n' "$plan" | tail -n +2)"
    else
        printf '%s\n\n' "$plan"
    fi
    [[ "$yes" -eq 1 ]] || confirm "Продолжить?" n || die "отменено"
    if [[ -n "$RESTORE_TO" && -f "$relsrc/install/awg.lock" ]]; then
        # ядро выпуска бэкапа — до остановки, как у update: не собралось — ничего не тронуто
        log "AmneziaWG: версия выпуска v$RESTORE_TO — собираю до остановки сервиса…"
        RESTORE_KERNEL=1; export RESTORE_KERNEL       # с этого места отказ — ядро прежней версии назад
        AWG_LOCK="$relsrc/install/awg.lock" bash "$relsrc/install/awg-kernel-install.sh" install \
            || restore_die "ядро AmneziaWG выпуска v$RESTORE_TO не собралось — ничего не изменилось"
    fi
    local bk_at
    bk_at="$(grep -o '"created_at": *"[^"]*"' "$copy/state/backup-meta.json" | sed 's/.*"\([^"]*\)"$/\1/')"
    # Каталог конфигов интерфейсов и ключ timezone — по ТЕКУЩЕМУ app.yaml, до
    # его подмены: иначе бэкап сам выбирал бы, куда положить свои конфиги.
    local awgdir; awgdir="$(awg_conf_dir)"
    local tz_had=0
    grep -qE '^timezone:' "$CONF_DIR/app.yaml" 2>/dev/null && tz_had=1

    [[ -n "$RESTORE_TO" ]] && update_guard_disarm       # сторож прежнего обновления — код сменится
    log "останавливаю ${SERVICE}…"; systemctl stop "$SERVICE" 2>/dev/null || true
    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        restore_die "$SERVICE не остановился — ничего не изменилось"
    fi
    RESTORE_PHASE=stopped
    RESTORE_COPY="$copy"; RESTORE_EXPECT="$expect"; RESTORE_BK_AT="$bk_at"; RESTORE_AWGDIR="$awgdir"
    RESTORE_TZ_HAD="$tz_had"; RESTORE_SRC="$src"
    if [[ -n "$RESTORE_TO" ]]; then
        # переход на выпуск бэкапа: первая половина update без старта сервиса,
        # дальше — слияние и замена руками скрипта выпуска бэкапа (новый код
        # читает живую базу и сливает её своим кодом); отказ — прежний код назад
        restore_code_swap "$relsrc"
        # Состояние операции — второй половине через окружение: договор между
        # версиями (скрипт vC передаёт, скрипт vV читает), имена не меняются.
        # RESTORE_YES/TMP/PENDING/PLAN_FILE скрипт обнуляет при загрузке — им
        # отдельные имена AWG_RESTORE_*
        export AWG_RESTORE_YES="$RESTORE_YES" AWG_RESTORE_TMP="$RESTORE_TMP" AWG_RESTORE_PENDING="$RESTORE_PENDING"
        export AWG_RESTORE_PLAN_FILE="$RESTORE_PLAN_FILE"
        export RESTORE_TO RESTORE_FROM RESTORE_DIGEST
        export RESTORE_COPY RESTORE_EXPECT RESTORE_BK_AT RESTORE_AWGDIR RESTORE_TZ_HAD RESTORE_SRC RESTORE_RELEASE
        export AWG_UPDATE_PREV="$INSTALL_DIR/.prev-code"
        RESTORE_FAIL="скрипт выпуска v$RESTORE_TO не запустился"    # exec не прошёл — причина для маркера
        # execfail: неудачный exec не обрывает процесс мимо ловушки — дальше
        # restore_die, и ловушка вернёт прежние код, окружение и ядро
        shopt -s execfail
        exec "$INSTALL_DIR/awg-bot.sh" __restore_after_update
        restore_die "$RESTORE_FAIL"
    fi
    restore_apply
}

restore_release_tree() {  # restore_release_tree TGZ DIR → печатает каталог поставки (или причину, код 1)
    mkdir -p "$2"
    tar xzf "$1" -C "$2" --no-same-owner 2>/dev/null || { echo "архив выпуска не распаковался"; return 1; }
    local main; main="$(find "$2" -maxdepth 3 -type f -path '*/awgbot/__main__.py' | head -n1)"
    [[ -n "$main" ]] || { echo "в архиве выпуска нет awgbot/ — не та поставка"; return 1; }
    local src; src="$(dirname "$(dirname "$main")")"
    [[ "$(tree_version "$src")" == "$RESTORE_TO" ]] \
        || { echo "архив выпуска v$RESTORE_TO несёт другую версию — переходить не буду"; return 1; }
    # вторая половина — скриптом выпуска: без неё exec оставил бы сервис стоять
    grep -q '__restore_after_update)' "$src/awg-bot.sh" 2>/dev/null \
        || { echo "выпуск v$RESTORE_TO не умеет восстанавливать с переходом — переходить не буду"; return 1; }
    chown -R root:root "$src" 2>/dev/null || true
    printf '%s' "$src"
}

restore_code_swap() {  # restore_code_swap SRC — код выпуска бэкапа на место (прежний — в .prev-code)
    local prev="$INSTALL_DIR/.prev-code"
    rm -rf "$prev"; mkdir -p "$prev"
    RESTORE_CODE=1; AWG_UPDATE_PREV="$prev"                 # с этого места отказ — прежний код назад
    # Окружение кода vC — явно: .venvs/.prev мог остаться от прежнего
    # обновления (сторож снят выше) и указывать на окружение vB — откат
    # поставил бы код vC на чужое окружение. Вторая половина (скрипт vV)
    # читает RESTORE_VENV из окружения и возвращает ссылку на него
    RESTORE_VENV=""
    [[ -L "$INSTALL_DIR/venv" ]] && RESTORE_VENV="$(readlink -f "$INSTALL_DIR/venv")"
    mkdir -p "$INSTALL_DIR/.venvs"
    if [[ -n "$RESTORE_VENV" ]]; then
        printf '%s\n' "$RESTORE_VENV" > "$INSTALL_DIR/.venvs/.prev"
    else
        rm -f "$INSTALL_DIR/.venvs/.prev"
    fi
    export RESTORE_VENV
    find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name venv ! -name .venvs ! -name .prev-code ! -name .hold-code \
        -exec mv -t "$prev" {} + || restore_die "прежний код не отодвинулся"
    cp -a "$1"/. "$INSTALL_DIR"/ || restore_die "код выпуска v$RESTORE_TO не лёг целиком"
    chmod +x "$INSTALL_DIR/awg-bot.sh" 2>/dev/null || true
    [[ -x "$INSTALL_DIR/awg-bot.sh" && -f "$INSTALL_DIR/awgbot/__main__.py" ]] \
        || restore_die "код выпуска v$RESTORE_TO лёг не полностью"
}

cmd_restore_after_update() {  # __restore_after_update — вторая половина: скрипт выпуска бэкапа
    # Окружение, юнит и досев conf — как во второй половине update, без старта;
    # затем слияние и замена этим кодом. Отказ до точки невозврата — прежние
    # код и окружение назад (restore_exit), сервис поднят на прежней версии.
    require_root
    RESTORE_TARGETS=(); RESTORE_DONE_T=(); RESTORE_HAD_OLD=(); RESTORE_FAIL=""; RESTORE_CODE=1
    RESTORE_YES="${AWG_RESTORE_YES:-0}"; RESTORE_TMP="${AWG_RESTORE_TMP:-}"
    RESTORE_PENDING="${AWG_RESTORE_PENDING:-}"; RESTORE_PLAN_FILE="${AWG_RESTORE_PLAN_FILE:-}"
    [[ -n "${RESTORE_TO:-}" && -n "${AWG_UPDATE_PREV:-}" ]] || die "__restore_after_update — только из awg-bot restore"
    RESTORE_PHASE=stopped
    trap restore_exit EXIT
    [[ -n "${RESTORE_COPY:-}" && -d "$RESTORE_COPY" ]] || restore_die "распакованный бэкап потерялся по дороге"
    export AWG_IN_UPDATE=1
    RESTORE_FAIL="окружение выпуска v$RESTORE_TO не собралось"   # die посреди шагов — причина для маркера
    update_env_steps restore                    # те же шаги, что у второй половины update
    RESTORE_FAIL=""
    restore_apply
}

restore_apply() {  # после остановки: слияние, подготовка, DROP, замена, подъём, маркер, старт
    local copy="$RESTORE_COPY" expect="$RESTORE_EXPECT" bk_at="$RESTORE_BK_AT" awgdir="$RESTORE_AWGDIR"
    local tz_had="$RESTORE_TZ_HAD" src="$RESTORE_SRC" err
    # Слияние — после остановки: читает живую базу неподвижной, пишет только в
    # распакованный бэкап и сверяет план с вопросом. Отказ — живое не тронуто.
    if ! err="$(bot_tool tools.restore_merge --copy "$copy" --expect "$expect" 2>&1 >/dev/null)"; then
        restore_die "${err:-слияние не прошло} — изменения отменены, сервис поднят"
    fi

    # ── подготовка: каждый заменяемый файл — рядом с целью ──
    local f i c changed="" hooked="" prep=0
    mkdir -p "$DATA_DIR" "$CONF_DIR"
    # Журналы WAL/SHM принадлежат ЗАМЕНЯЕМОЙ базе: рядом с чужой SQLite в
    # лучшем случае их отбросит, в худшем доложит в неё чужие страницы — они
    # уходят в дубль первыми (и возвращаются при откате последними).
    for f in "$DATA_DIR/bot.db-wal" "$DATA_DIR/bot.db-shm"; do
        [[ -e "$f" ]] && RESTORE_TARGETS+=("$f")
    done
    if [[ -f "$copy/state/bot.db" ]]; then
        restore_stage "$copy/state/bot.db" "$DATA_DIR/bot.db" 600 || prep=1
    fi
    for f in "$copy/state/conf"/*.yaml; do
        [[ -f "$f" ]] || continue
        restore_stage "$f" "$CONF_DIR/$(basename "$f")" 644 || prep=1
    done
    if [[ -f "$copy/state/env" ]]; then
        restore_stage "$copy/state/env" "$ENV_FILE" 600 || prep=1
    fi
    for c in "$copy/awg"/*.conf; do
        [[ -f "$c" ]] || continue
        i="$(basename "$c" .conf)"
        cmp -s "$c" "$awgdir/$i.conf" 2>/dev/null && continue
        changed="$changed $i"
        # Конфиг с ДРУГИМИ хуками (PreUp/PostUp/PreDown/PostDown) сам не
        # поднимаем: awg-quick исполняет их от root. Класть — кладём.
        if ! cmp -s <(grep -E '^(Pre|Post)(Up|Down) *=' "$c" 2>/dev/null) \
                    <(grep -E '^(Pre|Post)(Up|Down) *=' "$awgdir/$i.conf" 2>/dev/null); then
            hooked="$hooked $i"
        fi
        restore_stage "$c" "$awgdir/$i.conf" 600 || prep=1
    done
    for c in "$copy/root"/gw-*.conf; do
        [[ -f "$c" ]] || continue
        restore_stage "$c" "$ROOT_GW_DIR/$(basename "$c")" 600 || prep=1
    done
    [[ "$prep" -eq 0 ]] || restore_die "подготовка не прошла — изменения не применены, сервис поднят"

    # ── DROP заблокированным — до подъёма интерфейсов; не встал — замены нет ──
    restore_blocks "$copy/restore-merge.json" \
        || restore_die "DROP заблокированным не наложен — ничего не изменилось, сервис поднят"

    # ── замена ──
    if ! restore_swap; then
        restore_die "замена не прошла — вернул прежнее, сервис поднят: ${RESTORE_ERR:-причина не названа}"
    fi
    [[ "$tz_had" -eq 1 ]] || adopt_timezone quiet
    restore_finish                                      # точка невозврата: дальше — только вперёд
    if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
        # переход на выпуск бэкапа доведён: digest установки — digest метки
        # (бэкапы снова со ссылкой на выпуск); прежние код, окружение и ядро
        # снимаются только после старта сервиса (ниже)
        update_hold_drop                                # удержание прошлого хода — о другом коде
        if [[ -n "${RESTORE_DIGEST:-}" ]]; then
            ( umask 077; printf '%s %s\n' "$RESTORE_TO" "$RESTORE_DIGEST" > "$DATA_DIR/installed-digest" )
        fi
    fi

    # ── новый сервер: обвязка РФ-доступа из бэкапа — до линков ──
    if grep -q '"provision": *true' "$copy/restore-merge.json" 2>/dev/null; then
        restore_provision || true
    fi
    # ── подъём изменившихся интерфейсов, линки пустого сервера — юнитом ──
    for i in $changed; do
        case " $hooked " in *" $i "*)
            warn "$i: в бэкапе другие хуки Pre/Post — положен, но не поднят: проверь и подними руками (awg-quick up $i)"
            continue ;;
        esac
        if [[ -d "$SYS_NET/$i" ]]; then
            awg-quick down "$i" >/dev/null 2>&1 || true
            awg-quick up "$i" >/dev/null 2>&1 || warn "$i не поднялся"
        fi
    done
    local adopt
    adopt="$(sed -n 's/.*"adopt": *\[\([^]]*\)\].*/\1/p' "$copy/restore-merge.json" | tr -d '" ' | tr ',' ' ')"
    for i in $adopt; do
        [[ -n "$i" ]] || continue
        LINK_IF="$i" sh "$INSTALL_DIR/install/routing-link-setup.sh" --adopt >/dev/null \
            || warn "линк $i не поднялся — бот повторит попытку на старте"
    done
    # ── после подъёма, до старта сервиса — то же наложение ещё раз ──
    restore_blocks "$copy/restore-merge.json" || warn "не все DROP наложены — сверка бота на старте поправит"
    [[ -n "$changed" ]] && ok "изменившиеся интерфейсы восстановлены:$changed"

    # маркер для бота: новый процесс доложит итог по каждому шлюзу
    { printf '{"created_at": %s, "source": %s, "version": %s, "merge": ' "$(json_str "$bk_at")" \
          "$(json_str "$(basename "$src")")" "$(json_str "${RESTORE_TO:-}")"
      cat "$copy/restore-merge.json"; printf '}\n'; } > "$DATA_DIR/restore-done.json"
    systemctl start "$SERVICE" 2>/dev/null || true; sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "восстановлено, $SERVICE запущен."
        if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
            update_done                                 # сервис на выпуске бэкапа жив: прежнее прочь
            prune_old_kernel_builds
        fi
    else
        warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
        if [[ "${RESTORE_CODE:-0}" -eq 1 ]]; then
            warn "прежний код v${RESTORE_FROM:-?} оставлен в $INSTALL_DIR/.prev-code — для ручного разбора"
        fi
    fi
}

# ── uninstall (self-removal через отсоединённый пост-хук) ─────────────────────
cmd_uninstall() {
    require_root
    warn "Снятие сервиса awg-bot: будут удалены сервис, код и команда awg-bot."
    confirm "Точно снять awg-bot?" n || { log "отменено."; return 0; }
    # Роль — до того, как конфиг исчезнет: хвост снимает обвязку своей роли
    local role; role="$(sed -nE 's/^ *role: *"?([a-z]+)"?.*/\1/p' "$ETC_DIR/conf/app.yaml" 2>/dev/null | head -n1)"
    # ПОРЯДОК КРИТИЧЕН: сперва БЕЗУСЛОВНО и СИНХРОННО убираем
    # то, из-за чего установка считается установкой (код + симлинк), чтобы
    # прерывание на любом дальнейшем вопросе оставляло чистое поле, а не тупик.
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    update_guard_disarm                    # сторож обновления — без кода ему нечего возвращать
    rm -f "$UNIT_PATH"; systemctl daemon-reload 2>/dev/null || true
    rm -f "$SELF_LINK"
    local tmp_self; tmp_self="$(mktemp)"; cp "$SELF_PATH" "$tmp_self"; chmod +x "$tmp_self"
    rm -rf "$INSTALL_DIR"
    ok "сервис снят, код и команда удалены — хост чист для переустановки."
    AWG_ROLE="${role:-client}" exec "$tmp_self" __post_uninstall
}
# Хвост: опциональная зачистка данных/секретов и обвязки роли (из /tmp-копии,
# прерывание тут уже безопасно — установка полностью снята). Принцип: после
# снятия приложение не должно наследить — всё, что оно само поставило на хост,
# уходит; что было до него (аплинк шлюза, интерфейсы awg с клиентами на ВПС),
# остаётся.
cmd_post_uninstall() {
    warn "ДАННЫЕ: в $DATA_DIR — БД с приватными ключами устройств; в $ETC_DIR — секреты."
    confirm "Удалить $ETC_DIR (секреты + конфиг)?" n && { rm -rf "$ETC_DIR"; ok "удалён $ETC_DIR"; }
    confirm "Удалить $DATA_DIR (БД + бэкапы — НЕОБРАТИМО)?" n && { rm -rf "$DATA_DIR"; ok "удалён $DATA_DIR"; }
    if [[ "${AWG_ROLE:-}" == "gateway" ]]; then
        # Обвязка шлюза — целиком работа бота: линк до сервера AWG, юнит
        # реассерта, таблицы (в них же SSH-фильтр шлюза), /etc/awg-gw,
        # /var/lib/awg-gw, скрипты списков; /opt/awg-gw — остаток прежних
        # выпусков. Аплинк — связь с сервером AWG, поднятая до бота, — не трогается.
        if [[ -x /usr/local/sbin/routing-gw-setup.sh || -d /etc/awg-gw || -d /var/lib/awg-gw ]]; then
            warn "Найдена обвязка шлюза (линк до сервера AWG, юнит и таблицы, /etc/awg-gw, /var/lib/awg-gw)."
            if confirm "Снять её? (SSH-фильтр шлюза уйдёт вместе с таблицами — SSH снова станет открыт для всех адресов)" y; then
                [[ -x /usr/local/sbin/routing-gw-setup.sh ]] \
                    && sh /usr/local/sbin/routing-gw-setup.sh --rollback >/dev/null 2>&1 || true
                rm -rf /etc/awg-gw /var/lib/awg-gw /opt/awg-gw
                rm -f /usr/local/sbin/routing-gw-setup.sh /usr/local/sbin/awg-lan-lists.sh \
                      /usr/local/sbin/awg-lan-domain.sh /usr/local/sbin/awg-lan-services.sh
                ok "обвязка шлюза снята"
            else
                log "обвязка шлюза оставлена. Снять вручную: sh /usr/local/sbin/routing-gw-setup.sh --rollback"
            fi
        fi
    else
        # ВПС: линки до шлюзов и обвязка РФ-доступа (таблицы маршрутизации,
        # резолвер клиентов) — тоже работа бота. Интерфейсы awg с клиентами
        # остаются: VPN как таковой бот не ставил, только управлял им.
        local f name had=""
        for f in /etc/amnezia/amneziawg/awglink*.conf; do [[ -f "$f" ]] && had=1; done
        if [[ -n "$had" || -x /usr/local/sbin/routing-host-setup.sh || -f /etc/dnsmasq.d/awgbot-resolver.conf \
              || -f /etc/systemd/system/awgbot-admin-dns.service ]]; then
            warn "Найдена обвязка РФ-доступа (линки до шлюзов, таблицы маршрутизации, резолвер клиентов и резолвер админа)."
            if confirm "Снять её? (РФ-доступ у клиентов перестанет работать; интерфейсы awg с клиентами остаются)" y; then
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
                # резолвер админа — здесь же, без скрипта поставки: каталог
                # приложения к этому моменту может быть уже снят
                systemctl disable --now awgbot-admin-dns 2>/dev/null || true
                rm -f /etc/systemd/system/awgbot-admin-dns.service
                ip link del awgadmdns 2>/dev/null || true
                rm -rf /etc/awgbot-admin-dns
                systemctl daemon-reload 2>/dev/null || true
                systemctl try-restart dnsmasq 2>/dev/null || true
                ok "обвязка РФ-доступа снята"
            else
                log "обвязка РФ-доступа оставлена. Снять вручную: sh /usr/local/sbin/routing-host-setup.sh --rollback"
            fi
        fi
    fi
    # Firewall: снимаем ТОЛЬКО свою таблицу/файл (их создал awg-bot firewall setup).
    # Снятие адресных drop'ов делает SSH снова открытым для всех — доступ к хосту
    # при этом НЕ теряется (мы только убираем ограничение, а не рвём established).
    # учёт РФ-трафика — таблица без вердиктов, снимаем без вопроса
    nft delete table inet awg_bot_acct 2>/dev/null && ok "таблица учёта РФ-трафика (awg_bot_acct) снята" || true
    # адреса в туннеле локальных сетей шлюзов — без бота половина механизма
    # (таблицы слотов ставит он), снимаем без вопроса
    nft delete table inet awg_lan_map 2>/dev/null && ok "таблица адресов в туннеле (awg_lan_map) снята" || true
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
cmd_firewall() {
    require_root
    ( cd "$INSTALL_DIR" \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
        && exec ./venv/bin/python -m tools.firewall "$@" )
}

cmd_ssh() {  # шлюз: порт sshd, адреса снаружи, фильтр — то же, что раздел «🛡 SSH-доступ» агента
    require_root
    ( cd "$INSTALL_DIR" \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
        && exec ./venv/bin/python -m tools.gwssh "$@" )
}

cmd_first_device() {  # показать конфигурацию первого устройства админа в терминале
    require_installed
    ( cd "$INSTALL_DIR" \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
        && ./venv/bin/python -m tools.first_device "$@" )
}

cmd_awg() {  # awg status | install | reload | plan — ядро по манифесту поставки
    local sub="${1:-status}"
    [[ "$sub" == "status" || "$sub" == "plan" ]] || require_root
    AWG_LOCK="$INSTALL_DIR/install/awg.lock" exec bash "$INSTALL_DIR/install/awg-kernel-install.sh" "$sub"
}

ensure_awg_kernel() {  # первичная установка обеих ролей: AmneziaWG версии манифеста
    echo; log "─── AmneziaWG (версия прибита к поставке: install/awg.lock) ───"
    AWG_LOCK="$INSTALL_DIR/install/awg.lock" bash "$INSTALL_DIR/install/awg-kernel-install.sh" install \
        || die "AmneziaWG не установлен — без ядра бот бесполезен; исправь причину выше и повтори установку"
}

ensure_awg_server() {  # клиентская роль: сервер с нуля или топология уже живого
    local app="$CONF_DIR/app.yaml" out
    out="$(bash "$INSTALL_DIR/install/awg-server-init.sh")" || die "сервер AmneziaWG не создан"
    local created port prefix srv_addr iface
    created="$(sed -nE 's/^CREATED=(.*)$/\1/p' <<<"$out")"
    port="$(sed -nE 's/^LISTEN_PORT=(.*)$/\1/p' <<<"$out")"
    prefix="$(sed -nE 's/^SUBNET_PREFIX=(.*)$/\1/p' <<<"$out")"
    srv_addr="$(sed -nE 's/^SERVER_ADDR=(.*)$/\1/p' <<<"$out")"
    iface="$(sed -nE 's/^AWG_IF=(.*)$/\1/p' <<<"$out")"

    # Топология пишется В ОБОИХ случаях — и когда сервер создали мы, и когда он
    # уже стоял. Прежде её писали только для созданного, и установка на хост с
    # готовым awg0.conf (переустановка, образ с AmneziaWG) умирала на валидации
    # «не задан network.server_port» — уже после ввода токена и сопряжения.
    yaml_set "$app" runtime "\"host\""
    yaml_set "$app" awg_dir "\"/etc/amnezia/amneziawg\""
    [[ -n "$iface" ]] && yaml_set "$app" interface "\"$iface\""
    # Первый клиентский адрес — следующий за адресом сервера: у нашего сервера
    # это .1 → клиенты с .2, у доставшегося от докерной Amnezia .0 → с .1.
    if [[ "$srv_addr" =~ \.([0-9]+)$ ]]; then
        yaml_set "$app" ip_host_start "$(( ${BASH_REMATCH[1]} + 1 ))"
    fi
    [[ -n "$port" ]]   && yaml_set "$app" server_port "$port"
    [[ -n "$prefix" ]] && { yaml_set "$app" subnet_prefix "\"$prefix\""; yaml_set "$app" subnet_cidr "\"${prefix}.0/24\""; }
    awg_state_set AWG_GENERATION_APPLIED "$(lock_get AWG_GENERATION)"
    if [[ "$created" == "1" ]]; then
        ok "сервер AmneziaWG создан: $iface, порт $port, подсеть ${prefix}.0/24 (сервер $srv_addr)"
    else
        ok "сервер AmneziaWG уже был: $iface, порт $port, подсеть ${prefix}.0/24 (сервер $srv_addr)"
    fi
}

resolver_addr() {  # <подсеть>.1 по app.yaml — собственный адрес сервера в туннеле
    local app="$CONF_DIR/app.yaml" prefix
    prefix="$(yaml_get "$app" subnet_prefix)"
    [[ "$prefix" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    printf '%s.1' "$prefix"
}

admin_dns_addr() {  # адрес резолвера админа — тот же ключ и та же проверка, что у бота (infra/admindns.addr)
    local a o1 o2 prefix
    a="$(yaml_get "$CONF_DIR/app.yaml" admin_dns 2>/dev/null || true)"
    prefix="$(yaml_get "$CONF_DIR/app.yaml" subnet_prefix 2>/dev/null || true)"
    if [[ "$a" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        o1="${BASH_REMATCH[1]}"; o2="${BASH_REMATCH[2]}"
        # только частный и вне подсети клиентов — иначе умолчание, как у бота
        if { [[ "$o1" == 10 ]] || { [[ "$o1" == 172 ]] && (( o2 >= 16 && o2 <= 31 )); } \
                || { [[ "$o1" == 192 ]] && [[ "$o2" == 168 ]]; }; } \
                && { [[ -z "$prefix" ]] || [[ "${a%.*}" != "$prefix" ]]; }; then
            printf '%s' "$a"; return
        fi
    fi
    printf '10.255.53.53'
}

ensure_resolver() {  # первая установка клиентской роли: dnsmasq на <подсеть>.1
    # Публичный адрес в поле DNS — это Chrome/Edge, молча уходящие на DoH мимо
    # любого перехвата: условная маршрутизация у такого человека работает
    # «через раз». Приватный адрес браузеру не за что подхватить. Ставим с
    # первого дня, чтобы первое же устройство получило свой резолвер.
    local app="$CONF_DIR/app.yaml" addr
    [[ "$(yaml_get "$app" runtime)" == "host" ]] || return 0
    addr="$(resolver_addr)" || { warn "подсеть не разобрана — DNS клиентов остаётся публичным"; return 0; }
    echo; log "─── Резолвер клиентов: dnsmasq на $addr ───"
    if bash "$INSTALL_DIR/install/awg-resolver-setup.sh" install "$addr"; then
        yaml_set "$app" dns1 "\"$addr\""
        yaml_set "$app" dns2 "\"$addr\""
        ok "DNS клиентов — свой резолвер $addr (оба поля)."
        # резолвер админа (имена SMB локальных сетей шлюзов для устройств
        # админа в туннеле); не поднялся — поставит бот на старте
        ADMIN_ADDR="$(admin_dns_addr)" bash "$INSTALL_DIR/install/awg-admin-dns-setup.sh" install "$addr" \
            || warn "резолвер админа не поднят — бот попробует на старте; awg-bot resolver admin status"
    else
        warn "резолвер не поднят — DNS клиентов остаётся публичным; позже: awg-bot resolver install"
    fi
}

adopt_resolver() {  # обновление: dns1 уже <подсеть>.1 — резолвер ведёт бот, не обвязка
    # Хост, где приватный адрес прописали руками и слушала его обвязка
    # маршрутизации. Файл резолвера появляется у бота, обвязка при следующем
    # реассерте ужимает свой до адреса перехвата. Публичный dns1 не трогаем:
    # переход на свой резолвер — решение админа в боте (переезд профилей).
    local app="$CONF_DIR/app.yaml" addr dns1
    [[ "$(yaml_get "$app" runtime)" == "host" ]] || return 0
    addr="$(resolver_addr)" || return 0
    if [[ -f /etc/dnsmasq.d/awgbot-resolver.conf ]]; then
        # Уже под опекой. Но демон лежит — конфиг переписываем: v2.19.0 писала
        # ключи, которые dnsmasq принимает один раз на все файлы, и хост,
        # прошедший через неё, остался с резолвером в рестарт-цикле.
        systemctl is-active --quiet dnsmasq && return 0
        warn "dnsmasq не активен при живом конфиге резолвера — переписываю конфиг"
        bash "$INSTALL_DIR/install/awg-resolver-setup.sh" install "$addr" \
            || warn "резолвер не поднят — awg-bot resolver install $addr; journalctl -u dnsmasq -o short-iso -e"
        return 0
    fi
    dns1="$(yaml_get "$app" dns1)"
    [[ "$dns1" == "$addr" ]] || return 0
    log "dns1 = $addr — беру резолвер под опеку бота"
    bash "$INSTALL_DIR/install/awg-resolver-setup.sh" install "$addr" \
        || warn "резолвер не взят под опеку — позже: awg-bot resolver install $addr"
}

host_timezone() {  # имя пояса хоста: timedatectl, запасной путь — ссылка /etc/localtime
    local tz
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    [[ -n "$tz" ]] || { tz="$(readlink -f /etc/localtime 2>/dev/null || true)"; [[ "$tz" == */zoneinfo/* ]] && tz="${tz#*/zoneinfo/}" || tz=""; }
    printf '%s' "$tz"
}

TZ_ADOPT_BELOW="3.4.0"   # выпуски ниже писали timezone в app.yaml сами — у них ключ снимается на переходе
update_adopts_timezone() {  # 0 — это обновление снимает ключ timezone: прежняя версия ниже TZ_ADOPT_BELOW
    local prev
    [[ -n "${AWG_UPDATE_PREV:-}" && -d "${AWG_UPDATE_PREV:-}" ]] || return 1
    prev="$(tree_version "$AWG_UPDATE_PREV")"
    [[ -n "$prev" ]] && version_lt "$prev" "$TZ_ADOPT_BELOW"
}

adopt_timezone() {  # adopt_timezone [quiet] — ключ timezone в app.yaml снимается, бот берёт пояс хоста
    # Ключ остался от прежнего шаблона («Europe/Moscow») или его записал
    # установщик; снимается любое значение, только верхнего уровня. Обновление
    # зовёт это только на переходе с выпуска ниже TZ_ADOPT_BELOW. Комментируется, а не
    # очищается: строка с прежним значением видна и возвращается одним движением.
    # Обновление: прежнее значение — в метку $DATA_DIR/.tz-adopted (первый старт
    # бота скажет админу, в каком поясе теперь время, и снимет её); TZ_ADOPTED=1
    # — ключ снял этот прогон: прервётся вторая половина — ловушка вернёт его
    # вместе с прежним кодом (adopt_timezone_undo). quiet (после восстановления
    # копии) — без метки и без отката: пояс бота до восстановления и так был
    # поясом хоста.
    local app="$CONF_DIR/app.yaml" old tz tmp
    [[ -f "$app" ]] && grep -qE '^timezone:' "$app" || return 0
    old="$(yaml_get "$app" timezone)"; old="${old#\'}"; old="${old%\'}"
    tmp="$(mktemp)" || return 0
    sed -E 's|^timezone:|# timezone:|' "$app" > "$tmp" && cat "$tmp" > "$app"
    rm -f "$tmp"
    [[ "${1:-}" == "quiet" ]] || { TZ_ADOPTED=1; mkdir -p "$DATA_DIR" && printf '%s\n' "$old" > "$DATA_DIR/.tz-adopted"; }
    tz="$(host_timezone)"
    log "часовой пояс бота — пояс хоста (${tz:-не определён, UTC}); ключ timezone в app.yaml снят (был: ${old:-пусто})"
}

adopt_timezone_undo() {  # ловушка второй половины: ключ timezone в app.yaml — снова в силе, метки нет
    # Только когда ключ снял этот прогон (TZ_ADOPTED): возвращается строка —
    # раскомментированием на месте
    [[ "${TZ_ADOPTED:-0}" == "1" ]] || return 0
    local app="$CONF_DIR/app.yaml" tmp
    if [[ -f "$app" ]] && ! grep -qE '^timezone:' "$app" && grep -qE '^# timezone:' "$app" && tmp="$(mktemp)"; then
        awk '!d && /^# timezone:/ { sub(/^# /, ""); d = 1 } { print }' "$app" > "$tmp" && cat "$tmp" > "$app"
        rm -f "$tmp"
    fi
    rm -f "$DATA_DIR/.tz-adopted"
    TZ_ADOPTED=0
    warn "ключ timezone в app.yaml возвращён — прежняя версия работает в своём поясе"
}

cmd_resolver() {  # awg-bot resolver status|install|add|remove [ADDR] | admin status|plan|install|remove
    require_installed
    local mode="${1:-status}" addr="${2:-}"
    if [[ "$mode" == "admin" ]]; then
        # резолвер админа: отдаёт остальное резолверу клиентов на <подсеть>.1
        local sub="${2:-status}"
        [[ "$sub" == "status" || "$sub" == "plan" ]] || require_root
        if [[ "$sub" == "install" || "$sub" == "plan" ]]; then
            ADMIN_ADDR="$(admin_dns_addr)" bash "$INSTALL_DIR/install/awg-admin-dns-setup.sh" "$sub" "$(resolver_addr)"
        else
            ADMIN_ADDR="$(admin_dns_addr)" bash "$INSTALL_DIR/install/awg-admin-dns-setup.sh" "$sub"
        fi
        return
    fi
    [[ "$mode" == "status" || "$mode" == "plan" ]] || require_root
    [[ -n "$addr" ]] || addr="$(resolver_addr || true)"
    bash "$INSTALL_DIR/install/awg-resolver-setup.sh" "$mode" "$addr"
}

cmd_routing_doctor() {
    require_installed
    # Тем же интерпретатором, из того же каталога и с тем же conf/data, что и
    # сам бот, — иначе диагностика проверит не ту установку и соврёт. Каталог
    # обязателен: пакет лежит в $INSTALL_DIR, а не в site-packages.
    ( cd "$INSTALL_DIR" && AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
      ./venv/bin/python -m awgbot.runtime.routing_doctor )
}

cmd_lan() {
    # VPN-транзит (локальная сеть без VPN) на шлюзе: личные
    # списки и обновление фидов. Делегируем скриптам, которые кладёт обвязка
    # шлюза (routing-gw-setup.sh) — источник истины один; глагол здесь ради
    # находимости, как gw-kit. Только роль gateway с включённой функцией.
    require_root
    local dom="/usr/local/sbin/awg-lan-domain.sh" lists="/usr/local/sbin/awg-lan-lists.sh"
    case "${1:-}" in
        add|ru|del|list)
            [[ -x "$dom" ]] || die "VPN-транзит на этом шлюзе не включён (нет $dom) — включи его в боте сервера AWG: «🛰 Шлюзы» → карточка шлюза → «☑️ VPN-транзит»"
            exec "$dom" "$@" ;;
        update)
            [[ -x "$lists" ]] || die "VPN-транзит на этом шлюзе не включён (нет $lists)"
            exec "$lists" ;;
        *) die "usage: awg-bot lan add|ru|del <домен…> | list | update" ;;
    esac
}

cmd_gw_kit() {
    # Поставка для шлюза без доступа к GitHub: код этой установки и исходники
    # AmneziaWG из кэша сервера, без ключей и токенов (tools/gwkit.py)
    require_root; require_installed
    is_gateway_role && die "gw-kit собирается на сервере AWG, а здесь шлюз"
    [[ "${1:-}" == "--stdout" ]] || die "usage: awg-bot gw-kit --stdout | ssh root@ШЛЮЗ 'cat > /root/awg-gw-kit.sh && sh /root/awg-gw-kit.sh --join <токен AWGJ1…>'"
    bot_tool tools.gwkit --stdout
}

cmd_join() {  # join <команда подключения> | --status — шлюз: подключение командой из бота сервера AWG
    require_root
    if [[ "${1:-}" == "--status" ]]; then
        bot_tool tools.join --status
        return
    fi
    # токен — окружением (AWG_JOIN_TOKEN: установщик, обновление с подключением)
    # или строкой руками; python получает его только окружением — не в argv
    local tok="${AWG_JOIN_TOKEN:-}"
    [[ -n "${1:-}" ]] && tok="$*"
    [[ -n "$tok" ]] || die "usage: awg-bot join <команда подключения из бота сервера AWG> | --status"
    if ! is_gateway_role; then
        # первая установка агента идёт установщиком; здесь — уже поставленный агент
        die "агент шлюза не настроен — выполни команду подключения целиком (curl … --join …)"
    fi
    AWG_JOIN_TOKEN="$tok" bot_tool tools.join
}

cmd_status() {
    echo "Пути: код=$INSTALL_DIR conf=$CONF_DIR env=$ENV_FILE data=$DATA_DIR"
    systemctl status "$SERVICE" --no-pager -l -o short-iso 2>/dev/null | head -n 12 || warn "юнит $SERVICE не найден"
}
cmd_logs() { exec journalctl -u "$SERVICE" -n 200 -f -o short-iso; }   # время со смещением

cmd_start()   { require_root; systemctl start "$SERVICE";   _svc_feedback "запущен"; }
cmd_stop()    { require_root; systemctl stop "$SERVICE";    _svc_feedback "остановлен"; }
cmd_restart() { require_root; systemctl restart "$SERVICE"; _svc_feedback "перезапущен"; }
_svc_feedback() {  # короткий итог после start/stop/restart
    sleep 1
    if systemctl is-active --quiet "$SERVICE"; then
        ok "$SERVICE $1 (active)."
    else
        [[ "$1" == "остановлен" ]] && ok "$SERVICE $1." \
            || warn "$SERVICE не активен — journalctl -u $SERVICE -e -o short-iso"
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
  awg-bot update [tgz]       обновить код из архива (по умолч. awg-bot.tgz рядом)
  awg-bot backup             шифрованный бэкап сервера AWG (БД, конфиг, секреты, интерфейсы, шлюзы)
  awg-bot restore [файл]     восстановить из бэкапа со слиянием (по умолч. — самый свежий);
                             бэкап другой версии — сначала переход на его выпуск с GitHub
  awg-bot logs               журнал сервиса (follow)
  awg-bot firewall <cmd>     файервол хоста (единственная точка — таблица awg_bot_guard):
                             status | setup | confirm [--disable-ufw] | apply [--no-rollback] |
                             allow <ip…> | deny <ip…> | off | rollback
  awg-bot ssh <cmd>          шлюз: доступ по SSH (порт — факт от sshd, фильтр снаружи):
                             status | port <N> | allow <ip|cidr|имя…> | deny … | on | off
  awg-bot routing-doctor     где рвётся РФ-доступ (только чтение)
  awg-bot lan <cmd>          шлюз, VPN-транзит: add|ru|del <домен…> — свои списки
                             (в туннель / напрямую / убрать), list, update — обновить фиды
  awg-bot gw-kit --stdout    собрать поставку для шлюза без доступа к GitHub (без ключей и токенов):
                             sudo awg-bot gw-kit --stdout | ssh root@ШЛЮЗ 'cat > /root/awg-gw-kit.sh && sh /root/awg-gw-kit.sh --join <токен AWGJ1…>'
                             (токен — из команды бота; агент на шлюзе уже стоит — добавь --yes)
  awg-bot join <команда>     шлюз: подключиться к серверу AWG командой из его бота;
                             join --status — код и ход подключения ещё раз
  awg-bot awg <cmd>          ядро AmneziaWG по манифесту поставки (install/awg.lock):
                             status | install | reload | prune | plan
  awg-bot first-device       конфигурация первого устройства админа в терминал
                             (ссылка, QR и файл — когда Telegram недоступен)
  awg-bot resolver <cmd>     свой DNS-резолвер клиентов (dnsmasq на <подсеть>.1):
                             status | install [addr] | add <addr> | remove <addr> | plan |
                             dropin — обновить override юнита dnsmasq (зовёт бот на старте);
                             admin status|plan|install|remove — резолвер админа (client_config.admin_dns;
                             имена SMB локальных сетей шлюзов, «SMB в туннеле»)
  awg-bot uninstall          удалить приложение (опционально: данные приложения)
EOF
}

VERB="${1:-}"; shift || true
case "$VERB" in
    reconfigure) cmd_reconfigure "$@" ;;
    update)      cmd_update "${1:-}" ;;
    backup)      cmd_backup ;;
    restore)     cmd_restore "$@" ;;
    uninstall)   cmd_uninstall ;;
    __post_update)    cmd_post_update "${1:-0}" ;;
    # pid в update-running — пока процесс под замком работает; дальше он ничего не значит
    __update_guard)   cmd_update_guard "$@" && _rc=0 || _rc=$?; rm -f "$DATA_DIR/update-running"; exit "$_rc" ;;
    __update_commit)  cmd_update_commit      && _rc=0 || _rc=$?; rm -f "$DATA_DIR/update-running"; exit "$_rc" ;;
    __update_revert)  cmd_update_revert "$@" && _rc=0 || _rc=$?; rm -f "$DATA_DIR/update-running"; exit "$_rc" ;;
    __restore_after_update) cmd_restore_after_update ;;
    __post_uninstall) cmd_post_uninstall ;;
    status)      cmd_status ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart)     cmd_restart ;;
    logs)        cmd_logs ;;
    firewall)    cmd_firewall "$@" ;;
    ssh)         cmd_ssh "$@" ;;
    routing-doctor) cmd_routing_doctor ;;
    gw-kit)      cmd_gw_kit "$@" ;;
    join)        cmd_join "$@" ;;
    lan)         cmd_lan "$@" ;;
    awg)         cmd_awg "$@" ;;
    first-device) cmd_first_device "$@" ;;
    resolver)    cmd_resolver "$@" ;;
    -h|--help|help|"") usage ;;
    *) usage; die "неизвестная команда: $VERB" ;;
esac
