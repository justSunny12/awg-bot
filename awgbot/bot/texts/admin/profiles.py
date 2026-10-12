"""profiles.py — часть пакета admin (см. __init__): карточка профиля, правка, продление, лимиты, блокировка, удаление."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import ActivationStatus, SubStatus
from ..fmt import (
    _e, human_bytes, gb, gb_str, _limit_devices_str, rf_line, updown_brief, profile_link,
    access_status_line, holder_link, _n_devices, plural_ru)


# ─────────────────────────────────────────────────────────────────────────────
# Профили: карточка, «✏️ Изменить», продление, лимиты, блокировка, удаление
# ─────────────────────────────────────────────────────────────────────────────

def profiles_header(n: int, online: int) -> str:
    return ui.head("👥 Профили", meta=[str(n), f"онлайн {online}"])


def _pause_of(client) -> str:
    """«⏸️ Пауза: 4 из 24 дн.» — счёт паузы против максимума типа; у дня и
    недели дни не копятся — строки нет."""
    kind = str(client.period_kind or "")
    if kind == "year":
        cap = 2 * settings.get_int("pause.pause_max_total_days", 28)
    elif kind == "month":
        cap = 12 * settings.get_int("pause.monthly_pause_days", 2)
    else:
        return ""
    bal = int(client.pause_balance_days)
    if bal > cap:                      # счёт достался от прежнего вида подписки — потолок не его
        return f"⏸️ Пауза: {bal} дн."
    return f"⏸️ Пауза: {bal} из {cap} дн."


_KIND_SHORT = {"day": "день", "week": "неделя", "month": "месяц", "year": "год"}


def _sub_line(client) -> str:
    """«💳 🟢 до 12.10 18:00 · 18 дн. · месяц, с 12.09» и варианты: бессрочная,
    истекла, истекает, своя пауза, пауза администратора."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    if paused and mode == "user":
        until = ""
        if client.pause_active_since:
            import datetime
            dt = (timeutil.parse_iso(client.pause_active_since)
                  + datetime.timedelta(days=int(client.pause_reserved_days)))
            until = f" до {timeutil.fmt_date_ui(dt)}"
        return f"💳 ⏸️ на паузе{until}"
    if paused:
        return "💳 ⏸️ приостановлена администратором"
    if not client.period_end:
        return "💳 бессрочная"
    end = timeutil.parse_iso(client.period_end)
    kind = _KIND_SHORT.get(str(client.period_kind or ""), "")
    since = f", с {timeutil.fmt_date_ui(timeutil.parse_iso(client.period_start))}" if client.period_start else ""
    tail = f" · {kind}{since}" if kind else since.lstrip(",").strip() and f" · {since[2:]}"
    if client.status != SubStatus.ACTIVE or timeutil.remaining_seconds(end) <= 0:
        # срок вышел, а статус ещё не переключён (профиль ждёт активации,
        # такт истечения не дошёл) — «🟢 до … · истекло» сбивало с толку
        return f"💳 🔴 истекла {timeutil.fmt_date_ui(end)}{tail}"
    dot = "🟡 истекает" if client.notified_thresholds else "🟢 до"
    return f"💳 {dot} {timeutil.fmt_end_ui(end)} · {timeutil.remaining_brief(end)}{tail}"


def _rf_short(rt_visible: bool, enabled: int, total: int) -> str:
    if not rt_visible:
        return ""
    if not total or not enabled:
        return " · 🇷🇺 выкл"
    return " · 🇷🇺 вкл на всех" if enabled >= total else f" · 🇷🇺 вкл на {enabled} из {total}"


