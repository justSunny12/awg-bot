"""bundle.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e
from awgbot.bot.texts import gateway as pkg       # имена пакета — через него: их подменяют тесты
# отчёт о применении — текст уведомления домена, общий с экраном итога
from awgbot.domain.notices.gateway import gateway_apply_report  # noqa: F401


GW_BACKUP_NO_KEY = ("💾 Бэкап шлюза — только шифрованный: внутри приватные ключи линка. "
                    "Задай парольную фразу: 🔐 Шифрование")


def host_rebooted(hostname: str, who: str) -> str:
    """«Хост перезагружен» — после всех проверок старта; who — «бота»/«агента»."""
    return f"⚠️ Хост {_e(hostname)} был перезагружен.\n✅ Запуск {_e(who)} успешен"


def gw_confirm_reassert(carries: bool = True) -> str:
    return ("🔧 Восстановить шлюз?\nЮнит переставит правила (маскарад, изоляция, метка) и "
            "переподнимет линк" + (" — РФ-доступ прервётся на секунды" if carries else ""))


GW_FIRST_RUN_FILE = ("Это файл первого применения — его не присылают боту, а запускают на устройстве: "
                     "<code>sudo sh awg-gw-bundle.sh --install</code>. Из чата убрал: внутри ключи")
def gw_bundle_not_ours(cfg1: bool = False) -> str:
    """Прислан не тот файл. cfg1 — сервер шлёт документ по каналу: файла
    конфигурации из карточки шлюза для этого шлюза нет."""
    return "Это не конфигурация шлюза — файл не принят. " + (
        "Конфигурацию этот шлюз получает от бота сервера AWG по упр. каналу" if cfg1 else
        "Файлы конфигурации выпускает бот сервера AWG: «🛰 Шлюзы» → карточка шлюза → «📤 Конфигурация»")
GW_BUNDLE_PASSPHRASE_QUESTION = (
    ui.head("🔐 В конфигурации — ключевая фраза бэкапов, и она отличается "
            "от заданной на шлюзе") + "\n\nПерезаписать фразу шлюза фразой с сервера AWG? Прежние "
    "копии шлюза останутся открываемыми только старой фразой. Если оставить свою — "
    "всё остальное из файла применится как обычно.")


def gateway_claim_via_channel_text(status: str, by_channel: bool = False) -> str:
    """Токен пометки ушёл серверу по каналу — пересылать ничего не нужно.
    by_channel — сервер присылает документ конфигурации сам: файла не будет."""
    head = pkg._claim_head(status)
    tail = ("назначит его шлюзом и отправит конфигурацию по упр. каналу сам" if by_channel else
            "перевыпустит файл конфигурации — отправь его сюда")
    return (head + "\n\nЗапрос на назначение отправлен серверу AWG по управляющему каналу: "
            f"бот сервера AWG найдёт это устройство по ключу и {tail}")


def _claim_head(status: str) -> str:
    if status == "unmarked":
        return ui.head("🛰 Шлюз в боте сервера AWG не назначен")
    if status == "unconfirmed":
        return ui.head("⚠️ Аплинк этого устройства не найден",
                       value="подтвердить шлюз нечем: подними аплинк и примени конфигурацию ещё раз")
    return ui.head("⚠️ Шлюз этого слота — другое устройство",
                   value="линк на этом устройстве лежит: смени шлюз в боте сервера AWG: "
                         "«🛰 Шлюзы» → карточка шлюза → «✏️ Изменить» → «🔁 Заменить»")


def gateway_claim_forward_text(token: str, status: str, by_channel: bool = False) -> str:
    """by_channel — сервер, по памяти прошлых сессий, шлёт документ сам. Не
    помним — сервер мог и не говорить с этим шлюзом: сервер 3.3.0 ответит
    каналом (агент подписал cfg1), прежний — файлом."""
    head = pkg._claim_head(status)
    tail = ("отправит конфигурацию сам по упр. каналу, когда он поднимется" if by_channel else
            "пришлёт конфигурацию — по упр. каналу, когда он поднимется, или файлом; файл отправь сюда")
    return (head + "\n\nКанал конфигурации сервера AWG недоступен. Перешли это сообщение "
            "боту сервера AWG как есть: он найдёт это устройство по ключу, назначит его шлюзом "
            f"и {tail}\n\n<code>{_e(token)}</code>")


def gateway_config_result(ok: bool, detail: str, mail: dict | None = None) -> str:
    """Итог применения конфигурации: «✅ Конфигурация шлюза применена» и отчёт
    строками; отказ — общая маска «🔴 Конфигурация шлюза не применена:» и
    причина строкой ниже. mail — {state: ok|fail, why, backup}: почта из файла
    проверена сразу, итог — отдельной строкой; backup — бэкапы переключены на
    e-mail вслед за сервером, об этом ещё строка."""
    if ok:
        # отчёт — готовый HTML из gateway_apply_report (адреса в <code>)
        lines = ["✅ <b>Конфигурация шлюза применена</b>"] + ([detail] if detail else [])
    else:
        lines = ["🔴 <b>Конфигурация шлюза не применена:</b>", _e(detail or "причина не названа")]
    mail = mail or {}
    if mail.get("state") == "ok":
        lines.append("✉️ Почта из конфигурации принята: IMAP и SMTP отвечают, вход выполнен")
        if mail.get("backup"):
            lines.append("💾 Бэкапы теперь будут приходить на e-mail")
    elif mail.get("state") == "fail":
        lines.append(f"✉️ Почта из конфигурации принята, но проверка не прошла: {_e(mail.get('why') or '')}")
    return "\n".join(lines)


def gateway_op_result(title: str, ok: bool, detail: str) -> str:
    head = ui.head(f"{'✅' if ok else '🔴'} {title}: {'готово' if ok else 'не удалось'}")
    return head + (f"\n<code>{_e(detail)}</code>" if detail else "")


def gateway_bundle_received(link_changed: bool, carries: bool = True) -> str:
    if link_changed:
        return (ui.head("📦 Конфигурация с сервера AWG") + "\n"
                "Линк перезапустится" + (" — РФ-доступ у всех прервётся на секунды" if carries else "")
                + "; правила переставятся")
    return (ui.head("📦 Конфигурация с сервера AWG") + "\n"
            "Конфиг линка не изменился — линк не перезапустится; правила переставятся")


__all__ = [
    "gateway_apply_report", "GW_BACKUP_NO_KEY", "host_rebooted", "gw_confirm_reassert",
    "GW_FIRST_RUN_FILE", "gw_bundle_not_ours", "GW_BUNDLE_PASSPHRASE_QUESTION",
    "gateway_claim_via_channel_text", "_claim_head", "gateway_claim_forward_text",
    "gateway_config_result", "gateway_op_result", "gateway_bundle_received"]
