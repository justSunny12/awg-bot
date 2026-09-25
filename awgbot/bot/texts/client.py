"""Экраны клиента и гостя: главный экран, подписка, пауза, отсрочка, устройства, друзья."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.core.enums import SubStatus, ActivationStatus, FriendStatus

from .fmt import (
    _e, human_bytes, used_of_limit, gb_str, client_total_line, device_label,
    device_line, client_link, owner_link, holder_link, _n_devices, plural_ru,
    _days_word, _days)
from .routing import routing_status_line


# ─────────────────────────────────────────────────────────────────────────────
# Срок подписки
# ─────────────────────────────────────────────────────────────────────────────

def subscription_block(client, *, for_admin: bool = False, show_pause: bool = True) -> str:
    """Блок срока: период + остаток. Учитывает приостановку (самоблок клиента и
    админский блок с паузой). Тихий (silent) админ-блок пользователю не виден —
    для него период/статус как будто ничего не произошло.
    for_admin=True — админ видит всё (включая silent-паузу и temp-бессрочность).
    show_pause=False — скрыть счётчик дней приостановки (друг ей не управляет)."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    # видит ли ПОЛЬЗОВАТЕЛЬ эту паузу: самоблок — всегда; админская — только если
    # блок не тихий (есть видимый ADMIN_NOTIFIED). Админу видно всегда.
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)

    if not client.period_end:
        # бессрочно — либо реально, либо temp (admin_open). Пользователю при
        # silent-паузе показываем как обычную активную бессрочную «легенду»? Нет:
        # admin_open зануляет period_end. Если пауза пользователю не видна, покажем
        # сохранённый конец как обычный период.
        if paused and mode == "admin_open" and not pause_visible and client.pause_saved_end:
            start = timeutil.parse_iso(client.period_start) if client.period_start else None
            end = timeutil.parse_iso(client.pause_saved_end)
            status = "🟢 активна"
            body = f"Период подписки: {timeutil.fmt_period(start, end)}" if start else \
                   f"Период подписки: до {timeutil.fmt_dt(end)}"
            return f"Статус подписки: {status}\n{body}\nДо истечения: {timeutil.fmt_remaining(end)}"
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        if pause_visible and mode == "admin_open":
            status = "⏸ приостановлено администратором"
            return (f"Статус подписки: {status}\n"
                    "Период подписки: временно бессрочный "
                    "(пересчитается при снятии блокировки)")
        return f"Статус подписки: {status}\nПериод подписки: бессрочно"

    if not client.period_start:
        # аномалия данных: period_end есть, period_start — нет (не должно
        # случаться при нормальной работе, но не показываем голый прочерк)
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        return f"Статус подписки: {status}\nПериод подписки: дата начала не определена"
    start = timeutil.parse_iso(client.period_start)
    end = timeutil.parse_iso(client.period_end)

    if pause_visible:
        if mode == "user":
            status = "⏸ приостановлено пользователем"
        else:
            status = "⏸ приостановлено администратором"
    else:
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"

    lines = [f"Статус подписки: {status}"]
    period_line = f"Период подписки: {timeutil.fmt_period(start, end)}"
    if pause_visible and mode == "user":
        period_line += f" (+ до {int(client.pause_reserved_days)} дней приостановки)"
    elif pause_visible and mode == "admin_fixed":
        period_line += " (пересчитается при снятии блокировки)"
    lines.append(period_line)
    if not pause_visible:
        lines.append(f"До истечения: {timeutil.fmt_remaining(end)}")
    # счёт дней паузы против максимума типа (год ×2, месяц ×12); без срока —
    # дни не сгорают. Другу не показываем — паузой управляет владелец, другу
    # счётчик бесполезен.
    if show_pause and end:
        bal = int(client.pause_balance_days)
        kind = str(client.period_kind or "")
        if kind == "year":
            of = f"/{2 * settings.get_int('pause.pause_max_total_days', 28)}"
        elif kind == "month":
            of = f"/{12 * settings.get_int('pause.monthly_pause_days', 2)}"
        else:
            of = ""          # день/неделя: не копят, максимум — чужой, не показываем
        word = "дней" if of else _days_word(bal)
        lines.append(f"Приостановка: доступно {bal}{of} {word}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Карточка клиента (для админа и для самого клиента)
# ─────────────────────────────────────────────────────────────────────────────


def _guest_consumption(held, donor) -> str:
    """Потребление по каждому удерживаемому устройству против его лимита —
    своего, иначе профиля владельца; ни того ни другого — «без ограничений».
    Одно устройство — в строку, несколько — списком."""
    rows = []
    for d in held:
        used = int(d.traffic_rx_month) + int(d.traffic_tx_month)
        limit = int(d.traffic_limit) or int(donor.traffic_limit)
        if limit:
            rows.append(f"{used_of_limit(used, limit)} ({_e(d.name)})")
        else:
            rows.append(f"{human_bytes(used)} ({_e(d.name)}, без ограничений)")
    if len(rows) == 1:
        return f"Потребление за месяц: {rows[0]}"
    return "Потребление за месяц:\n" + "\n".join(f"• {r}" for r in rows)


def greeting_guest(name: str, server_ok: bool, donor, held, routing_ok: bool = None) -> str:
    """Главный экран гостя (концепт «гость»): как клиентский, подписка —
    владельца (без срока: это его дело), потребление — по удерживаемым
    устройствам, устройств — сколько держит. name — имя гостя из Telegram
    (client.tg_name, иначе профильное)."""
    status_block = server_status_client(server_ok)
    if routing_ok is not None:
        status_block += "\n" + routing_status_line(routing_ok)
    held = list(held)
    if donor is None or not held:
        # устройств нет — профиль живёт (список адресов и история при нём),
        # подписки показывать нечьей
        return (f"Привет, {_e(name)}! 👋\n\n{status_block}\n\n{GUEST_NO_DEVICES_LEFT}")
    owner = f" (владелец: {client_link(donor)})"
    return (f"Привет, {_e(name)}! 👋\n\n"
            f"{status_block}\n\n"
            f"Статус подписки: {subscription_status_only(donor)}{owner}\n\n"
            f"{_guest_consumption(held, donor)}\n\n"
            f"У тебя {_n_devices(len(held))}")


def held_devices_tail(held) -> str:
    """Хвост «+ 1 от [Вася]» к строке устройств обычного клиента, который держит
    чужие; пусто — не держит."""
    if not held:
        return ""
    d = held[0]
    return f" (+ {len(held)} от {owner_link(d)})"


def _device_limit_line(dev) -> str:
    """Хвост потребления у переданного устройства: чей лимит его ограничивает."""
    if dev.traffic_limit:
        return f"лимит устройства {gb_str(dev.traffic_limit)}"
    return ""


def held_device_card(dev, owner_limit_bytes: int) -> str:
    """Карточка переданного устройства у ДЕРЖАТЕЛЯ: строка, потребление с
    указанием, чей лимит, «получено от»; причины блокировки — как у клиента."""
    from awgbot.core import blocks
    used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
    mask = int(dev.block_reason)
    if dev.traffic_limit:
        line = used_of_limit(used, dev.traffic_limit, "лимит устройства")
    elif owner_limit_bytes:
        line = used_of_limit(used, owner_limit_bytes)         # лимит профиля владельца
    else:
        line = used_of_limit(used, 0)
    parts = [device_line(dev), f"Потребление за месяц: {line}"]
    reasons = blocks.device_reasons_ru(mask, for_admin=False)
    if reasons:
        parts.append("⛔ Заблокировано: " + ", ".join(reasons))
    parts.append(f"\n👤 Получено от {owner_link(dev)}")
    return "\n".join(parts)


def lent_out_marker(dev) -> str:
    """Строка в карточке владельца: кому передано."""
    return f"👤 Передано {holder_link(dev)} и управляется им"


def device_delete_by_holder_ask(name: str) -> str:
    return (f"Удалить «{_e(name)}»? Это устройство, переданное другом: после удаления "
            "доступ с него пропадёт, а создать новое ты не сможешь — только получить "
            "новый код от друга.")


def device_delete_by_owner_ask(dev) -> str:
    return (f"Удалить «{_e(dev.name)}»? Устройство передано "
            f"{holder_link(dev)}: у него пропадёт доступ с этого "
            "устройства, а создать новое сам он не сможет — только получить от тебя новый код.")


def lent_device_deleted_by_holder_notice(dev, used: int, limit: int) -> str:
    """Владельцу: держатель удалил переданное устройство."""
    now = (f"Теперь у тебя {used} из {limit} устройств" if limit
           else f"Теперь у тебя {_n_devices(used)}")
    return (f"Устройство «{_e(dev.name)}», ранее переданное "
            f"{holder_link(dev)}, удалено по его запросу.\n{now}.")


def lent_device_deleted_by_admin_notice(dev) -> str:
    """Держателю: переданное ему устройство удалил администратор."""
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено администратором — "
            "доступ по нему больше не работает.")