def admin_client_card(d: dict, bot_username: str = "") -> str:
    """Карточка профиля у админа (данные — services.client_card_data)."""
    from awgbot.core import blocks
    client, devices = d["client"], d["devices"]
    from ..fmt import tg_link
    name = tg_link(client.name, client.tg_id, getattr(client, "tg_username", ""))
    lines = [f"👤 {name} " + ("🟢 онлайн" if d["online"] else "⚪ офлайн"), ""]
    if client.activation_status == ActivationStatus.PENDING:
        lines.append("⏳ ждёт активации")
    # ручная блокировка — «⛔»; истечение, пауза и исчерпанный трафик — «🟡»
    # строкой состояния доступа (та же у клиента)
    manual = int(client.block_reason) & int(blocks.CLIENT_MANUAL)
    reasons = blocks.client_reasons_ru(manual, for_admin=True)
    if reasons:
        lines.append("⛔ Заблокирован: " + ", ".join(reasons))
    access = access_status_line(client)
    if access:
        lines.append(access)
    lines.append(_sub_line(client))
    pause = _pause_of(client)
    if pause and client.period_end:
        lines.append(pause)
    lim = client.device_limit
    devs = f"📱 {len(devices)}" + (f" из {lim}" if lim else "")
    enabled, total = d.get("rt_counts") or (0, 0)
    lines.append(devs + _rf_short(bool(d.get("rt_visible")), enabled, total))
    t = d["traffic"]
    rx, tx = int(t["rx_month"]), int(t["tx_month"])
    arrows = f" {updown_brief(rx, tx)}" if rx + tx else ""
    if client.traffic_limit:
        bonus = f"(+{gb(client.bonus_bytes)})" if client.bonus_bytes else ""
        lines.append(f"📊 {gb(rx + tx)} из {gb(client.traffic_limit)}{bonus} ГБ{arrows}")
    elif rx + tx:
        lines.append(f"📊 {human_bytes(rx + tx)}{arrows}")
    else:
        lines.append("📊 0 ГБ (безлимит)")
    rf = d.get("rf")                              # None — строка не положена; ноль при
    if rf is not None:                            # включённой функции показываем: так
        lines.append(rf_line(*rf, arrows=False))  # видно, что маркировка не работает
    if d.get("progress") is not None:
        done, live, total_d = d["progress"]
        if live:                                  # временное — отдельным блоком
            lines.append("")
            lines.append(f"🚚 Переезд: {done} из {live} живых устройств")
    return "\n".join(lines)


def client_edit_text(client) -> str:
    """«✏️ Ксюша — изменить» и строка текущих значений."""
    if client.period_end:
        start = timeutil.parse_iso(client.period_start) if client.period_start else None
        end = timeutil.parse_iso(client.period_end)
        period = f"Период {timeutil.fmt_period_ui(start, end) if start else 'до ' + timeutil.fmt_date_ui(end)}"
    else:
        period = "Бессрочная"
    devs = _n_devices(client.device_limit) if client.device_limit else "∞ устройств"
    traf = f"{gb_str(client.traffic_limit)} в месяц" if client.traffic_limit else "∞ ГБ в месяц"
    return f"{ui.head(f'✏️ {_e(client.name)}')} — изменить\n{period} · {devs} · {traf}"


def client_name_prompt(client) -> str:
    return ui.head(f"✏️ Новое имя для профиля «{_e(client.name)}»")


def client_name_note(old: str, new: str) -> str:
    return f"✅ Имя профиля: {_e(old)} → {_e(new)}"


def devs_limit_prompt(client, used: int) -> str:
    cur = _limit_devices_str(client.device_limit)
    return ui.head(f"🔢 Лимит устройств профиля {_e(client.name)}", meta=[f"сейчас {cur}, занято {used}"])


def devs_limit_note(old: int, new: int, used: int) -> str:
    note = f"✅ Устройств: {_limit_devices_str(old)} → {_limit_devices_str(new)}"
    if new and used > new:
        note += f" · ⚠️ сейчас {used} из {new} — новые не добавить, пока не станет меньше"
    return note


def traffic_limit_prompt(client) -> str:
    cur = gb_str(client.traffic_limit) if client.traffic_limit else "∞"
    return ui.head(f"📊 Трафик профиля {_e(client.name)} в месяц", meta=[f"сейчас {cur}"])


def traffic_limit_note(old_b: int, new_b: int) -> str:
    def _f(b):
        return gb_str(b) if b else "∞"
    return f"✅ Трафик: {_f(old_b)} → {_f(new_b)}"


OTHER_NUMBER_PROMPT = "✏️ Число, 0 — без лимита"
NUMBER_BAD_LIMIT = "⚠️ Нужно целое число, 0 — без лимита"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def extend_text(client, cut_days: int = 0, bot_username: str = "") -> str:
    """«⏱ Продление: [Ксюша]» и текущий срок; отсрочка — предупреждением."""
    head = ui.head("⏱ Продление", value=profile_link(client, bot_username))
    if not client.period_end:
        now = "Сейчас: бессрочная"
    else:
        end = timeutil.parse_iso(client.period_end)
        if client.status != SubStatus.ACTIVE or timeutil.remaining_seconds(end) <= 0:
            now = f"Сейчас: истекла {timeutil.fmt_date_ui(end)}"      # и по сроку: статус переключает такт
        else:
            now = f"Сейчас до {timeutil.fmt_end_ui(end)} · осталось {timeutil.remaining_brief(end)}"
    lines = [head, now]
    if cut_days > 0:
        lines.append(f"⚠️ Брал отсрочку на {cut_days} дн. — вычтется")
    return "\n".join(lines)


