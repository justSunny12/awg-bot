"""common.py — общее для частей канала: журнал, обрезка значений, строки расхождения, замок слияния своих списков."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("awgbot.domain.services.gwchannel")


def _cut(v: str) -> str:
    return v if len(v) <= 160 else v[:157] + "…"


def drift_lines(items: list, html: bool) -> list[str]:
    """Строки расхождения «что выдаёт сервер — что стоит на шлюзе» из структуры
    (ключ, человеческое имя, у сервера, на шлюзе). Режим без VPN — словами, а
    не 1/0; подсети и адреса — в <code>, когда строка идёт в HTML. Живёт в
    домене: уведомления собираются здесь же, а слой текстов только берёт."""
    import html as _html
    esc = (lambda s: _html.escape(str(s), quote=False)) if html else (lambda s: str(s))
    out = []
    for key, human, mine, theirs in items:
        if key == "LAN_MODE":
            fmt = lambda v: {"1": "включён", "0": "выключен"}.get(v, esc(v) if v else "—")   # noqa: E731
        elif html:
            fmt = lambda v: f"<code>{esc(v)}</code>" if v else "«—»"            # noqa: E731
        else:
            fmt = lambda v: f"«{v}»" if v else "«—»"                              # noqa: E731
        out.append(f"{esc(human)}: у сервера {fmt(mine)}, на шлюзе {fmt(theirs)}")
    return out



_OWN_LOCK = threading.RLock()  # слияние своих списков: два слота — два потока; создание канона — внутри
