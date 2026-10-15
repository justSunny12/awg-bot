"""assign.py — назначение шлюза: выбор устройства, новое устройство, токен, итог развёртывания."""

from __future__ import annotations


from awgbot.bot import ui
from ..fmt import _e

from .slots import slot_ref
from awgbot.domain.notices.routing import gateways_full   # отказ общий с сервисом


# ── назначение шлюза ─────────────────────────────────────────────────────────

GATEWAY_CHOOSE_INTRO = (ui.head("🛰 Назначить шлюз") + "\n"
                        "Выбери из своих устройств или создай новое. Для шлюза поднимется отдельный линк; "
                        "файл конфигурации шлюза бот выпустит и подскажет команду установки")
GATEWAY_STANDBY_CHOOSE_INTRO = (ui.head("🛰 Резервный шлюз") + "\n"
                                "Когда активный перестанет отвечать, РФ-доступ будет работать через резервный. "
                                "Выбери из своих устройств или создай новое; файл конфигурации шлюза бот выпустит "
                                "и подскажет команду установки")
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
                      f"Ключи линка сменятся — прежнее устройство отключится от линка само. "
                      f"{_role_line(name, bool(state.get('active')))}.",
                      "Выбери замену — из своих устройств или новое"])


def gateway_mark_ask(dev, prev, *, standby: bool = False, replace_state=None) -> str:
    name = _e(dev.name)
    question = (f"🛰 Устройство «{name}» станет шлюзом вместо «{_e(prev.name)}»?" if prev is not None
                else f"🛰 Устройство «{name}» станет шлюзом?")
    head = ui.confirm(question,
                      "Перестанет считаться в лимите устройств; удалить, заблокировать и выдать ссылку будет "
                      "нельзя. Для него выпущу новые ключи линка и файл конфигурации шлюза")
    if prev is not None:
        head += f"\n«{_e(prev.name)}» отключится от линка само"
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
    text = f"➕ Новое устройство «{gateway_new_name(slot)}» в твоём профиле: выпущу ключи линка и файл конфигурации шлюза"
    if prev_name:
        text += f"\nСейчас шлюз — «{_e(prev_name)}»: оно отключится от линка само"
        text += "\n" + _role_line(_e(prev_name), active)
    return text


def gateway_ask_token(name: str) -> str:
    """Приглашение к вводу токена бота шлюза — для будущего устройства по
    имени (не по номеру слота: номер человеку ничего не говорит)."""
    return "\n".join([ui.head(f"🤖 Токен бота для «{_e(name)}»"), "",
                      "У каждого шлюза свой бот: создай его у @BotFather (/newbot) и пришли токен вида "
                      "<code>123456789:AA…</code>. Токен попадёт в файл конфигурации шлюза, сообщение с ним "
                      "сразу удалю"])


def routing_provisioned(tail: str) -> str:
    return ("✅ Обвязка развёрнута, линк поднят\n"
            "Чтобы РФ-доступ заработал, перезапусти бота, затем назначь шлюз — «🛰 Шлюзы» на главной"
            + (f"\n<pre>{_e(tail[-700:])}</pre>" if tail else ""))


__all__ = ["GATEWAY_CHOOSE_INTRO", "GATEWAY_STANDBY_CHOOSE_INTRO", "GATEWAY_PICK_INTRO", "GATEWAY_PICK_EMPTY",
           "gateways_full", "gateway_replace_intro", "gateway_mark_ask", "gateway_new_name", "gateway_new_ask",
           "gateway_ask_token", "routing_provisioned"]