def lent_device_reassigned_notice(name: str) -> str:
    """Держателю: администратор перенёс устройство в другой профиль — ключи
    перевыпущены, прежний конфиг не работает."""
    return (f"Устройство «{_e(name)}», которым ты управлял, перенесено в другой профиль "
            "администратором — доступ по нему у тебя больше не работает.")


def lent_device_deleted_by_owner_notice(dev) -> str:
    """Держателю: владелец удалил переданное ему устройство."""
    return (f"Устройство «{_e(dev.name)}», которым ты управлял, удалено владельцем "
            f"({owner_link(dev)}) — доступ по нему больше не работает.")


def _names_list(names: list) -> str:
    """«A», «B» и «C» — через запятую, перед последним «и»."""
    q = [f"«{_e(n)}»" for n in names]
    if len(q) <= 1:
        return "".join(q)
    return ", ".join(q[:-1]) + " и " + q[-1]


def friend_device_added(dev, donor, n_held: int, own_slots: tuple | None = None) -> str:
    """Держателю: ещё одно устройство от того же владельца. Вторая строка —
    только когда устройств стало больше одного; у обычного клиента — со
    своими: «2 из 3 устройств + 1 от [Вася]»."""
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
    """Код от другого владельца при уже удерживаемых устройствах."""
    names = [d.name for d in held]
    one = len(names) == 1
    link = client_link(donor)
    return (f"У тебя уже есть {'устройство' if one else 'устройства'} {_names_list(names)}, "
            f"{'переданное' if one else 'переданные'} {link}.\n"
            "Владеть устройствами от разных друзей одновременно не получится 😔\n"
            f"Ты можешь либо удалить {'устройство' if one else 'все устройства'} от {link} и "
            "отправить мне этот код повторно, либо оставить всё как есть — решать тебе 🤷‍♂️")


