"""Переезд профилей: подготовка, прогресс, подтверждения, итоги."""

from __future__ import annotations

from awgbot.bot import ui
from .fmt import _e, plural_ru
from .routing.slots import _bot_link
# поздравление и доклад о готовности — уведомления домена, тексты общие
from awgbot.domain.notices.migration import migration_hello, migration_ready  # noqa: F401


def _n_profiles(n: int) -> str:
    return f"{n} {plural_ru(n, 'профиль', 'профиля', 'профилей')}"


def _n_devices(n: int) -> str:
    return f"{n} {plural_ru(n, 'устройство', 'устройства', 'устройств')}"


FAILED_TITLE = "⚠️ Не создано новых конфигураций"
DROPPED_TITLE = "⚠️ Потеряли связь при завершении переезда"
NOT_REMOVED_TITLE = "⚠️ Не снято прежних конфигураций"
LIST_CAP = 20                 # имён поимённо в итогах переезда; дальше — «и ещё N»
STARTED_FAILED_SHOWN = 5      # «⚠️ Переезд не начат»: у кого не создано — в скобках, коротко


def migration_started(res, more: str = "") -> str:
    """Итог включения функции переезда; more — хвост «и ещё N» ссылкой на снимок всех,
    у кого не создано (пусто — текстом)."""
    if not res.started:
        names = ", ".join(_e(n) for n in res.failed[:STARTED_FAILED_SHOWN])
        if len(res.failed) > STARTED_FAILED_SHOWN:
            names += more or f" и ещё {len(res.failed) - STARTED_FAILED_SHOWN}"
        return (ui.head("⚠️ Переезд не начат", value=f"не удалось создать ни одной новой конфигурации ({names}).")
                + "\n\nПохоже, новый интерфейс ещё не поднят на сервере. Функция осталась выключенной, "
                "выдача идёт по-старому — подними интерфейс и включи снова.")
    lines = ["🚚 Переезд начат",
             f"Новых конфигураций создано: {res.born}"]
    if res.already:
        lines.append(f"Уже существовало: {res.already}")
    lines.append(f"Готовность к завершению переезда считаем по списку: {_n_profiles(res.cohort_clients)}, "
                 f"{_n_devices(res.cohort_devices)}")
    if res.failed:
        lines.append(f"\n⚠️ Не удалось создать конфигурацию: {_names_capped(res.failed, more=more)}. "
                     f"Повтори включение — недостающие докинутся, существующие "
                     f"не задвоятся.")
    lines.append("\nТеперь выдаются только новые конфигурации. Проверь на своём "
                 "устройстве, прежде чем звать остальных: если не заработает — "
                 "«↩️ Отменить», и никто, кроме тебя, ничего не заметит")
    return "\n".join(lines)


def migration_start_refused(reason: str) -> str:
    """Механика переезда отказала до рождения двойников (резолвер нового
    интерфейса не поднят, переезд не настроен): функция не включена."""
    why = str(reason).strip().rstrip(".")
    why = why[:1].upper() + why[1:900]
    return ("⚠️ Переезд не начат\n\n" + _e(why) + ".\n\n"
            "Функция осталась выключенной, выдача идёт по-старому")


def migration_prepare_failed(reason: str) -> str:
    return ("⚠️ Новый интерфейс не поднят\n\n<pre>" + _e(str(reason)[-900:]) + "</pre>\n\n"
            "Ничего не изменилось: конфиг бота не тронут, старый интерфейс работает как прежде")


def migration_generation_pending() -> str:
    """Переезд завершён, а смена поколения ядра ещё ждёт своей очереди."""
    return ("🚚 Новое ядро AmneziaWG ждёт переезда профилей\n\n"
            "Для переезда нужен новый интерфейс на новых параметрах ядра — подниму его по кнопке, "
            "потом предложу начать переезд")


