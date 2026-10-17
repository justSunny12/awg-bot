"""zone.py — часовой пояс показа в текстах обеих ролей: строка «🔧 Сервис» и
уведомление после обновления, снявшего ключ timezone.

«сервера» / «шлюза» — br.host_gen роли."""

from __future__ import annotations

from .fmt import _e

_SET_ZONE = "<code>sudo timedatectl set-timezone &lt;пояс&gt;</code>"


def zone_name_label(name: str, tz=None) -> str:
    """«Asia/Yekaterinburg (UTC+5)»; пояс без имени — «UTC+5»; tz — пояс,
    чьё смещение показать (None — пояс показа)."""
    from awgbot.util import timeutil
    label = timeutil.tz_label(tz=tz)
    return f"{_e(name)} ({label})" if name else label


def zone_adopted(br, prev: str = "") -> str:
    """Первый старт после обновления, снявшего ключ timezone: в каком поясе
    теперь время и какой был раньше (пояс не изменился — уведомления нет вовсе,
    runtime/main._announce_zone). Уведомление — без точки в конце."""
    from awgbot.core import config, zone
    line = f"🕐 Время теперь показываю в поясе {br.host_gen}: {zone_name_label(config.TZ_NAME)}"
    old = zone.load(prev) if prev and prev != config.TZ_NAME else None
    if old is not None:
        line += f", раньше был {zone_name_label(prev, old)}"
    return (line + f"\nДругой пояс — {_SET_ZONE} на "
            f"{br.host_loc} и «🔁 Перезапуск бота» в «⚙️ Настройки → 🔧 Сервис»")


def svc_zone_warning(br) -> str:
    """Ключ timezone не распознан — открытой строкой «🔧 Сервис»; пусто, когда
    ключ распознан или его нет."""
    from awgbot.core import config
    if config.TZ_SOURCE != "conf_bad":
        return ""
    return (f"⚠️ Часовой пояс <code>{_e(config.TZ_BAD_VALUE)}</code> из app.yaml не распознан — взят пояс "
            f"{br.host_gen} {zone_name_label(config.TZ_NAME)}. Исправь ключ <code>timezone</code> и "
            "перезапусти бота")


def svc_zone_line(br) -> str:
    """Пояс под «подробнее» «🔧 Сервис»: имя и смещение, откуда взят, как
    сменить; при нераспознанном ключе — пусто (там открытая строка ⚠️)."""
    from awgbot.core import config, zone
    if config.TZ_SOURCE == "conf_bad":
        return ""
    shown = zone_name_label(config.TZ_NAME)
    if config.TZ_SOURCE == "conf":
        line = f"Часовой пояс — {shown}, задан ключом <code>timezone</code> в app.yaml"
        host = zone.load(config.TZ_HOST_NAME)
        if config.TZ_HOST_NAME != config.TZ_NAME and host is not None:
            line += f"; у {br.host_gen} — {zone_name_label(config.TZ_HOST_NAME, host)}"
        return line + f". Перейти на пояс {br.host_gen}: убери ключ и перезапусти бота"
    return f"Часовой пояс — {shown}, пояс {br.host_gen}. Сменить: {_SET_ZONE} и «🔁 Перезапуск бота»"