def guest_upgraded(donor, moved: list, limit: int) -> str:
    """Гость стал владельцем: что перенесено; сверх лимита — честно."""
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
    """Прежнему владельцу — одним сообщением про все уехавшие."""
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


GUEST_NO_DEVICES_LEFT = ("Устройств больше нет. Чтобы снова пользоваться VPN, попроси у "
                         "друга новый код.")


def block_device_ask(name: str) -> str:
    return (f"Заблокировать «{_e(name)}»? Устройство перестанет подключаться, пока ты "
            "его не разблокируешь.")


# Единственный, кому код друга не даётся, — администратор: все устройства
# сервера и так под его управлением.
FRIEND_ALREADY_USER = (
    "Ты администратор — принимать чужие устройства незачем: все устройства "
    "сервера и так под твоим управлением 🙂"
)


def friend_activated(device_name: str) -> str:
    return (f"Готово! Тебе передали устройство «{_e(device_name)}» 🎉\n"
            "Ниже — панель управления им.")


def friend_activated_host_notice(device_name: str, who: str) -> str:
    return f"👤 Друг ({_e(who)}) активировал устройство «{_e(device_name)}»."


# Контекстные «завершители» под выданным контентом (баббл с кнопкой «В меню»).
def finish_link(name: str) -> str:
    return (f"☝️ Вот, держи — ссылка для подключения твоего устройства "
            f"«{_e(name)}». Вставь её в приложение AmneziaVPN.")


def finish_qr(name: str) -> str:
    return (f"☝️ Вот, держи — QR-код для твоего устройства «{_e(name)}».\n"
            "В AmneziaVPN: «＋» → «Создать из QR-кода» и наведи камеру на "
            "анимацию.")


