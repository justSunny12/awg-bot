"""Объявления пользователям: выбор адресатов, продление, превью, отчёт."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.core import config
from awgbot.util import timeutil

from .fmt import plural_ru, _days, profile_link


# ── Броадкаст ────────────────────────────────────────────────────────────────
BROADCAST_EMPTY = "⚠️ Пустое объявление — пришли текст или картинку"


def broadcast_targets_text(selected: int, extend: bool) -> str:
    """Экран адресатов: «📢 Объявление · отмечено 2» (ноль — без хвоста); с
    продлением — кто получит и что значат ∞ и 🔴 у имён."""
    marked = [f"отмечено {selected}"] if selected else []
    if extend:
        return (ui.head("📢 Объявление с продлением", meta=marked) + "\n"
                "Получат только владельцы профилей с подпиской\n"
                "∞ — бессрочная, не продлится · 🔴 — истекла, продлится с сегодняшнего дня")
    return (ui.head("📢 Объявление", meta=marked) + "\n"
            "Получат владельцы и те, с кем они делятся устройствами")


BROADCAST_NO_PROFILES = ui.head("📢 Объявление") + "\n\nПрофилей пока нет"
BROADCAST_NO_TARGETS = "Никого не отметил — выбери хотя бы один профиль"
BROADCAST_ALL_UNLIMITED = ("Продлевать некого: у всех отмеченных подписка бессрочная. "
                           "Сними «✅ С продлением подписки» — уйдёт обычное объявление")
BROADCAST_DAYS_BAD = "⚠️ Число дней — от 1 до 365, попробуй ещё раз"
BROADCAST_DAYS_OTHER = ui.head("✏️ Продление, дней", meta=["1–365"]) + "\n\nПришли число"


def subscription_mark(c) -> str:
    """Хвост к имени профиля в выборе адресатов с продлением: « ∞» — бессрочная,
    « 🔴 DD.MM» — истекла тогда-то; активной — ничего."""
    end = c.effective_period_end
    if not end:
        return " ∞"
    dt = timeutil.parse_iso(end)
    if c.status == "expired" or timeutil.elapsed(dt) >= 0:
        return f" 🔴 {timeutil.fmt_date_ui(dt)}"
    return ""


NAMES_SHOWN = 12                       # адресатов поимённо; дальше — «… и ещё N» строкой или ссылка на снимок


def _names(clients, bot_username: str, more: str = "") -> str:
    """Имена ссылками, не больше NAMES_SHOWN; дальше — more (« и ещё <ссылка>») или « [+N]»."""
    from awgbot.util.textfmt.tail import more_count
    clients = list(clients)
    out = ", ".join(profile_link(c, bot_username) for c in clients[:NAMES_SHOWN])
    rest = len(clients) - NAMES_SHOWN
    return out + ((more or more_count(rest)) if rest > 0 else "")


def broadcast_days_prompt(plan, bot_username: str = "") -> str:
    """Шаг дней: «📢 Продление подписки: [Ксюша], [Петя]» ⏎ «Без продления (∞):
    [Вечный]» ⏎ ⏎ «На сколько дней продлеваем?»."""
    names = _names([e.client for e in plan if not e.unlimited], bot_username)
    unl = _names([e.client for e in plan if e.unlimited], bot_username)
    lines = [ui.head("📢 Продление подписки", value=names)]
    if unl:
        lines.append(f"Без продления (∞): {unl}")
    lines.append("")                                   # вопрос — отдельным абзацем (вычитка 29.10.2026)
    lines.append("На сколько дней продлеваем?")
    return "\n".join(lines)


def extension_header(days: int, ext) -> str:
    """Шапка объявления с продлением — у каждого адресата своя (даты его).
    Бессрочному — пусто. Истёкшему — отсчёт от сегодня, и так и сказано."""
    if ext is None or ext.unlimited:
        return ""
    new = timeutil.fmt_date_ui(ext.new_end)
    if ext.from_now:
        return f"<b>Подписка продлена на {_days(days)} 🙂\nс сегодняшнего дня — до {new}</b>"
    return (f"<b>Подписка продлена на {_days(days)} 🙂\n"
            f"{timeutil.fmt_date_ui(ext.old_end)} → {new}</b>")


EXTENSION_HEADER_MASK = "<b>Подписка продлена на {дней} 🙂\n{период подписки}</b>"
"""Шапка объявления в чате админа (превью и копия после рассылки): у каждого
адресата числа свои, админу — маски вместо чисел первого адресата."""


def announcement_text(header: str, text: str) -> str:
    """Шапка над текстом; без текста — одна шапка; без шапки — один текст."""
    if not header:
        return text
    return header + (f"\n\n{text}" if text else "")


def extension_reserve() -> int:
    """Сколько видимых символов шапка отнимает у лимита текста — по самому
    длинному варианту (трёхзначные дни, полные даты) плюс пустая строка. Теги
    в счёт Telegram не идут."""
    import re
    from types import SimpleNamespace
    d = timeutil.now()
    longest = max(
        len(re.sub(r"<[^>]+>", "", extension_header(
            365, SimpleNamespace(unlimited=False, from_now=fn, old_end=d, new_end=d))))
        for fn in (True, False))
    return longest + 2


def _extension_footer(days: int, plan, n: int, bot_username: str = "") -> str:
    """Подвал превью с продлением: даты каждого адресата, не больше NAMES_SHOWN
    строк — дальше «… и ещё N» своей строкой (к датам последнего не клеить:
    читалось бы как «+N дней»; превью — подтверждение, ссылок на снимок тут нет;
    список без предела пробивал 4096 уже на ~60 профилях)."""
    who = plural_ru(n, "адресат", "адресата", "адресатов")
    lines = [f"{n} {who} · продление на <b>{_days(days)}</b>:"]
    plan = list(plan)
    for e in plan[:NAMES_SHOWN]:
        if e.unlimited:
            lines.append(f"• {profile_link(e.client, bot_username)}: ∞ — без продления")
            continue
        old, new = timeutil.fmt_date_ui(e.old_end), timeutil.fmt_date_ui(e.new_end)
        lines.append(f"• {profile_link(e.client, bot_username)}: {'🔴 ' if e.from_now else ''}{old} → {new}")
    if len(plan) > NAMES_SHOWN:
        lines.append(f"… и ещё {len(plan) - NAMES_SHOWN}")   # своей строкой: к датам последнего не клеить
    return "\n".join(lines)


def _bc_audience(clients: list, with_friends: bool, bot_username: str = "", more: str = "",
                 case: str = "nom") -> str:
    """«владельцы профилей: Ксюша, Петя и те, с кем они делятся устройствами» —
    имена ссылками; про друзей — только когда они есть среди адресатов. case —
    падеж: "nom" (превью: «40 адресатов, владельцы профилей: …») или "dat"
    (отчёт: «Разослано владельцам профилей: … и тем, с кем …»)."""
    who = _names(clients, bot_username, more)
    solo = len(clients) == 1
    dat = case == "dat"
    if solo:
        head = ("владельцу профиля" if dat else "владелец профиля") + f": {who}"
        tail = " и тому, с кем он делится устройствами" if dat else " и тот, с кем он делится устройствами"
    else:
        head = ("владельцам профилей" if dat else "владельцы профилей") + f": {who}"
        tail = " и тем, с кем они делятся устройствами" if dat else " и те, с кем они делятся устройствами"
    return head + (tail if with_friends else "")


def _how_to(text_max: int, caption_max: int) -> str:
    from .fmt import details
    return (f"Пришли текст или картинки — форматирование Telegram сохранится, картинок до {config.TG_ALBUM_MAX}\n"
            + details(f"без картинок — до {text_max} символов, с картинками — до {caption_max}"))


def broadcast_prompt(clients: list, with_friends: bool = False, *,
                     extend_days: int | None = None, bot_username: str = "") -> str:
    """Приглашение ввести текст: «📢 Текст для профилей: [Ксюша], [Петя]»,
    адресаты поимённо — к подтверждению легко забыть, кого отметил."""
    r = extension_reserve() if extend_days is not None else 0
    head = ui.head(f"📢 Текст для {'профиля' if len(clients) == 1 else 'профилей'}",
                   value=_names(clients, bot_username))
    if extend_days is not None:
        head += f" · продление на {_days(extend_days)}"
    return head + "\n\n" + _how_to(config.TG_TEXT_MAX - r, config.TG_CAPTION_MAX - r)


def broadcast_preview(text: str, n: int, clients: list = (),
                      with_friends: bool = False, extension=None, bot_username: str = "") -> str:
    """Превью с текстом и подвалом «кому»; с продлением — кому и на сколько."""
    if extension is not None:
        foot = _extension_footer(extension[0], extension[1], n, bot_username)
    else:
        w = plural_ru(n, "адресат", "адресата", "адресатов")
        foot = f"{n} {w}, {_bc_audience(list(clients), with_friends, bot_username)}"
    return ui.head("👇 Так увидят получатели", meta=[foot]) + f"\n\n{text}"


def broadcast_preview_photos(n: int, clients: list = (),
                             with_friends: bool = False,
                             has_text: bool = True, extension=None, bot_username: str = "") -> str:
    """Блок подтверждения ПОД альбомом-превью: альбом с подписью — само
    объявление в том виде, в каком уйдёт людям."""
    if extension is not None:
        foot = _extension_footer(extension[0], extension[1], n, bot_username)
    else:
        w = plural_ru(n, "адресат", "адресата", "адресатов")
        foot = f"{n} {w}, {_bc_audience(list(clients), with_friends, bot_username)}"
    head = ui.head("👆 Так увидят получатели", meta=[foot])
    if not has_text:
        head += "\n✍️ Текста нет — уйдут только картинки. Нужен текст — пришли его сообщением"
    return head


def broadcast_too_many_photos() -> str:
    return (f"🖼 Больше {config.TG_ALBUM_MAX} картинок в одно сообщение Telegram "
            "не берёт — лишние не приняты, объявление уйдёт с первыми "
            f"{config.TG_ALBUM_MAX}.")


def broadcast_too_long(actual: int, limit: int, with_photos: bool) -> str:
    """Отказ по длине. Называем и фактическую длину, и лимит: «слишком длинно»
    без цифр заставляет резать наугад."""
    over = actual - limit
    why = (" — с картинками текст едет подписью, а у неё лимит жёстче"
           if with_photos else "")
    return (f"✂️ Не отправлено: в объявлении <b>{actual}</b> символов при лимите "
            f"<b>{limit}</b>{why}.\n\nЛишних: {over}. Сократи и пришли заново — "
            "картинки и адресаты сохранены.")


def broadcast_preview_refused(err: str) -> str:
    """Telegram не принял превью: длина — своим именем (с подвалом «кому» текст
    перерос 4096), всё остальное — разметка: тот же текст провалил бы и рассылку."""
    if "too long" in str(err):
        return ("⚠️ Превью не отправлено: вместе с подвалом «кому» текст длиннее лимита Telegram. "
                "Сократи текст и пришли заново")
    return "⚠️ Разметка бракованная (незакрытый тег?). Проверь текст и пришли заново."


UNDELIVERED_SHOWN = 3                  # недоставленных поимённо в отчёте; дальше — «и ещё N» ссылкой на снимок


def _undelivered_named(failed, clients: list, bot_username: str) -> list[str]:
    """Недоставленные с профилем — имена ссылками; гости (чужие tg) сюда не входят."""
    by_tg = {c.tg_id: c for c in clients if getattr(c, "tg_id", 0)}
    return [profile_link(by_tg[t], bot_username) for t in failed if t in by_tg]


def undelivered_tail(failed, clients: list) -> int:
    """Сколько недоставленных уходит в хвост «и ещё N» со ссылкой на снимок; 0 —
    хвоста со ссылкой нет: все названы или никто не назван поимённо (одни гости —
    в отчёте только число, без ссылки). Обработчик по этому числу решает,
    писать ли снимок."""
    named = _undelivered_named(failed, clients, "")
    return len(failed) - min(len(named), UNDELIVERED_SHOWN) if named else 0


def _undelivered_names(failed, clients: list, bot_username: str, more: str = "") -> str:
    """«[Петя], [Вася]» — недоставленные по tg_id поимённо (ссылками); больше
    UNDELIVERED_SHOWN или чужие tg (гости) — «и ещё N» (more — ссылкой на снимок
    всего списка)."""
    named = _undelivered_named(failed, clients, bot_username)
    out = ", ".join(named[:UNDELIVERED_SHOWN])
    if not named:
        return f"{len(failed)}"
    rest = undelivered_tail(failed, clients)
    if rest > 0:
        out += more or f" и ещё {rest}"
    return out


def undelivered_rows(failed, clients: list, bot_username: str) -> list[str]:
    """Строки снимка «⚠️ Не доставлено»: профили ссылками и одной строкой — гости без профиля."""
    by_tg = {c.tg_id: c for c in clients if getattr(c, "tg_id", 0)}
    rows = [f"профиль {profile_link(by_tg[t], bot_username)}" for t in failed if t in by_tg]
    guests = sum(1 for t in failed if t not in by_tg)
    if guests:
        w = plural_ru(guests, "адресат", "адресата", "адресатов")
        rows.append(f"{guests} {w} без профиля — те, с кем делятся устройствами")
    return rows


UNDELIVERED_TITLE = "⚠️ Не доставлено"
AUDIENCE_TITLE = "📢 Адресаты рассылки"


def broadcast_report(clients: list, with_friends: bool, delivered: int,
                    failed, extension=None, bot_username: str = "", audience_more: str = "",
                    undelivered_more: str = "") -> str:
    """Отчёт: только факт доставки. Само объявление остаётся в чате строкой
    выше. «✅ Разослано владельцам профилей: [Ксюша], [Петя] и тем, с кем они делятся
    устройствами — доставлено: 3 адресата · продлено на 7 дн.» (один профиль — «✅ Отправлено
    владельцу профиля: …») ⏎ «⚠️ Не доставлено: [Петя] — бот заблокирован или аккаунт удалён».
    failed — tg_id недоставленных."""
    w = plural_ru(delivered, "адресат", "адресата", "адресатов")
    verb = "✅ Отправлено" if len(clients) == 1 else "✅ Разослано"
    line = (f"{verb} {_bc_audience(clients, with_friends, bot_username, audience_more, case='dat')} — "
            f"доставлено: {delivered} {w}")
    if extension is not None:
        days, plan = extension
        unl = [e.client for e in plan if e.unlimited]
        line += f" · продлено на {_days(days)}"
        if unl:
            line += f" ({_names(unl, bot_username)} — без продления)"
    failed = list(failed) if not isinstance(failed, int) else [0] * failed
    if failed:
        # молчать о недоставленных нельзя: «доставлено» стало бы неправдой
        line += (f"\n⚠️ Не доставлено: {_undelivered_names(failed, clients, bot_username, undelivered_more)} — "
                 "бот заблокирован или аккаунт удалён"
                 + ("; подписка всё равно продлена" if extension is not None else ""))
    return line
