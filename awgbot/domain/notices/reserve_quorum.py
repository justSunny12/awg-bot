"""reserve_quorum.py — часть пакета notices (см. __init__): кворум резерва —
нехватка голосов и приём аварийного кода (Т-К21, Т-РН3), голосование
включено, без кворума и кворум вернулся (Т-К7, Т-К8, Т-К12), ничья и её итог
(Т-К5…Т-К6). Правила — как у reserve; имена узлов кворума без двойных кавычек."""
from __future__ import annotations

import html

from awgbot.util.textfmt.base import plural_ru

from .reserve_fmt import _e, _n, _when


# ── нехватка голосов и аварийный код (этап 5) ───────────────────────────────

OFF_PLAIN = "кворум отключён"          # причина без кода (codewin.rn_off_why): в скобках не повторяется


def _off_paren(off_why: str) -> str:
    why = off_why or "нет активных участников кворума"
    return "" if why == OFF_PLAIN else f" ({_e(why)})"


def quorum_code_needed_reserve(primary: str, since: str, n: int, v: int, t: int, admin_opened: bool, off_why: str = "") -> str:
    """Т-К21 (Т-РУ7 под кворумом, админу резерва): голосов для переезда не хватает — приём кода
    открыт; Т-К21г — голосует только этот сервер (v = 1); Т-К21д — голосование о сервере не
    включено (причина), сигнал по линку пары."""
    tail = ("нужен аварийный код администратора " + _n(primary)
            + (" — уже отправил ему запрос" if admin_opened else ", но он ещё не запускал этого бота, написать ему не могу"))
    if off_why or not v:
        return (f"⚠️ {_n(primary)} недоступен с {_when(since)}, а кворум отключён{_off_paren(off_why)} — принять "
                f"пользователей автоматически не смогу: {tail}")
    if int(v) < 2:
        return (f"⚠️ {_n(primary)} не отвечает с {_when(since)}, а в кворуме участвует только этот сервер: шлюзов с "
                f"агентом v4.2.0+ нет — автоматический переезд недоступен, {tail}")
    return (f"⚠️ {_n(primary)} не отвечает с {_when(since)}, а голосов для переезда не хватает: сбой фиксируют {int(n)} из "
            f"{int(v)} {plural_ru(int(v), 'узла', 'узлов', 'узлов')} (требуется {int(t)}) — принять пользователей "
            f"автоматически не смогу: {tail}\n"
            "Если большинство зафиксирует недоступность, переезд произойдёт автоматически")


def quorum_code_needed_primary(primary: str, reserve: str, since: str, n: int, v: int, t: int, off_why: str = "") -> str:
    """Т-К21 (Т-РУ7б под кворумом, админу основы в чат его роли в боте резерва); Т-К21г — v = 1;
    Т-К21д — без кворума."""
    if off_why or not v:
        return (f"🆘 {_n(reserve)} не видит {_n(primary)} с {_when(since)}, а автоматический кворум "
                f"отключён{_off_paren(off_why)}\n"
                "Если сервер действительно упал — пришли сюда аварийный код")
    if int(v) < 2:
        return (f"🆘 {_n(reserve)} не видит {_n(primary)} с {_when(since)}, а в кворуме остался только сервер резерва: "
                "шлюзов с агентом v4.2.0+ нет — автоматический переезд недоступен\n"
                "Если сервер действительно упал — пришли сюда аварийный код")
    return (f"🆘 {_n(reserve)} не видит {_n(primary)} с {_when(since)}, а голосов для переезда не хватает: сбой фиксируют "
            f"{int(n)} из {int(v)} {plural_ru(int(v), 'узла', 'узлов', 'узлов')} (требуется {int(t)})\n"
            "Если сервер действительно упал — пришли сюда аварийный код; если большинство зафиксирует недоступность, "
            "переезд произойдёт автоматически")


def code_window_closed(primary: str, why: str, reserve: str = "") -> str:
    """Т-РН3 (админу основы): приём кода закрыт — большинство снова видит сервер (quorum), голосов
    хватило (taken), второй сервер снова видит его (seen, v = 1) или снова на связи по линку пары (link)."""
    if why == "taken":
        return "ℹ️ Голосов хватило — переезд идёт сам, код не нужен"
    if why == "link":
        return f"ℹ️ {_n(reserve) if reserve else 'Резерв'} снова на связи с {_n(primary)} — переезд не нужен"
    if why == "seen":
        return f"ℹ️ {_n(reserve) if reserve else 'Резерв'} снова видит {_n(primary)} — переезд не нужен"
    return f"ℹ️ Большинство снова видит {_n(primary)} — переезд не нужен"


def code_wrong(left: int) -> str:
    """Т-РН3: код не подошёл."""
    return f"⚠️ Код не подошёл. Осталось попыток: {int(left)}" + (" — следующие через час" if left else "")


def code_open_failed(primary: str, reason: str) -> str:
    return f"⚠️ Код подошёл, но пользователей {_n(primary)} поднять не удалось: {_e(reason)}. Приём кода снова открыт"


