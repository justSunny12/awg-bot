"""firewall.py — часть пакета settings (см. __init__): «🛡 SSH-доступ» и порт SSH."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, plural_ru, more


def _more_tail(items, shown: int = 12) -> str:
    """Однострочная сводка адресов: хвост обрезанного списка — «[+N]»."""
    items = list(items)
    out = more(items[:shown])
    return out + (f" [+{len(items) - shown}]" if len(items) > shown else "")


def address_list_line(n: int, enabled: bool = False) -> str:
    """Список адресов в инфобокс не выносится — он редактируется кнопками
    под ним; здесь только число и отсылка. enabled — фильтр уже включён:
    звать «включить фильтр» нечего, адрес нужен, чтобы снаружи вошёл ты."""
    if not n and enabled:
        return ("Адреса для SSH-доступа (фильтр): не заданы — добавь «➕ Адрес», "
                "чтобы входить снаружи со своих адресов")
    if not n:
        return ("Адреса для SSH-доступа (фильтр): не заданы — добавь «➕ Адрес», чтобы включить "
                "фильтр снаружи")
    return (f"Адреса для SSH-доступа (фильтр): {n} "
            + plural_ru(n, "адрес", "адреса", "адресов") + " — убрать: «➖» ниже")


def warnings_block(items: list[str]) -> list[str]:
    """Предупреждения раздела — отдельным блоком после пустой строки, по
    одному на строку; нет предупреждений — ничего."""
    return ["", ui.label("Предупреждения"), *items] if items else []


def settings_firewall_text(st: dict) -> str:
    """Раздел «SSH-доступ»: порт, туннель, снаружи, адреса, предупреждения.
    Текст утверждён на вычитке 3.1.0 — только заголовок по новому имени."""
    allow = st.get("raw_allow") or []
    port_line = f"Порт SSH: {st.get('ssh_port')}"
    if st.get("owner") == "omv":
        port_line += " — <b>контролирует OMV</b> <i>(в его UI: Службы → SSH)</i>"
    elif st.get("owner"):
        port_line += " — <b>контролирует другой процесс</b>"
    lines = [ui.head("🛡 SSH-доступ"), "", port_line, ""]
    if st.get("admin_ips"):
        lines += [f"Из туннеля SSH открыт устройствам админа ({len(st['admin_ips'])})", ""]
    if st.get("enabled") and not allow:
        # фильтр без адресов открывает SSH снаружи всем (nftguard: ssh_open);
        # из чата последний адрес при включённом фильтре не снимается — это
        # правка conf руками или `awg-bot firewall setup` с пустым списком
        lines.append("⚠️ Снаружи: фильтр включён, список пуст — SSH открыт всем (только по SSH-ключам)")
    elif st.get("enabled") and not st.get("allow"):
        # в списке одни имена, ни одно не резолвится и прежних адресов нет
        lines.append("⚠️ Снаружи: фильтр включён, но ни одно имя из списка не резолвится — SSH открыт всем (только по SSH-ключам)")
    elif st.get("enabled"):
        lines.append("🟢 Снаружи: фильтр включён — только адреса из списка")
    else:
        lines.append("Снаружи: фильтр выключен — открыт всем (только по SSH-ключам)")
    lines.append("")
    lines.append(address_list_line(len(allow), bool(st.get("enabled"))))
    warns: list[str] = []
    if st.get("unresolved"):
        # весь список — экраном «🛡 Не резолвятся» (без имени бота — «[+N]»)
        from awgbot.util import listlink
        bot = str(st.get("bot_username") or "")
        warns.append("⚠️ Не резолвятся: " + (more(st["unresolved"], link=(bot, listlink.build("fwunres"))) if bot
                                            else _more_tail(st["unresolved"])))
    if st.get("drift") and st.get("enabled"):
        warns.append(f"⚠️ sshd слушает порт {st['listening']}, а фильтр держит {st.get('ssh_port')} — "
                     f"вход снаружи и из туннеля закрыт. Нажми «🅿️ Порт» → {st['listening']} "
                     f"или верни sshd на {st.get('ssh_port')}")
    elif st.get("drift"):
        # фильтр выключен — таблица ничего не держит, вход открыт; запрётся,
        # только если включить фильтр с прежним портом
        warns.append(f"⚠️ sshd слушает порт {st['listening']}, а в настройках бота — {st.get('ssh_port')}: "
                     f"включённый фильтр закроет вход. Нажми «🅿️ Порт» → {st['listening']} "
                     f"или верни sshd на {st.get('ssh_port')}")
    elif st.get("sshd_down"):
        warns.append("🔴 sshd не запущен")
    if st.get("owner") == "generator" and st.get("owner_detail"):
        warns.append(f"⚠️ В <code>{_e((st.get('owner_files') or ['sshd_config'])[0])}</code> сказано: "
                     f"«<i>{_e(st['owner_detail'])}</i>»")
    if st.get("ufw"):
        warns.append("⚠️ ufw активен: второй владелец правил, лучше выключить (<code>ufw disable</code>)")
    if st.get("firewalld"):
        warns.append("⚠️ firewalld активен: второй владелец правил, новый порт открывай и в нём или выключи его")
    return "\n".join(lines + warnings_block(warns))


def ssh_owner_refusal(st: dict, listening: int | None, br, pressed: bool = False) -> str:
    """Отказ смены порта: конфигом sshd владеет не бот. Экран, не alert —
    текст длинный и нужен целиком. Где файл — словами роли (host_loc).
    pressed — человек только нажал «🅿️ Порт», ничего не менял: заголовок не
    «не выполнена», а «здесь не меняется»."""
    place = br.host_loc
    head = "⛔ Порт SSH здесь не меняется" if pressed else "⛔ Смена порта SSH не выполнена"
    if st.get("owner") == "omv":
        if listening and st.get("owner_port") and int(st["owner_port"]) == int(listening):
            now = f"порт {listening} — и в OMV, и у sshd"
        else:
            now = f"sshd слушает {listening}" if listening else "sshd не запущен"
            if st.get("owner_port"):
                now += f", в OMV задан {st['owner_port']}"
        return (ui.head(head, value=f"файлом sshd_config на этом {place} управляет OMV") + "\n\n"
                "Порт задаётся в OMV: Службы → SSH → «Порт», затем «Применить» в жёлтой плашке. "
                "Здесь менять бесполезно: при первом же «Применить» OMV перепишет sshd_config и вернёт свой порт.\n\n"
                "Бот сам увидит новый порт (сверяет каждые несколько минут) и переведёт на него фильтр — "
                "из туннеля, из локальной сети и снаружи. Проброс порта на роутере (при наличии) поправь сам."
                f"\n\nСейчас: {now}.")
    f = (st.get("owner_files") or ["/etc/ssh/sshd_config"])[0]
    return (ui.head(head,
                    value=f"файлом sshd_config на этом {place} управляет другой процесс")
            + f"\n\nВ <code>{_e(f)}</code> сказано: «<i>{_e(st.get('owner_detail') or '')}</i>». "
            "Порт меняй там, откуда файл генерируется, иначе настройка проживёт до его следующей "
            "генерации. Бот увидит новый порт сам и переведёт на него фильтр.")


def ssh_port_ask(current: int | None, br) -> str:
    return (ui.head("🅿️ Порт SSH", meta=[f"сейчас {current}" if current else ""]) + "\n\n"
            "Пришли номер порта 1–65535. Занятый порт не возьму; текущие сеансы не рвутся — "
            f"проверь вход новым подключением{br.ssh_port_tail}")


def ssh_port_busy(port: int, proc: str = "") -> str:
    who = f" уже занят процессом <code>{_e(proc)}</code>" if proc else " занят"
    return f"⛔ Смена порта SSH не выполнена: порт {port}{who}"


def ssh_port_same(port: int) -> str:
    return f"ℹ️ Порт SSH не изменился — выбран уже установленный ({port})"


def ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH: {old} → {new}. Текущие сеансы не рвутся — проверь вход новым "
            f"подключением на порт {new}; файервол провайдера, ufw или fail2ban — открой порт и там")


def firewall_confirmed() -> str:
    return "✅ Таймер снят, фильтр остаётся включённым"


def firewall_rolled_back() -> str:
    return "↩️ Фильтр снят: снаружи SSH снова открыт всем. VPN у пользователей работает как прежде"


FIREWALL_ON_ALERT = ("Фильтр включён: снаружи — только адреса из списка, из туннеля — устройства "
                     "админа. Проверь вход новым подключением")