def finish_file(name: str) -> str:
    return (f"☝️ Вот, держи — файл настроек для твоего устройства «{_e(name)}». "
            "Импортируй его в приложение AmneziaVPN.")


def finish_config(kind: str, name: str) -> str:
    """Завершитель под выданным конфигом по виду выдачи: link | qr | file."""
    return {"link": finish_link, "qr": finish_qr, "file": finish_file}[kind](name)


CONNECT_METHOD_ASK = "Как планируешь подключить устройство?"
FINISH_CLIENT_INVITE = (
    "☝️ Выше — ссылка-приглашение. Перешли её человеку, чтобы он активировал доступ.\n\n"
    "❗️ После возврата в меню это сообщение исчезнет — повторно сгенерировать его "
    "будет можно из профиля клиента, до момента принятия приглашения. Уже "
    "пересланное сообщение останется рабочим."
)
FINISH_FRIEND_INVITE = "☝️ Выше — приглашение для друга. Перешли его — друг активирует и получит своё устройство."


ADD_FOR_WHOM = (
    "Для кого создаём устройство?\n\n"
    "<b>\U0001F4F1 Себе</b> — получишь данные для подключения прямо сейчас.\n"
    "<b>\U0001F464 Другу</b> — сгенерирую приглашение в бота. Друг активирует его "
    "и сможет <b>сам</b> получать данные для подключения здесь, в боте — "
    "тебе не придётся пересылать их ему вручную."
)


