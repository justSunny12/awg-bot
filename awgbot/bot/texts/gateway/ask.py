"""ask.py — приглашения и вопросы человеку о документе с сервера AWG: ключи линка, конфиг аплинка, почта, фраза бэкапов, настройки шлюза из бэкапа сервера; итоги, строка панели."""
from __future__ import annotations

from awgbot.bot import ui

from ..fmt import _e
from .common import mail_why, MAIL_FIX_TAIL

QUESTION_HUMAN = {"link": "ключи линка", "uplink": "конфиг аплинка", "mail": "почта", "backup": "ключевая фраза бэкапов",
                  "keep": "настройки из бэкапа", "keep_dev": "настройки прежнего устройства"}
QUESTION_STALE = {"mail": "ℹ️ Вопрос снят: ящик на шлюзе задан вручную",
                  "backup": "ℹ️ Вопрос снят: ключевая фраза бэкапов на шлюзе задана вручную",
                  "uplink": "ℹ️ Вопрос снят: аплинк на шлюзе поменяли вручную"}
LINK_KEYS_ACCEPTED = "🔑 Новые ключи приняты — линк переключится по команде сервера AWG"
UPLINK_APPLYING = "⏳ Применяю конфиг аплинка — бот шлюза пропадёт из Telegram на секунды"
UPLINK_BUSY = "⏳ Обвязка шлюза сейчас применяется — нажми «🔐 Применить» через минуту"
QUESTION_ANSWERED = "Вопрос уже решён"
QUESTIONS_NONE = "Вопросы уже решены"
# одна пара кнопок на все вопросы «своё или с сервера» — подписи дословно в тексте
KEEP_LABEL, ACCEPT_LABEL = "⬅️ Оставить своё", "✅ Принять"
# копия настроек шлюза с сервера: после восстановления сервера и для нового устройства в слоте
RESTORE_KEEP_LABEL, RESTORE_ACCEPT_LABEL = "⬅️ Оставить текущие", "↩️ Вернуть из бэкапа"
DEVICE_KEEP_LABEL, DEVICE_ACCEPT_LABEL = "⬅️ Оставить по умолчанию", "↩️ Перенести настройки"
GW_TOKEN_SWITCHED = "🤖 Бот шлюза на новом токене с сервера AWG"


def _fp(fp: str) -> str:
    return f"<code>{_e(fp)}</code>" if fp else "<code>—</code>"


def _date(iso: str) -> str:
    from awgbot.util import timeutil
    try:
        return timeutil.parse_iso(iso).astimezone(timeutil.TZ).strftime("%d.%m.%y")
    except (ValueError, TypeError):
        return "?"


def keep_question_text(q: dict) -> str:
    """Настройки шлюза с сервера AWG — вопросом: после восстановления сервера
    (у того же или другого устройства) и для нового устройства в слоте."""
    what = _e(", ".join(q.get("groups") or []) or "настройки бота шлюза")
    if q["section"] == "keep":
        frm = _date(q.get("from", ""))
        lead = (f"Сервер AWG восстановлен из бэкапа от {frm} — тогда в этом слоте было другое устройство, и "
                f"настройки у него другие: {what}." if q.get("other") else
                f"Сервер AWG восстановлен из бэкапа от {frm}. Настройки этого шлюза в нём другие: {what}.")
        return "\n".join([ui.head("💾 Настройки шлюза из бэкапа"), "", lead, "",
                          f"«{RESTORE_ACCEPT_LABEL}» — применятся настройки от {_date(q.get('at', ''))}; "
                          "«Оставить текущие» — сервер AWG сохранит текущие"])
    return "\n".join([ui.head("💾 Настройки прежнего устройства"), "",
                      "У сервера AWG есть настройки устройства, которое стояло в этом слоте до замены "
                      f"(на {_date(q.get('at', ''))}): {what}.", "",
                      f"«{DEVICE_ACCEPT_LABEL}» — применятся они; «Оставить по умолчанию» — шлюз останется "
                      "на настройках по умолчанию, сервер AWG сохранит их"])


def keep_answer_text(answer: dict) -> str:
    """Итог ответа на вопрос о настройках с сервера."""
    if answer.get("error"):
        return f"⚠️ Настройки из бэкапа не применены: {_e(answer['error'])} — остались текущие"
    if not answer.get("accepted"):
        return "ℹ️ Настройки шлюза остались текущими — сервер AWG сохранил их"
    if answer.get("why") == "restore":
        return f"✅ Настройки шлюза вернулись к бэкапу от {_date(answer.get('from', ''))}"
    return "✅ Настройки прежнего устройства перенесены"


