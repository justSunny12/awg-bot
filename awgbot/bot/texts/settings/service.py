"""service.py — часть пакета settings (см. __init__): «🔧 Сервис» и «⬆️ Обновления»."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e
from ..updates import _ver


# ── 🔧 Сервис ────────────────────────────────────────────────────────────────

def svc_confirm_awg(br, carries: bool = True) -> str:
    """«🔁 Перезапустить AWG?» и цена словами роли; carries — роль сейчас
    несёт трафик (у агента предупреждение про РФ-доступ только тогда)."""
    return ui.confirm("🔁 Перезапустить AWG?", br.awg_restart_cost_carrying if carries else br.awg_restart_cost)


def svc_confirm_bot(br) -> str:
    return ui.confirm("🔁 Перезапустить бота?", br.bot_restart_cost)


def settings_svc_text(br, state: str = "", progress=None, available: bool = False,
                      carries: bool = True) -> str:
    """«🔧 Сервис»: цена перезапусков одной строкой роли — та же развилка, что
    в подтверждении (carries — роль несёт трафик); переезд — только у роли с
    has.migration и только когда идёт."""
    from ..admin import migration_counts
    lines = [ui.head("🔧 Сервис"), br.svc_about_carrying if carries else br.svc_about]
    if br.has.migration and state:
        p = progress
        nums = (f" переехали {migration_counts(p)}"
                if p is not None and getattr(p, "clients_total", 0) else "")
        lines.append(f"🚚 Переезд идёт:{nums} · выдаются только новые конфиги")
    return "\n".join(lines)


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

def settings_upd_text(installed: str | None = None, target=None, blocked: str = "",
                      scan_failed: bool = False, waiting: dict | None = None,
                      silent: dict | None = None, skipped=(), hold: dict | None = None,
                      bot_username: str = "") -> str:
    """«⬆️ Обновления · v3.1.0 🟢 актуальна» или цель обновления со
    списком изменений под «подробнее»; блок — строкой «⛔ … недоступно: …».
    waiting — новейший релиз ждёт другую сторону (строка «⏳ … ждёт …»);
    silent — молчащие шлюзы, чью версию цель не обслуживает (основной бот);
    skipped — релизы с непрочитанными метаданными; hold — окно ожидания
    после старта: цель ещё не считалась."""
    from awgbot.core import config
    from ..updates import (changelog_details, update_wait_line, update_silent_lines,
                           update_hold_line, update_skipped_lines)
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    tail = update_skipped_lines(skipped)
    if hold:
        return "\n".join([ui.head("⬆️ Обновления", meta=[_e(cur)]), update_hold_line(hold)] + tail)
    if target is None:
        head = ui.head("⬆️ Обновления", meta=[f"{_e(cur)} " + (ui.st("off", "проверка не удалась") if scan_failed else ui.st("ok", "актуальна"))])
        return "\n".join([head] + ([update_wait_line(waiting, bot_username)] if waiting else []) + tail)
    lines = [ui.head("⬆️ Обновления", meta=[f"{_e(cur)} → {_e(_ver(target.tag))}"])]
    if blocked:
        lines.append(f"⛔ Обновление до {_e(_ver(target.tag))} сейчас недоступно: {_e(blocked)}")
    if waiting:
        lines.append(update_wait_line(waiting, bot_username))
    lines.extend(update_silent_lines(silent, bot_username))
    lines.extend(tail)
    header = "\n".join(lines) + "\n"
    body = changelog_details(getattr(target, "body", "") or "", header=header,   # бюджет — от настоящей шапки
                             tag=str(target.tag))
    if body:
        lines.append(body)
    return "\n".join(lines)


UPDATE_SCHEDULE_CYCLE = ("day", "week", "month")
UPDATE_SCHEDULE_LABELS = {"day": "день", "week": "неделя", "month": "месяц"}