def migration_needed(generation: int, applied: int) -> str:
    """Инфобокс при старте: поставка привезла ядро нового поколения."""
    return ("🚚 Нужен переезд профилей\n\nОбновление привезло ядро "
            f"AmneziaWG поколения {generation} — конфигурации поколения {applied} оно не "
            "обслуживает. Рядом со старым интерфейсом уже поднят новый: начни переезд — "
            "каждое устройство получит вторую конфигурацию на новых параметрах. Пользователи "
            "применят изменения, когда им будет удобно — прежние конфигурации продолжат "
            "работать до завершения переезда\n\nПока переезд не завершён, обновления со "
            "следующей сменой ядра не встанут: переезд держит только два интерфейса сразу")


def migration_promoted(iface: str, dns: str = "") -> str:
    """Завершение переезда: новый интерфейс стал основным, нужен рестарт."""
    return (f"🚚 Основным интерфейсом стал <code>{_e(iface)}</code> — новые устройства "
            "создаются на нём. Старый остановлен и снят с автозагрузки, его конфиг сохранён "
            "на всякий случай"
            + (f"\n🔒 DNS пользователей — свой резолвер <code>{_e(dns)}</code>" if dns else "")
            + "\n\nПараметры интерфейса бот читает при старте — нужен перезапуск")


def _rows(rows) -> list[str]:
    """Строки «• профиль — устройство» — экраны листаются страницами (bot/lists/page)."""
    return [f"• {_e(client)} — {_e(device)}" for client, device in rows]


def _names_capped(names, bot: str = "", more: str = "") -> str:
    """Имена через запятую; больше LIST_CAP — «и ещё N»: more — готовый хвост ссылкой на
    снимок события; иначе ссылкой на экран «🚚 Потеряют связь при завершении»
    (без имени бота more_link даёт текст)."""
    from awgbot.util import listlink
    from awgbot.util.textfmt import more_link
    names = list(names)
    out = ", ".join(_e(n) for n in names[:LIST_CAP])
    rest = len(names) - LIST_CAP
    if rest > 0:
        out += more or more_link(rest, bot, listlink.build("migdrop"))
    return out


def migration_pending_page(rows, page: int = 0):
    """Кто ещё не переехал, поимённо; длинный список — страницами."""
    from awgbot.bot.lists.page import Page, paged
    rows = list(rows)
    if not rows:
        return Page(text="✅ Непереехавших нет — все недавно активные устройства уже на новом интерфейсе",
                    cur=0, total=1, start=0, stop=0)
    return paged(_rows(rows), page, gap="\n",
                 header=lambda lbl: ui.head("🚚 Ещё не переехали", meta=[f"из недавно активных ({len(rows)})", lbl]),
                 tail="\nКаждому нужно заново получить конфигурацию — ссылкой, QR или файлом — и импортировать её; "
                      "прежняя работает до завершения переезда")


def migration_start_confirm(clients: int, devices: int, to_birth: int) -> str:
    """Подтверждение старта: с этой секунды меняется выдача, и знаменатель
    готовности замораживается здесь же."""
    # формулировка пользователя 16.10.2026: что произойдёт — без чисел, их несёт итог старта
    return ui.confirm("🚚 Начать переезд?",
                      "Для всех устройств будут созданы новые конфигурации — и с этой секунды выдаваться "
                      "будут только они. Прежние конфигурации продолжат работать до завершения переезда")


