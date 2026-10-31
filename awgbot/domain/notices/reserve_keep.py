"""reserve_keep.py — часть пакета notices (см. __init__): приём пользователей
основы сорвался после того, как процесс открытия на резерве сменил её запись
DNS — зона не снимается (domain/services/reserve/inbound_keep, outbound_keep).
Админу резерва — что не доделано и что делаю; админу основы (на связи) — то же
с её стороны; запись, возвращённая основой на себя. Правила — как у reserve:
имена серверов «имя», имена записей и адреса моноширинным, без точки в конце."""
from __future__ import annotations

from .reserve import _e, _n, hosting_without_bot

_TODO = {"record": "смена записи DNS не подтвердилась", "serve": "вход вебхука в зону не настроен",
         "tg": "бот здесь не отвечает в Telegram"}
_RETRY = {"record": "проверяю запись раз в минуту", "serve": "повторяю раз в минуту"}


def kept(primary: str, kind: str, reason: str) -> str:
    """Админу резерва: запись уже здесь, приём сорвался, зона остаётся."""
    if kind == "tg":
        return hosting_without_bot(primary, reason)
    if kind == "relaunch":
        return (f"⚠️ Запись DNS пользователей {_n(primary)} уже на тебе, а клона нет: {_e(reason)} — "
                f"перезапускаю открытие раз в минуту")
    return (f"⚠️ Пользователи {_n(primary)} у тебя, VPN работает, а {_TODO.get(kind, 'переезд не доделан')}: "
            f"{_e(reason)} — {_RETRY.get(kind, 'повторяю раз в минуту')}")


def peer_kept(reserve: str, kind: str, reason: str) -> str:
    """Админу основы: её пользователи уже идут на резерв, а там сбой — резерв повторяет сам."""
    if kind == "relaunch":
        return (f"⚠️ Твоя запись DNS уже на {_n(reserve)}, а клона там нет: {_e(reason)} — "
                f"{_n(reserve)} перезапускает открытие")
    what = {"record": "смена записи не подтвердилась", "serve": "вход вебхука не настроен",
            "tg": f"твой бот на {_n(reserve)} не отвечает в Telegram"}.get(kind, "переезд не доделан")
    return f"⚠️ Твои пользователи на {_n(reserve)}, VPN работает, а {what}: {_e(reason)} — {_n(reserve)} повторяет сам"


def record_back(reserve: str, record: str, own: str, why: str) -> str:
    """Админу основы: резерв не принял её пользователей после смены записи — запись на себя."""
    return (f"↩️ Запись <code>{_e(record)}</code> снова на <code>{_e(own)}</code>: {_n(reserve)} не принял твоих "
            f"пользователей" + (f" — {_e(why)}" if why else ""))
