"""zone.py — часовой пояс показа в текстах обеих ролей: уведомление после
обновления, снявшего ключ timezone.

«сервера» / «шлюза» — br.host_gen роли."""

from __future__ import annotations

from .fmt import _e


def zone_name_label(name: str, tz=None) -> str:
    """«Asia/Yekaterinburg (UTC+5)»; пояс без имени — «UTC+5»; tz — пояс,
    чьё смещение показать (None — пояс показа)."""
    from awgbot.util import timeutil
    label = timeutil.tz_label(tz=tz)
    return f"{_e(name)} ({label})" if name else label


def zone_adopted(br, prev: str = "") -> str:
    """Первый старт после обновления, снявшего ключ timezone: в каком поясе
    теперь время; прежний пояс — когда он был другим. Уведомление — без точки
    в конце."""
    from awgbot.core import config, zone
    line = f"🕐 Время теперь показываю в поясе {br.host_gen}: {zone_name_label(config.TZ_NAME)}"
    old = zone.load(prev) if prev and prev != config.TZ_NAME else None
    if old is not None:
        line += f", раньше — {zone_name_label(prev, old)}"
    return (line + "\nДругой пояс — <code>sudo timedatectl set-timezone &lt;пояс&gt;</code> на "
            f"{br.host_loc} и «🔁 Перезапустить бота» в «⚙️ Настройки → 🔧 Сервис»")
