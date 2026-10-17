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
    """«👥 Профили · 4 · онлайн 2»; пустой список — словом, а не двумя нулями."""
    if not n:
        return ui.head("👥 Профили") + "\n\nПрофилей пока нет"
    return ui.head("👥 Профили", meta=[str(n), f"онлайн {online}"])


def _pause_of(client) -> str:
    """«⏸️ Дней паузы: 4 из 24» — счёт паузы против максимума типа; у дня и
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
        kind_ru = "годовой" if kind == "year" else "месячной"
        return f"⏸️ Дней паузы: {bal} (максимум {kind_ru} подписки — {cap})"
    return f"⏸️ Дней паузы: {bal} из {cap}"


_KIND_SHORT = {"day": "день", "week": "неделя", "month": "месяц", "year": "год"}


def _sub_line(client) -> str:
    """«💳 🟢 до 12.10 18:00 · 18 дн. · месяц, с 12.09» и варианты: бессрочная,
    истекла, истекает, своя пауза, пауза на время блокировки."""
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
        return "💳 ⏸️ на паузе на время блокировки"
    if not client.period_end:
        return "💳 🟢 бессрочная"
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
    # ни разу не входил — «офлайн» вводил бы в заблуждение: ждёт активации
    pending = client.activation_status == ActivationStatus.PENDING
    state = "⏳ ждёт активации" if pending else "🟢 онлайн" if d["online"] else "🔴 офлайн"
    lines = [f"👤 {name} {state}", ""]
    # ручная блокировка — «⛔»; истечение, пауза и исчерпанный трафик —
    # строкой состояния доступа (та же у клиента)
    manual = int(client.block_reason) & int(blocks.CLIENT_MANUAL)
    reasons = blocks.client_reasons_ru(manual, for_admin=True)
    if reasons:
        lines.append("⛔ Заблокирован " + ", ".join(reasons))
    access = access_status_line(client)
    # пауза на время блокировки — часть блокировки: о ней говорит строка 💳,
    # третья строка про тот же факт была бы лишней
    admin_pause = str(client.pause_mode or "").startswith("admin")
    if access and not (reasons and admin_pause and access.startswith("⏸️")):
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
            lines.append(f"🚚 Переезд: {done} из {live} недавно активных устройств")
    return "\n".join(lines)


def client_edit_text(client) -> str:
    """«✏️ Ксюша — изменить» и строка текущих значений."""
    if client.period_end:
        start = timeutil.parse_iso(client.period_start) if client.period_start else None
        end = timeutil.parse_iso(client.period_end)
        # год — только не текущий: «15.09 → 15.10.27» (как в карточке «с 15.09»)
        period = (f"Период: {timeutil.fmt_date_ui(start)} → {timeutil.fmt_date_ui(end)}" if start
                  else f"Период: до {timeutil.fmt_date_ui(end)}")
    else:
        period = "Период: бессрочно"
    devs = _n_devices(client.device_limit) if client.device_limit else "∞ устройств"
    traf = f"{gb_str(client.traffic_limit)} в месяц" if client.traffic_limit else "∞ ГБ в месяц"
    return f"{ui.head(f'✏️ {_e(client.name)}')} — изменить\n{period} · {devs} · {traf}"


def client_name_prompt(client) -> str:
    return ui.head("✏️ Имя профиля", meta=[f"сейчас {_e(client.name)}"]) + "\n\nПришли новое имя"


def client_name_note(old: str, new: str) -> str:
    return f"✅ Имя профиля: {_e(old)} → {_e(new)}"


def devs_limit_prompt(client, used: int) -> str:
    cur = _limit_devices_str(client.device_limit)
    return ui.head(f"📱 Лимит устройств профиля {_e(client.name)}", meta=[f"сейчас {cur}, занято {used}"])


def devs_limit_other_prompt(client) -> str:
    """«✏️ Другое» у лимита устройств: тот же заголовок, пустая строка, что прислать."""
    cur = _limit_devices_str(client.device_limit)
    return (ui.head(f"✏️ Лимит устройств профиля {_e(client.name)}", meta=[f"сейчас {cur}"])
            + "\n\nПришли число; 0 — без лимита")


def devs_limit_note(old: int, new: int, used: int) -> str:
    note = f"✅ Лимит устройств: {_limit_devices_str(old)} → {_limit_devices_str(new)}"
    if new and used > new:
        over = used - new
        note += f" · ⚠️ занято {used} — на {over} больше лимита, новое не добавить"
    return note


def _traffic_cur(client) -> str:
    return gb_str(client.traffic_limit) if client.traffic_limit else "∞"


def traffic_limit_prompt(client) -> str:
    return ui.head(f"📊 Лимит трафика профиля {_e(client.name)}", meta=[f"сейчас {_traffic_cur(client)} в месяц"])


def traffic_limit_other_prompt(client) -> str:
    """«✏️ Другое» у лимита трафика профиля."""
    return (ui.head(f"✏️ Лимит трафика профиля {_e(client.name)}", meta=[f"сейчас {_traffic_cur(client)} в месяц"])
            + "\n\nПришли число ГБ; 0 — без лимита")


def traffic_limit_note(old_b: int, new_b: int) -> str:
    """«✅ Лимит трафика: 50 → 70 ГБ в месяц» — единица одна, у «∞» её нет."""
    def _f(b):
        return gb(b) if b else "∞"
    tail = " ГБ в месяц" if new_b else " в месяц"
    return f"✅ Лимит трафика: {_f(old_b)} → {_f(new_b)}{tail}"


NUMBER_BAD_LIMIT = "⚠️ Лимит — целое число (0 — без лимита), попробуй ещё раз"
NUMBER_BAD_TRAFFIC = "⚠️ Лимит — целое число ГБ (0 — без лимита), попробуй ещё раз"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def extend_text(client, cut_days: int = 0, bot_username: str = "", *, keep: bool = True) -> str:
    """«⏱ Продление: [Ксюша]» и точка отсчёта — она меняется вместе с тумблером
    остатка; отсрочка — предупреждением. Бессрочную не продлевают — срок
    задаётся в «✏️ Изменить»."""
    head = ui.head("⏱ Продление", value=profile_link(client, bot_username))
    if not client.period_end:
        return head + "\nПодписка бессрочная — срок задаётся в «✏️ Изменить» → «✏️ Период»"
    end = timeutil.parse_iso(client.period_end)
    if client.status != SubStatus.ACTIVE or timeutil.remaining_seconds(end) <= 0:
        now = f"Продлю от сегодня — подписка истекла {timeutil.fmt_date_ui(end)}"      # и по сроку: статус переключает такт
    elif keep:
        now = f"Продлю от {timeutil.fmt_end_ui(end)} — остаток {timeutil.remaining_brief(end)} сохранится"
    else:
        now = f"Продлю от сегодня — остаток {timeutil.remaining_brief(end)} сгорит"
    lines = [head, now]
    if cut_days > 0:
        lines.append(f"⚠️ Брал отсрочку на {cut_days} дн. — она вычтется из продления, поэтому сроки короче отсрочки недоступны")
    return "\n".join(lines)


EXTEND_FOREVER_TOAST = "Подписка бессрочная — срок задаётся в «✏️ Изменить»"


def extended_toast(new_end) -> str:
    """Всплывашка итога продления — с датой."""
    return f"Продлено до {timeutil.fmt_end_ui(new_end)}" if new_end else "Подписка теперь бессрочная"


_PERIOD_ACC = {"day": "день", "week": "неделю", "month": "месяц", "year": "год"}


def extended_note(client, kind: str, new_end, pause, bot_username: str = "") -> str:
    """След продления одной строкой: «✅ [Ксюша]: подписка продлена на месяц,
    до 12.11 18:00 · дней паузы +2 → 6»."""
    who = profile_link(client, bot_username)
    if new_end is None:
        return f"✅ {who}: подписка теперь бессрочная"
    tail = f"до {timeutil.fmt_end_ui(new_end)}"
    if pause is not None and getattr(pause, "kind", None) in ("year", "month"):
        if getattr(pause, "reason", None) in ("expired", "grace", "cap"):
            tail += f" · дни паузы не начислены, доступно {pause.after}"
        else:
            tail += f" · дней паузы +{pause.added} → {pause.after}"
    return f"✅ {who}: подписка продлена на {_PERIOD_ACC.get(kind, kind)}, {tail}"


def _period_hint() -> str:
    """Ввод — стенные часы пояса бота, приглашение называет его смещение."""
    return (f"Пришли дату и время по {timeutil.tz_label()} в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>; "
            "без времени — начало суток")


def period_start_prompt(client) -> str:
    cur = timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_start), seconds=True) if client.period_start else "—"
    return ui.head(f"📅 Начало периода профиля {_e(client.name)}", meta=[f"сейчас {cur}"]) + f"\n\n{_period_hint()}"


def period_end_prompt(client) -> str:
    cur = (timeutil.fmt_dt_ui(timeutil.parse_iso(client.period_end), seconds=True)
           if client.period_end else "бессрочно")
    return ui.head(f"📅 Окончание периода профиля {_e(client.name)}", meta=[f"сейчас {cur}"]) + f"\n\n{_period_hint()}"


PERIOD_BAD = ("⚠️ Не разобрал дату — формат <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>, время можно опустить; "
              "попробуй ещё раз")
PERIOD_NO_START = "⚠️ У профиля нет даты начала — пришли её"


def _period_point(dt) -> str:
    """Дата периода: время с меткой — только не полночь («15.09 12:00 (UTC+5)»,
    «15.10.27»)."""
    dt = dt.astimezone(timeutil.TZ)
    return timeutil.fmt_dt_ui(dt) if (dt.hour or dt.minute) else timeutil.fmt_date_ui(dt)


def period_changed_note(start, end) -> str:
    if end is None:
        return f"✅ Период: с {_period_point(start)}, бессрочно"
    return f"✅ Период: с {_period_point(start)} до {_period_point(end)}"


# ── блокировка ───────────────────────────────────────────────────────────────

PAUSE_ON_BLOCK_LINE = "Подписка на время блокировки встанет на паузу — дни паузы профиля не расходуются"


def block_client_ask(client, bot_username: str = "", *, pause: bool = True) -> str:
    """Один экран блокировки профиля: заголовок и строка про паузу, когда
    тумблер паузы включён."""
    head = ui.head(f"🔒 Блокировка профиля {_e(client.name)}")
    return head + (f"\n{PAUSE_ON_BLOCK_LINE}" if pause and client.period_end else "")


def unblock_ask(name: str, *, profile: bool, owner: str = "") -> str:
    """«🔓 Разблокировка профиля Ксюша» / «… устройства «X» ([Ксюша])» и вопрос;
    owner — готовая разметка (ссылка на профиль владельца)."""
    if profile:
        head = ui.head(f"🔓 Разблокировка профиля {_e(name)}")
    else:
        head = ui.head(f"🔓 Разблокировка устройства «{_e(name)}»") + (f" ({owner})" if owner else "")
    return head + "\nКакую блокировку снять?"


def blocked_toast(name: str, *, silent: bool, profile: bool, owner: str = "") -> str:
    """Всплывашка — простой текст: «🔒 Профиль Ксюша заблокирован» /
    «🔒 Устройство «iPhone» (Петя) заблокировано без уведомления»."""
    if profile:
        head = f"🔒 Профиль {name} заблокирован"
    else:
        head = f"🔒 Устройство «{name}»" + (f" ({owner})" if owner else "") + " заблокировано"
    return head + (" без уведомления" if silent else "")


def unblocked_toast(name: str, *, profile: bool, owner: str = "") -> str:
    """Пара к blocked_toast: «🔓 Устройство «X» (Ксюша) разблокировано»."""
    if profile:
        return f"🔓 Профиль {name} разблокирован"
    return f"🔓 Устройство «{name}»" + (f" ({owner})" if owner else "") + " разблокировано"


# ── снятие паузы администратором ─────────────────────────────────────────────

def resume_ask(client, bot_username: str = "") -> str:
    """Подтверждение снятия паузы: подписка продолжится от сегодня."""
    return ui.confirm(f"▶️ Снять паузу профиля {profile_link(client, bot_username)}?",
                      "Подписка продолжится с этого момента; неиспользованные дни паузы вернутся на счёт")


# ── удаление ─────────────────────────────────────────────────────────────────

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
    if not n_devices:
        return f"🗑 Профиль {_e(name)} удалён"
    word = plural_ru(n_devices, "устройством", "устройствами", "устройствами")
    return f"🗑 Профиль {_e(name)} удалён вместе с {n_devices} {word}"


def client_delete_partial(name: str, failed: list) -> str:
    """Профиль не удалён: AWG не снял часть устройств — без этого доступ по ним
    остался бы."""
    names = ", ".join(f"«{_e(x)}»" for x in failed)
    word = "устройство" if len(failed) == 1 else "устройства"
    them = "нему" if len(failed) == 1 else "ним"
    return (f"⚠️ Профиль {_e(name)} не удалён: AWG не снял {word} {names} — без этого доступ "
            f"по {them} остался бы. Проверь, отвечает ли AWG («🔁 Перезапуск AWG» в «🔧 Сервис»), и повтори")


CLIENT_DELETE_PARTIAL_TOAST = "AWG не ответил — профиль не удалён"


def resumed_note(client, actual: int, new_end, bot_username: str = "") -> str:
    sub = f"подписка до {timeutil.fmt_end_ui(new_end)}" if new_end else "подписка бессрочная"
    spent = f" · списано {actual} дн." if actual else ""
    return f"▶️ {profile_link(client, bot_username)}: пауза снята{spent} · {sub}"
