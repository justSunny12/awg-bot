"""reserve_tail.py — часть пакета notices (см. __init__): уведомления журнала
данных резерва — хвост старой эпохи (Т-Ж4…Т-Ж6), строки Т-РУ5б о недошедших
правках, разрыв журнала при открытии, диск резерва. Общие правила — как у
reserve: имена серверов — «имя», без точки в конце, «один раз на расхождение».

Строки собираются готовыми к разметке: профиль и устройство — уже ссылками или
экранированным текстом (их собирает journal/tail), значения — экранируются здесь."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.textfmt.base import plural_ru

from .reserve import _e, _n

TAIL_LINES_MAX = 5

# (таблица, колонка) → как назвать правку в строке Т-Ж4 (по схеме); остальные — общей строкой без значений
_TAIL_FIELDS = {
    ("clients", "name"): "имя", ("clients", "tg_id"): "Telegram-аккаунт", ("clients", "device_limit"): "устройств не больше",
    ("clients", "activation_status"): "вход", ("clients", "routing_allowed"): "РФ-доступ",
    ("client_subscription", "period_end"): "срок до", ("client_subscription", "period_start"): "период с",
    ("client_subscription", "status"): "подписка", ("client_quota", "traffic_limit"): "квота",
    ("client_quota", "bonus_bytes"): "бонус", ("devices", "name"): "имя устройства", ("devices", "routing_on"): "РФ-доступ",
    ("device_traffic", "traffic_limit"): "лимит трафика",
}
_TAIL_DATES = ("period_end", "period_start")
_TAIL_TAKEN = {"tg_id": "этот Telegram-аккаунт уже у другого профиля", "invite_code": "такой код приглашения уже есть",
               "public_key": "такой ключ уже у другого устройства"}
_TAIL_BYTES = ("traffic_limit", "bonus_bytes")
_TAIL_FLAGS = ("routing_allowed", "routing_on")
_TAIL_WORDS = {"activation_status": {"pending": "не выполнен", "active": "выполнен"},
               "status": {"active": "активна", "expired": "истекла"}}


# ── строки Т-РУ5б и разрыв журнала ───────────────────────────────────────────

def jrn_lost(name: str, first: int, last: int) -> str:
    """Строка Т-РУ5б: возврат не дождался правок бота на резерве (срок вышел) — они не
    применены; сколько их — по номерам."""
    n = last - first + 1
    return (f"⚠️ {n} {plural_ru(n, 'правка', 'правки', 'правок')} бота на {_n(name)} не {plural_ru(n, 'дошла', 'дошли', 'дошли')} "
            f"до возврата — профили и устройства, изменённые там в последние минуты, проверь глазами")


def jrn_mine_lost(name: str, first: int, last: int) -> str:
    """Строка Т-РУ5б: гость открылся с разрывом журнала (резерв не дочитал записи) — мои
    решения между его точкой и подтверждённым он не видел, его правки легли поверх."""
    n = last - first + 1
    return (f"⚠️ {n} {plural_ru(n, 'моё решение', 'моих решения', 'моих решений')} перед переездом до базы бота на {_n(name)} "
            f"не {plural_ru(n, 'дошло', 'дошли', 'дошли')} (журнал на резерве прочитан не целиком) — его правки легли поверх, проверь глазами")


def returned_without_guest_tail(name: str) -> str:
    """Строка Т-РУ5б на журнальном пути: гостя на резерве уже не было, всё подтверждённое
    применено в следовании, а последние его записи могли не дойти."""
    return f"ℹ️ Гостя на {_n(name)} уже не было — последние правки там могли не дойти сюда"


def keep_write_failed(name: str, why: str, no_space: bool = False) -> str:
    """Резерв не может дописать журнал второй стороны (диск): её правки у него не копятся,
    данные для переезда стареют. Совет освободить место — только когда его нет (ENOSPC);
    прочие ошибки ввода-вывода — проверить диск."""
    fix = "освободи место на диске" if no_space else "проверь диск"
    return (f"⚠️ Журнал правок {_n(name)} здесь не пишется: {_e(why)} — его данные для переезда не обновляются; "
            f"{fix}, запись возобновится сама")


def open_gap(name: str, gap: int, lost: int) -> str:
    """Админу резерва: база бота основы собрана не по всему её журналу."""
    what = []
    if gap:
        what.append(f"не прочитано записей: {gap}")
    if lost:
        what.append(f"не встало событий: {lost}")
    return (f"⚠️ База бота {_n(name)} собрана не по всему журналу ({'; '.join(what)}) — последние правки основы "
            f"до аварии в ней могут отсутствовать; основа узнает об этом при встрече")


def tail_self_failed(name: str, why: str) -> str:
    """Свой хвост после переезда на резерв не переносится сюда несколько раз подряд
    (сбой базы): правки не потеряны — лежат в хвосте, перенос повторяется."""
    return (f"⚠️ Правки, сделанные здесь перед переездом на {_n(name)}, пока не перенесены: {_e(why)} — "
            f"повторяю каждую минуту; если не проходит — <code>journalctl -u awg-bot</code>")


# ── хвост старой эпохи (Т-Ж4…Т-Ж6) ───────────────────────────────────────────

def _date_yy(iso: str) -> str:
    """Дата с годом двумя цифрами: «15.11.26»; ISO-дата без времени — тоже."""
    from datetime import date
    try:
        return timeutil.fmt_date_yy(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(str(iso)[:10]).strftime("%d.%m.%y")
        except ValueError:
            return str(iso)


def _tail_value(col: str, v) -> str:
    if v is None or v == "":
        return "—"
    if col in _TAIL_WORDS and str(v) in _TAIL_WORDS[col]:
        return _TAIL_WORDS[col][str(v)]
    if col in _TAIL_DATES and isinstance(v, str):
        return _e(_date_yy(v))
    if col == "name":
        return f"«{_e(v)}»"
    if col in _TAIL_FLAGS:
        return "вкл" if v else "выкл"
    if col in _TAIL_BYTES:
        if not v:
            return "без ограничения"
        from awgbot.util.textfmt.base import volume
        return _e(volume(int(v)))
    return _e(str(v))


def tail_where(variant: str, reserve: str) -> str:
    """Где осталось значение, которое победило: в аварии и при возврате — на резерве, в С9 — здесь."""
    return "здесь" if variant == "c9" else f"на {_n(reserve)}"


def tail_line(d: dict, profile: str, device: str, taken_by: str, reserve: str, variant: str) -> str:
    """Строка Т-Ж4 по записи спорного (rsvjrn.rebase): profile — имя профиля (ссылкой или
    экранированным текстом), device — экранированное имя устройства (строка про
    устройство), taken_by — «устройство «Pixel» профиля Анна» (кто занял уникальное)."""
    where = tail_where(variant, reserve)
    kind, table, col = str(d.get("kind") or ""), str(d.get("table") or ""), str(d.get("col") or "")
    if device:
        subject = f"устройство «{device}»" + (f" профиля {profile}" if profile and profile != device else "")
    else:
        subject = f"профиль {profile or '?'}"
    if kind == "unique":
        made = f"заведённый на {_n(reserve)}" if variant != "c9" else "заведённый здесь"
        return (f"{subject} — его адрес занял {taken_by}, {made}. Выдай «{device}» конфигурацию заново"
                if col == "address" else f"{subject}: не встало — {_TAIL_TAKEN.get(col, 'такое значение уже занято')}")
    if kind == "delete":
        tail = ". Выдай конфигурацию заново" if table == "devices" else ""   # закрытого ключа после отката нет
        return f"{subject}: удаление не перенесено — {where} его успели поменять{tail}"
    if kind == "gone":
        return f"{subject}: правка не перенесена — {where} этой записи уже нет"
    if kind == "failed":
        return f"{subject}: правка не встала"
    if col in ("private_key", "preshared_key", "public_key"):
        return f"{subject}: ключи поменяли и там, и здесь — {where} остались свои. Выдай конфигурацию заново"
    if d.get("secret"):
        return f"{subject}: код поменяли и там, и здесь — {where} остался свой"
    if col == "block_reason":
        return f"{subject}: блокировку поменяли и там, и здесь — {where} осталась своя"
    field = _TAIL_FIELDS.get((table, col))
    if field is None:
        return f"{subject}: правку поменяли и там, и здесь — {where} осталась своя"
    tail, kept = _tail_value(col, d.get("tail")), _tail_value(col, d.get("kept"))
    if col in _TAIL_DATES and field.endswith(" до"):
        return f"{subject}: {field} {tail} — {where} осталось до {kept}"
    return f"{subject}: {field} {tail} — {where} осталось {kept}"


def _tail_head(variant: str, reserve: str, n: int) -> str:
    word = f"{n} {plural_ru(n, 'правка', 'правки', 'правок')}"
    verb = plural_ru(n, "перенесена", "перенесены", "перенесены")
    made = plural_ru(n, "сделанная", "сделанные", "сделанные")
    if variant == "c9":
        return (f"⚠️ Не {verb} {word} бота на {_n(reserve)}, {made} после потери связи: здесь те же поля "
                "успели поменять по-своему")
    return (f"⚠️ Не {verb} {word}, {made} здесь перед переездом на {_n(reserve)}: те же поля там успели "
            "поменять по-своему")


def tail_disputed(variant: str, reserve: str, lines: list[str], total: int, more: str = "") -> str:
    """Т-Ж4: хвост старой эпохи со спорным — пять строк и «и ещё N» (more — готовая
    строка-хвост ссылкой на снимок всего списка; пусто — текстом)."""
    shown = lines[:TAIL_LINES_MAX]
    text = _tail_head(variant, reserve, total) + "".join(f"\n• {line}" for line in shown)
    rest = int(total) - len(shown)
    if rest > 0:
        text += "\n" + (more or f"… и ещё {rest}")
    return text


def tail_snap_rest(reserve: str) -> str:
    """Хвост снимка Т-Ж4 после аварии: строк больше, чем прислал резерв, — остальные в журнале его бота."""
    return f"в журнале бота на {_n(reserve)}"


def tail_snap_title(variant: str, reserve: str) -> str:
    """Заголовок снимка всего списка Т-Ж4 (экран «весь список»)."""
    if variant == "c9":
        return f"⚠️ Не перенесены правки бота на {_n(reserve)}"
    return f"⚠️ Не перенесены правки, сделанные здесь перед переездом на {_n(reserve)}"


def tail_moved(variant: str, reserve: str, n: int) -> str:
    """Т-Ж5: хвост перенесён целиком (self — свой хвост перенесён здесь после возврата: бот на
    резерве его не принял)."""
    if variant == "c9":
        return f"ℹ️ Правки бота на {_n(reserve)} после потери связи перенесены сюда: {int(n)}"
    if variant == "self":
        return f"ℹ️ Правки, сделанные здесь перед переездом на {_n(reserve)}, перенесены после возврата: {int(n)}"
    return f"ℹ️ Правки, сделанные здесь перед переездом на {_n(reserve)}, перенесены туда: {int(n)}"


def follow_stalled(reserve: str, why: str) -> str:
    """Т-Ж6: основа в следовании перестала догонять бот на резерве."""
    return f"⚠️ Данные здесь перестали догонять бот на {_n(reserve)}: {_e(why)} — возврат подождёт"


def follow_ok(reserve: str) -> str:
    return f"🟢 Данные здесь снова идут за ботом на {_n(reserve)}"
