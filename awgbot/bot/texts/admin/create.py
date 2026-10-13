"""create.py — часть пакета admin (см. __init__): новый профиль, приглашение, устройство профилю."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import _e, gb_str, profile_link, _n_devices


# ─────────────────────────────────────────────────────────────────────────────
# Новый профиль, приглашение, устройство профилю
# ─────────────────────────────────────────────────────────────────────────────

NEW_PROFILE_NAME = ui.head("➕ Новый профиль") + " — как назвать?"


def new_profile_devs(name: str) -> str:
    return ui.head(f"➕ {_e(name)}") + " — сколько устройств?"


def new_profile_traffic(name: str, devs: int) -> str:
    return ui.head(f"➕ {_e(name)}", meta=[_n_devices(devs) if devs else "∞ устройств"]) + " — трафик в месяц? Общий на все устройства"


def new_profile_period(name: str, devs: int, gb_limit: int) -> str:
    d = _n_devices(devs) if devs else "∞ устройств"
    t = f"{gb_limit} ГБ" if gb_limit else "∞ ГБ"
    return ui.head(f"➕ {_e(name)}", meta=[d, t]) + " — срок подписки?"


INVITE_FORWARD_TEMPLATE = (
    "Привет! Тебе открыт доступ в свободный интернет 🎉\n"
    "Жми ссылку и «Старт» — дальше подскажу\n"
    "{link}"
)


def invite_plain(link: str) -> str:
    return INVITE_FORWARD_TEMPLATE.format(link=link)


def invite_screen(link: str, client, *, new: bool = True) -> str:
    """Приглашение одним сообщением: текст для пересылки (его же несут кнопки
    «📤 Отправить» / «📋 Скопировать»), черта, чьё оно — имя без ссылки (в
    карточку ведёт кнопка), лимиты и срок. new — только что созданный профиль."""
    d = _n_devices(client.device_limit) if client.device_limit else "∞ устройств"
    t = f"{gb_str(client.traffic_limit)} в месяц" if client.traffic_limit else "∞ ГБ в месяц"
    end = client.effective_period_end
    till = f"→ {timeutil.fmt_date_ui(timeutil.parse_iso(end))}" if end else "бессрочно"
    head = ("☝️ Приглашение с индивидуальным кодом для нового профиля:" if new
            else "☝️ Новое приглашение с индивидуальным кодом для профиля:")
    return f"{invite_plain(link)}\n—\n{head}\n{_e(client.name)} — {d} · {t} · {till}"


def add_device_prompt_admin(client, used: int, limit: int) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    return f"{ui.head(f'➕ Устройство профилю {_e(client.name)}')}{slots}\nКак назвать?"


def device_created_admin(name: str, client, bot_username: str = "") -> str:
    return f"✅ {_e(name)} создано для профиля {profile_link(client, bot_username)}"


def admin_bootstrap_device(address: str) -> str:
    """Сообщение о первом устройстве админа, заведённом ботом самостоятельно."""
    return ("🔑 Завёл тебе первое устройство «Админ» (<code>" + _e(address) + "</code>).\n\n"
            "Пиры, созданные в обход бота, теперь попадают в карантин и поднимают "
            "тревогу — значит взять себе доступ «снаружи» больше нельзя, и первое "
            "устройство бот обязан выдать сам. Ссылка ниже: импортируй её в "
            "AmneziaVPN.\n\n"
            "QR и файл — в меню, карточка устройства")
