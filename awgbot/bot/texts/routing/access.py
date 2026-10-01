"""access.py — параметры и доступ: настройки, статусные строки, файл конфигурации и установка, личный список доменов, уведомления."""

from __future__ import annotations

from awgbot.util import timeutil

from ..fmt import _e, plain_ip, client_link, holder_link, plural_ru, details

from .slots import ROUTING_NAME, _fmt_n


# ── параметры и доступ ───────────────────────────────────────────────────────

def routing_params_text(info: dict, lists: dict) -> str:
    """«⚙️ Параметры РФ-доступа»: проверка живости и списки одной строкой
    каждая, объяснение — под «подробнее»."""
    cnt, src = int(lists.get("count", 0)), int(lists.get("sources", 0))
    age = lists.get("age_seconds")
    if age is None:
        lists_line = f"Списки: ещё не обновлялись · раз в {lists.get('every_hours', 6)} ч"
    else:
        lists_line = (f"Списки: {_fmt_n(cnt)} {plural_ru(cnt, 'запись', 'записи', 'записей')} из "
                      f"{src} {plural_ru(src, 'источника', 'источников', 'источников')}, {timeutil.age_ago(age)} · "
                      f"раз в {lists.get('every_hours', 6)} ч")
    need = int(info["need"])
    return "\n".join([
        "⚙️ <b>Параметры РФ-доступа</b>",
        f"Проверка живости: такт {info['probe_seconds']} с · окно {info['window']} · "
        f"порог {info['availability']}% ({need} {plural_ru(need, 'неудача', 'неудачи', 'неудач')} из {info['window']})",
        lists_line,
        details("наружу зонд ходит не каждый такт — трафик клиентов сам доказывает путь, без него "
                "зонд реже (защита от поведенческих блокировок) · резерв зондируется раз в "
                f"{info['standby_minutes']} мин · второе автопереключение не раньше чем через "
                f"{info['interval_minutes']} мин · окно живёт в памяти бота и после перезапуска "
                "копится заново"),
    ])


def routing_users_text() -> str:
    return ("👥 <b>Кому доступен РФ-доступ</b> (тебе — всегда)\n"
            "Владельцы устройств управляют настройкой на них сами")


ROUTING_DISABLE_CONFIRM = ("🔴 Выключить РФ-доступ для всех? Российские сервисы снова будут ругаться "
                           "на VPN; разрешения и списки сохранятся")


def gateway_bundle_applied_text(display: str, ok: bool, error: str = "") -> str:
    """Уведомление в чате админа по итогу применения файла на шлюзе (каналом):
    файл и карточка над ним уходят, следом — карточка слота."""
    if ok:
        return f"✅ Конфигурация шлюза <b>{_e(display)}</b> успешно обновлена"
    return (f"⚠️ Конфигурация шлюза <b>{_e(display)}</b> не обновлена"
            + (f": {_e(error)}" if error else ""))


def gateway_installed_text(display: str, agent_bot: dict | None) -> str:
    """Шлюз, поставленный файлом первого применения, вышел на связь: агент
    стоит, канал поднят. Ссылка на бота — из снимка канала (агент знает
    своего бота сам); без неё строки бота нет."""
    me = agent_bot or {}
    text = f"✅ Шлюз <b>{_e(display)}</b> успешно настроен 🎉"
    if me.get("username"):
        text += (f'\n<b>Бот шлюза:</b> <a href="https://t.me/{_e(me["username"])}">'
                 f'{_e(me.get("name") or me["username"])}</a>')
    return text


def gateway_bundle_caption(display: str, agent_bot: dict | None) -> str:
    """Подпись под файлом конфигурации: какому шлюзу и какому боту его
    пересылать — ссылкой в чат, как в карточке. Бота ещё не спросили —
    без ссылки."""
    me = agent_bot or {}
    who = "боту шлюза"
    if me.get("username"):
        who += f' (<a href="https://t.me/{_e(me["username"])}">{_e(me.get("name") or me["username"])}</a>)'
    return (f"📤 <b>Конфигурация шлюза {_e(display)}</b>\n"
            f"Перешли это сообщение {who} — он проверит и применит сам\n\n"
            "ℹ️ Возврат в меню удалит это сообщение")


