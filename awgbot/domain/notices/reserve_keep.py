"""reserve_keep.py — часть пакета notices (см. __init__): приём пользователей
основы сорвался после того, как процесс открытия на резерве сменил её запись
DNS — зона не снимается (domain/services/reserve/inbound_keep, outbound_keep).
Админу резерва — что не доделано и что делаю; админу основы (на связи) — то же
с её стороны; запись, возвращённая основой на себя. Правила — как у reserve:
имена серверов «имя», имена записей и адреса моноширинным, без точки в конце."""
from __future__ import annotations

from .reserve_fmt import _e, _n
from .reserve_move import hosting_without_bot

_TODO = {"record": "смена DNS-записи не подтвердилась",
         "serve": "трафик Telegram не доходит до гостевого экземпляра"}   # tg — hosting_without_bot
_RETRY = {"record": "проверяю запись раз в минуту", "serve": "перенастраиваю проброс и повторяю раз в минуту"}


def kept(primary: str, kind: str, reason: str) -> str:
    """Админу резерва: запись уже здесь, приём сорвался, зона остаётся."""
    if kind == "tg":
        return hosting_without_bot(primary, reason)
    if kind == "relaunch":
        return (f"⚠️ DNS-запись пользователей {_n(primary)} уже указывает на этот сервер, но интерфейс резерва не "
                f"запущен: {_e(reason)} — перезапускаю открытие раз в минуту")
    return (f"⚠️ Пользователи {_n(primary)} переведены сюда, VPN работает, но "
            f"{_TODO.get(kind, 'переезд не завершён')}: {_e(reason)} — {_RETRY.get(kind, 'повторяю раз в минуту')}")


def peer_kept(reserve: str, kind: str, reason: str) -> str:
    """Админу основы: её пользователи уже идут на резерв, а там сбой — резерв повторяет сам."""
    if kind == "relaunch":
        return (f"⚠️ Твоя DNS-запись уже переключена на {_n(reserve)}, но интерфейс резерва там не запущен: "
                f"{_e(reason)} — {_n(reserve)} перезапускает сетевой интерфейс")
    what = {"record": "смена DNS-записи не подтвердилась", "serve": "трафик Telegram не доходит до гостевого экземпляра",
            "tg": f"гостевой экземпляр на {_n(reserve)} не отвечает в Telegram"}.get(kind, "переезд не завершён")
    tail = {"record": "автоматически повторяет проверку", "serve": "настраивает проброс повторно",
            "tg": "пытается восстановить связь"}.get(kind, "повторяет попытку")
    return f"⚠️ Твои пользователи на {_n(reserve)}, VPN работает, но {what}: {_e(reason)} — {_n(reserve)} {tail}"


def record_back(reserve: str, record: str, own: str, why: str) -> str:
    """Админу основы: резерв не принял её пользователей после смены записи — запись на себя."""
    return (f"↩️ Запись <code>{_e(record)}</code> возвращена на <code>{_e(own)}</code>: {_n(reserve)} не смог принять "
            "пользователей" + (f" — {_e(why)}" if why else ""))


# ── исход смены записи процессом открытия неизвестен (inbound_recwait) ───────

def record_unknown(primary: str, record: str, ip: str, minutes: int, why: str) -> str:
    """Админу резерва: вход в ожидание факта — ответ провайдера не дошёл или процесс открытия погиб."""
    rec = f"Запись <code>{_e(record)}</code>" if record else "DNS-запись"
    to = f" на <code>{_e(ip)}</code>" if ip else ""
    return (f"⏳ {rec} пользователей {_n(primary)}: успешность смены{to} не подтверждена" + (f" ({_e(why)})" if why else "")
            + f". Проверяю авторитетные DNS-серверы раз в минуту (до {minutes} мин)")


def record_confirmed(primary: str, record: str, ip: str) -> str:
    """Админу резерва: факт после ожидания — запись здесь, приём идёт дальше."""
    rec = f" <code>{_e(record)}</code>" if record else ""
    what = (f"адрес <code>{_e(ip)}</code>" + (f" для{rec}" if rec else "")) if ip else f"обновление записи{rec}"
    return f"✅ Авторитетный DNS-сервер подтвердил {what} — перевод пользователей {_n(primary)} продолжается"
