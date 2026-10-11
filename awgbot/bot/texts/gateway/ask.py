"""ask.py — приглашения и вопросы человеку о документе с сервера AWG: ключи линка, конфиг аплинка, почта, фраза бэкапов; итоги, строка панели."""
from __future__ import annotations

from awgbot.bot import ui

from ..fmt import _e

QUESTION_HUMAN = {"link": "ключи линка", "uplink": "конфиг аплинка", "mail": "почта", "backup": "фраза бэкапов"}
QUESTION_STALE = {"mail": "Вопрос снят: ящик задан здесь вручную",
                  "backup": "Вопрос снят: фраза задана здесь вручную",
                  "uplink": "Вопрос снят: аплинк поменяли здесь вручную"}
LINK_KEYS_ACCEPTED = "🔑 Новые ключи приняты — линк переключится по команде сервера AWG"
UPLINK_APPLYING = "⏳ Применяю конфиг аплинка — бот шлюза пропадёт из Telegram на секунды"
UPLINK_BUSY = "⏳ Обвязка шлюза сейчас применяется — нажми «🔐 Применить» через минуту"
QUESTION_ANSWERED = "Вопрос уже решён"
GW_TOKEN_SWITCHED = "🤖 Бот шлюза на новом токене с сервера AWG"


def _fp(fp: str) -> str:
    return f"<code>{_e(fp)}</code>" if fp else "<code>—</code>"


def cfg_question_text(q: dict) -> str:
    """Вопрос по правилу «молча или вопрос»: что отличается — без единого
    значения (ни адресов, ни паролей, ни фразы в чате нет), цена каждого ответа."""
    if q["section"] == "link":
        price = ("Линк переключится — РФ-доступ прервётся на секунды; не поднимется за 90 с — вернутся прежние ключи"
                 if q.get("carries") else "Линк переключится; не поднимется за 90 с — вернутся прежние ключи")
        return "\n".join([
            ui.head("🔑 Новые ключи линка с сервера AWG"),
            f"Отпечаток {_fp(q.get('fp', ''))} — сверь с карточкой шлюза на сервере AWG. {price}"])
    if q["section"] == "uplink":
        device = f"<code>{_e(q['device'])}…</code>" if q.get("device") else "<code>—</code>"
        return "\n".join([
            ui.head("🔐 Новый конфиг аплинка с сервера AWG"),
            f"Ключ устройства {device} · отпечаток {_fp(q.get('fp', ''))}. Аплинк переподнимется — бот шлюза "
            "пропадёт из Telegram на секунды; не поднимется за минуту — вернётся прежний"])
    if q["section"] == "mail":
        differs = ", ".join(q.get("differs") or ["ящик"])
        return "\n".join([
            ui.head("✉️ Почта на шлюзе отличается от почты сервера AWG"),
            f"Отличается: {_e(differs)}",
            "Принять с сервера — ящик шлюза заменится и проверится; оставить своё — сервер AWG не спросит "
            "снова, пока не сменит свою почту"])
    head = ("🔐 Бэкапы шлюза шифруются случайным ключом, на сервере AWG — фраза" if q.get("random_key")
            else "🔐 Фраза шифрования бэкапов на шлюзе отличается от фразы сервера AWG")
    return "\n".join([
        ui.head(head),
        "Принять с сервера — новые копии шлюза зашифруются фразой сервера, прежние откроются только своей; "
        "оставить своё — сервер AWG не спросит снова, пока не сменит свою фразу"])


def cfg_answer_text(section: str, accepted: bool, note: dict | None) -> str:
    """Итог решения — правкой сообщения вопроса."""
    note = note or {}
    if section == "link":
        return LINK_KEYS_ACCEPTED if accepted else "Ключи линка остались прежними"
    if section == "uplink":
        if not accepted:
            return "Аплинк остался прежним"
        if note.get("state") == "fail":
            why = _e(str(note.get("why") or "ошибка"))
            if note.get("how") == "reverted":
                return "⚠️ Новый конфиг аплинка не поднялся — вернул прежний"
            if note.get("how") == "revert_failed":
                return f"⚠️ Новый конфиг аплинка не поднялся, и прежний не вернулся: {why}"
            return f"⚠️ Новый конфиг аплинка не применён: {why}"
        return "✅ Аплинк на новом конфиге"
    if section == "mail":
        if not accepted:
            return "Почта шлюза осталась своей — сервер AWG не спросит снова, пока не сменит свою"
        if note.get("state") == "ok":
            lines = ["✉️ Почта с сервера AWG принята: IMAP и SMTP отвечают, вход выполнен"]
        else:
            lines = [f"✉️ Почта с сервера AWG принята, но проверка не прошла: {_e(str(note.get('why') or 'ошибка'))}"]
        if note.get("backup"):
            lines.append("💾 Бэкапы теперь будут приходить на e-mail")
        return "\n".join(lines)
    if not accepted:
        return "Фраза шлюза осталась своей — сервер AWG не спросит снова, пока не сменит свою"
    if note.get("state") == "fail":
        return f"⚠️ Фраза с сервера AWG не принята: {_e(str(note.get('why') or 'ошибка'))}"
    return "🔐 Фраза с сервера AWG принята — новые копии шлюза шифруются ею"


def cfg_pending_line(sections) -> str:
    """«⏳ Ждут решения: почта, фраза бэкапов» — в порядке очереди; пусто — строки нет."""
    names = [QUESTION_HUMAN.get(s, s) for s in sections]
    return f"⏳ Ждут решения: {', '.join(names)}" if names else ""
