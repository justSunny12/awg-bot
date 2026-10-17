"""common.py — часть пакета admin (см. __init__): payload'ы экранов, месяц, итог трафика, нарезка списков."""

from __future__ import annotations

from awgbot.util import timeutil
from ..fmt import human_bytes, updown_brief


RF_PAYLOAD = "traffic_local"     # /start traffic_local[-<id>] — прежние ссылки на экраны РФ
TRAFFIC_PAYLOAD = "traffic"      # /start traffic[-<id>] — экраны трафика
UPD_PAYLOAD = "upd"              # /start upd — раздел обновлений
UNASSIGNED_PAYLOAD = "unassigned"
ONLINE_PAYLOAD = "online"
EXPIRING_PAYLOAD = "expiring"


_MONTHS_ACC = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
               "сентябрь", "октябрь", "ноябрь", "декабрь")


def month_label() -> str:
    """«сентябрь» — текущий месяц в заголовках трафика («Трафик за сентябрь»):
    месяц всегда текущий, а текущий год не пишем (прошлый был бы «09.25»)."""
    return _MONTHS_ACC[timeutil.now().month - 1]


def _total(rx: int, tx: int) -> str:
    """«123.4 ГБ (↑12.1 ↓111.3)»; ноль — без стрелок."""
    total = int(rx) + int(tx)
    return f"{human_bytes(total)} {updown_brief(rx, tx)}" if total else human_bytes(0)


# Списки с эмодзи в начале строк: подряд строки сливаются, а межстрочный
# интервал Telegram не настраивает. Единственный рычаг — пустая строка.
_LIST_SEP = "\n\n"
# Экран — не отчёт: длинный список режется, хвост — счётчиком; иначе текст
# упёрся бы в 4096 знаков Telegram примерно на трёх десятках записей
_LIST_CAP = 25


def _more(n: int) -> str:
    return f"{_LIST_SEP}… и ещё {n - _LIST_CAP}" if n > _LIST_CAP else ""


LIMIT_REACHED = "Достигнут лимит устройств"


def refreshed_line(bot_username: str, payload: str, at=None) -> str:
    """Последняя строка живого экрана (главная админа, «📶 Онлайн»):
    «<i>обновлено 15.10 08:08:14 (UTC+5) 🔄</i>» — общая строка обеих ролей
    (textfmt.refreshed_link). at — момент снимка (None — сейчас)."""
    from ..fmt import refreshed_link
    return refreshed_link(bot_username, payload, at)


REFRESH_PAYLOAD = "refresh"      # /start refresh — перечитать статус и перерисовать главную