# ── кворум (4.2.0) ──────────────────────────────────────────────────────────

def _names(names) -> str:
    return ", ".join(_n(x) for x in names or [])


def quorum_enabled(reserve: str, gateways: list, me: str = "") -> str:
    """Т-К12 (админу основы, без звука): голосование включено; ключ и код прежние."""
    who = _n(reserve) + (" и шлюзы " + _names(gateways) if gateways else "")
    whose = f"пользователей {_n(me)}" if me else "пользователей"
    return f"🗳 Голосование включено: о переезде {whose} решает большинство — {who}"


def quorum_fenced(name: str, y: int, v: int, since: str) -> str:
    """Т-К7 (админу лидера, без звука): без кворума."""
    return (f"⚠️ {_n(name)} без кворума с {_when(since)}: видят {int(y)} из {int(v)} — бот не обрабатывает запросы "
            "пользователей, VPN работает. Пройдёт само, когда голоса вернутся")


def quorum_unfenced(name: str, y: int, v: int) -> str:
    """Т-К8 (тому же, без звука): кворум вернулся."""
    return f"✅ Кворум вернулся: {_n(name)} видят {int(y)} из {int(v)} — бот снова отвечает пользователям"


def quorum_enabled_peer(primary: str) -> str:
    """Т-К12б (админу второго сервера, без звука)."""
    return f"🗳 Голосование о {_n(primary)} включено: этот сервер и шлюзы {_n(primary)} решают, когда его пользователям переезжать сюда"


def _qn(name: str) -> str:
    """Имя узла кворума: чужой шлюз приходит уже с кавычками («шлюз 3 «Beta»») — не удваивать."""
    return html.escape(name) if "«" in name else _n(name)


def _qnames(names) -> str:
    return ", ".join(_qn(x) for x in names or [])


def _tie_head(name: str, since: str, see: list, no: list) -> list[str]:
    return [f"🚨 Ничья о сервере {_n(name)} с {_when(since)}: половина голосующих его видит, половина — нет",
            "Видят: " + (_qnames(see) or "никто"), "Не видят: " + (_qnames(no) or "никто")]


def _tie_info(name: str, info: dict | None, said: str = "") -> str:
    if not info:
        return f"Справки о трафике {_n(name)} нет: видящие не на связи с этим сервером"
    hs = int(info.get("hs") or 0)
    flow = "трафик идёт" if info.get("flow") else "трафика нет"
    tail = f" (со слов {_qn(said)}, {_when(str(info.get('at') or ''))})" if said else ""
    return f"На {_n(name)}: на связи {hs} {plural_ru(hs, 'устройство', 'устройства', 'устройств')}, {flow}{tail}"


def quorum_tie_leader(name: str, reserve: str, since: str, see: list, no: list, info: dict | None) -> str:
    """Т-К5 (админу лидера, со звуком): ничья о моём сервере — поимённо, справка своя."""
    return "\n".join(_tie_head(name, since, see, no) + [_tie_info(name, info),
                      f"Сам бот пользователей не переведёт: проверь, доходит ли VPN до пользователей, и, если нет, — "
                      f"«🔀 Переезд на {_n(reserve)}»"])


def quorum_tie_candidate(name: str, since: str, see: list, no: list, info: dict | None, said: str, told_peer: bool) -> str:
    """Т-К5б (админу кандидата, со звуком)."""
    last = (f"Пользователи {_n(name)} сами сюда не переедут — решает администратор {_n(name)}, бот написал ему" if told_peer
            else f"Пользователи {_n(name)} сами сюда не переедут — решает администратор {_n(name)}, его администратор этот бот ещё не открывал — "
                 "написать ему некуда")
    return "\n".join(_tie_head(name, since, see, no) + [_tie_info(name, info, said), last])


def quorum_tie_peer(name: str, since: str, see: list, no: list, info: dict | None, said: str) -> str:
    """Т-К5в (админу спорного сервера в его роли, со звуком, кнопка peer_code)."""
    return "\n".join([f"🚨 Ничья о твоём сервере {_n(name)} с {_when(since)}",
                       "Видят: " + (_qnames(see) or "никто"), "Не видят: " + (_qnames(no) or "никто"), _tie_info(name, info, said),
                       f"Если {_n(name)} не доходит до пользователей, а в её бот не попасть, — пришли аварийный код"])


def quorum_tie_over(name: str, y: int, v: int, outcome: str, *, mine: bool = False) -> str:
    """Т-К6 (тем же адресатам, со звуком): чем кончилось — stayed | moved | fenced."""
    tail = {"moved": f"пользователи переехали{'' if mine else ' сюда'}",
            "fenced": f"{_n(name)} без кворума, бот не обрабатывает запросы"}.get(outcome, f"пользователи остались на {_n(name)}")
    return f"✅ Ничьей о {_n(name)} больше нет: видят {int(y)} из {int(v)} — {tail}"
