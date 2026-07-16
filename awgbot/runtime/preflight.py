"""preflight.py — самопроверка окружения на старте.

Две градации:
  • FATAL   — стоп-факторы (битая БД/нет записи в data-dir; отвергнутый токен
    проверяется в main через getMe, причём fatal только на 401 — сетевые сбои
    переживаемы). Бегут ДО создания бота; при провале поднимаем PreflightError
    с человекочитаемым текстом в stderr — его видно в `journalctl -u awg-bot`,
    потому что бот ещё не готов слать в чат.
  • WARNING — не блокируют старт (мало места, контейнер молчит, конфиг с
    дефолтами). Собираются в список и уходят админу ПЕРВЫМ сообщением после
    успешного подъёма (см. main: send_startup_warnings).

Каждая проверка обёрнута в try: сам preflight не должен добавлять хрупкости —
если проверка не смогла отработать (docker временно недоступен и т.п.), это
максимум WARNING, но не падение бота на ровном месте.

config.validate() (обязательные секреты/топология) остаётся и зовётся отдельно
раньше — preflight его не дублирует, а дополняет проверками рантайма.
"""
from __future__ import annotations

import logging

import shutil
import sqlite3

from awgbot.core import config

log = logging.getLogger("awgbot.preflight")

# порог свободного места под data-dir, ниже которого — предупреждение
_DISK_WARN_MB = 200


class PreflightError(RuntimeError):
    """Fatal-провал preflight. Текст рассчитан на чтение человеком в journalctl."""


# ── FATAL ────────────────────────────────────────────────────────────────────
def check_fatal() -> None:
    """Стоп-факторы. Любой провал → PreflightError (бот не стартует).
    Токен getMe проверяется отдельно (async, в main) — здесь только локальное,
    что можно проверить синхронно и без сети."""
    problems: list[str] = []

    # data-dir существует и РЕАЛЬНО доступен на запись. os.access(W_OK) под root
    # бесполезен (root игнорит режим-биты), поэтому пробуем записать файл —
    # это ловит read-only mount / immutable / переполнение, где не пишет и root.
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = config.DATA_DIR / ".preflight_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            problems.append(f"нет записи в data-dir {config.DATA_DIR} "
                            f"(ro-mount/переполнение?): {e}")
    except OSError as e:
        problems.append(f"data-dir недоступен ({config.DATA_DIR}): {e}")

    # БД открывается, не повреждена (тяжёлый integrity_check — по согласованию
    # включён: БД маленькая, а битую базу лучше поймать на старте, чем в бою)
    if config.DB_PATH.exists():
        try:
            con = sqlite3.connect(str(config.DB_PATH))
            try:
                res = con.execute("PRAGMA integrity_check").fetchone()
                if not res or str(res[0]).lower() != "ok":
                    problems.append(f"БД повреждена (integrity_check: {res[0] if res else '?'})")
            finally:
                con.close()
        except sqlite3.Error as e:
            problems.append(f"БД не открывается ({config.DB_PATH}): {e}")
    # если файла БД нет — это первый запуск, init_schema создаст; не проблема

    if problems:
        raise PreflightError(
            "Проверка окружения не пройдена — бот не запущен:\n  • "
            + "\n  • ".join(problems)
            + "\nИсправьте и перезапустите (systemctl restart awg-bot).")


# ── WARNING ──────────────────────────────────────────────────────────────────
def collect_warnings(services) -> list[str]:
    """Не-блокирующие замечания. Возвращает список строк для отправки админу.
    Каждая проверка изолирована: её собственный сбой не роняет остальные и не
    роняет бота — в худшем случае конкретная проверка молча пропускается."""
    warns: list[str] = []

    # свободное место под data-dir
    try:
        free_mb = shutil.disk_usage(config.DATA_DIR).free // (1024 * 1024)
        if free_mb < _DISK_WARN_MB:
            warns.append(f"мало места на диске: {free_mb} МБ свободно "
                         f"(порог {_DISK_WARN_MB} МБ) — бэкапы/логи могут не поместиться")
    except OSError as e:
        log.warning("preflight: проверка диска не удалась: %s", e)

    # контейнер AmneziaWG отвечает
    try:
        if not services.server_ok():
            warns.append("контейнер AmneziaWG не отвечает на старте — "
                         "проверьте `docker ps` и журнал контейнера")
    except Exception as e:                               # noqa: BLE001
        log.warning("preflight: проверка контейнера не удалась: %s", e)

    # серверный awg0.conf читается (единственная копия вне контейнера — в бэкапе)
    try:
        from awgbot.infra import awg
        awg.read_file(config.CONF_PATH)
    except Exception as e:                               # noqa: BLE001
        warns.append(f"не читается {config.CONF_PATH} в контейнере ({e}) — "
                     "выдача конфигов/реконсиляция могут не работать")

    # email-выход из приостановки: IMAP доступен и пускает по кредам. Проверяем
    # ТОЛЬКО если фича активна (заданы креды и хосты) — иначе она спит и мешать
    # не должна. Таймаут короткий: глухой хост не должен задерживать старт.
    if config.EMAIL_RESUME_ENABLED:
        try:
            import imaplib
            import socket
            import ssl
            ctx = ssl.create_default_context()
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(10)
            try:
                conn = imaplib.IMAP4_SSL(config.EMAIL_IMAP_HOST,
                                         config.EMAIL_IMAP_PORT, ssl_context=ctx)
                try:
                    conn.login(config.EMAIL_RESUME_LOGIN, config.EMAIL_RESUME_PASSWORD)
                finally:
                    try:
                        conn.logout()
                    except Exception:                    # noqa: BLE001
                        pass
            finally:
                socket.setdefaulttimeout(old_timeout)
        except Exception as e:                           # noqa: BLE001
            warns.append(f"IMAP для email-выхода недоступен "
                         f"({config.EMAIL_IMAP_HOST}:{config.EMAIL_IMAP_PORT}): {e} — "
                         "аварийный выход из приостановки письмом не сработает")

    return warns


def format_warnings(warns: list[str]) -> str:
    """Сообщение админу с накопленными предупреждениями старта. Тексты содержат
    str(e) — экранируем: угловая скобка в тексте ошибки не должна ломать
    HTML-отправку (иначе предупреждение молча потеряется)."""
    import html
    safe = [html.escape(w) for w in warns]
    return ("⚠️ <b>Замечания при запуске</b>\n\nБот работает, но обрати внимание:\n• "
            + "\n• ".join(safe))