def cfg_question_text(q: dict) -> str:
    """Вопрос по правилу «молча или вопрос»: что отличается — без единого
    значения (ни адресов, ни паролей, ни фразы в чате нет), цена каждого ответа."""
    if q["section"] in ("keep", "keep_dev"):
        return keep_question_text(q)
    if q["section"] == "link":
        price = ("Линк переключится — РФ-доступ у всех прервётся на секунды; не поднимется за 90 с — вернутся прежние ключи"
                 if q.get("carries") else "Линк переключится; не поднимется за 90 с — вернутся прежние ключи")
        return "\n".join([
            ui.head("🔑 Новые ключи линка с сервера AWG"),
            f"Отпечаток {_fp(q.get('fp', ''))} — сверь с карточкой шлюза в боте сервера AWG. {price}"])
    if q["section"] == "uplink":
        device = f"<code>{_e(q['device'])}…</code>" if q.get("device") else "<code>—</code>"
        return "\n".join([
            ui.head("🔐 Новый конфиг аплинка с сервера AWG"),
            f"Ключ устройства {device} · отпечаток {_fp(q.get('fp', ''))} — сверь с карточкой шлюза в боте "
            "сервера AWG. Аплинк переподнимется — бот шлюза пропадёт из Telegram на секунды; "
            "не поднимется за 60 с — вернётся прежний"])
    if q["section"] == "mail":
        differs = ", ".join(q.get("differs") or ["ящик"])
        return "\n".join([
            ui.head("✉️ Почта на шлюзе отличается от почты сервера AWG"),
            f"Различия: {_e(differs)}",
            f"«{ACCEPT_LABEL}» — ящик шлюза заменится после проверки; «{KEEP_LABEL}» — сервер AWG "
            "не спросит снова, пока не сменит свою почту"])
    random_key = bool(q.get("random_key"))
    # вопрос пришёл по каналу — «в конфигурации» тут непонятно: как у почты, «на шлюзе … сервера AWG»
    head = ("🔐 Бэкапы шлюза шифруются случайным ключом, на сервере AWG — ключевая фраза" if random_key
            else "🔐 Ключевая фраза бэкапов на шлюзе отличается от фразы сервера AWG")
    prev = "прежним ключом" if random_key else "прежней фразой"
    return "\n".join([
        ui.head(head),
        f"«{ACCEPT_LABEL}» — новые копии шлюза зашифруются фразой с сервера AWG, прежние откроются "
        f"только {prev}; «{KEEP_LABEL}» — сервер AWG не спросит снова, пока не сменит свою фразу"])


def cfg_answer_text(section: str, accepted: bool, note: dict | None) -> str:
    """Итог решения — правкой сообщения вопроса."""
    note = note or {}
    if section == "link":
        return LINK_KEYS_ACCEPTED if accepted else "ℹ️ Ключи линка остались прежними"
    if section == "uplink":
        if not accepted:
            return "ℹ️ Аплинк остался прежним"
        if note.get("state") == "fail":
            why = _e(str(note.get("why") or "ошибка"))
            if note.get("how") == "reverted":
                return "⚠️ Новый конфиг аплинка не поднялся — вернул прежний"
            if note.get("how") == "revert_failed":
                back = _e(str(note.get("back_why") or note.get("why") or "ошибка"))
                return (f"🔴 Новый конфиг аплинка не поднялся, прежний вернуть не удалось: {back}. "
                        "Загляни в «🩺 Здоровье»; связь с сервером AWG может пропасть")
            return f"⚠️ Новый конфиг аплинка не применён: {why}"
        return "✅ Новый конфиг аплинка применён"
    if section == "mail":
        if not accepted:
            return "ℹ️ Почта шлюза осталась прежней — сервер AWG не спросит снова, пока не сменит свою"
        if note.get("state") == "ok":
            lines = ["✉️ Почта с сервера AWG принята: IMAP и SMTP отвечают, вход выполнен"]
        else:
            lines = [f"⚠️ Почта с сервера AWG принята, но не работает: {mail_why(note.get('why') or '')}. "
                     + MAIL_FIX_TAIL]
        if note.get("backup"):
            lines.append("💾 Бэкапы теперь уходят на почту")
        return "\n".join(lines)
    if not accepted:
        return "ℹ️ Ключевая фраза бэкапов шлюза осталась прежней — сервер AWG не спросит снова, пока не сменит свою"
    if note.get("state") == "fail":
        return f"⚠️ Ключевая фраза бэкапов с сервера AWG не принята: {_e(str(note.get('why') or 'ошибка'))}"
    return "🔐 Ключевая фраза бэкапов с сервера AWG принята — новые копии шлюза шифруются ею"


def cfg_pending_line(sections) -> str:
    """«⏳ Ждут решения: почта, ключевая фраза бэкапов» — в порядке очереди; пусто — строки нет."""
    names = [QUESTION_HUMAN.get(s, s) for s in sections]
    return f"⏳ Ждут решения: {', '.join(names)}" if names else ""