def gateway_plain_bundle_caption(display: str) -> str:
    """Подпись под файлом первого применения: инструкция — сообщением выше."""
    return ("🛰 Файл конфигурации шлюза\n"
            f"Воспользуйся инструкцией выше для настройки нового шлюза: <b>{_e(display)}</b>\n\n"
            "После возврата в меню сообщение с файлом и инструкция удалятся из чата.")


def routing_provision_failed(reason: str) -> str:
    return ("⚠️ Обвязка не развёрнута.\n\n<pre>" + _e(str(reason)[-900:]) + "</pre>\n\n"
            "Ничего наполовину не осталось: следующий запуск начнёт с того же места. "
            "Частая причина — нет пакета dnsmasq в репозиториях образа.")


GW_INSTALL_URL = ("https://raw.githubusercontent.com/justSunny12/awg-bot/main/"
                  "install/awg-bot-install.sh")


def gateway_install_instructions(dev, bundle_name: str = "awg-gw-bundle.sh",
                                 routing_reset: bool = False) -> str:
    """Что делать с файлом первого применения — ОДНА строка со своей машины.

    Копирование и установка склеены намеренно: установка на шлюзе не задаёт
    вопросов, значит её незачем отделять от копирования и незачем заходить на
    шлюз отдельным сеансом. `ssh -t` — чтобы у sudo был терминал для пароля.
    Поставка агента едет ВНУТРИ файла: с шлюза в России GitHub без туннеля не
    достать, а туннель этим файлом и ставится.
    """
    reset = "\nРФ-доступ у устройства снят: шлюзу он не нужен." if routing_reset else ""
    return (f"🛰 Шлюзом назначен «{_e(dev.name)}» ({plain_ip(dev.address)}).{reset}\n\n"
            "Сохрани файл ниже и выполни <b>со своего компьютера</b> (из директории с файлом) "
            "одну команду — она скопирует его на шлюз и сразу поставит агента:\n\n"
            f"<code>scp {_e(bundle_name)} root@ШЛЮЗ:/root/ &amp;&amp; \\\n"
            f"  ssh -t root@ШЛЮЗ 'sudo sh /root/{_e(bundle_name)} --install'</code>\n\n"
            "Установка полностью автоматическая: все необходимые настройки уже собраны в файле.\n"
            "Когда установка закончится, отправь <b>боту шлюза</b> <code>/start</code> — "
            "и всё готово 🙂\n\n"
            "После установки файл конфигурации удалится с хоста шлюза сам.")


def routing_status_line(ok: bool) -> str:
    """Строка о состоянии РФ-доступа в ОБЩЕМ статусном блоке клиента — рядом
    со статусом сервера, а не отдельным сообщением. Два состояния: трафик
    проходит или нет; про шлюзы и резерв клиенту знать незачем.

    Показывается только тем, кому админ функцию разрешил: рассказывать про
    механизм тому, кто им не пользуется, — шум.
    """
    return (f"🇷🇺 {ROUTING_NAME}: 🟢 работает" if ok else
            f"🇷🇺 {ROUTING_NAME}: 🔴 не работает")


GW_CARD_PAYLOAD = "gw"          # «/start gw-<слот>» — карточка слота из шапки


