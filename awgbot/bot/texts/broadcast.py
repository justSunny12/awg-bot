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
    продлением — кто получит и что значат ∞ и 🟡 у имён."""
    marked = [f"отмечено {selected}"] if selected else []
    if extend:
        return (ui.head("📢 Объявление с продлением", meta=marked) + "\n"
                "Получат только владельцы профилей с подпиской\n"
                "∞ — бессрочная, не продлится · 🟡 — истекла, продлится с сегодняшнего дня")
    return (ui.head("📢 Объявление", meta=marked) + "\n"
            "Получат владельцы и те, с кем они делятся устройствами")


BROADCAST_NO_TARGETS = "Никого не отметил — выбери хотя бы один профиль"
BROADCAST_ALL_UNLIMITED = ("Продлевать некого: у всех отмеченных подписка бессрочная. "
                           "Сними «✅ С продлением подписки» — уйдёт обычное объявление")
BROADCAST_DAYS_BAD = "⚠️ Дней — число от 1 до 365, попробуй ещё раз"
BROADCAST_DAYS_OTHER = ui.head("✏️ Продление, дней", meta=["1–365"]) + "\n\nПришли число"


def subscription_mark(c) -> str:
    """Хвост к имени профиля в выборе адресатов с продлением: « ∞» — бессрочная,
    « 🟡 DD.MM» — истекла тогда-то; активной — ничего."""
    end = c.effective_period_end
    if not end:
        return " ∞"
    dt = timeutil.parse_iso(end)
    if c.status == "expired" or dt <= timeutil.now():
        return f" 🟡 {timeutil.fmt_date_ui(dt)}"
    return ""


def _names(clients, bot_username: str) -> str:
    return ", ".join(profile_link(c, bot_username) for c in clients)


def broadcast_days_prompt(plan, bot_username: str = "") -> str:
    """Шаг дней: «📢 Продление подписки: [Ксюша], [Петя]» ⏎ «Без продления (∞):
    [Вечный]» ⏎ «На сколько дней продлеваем?»."""
    names = ", ".join(profile_link(e.client, bot_username) for e in plan if not e.unlimited)
    unl = ", ".join(profile_link(e.client, bot_username) for e in plan if e.unlimited)
    lines = [ui.head("📢 Продление подписки", value=names)]
    if unl:
        lines.append(f"Без продления (∞): {unl}")
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
    who = "адресат" if n == 1 else "адресата" if 2 <= n <= 4 else "адресатов"
    lines = [f"{n} {who} · продление на <b>{_days(days)}</b>:"]
    for e in plan:
        if e.unlimited:
            lines.append(f"• {profile_link(e.client, bot_username)}: ∞ — без продления")
            continue
        old, new = timeutil.fmt_date_ui(e.old_end), timeutil.fmt_date_ui(e.new_end)
        lines.append(f"• {profile_link(e.client, bot_username)}: {'🟡 ' if e.from_now else ''}{old} → {new}")
    return "\n".join(lines)


def _bc_audience(clients: list, with_friends: bool, bot_username: str = "") -> str:
    """«профили Ксюша, Петя и те, с кем они делятся устройствами» — имена
    ссылками; про друзей — только когда они есть среди адресатов."""
    who = _names(clients, bot_username)
    solo = len(clients) == 1
    head = f"профиль {who}" if solo else f"профили {who}"
    if not with_friends:
        return head
    return head + (" и те, с кем он делится устройствами" if solo else " и те, с кем они делятся устройствами")


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
    return head + "\n" + _how_to(config.TG_TEXT_MAX - r, config.TG_CAPTION_MAX - r)


def broadcast_preview(text: str, n: int, clients: list = (),
                      with_friends: bool = False, extension=None, bot_username: str = "") -> str:
    """Превью с текстом и подвалом «кому»; с продлением — кому и на сколько."""
    if extension is not None:
        foot = _extension_footer(extension[0], extension[1], n, bot_username)
    else:
        w = plural_ru(n, "адресат", "адресата", "адресатов")
        foot = f"{n} {w}: {_bc_audience(list(clients), with_friends, bot_username)}"
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
        foot = f"{n} {w}: {_bc_audience(list(clients), with_friends, bot_username)}"
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


_UNDELIVERED_SHOWN = 3


def _undelivered_names(failed, clients: list, bot_username: str) -> str:
    """«[Петя], [Вася]» — недоставленные по tg_id поимённо (ссылками); больше
    трёх или чужие tg (гости) — «и ещё N»."""
    by_tg = {c.tg_id: c for c in clients if getattr(c, "tg_id", 0)}
    named = [profile_link(by_tg[t], bot_username) for t in failed if t in by_tg]
    rest = len(failed) - min(len(named), _UNDELIVERED_SHOWN)
    out = ", ".join(named[:_UNDELIVERED_SHOWN])
    if rest > 0:
        out += f" и ещё {rest}" if out else f"{rest}"
    return out


def broadcast_report(clients: list, with_friends: bool, delivered: int,
                    failed, extension=None, bot_username: str = "") -> str:
    """Отчёт: только факт доставки. Само объявление остаётся в чате строкой
    выше. «✅ Доставлено: профили [Ксюша], [Петя] и те, с кем они делятся
    устройствами — 3 адресата · продлено на 7 дн.» ⏎ «⚠️ Не доставлено: [Петя] —
    бот заблокирован или аккаунт удалён». failed — tg_id недоставленных."""
    w = plural_ru(delivered, "адресат", "адресата", "адресатов")
    line = f"✅ Доставлено: {_bc_audience(clients, with_friends, bot_username)} — {delivered} {w}"
    if extension is not None:
        days, plan = extension
        unl = [profile_link(e.client, bot_username) for e in plan if e.unlimited]
        line += f" · продлено на {_days(days)}"
        if unl:
            line += f" ({', '.join(unl)} — без продления)"
    failed = list(failed) if not isinstance(failed, int) else [0] * failed
    if failed:
        # молчать о недоставленных нельзя: «доставлено» стало бы неправдой
        line += (f"\n⚠️ Не доставлено: {_undelivered_names(failed, clients, bot_username)} — "
                 "бот заблокирован или аккаунт удалён"
                 + ("; подписка всё равно продлена" if extension is not None else ""))
    return line