def migration_finish_confirm(dropped, old_uplink=(), bot: str = "") -> str:
    """Уронённых называем поимённо, а не числом. Шлюз, не подтвердивший новый
    аплинк, — отдельной строкой: после завершения его бот потеряет Telegram."""
    tail = "Прежние конфигурации снимутся, трафик старых и новых конфигураций сложится в одну историю"
    if not dropped:
        text = ui.confirm("🚚 Завершить переезд?",
                          f"Все недавно активные устройства уже на новом интерфейсе. {tail}")
    else:
        names = _names_capped(dropped, bot)
        text = ui.confirm("🚚 Завершить переезд?",
                          f"Потеряют связь сейчас ({len(dropped)}): {names}. Вернуть их — перенастроить "
                          f"устройства на новую конфигурацию. {tail}")
    for name, gw_bot in old_uplink:
        link = _bot_link(gw_bot)
        where = f"в чате бота шлюза ({link})" if link else "в чате бота шлюза"
        text += (f"\n⚠️ {_e(name)} ещё на прежнем аплинке: после завершения бот шлюза потеряет Telegram. "
                 f"Подтверди новый аплинк {where}, затем заверши переезд")
    return text


def _moved_phrase(moved: int) -> str:
    """«1 устройство — соединение не оборвётся, но при следующей настройке ему
    выдадут прежнюю конфигурацию» / «N устройств — соединения не оборвутся, … им
    выдадут прежние конфигурации»."""
    one = moved == 1
    return (f"{_n_devices(moved)} — {'соединение не оборвётся' if one else 'соединения не оборвутся'}, "
            f"но при следующей настройке {'ему выдадут прежнюю конфигурацию' if one else 'им выдадут прежние конфигурации'}")


def migration_cancel_confirm(moved: int) -> str:
    if not moved:
        return ui.confirm("🚚 Отменить переезд?", "Перейти никто не успел — всё вернётся ровно как было")
    return ui.confirm("🚚 Отменить переезд?",
                      f"Уже переехали: {_moved_phrase(moved)}. Список переехавших будет в разделе «🔧 Сервис»")


def migration_orphans_page(rows, page: int = 0):
    """Конфигурации нового интерфейса, пережившие отмену; длинный список — страницами."""
    from awgbot.bot.lists.page import Page, paged
    rows = list(rows)
    if not rows:
        return Page(text="Переехавших после отмены нет", cur=0, total=1, start=0, stop=0)
    return paged(_rows(rows), page, gap="\n",
                 header=lambda lbl: ui.head("⚠️ Переехали до отмены", f"({len(rows)})", meta=[lbl]),
                 tail="\nОни работают через новый интерфейс, а выдаются снова прежние конфигурации. Прежде чем "
                      "снимать новый интерфейс, перенастрой их на старый")


def migration_cancelled(moved: int) -> str:
    if not moved:
        return "↩️ Переезд отменён — изменения откачены: выдаются прежние конфигурации, влияния на пользователей нет"
    return (f"↩️ Переезд отменён: выдаются прежние конфигурации. Уже переехали: {_moved_phrase(moved)}; "
            "список переехавших — в разделе «🔧 Сервис»")


def migration_finished(removed: int, dropped, failed=(), more_failed: str = "", more_dropped: str = "") -> str:
    """Итог завершения: первые строки открыто, что делать руками — под «подробнее»;
    more_* — хвосты ссылками на снимки полных списков."""
    from .fmt import details
    if failed:
        return (f"⚠️ Завершено не до конца: снято прежних конфигураций {removed}, не закрыто {len(failed)} — "
                "сервер не дал снять конфигурацию: " + _names_capped(failed, more=more_failed)
                + ". Функция оставлена включённой, повтори завершение — доберутся только они")
    head = f"✅ Переезд завершён · снято прежних конфигураций: {removed}"
    if dropped:
        head += (f"\n⚠️ Потеряли связь ({len(dropped)}): " + _names_capped(dropped, more=more_dropped)
                 + " — вернуть можно повторной настройкой устройства")
    # старый интерфейс и его юнит, app.yaml — завершение делает само
    # (migration_finish); здесь — то, чего бот на хосте не знает
    return head + "\nПроверь на сервере:" + details(
        "1. DNAT <code>1.1.1.1</code> и старый порт — убрать и закрыть;\n"
        "2. старая подсеть там, где её помнят: <code>Match Address</code> в <code>sshd_config</code> "
        "(обе строки), список <code>ssh_allow4</code>, правила сервера для DNS и MASQUERADE;\n"
        "3. если стоит шлюз — сменить подсеть пользователей на обеих его сторонах одним заходом: исключение "
        "из MASQUERADE на сервере AWG, на шлюзе — его MASQUERADE и AllowedIPs линка; иначе шлюз увидит "
        "реальные адреса пользователей вне своих правил — РФ-доступ перестанет работать")