def routing_admin_status_line(info: dict, bot_username: str = "") -> str:
    """Та же строка в шапке админа — с тем, кто несёт трафик, и состоянием
    резерва (services.routing_admin_status): один шлюз — «работает · имя»;
    два — «… · резерв жив / не отвечает / проверяется», мёртвый резерв красит
    строку в 🟠; выключен — кто именно не отвечает. Имя шлюза и слово
    «резерв» — ссылки в карточку слота (deep-link на себя), когда username
    известен; состояние резерва остаётся текстом."""
    from ..fmt import deep_link
    active, standby = info.get("active", ""), info.get("standby") or []

    def _link(label: str, slot) -> str:
        return deep_link(bot_username, f"{GW_CARD_PAYLOAD}-{int(slot)}", label) if slot else _e(label)

    if info.get("ok"):
        dead = [s for s in standby if s["state"] == "dead"]
        dot = "🟠" if dead else "🟢"
        line = f"🇷🇺 {ROUTING_NAME}: {dot} работает"
        if active:
            line += f" · {_link(active, info.get('active_slot'))}"
        if standby:
            # ссылка — только на слове «резерв»: состояние остаётся текстом
            st = standby[0]
            tail = {"alive": "жив", "dead": "не отвечает"}.get(st["state"], "проверяется")
            line += " · " + _link("резерв", st.get("slot")) + " " + tail
        return line
    names = ([(active, info.get("active_slot"))] if active else []) \
        + [(s["name"], s.get("slot")) for s in standby if s["state"] != "alive"]
    alive = [s for s in standby if s["state"] == "alive"]
    if not names and not alive:
        return ""                                     # шлюзов нет — строке нечего сказать
    line = f"🇷🇺 {ROUTING_NAME}: 🔴 недоступен"
    if len(names) == 1:
        line += f", {_link(*names[0])} не отвечает"
    elif names:
        line += " — " + ", ".join(_link(n, s) for n, s in names) + " не отвечают"
    if alive:
        line += ", " + _link("резерв", alive[0].get("slot")) + " жив"
    return line


ROUTING_ABOUT = (
    "Включишь — банки, госуслуги, маркетплейсы будут открываться с российского "
    "адреса, заблокированное — через VPN. Ссылки менять не нужно"
)
ROUTING_ABOUT_OFF = ROUTING_ABOUT

ROUTING_ADD_PROMPT = (
    "➕ <b>Сайты с российского адреса</b>\n"
    "Пришли адреса — по одному в строке или через запятую, можно ссылками. "
    "Добавляй то, что пишет «вы не из России»"
)

ROUTING_APPLY_HINT = "применится в теч. минуты, не сработало — переподключись"
ROUTING_ADDED_HINT = ROUTING_APPLY_HINT
ROUTING_SITES_ABOUT = ("Открываются с российского адреса. Банки, госуслуги, маркетплейсы — "
                       "уже в общем списке")


def _short(domain: str, limit: int = 60) -> str:
    return domain if len(domain) <= limit else domain[:limit - 1] + "…"


def routing_domain_removed(domain: str) -> str:
    """Всплывашка после «➖» — не длиннее 200 знаков answer(): домен режется."""
    return f"{_short(domain)} убран · применится в теч. минуты"


def _lent_out_lines(lent_out) -> list[str]:
    return [f"{_e(d.name)} — у профиля {holder_link(d)}, управляет функцией он" for d in lent_out]


def routing_panel_text(*, enabled: int, total: int, domains: list, lent_out=(),
                       link_ok: bool = True) -> str:
    """Экран «🇷🇺 РФ-доступ»: охват первой строкой, свои сайты второй; на всех
    выключено — объяснение открытым текстом; переданные — строкой без кнопки."""
    head = f"🇷🇺 <b>{ROUTING_NAME}:</b>"
    lines = []
    if not total:
        lines.append(f"{head} устройств пока нет")
    elif not enabled:
        lines += [f"{head} выкл", ROUTING_ABOUT]
    else:
        lines.append(f"{head} вкл на " + ("всех" if enabled >= total else f"{enabled} из {total}"))
        if not link_ok:
            lines.append("🔴 временно недоступен")
    if domains:
        shown = ", ".join(f"<code>{_e(d)}</code>" for d in domains[:2])
        more = f" [+{len(domains) - 2}]" if len(domains) > 2 else ""
        lines.append(f"Свои сайты: {shown}{more}")
    lines += _lent_out_lines(lent_out)
    return "\n".join(lines)


def routing_sites_text(domains: list) -> str:
    """Экран «📋 Сайты»: счётчик и подсказка; сами адреса — кнопками «➖»."""
    if not domains:
        return ("📋 <b>Свои сайты</b>\n"
                "Тут пока пусто. Банки, госуслуги, маркетплейсы — уже в общем списке; "
                "добавляй то, что пишет «вы не из России»")
    return f"📋 <b>Свои сайты</b> · {len(domains)}\n{ROUTING_SITES_ABOUT}"


