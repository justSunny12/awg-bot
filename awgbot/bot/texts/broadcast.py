"""Объявления пользователям: выбор адресатов, продление, превью, отчёт."""

from __future__ import annotations

from awgbot.core import config
from awgbot.util import timeutil

from .fmt import client_label, plural_ru, _days


# ── Броадкаст ────────────────────────────────────────────────────────────────
BROADCAST_EMPTY = ("Так не пойдёт: жду текст объявления или картинку. "
                   "Пришли что-нибудь из этого или нажми «Отмена».")


BROADCAST_MODE = (
    "📢 <b>Объявление пользователям</b>\n\n"
    "Уведомление выбранным пользователям от имени бота.\n\n"
    "<b><i>Простое</i></b> — текст и/или изображения. Отправляется владельцам "
    "профилей <u>и тем, с кем они поделились устройствами</u>.\n"
    "<b><i>С продлением подписки</i></b> — отличается от простого возможностью "
    "продлить подписку адресатам на заданное количество дней. Отправляется "
    "<u>только владельцам профилей</u>."
)

BROADCAST_TARGETS = (
    "📢 <b>Кому объявление</b>\n\n"
    "Отметь профили. Объявление получит владелец каждого отмеченного профиля "
    "<b>и те, с кем он поделился устройствами</b>."
)

BROADCAST_TARGETS_EXTEND = (
    "📢 <b>Кому объявление с продлением</b>\n\n"
    "Отметь профили. Подписка будет продлена владельцу каждого отмеченного; те, "
    "с кем он поделился устройствами, объявление не получат.\n"
    "∞ — бессрочная: получит объявление, продлевать нечего.\n"
    "⛔ — истекла, дата рядом: продление от текущей даты."
)

BROADCAST_NO_TARGETS = "Никого не отметил — выбери хотя бы один профиль."
BROADCAST_ALL_UNLIMITED = ("Все отмеченные — с бессрочной подпиской, продлевать некого. "
                           "Для них — простое объявление.")
BROADCAST_DAYS_BAD = "⚠️ Нужно целое число от 1 до 365. Попробуй ещё раз."


def subscription_mark(c) -> str:
    """Хвост к имени профиля в выборе адресатов с продлением: « ∞» — бессрочная,
    « ⛔ DD.MM.YYYY» — истекла тогда-то; активной — ничего."""
    end = c.effective_period_end
    if not end:
        return " ∞"
    dt = timeutil.parse_iso(end)
    if c.status == "expired" or dt <= timeutil.now():
        return f" ⛔ {timeutil.fmt_date(dt)}"
    return ""


def broadcast_days_prompt(plan) -> str:
    """Шаг дней: кому продлеваем (бессрочные — с оговоркой) и сколько."""
    def row(e):
        return client_label(e.client) + (" (∞, без продления)" if e.unlimited else "")
    if len(plan) == 1:
        head, whom = f"Адресат уведомления — {row(plan[0])}.", "ему"
    else:
        head, whom = "Выбранные адресаты:\n" + "\n".join("• " + row(e) for e in plan), "им"
    return (f"📢 <b>Объявление с продлением подписки</b>\n\n{head}\n\n"
            f"На какое количество дней {whom} необходимо продлить подписку? (1–365)")


def extension_header(days: int, ext) -> str:
    """Шапка объявления с продлением — у каждого адресата своя (даты его).
    Бессрочному — пусто. Истёкшему — отсчёт от сегодня, и так и сказано."""
    if ext is None or ext.unlimited:
        return ""
    new = timeutil.fmt_date(ext.new_end)
    if ext.from_now:
        return (f"<b>К длительности твоей подписки добавлено {_days(days)} с текущей даты 🙂\n"
                f"Теперь срок подписки: до {new}</b>")
    return (f"<b>Длительность твоей подписки увеличена на {_days(days)} 🙂\n"
            f"{timeutil.fmt_date(ext.old_end)} → {new}</b>")


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


