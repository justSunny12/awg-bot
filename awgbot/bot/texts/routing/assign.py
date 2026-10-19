"""assign.py — назначение шлюза: выбор устройства, новое устройство, токен, итог развёртывания."""

from __future__ import annotations


from awgbot.bot import ui
from ..fmt import _e

from .slots import slot_ref
from awgbot.domain.notices.routing import gateways_full   # отказ общий с сервисом


# ── назначение шлюза ─────────────────────────────────────────────────────────

GATEWAY_CHOOSE_INTRO = (ui.head("🛰 Подключить шлюз") + "\n"
                        "Выбери из своих устройств или создай новое. Для шлюза поднимется отдельный линк; "
                        "бот пришлёт команду для шлюза")
GATEWAY_STANDBY_CHOOSE_INTRO = (ui.head("🛰 Резервный шлюз") + "\n"
                                "Когда активный перестанет отвечать, РФ-доступ будет работать через резервный. "
                                "Выбери из своих устройств или создай новое; бот пришлёт команду для шлюза")
GATEWAY_PICK_INTRO = ui.head("📱 Из моих устройств") + " — выбери, какое станет шлюзом. Обычным устройством оно быть перестанет"
GATEWAY_PICK_EMPTY = "📱 У тебя нет устройств, выпущенных ботом — создай новое"


def _role_line(name: str, active: bool) -> str:
    """Что будет с трафиком при замене устройства слота."""
    if active:
        return f"{name} сейчас активный: трафик уйдёт на резерв, если он жив"
    return "Шлюз резервный: трафик клиентов не затронут"


def gateway_replace_intro(state: dict) -> str:
    name = slot_ref(state)
    return "\n".join([f"🔁 Заменить устройство «{name}»?",
                      "Линк сразу перейдёт на ключи новой команды — прежнее устройство отключится. Пока новое не "
                      "подключено, замену можно отменить, и линк вернётся прежнему устройству; через час без "
                      "подключения замена отменится сама",
                      _role_line(name, bool(state.get("active"))),
                      "Выбери замену — из своих устройств, новое или это же устройство заново",
                      "🔁 Настроить заново — подключить «" + name + "» заново: новая команда, новые ключи линка; "
                      "настройки бота шлюза вернутся сами"])


def gateway_same_ask(state: dict) -> str:
    """«🔁 Настроить заново» — подтверждение (то же устройство слота)."""
    name = slot_ref(state)
    return "\n".join([f"🔁 Подключить «{name}» заново?",
                      "Бот пришлёт новую команду для этого же устройства. Линк сразу перейдёт на её ключи — пока "
                      "шлюз не подключится, линк простаивает; отменить можно до ввода кода, через час без "
                      "подключения настройка отменится сама",
                      _role_line(name, bool(state.get("active")))])


def gateway_mark_ask(dev, prev, *, standby: bool = False, replace_state=None) -> str:
    name = _e(dev.name)
    question = (f"🛰 Устройство «{name}» станет шлюзом вместо «{_e(prev.name)}»?" if prev is not None
                else f"🛰 Устройство «{name}» станет шлюзом?")
    head = ui.confirm(question,
                      "Перестанет считаться в лимите устройств; удалить, заблокировать и выдать ссылку будет "
                      "нельзя. Бот пришлёт команду для шлюза")
    if prev is not None:
        head += f"\nЛинк сразу перейдёт на ключи новой команды — устройство «{_e(prev.name)}» отключится"
    if replace_state is not None and prev is not None:
        head += "\n" + _role_line(_e(prev.name), bool(replace_state.get("active")))
    elif standby:
        head += "\nСтанет резервным: трафик пойдёт через него, только если активный не отвечает"
    return head


def gateway_new_name(slot: int = 1) -> str:
    """Имя нового устройства-шлюза в профиле админа: «Шлюз» / «Шлюз N»."""
    return "Шлюз" if slot <= 1 else f"Шлюз {slot}"


def gateway_new_ask(slot: int = 1, prev_name: str = "", active: bool = False) -> str:
    """Замена устройства слота новым — те же строки, что при выборе
    из своих: кто сейчас в слоте и что будет с трафиком."""
    text = f"➕ Новое устройство «{gateway_new_name(slot)}» в твоём профиле: бот пришлёт команду для шлюза"
    if prev_name:
        text += (f"\nСейчас шлюз — устройство «{_e(prev_name)}»: линк сразу перейдёт на ключи новой команды, "
                 "оно отключится")
        text += "\n" + _role_line(_e(prev_name), active)
    return text


def gateway_ask_token(name: str) -> str:
    """Приглашение к вводу токена бота шлюза — для будущего устройства по
    имени (не по номеру слота: номер человеку ничего не говорит)."""
    return "\n".join([ui.head(f"🤖 Токен бота для шлюза «{_e(name)}»"), "",
                      "У каждого шлюза свой бот: создай его у @BotFather (/newbot) и пришли токен вида "
                      "<code>123456789:AA…</code>. Токен будет отправлен шлюзу по каналу конфигурации после "
                      "подтверждения кода, сообщение с токеном сразу удалю"])


def routing_provisioned(tail: str) -> str:
    return ("✅ РФ-доступ развёрнут на сервере, линк поднят\n"
            "Чтобы РФ-доступ заработал, перезапусти бота, затем назначь шлюз — «🛰 Шлюзы» на главной"
            + (f"\n<pre>{_e(tail[-700:])}</pre>" if tail else ""))


__all__ = ["GATEWAY_CHOOSE_INTRO", "GATEWAY_STANDBY_CHOOSE_INTRO", "GATEWAY_PICK_INTRO", "GATEWAY_PICK_EMPTY",
           "gateways_full", "gateway_replace_intro", "gateway_same_ask", "gateway_mark_ask", "gateway_new_name", "gateway_new_ask",
           "gateway_ask_token", "routing_provisioned"]
