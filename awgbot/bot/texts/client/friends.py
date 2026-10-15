"""friends.py — часть пакета client (см. __init__): приглашение другу, переданные устройства, пояснения к выдаче."""

from __future__ import annotations

from ..fmt import _e, client_link, owner_link, holder_link, _n_devices
from .activation import ACTIVATION_OK


# ── приглашение другу ────────────────────────────────────────────────────────

def _invite_link(code: str, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start={code}"


def friend_invite_message(device_name: str, code: str, bot_username: str) -> str:
    """Сообщение для пересылки: ссылка и команда, «в боте» — ссылкой на бота."""
    bot = f'<a href="https://t.me/{bot_username}">в боте</a>' if bot_username else "в боте"
    return (f"Твоё приглашение для устройства «{_e(device_name)}» 👇\n"
            f"{_invite_link(code, bot_username)}\n"
            f"или {bot}: <code>/code {code}</code>")


def friend_invite_plain(device_name: str, code: str, bot_username: str) -> str:
    """То же без разметки — для кнопок «📋 Скопировать» и «📤 Отправить».
    Текст копирования — не длиннее 256 знаков (лимит Telegram), поэтому имя
    устройства режется, а не код в конце."""
    name = device_name if len(device_name) <= 40 else device_name[:39] + "…"
    return (f"Твоё приглашение для устройства «{name}»: "
            f"{_invite_link(code, bot_username)} "
            f"или в Telegram-боте @{bot_username}: /code {code}")


def finish_friend_invite(device_name: str) -> str:
    return f"☝️ Отправь приглашение другу — он активирует и получит устройство «{_e(device_name)}»"


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления о переданных устройствах (как были)
# ─────────────────────────────────────────────────────────────────────────────

def lent_device_deleted_by_holder_notice(dev, used: int, limit: int) -> str:
    """Владельцу: держатель удалил переданное устройство; второй строкой —
    сколько ещё можно добавить (без лимита или без места — строки нет)."""
    head = (f"🗑 Профиль {holder_link(dev)} удалил у себя устройство «{_e(dev.name)}», "
            "которое ты ему передал")
    free = limit - used
    if not limit or free <= 0:
        return head
    return f"{head}\nМесто освободилось: можно добавить ещё {free}"


def lent_device_deleted_by_admin_notice(dev) -> str:
    return (f"🗑 Администратор удалил устройство «{_e(dev.name)}», которым ты пользовался, — "
            "VPN на нём больше не работает")


def lent_device_reassigned_notice(name: str) -> str:
    return (f"🔀 Устройство «{_e(name)}», которым ты пользовался, администратор перенёс "
            "в другой профиль — VPN на нём у тебя больше не работает")


def lent_device_deleted_by_owner_notice(dev) -> str:
    return (f"🗑 Профиль {owner_link(dev)} удалил устройство «{_e(dev.name)}», "
            "которым ты пользовался, — VPN на нём больше не работает")


def _names_list(names: list) -> str:
    q = [f"«{_e(n)}»" for n in names]
    if len(q) <= 1:
        return "".join(q)
    return ", ".join(q[:-1]) + " и " + q[-1]


def friend_device_added(dev, donor, n_held: int, *, guest: bool = False) -> str:
    """«✅ Устройство «X» от профиля [Петя] добавлено»; гостю с несколькими —
    второй строкой счёт (у клиента его говорит главная, которая идёт следом)."""
    head = f"✅ Устройство «{_e(dev.name)}» от профиля {client_link(donor)} добавлено"
    if guest and n_held > 1:
        return head + f".\nТеперь у тебя {_n_devices(n_held)}"
    return head


def friend_other_donor_refusal(held: list, donor) -> str:
    """Отказ по правилу одного дарителя: что и почему, затем шаг."""
    names = [d.name for d in held]
    one = len(names) == 1
    link = client_link(donor)
    return (f"⛔ Это приглашение от другого профиля, а у тебя уже есть "
            f"{'устройство' if one else 'устройства'} {_names_list(names)} от профиля {link}. "
            "Устройства можно получать только от одного профиля.\n"
            f"Хочешь принять новое — удали {'устройство' if one else 'все устройства'} "
            f"от профиля {link} и открой приглашение ещё раз")


def guest_upgraded(donor, moved: list, limit: int) -> str:
    lines = [ACTIVATION_OK, "",
             f"Переданные тебе устройства от профиля {client_link(donor)} "
             "перенесены в твой профиль — перенастраивать ничего не нужно, они работают как раньше:"]
    lines += [f"• {_e(d.name)}" for d in moved]
    if limit and len(moved) > limit:
        lines += ["", f"В твою подписку входит {_n_devices(limit)}, а перенесено {len(moved)} — "
                      f"работают все. Чтобы добавить новое, удали {len(moved) - limit + 1}"]
    return "\n".join(lines)


def guest_upgraded_donor_notice(moved: list, holder, used: int, limit: int) -> str:
    names = [d.name for d in moved]
    one = len(names) == 1
    now = f"У тебя теперь {used} из {limit} устройств" if limit else f"У тебя теперь {_n_devices(used)}"
    return (f"📤 У профиля {client_link(holder)} теперь своя подписка — "
            f"{'устройство' if one else 'устройства'} {_names_list(names)} "
            f"{'перешло' if one else 'перешли'} в его профиль. {now}")


def guest_upgraded_admin_tail(donor, moved: list, limit: int) -> str:
    return (f"\nПеренесено переданных устройств: {len(moved)} (от {_e(donor.name)}), "
            f"лимит подписки {limit if limit else 'без ограничения'}")


FRIEND_ALREADY_USER = (
    "Ты администратор — принимать чужие устройства незачем: все устройства "
    "сервера и так под твоим управлением 🙂"
)


def friend_activated(device_name: str) -> str:
    """Первый экран гостя после активации — сразу выбор платформы, как у клиента."""
    return f"🎉 Тебе передали устройство «{_e(device_name)}». Какое у тебя устройство?"


def friend_activated_host_notice(device_name: str, who: str) -> str:
    """Владельцу: who — имя принявшего ссылкой на его Telegram (tg_link)."""
    return f"👤 {who} принял приглашение — устройство «{_e(device_name)}» теперь у него"


# ── пояснения к выдаче (в одном сообщении с содержимым) ──────────────────────

def finish_link(name: str) -> str:
    return (f"☝️ Ссылка для устройства «{_e(name)}» — нажми на неё, чтобы скопировать, "
            "и вставь в AmneziaVPN")


def finish_qr(name: str) -> str:
    return (f"🔳 QR для устройства «{_e(name)}» — в AmneziaVPN «＋» → «Создать из QR-кода» "
            "и наведи камеру на этот код")


def finish_file(name: str) -> str:
    return f"📄 Файл для устройства «{_e(name)}» — открой его в AmneziaVPN"
