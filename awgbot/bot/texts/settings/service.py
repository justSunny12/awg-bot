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


def settings_svc_text(br, state: str = "", progress=None, available: bool = False) -> str:
    """«🔧 Сервис»: цена перезапусков одной строкой роли; переезд — только у
    роли с has.migration и только когда идёт."""
    lines = [ui.head("🔧 Сервис"), br.svc_about]
    if br.has.migration and state:
        p = progress
        nums = (f" {p.clients_done}/{p.clients_total} профилей, {p.devices_done}/{p.devices_total} устройств"
                if p is not None and getattr(p, "clients_total", 0) else "")
        lines.append(f"🚚 Переезд идёт:{nums} · выдаются только новые конфиги, отмена безопасна")
    return "\n".join(lines)


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

def settings_upd_text(installed: str | None = None, target=None, blocked: str = "",
                      scan_failed: bool = False) -> str:
    """«⬆️ Обновления · v3.1.0 🟢 актуальна» или цель обновления со
    списком изменений под «подробнее»; блок — строкой «⛔ … недоступно: …»."""
    from awgbot.core import config
    from ..updates import changelog_details
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    if target is None:
        return ui.head("⬆️ Обновления", meta=[f"{_e(cur)} " + (ui.st("off", "проверка не удалась") if scan_failed else ui.st("ok", "актуальна"))])
    lines = [ui.head("⬆️ Обновления", meta=[f"{_e(cur)} → {_e(_ver(target.tag))}"])]
    if blocked:
        lines.append(f"⛔ Обновление до {_e(_ver(target.tag))} сейчас недоступно: {_e(blocked)}")
    header = "\n".join(lines) + "\n"
    body = changelog_details(getattr(target, "body", "") or "", header=header,   # бюджет — от настоящей шапки
                             tag=str(target.tag))
    if body:
        lines.append(body)
    return "\n".join(lines)


UPDATE_SCHEDULE_CYCLE = ("day", "week", "month")
UPDATE_SCHEDULE_LABELS = {"day": "день", "week": "неделя", "month": "месяц"}