def _extension_footer(days: int, plan, n: int) -> str:
    who = "человеку, его" if n == 1 else "людям, их"
    lines = [f"Будет отправлено <b>{n}</b> {who} подписка будет продлена на "
             f"<b>{_days(days)}</b>:"]
    for e in plan:
        if e.unlimited:
            lines.append(f"• {client_label(e.client)}: ∞ — без продления")
            continue
        old, new = timeutil.fmt_date(e.old_end), timeutil.fmt_date(e.new_end)
        lines.append(f"• {client_label(e.client)}: {'⛔ ' if e.from_now else ''}{old} → {new}")
    return "\n".join(lines)


def _bc_audience(clients: list, with_friends: bool) -> str:
    """«Получит/Получат: <кто>» одной фразой, согласованной по числу.

    Именительный падеж, а не дательный: это подлежащее при «получит», а не
    адресат при «кому». Форма «Получит: Ксюша — владельцу профиля» была
    грамматически битой.

    Про друзей упоминаем ТОЛЬКО когда они реально есть среди адресатов: иначе
    предупреждение про гостевой доступ висит на каждом объявлении и перестаёт
    читаться ровно к тому моменту, когда оно понадобится.
    """
    who = ", ".join(client_label(c) for c in clients)
    solo = len(clients) == 1
    # «Получит» — единственное число, и оно уместно, только когда получатель
    # ровно один: профиль один И друзей у него нет.
    verb = "Получит" if solo and not with_friends else "Получат"
    if solo:
        tail = " и те, с кем он поделился устройствами" if with_friends else ""
        return f"{verb}: {who} — владелец профиля{tail}"
    tail = " и те, с кем они поделились устройствами" if with_friends else ""
    return f"{verb}: {who} — владельцы этих профилей{tail}"


def _broadcast_how_to(text_max: int, caption_max: int) -> str:
    return ("Форматируй как обычно в Telegram — "
            "<b>жирный</b>, <i>курсив</i>, ссылки сохранятся. "
            "Следующим сообщением покажу превью.\n\n"
            f"Можно с картинками — до {config.TG_ALBUM_MAX} штук, и удобнее "
            "всего одним действием: выбери снимки и набери текст прямо в окне "
            "отправки вложений. Порядок не важен — текст можно прислать и до, "
            "и после картинок, превью пересоберётся. Уйдёт одним сообщением: "
            "картинки, текст под ними.\n\n"
            f"<b>Лимит текста зависит от того, есть ли картинки:</b> без них — "
            f"{text_max} символов, с ними — {caption_max}. "
            "Второе не наша скупость: длинную подпись умеют только "
            "Premium-аккаунты, а бот таким быть не может. Не влезло — скажу "
            "сразу и не отправлю.")


def broadcast_prompt(clients: list, with_friends: bool = False, *,
                     extend_days: int | None = None) -> str:
    """Приглашение ввести текст. Адресатов называем поимённо.

    Не «выбрано 3 профиля», а именно список: между выбором и отправкой стоит
    ввод текста, и к моменту подтверждения легко забыть, кого отметил. Цена
    ошибки несимметрична — лишний адресат объявление уже прочитал.

    extend_days — объявление с продлением: адресаты уже названы на шаге дней,
    здесь — «принято» и лимиты за вычетом шапки.
    """
    if extend_days is None:
        return (f"📢 <b>Объявление</b>\n\n"
                f"{_bc_audience(clients, with_friends)}.\n\n"
                "Пришли текст. " + _broadcast_how_to(config.TG_TEXT_MAX, config.TG_CAPTION_MAX))
    r = extension_reserve()
    whom = "адресата" if len(clients) == 1 else "адресатов"
    return (f"✅ Принято: перед отправкой уведомления подписка {whom} будет продлена на "
            f"<b>{_days(extend_days)}</b> (бессрочным — не продлевается), информация об "
            "этом будет добавлена к тексту объявления автоматически — сам можешь не "
            "писать.\n\nТеперь пришли текст объявления. "
            + _broadcast_how_to(config.TG_TEXT_MAX - r, config.TG_CAPTION_MAX - r))