def routing_add_report(added: list, rejected: list, over_limit: int, limit: int) -> str:
    """Итог разбора пачки — первой строкой экрана «Сайты»: что взято, что нет
    и почему; человек вставляет списком, и молча взять половину нельзя."""
    parts = []
    if added:
        shown = ", ".join(f"<code>{_e(_short(d))}</code>" for d in added[:5])
        more = f" и ещё {len(added) - 5}" if len(added) > 5 else ""
        parts.append(f"✅ Добавлено: {shown}{more}")
    if rejected:
        shown = "; ".join(f"<code>{_e(_short(raw, 40))}</code> — {_e(reason)}" for raw, reason in rejected[:3])
        more = f"; и ещё {len(rejected) - 3}" if len(rejected) > 3 else ""
        parts.append(f"⚠️ Не добавлено: {shown}{more}")
    if over_limit:
        parts.append(f"📦 Не поместилось: {over_limit} — в списке максимум {limit} "
                     f"{plural_ru(limit, 'адрес', 'адреса', 'адресов')}")
    if not parts:
        return "⚠️ Не нашёл в сообщении ни одного адреса"
    if added:
        parts.append(ROUTING_APPLY_HINT)
    return " · ".join(parts)


def routing_clear_ask(n: int) -> str:
    return f"🗑 Удалить все свои сайты ({n})? Общий список останется"


ROUTING_CLEAR_CONFIRM = routing_clear_ask(0)

ROUTING_UNAVAILABLE = "РФ-доступ временно недоступен. Попробуй позже"


def routing_gateway_warning(verdict: str, *, at_start: bool) -> str:
    """Замер шлюза не прошёл: одна формулировка для замечаний при запуске и для
    момента включения функции в настройках. PROBE_OK — пусто."""
    from awgbot.infra import routing as _rt
    when = "на старте" if at_start else "при включении"
    if verdict == _rt.PROBE_NO_PATH:
        return ("шлюз РФ-доступа отвечает, но интернета за ним нет — "
                "чинить на самом шлюзе (аплинк, ip_forward, MASQUERADE). Маркировка "
                "снята, российские сервисы временно открываются с зарубежного адреса")
    if verdict != _rt.PROBE_OK:
        return (f"шлюз РФ-доступа не отвечает {when} — маркировка снята, "
                "российские сервисы временно открываются с зарубежного адреса")
    return ""

# Подтверждение выключения фичи целиком (общая настройка, не профиль).

# Владельцу профиля — админ выдал/отозвал разрешение на РФ-доступ.
ROUTING_GRANTED_NOTICE = (
    "🇷🇺 К твоей подписке добавлен РФ-доступ 🎉\n"
    "РФ-приложения и сайты теперь будут работать со включённым VPN. Включён на всех "
    "твоих устройствах; выключить и добавить свои сайты — в разделе «🇷🇺 РФ-доступ»"
)
def routing_granted_holder_notice(donor) -> str:
    return (f"🇷🇺 К устройствам от профиля {client_link(donor)} добавлен РФ-доступ 🎉\n"
            "РФ-приложения и сайты теперь будут работать со включённым VPN. Включён на всех "
            "твоих устройствах; выключить и добавить свои сайты — в разделе «🇷🇺 РФ-доступ»")


def routing_revoked_holder_notice(donor) -> str:
    return (f"🇷🇺 РФ-доступ для устройств от профиля {client_link(donor)} больше недоступен 😔\n"
            "Для доступа к ресурсам, которые ругаются на VPN, теперь придётся его выключать")


ROUTING_REVOKED_NOTICE = (
    "🇷🇺 РФ-доступ больше не входит в твою подписку 😔\n"
    "Для доступа к ресурсам, которые ругаются на VPN, теперь придётся его выключать"
)


ROUTING_LENT_OUT_NOTE = "Переданными устройствами управляют те, кому они переданы"
