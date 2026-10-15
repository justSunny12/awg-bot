"""bundle.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты
# отчёт о применении — текст уведомления домена, общий с экраном итога
from awgbot.domain.notices.gateway import gateway_apply_report, APPLY_PARTIAL_MARK  # noqa: F401
from .common import mail_why, MAIL_FIX_TAIL


GW_BACKUP_NO_KEY = ("💾 Бэкап шлюза — только шифрованный: в копии приватные ключи линка и туннеля. "
                    "Задай фразу — 🔐 Шифрование")
GW_NO_FILE = "Файл уже не у меня — пришли его заново"


def host_rebooted(hostname: str, who: str) -> str:
    """«Шлюз pi перезагрузился — бот снова на связи» — после всех проверок
    старта; who — «бота» (сервер AWG) / «агента» (шлюз): функция общая двум
    ролям, развилка по тому, кто запустился."""
    if who == "агента":
        return f"⚠️ Шлюз {_e(hostname)} перезагрузился — бот снова на связи"
    return "⚠️ Сервер AWG перезагрузился — бот снова на связи"


def gw_confirm_reassert(carries: bool = True) -> str:
    return ("🔧 Восстановить шлюз?\nБот переставит правила шлюза (маскарад, изоляция, метка) и "
            "переподнимет линк" + (" — РФ-доступ прервётся на секунды" if carries else ""))


def gw_first_run_file(name: str = "awg-gw-bundle.sh") -> str:
    """Прислан файл установки шлюза — под тем именем, под каким прислали:
    команда с чужим именем файла у человека бы не нашлась."""
    return ("⚠️ Это файл установки шлюза — его не присылают боту, а запускают в терминале шлюза: "
            f"<code>sudo sh {_e(name or 'awg-gw-bundle.sh')} --install</code>. Из чата убрал: внутри ключи")



def gw_bundle_not_ours(cfg1: bool = False) -> str:
    """Прислан не тот файл. cfg1 — сервер шлёт документ по каналу: файла
    конфигурации из карточки шлюза для этого шлюза нет."""
    return "⚠️ Это не конфигурация шлюза — файл не принят. " + (
        "Конфигурацию этот шлюз получает от бота сервера AWG по каналу конфигурации" if cfg1 else
        "Файлы конфигурации выпускает бот сервера AWG: «🛰 Шлюзы» → карточка шлюза → «📤 Конфигурация»")


GW_BUNDLE_PASSPHRASE_QUESTION = (
    ui.head("🔐 Ключевая фраза бэкапов в конфигурации отличается от фразы шлюза") + "\n\n"
    "«✅ Принять» — новые копии шлюза зашифруются фразой с сервера AWG, прежние копии шлюза "
    "откроются только прежней фразой; «⬅️ Оставить своё» — всё остальное из файла применится как обычно")


def gateway_claim_via_channel_text(status: str, by_channel: bool = False) -> str:
    """Токен пометки ушёл серверу по каналу — пересылать ничего не нужно.
    by_channel — сервер присылает документ конфигурации сам: файла не будет."""
    head = pkg._claim_head(status)
    tail = ("назначит его шлюзом и пришлёт конфигурацию сам" if by_channel else
            "перевыпустит файл конфигурации — отправь его сюда")
    return (head + "\n\nЗапрос на назначение ушёл по каналу конфигурации: "
            f"бот сервера AWG найдёт это устройство по ключу и {tail}")


def _claim_head(status: str) -> str:
    """Заголовок пометки — жирным, точка после него, пояснение обычным."""
    if status == "unmarked":
        return ui.head("🛰 Шлюз в боте сервера AWG не назначен")
    if status == "unconfirmed":
        return ("⚠️ <b>Аплинк этого устройства не найден.</b> Подтвердить шлюз нечем: "
                "подними аплинк и примени конфигурацию ещё раз")
    return "⚠️ <b>Шлюз указывает на устройство, отличное от выбранного.</b> Линк на этом устройстве лежит"


def gateway_claim_forward_text(token: str, status: str, by_channel: bool = False) -> str:
    """by_channel — сервер, по памяти прошлых сессий, шлёт документ сам. Не
    помним — сервер мог и не говорить с этим шлюзом: сервер 3.3.0 ответит
    каналом (агент подписал cfg1), прежний — файлом. Чужой слот — пересылка
    и делает это устройство шлюзом; ручная замена в боте сервера AWG — в скобках."""
    head = pkg._claim_head(status)
    tail = ("пришлёт конфигурацию сам, когда поднимется канал конфигурации" if by_channel else
            "пришлёт конфигурацию — сам, когда поднимется канал конфигурации, или файлом; файл отправь сюда")
    how = ("Чтобы шлюзом стало это устройство, перешли это сообщение боту сервера AWG как есть "
           "(или замени вручную: «🛰 Шлюзы» → карточка шлюза → «✏️ Изменить» → «🔁 Заменить»)"
           if status == "foreign" else "Перешли это сообщение боту сервера AWG как есть")
    return (head + f"\n\nКанал конфигурации сервера AWG недоступен. {how}: бот найдёт это устройство "
            f"по ключу, назначит его шлюзом и {tail}\n\n<code>{_e(token)}</code>")


def _script_fail(detail: str) -> str:
    """Причина отказа скрипта без имени скрипта и «откатываю»: «аплинк awg1 не
    поднялся» — имя интерфейса моноширинным."""
    import re
    text = (detail or "причина не названа").strip()
    text = re.sub(r"^routing-gw-setup\.sh:\s*", "", text)
    text = re.sub(r"\s*—\s*откатываю.*$", "", text)
    return re.sub(r"&lt;([^&]+)&gt;", r"<code>\1</code>", _e(text))


def gateway_config_result(ok: bool, detail: str, mail: dict | None = None) -> str:
    """Итог применения конфигурации: «✅ Конфигурация шлюза применена» и отчёт
    строками; есть строка «не применён» — «⚠️ … применена не полностью»;
    отказ — «🔴 Конфигурация шлюза не применена — прежняя осталась» и причина
    строкой ниже. mail — {state: ok|fail, why, backup}: почта из файла
    проверена сразу, итог — отдельной строкой; backup — бэкапы переключены на
    e-mail вслед за сервером, об этом ещё строка."""
    mail = mail or {}
    if ok:
        # отчёт — готовый HTML из gateway_apply_report (адреса в <code>)
        partial = APPLY_PARTIAL_MARK in (detail or "") or mail.get("state") == "fail"
        head = ("⚠️ <b>Конфигурация шлюза применена не полностью</b>" if partial
                else "✅ <b>Конфигурация шлюза применена</b>")
        lines = [head] + ([detail] if detail else [])
    else:
        lines = ["🔴 <b>Конфигурация шлюза не применена — прежняя осталась</b>", _script_fail(detail),
                 "Причина — в «🩺 Здоровье»"]
    if mail.get("state") == "ok":
        lines.append("✉️ Почта из конфигурации принята: IMAP и SMTP отвечают, вход выполнен")
        if mail.get("backup"):
            lines.append("💾 Бэкапы теперь уходят на почту")
    elif mail.get("state") == "fail":
        lines.append(f"⚠️ Почта из конфигурации принята, но не работает: {mail_why(mail.get('why') or '')}. "
                     + MAIL_FIX_TAIL)
    return "\n".join(lines)


def gateway_op_result(title: str, ok: bool, detail: str, hint: str = "") -> str:
    """«✅ Восстановление: готово» / «🔴 Восстановление: не удалось» + деталь
    моноширинным; hint — следующий шаг при отказе, строкой ниже."""
    head = ui.head(f"{'✅' if ok else '🔴'} {title}: {'готово' if ok else 'не удалось'}")
    text = head + (f"\n<code>{_e(detail)}</code>" if detail else "")
    return text + (f"\n{hint}" if hint and not ok else "")


REASSERT_FAIL_HINT = "Причина — в «🩺 Здоровье»; повтори через минуту"


def gateway_bundle_received(link_changed: bool, carries: bool = True) -> str:
    if link_changed:
        return (ui.head("📦 Конфигурация с сервера AWG") + "\n"
                "Линк перезапустится" + (" — РФ-доступ у всех прервётся на секунды" if carries else "")
                + "; правила переставятся")
    return (ui.head("📦 Конфигурация с сервера AWG") + "\n"
            "Конфиг линка не изменился — линк не перезапустится; правила переставятся")


__all__ = [
    "gateway_apply_report", "GW_BACKUP_NO_KEY", "GW_NO_FILE", "host_rebooted", "gw_confirm_reassert",
    "gw_first_run_file", "gw_bundle_not_ours", "GW_BUNDLE_PASSPHRASE_QUESTION",
    "gateway_claim_via_channel_text", "_claim_head", "gateway_claim_forward_text",
    "gateway_config_result", "gateway_op_result", "REASSERT_FAIL_HINT", "gateway_bundle_received"]