def friend_invite_message(device_name: str, code: str, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start={code}"
    return (f"Приглашение на устройство «{_e(device_name)}» готово 👇\n"
            "Перешли другу — он активирует и получит управление этим устройством:\n\n"
            f"{link}\n\n"
            f"Или пусть отправит боту: <code>/code {code}</code>")


def friend_marker(dev) -> str:
    if dev.is_lent:
        return f"👤 Передано {holder_link(dev)}"
    if dev.friend_status == FriendStatus.PENDING:
        return "⏳ Приглашение другу ждёт активации"
    return ""


TRANSFER_FRIEND_WARNING = (
    "<blockquote>Передавая устройство другу, ты отдаёшь ему это подключение. "
    "Пользоваться одним подключением с нескольких устройств одновременно "
    "нормально не выйдет — каждому нужна своя ссылка.\n"
    "Если сам пользуешься этим устройством — сначала заведи себе новое.</blockquote>\n"
    "Передать устройство «{name}» другу?"
)


def client_card(client, devices, traffic, online: bool, *, for_admin: bool,
                rf: tuple[int, int] | None = None) -> str:
    """Полная карточка: имя, подписка, онлайн, потребление, устройства. rf —
    РФ-часть под потреблением: только админу и только когда профилю положена
    (services.client_card_data решает)."""
    head = f"👤 {_e(client.name)}"
    if for_admin and client.activation_status == ActivationStatus.PENDING:
        head += "  ⏳ ждёт активации"
    online_line = "Сейчас: " + ("🟢 онлайн" if online else "🔴 оффлайн")

    sub = subscription_block(client, for_admin=for_admin)

    # потребление за месяц: клиенту — сумма, админу — с разбивкой ↑↓; тотал-лимит
    tr = client_total_line(
        traffic["rx_month"], traffic["tx_month"],
        client.traffic_limit, client.bonus_bytes, for_admin=for_admin)
    if for_admin and rf is not None:
        from .fmt import rf_line
        tr += "\n" + rf_line(*rf)

    lim = client.device_limit
    limit_line = (f"Устройств: {len(devices)} (без ограничения)" if lim == 0
                  else f"Устройств: {len(devices)} из {lim}")

    dev_block = "\n".join("  " + device_label(d, for_admin=for_admin) for d in devices)

    limit_and_devs = f"{limit_line}\n{dev_block}" if dev_block else limit_line
    parts = [f"{head}\n{online_line}", sub, tr, limit_and_devs]
    # причины блокировки клиента (тихий админ-блок пользователю не виден)
    from awgbot.core import blocks
    reasons = blocks.client_reasons_ru(int(client.block_reason), for_admin=for_admin)
    if reasons:
        parts.insert(1, "⛔ Заблокирован: " + ", ".join(reasons))
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Приветствие / активация
# ─────────────────────────────────────────────────────────────────────────────


def _pause_visibility(client, *, for_admin: bool = False) -> tuple:
    """(paused, mode, pause_visible) — видит ли ПОЛЬЗОВАТЕЛЬ текущую паузу:
    самоблок — всегда; админская — только если блок не тихий. Админу видно
    всегда. Общая логика для subscription_status_only/subscription_manage_text
    и (частично) subscription_block."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)
    return paused, mode, pause_visible


_KIND_LABELS = {"day": "на день", "week": "на неделю", "month": "ежемесячная",
                "year": "годовая", "never": "бессрочная"}


def subscription_kind_label(kind) -> str:
    return _KIND_LABELS.get(str(kind or ""), str(kind or "—"))


def _pause_kind_ru(kind: str) -> str:
    return "годовой" if kind == "year" else "ежемесячной"


def pause_credit_line(pc) -> str:
    """Владельцу при продлении — что стало со счётом паузы (вторая строка к
    «Подписка продлена до …»). Типы, которые не копят, — пусто."""
    if pc is None or pc.kind not in ("year", "month"):
        return ""
    if pc.reason == "expired":
        return ("⏸ Дни паузы за этот период не начислены: подписка продлена после истечения. "
                f"Доступно {_days(pc.after)}.")
    if pc.reason == "grace":
        return ("⏸ Дни паузы за этот период не начислены: в прошлом периоде использована "
                f"отсрочка. Доступно {_days(pc.after)}.")
    if pc.reason == "cap":
        return ("⏸ Дни паузы не добавлены: достигнуто максимальное количество для "
                f"{_pause_kind_ru(pc.kind)} подписки ({pc.cap}).")
    full = (settings.get_int("pause.pause_max_total_days", 28) if pc.kind == "year"
            else settings.get_int("pause.monthly_pause_days", 2))
    partial = pc.added < full
    note = ("" if not partial
            else " (максимум)" if pc.kind == "year"
            else " (максимум для ежемесячной подписки)")
    return f"⏸ Дней паузы добавлено: +{pc.added}, доступно {pc.after}{note}."


def pause_credit_admin(pc) -> str:
    """Админу в финишер продления — то же коротко."""
    if pc is None or pc.kind not in ("year", "month"):
        return ""
    if pc.reason == "expired":
        return f"Дней паузы: не начислены — после истечения, доступно {pc.after}"
    if pc.reason == "grace":
        return f"Дней паузы: не начислены — отсрочка, доступно {pc.after}"
    if pc.reason == "cap":
        return f"Дней паузы: не добавлены — максимум {_pause_kind_ru(pc.kind)} ({pc.cap})"
    return f"Дней паузы: +{pc.added} → {pc.after}" + (" (максимум)" if pc.after == pc.cap else "")


def pause_balance_line(client) -> str:
    """«Приостановка подписки: доступно N дней» + как счёт пополняется — всем,
    кроме бессрочных (им останавливать нечего): и тем, у кого дней нет — пусть
    видят, за что их дают."""
    if not client.effective_period_end:
        return ""
    bal = int(client.pause_balance_days)
    year_days = settings.get_int("pause.pause_max_total_days", 28)
    month_days = settings.get_int("pause.monthly_pause_days", 2)
    return (f"<b>Приостановка подписки:</b> доступно {bal} {_days_word(bal)}\n"
            f"<i>+{month_days} {_days_word(month_days)} за каждое своевременное продление "
            f"на месяц, не более {12 * month_days}</i>\n"
            f"<i>+{year_days} {_days_word(year_days)} за продление на год, "
            f"не более {2 * year_days}</i>")


def subscription_manage_text(client, *, routing_visible: bool) -> str:
    """Экран «Управлять подпиской» / «Моя подписка»: тип, статус, РФ-доступ,
    период и остаток срока, счёт дней паузы, лимиты. Потребление — на главной,
    список устройств — в «Мои устройства»."""
    paused, mode, pause_visible = _pause_visibility(client)
    status = "⏳ приостановлена" if pause_visible else subscription_status_only(client, expiring=True)
    lines = ["<b>Информация о подписке:</b>", "",
             f"Тип подписки: {subscription_kind_label(client.period_kind)}",
             f"Статус: {status}",
             f"РФ-доступ: {'🟢 доступен' if routing_visible else '🔴 не доступен'}"]
    start = timeutil.parse_iso(client.period_start) if client.period_start else None
    end_iso = client.effective_period_end
    if end_iso:
        end = timeutil.parse_iso(end_iso)
        period = timeutil.fmt_period(start, end) if start else f"до {timeutil.fmt_dt(end)}"
        lines.append(f"Период подписки: {period}")
        if not pause_visible:
            lines.append(f"До истечения: {timeutil.fmt_remaining(end)}")
    else:
        lines.append("Период подписки: " + (f"с {timeutil.fmt_dt(start)} — бессрочно" if start
                                             else "бессрочно"))
    pause = pause_balance_line(client)
    if pause:
        lines += ["", pause]
    lines += ["",
              "Лимит потребления в месяц: " + (gb_str(client.traffic_limit) if client.traffic_limit
                                               else "без ограничения"),
              "Лимит устройств: " + (str(client.device_limit) if client.device_limit
                                     else "без ограничения")]
    return "\n".join(lines)


def subscription_status_only(client, *, expiring: bool = False) -> str:
    """Только статус подписки (без периода/дат — те в «Управлять подпиской»).
    Для лёгкого инфобокса главного меню клиента. expiring — показывать
    «🟠 истекает DD.MM HH:MM» с момента, когда человеку ушло первое «истекает
    через…» (пороги в notified_thresholds); гостю про срок дарителя не говорим."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸ приостановлено пользователем" if mode == "user" else "⏸ приостановлено администратором"
    if client.status != SubStatus.ACTIVE:
        return "🔴 истекла"
    if expiring and client.period_end and client.notified_thresholds:
        end = timeutil.parse_iso(client.period_end).astimezone(timeutil.TZ)
        return f"🟠 истекает {end.strftime('%d.%m %H:%M')}"
    return "🟢 активна"


def server_status_client(ok: bool) -> str:
    return "🟢 VPN-сервер работает нормально" if ok else "🔴 VPN-сервер не отвечает"


def greeting_client(client, server_ok: bool, slots: tuple[int, int] = None,
                    routing_ok: bool = None, held=(), traffic: dict | None = None) -> str:
    """Инфобокс главного меню клиента: приветствие, статус сервера (отдельным
    абзацем сразу после приветствия — пустая строка с обеих сторон), статус
    подписки (только статус — период/даты в «Управлять подпиской»; «истекает
    DD.MM HH:MM» — с первого напоминания), потребление за месяц против лимита
    (traffic — rx_month/tx_month профиля), максимум устройств и текущее
    количество.

    routing_ok=None — строки о РФ-шлюзе нет вовсе: админ функцию не разрешил,
    и рассказывать про механизм тому, кому он недоступен, — шум. Разрешил —
    строка есть всегда, и в исправном состоянии тоже: она отвечает на вопрос
    «работает ли», который иначе задаётся заходом в раздел."""
    status_block = server_status_client(server_ok)
    if routing_ok is not None:
        status_block += "\n" + routing_status_line(routing_ok)
    text = (f"Привет, {_e(client.name)}! 👋\n\n"
           f"{status_block}\n\n"
           f"Статус подписки: {subscription_status_only(client, expiring=True)}")
    if traffic is not None:
        text += "\n\n" + client_total_line(traffic["rx_month"], traffic["tx_month"],
                                            client.traffic_limit, client.bonus_bytes,
                                            for_admin=False)
    if slots is not None:
        used, limit = slots
        tail = held_devices_tail(held)             # «+ 1 от [Вася]» — чужие, которые держит
        if limit == 0:                             # безлимит
            text += f"\n\nУстройств добавлено: {used} (без ограничения){tail}."
        elif used == 0 and not tail:
            text += f"\n\nВсего можно добавить до {limit} {plural_ru(limit, 'устройства', 'устройств', 'устройств')}. Пока не добавлено ни одного."
        else:
            text += f"\n\nУстройств добавлено: {used} из {limit}{tail}."
    return text


def device_deleted(name: str, used: int, limit: int) -> str:
    """Финишер после удаления устройства клиентом: что удалено и сколько теперь
    можно добавить. Остаётся в чате, меню приходит следом."""
    head = f"🗑 Устройство «{_e(name)}» удалено."
    free = limit - used
    if limit == 0 or free <= 0:
        return head
    return head + f" Теперь можно добавить до {free} {plural_ru(free, 'устройства', 'устройств', 'устройств')}."


def device_slots_line(used: int, limit: int) -> str:
    if limit == 0:                                 # безлимит
        return f"Устройств добавлено: {used}. Можно добавлять без ограничения."
    if used == 0:
        return f"Всего можно добавить до {limit} {plural_ru(limit, 'устройства', 'устройств', 'устройств')}. Пока не добавлено ни одного."
    if used >= limit:
        return (f"Устройств добавлено: {used} из {limit}. Лимит исчерпан — "
                f"чтобы добавить новое, сначала удали одно из существующих.")
    return f"Устройств добавлено: {used} из {limit}. Можно добавить ещё {limit - used}."


DELETE_ONLY_DEVICE_WARNING = (
    "⚠️ Это твоё <b>единственное</b> устройство.\n\n"
    "Если удалить — VPN сразу перестанет работать. И, если прямо сейчас ты "
    "пользуешься Telegram только через этот VPN, ты потеряешь доступ и к боту — "
    "и не сможешь подключиться заново сам.\n\n"
    "Точно удалить?"
)

DELETE_DEVICE_CONFIRM = (
    "<blockquote>При удалении устройства его ссылка для подключения станет "
    "неактивной, и VPN по ней работать перестанет!\n"
    "Если потом захочешь добавить его снова — ссылка будет новая.</blockquote>\n"
    "Удалить устройство «{name}»?"
)


HELP_INTRO = "Нужна помощь с настройкой? Выбери своё устройство:"

UNMANAGED_DEVICE_EXPLAIN = (
    "\n\n<blockquote>Это устройство добавлял не бот — оно появилось в конфиге "
    "сервера само. Приватный ключ WireGuard хранится только на самом устройстве, "
    "поэтому выдать ссылку, файл или QR бот не может, и передать его другу — "
    "тоже.\n"
    "Удали его и добавь новое через бота: тогда всё это станет доступно.</blockquote>"
)

UNMANAGED_DEVICE_DIALOG = (
    "Не помню, чтобы это устройство добавлялось через меня 😳\n"
    "Значит, пир прописали в конфиг сервера мимо бота. Ссылки для него у меня "
    "нет и взять её неоткуда.\n"
    "Удали это устройство и добавь новое через бота."
)


def limit_changed_notice(old: int, new: int) -> str:
    def _fmt(v):
        return "без ограничения" if v == 0 else str(v)
    return f"Максимальное количество устройств для тебя изменено: {_fmt(old)} → {_fmt(new)}."


INVITE_FORWARD_TEMPLATE = (
    "Привет!\n"
    "Тебе одобрен доступ в свободный интернет 😊\n"
    "Для получения настроек переходи по ссылке и жми \"СТАРТ\" — расскажу, "
    "что делать дальше\n"
    "{link}"
)

ACTIVATION_OK = "Готово! Доступ активирован. 🎉"
ACTIVATION_INVALID = (
    "Не помню такого кода в списках, что-то ты путаешь...\n"
    "Как найдёшь правильный код — пиши, пообщаемся 🙂"
)
ACTIVATION_ALREADY = "У тебя уже есть доступ."

COLD_START_GREETING = (
    "Привет!\n"
    "Ой, что-то я тебя не припоминаю 😳\n"
    "Если ты перешёл по ссылке-приглашению, но код не подхватился (так бывает — "
    "Telegram иногда не передаёт его с первого раза), просто отправь мне код "
    "командой <code>/code КОД</code> — сверю по спискам."
)

CODE_NO_ARG = "Отправь код после команды, вот так: <code>/code твой_код</code>"


# ── «Продли на пару недель» (самостоятельная отсрочка) ──────────────────────────

def grace_activated_client(days: int, end) -> str:
    return (f"🙏 Готово! Подписка продлена на {days} дн. — до {end}.\n"
            "Эти дни вычтутся из следующего продления.")

GRACE_STALE = "Это предложение уже неактуально."

def grace_activated_admin(name: str, days: int) -> str:
    return f"🙏 Профиль «{_e(name)}» активировал отсрочку на {days} дн."


# ── Приостановка: инфобоксы диалога ──────────────────────────────────────────

def pause_ask(available_days: int) -> str:
    """Инфобокс перед выбором длительности: сколько доступно и как считается."""
    return (f"⏸ Подписку можно приостановить максимум на "
            f"{available_days} {_days_word(available_days)}.\n\n"
            "Пока подписка на паузе, её срок не тикает. Возобновить можно в любой "
            "момент. Тогда неиспользованные дни приостановки вернутся обратно — "
            "их можно будет использовать позже, а израсходуется только фактическое "
            "количество <i>начатых</i> дней паузы.\n\n"
            "На сколько дней приостановить?")


def pause_warning(days: int) -> str:
    """Предупреждение перед подтверждением: пауза отключает VPN, а выйти можно
    только через этот бот. Если Telegram доступен лишь через этот VPN — клиент
    рискует запереться. (Аварийный e-mail-выход добавит фича 2.)"""
    return ("⚠️ <b>Прежде, чем мы продолжим:</b>\n\n"
            "На время паузы VPN отключается. Выйти из приостановки досрочно можно "
            "только кнопкой «Возобновить» здесь, в этом боте.\n\n"
            "Если ты заходишь в Telegram <b>только через этот VPN</b>, то после "
            "постановки на паузу потеряешь доступ и к боту — и не сможешь снять "
            "паузу сам.\n\n"
            "Продолжить?")


def pause_emergency_code(code: str, address: str) -> str:
    """Аварийный код email-выхода — показывается после входа в паузу, если фича
    включена. Клиент, заперевшийся без Telegram, шлёт этот код письмом."""
    return (f"🆘 <b>Аварийный выход без Telegram</b>\n\n"
            f"Если потеряешь доступ к боту (Telegram только через этот VPN), "
            f"отправь письмо на <code>{_e(address)}</code>, указав в теме письма "
            f"только этот код:\n\n<code>{_e(code)}</code>\n\n"
            f"Доступ восстановится автоматически. Код одноразовый и действует, "
            f"пока активна приостановка.\n\n"
            f"<b>Сохрани код и e-mail в заметках на всякий случай.</b>")


def pause_entered_summary(until: str) -> str:
    """Итог входа в паузу — остаётся в чате как результат действия (промежуточные
    шаги стираются). until — дата авто-возобновления (DD.MM.YYYY HH:MM)."""
    return (f"Подписка приостановлена до {until}.\n"
            "При необходимости можно возобновить досрочно через бота.")

def pause_unavailable() -> str:
    """Лимит берём из конфига (не хардкод): при смене PAUSE_MAX_TOTAL_DAYS текст
    иначе называл бы пользователю неверную цифру. Функция (а не константа) —
    config импортируется лениво, как и в остальных динамических текстах модуля."""
    md = settings.get_int("pause.monthly_pause_days", 2)
    return ("Приостановка сейчас недоступна: на счету нет дней. Годовая подписка даёт "
            f"{settings.get_int('pause.pause_max_total_days', 28)} дней за период, "
            f"ежемесячная — по {md} {_days_word(md)} за каждое своевременное продление.")


def pause_limit_exhausted() -> str:
    """Годовая подписка, но доступных дней приостановки не осталось."""
    return "Дни приостановки на счету закончились — пополнится при продлении подписки."

def pause_resume_ask(actual: int, reserved: int) -> str:
    """Инфобокс подтверждения досрочного выхода из паузы — явно указываем,
    что спишутся ФАКТИЧЕСКИЕ дни, а не весь зарезервированный остаток."""
    if reserved:
        return (f"▶️ Возобновить сейчас? Будет использовано {actual} из "
                f"{reserved} зарезервированных дней приостановки — "
                f"неиспользованный остаток вернётся в подписку.")
    return f"▶️ Возобновить сейчас? Приостановка длилась {actual} дн."


def pause_resumed_self(actual_days: int, new_end) -> str:
    return (f"▶️ Подписка возобновлена. Использовано {actual_days} дн. паузы, "
            f"активна до {timeutil.fmt_dt(new_end)}.")
