"""
routing_doctor.py — послойная проверка тракта условной маршрутизации.

Зачем отдельный инструмент. Отказы этой фичи выглядят одинаково («не тот адрес»
или «интернет отвалился»), а причин у них штук десять на четырёх машинах: ВПС,
контейнер Amnezia, линк-туннель, сам шлюз. Разбирать это по логам бота нельзя —
бот видит только свой слой. Поэтому проверка идёт снизу вверх, и КАЖДЫЙ слой
докладывает отдельно: первый упавший и есть место ремонта.

Ничего не чинит и не меняет — только читает. Запускать можно в любой момент,
включая момент отказа.
"""
from __future__ import annotations

import sys

from awgbot.core import config
from awgbot.infra import routing

_OK, _BAD, _WARN = "  ок  ", " СБОЙ ", " ??? "


def _line(mark: str, title: str, detail: str = "") -> None:
    print(f"[{mark}] {title}" + (f"\n         {detail}" if detail else ""))


def _probe_layers() -> list[tuple[str, str, str]]:
    """Слои снизу вверх. Возвращает (метка, заголовок, подробность)."""
    out: list[tuple[str, str, str]] = []

    # ── 0. включена ли фича вообще ──
    if not config.ROUTING_GW_INTERFACE:
        return [(_WARN, "Фича спит: gw_interface пуст",
                 "Это штатное состояние, если условная маршрутизация не нужна.")]
    out.append((_OK, f"Интерфейс линка: {config.ROUTING_GW_INTERFACE}", ""))

    # ── 1. обвяз на месте ──
    ok, why = routing.self_check(force=True)
    out.append((_OK if ok else _BAD, "Обвяз хоста (ipset/iptables/ip/dnsmasq)", why))
    if not ok:
        out.append((_WARN, "Дальше не проверяем", "Сначала почини обвяз."))
        return out

    # ── 2. линк поднят ──
    age = routing.link_handshake_age()
    peer = routing.link_peer_address()
    if age is None:
        out.append((_BAD, "Линк до шлюза", "Интерфейс не отвечает или пира нет."))
    else:
        out.append((_OK, f"Линк поднят, хендшейку {age} с",
                    f"Адрес шлюза в линке: {peer or 'не определён'}"))

    # ── 3. постоянная обвязка — ДО зонда ──
    # Порядок принципиален: зонд ходит по метке, то есть через эту самую
    # таблицу. Проверять её после зонда бессмысленно — а раньше здесь было
    # хуже: зонд сам доводил обвязку, и «ip rule есть» означало лишь, что его
    # только что создали. Отсутствие правила не обнаруживалось никогда.
    tbl = routing.table_route()
    out.append((_OK if tbl else _BAD,
                f"Маршрут в таблице {config.ROUTING_TABLE}: {tbl or 'НЕТ'}",
                "" if tbl else "Помеченному трафику некуда идти."))
    rule = routing.rule_present()
    out.append((_OK if rule else _BAD,
                f"ip rule по метке (постоянная обвязка): {'есть' if rule else 'НЕТ'}",
                "" if rule else "Метка никуда не ведёт — таблица не выбирается."))
    # Без клампа отказ обманчив: пинг и DNS ходят (пакеты мелкие), а страницы
    # не грузятся — путь на шлюз инкапсулирован дважды.
    clamp = routing.mss_clamp_present()
    out.append((_OK if clamp else _BAD,
                f"MSS-кламп на {config.ROUTING_GW_INTERFACE}",
                "" if clamp else "Без него мелкие пакеты ходят, а страницы не грузятся."))

    # ── 4. САМОЕ ВАЖНОЕ: ходит ли трафик НАРУЖУ через шлюз ──
    # Именно тут расходятся «туннель поднят» и «трафик проходит»: у шлюза может
    # быть жив туннель и лежать аплинк — тогда пункт 2 зелёный, а интернета у
    # пользователей нет.
    target = "77.88.8.8"
    verdict = routing.probe_gateway(target)   # TCP с меткой — путь клиентов
    if verdict == routing.PROBE_OK:
        out.append((_OK, f"Через шлюз проходит наружу ({target})", ""))
    elif not (tbl and rule):
        # Обвязки нет — значит зонд не дошёл даже до шлюза, и его вердикт не про
        # шлюз. Назвать это отказом шлюза значило бы отправить чинить не туда.
        out.append((_WARN, "Проходимость шлюза не проверена",
                    "Сначала маршрут и ip rule: без них зонд идёт не тем путём."))
    elif verdict == routing.PROBE_NO_PATH:
        # Называем ИСТОЧНИК: чаще всего дело именно в нём — шлюз маскарадит
        # клиентскую подсеть, а зонд идёт с адреса линка, и без маскарада для
        # него пакеты уходят наружу с немаршрутизируемым адресом.
        src = routing.probe_source() or "?"
        out.append((_BAD, "Шлюз отвечает, но интернета за ним нет",
                    f"Зонд идёт с адреса {src}. Проверь НА ШЛЮЗЕ, что он "
                    f"маскарадится: iptables -t nat -L POSTROUTING -n. "
                    f"Дальше — аплинк и ip_forward."))
    else:
        out.append((_BAD, "Шлюз не отвечает вовсе",
                    "Чинить ЛИНК: awg-quick, порт, endpoint, firewall."))

    # ── 5. рубильник ──
    # Снятым он бывает законно: так и выглядит деградация.
    hook = routing.hook_present()
    out.append((_OK if hook else _WARN,
                f"Рубильник маркировки (хук в PREROUTING): {'включён' if hook else 'снят'}",
                "" if hook else "Снят — бот считает шлюз непроходимым."))

    # ── 6. наборы ──
    sets = routing.list_sets()
    users = [n for n in sets if n.startswith(config.ROUTING_SET_USER_PREFIX)]
    if not users:
        out.append((_WARN, "Пользовательских наборов нет",
                    "Никто не включил режим — или реконсиляция не отработала."))
    for name in sorted(users):
        n = routing.set_count(name)
        out.append((_OK if n else _BAD, f"Набор {name}: {n} записей",
                    "" if n else "Пустой набор ⇒ на шлюз уйдёт ВСЁ, включая заблокированное."))
    return out


def main() -> int:
    print("Диагностика условной маршрутизации. Слои снизу вверх;")
    print("первый СБОЙ — и есть место ремонта.\n")
    bad = 0
    try:
        for mark, title, detail in _probe_layers():
            _line(mark, title, detail)
            bad += mark == _BAD
    except routing.RoutingError as e:
        _line(_BAD, "Проверка сорвалась", str(e))
        return 2
    print()
    print("Отказов не найдено." if not bad else f"Отказов: {bad}. Чини первый сверху.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
