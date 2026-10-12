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


def month_label() -> str:
    """«09.26» — текущий месяц в заголовках трафика (год — всегда)."""
    return timeutil.now().strftime("%m.%y")


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
