"""marks.py — часть пакета selfupdate (см. __init__): метки установки в каталоге
данных, которыми бот говорит с `awg-bot.sh` (обе роли).

- `installed-digest` — «<версия> <sha256>» архива, совпавшего с ассетом выпуска
  той же версии на GitHub: кладут установщик одной командой и ручной `awg-bot
  update`. Бот на старте переносит его в `update_installed_digest` (метка
  бэкапа берёт оттуда digest выпуска) и удаляет. Файл другой версии — не наш
  (обновление прервалось до старта, откат): удаляется, база не трогается.
- `update-guard` — сторож обновления взведён (пишет `awg-bot.sh` до старта
  нового сервиса); `update-ok` — отметка жизни «<версия> <время>», её пишет
  новый процесс, когда вышел на связь: основной бот — после первого ответа
  Telegram на getUpdates, агент — после него же или первого подписанного
  слова сервера AWG в сессии канала. Раньше — нельзя: старт процесса ещё не
  значит, что он работает. Сторожа нет — отметка не нужна и не пишется.
  Поставив отметку, бот сразу зовёт фиксацию (`awg-bot __update_guard
  --alive`, вне своего cgroup): живой код не ждёт окна сторожа.
- `update_installed_digest_prev` — digest прежних версий, до трёх строк
  «<версия> <sha256>», свежая первой: новая версия при первом старте
  переписывает `update_installed_digest`, и без копии прежняя после отката
  (сторожем или откатом набора через цепочку ступеней) осталась бы без
  digest (бэкапы без ссылки на выпуск). Старт с другой версией, чем в базе,
  откладывает прежнее значение; есть отложенное этой версии — откат: оно
  возвращается.

Каталог — каталог базы (как у прочих меток старта, runtime/main/common.py).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

log = logging.getLogger("awgbot.selfupdate")

INSTALLED_DIGEST_FILE = "installed-digest"
UPDATE_GUARD_FILE = "update-guard"
UPDATE_OK_FILE = "update-ok"
RUNNING_FILE = "update-running"      # pid awg-bot.sh под замком установки (update, откат, сторож)

DIGEST_KEY = "update_installed_digest"
DIGEST_PREV_KEY = "update_installed_digest_prev"

_HEX = re.compile(r"[0-9a-f]{64}")
DIGEST_PREV_MAX = 3                  # цепочка ступеней одного хода — не длиннее

# отметка жизни — одна на процесс: решается на первом признаке
_alive_lock = threading.Lock()
_alive_done = False


def _data_dir(data_dir: Optional[str]) -> str:
    from awgbot.core import config
    return data_dir or os.path.dirname(str(config.DB_PATH))


def _shift_digest(db) -> None:
    """Старт другой версией, чем у digest в базе: прежний — в отложенный, а
    если отложенный как раз этой версии (откат) — он возвращается."""
    from awgbot.core import config
    cur = db.get_state(DIGEST_KEY) or ""
    if cur.partition(" ")[0] == config.INSTALLED_VERSION:
        return
    kept = [x.strip() for x in (db.get_state(DIGEST_PREV_KEY) or "").splitlines() if x.strip()]
    back = next((x for x in kept if x.partition(" ")[0] == config.INSTALLED_VERSION), "")
    if not cur and not back:
        return
    kept = [x for x in kept if x != back]
    if cur:
        kept = [cur] + [x for x in kept if x.partition(" ")[0] != cur.partition(" ")[0]]
    if back:
        db.set_state(DIGEST_KEY, back)
        log.info("digest выпуска v%s возвращён после отката", config.INSTALLED_VERSION)
    db.set_state(DIGEST_PREV_KEY, "\n".join(kept[:DIGEST_PREV_MAX]))


def adopt_release_digest(db, version: str, sha256: str) -> bool:
    """Digest установки пуст, а установленная версия — опубликованный выпуск с
    таким ассетом: записать его. Непустой digest не трогается (его дал архив
    при установке). True — записан."""
    sha = str(sha256 or "").lower()
    if not _HEX.fullmatch(sha):
        return False
    cur = db.get_state(DIGEST_KEY) or ""
    if cur.partition(" ")[0] == version and _HEX.search(cur):
        return False
    db.set_state(DIGEST_KEY, f"{version} {sha}")
    log.info("установка — выпуск v%s: digest ассета взят из списка выпусков", version)
    return True


def adopt_installed_digest(db, data_dir: Optional[str] = None) -> bool:
    """Перенести `installed-digest` в `update_installed_digest` и удалить файл;
    перед этим — отложить или вернуть digest прежней версии (_shift_digest).
    True — перенесён (версия файла — установленная, sha256 читается)."""
    from awgbot.core import config
    _shift_digest(db)
    path = os.path.join(_data_dir(data_dir), INSTALLED_DIGEST_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return False
    except OSError as e:
        log.warning("installed-digest не прочитан: %s", e)
        return False
    ver, _, hexd = raw.partition(" ")
    hexd = hexd.strip().lower()
    ours = ver == config.INSTALLED_VERSION and bool(_HEX.fullmatch(hexd))
    if ours:
        db.set_state(DIGEST_KEY, f"{ver} {hexd}")
        log.info("установка из выпуска v%s: digest известен", ver)
    else:
        log.info("installed-digest не этой установки (%s) — снят", ver or "пусто")
    try:
        os.remove(path)
    except OSError as e:
        log.warning("installed-digest не удалён: %s", e)
    return ours


def mark_alive(reason: str, data_dir: Optional[str] = None) -> bool:
    """Отметка жизни для сторожа обновления — один раз на процесс, на первом
    признаке. Сторож не взведён — отметки нет (и решать больше нечего: новый
    сторож бывает только с новым процессом). True — отметка записана."""
    global _alive_done
    with _alive_lock:
        if _alive_done:
            return False
        _alive_done = True
    from awgbot.core import config
    from awgbot.util import timeutil
    base = _data_dir(data_dir)
    if not os.path.exists(os.path.join(base, UPDATE_GUARD_FILE)):
        return False
    path = os.path.join(base, UPDATE_OK_FILE)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{config.INSTALLED_VERSION} {timeutil.now_iso()}\n")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("отметка жизни для сторожа обновления не записана: %s", e)
        with _alive_lock:
            _alive_done = False            # следующий признак попробует снова
        return False
    log.info("обновление: v%s на связи (%s) — отметка для сторожа", config.INSTALLED_VERSION, reason)
    try:
        _commit_now()
    except Exception as e:                               # noqa: BLE001
        log.warning("фиксация обновления не запущена — решит сторож по таймеру: %s", e)
    return True


def _commit_now() -> None:
    """Фиксация сразу по отметке — вне cgroup бота (как апдейтер): прежние код
    и окружение снимает `awg-bot.sh`, без отметки он ничего не делает."""
    from awgbot.infra.detach import spawn_detached
    from awgbot.infra.updates import _AWG_BOT_BIN
    spawn_detached([_AWG_BOT_BIN, "__update_guard", "--alive"], unit_prefix="awg-bot-update-commit")


def install_busy() -> bool:
    """Идёт ли сейчас `awg-bot update` / откат / решение сторожа (pid под
    замком установки жив)."""
    try:
        with open(os.path.join(_data_dir(None), RUNNING_FILE), encoding="utf-8") as f:
            pid = int((f.read().split() or ["0"])[0])
    except (OSError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                   # процесс есть, просто не наш
    except OSError:
        return False
    return True
