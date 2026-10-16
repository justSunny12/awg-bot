"""access.py — параметры и доступ: настройки, статусные строки, файл конфигурации и установка, личный список доменов, уведомления."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil

from ..fmt import _e, plain_ip, holder_link, plural_ru, details

from .slots import ROUTING_NAME
from ..fmt import num
# замечание о шлюзе и уведомления о доступе — тексты уведомлений домена, общие с экранами
from awgbot.domain.notices.routing import (  # noqa: F401
    routing_gateway_warning, ROUTING_GRANTED_NOTICE, routing_granted_holder_notice,
    routing_revoked_holder_notice, ROUTING_REVOKED_NOTICE)


# ── параметры и доступ ───────────────────────────────────────────────────────

def routing_params_text(info: dict, lists: dict) -> str:
    """«⚙️ Параметры РФ-доступа»: проверка живости и списки одной строкой
    каждая, объяснение — под «подробнее»."""
    cnt, src = int(lists.get("count", 0)), int(lists.get("sources", 0))
    age = lists.get("age_seconds")
    if age is None:
        lists_line = f"Списки: ещё не обновлялись · раз в {lists.get('every_hours', 6)} ч"
    else:
        lists_line = (f"Списки: {num(cnt)} {plural_ru(cnt, 'запись', 'записи', 'записей')} из "
                      f"{src} {plural_ru(src, 'источника', 'источников', 'источников')}, {timeutil.age_ago(age)} · "
                      f"раз в {lists.get('every_hours', 6)} ч")
    need = int(info["need"])
    return "\n".join([
        ui.head("⚙️ Параметры РФ-доступа"),
        f"Проверка шлюза: раз в {info['probe_seconds']} с · окно {info['window']} · "
        f"порог {info['availability']}% — переключение после {need} "
        f"{plural_ru(need, 'неудачи', 'неудач', 'неудач')} из {info['window']}",
        lists_line,
        details("наружу зонд ходит не каждый такт: пока через шлюз идёт трафик клиентов, он сам подтверждает, "
                "что путь жив; без трафика — реже, чтобы не выглядеть маячком;\n"
                f"резерв проверяется раз в {info['standby_minutes']} мин;\n"
                f"повторное автопереключение — не раньше чем через {info['interval_minutes']} мин;\n"
                "окно хранится в памяти бота: после перезапуска набирается заново"),
    ])


def routing_users_text() -> str:
    return (ui.head("👥 Кому доступен РФ-доступ", meta=["тебе — всегда"]) + "\n"
            "Разрешённым — включается на всех их устройствах; выключить у себя владельцы могут сами. "
            "Отметь профили и нажми «✅ Применить».")


ROUTING_DISABLE_CONFIRM = ui.confirm("☑️ Выключить РФ-доступ для всех?",
                                     "Российские сервисы снова будут ругаться на VPN; разрешения и списки сохранятся")


def routing_allowed_toast(changes: dict) -> str:
    """Итог «✅ Применить» на «Кому доступен»: один профиль — по имени, несколько — счётом
    («РФ-доступ разрешён 2 профилям», «… разрешён 1, запрещён 2 профилям»).
    changes — {имя: разрешён}."""
    if len(changes) == 1:
        name, on = next(iter(changes.items()))
        return f"Профиль {name}: РФ-доступ " + ("разрешён" if on else "запрещён")
    on = sum(1 for v in changes.values() if v)
    off = len(changes) - on
    return _counted(("разрешён", on), ("запрещён", off), ("профилю", "профилям", "профилям"), "")


def _counted(a: tuple, b: tuple, noun: tuple, prep: str) -> str:
    """«РФ-доступ включён на 1, выключен на 2 устройствах»: нулевая часть не
    выводится, существительное — один раз, по последнему числу."""
    parts = [(word, n) for word, n in (a, b) if n]
    body = ", ".join(f"{word} {prep}{n}" for word, n in parts)
    return f"РФ-доступ {body} " + plural_ru(parts[-1][1], *noun)


def routing_devices_toast(changes: dict) -> str:
    """Итог «✅ Применить» в РФ-доступе профиля: одно устройство — по имени,
    несколько — счётом («РФ-доступ выключен на 2 устройствах»). changes — {имя: включён}."""
    if len(changes) == 1:
        name, on = next(iter(changes.items()))
        return f"Устройство «{name}»: РФ-доступ " + ("включён" if on else "выключен")
    on = sum(1 for v in changes.values() if v)
    off = len(changes) - on
    return _counted(("включён", on), ("выключен", off), ("устройстве", "устройствах", "устройствах"), "на ")


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
        text += (f'\n{ui.label("Бот шлюза")} <a href="https://t.me/{_e(me["username"])}">'
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
    return (ui.head(f"📤 Конфигурация шлюза «{_e(display)}»") + "\n"
            f"Перешли это сообщение {who} — он проверит и применит сам\n\n"
            "⚠️ Сначала перешли его: любая кнопка ниже удалит это сообщение")


def gateway_plain_bundle_caption(display: str) -> str:
    """Подпись под файлом конфигурации нового шлюза: команда установки —
    сообщением выше."""
    return (f"🛰 Файл конфигурации шлюза «{_e(display)}»\n"
            "Команда установки — в сообщении выше. Любая кнопка ниже удалит файл и инструкцию из чата")


def routing_provision_failed(reason: str) -> str:
    return ("⚠️ РФ-доступ не развёрнут.\n\n<pre>" + _e(str(reason)[-900:]) + "</pre>\n\n"
            "Ничего наполовину не осталось: следующий запуск начнёт с того же места. "
            "Частая причина — нет пакета dnsmasq в репозиториях образа.")


def gateway_install_instructions(dev, bundle_name: str = "awg-gw-bundle.sh",
                                 routing_reset: bool = False) -> str:
    """Что делать с файлом конфигурации шлюза — ОДНА строка со своего компьютера.

    Копирование и установка склеены намеренно: установка на шлюзе не задаёт
    вопросов, значит её незачем отделять от копирования и незачем заходить на
    шлюз отдельным сеансом. `ssh -t` — чтобы у sudo был терминал для пароля.
    Поставка агента едет ВНУТРИ файла: с шлюза в России GitHub без туннеля не
    достать, а туннель этим файлом и ставится.
    """
    reset = "\nРФ-доступ у устройства снят: шлюзу он не нужен." if routing_reset else ""
    return (f"🛰 Шлюзом назначено устройство «{_e(dev.name)}» ({plain_ip(dev.address)}).{reset}\n\n"
            "Сохрани файл ниже и выполни <b>со своего компьютера</b> (из папки с файлом) "
            "одну команду — вместо ШЛЮЗ подставь адрес устройства в локальной сети; команда скопирует "
            "файл и поставит агента:\n\n"
            f"<code>scp {_e(bundle_name)} root@ШЛЮЗ:/root/ &amp;&amp; \\\n"
            f"  ssh -t root@ШЛЮЗ 'sudo sh /root/{_e(bundle_name)} --install'</code>\n\n"
            "Установка полностью автоматическая: все необходимые настройки уже собраны в файле.\n"
            "Когда установка закончится, открой <b>бота шлюза</b> и нажми «Запустить» — "
            "и всё готово 🙂\n\n"
            "После установки файл конфигурации удалится со шлюза сам.")


def routing_status_line(ok: bool) -> str:
    """Строка о состоянии РФ-доступа в ОБЩЕМ статусном блоке клиента — рядом
    со статусом сервера, а не отдельным сообщением. Два состояния: трафик
    проходит или нет; про шлюзы и резерв клиенту знать незачем.

    Показывается только тем, кому админ функцию разрешил: рассказывать про
    механизм тому, кто им не пользуется, — шум.
    """
    return (f"🇷🇺 {ROUTING_NAME}: 🟢 работает" if ok else
            f"🇷🇺 {ROUTING_NAME}: 🔴 временно недоступен")


GW_CARD_PAYLOAD = "gw"          # «/start gw-<слот>» — карточка слота из шапки


def routing_admin_status_line(info: dict, bot_username: str = "") -> str:
    """Та же строка в шапке админа — с тем, кто несёт трафик, и состоянием
    резерва (services.routing_admin_status): один шлюз — «работает · имя»;
    два — «… · резерв жив / не отвечает / проверяется», мёртвый резерв красит
    строку в 🟡; выключен — кто именно не отвечает. Имя шлюза и слово
    «резерв» — ссылки в карточку слота (deep-link на себя), когда username
    известен; состояние резерва остаётся текстом."""
    from ..fmt import deep_link
    active, standby = info.get("active", ""), info.get("standby") or []

    def _link(label: str, slot) -> str:
        return deep_link(bot_username, f"{GW_CARD_PAYLOAD}-{int(slot)}", label) if slot else _e(label)

    if info.get("ok"):
        dead = [s for s in standby if s["state"] == "dead"]
        dot = "🟡" if dead else "🟢"
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
        if info.get("active"):
            # активный лежит, резерв жив — следующий шаг человеку
            line += " — переключи на резерв в «🛰 Шлюзы»"
    return line


ROUTING_ABOUT = (
    "Включишь — банки, госуслуги, маркетплейсы будут открываться с российского "
    "адреса, заблокированное — через VPN. Отметь устройства ниже и нажми «✅ Применить» — "
    "ссылки менять не нужно."
)

ROUTING_ADD_PROMPT = "\n".join([
    ui.head("➕ Свои сайты"), "",
    "Пришли адреса — через пробел, с новой строки или через запятую, можно ссылками. "
    "Добавляй те, что пишут «вы не из России»."])

ROUTING_APPLY_HINT = "Заработает в течение минуты; не сработало — переподключи VPN"
# админ пополняет чужой список: переподключаться — владельцу
ROUTING_APPLY_HINT_OWNER = "Заработает в течение минуты; не сработало — пусть владелец переподключится"
ROUTING_SITES_ABOUT = "Открываются с российского адреса"


def _short(domain: str, limit: int = 60) -> str:
    return domain if len(domain) <= limit else domain[:limit - 1] + "…"


def routing_domain_removed(domain: str) -> str:
    """Всплывашка после «➖» — не длиннее 200 знаков answer(): домен режется."""
    return f"{_short(domain)}: убран · применится в течение минуты"


def _lent_out_lines(lent_out) -> list[str]:
    return [f"Устройство «{_e(d.name)}» — у профиля {holder_link(d)}, РФ-доступом на нём управляет он" for d in lent_out]


def routing_panel_text(*, enabled: int, total: int, domains: list, lent_out=(),
                       link_ok: bool = True, profile: str = "", donors: str = "",
                       own_admin: bool = False) -> str:
    """Экран «🇷🇺 РФ-доступ»: охват первой строкой (сохранённое состояние —
    отметки на кнопках применяет «✅ Применить»), свои сайты второй; на всех
    выключено — объяснение открытым текстом; переданные — строкой без кнопки.
    profile — профиль в заголовке, когда админ открыл чужой раздел: готовый
    HTML (ссылка на карточку, profile_link), «· профиль Имя»; donors — «от
    профиля X» у гостя (хвост с кнопок устройств ушёл сюда); own_admin — свой
    раздел админа: шапка как его кнопка на главной, «🇷🇺 Мой РФ-доступ»."""
    tag = f" · профиль {profile}" if profile else (f" · {donors}" if donors else "")
    # свой раздел — «🇷🇺 <b>РФ-доступ:</b> …» (двоеточие внутри жирного); чужой или
    # гостевой — имя профиля после « · », двоеточие после него
    name = f"Мой {ROUTING_NAME}" if own_admin and not tag else ROUTING_NAME
    head = ui.head(f"🇷🇺 {ROUTING_NAME}") + f"{tag}:" if tag else ui.head(f"🇷🇺 {name}:")
    lines = []
    if not total:
        lines += [f"{head} устройств пока нет", "Добавь устройство — и отметь его здесь"]
    elif not enabled:
        lines += [f"{head} выкл", ROUTING_ABOUT]
    else:
        lines.append(f"{head} вкл на " + ("всех" if enabled >= total else f"{enabled} из {total}"))
        if not link_ok:
            lines.append("🔴 временно недоступен — российские сайты пока открываются с зарубежного адреса")
    if domains:
        shown = ", ".join(f"<code>{_e(d)}</code>" for d in domains[:2])
        more = f" [+{len(domains) - 2}]" if len(domains) > 2 else ""
        lines.append(f"Свои сайты: {shown}{more}")
    lines += _lent_out_lines(lent_out)
    return "\n".join(lines)


def routing_sites_text(domains: list) -> str:
    """Экран «📋 Свои сайты»: счётчик и подсказка (объяснение про общий список
    — пока пусто); сами адреса — кнопками «➖»."""
    if not domains:
        return (ui.head("📋 Свои сайты") + "\n"
                "Тут пока пусто. Банки, госуслуги, маркетплейсы — уже в общем списке; "
                "добавляй те, что пишут «вы не из России».")
    return f"{ui.head('📋 Свои сайты', meta=[len(domains)])}\n{ROUTING_SITES_ABOUT}"


def routing_add_report(added: list, rejected: list, over_limit: int, limit: int, *, owner: bool = False) -> str:
    """Итог разбора пачки — первыми строками экрана «Свои сайты»: что взято,
    что нет и почему (отказы — по причине, одной строкой на причину), когда
    заработает; человек вставляет списком, и молча взять половину нельзя.
    owner — список чужой (админ): переподключается владелец."""
    parts = []
    if added:
        shown = ", ".join(f"<code>{_e(_short(d))}</code>" for d in added[:5])
        more = f" и ещё {len(added) - 5}" if len(added) > 5 else ""
        parts.append(f"✅ Добавлено: {shown}{more}")
    if rejected:
        by_reason: dict[str, list[str]] = {}
        for raw, reason in rejected:
            by_reason.setdefault(reason, []).append(raw)
        for reason, raws in by_reason.items():
            shown = ", ".join(f"<code>{_e(_short(r, 40))}</code>" for r in raws[:5])
            more = f" и ещё {len(raws) - 5}" if len(raws) > 5 else ""
            parts.append(f"⚠️ Не добавлено: {shown}{more} — {_reason_html(reason)}")
    if over_limit:
        parts.append(f"📦 Не поместилось: {over_limit} — в списке максимум {limit} "
                     f"{plural_ru(limit, 'адрес', 'адреса', 'адресов')}")
    if not parts:
        return "⚠️ Не нашёл в сообщении ни одного адреса"
    if added:
        parts.append(ROUTING_APPLY_HINT_OWNER if owner else ROUTING_APPLY_HINT)
    return "\n".join(parts)


def _reason_html(reason: str) -> str:
    """Причина отказа из домена — без разметки; образец адреса после «вида»
    («нужен адрес вида sberbank.ru») — моноширинным, остальное экранировано."""
    import re
    return re.sub(r"вида (\S+)", r"вида <code>\1</code>", _e(str(reason)))


def routing_clear_ask(n: int) -> str:
    return ui.confirm(f"🗑 Удалить все свои сайты ({n})?", "Общий список останется")


ROUTING_UNAVAILABLE = "РФ-доступ временно недоступен. Попробуй позже"
# админу — причина и следующий шаг, а не «попробуй позже»
ROUTING_NOT_ALLOWED_ADMIN = "РФ-доступ этому профилю не разрешён — разреши в «🛰 Шлюзы» → «👥 Кому доступен»"


# Подтверждение выключения фичи целиком (общая настройка, не профиль).


__all__ = [
    "routing_gateway_warning", "ROUTING_GRANTED_NOTICE", "routing_granted_holder_notice",
    "routing_revoked_holder_notice", "ROUTING_REVOKED_NOTICE", "routing_params_text",
    "routing_users_text", "ROUTING_DISABLE_CONFIRM", "routing_allowed_toast", "routing_devices_toast",
    "gateway_bundle_applied_text",
    "gateway_installed_text", "gateway_bundle_caption", "gateway_plain_bundle_caption",
    "routing_provision_failed", "gateway_install_instructions", "routing_status_line",
    "GW_CARD_PAYLOAD", "routing_admin_status_line", "ROUTING_ABOUT", "ROUTING_ADD_PROMPT",
    "ROUTING_APPLY_HINT", "ROUTING_APPLY_HINT_OWNER", "ROUTING_SITES_ABOUT", "_short", "routing_domain_removed",
    "_lent_out_lines", "routing_panel_text", "routing_sites_text", "routing_add_report",
    "routing_clear_ask", "ROUTING_UNAVAILABLE", "ROUTING_NOT_ALLOWED_ADMIN"]