_PERIOD_ACC = {"day": "день", "week": "неделю", "month": "месяц", "year": "год"}


def extended_note(client, kind: str, new_end, pause, bot_username: str = "") -> str:
    """След продления двумя строками: «✅ [Ксюша]: подписка продлена на месяц,»
    ⏎ «→ 12.11 18:00 · дней паузы +2 → 6»."""
    who = profile_link(client, bot_username)
    if new_end is None:
        return f"✅ {who}: подписка теперь бессрочная"
    tail = f"→ {timeutil.fmt_end_ui(new_end)}"
    if pause is not None and getattr(pause, "kind", None) in ("year", "month"):
        if getattr(pause, "reason", None) in ("expired", "grace", "cap"):
            tail += f" · дни паузы не начислены, доступно {pause.after}"
        else:
            tail += f" · дней паузы +{pause.added} → {pause.after}"
    return f"✅ {who}: подписка продлена на {_PERIOD_ACC.get(kind, kind)},\n{tail}"


def period_start_prompt(client) -> str:
    cur = timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_start), seconds=True) if client.period_start else "—"
    return (ui.head(f"📅 Начало периода профиля {_e(client.name)}", meta=[f"сейчас {cur}"]) + "\n"
            "Введи дату в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (без времени — 00:00), «-» — не менять")


def period_end_prompt(client) -> str:
    cur = (timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_end), seconds=True)
           if client.period_end else "бессрочно")
    return (ui.head(f"📅 Окончание периода профиля {_e(client.name)}", meta=[f"сейчас {cur}"]) + "\n"
            "Дата в том же формате, «-» — не менять, «0» — бессрочно")


PERIOD_BAD = "⚠️ Не разобрал дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ, время можно опустить"
PERIOD_NO_START = "⚠️ У профиля нет даты начала — введи её"


def period_changed_note(start, end) -> str:
    e = timeutil.fmt_dt_ui(end, seconds=True) if end else "бессрочно"
    return f"✅ Период: {timeutil.fmt_dt_ui(start, seconds=True)} → {e}"


def block_client_ask(client, bot_username: str = "") -> str:
    return (ui.head(f"🛑 Блокировка профиля {_e(client.name)}") + "\n"
            "Поставить подписку на паузу на время блокировки?")


BLOCK_NOTIFY_ASK = "Уведомить владельца профиля?"


def blocked_toast(name: str, *, silent: bool, profile: bool, owner: str = "") -> str:
    """Всплывашка — простой текст: «🛑 Профиль Ксюша заблокирован» /
    «🛑 Устройство «iPhone» (Петя) заблокировано»."""
    if profile:
        head = f"🛑 Профиль {name} заблокирован"
    else:
        head = f"🛑 Устройство «{name}»" + (f" ({owner})" if owner else "") + " заблокировано"
    return head + (" (тихо)" if silent else "")


def client_delete_ask(client, devices, lent: list) -> str:
    """«🗑 Удалить профиль Ксюша?» + последствия: устройства и держатели."""
    lines = [f"🗑 Удалить профиль {_e(client.name)}?"]
    n = len(devices)
    if n:
        word = plural_ru(n, "устройством", "устройствами", "устройствами")
        lines.append(f"Вместе с {n} {word} — {'его ссылка перестанет' if n == 1 else 'их ссылки перестанут'} работать" + (";" if lent else ""))
    for dev in lent:
        lines.append(f"профиль {holder_link(dev)} потеряет доступ к переданному устройству «{_e(dev.name)}»")
    return "\n".join(lines)


def client_deleted_note(name: str, n_devices: int) -> str:
    return f"🗑 Профиль {_e(name)} удалён" + (f" · устройств удалено: {n_devices}" if n_devices else "")


CLIENT_DELETE_PARTIAL = (
    "⚠️ Профиль «{name}» НЕ удалён: сервер не снял пиры {devices}. "
    "Удалить запись, оставив пир живым, нельзя — доступ по нему продолжал бы работать. "
    "Проверь, отвечает ли AWG, и повтори"
)


def resumed_note(client, actual: int, new_end, bot_username: str = "") -> str:
    sub = f"подписка до {timeutil.fmt_end_ui(new_end)}" if new_end else "подписка бессрочная"
    return f"▶️ {profile_link(client, bot_username)}: пауза снята · {actual} дн. списано · {sub}"
