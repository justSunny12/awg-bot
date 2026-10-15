"""routing.py — часть пакета notices (см. __init__): замечание о шлюзе
РФ-доступа при запуске и включении, уведомления владельцу и держателям
переданных устройств о выдаче и отзыве РФ-доступа."""

from __future__ import annotations

from awgbot.util.textfmt import client_link


def gateways_full(current: int, maximum: int) -> str:
    """Отказ при полном наборе шлюзов (экран и всплывашка): сколько убрать,
    чтобы добавить ещё один — при полном наборе один."""
    n = max(1, int(current) - int(maximum) + 1)
    return f"Достигнуто максимальное количество шлюзов: убери {n}, чтобы добавить другой"


ROUTING_GW_DOWN_EFFECT = ("РФ-доступ включён, но пока не работает — российские сервисы временно "
                          "открываются с зарубежного адреса")


def routing_gateway_warning(verdict: str, *, at_start: bool, gw: str = "") -> str:
    """Замер шлюза не прошёл: одна формулировка для замечаний при запуске и для
    момента включения функции в настройках. gw — имя шлюза готовой разметкой
    (ссылка на карточку); без имени — «Шлюз РФ-доступа». PROBE_OK — пусто."""
    from awgbot.infra import routing as _rt
    # замечание при запуске — пункт списка под общей шапкой «⚠️ Замечания»,
    # уведомление при включении — само по себе, со значком
    mark = "" if at_start else "⚠️ "
    who = f"Шлюз {gw}" if gw else "Шлюз РФ-доступа"
    when = " на старте" if at_start else ""
    if verdict == _rt.PROBE_NO_PATH:
        return (f"{mark}{who} отвечает, но интернета за ним нет — чинить на самом шлюзе (аплинк, ip_forward, "
                f"MASQUERADE): {ROUTING_GW_DOWN_EFFECT}")
    if verdict != _rt.PROBE_OK:
        return f"{mark}{who} не отвечает{when}: {ROUTING_GW_DOWN_EFFECT}"
    return ""


# Владельцу профиля — админ выдал/отозвал разрешение на РФ-доступ.
_GRANTED_BODY = ("Российские приложения и сайты теперь работают и с включённым VPN. РФ-доступ включён на всех "
                 "твоих устройствах; выключить его или добавить свои сайты — в разделе «🇷🇺 РФ-доступ»")
_REVOKED_BODY = "Сайты и приложения, которые ругаются на VPN, теперь придётся открывать с выключенным VPN"

ROUTING_GRANTED_NOTICE = f"🇷🇺 К твоей подписке добавлен РФ-доступ 🎉\n{_GRANTED_BODY}"


def routing_granted_holder_notice(donor) -> str:
    return f"🇷🇺 К устройствам от профиля {client_link(donor)} добавлен РФ-доступ 🎉\n{_GRANTED_BODY}"


def routing_revoked_holder_notice(donor) -> str:
    return f"🇷🇺 РФ-доступ для устройств от профиля {client_link(donor)} больше недоступен 😔\n{_REVOKED_BODY}"


ROUTING_REVOKED_NOTICE = f"🇷🇺 РФ-доступ больше не входит в твою подписку 😔\n{_REVOKED_BODY}"