def broadcast_preview(text: str, n: int, clients: list = (),
                      with_friends: bool = False, extension=None) -> str:
    """extension — (days, plan) объявления с продлением: подвал — кому и на
    сколько, вместо строки «Получит…»."""
    if extension is not None:
        foot = _extension_footer(extension[0], extension[1], n)
    else:
        who = "человеку" if n == 1 else "людям"
        scope = f"\n{_bc_audience(list(clients), with_friends)}." if clients else ""
        foot = f"Будет отправлено <b>{n}</b> {who}.{scope}"
    return (f"📢 <b>Превью объявления</b> (так его увидят):\n\n{text}\n\n"
            f"— — —\n{foot}\nОтправляем?")


def broadcast_preview_photos(n: int, clients: list = (),
                             with_friends: bool = False,
                             has_text: bool = True, extension=None) -> str:
    """Блок подтверждения ПОД альбомом-превью.

    С картинками превью — не пересказ, а само объявление: альбом с подписью
    отправляется админу ровно в том виде, в каком уйдёт людям. Пересказать
    альбом текстом нельзя, а «приложено 3 фото» не показывает ни порядок, ни
    то, как подпись села под картинками.

    Без текста это НЕ отдельный шаг-переспрос, а тот же самый блок: превью уже
    стоит, отправить можно как есть, а присланный следом текст вливается сам.
    """
    if extension is not None:
        foot = _extension_footer(extension[0], extension[1], n)
    else:
        who = "человеку" if n == 1 else "людям"
        scope = f"\n{_bc_audience(list(clients), with_friends)}." if clients else ""
        foot = f"Будет отправлено <b>{n}</b> {who}.{scope}"
    head = "👆 Так объявление увидят получатели."
    if not has_text:
        head += ("\n\n✍️ Текста в нём нет — уйдут только картинки. Хочешь с "
                 "текстом — пришли его сообщением, добавлю подписью.")
    return f"{head}\n\n{foot}\nОтправляем?"


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


def broadcast_report(clients: list, with_friends: bool, delivered: int,
                    failed: int, extension=None) -> str:
    """Отчёт об отправленном объявлении: ТОЛЬКО факт доставки/недоставки.

    Само объявление — текстом или альбомом — остаётся в чате СТРОКОЙ ВЫШЕ
    (превью после отправки живёт как след), поэтому ни текст, ни вложения здесь
    не пересказываются: дублировать то, что видно глазами, значит удваивать
    каждую рассылку в истории.

    Число адресатов называем, только когда в рассылку вошли друзья: без них оно
    равно числу профилей и уже видно из перечисления.
    """
    solo = len(clients) == 1
    who = ", ".join(client_label(c, bold=False) for c in clients)
    if solo:
        head, tail = f"владельцу профиля {who}", "и тем, с кем он поделился устройствами"
    else:
        head, tail = f"владельцам профилей {who}", "и тем, с кем они поделились устройствами"

    if with_friends:
        word = plural_ru(delivered, "адресат", "адресата", "адресатов")
        line = f"✅ Объявление выше доставлено {head} {tail}: всего {delivered} {word}."
    else:
        line = f"✅ Объявление выше доставлено {head}"
    if extension is not None:
        days, plan = extension
        unl = [client_label(e.client, bold=False) for e in plan if e.unlimited]
        note = (f" ({', '.join(unl)} — {'бессрочная' if len(unl) == 1 else 'бессрочные'}, "
                "без продления)" if unl else "")
        line += f"; подписка продлена на {_days(days)}{note}."

    if failed:
        # Молчать о недоставленных нельзя: «доставлено» тогда становится
        # неправдой, а узнать об этом больше неоткуда.
        w = plural_ru(failed, "адресату", "адресатам", "адресатам")
        line += (f"\n⚠️ Не доставлено {failed} {w} — заблокировали бота "
                 "или удалили аккаунт"
                 + (f"; подписка {'ему' if failed == 1 else 'им'} всё равно продлена."
                    if extension is not None else "."))
    return line
