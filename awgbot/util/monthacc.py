"""
monthacc.py — месячный накопитель дельт счётчиков интерфейса; обе роли.

Счётчики `rx`/`tx` живут от подъёма интерфейса и обнуляются каждым
рестартом. Накопитель копит дельты в месячный итог: счётчик меньше прошлого
— значит, обнулился, и дельта — весь текущий. Новый календарный месяц
начинает итог заново; записанный месяц новее «сейчас» (пояс сменился на
западный в первые часы месяца) — итог тот же, не обнуляется.

Чистая функция над словарём состояния: агент копит так трафик линка
(`gw_traffic`), сервер — трафик клиентов через резерв (`rsv_traffic`).
"""
from __future__ import annotations


def account(acc: dict | None, rx: int, tx: int, month: str) -> dict:
    """Новое состояние накопителя после замера `rx`/`tx` в месяце `month`
    (`YYYY-MM`). Ключи: `month`, `rx`, `tx` — итог месяца; `last_rx`,
    `last_tx` — прошлый замер."""
    acc = dict(acc or {})
    if str(acc.get("month") or "") < month:
        acc = {"month": month, "rx": 0, "tx": 0,
               "last_rx": acc.get("last_rx", 0), "last_tx": acc.get("last_tx", 0)}
    last_rx, last_tx = int(acc.get("last_rx", 0)), int(acc.get("last_tx", 0))
    d_rx = rx - last_rx if rx >= last_rx else rx
    d_tx = tx - last_tx if tx >= last_tx else tx
    acc["rx"] = int(acc.get("rx", 0)) + max(0, d_rx)
    acc["tx"] = int(acc.get("tx", 0)) + max(0, d_tx)
    acc["last_rx"], acc["last_tx"] = int(rx), int(tx)
    return acc
