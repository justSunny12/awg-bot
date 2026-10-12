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
            f"или в TG-боте (@{bot_username}): /code {code}")


def finish_friend_invite(device_name: str) -> str:
    return f"☝️ Отправь приглашение другу — он активирует и получит устройство «{_e(device_name)}»"


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления о переданных устройствах (как были)
# ─────────────────────────────────────────────────────────────────────────────

def lent_device_deleted_by_holder_notice(dev, used: int, limit: int) -> str:
    """Владельцу: держатель удалил переданное устройство."""
    now = (f"Теперь у тебя {used} из {limit} устройств" if limit
           else f"Теперь у тебя {_n_devices(used)}")
    return (f"Устройство «{_e(dev.name)}», ранее переданное "
            f"{holder_link(dev)}, удалено по его запросу.\n{now}.")


def lent_device_deleted_by_admin_notice(dev) -> str:
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено администратором — "
            "доступ по нему больше не работает.")


def lent_device_reassigned_notice(name: str) -> str:
    return (f"Устройство «{_e(name)}», которым ты управлял, перенесено в другой профиль "
            "администратором — доступ по нему у тебя больше не работает.")


def lent_device_deleted_by_owner_notice(dev) -> str:
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено владельцем "
            f"({owner_link(dev)}) — доступ по нему больше не работает.")


def _names_list(names: list) -> str:
    q = [f"«{_e(n)}»" for n in names]
    if len(q) <= 1:
        return "".join(q)
    return ", ".join(q[:-1]) + " и " + q[-1]


def friend_device_added(dev, donor, n_held: int, own_slots: tuple | None = None) -> str:
    head = (f"✅ Устройство «{_e(dev.name)}» от {client_link(donor)} "
            "успешно добавлено.")
    if own_slots is not None:
        used, limit = own_slots
        own = f"{used} из {limit} устройств" if limit else _n_devices(used)
        return head + f"\nТеперь у тебя {own} (+ {n_held} от {client_link(donor)})."
    if n_held > 1:
        return head + f"\nТеперь у тебя {_n_devices(n_held)}."
    return head


def friend_other_donor_refusal(held: list, donor) -> str:
    names = [d.name for d in held]
    one = len(names) == 1
    link = client_link(donor)
    return (f"У тебя уже есть {'устройство' if one else 'устройства'} {_names_list(names)}, "
            f"{'переданное' if one else 'переданные'} {link}.\n"
            "Владеть устройствами от разных друзей одновременно не получится 😔\n"
            f"Ты можешь либо удалить {'устройство' if one else 'все устройства'} от {link} и "
            "отправить мне этот код повторно, либо оставить всё как есть — решать тебе 🤷‍♂️")


def guest_upgraded(donor, moved: list, limit: int) -> str:
    lines = [ACTIVATION_OK, "",
             f"Переданные тебе устройства от профиля {client_link(donor)} "
             "перенесены в твой профиль — перенастраивать ничего не нужно, они работают как раньше:"]
    lines += [f"• {_e(d.name)}" for d in moved]
    if limit and len(moved) > limit:
        lines += ["", f"В твою подписку входит {_n_devices(limit)}, а перенесено {len(moved)} — "
                      "все они продолжают работать. Добавить новое получится, когда освободится "
                      "место в рамках лимита."]
    return "\n".join(lines)


def guest_upgraded_donor_notice(moved: list, holder, used: int, limit: int) -> str:
    names = [d.name for d in moved]
    one = len(names) == 1
    now = f"У тебя теперь {used} из {limit} устройств" if limit else f"У тебя теперь {_n_devices(used)}"
    return (f"📤 {'Устройство' if one else 'Устройства'} {_names_list(names)} "
            f"{'перешло' if one else 'перешли'} к {client_link(holder)} — он активировал "
            f"собственную подписку, и {'устройство переехало' if one else 'устройства переехали'} "
            f"в его профиль. {now}.")


def guest_upgraded_admin_tail(donor, moved: list, limit: int) -> str:
    return (f"\nПеренесено переданных устройств: {len(moved)} (от {_e(donor.name)}), "
            f"лимит подписки {limit if limit else 'без ограничения'}.")


FRIEND_ALREADY_USER = (
    "Ты администратор — принимать чужие устройства незачем: все устройства "
    "сервера и так под твоим управлением 🙂"
)


def friend_activated(device_name: str) -> str:
    return f"🎉 Тебе передали устройство «{_e(device_name)}»"


def friend_activated_host_notice(device_name: str, who: str) -> str:
    return f"👤 Друг ({_e(who)}) активировал устройство «{_e(device_name)}»."


# ── пояснения к выдаче (в одном сообщении с содержимым) ──────────────────────

def finish_link(name: str) -> str:
    return (f"☝️ Ссылка для {_e(name)} — нажми на неё, чтобы скопировать, "
            "и вставь в AmneziaVPN")


def finish_qr(name: str) -> str:
    return f"🔳 Для {_e(name)} — в AmneziaVPN «＋» → «Создать из QR-кода», наведи камеру"


def finish_file(name: str) -> str:
    return f"📄 Для {_e(name)} — импортируй файл в AmneziaVPN"
