"""status.py — часть пакета rsvproc (см. __init__): ход процесса открытия
строкой stdout и open.status, отказ с полями сменённой записи DNS, OpenError."""
from __future__ import annotations


def _p(text: str) -> None:
    print(text, flush=True)


class OpenError(RuntimeError):
    """`pre_m` — отказ до сборки ключа основы `M` (кода не было, не подошёл, точек мало,
    пакета нет): ключ основы мне не стал известен. Признак — полем итога (open.status
    `pre_m`), бот резерва по нему решает об отметке `st.exposed`, не по тексту причины."""
    def __init__(self, reason: str, *, pre_m: bool = False):
        super().__init__(reason)
        self.pre_m = pre_m


def _step(step: str, **fields) -> None:
    from awgbot.infra import rsvzone
    rsvzone.open_status_write(step, **fields)
    _p(step)


# запись уже сменена этим процессом (после `set_record`): любой отказ дальше несёт поля
# record=set, name, ip — бот резерва по ним зону не снимает и доделывает сам (inbound_keep)
_RECORD: dict = {}


def _fail(reason: str, *, pre_m: bool = False) -> int:
    from awgbot.infra import rsvzone
    rsvzone.open_status_write("fail", reason=reason, pre_m="1" if pre_m else "", **_RECORD)
    _p(f"fail {reason}")
    return 1


def _fail_err(e: OpenError) -> int:
    return _fail(str(e), pre_m=bool(getattr(e, "pre_m", False)))

