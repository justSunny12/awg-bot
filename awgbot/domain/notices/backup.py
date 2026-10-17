"""backup.py — часть пакета notices (см. __init__): тексты резервной копии
сервера AWG — подпись копии и письмо, уведомление о пропущенной плановой
копии, план восстановления (вопрос в чате и тот же текст в терминале без
HTML), отчёт после восстановления, выход шлюзов на связь.

Имена шлюзов — «имя» (подпись), имя — ссылкой на карточку слота, когда
слот есть на сервере; даты — дд.мм.гг; уведомления без точки в конце."""

from __future__ import annotations

import html
import re

from awgbot.util import timeutil

RESTORE_WAIT_MIN = 15          # сколько ждать выхода шлюза на связь после восстановления


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def gw_ref(name: str, label: str = "", *, slot: int = 0, bot_username: str = "") -> str:
    """«Шлюз 2» (дача): имя — ссылкой на карточку слота, если она есть."""
    shown = _e(name) or (f"шлюз {slot}" if slot else "шлюз")
    if slot and bot_username:
        shown = f'<a href="https://t.me/{bot_username}?start=gw-{int(slot)}">{shown}</a>'
    return f"«{shown}»" + (f" ({_e(label)})" if label else "")


def plain(text: str) -> str:
    """Тот же текст для терминала: без тегов, с раскрытыми сущностями."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _date(iso: str) -> str:
    try:
        return timeutil.parse_iso(iso).astimezone(timeutil.TZ).strftime("%d.%m.%y")
    except (ValueError, TypeError):
        return "?"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_dt_ui(timeutil.parse_iso(iso), year=True)
    except (ValueError, TypeError):
        return "?"


# ── раздел «💾 Бэкапы» ───────────────────────────────────────────────────────

GATEWAYS_IN_BACKUP = "🛰 Конфигурации шлюзов тоже попадают в бэкап: параметры и ключи линков, настройки ботов"


def no_phrase_line(has_slots: bool) -> str:
    keys = "ключи линков шлюзов" if has_slots else "ключи устройств"
    return f"⚠️ Без ключевой фразы бэкап не сделать: в нём {keys} — задай фразу в «🔐 Шифрование»"


def slot_backup_line(ref: str, state: str, seen_iso: str = "", keep_at: str = "") -> str:
    """Строка слота в разделе — только когда есть что сказать; state —
    offline | outdated | waiting (gwchannel: gwkeep_line_state)."""
    if state == "offline":
        return f"🟡 {ref} не на связи с {_date(seen_iso)} — в бэкапе его настройки на {_date(keep_at)}"
    if state == "outdated":
        return f"⚠️ {ref}: настройки бота шлюза не сохраняются в бэкап — обнови агент шлюза"
    if state == "waiting":
        return f"🟡 {ref}: настройки бота шлюза ещё не приходили — придут, когда шлюз выйдет на связь"
    return ""


def missed_backup_notice(has_slots: bool) -> str:
    keys = "ключи линков шлюзов" if has_slots else "ключи устройств"
    return (f"⚠️ Плановый бэкап сервера AWG не сделан: ключевая фраза шифрования не задана, а в бэкапе "
            f"{keys}. Задай фразу в «💾 Бэкапы» → «🔐 Шифрование»")


REKEY_WAIT_TOAST = "Идёт смена ключей линка {name} — бэкап сниму сразу после неё"


# ── подпись копии и письмо ───────────────────────────────────────────────────

_EVENT_TAIL = {
    "rekey": "после смены ключей линка {ref}",
    "assign": "после назначения шлюза {ref}",
    "replace": "после замены устройства {ref}",
    "remove": "после снятия шлюза {ref}",
    "token": "после смены токена бота {ref}",
    "chport": "после смены порта канала конфигурации",
    "host": "после смены адреса сервера",
    "keep": "после настройки нового устройства {ref}",
}


def event_tail(why: list[dict]) -> str:
    """«после смены ключей линка «Шлюз 2» (дача)»; несколько событий —
    «после изменений шлюзов»."""
    kinds = {(w.get("kind"), w.get("ref", "")) for w in why if isinstance(w, dict)}
    if not kinds:
        return ""
    if len(kinds) > 1:
        return "после изменений шлюзов"
    kind, ref = next(iter(kinds))
    return _EVENT_TAIL.get(kind, "после изменений шлюзов").format(ref=ref)


def backup_caption(created: str, refs: list[str], offline: list[tuple[str, str, str]], why_tail: str = "") -> str:
    """Подпись копии в чате и тело письма: одна строка на слот — только
    отклонения; без шлюзов — без блока. offline — [(ref, seen, keep_at)]."""
    head = f"💾 Бэкап сервера AWG от {created}" + (f" — {why_tail}" if why_tail else "")
    lines = [head]
    if refs:
        lines.append("🛰 Шлюзы: " + ", ".join(refs))
    for ref, seen, keep_at in offline:
        lines.append(f"🟡 {ref} не на связи с {_date(seen)} — его настройки в бэкапе на {_date(keep_at)}")
    return "\n".join(lines)


MAIL_BODY = ("Бэкап сервера AWG во вложении — вместе с шлюзами. Восстановить: пришли файл боту сервера AWG "
             "или выполни на сервере sudo awg-bot restore <файл>")


# ── план восстановления (вопрос) ─────────────────────────────────────────────

_PLAN_CASE = {
    "same": "без изменений",
    "rekeyed": "ключи линка новее бэкапа: останутся текущие",
    "device": "в бэкапе другое устройство: связь останется текущей",
    "twin": "устройство в переезде: останется текущим",
    "removed": "снят после бэкапа: бэкап применяется только к текущим шлюзам",
    "added": "добавлен после бэкапа: останется как есть",
}


def _plan_ref(s, bot_username: str) -> str:
    live = s.case not in ("removed", "restored")
    return gw_ref(s.name, s.label, slot=s.slot if live else 0, bot_username=bot_username)


def restore_plan(plan, *, bot_username: str = "") -> str:
    """Вопрос перед восстановлением: что вернётся, что останется текущим,
    что будет со шлюзами."""
    parts = [f"♻️ Восстановить бэкап от {_when(plan.created_at)}?"]
    if plan.line == "same":
        parts[0] += ("\nВернутся к тому моменту настройки профилей, устройств и сервера; профили и "
                     "устройства, созданные или удалённые после бэкапа, останутся как сейчас.\n"
                     "Статистика расхода трафика сохранится.")
        parts.append("Не откатятся: сроки и блокировки — истёкшее и заблокированное останется таким; "
                     "бонусы и отметки сброса трафика")
    elif plan.line == "clean":
        parts.append("🆕 Сервер AWG новый: всё — из бэкапа, включая ключ сервера, порт и подсеть клиентов, — "
                     "устройства и шлюзы подключатся со своими прежними конфигами")
    elif plan.line == "unreadable":
        kept = [_plan_ref(s, "") for s in plan.slots if s.case not in ("removed", "restored")]
        text = "⚠️ Текущая база не читается — сопоставлять не с чем: всё возьму из бэкапа."
        if kept:
            text += f" Шлюзы — по файлам линков на сервере: {', '.join(kept)} останутся на текущих ключах"
            first = plan.slot(1)
            if plan.first_slot_back and first is not None:
                text += (f"; {_plan_ref(first, '')} мог быть снят после бэкапа — тогда он вернётся, "
                         "сними его снова")
        parts.append(text)
    else:
        clients, devices = plan.replaced
        what = " и ".join(x for x in (f"профили ({clients})" if clients else "",
                                       f"устройства ({devices})" if devices else "") if x)
        parts.append(f"⚠️ Бэкап снят на другом сервере: текущие {what} пропадут, вместо них встанут профили "
                     "и устройства из бэкапа. Шлюзы этого сервера останутся как есть")
    if plan.line != "same" and plan.expired_now:
        parts.append(f"Подписки, истёкшие после бэкапа ({plan.expired_now}), сразу станут истёкшими — "
                     "доступа у них не будет ни минуты")
    if plan.empty and plan.copy_slots():
        refs = ", ".join(_plan_ref(s, bot_username) for s in plan.slots if s.case == "restored")
        host = plan.host_copy or plan.host_cur
        parts.append(f"🛰 Шлюзы из бэкапа вернутся: {refs}. Они найдут сервер по адресу <code>{_e(host)}</code> — "
                     "если сервер AWG переехал, этот адрес должен вести сюда. Линки поднимутся, если ключи на "
                     "шлюзах те же, что в бэкапе: после восстановления проверю каждый")
    elif plan.line != "unreadable" and plan.slots:
        lines = ["🛰 Шлюзы — связь с сервером AWG останется текущей:"]
        lines += [f"• {_plan_ref(s, bot_username)} — {_PLAN_CASE.get(s.case, '')}" for s in plan.slots]
        lines.append("Настройки ботов шлюзов из бэкапа предложу в их чатах при расхождении с текущими")
        parts.append("\n".join(lines))
    if plan.host_copy and plan.host_cur and plan.host_copy != plan.host_cur:
        parts.append(f"⚠️ В бэкапе адрес сервера <code>{_e(plan.host_copy)}</code>, сейчас — "
                     f"<code>{_e(plan.host_cur)}</code>. Устройства и шлюзы ищут сервер по прежнему: пока он "
                     "не ведёт сюда, они не подключатся. Шлюзам новый адрес придёт сам, как только они выйдут "
                     "на связь; устройствам — только с перевыпущенным конфигом")
    return "\n\n".join(parts)


# ── отчёт после восстановления ───────────────────────────────────────────────

_DONE_CASE = {
    "rekeyed": "ключи линка оставлены текущие",
    "device": "в бэкапе было другое устройство — связь оставлена текущей",
    "twin": "устройство в переезде — оставлено текущим",
    "added": "добавлен после бэкапа — оставлен как есть",
    "restored": "восстановлен из бэкапа",
}
WAITING = "🟡 жду выхода на связь"


def restore_report(created_at: str, report: dict, *, bot_username: str = "") -> str:
    """Отчёт админу после восстановления: итог по каждому шлюзу и
    устройства, которые не встали."""
    head = f"✅ Восстановлено из бэкапа от {_when(created_at)}"
    slots = report.get("slots") or []
    lines = []
    for s in slots:
        case = str(s.get("case") or "")
        live = case != "removed"
        ref = gw_ref(s.get("name", ""), s.get("label", ""), slot=int(s.get("slot") or 0) if live else 0,
                     bot_username=bot_username)
        if case == "removed":
            dev = gw_ref(s.get("name", ""))
            lines.append(f"• {ref} — снят после бэкапа, бэкап к нему не применён; устройство {dev} осталось в "
                         "твоём профиле — удали его, если не нужно")
            continue
        tail = " · ".join(x for x in (_DONE_CASE.get(case, ""), WAITING) if x)
        lines.append(f"• {ref} — {tail}")
    parts = [head]
    if lines:
        parts.append("🛰 Шлюзы:\n" + "\n".join(lines))
    lost = report.get("lost") or []
    if lost:
        parts.append("\n".join(
            f"• устройство «{_e(d.get('device'))}» профиля {_e(d.get('client'))} не восстановлено: его адрес "
            f"<code>{_e(d.get('address'))}</code> уже у устройства шлюза" for d in lost))
    if any(str(s.get("case")) != "removed" for s in slots):
        parts.append("Свои списки шлюзов: добавленное после бэкапа сохранится, удалённое — вернётся")
    return "\n\n".join(parts)


def gateway_back(ref: str) -> str:
    return f"🟢 {ref} на связи после восстановления"


def gateway_not_back(ref: str, *, empty: bool, link_if: str = "") -> str:
    if empty:
        out = "awg-gw-bundle.sh" if link_if == "awglink" else f"awg-gw-bundle-{link_if}.sh"
        return (f"⚠️ {ref} не вышел на связь за {RESTORE_WAIT_MIN} минут после восстановления. Если на шлюзе "
                "ключи линка новее бэкапа — выпусти файл конфигурации этого шлюза: "
                f"<code>sudo awg-bot gw-bundle --link {_e(link_if)}</code>, перенеси на шлюз и примени: "
                f"<code>sudo sh {_e(out)}</code>")
    return (f"⚠️ {ref} не вышел на связь за {RESTORE_WAIT_MIN} минут после восстановления — связь с ним не "
            "менялась, загляни в «🩺 Здоровье» в чате бота шлюза")
