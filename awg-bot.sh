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
# Обновление со сторожем и набором (update, __post_update, __update_*) и
# бэкап с восстановлением (backup, restore, __restore_after_update) — частями
# в install/lib (update.sh, restore.sh): скрипт читает их при разборе.
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
#                          если явно не согласиться на их удаление; без
#                          терминала — самообновление из бота транзиентным
#                          юнитом awg-bot-selfupdate-* — вопросы не задаются,
#                          берётся умолчание, данные остаются). Распаковка
#                          (--no-same-owner, код — root), проверка состава и сборка
#                          ядра — ДО остановки сервиса; прежний код отодвигается в
#                          $INSTALL_DIR/.prev-code и при отказе подмены возвращается
#                          ловушкой, сервис поднимается. Окружение Python вторая
#                          половина собирает РЯДОМ с рабочим (копия в
#                          $INSTALL_DIR/.venvs/ + pip) и переключает ссылкой venv;
#                          отказ любого шага до старта сервиса возвращает и код
#                          (.prev-code), и окружение (.venvs/.prev).
#                          Из установщика (пункт «обновить» поверх установки):
#                          AWG_UPDATE_KEEP=1 — вопрос об удалении данных не
#                          задаётся; AWG_UPDATE_THEN_JOIN=<токен> — шлюзу после
#                          обновления подключиться командой новым кодом
#                          (`awg-bot join`, токен — в AWG_JOIN_TOKEN).
#                          При паре резерва после проверки архива и минимума,
#                          до остановки сервиса — сторож reserve_busy_guard
#                          (tools/rsvbusy.py, только чтение базы) — код 3 и
#                          отказ с причиной («обновление сейчас невозможно: …»):
#                          свои пользователи у резерва (или едут); здесь
#                          пользователи второго сервера; затем ожидание — теми
#                          же словами, что отказ «⬆️ Обновить» в боте: свой
#                          хвост правок ещё переносится на резерв, правки бота
#                          с резерва после «вернуть без резерва» ещё не пришли
#                          (база открылась, а проверка хвоста упала — тоже
#                          отказ: «состояние резерва не прочитано (…) —
#                          повтори через минуту»), с хвостом-подсказкой обхода.
#                          AWG_RESERVE_IGNORE_WAIT=1 снимает только ожидание и
#                          непрочитанное состояние (изменения при этом могут
#                          быть потеряны), не отъезд и не гостя (переехавший
#                          инстанс второго сервера здесь). Базы нет или в ней нет пары — свободен.
#                          stderr сторожа (обход ожидания, «база не прочитана —
#                          проверку резерва пропускаю») печатается человеку.
#                          AWG_UPDATE_CLEANUP=<путь> (установщик и самообновление
#                          из бота) — архив или каталог поставки убрать после
#                          обновления; только из /tmp, чужое не трогается.
#                          Поставка ниже версии совместимости установленного
#                          кода (COMPAT_FLOOR, awgbot/core/compat.py) — отказ до
#                          остановки сервиса и до вопроса об удалении данных
#                          (порядок: распаковка → версия → минимум → digest →
#                          вопрос → Python → ядро): откат — только чистой
#                          установкой. Установка ниже минимума обновления новой
#                          поставки (UPDATE_FROM там же) — отказ там же:
#                          «обновиться с vA сразу до vB нельзя — сначала обнови
#                          до vMIN:» и строкой ниже команда curl … vMIN/awg-bot.tgz
#                          && sudo awg-bot update /tmp/awg-bot.tgz (репозиторий —
#                          repo из updates.yaml; шагов перехода с версий ниже
#                          минимума в поставке нет).
#                          Копии обвязки (routing-host-setup.sh или
#                          routing-gw-setup.sh) и скрипта линка в
#                          /usr/local/sbin освежаются, если они там уже есть;
#                          откат неудачного обновления шлюза возвращает и
#                          прежнюю копию routing-gw-setup.sh (gw_plumbing_sync).
#                          Pid апдейтера под замком — в
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
#                          шлюзов, таблицы маршрутизации, резолвер пользователей и
#                          резолвер админа).
#                          Что было до бота — аплинк шлюза, интерфейсы awg с
#                          пользователями — остаётся. Линк пары резерва и гостевую
#                          зону — без вопроса; зону — её же скриптом (копия
#                          во временный файл до удаления кода, хвосту — в
#                          AWG_ZONE_SCRIPT): --teardown и --wipe снимают клоны,
#                          CAKE, ip rule iif awggr-h, юниты и журнал гостя.
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
#                          сервиса; последним перед остановкой — тот же сторож
#                          резерва (reserve_busy_guard, «восстановление сейчас
#                          невозможно: …»). После остановки — слияние с текущим
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
#   reserve status|plan|selftest|open|zone --<режим>|teardown|tgpool show|refresh
#                          сервер AWG, взаимный резерв: гостевая зона и процесс
#                          открытия. status — линк пары и зона (reserve-pair-setup.sh
#                          --status, reserve-zone-setup.sh --status) и РФ-доступ
#                          через второй сервер: rfpeer=offer|slot|offer,slot|off
#                          (файл согласия $DATA_DIR/reserve/rfpeer.json: даю шлюзы /
#                          хожу через шлюзы второго / обе метки / нет) и allowed= (AllowedIPs
#                          линка пары в ядре: у просящего — с 0.0.0.0/0); plan — что
#                          умеет хост (хранилище в памяти, порт вебхука, dnsmasq,
#                          conntrack, python);
#                          selftest — механика зоны на модуле ядра (root);
#                          open — tools/rsvopen.py (root; вход — JSON ТОЛЬКО в
#                          stdin: точки кворума с ящиком, код с точкой и ящиком
#                          или ключ пакета, секреты не в argv;
#                          ход — /run/awg-bot/rsv/open.status; при любом
#                          переезде — кнопкой, по точкам кворума, по аварийному
#                          коду — его зовёт бот резерва временным юнитом
#                          awg-bot-rsvopen, руками — для проверки и разбора);
#                          zone --<режим> — скрипт зоны напрямую (root);
#                          teardown — аварийно зона прочь: --teardown, затем
#                          --wipe (root; пользователи основы на резерве обрываются,
#                          бот резерва увидит пустую зону по состоянию);
#                          tgpool show — файл пула адресов Telegram
#                          $DATA_DIR/reserve/tg_pool.json (ядро — в коде, не в
#                          файле); tgpool refresh — отметка
#                          $DATA_DIR/reserve/tg_pool.refresh (root): её снимает
#                          минутный такт бота резерва и обновляет анонсы вне
#                          расписания.
#                          uninstall снимает линк пары и зону без вопроса.
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
confirm() {  # confirm "prompt" "default(y/n)" → 0/1  (Enter = default; без терминала — default молча)
    local p="$1" d="${2:-n}" a hint="[y/N]"
    [[ "$d" == "y" ]] && hint="[Y/n]"
    if { : < /dev/tty; } 2>/dev/null; then
        read -r -p "$p $hint: " a < /dev/tty; a="${a:-$d}"
    else
        a="$d"                                  # апдейтер под systemd-run: терминала нет, ответ — умолчание
    fi
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
    # На шлюзе таблицу awg_gw_guard ставит юнит обвязки, и живёт она только в
    # ядре — после ребута её восстанавливает он же. Без упорядочивания агент
    # успевал сделать проверки раньше и первым сообщением после каждой
    # перезагрузки говорил «таблицы нет, шлюз открыт пользователям туннеля».
    # Wants, а не Requires: не отработавшая обвязка не должна уносить агента —
    # через него её и чинят.
    local link_after="" link_want=""
    if [[ "$(yaml_get "$CONF_DIR/app.yaml" role)" == "gateway" ]]; then
        link_after=" awg-link-gw.service"; link_want=" awg-link-gw.service"
    fi
    cat > "$UNIT_PATH" <<EOF
[Unit]
Description=AmneziaWG Telegram bot
After=network-online.target${link_after}
Wants=network-online.target${link_want}

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
                _pub="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
                if [[ -n "$_pub" && "$_pub" != "$host" ]]; then
                    log "локальный адрес $host приватный (хост за NAT) — беру внешний $_pub"
                    host="$_pub"
                fi ;;
        esac
        [[ -n "$host" ]] || host="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
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

    # Имя сервера — это description в vpn:// (видно в приложении Amnezia у пользователя).
    # Одностороннее: попадает в НОВЫЕ ссылки; уже импортированные у пользователей не
    # меняются. При reconfigure текущее имя предлагается как дефолт (Enter — оставить).
    local srv_name cur_name
    cur_name="$(yaml_get "$app" server_name)"; [[ -n "$cur_name" ]] || cur_name="Сервер 1"
    ask srv_name "Имя сервера в ссылках (видно пользователю в приложении)" "$cur_name"
    yaml_set "$app" server_name "\"$srv_name\""

    # Порт awg и VPN-подсеть: приоритет — текущее значение из конфига (так
    # reconfigure с Enter сохраняет уже настроенное), затем автодетект из живого
    # конфига интерфейса (detect_topology), затем вопрос. Спрашиваем ТОЛЬКО то,
    # что ни конфиг, ни детект не дали: первичная установка до сервера.
    local det="" cur_port cur_prefix d_port="" d_prefix="" d_cidr=""
    cur_port="$(yaml_get "$app" server_port)"
    cur_prefix="$(yaml_get "$app" subnet_prefix)"
    if [[ -x "$INSTALL_DIR/venv/bin/python" ]]; then
        det="$(cd "$INSTALL_DIR" && AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
            ./venv/bin/python -c "
from awgbot.infra import awg
t = awg.detect_topology()
print('%s|%s|%s' % (t['listen_port'] or '', t['subnet_prefix'] or '', t['subnet_cidr'] or ''))
" 2>/dev/null || true)"
        d_port="${det%%|*}"; local d_rest="${det#*|}"
        d_prefix="${d_rest%%|*}"; d_cidr="${d_rest#*|}"
    fi
    if [[ -z "$d_port$cur_port" || -z "$d_prefix$cur_prefix" ]]; then
        warn "порт/подсеть awg не определены — спрошу их ниже."
    fi

    # ── Порт ──────────────────────────────────────────────────────────────────
    local port
    if [[ -n "$cur_port" ]]; then
        port="$cur_port"                     # reconfigure: конфиг в приоритете
        [[ -n "$d_port" && "$d_port" != "$cur_port" ]] && \
            warn "в конфиге интерфейса порт $d_port, в app.yaml $cur_port — оставляю app.yaml (меняй тут, если надо)."
    elif [[ -n "$d_port" ]]; then
        port="$d_port"; ok "порт из конфига интерфейса: $port"
    else
        while :; do
            ask port "Порт awg-сервера (server_port)"
            [[ "$port" =~ ^[0-9]+$ ]] && break; warn "порт — число"
        done
    fi
    yaml_set "$app" server_port "$port"

    # ── Подсеть (первые три октета) ───────────────────────────────────────────
    local prefix
    if [[ -n "$cur_prefix" ]]; then
        prefix="$cur_prefix"
        [[ -n "$d_prefix" && "$d_prefix" != "$cur_prefix" ]] && \
            warn "в конфиге интерфейса подсеть ${d_prefix}.0, в app.yaml ${cur_prefix}.0 — оставляю app.yaml."
    elif [[ -n "$d_prefix" ]]; then
        prefix="$d_prefix"; ok "подсеть из конфига интерфейса: ${d_cidr:-${prefix}.0/24}"
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
    # основного бота не должна получить чужую секцию).
    local app="$CONF_DIR/app.yaml"
    [[ -f "$app" ]] || die "нет $app — сначала seed_conf"
    sed -i -E 's/^# (role: "gateway")/\1/; s/^# (gateway:)/\1/' "$app"
    sed -i -E 's/^#   ((link_interface|conf_dir|unit|client_subnet|monitor_minutes|handshake_max_age|link_alert_streak|temp_alert_c|ipv4_only):)/  \1/' "$app"
    # подсеть пользователей не спрашиваем: она приезжает в конфигурации с ВПС и
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
        # ни email-выхода. Общее с основным ботом — python, venv, секреты,
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
        ensure_resolver                    # свой DNS пользователям: dnsmasq на <подсеть>.1
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

# ── update, сторож и набор обновлений; backup / restore — частями скрипта ────
# Части лежат в поставке рядом (install/lib) и читаются при разборе этого
# файла: после подмены кода новый awg-bot.sh берёт и новые части (exec в
# cmd_update). Сами по себе не запускаются.
. "$SELF_DIR/install/lib/update.sh"
. "$SELF_DIR/install/lib/restore.sh"

# ── host-режим: интерфейс на автозагрузке, мусор прежних версий — долой ───────
ensure_host_autostart() {
    # Сам юнит бота — на автозагрузке, обе роли. Ребут шлюза показал, что
    # установка могла оставить его disabled; проверяем на каждом обновлении.
    if [[ "$(systemctl is-enabled "$SERVICE" 2>/dev/null)" != "enabled" ]]; then
        systemctl enable "$SERVICE" >/dev/null 2>&1 \
            && ok "$SERVICE включён на автозагрузку." \
            || warn "не удалось включить $SERVICE на автозагрузку — после ребута бот не поднимется."
    fi
    # Дыра, найденная ребутом ВПС: интерфейс поднимает awg-quick@<iface> — и
    # его никто не включал. Ребут оставлял хост без туннелей. Включаем на
    # каждом обновлении: idempotent.
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
    # новое ядро не обслужит пользователей прежнего; тогда рядом со старым
    # интерфейсом поднимается второй, на новых параметрах, и бот просит
    # админа начать переезд профилей. Сам переезд — в UI, не здесь.
    local app="$CONF_DIR/app.yaml"
    [[ -f "$AWG_LOCK_FILE" && -f "$app" ]] || return 0
    [[ "$(yaml_get "$app" role)" != "gateway" ]] || return 0     # у шлюза пользователей нет
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
    local conf_dir; conf_dir="$(yaml_get "$app" conf_dir)"; conf_dir="${conf_dir:-/etc/amnezia/amneziawg}"
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

    log "ядро поколения $want не обслужит профили поколения $applied — поднимаю второй интерфейс $new_if ($new_prefix.0/24) под переезд профилей"
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

# ── роль установки и инструменты кодом установки (update, restore, join, gw-kit) ──
is_gateway_role() {  # роль установки — шлюз (role: gateway в app.yaml)
    [[ "$(yaml_get "$CONF_DIR/app.yaml" role 2>/dev/null)" == "gateway" ]]
}

bot_tool() {  # bot_tool MODULE ARGS… — python -m MODULE кодом установки, с путями этой установки
    ( cd "$INSTALL_DIR" \
        && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
        && ./venv/bin/python -m "$@" )
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
    # скрипт зоны резерва — рядом: хвост снимает зону им (CAKE, ip rule, журнал гостя)
    local tmp_zone=""
    if [[ -f "$INSTALL_DIR/install/reserve-zone-setup.sh" ]]; then
        tmp_zone="$(mktemp)"; cp "$INSTALL_DIR/install/reserve-zone-setup.sh" "$tmp_zone"
    fi
    rm -rf "$INSTALL_DIR"
    ok "сервис снят, код и команда удалены — хост чист для переустановки."
    AWG_ROLE="${role:-client}" AWG_ZONE_SCRIPT="$tmp_zone" exec "$tmp_self" __post_uninstall
}
# Хвост: опциональная зачистка данных/секретов и обвязки роли (из /tmp-копии,
# прерывание тут уже безопасно — установка полностью снята). Принцип: после
# снятия приложение не должно наследить — всё, что оно само поставило на хост,
# уходит; что было до него (аплинк шлюза, интерфейсы awg с пользователями на ВПС),
# остаётся.
cmd_post_uninstall() {
    warn "ДАННЫЕ: в $DATA_DIR — БД с приватными ключами устройств; в $ETC_DIR — секреты."
    confirm "Удалить $ETC_DIR (секреты + конфиг)?" n && { rm -rf "$ETC_DIR"; ok "удалён $ETC_DIR"; }
    confirm "Удалить $DATA_DIR (БД + бэкапы — НЕОБРАТИМО)?" n && { rm -rf "$DATA_DIR"; ok "удалён $DATA_DIR"; }
    # пространство зондов кворума резерва (обе роли) и его конфиги — работа бота, прочь
    ip netns del awg-quo 2>/dev/null || true
    rm -rf /var/lib/awg-gw/quorum
    if [[ "${AWG_ROLE:-}" != "gateway" ]]; then
        # ВПС: пиры-зонды и второй адрес ответчика (169.254.88.0/24) на интерфейсах AWG — тоже работа бота;
        # интерфейсы с пользователями остаются
        local qf qi pub
        for qf in /etc/amnezia/amneziawg/*.conf; do
            [[ -f "$qf" ]] && grep -q '169\.254\.88\.' "$qf" || continue
            qi="$(basename "$qf" .conf)"
            while read -r pub _; do
                [[ -n "$pub" ]] && awg set "$qi" peer "$pub" remove 2>/dev/null || true
            done < <(awg show "$qi" allowed-ips 2>/dev/null | grep '169\.254\.88\.' || true)
            ip -4 addr show dev "$qi" 2>/dev/null | awk '/inet 169\.254\.88\./{print $2}' | while read -r a; do ip addr del "$a" dev "$qi" 2>/dev/null || true; done
            awk -v RS= -v ORS='\n\n' '!/AllowedIPs *= *169\.254\.88\./' "$qf" | sed '/^Address *= *169\.254\.88\./d' > "$qf.tmp" && mv "$qf.tmp" "$qf"
        done
    fi
    if [[ "${AWG_ROLE:-}" == "gateway" ]]; then
        # Обвязка шлюза — целиком работа бота: линк до сервера AWG, юнит
        # реассерта, таблицы (в них же SSH-фильтр шлюза), /etc/awg-gw,
        # /var/lib/awg-gw, скрипты списков; /opt/awg-gw — остаток прежних
        # выпусков. Аплинк — связь с сервером AWG, поднятая до бота, — не трогается.
        if [[ -x /usr/local/sbin/routing-gw-setup.sh || -d /etc/awg-gw || -d /var/lib/awg-gw ]]; then
            warn "Найдена обвязка шлюза (линк до сервера AWG, линк к резерву, юниты и таблицы, /etc/awg-gw, /var/lib/awg-gw)."
            if confirm "Снять её? (SSH-фильтр шлюза уйдёт вместе с таблицами — SSH снова станет открыт для всех адресов)" y; then
                # постоянный линк к резерву (awgrsv, своя таблица и юнит) — раньше обвязки;
                # каталог установки к этому моменту снят — копия скрипта в /usr/local/sbin
                [[ -x /usr/local/sbin/reserve-gw-setup.sh ]] \
                    && sh /usr/local/sbin/reserve-gw-setup.sh --remove >/dev/null 2>&1 || true
                rm -f /usr/local/sbin/reserve-gw-setup.sh
                [[ -x /usr/local/sbin/routing-gw-setup.sh ]] \
                    && sh /usr/local/sbin/routing-gw-setup.sh --rollback >/dev/null 2>&1 || true
                rm -rf /etc/awg-gw /var/lib/awg-gw /opt/awg-gw
                rm -f /usr/local/sbin/routing-gw-setup.sh /usr/local/sbin/awg-lan-lists.sh \
                      /usr/local/sbin/awg-lan-domain.sh /usr/local/sbin/awg-lan-services.sh
                ok "обвязка шлюза снята"
            else
                log "обвязка шлюза оставлена. Снять вручную: sh /usr/local/sbin/reserve-gw-setup.sh --remove (линк к резерву, если есть), затем sh /usr/local/sbin/routing-gw-setup.sh --rollback"
            fi
        fi
    else
        # ВПС: линки до шлюзов и обвязка РФ-доступа (таблицы маршрутизации,
        # резолвер пользователей) — тоже работа бота. Интерфейсы awg с пользователями
        # остаются: VPN как таковой бот не ставил, только управлял им.
        local f name had=""
        for f in /etc/amnezia/amneziawg/awglink*.conf; do [[ -f "$f" ]] && had=1; done
        if [[ -n "$had" || -x /usr/local/sbin/routing-host-setup.sh || -f /etc/dnsmasq.d/awgbot-resolver.conf \
              || -f /etc/systemd/system/awgbot-admin-dns.service ]]; then
            warn "Найдена обвязка РФ-доступа (линки до шлюзов, таблицы маршрутизации, резолвер пользователей и резолвер админа)."
            if confirm "Снять её? (РФ-доступ у пользователей перестанет работать; интерфейсы awg с пользователями остаются)" y; then
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
    # Взаимный резерв серверов: линк пары, гостевая зона с хранилищем в памяти
    # и её юниты — целиком работа бота; снимаем без вопроса (данных основы на
    # диске нет, только шифротексты).
    if [[ -x /usr/local/sbin/reserve-pair-setup.sh || -e /run/netns/awg-guest || -f /etc/amnezia/amneziawg/awgpair.conf \
          || -f /etc/systemd/system/awg-bot-guest.service ]]; then
        if [[ -x /usr/local/sbin/reserve-pair-setup.sh ]]; then
            sh /usr/local/sbin/reserve-pair-setup.sh --down >/dev/null 2>&1 || true
            rm -f /usr/local/sbin/reserve-pair-setup.sh
        fi
        # зона — её же скриптом (копия сделана до удаления кода): клоны, CAKE, ip rule,
        # юниты и журнал гостя, хранилище; без копии — руками
        if [[ -n "${AWG_ZONE_SCRIPT:-}" && -f "${AWG_ZONE_SCRIPT:-}" ]]; then
            bash "$AWG_ZONE_SCRIPT" --teardown >/dev/null 2>&1 || true
            bash "$AWG_ZONE_SCRIPT" --wipe >/dev/null 2>&1 || true
            rm -f "$AWG_ZONE_SCRIPT"
        fi
        systemctl disable --now awg-pair.service awg-bot-guest.service awg-bot-guest-dns.service 2>/dev/null || true
        systemctl stop systemd-journald@awg-guest.service 2>/dev/null || true
        rm -f /etc/systemd/system/awg-pair.service /etc/systemd/system/awg-bot-guest.service \
              /etc/systemd/system/awg-bot-guest-dns.service /etc/systemd/journald@awg-guest.conf
        rm -f /etc/amnezia/amneziawg/awgpair.conf /etc/amnezia/amneziawg/awgpair.conf.next /etc/amnezia/amneziawg/awgpair.conf.prev
        if mountpoint -q /run/awg-bot-guest 2>/dev/null; then umount -l /run/awg-bot-guest 2>/dev/null || true; fi
        while ip rule del iif awggr-h 2>/dev/null; do :; done
        while ip rule del pref 151 2>/dev/null; do :; done     # транзит РФ второй стороны (reserve/rfpeer)
        rm -rf /run/awg-bot-guest /var/lib/awg-bot-guest /run/awg-bot/rsv /run/log/journal/*.awg-guest
        ip netns del awg-guest 2>/dev/null || true
        systemctl daemon-reload 2>/dev/null || true
        ok "линк пары и гостевая зона резерва сняты"
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

cmd_reserve() {  # reserve status | plan | selftest | open | zone | teardown | tgpool — взаимный резерв серверов
    local sub="${1:-status}"; shift || true
    local zone="$INSTALL_DIR/install/reserve-zone-setup.sh"
    case "$sub" in
        status)
            require_installed
            [[ -x /usr/local/sbin/reserve-pair-setup.sh ]] && sh /usr/local/sbin/reserve-pair-setup.sh --status 2>/dev/null || echo "pair=absent"
            # РФ-доступ через второй сервер: файл согласия бота и AllowedIPs линка пары (рантайм)
            if [[ -f "$DATA_DIR/reserve/rfpeer.json" ]]; then
                local _rfp; _rfp="$(tr -d '\n ' < "$DATA_DIR/reserve/rfpeer.json")"
                local _rfl=""
                case "$_rfp" in *'"offer":true'*) _rfl="offer" ;; esac
                case "$_rfp" in *'"slot":true'*) _rfl="${_rfl:+$_rfl,}slot" ;; esac
                echo "rfpeer=${_rfl:-off}"
            else
                echo "rfpeer=off"
            fi
            command -v awg >/dev/null 2>&1 && echo "allowed=$(awg show awgpair allowed-ips 2>/dev/null | awk '{$1=""; print}' | tr -d '\n' | sed 's/^ //')"
            bash "$zone" --status ;;
        plan)     bash "$zone" --plan ;;
        selftest) require_root; bash "$zone" --selftest ;;
        open)
            # процесс открытия: вход — ТОЛЬКО stdin (доли, код или K — секреты
            # не в argv); зовёт бот резерва временным юнитом с песочницей
            require_root
            ( cd "$INSTALL_DIR" \
                && export AWG_BOT_ENV="$ENV_FILE" AWG_BOT_CONF_DIR="$CONF_DIR" AWG_BOT_DATA_DIR="$DATA_DIR" \
                && exec ./venv/bin/python -m tools.rsvopen "$@" ) ;;
        zone)     require_root; bash "$zone" "$@" ;;
        teardown)
            # аварийно: зона и хранилище гостя прочь (пользователи основы у меня обрываются);
            # бот резерва увидит пустую зону по состоянию
            require_root; bash "$zone" --teardown || { echo "зона снята не полностью — хранилище всё равно стираю" >&2; true; }; bash "$zone" --wipe ;;
        tgpool)
            # пул адресов Telegram для порта вебхука: show — файл пула (ядро — в коде),
            # refresh — отметка «обновить анонсы сейчас», её снимет такт бота
            case "${1:-show}" in
                show)    require_root; [[ -f "$DATA_DIR/reserve/tg_pool.json" ]] && cat "$DATA_DIR/reserve/tg_pool.json" || echo '{"pool": []}' ;;
                refresh) require_root; mkdir -p "$DATA_DIR/reserve"; : > "$DATA_DIR/reserve/tg_pool.refresh"; echo "refresh: отметка поставлена" ;;
                *) die "awg-bot reserve tgpool: show | refresh" ;;
            esac ;;
        *) die "awg-bot reserve: status | plan | selftest | open | zone --<режим> | teardown | tgpool show|refresh" ;;
    esac
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

ensure_awg_server() {  # роль основного бота: сервер с нуля или топология уже живого
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
    yaml_set "$app" conf_dir "\"/etc/amnezia/amneziawg\""
    [[ -n "$iface" ]] && yaml_set "$app" interface "\"$iface\""
    # Первый адрес пользователей — следующий за адресом сервера: у нашего сервера
    # это .1 → пользователи с .2, у сервера прежней раскладки на .0 → с .1.
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
        # только частный и вне подсети пользователей — иначе умолчание, как у бота
        if { [[ "$o1" == 10 ]] || { [[ "$o1" == 172 ]] && (( o2 >= 16 && o2 <= 31 )); } \
                || { [[ "$o1" == 192 ]] && [[ "$o2" == 168 ]]; }; } \
                && { [[ -z "$prefix" ]] || [[ "${a%.*}" != "$prefix" ]]; }; then
            printf '%s' "$a"; return
        fi
    fi
    printf '10.255.53.53'
}

ensure_resolver() {  # первая установка основного бота: dnsmasq на <подсеть>.1
    # Публичный адрес в поле DNS — это Chrome/Edge, молча уходящие на DoH мимо
    # любого перехвата: условная маршрутизация у такого человека работает
    # «через раз». Приватный адрес браузеру не за что подхватить. Ставим с
    # первого дня, чтобы первое же устройство получило свой резолвер.
    local app="$CONF_DIR/app.yaml" addr
    addr="$(resolver_addr)" || { warn "подсеть не разобрана — DNS пользователей остаётся публичным"; return 0; }
    echo; log "─── Резолвер пользователей: dnsmasq на $addr ───"
    if bash "$INSTALL_DIR/install/awg-resolver-setup.sh" install "$addr"; then
        yaml_set "$app" dns1 "\"$addr\""
        yaml_set "$app" dns2 "\"$addr\""
        ok "DNS пользователей — свой резолвер $addr (оба поля)."
        # резолвер админа (имена SMB локальных сетей шлюзов для устройств
        # админа в туннеле); не поднялся — поставит бот на старте
        ADMIN_ADDR="$(admin_dns_addr)" bash "$INSTALL_DIR/install/awg-admin-dns-setup.sh" install "$addr" \
            || warn "резолвер админа не поднят — бот попробует на старте; awg-bot resolver admin status"
    else
        warn "резолвер не поднят — DNS пользователей остаётся публичным; позже: awg-bot resolver install"
    fi
}

adopt_resolver() {  # обновление: dns1 уже <подсеть>.1 — резолвер ведёт бот, не обвязка
    # Хост, где приватный адрес прописали руками и слушала его обвязка
    # маршрутизации. Файл резолвера появляется у бота, обвязка при следующем
    # реассерте ужимает свой до адреса перехвата. Публичный dns1 не трогаем:
    # переход на свой резолвер — решение админа в боте (переезд профилей).
    local app="$CONF_DIR/app.yaml" addr dns1
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

cmd_resolver() {  # awg-bot resolver status|install|add|remove [ADDR] | admin status|plan|install|remove
    require_installed
    local mode="${1:-status}" addr="${2:-}"
    if [[ "$mode" == "admin" ]]; then
        # резолвер админа: отдаёт остальное резолверу пользователей на <подсеть>.1
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
  awg-bot reserve <cmd>      взаимный резерв серверов: status — линк пары и гостевая зона;
                             plan — что умеет хост (хранилище в памяти, порт вебхука, очередь);
                             selftest — проверка механики зоны на модуле ядра;
                             open — процесс открытия пакета (вход — JSON в stdin, зовёт бот);
                             zone --<режим> — скрипт зоны напрямую;
                             teardown — аварийно снять зону (пользователи второго сервера обрываются);
                             tgpool show | refresh — пул адресов Telegram: показать / обновить анонсы
  awg-bot resolver <cmd>     свой DNS-резолвер пользователей (dnsmasq на <подсеть>.1):
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
    reserve)     cmd_reserve "$@" ;;
    first-device) cmd_first_device "$@" ;;
    resolver)    cmd_resolver "$@" ;;
    -h|--help|help|"") usage ;;
    *) usage; die "неизвестная команда: $VERB" ;;
esac