def migration_prepare_intro(d: dict) -> str:
    """Экран перед переездом: что сменится и чего это стоит."""
    from .fmt import details
    port = f"порт {d['want_port']}" if d.get("want_port") else "случайный высокий порт"
    dns = ", DNS пользователей — свой резолвер" if d.get("private_dns") else ""
    return (ui.head("🚚 Порт или подсеть", meta=[f"сейчас {_e(d['iface'])}, порт {d['port']}, <code>{_e(d['subnet'])}</code>"]) + "\n\n"
            f"Новый интерфейс, {port}, свободная подсеть{dns}\n"
            # формулировка пользователя 16.10.2026: «остальные» читалось как «не онлайн за 2 недели»
            "Вторую конфигурацию получат все устройства, но готовность к завершению переезда считаем по "
            f"{d['devices']} {plural_ru(d['devices'], 'устройству', 'устройствам', 'устройствам')} "
            f"у {d['clients']} {plural_ru(d['clients'], 'профиля', 'профилей', 'профилей')} — тем, что были "
            "онлайн за последние две недели\n"
            "⚠️ При завершении переезда устройства, не применившие новую конфигурацию, потеряют доступ — "
            "но перевыпуск конфигурации по ним останется доступен"
            + details("порт и подсеть вшиты в каждую ссылку, поэтому меняются переездом: рядом со старым "
                      "интерфейсом поднимается новый, каждое устройство получает вторую конфигурацию;\n"
                      "новые настройки выдаются с момента начала переезда — пользователи перенастраивают "
                      "устройства, когда им удобно;\n"
                      "прежние конфигурации работают до завершения переезда;\n"
                      "отменить переезд можно в любой момент — без последствий;\n"
                      "при завершении прежние конфигурации удаляются, непереехавшие теряют связь — бот назовёт "
                      "их поимённо до подтверждения;\n"
                      "конфигурации на новом интерфейсе остаются — перейти на них можно и после завершения"))


MIGRATION_ASK_PORT = (ui.head("✏️ Порт нового интерфейса", meta=["1–65535"]) + "\n\n"
                      "Пришли номер порта; «✖️ Отмена» — останется случайный высокий. 443 подойдёт, если на "
                      "сервере его никто не слушает: UDP на 443 похож на QUIC. Занятый порт не возьму")

MIGRATION_PREPARING = ("🚚 Поднимаю новый интерфейс: ключи, порт, обфускация, автозагрузка — "
                       "несколько секунд")


def migration_prepared(res: dict) -> str:
    return (f"✅ Новый интерфейс поднят: <code>{_e(res['iface'])}</code>"
            + (f", порт {_e(str(res['port']))}" if res.get("port") else "")
            + f", <code>{_e(res['subnet'])}</code>. После перезапуска бота — «🔧 Сервис» → «🚚 Начать переезд»")


__all__ = [
    "migration_hello", "migration_ready", "migration_started", "migration_prepare_failed",
    "migration_generation_pending", "migration_needed", "migration_promoted", "LIST_CAP", "STARTED_FAILED_SHOWN",
    "_rows", "_names_capped", "migration_pending_page", "migration_orphans_page", "migration_start_confirm",
    "migration_finish_confirm", "migration_cancel_confirm",
    "migration_cancelled", "migration_finished", "migration_prepare_intro", "MIGRATION_ASK_PORT",
    "MIGRATION_PREPARING", "migration_prepared"]
