"""pick.py — часть пакета updates (см. __init__): выбор цели обновления роли
по списку релизов — минимумы, потолок поколения, условие на версию другой
роли, релизная ли сборка, пропущенные релизы."""
from __future__ import annotations

from typing import Callable, Optional

from awgbot.core import config

from .model import Release, parse_version
from .github import _up


# peer(need) → выполнено ли условие «другая сторона не ниже need»: версии
# другой роли знает вызывающий (домен), здесь — только выбор по списку.
Peer = Callable[[tuple], bool]


# decide(installed, releases, role, max_generation) → цель: выбор целиком у
# вызывающего — версии другой стороны он читает ПОСЛЕ ответа GitHub и решает
# под своим замком вместе с записью результата (domain/selfupdate/cache.py).
Decide = Callable[[tuple, list, str, Optional[int]], Optional[Release]]


def next_release(role: Optional[str] = None,
                 max_generation: Optional[int] = None,
                 peer: Optional[Peer] = None,
                 meta_cache: Optional[dict] = None,
                 decide: Optional[Decide] = None) -> Optional[Release]:
    """Цель обновления для ЭТОЙ РОЛИ, или None.

    Цель — последний релиз новее установленного, адресованный роли; чужие не
    в счёт: агент шлюза на 2.4.5 при релизах 2.4.6 (только main) и 2.4.7 (gw)
    идёт на 2.4.7. Минимум роли у любого из пропускаемых релизов, которого
    установленная версия не достигла, опускает цель до ближайшего релиза роли
    не ниже него; у той ступени может быть свой минимум — цепочка. Релизы с
    непрочитанными метаданными не рассматриваются вовсе (Release.usable).

    max_generation — потолок поколения AmneziaWG (идёт переезд: поставка дальше
    его цели недопустима, профили на трёх интерфейсах мигрировать нечем); релизы
    выше потолка не рассматриваются вовсе.

    peer — условие на версию другой роли (см. pick_target); без него условий
    нет. meta_cache — кэш метаданных вызывающего (list_releases). decide —
    выбор у вызывающего (вместо pick_target): зовётся только у релизной
    сборки — нерелизной решать не о чем, и её сохранённое не затирается.

    None означает «обновлять не на что / не от чего»:
      • установленная версия НЕ найдена среди тегов релизов → молчим навсегда
        (нерелизная сборка);
      • нет релиза новее установленной, адресованного этой роли (и под потолком).
    """
    installed = parse_version(config.INSTALLED_VERSION)
    if installed is None:
        return None
    role = (role if role is not None else config.ROLE) or ""
    releases = (_up().list_releases(meta_cache=meta_cache) if meta_cache is not None
                else _up().list_releases())
    if not _is_release_build(installed, releases):
        return None                      # нерелизная сборка → не трогаем (и кэш вызывающего тоже)
    if decide is not None:
        return decide(installed, releases, role, max_generation)
    return pick_target(installed, releases, role, max_generation, peer)


def _is_release_build(installed: tuple, releases: list) -> bool:
    """Установленная версия — из релизов? Точное совпадение тега либо та же
    база X.Y.Z у любого хотфикса X.Y.Z.N: снятый с GitHub релиз (так убрали
    v2.19.0 с битым усыновлением резолвера) не должен глушить обновления у
    тех, кто его успел поставить, — иначе они застревают на нём навсегда,
    а именно им хотфикс и нужен. Кастомная сборка с чужим номером по-прежнему
    молчит. releases — Release или кортежи версий."""
    vers = [getattr(r, "version", r) for r in releases]
    if any(v == installed for v in vers):
        return True
    base = installed[:3]
    return any(v[:3] == base and v != installed for v in vers)


def _barrier(installed: tuple, releases: list) -> Optional[tuple]:
    """Версия первого пропущенного релиза новее установленного (метаданные не
    прочитаны) или None. Это барьер для обеих ролей: адресаты, минимум и
    условие на другую роль у него неизвестны — прыгать через него нельзя,
    иначе хост перешагнул бы ступень, о которой ничего не знает."""
    return next((r.version for r in sorted(releases, key=lambda x: x.version)
                 if r.version > installed and not r.usable()), None)


def _newer(installed: tuple, releases: list, role: str,
           max_generation: Optional[int]) -> list:
    """Релизы новее установленного, адресованные роли, под потолком поколения
    и ниже первого пропущенного (_barrier)."""
    barrier = _barrier(installed, releases)
    newer = [r for r in releases if r.version > installed and r.applies_to(role)
             and (barrier is None or r.version < barrier)]
    if max_generation is not None:
        newer = [r for r in newer if r.awg_generation() <= max_generation]
    return newer


def _chain_need(installed: tuple, newer: list, target: Release, role: str) -> Optional[tuple]:
    """Наибольшее условие на другую сторону по цепочке: все адресованные роли
    релизы от установленного до target включительно. Хотфиксы условий не
    несут (Release.needs) — но и не обходят условие своей базы: оно в цепочке."""
    return max((r.needs(role) for r in newer
                if installed < r.version <= target.version and r.needs(role)), default=None)


def _gate(installed: tuple, newer: list, role: str, peer: Peer):
    """(новейший допустимый, новейший ждущий) — от новейшего к старшему, первый
    с выполненной цепочкой условий; первый невыполненный — тот, что ждёт."""
    waiting = None
    for target in reversed(newer):
        need = _chain_need(installed, newer, target, role)
        if need is None or peer(need):
            return target, waiting
        if waiting is None:
            waiting = target
    return None, waiting


def pick_target(installed: tuple, releases: list, role: str,
                max_generation: Optional[int] = None,
                peer: Optional[Peer] = None) -> Optional[Release]:
    """Чистый выбор цели по списку (см. next_release). releases — по возрастанию.

    peer — условие на другую сторону: цель — новейший релиз, у которого
    выполнены условия всей цепочки от установленного (§ «сверка» в шапке
    пакета); затем — понижение по минимумам, как без условия. Понижение
    условие не нарушает: цепочка пониженной цели — часть цепочки допустимой.
    Без peer — выбор как до сверки, байт в байт."""
    newer = _newer(installed, releases, role, max_generation)
    if not newer:
        return None
    target = newer[-1]
    if peer is not None:
        target, _ = _gate(installed, newer, role, peer)
        if target is None:
            return None
    while True:
        chain = [r for r in newer if r.version <= target.version]
        required = max((r.requires(role) for r in chain if r.requires(role)), default=None)
        if required is None or installed >= required:
            break
        lowered = next((r for r in chain if r.version >= required), None)
        if lowered is None or lowered.version == target.version:
            break
        target = lowered
    return target


def waiting_target(installed: tuple, releases: list, role: str,
                   max_generation: Optional[int] = None,
                   peer: Optional[Peer] = None) -> Optional[Release]:
    """Новейший релиз роли, который ждёт другую сторону (его цепочка условий
    не выполнена), или None. Без peer — ничего не ждёт."""
    if peer is None:
        return None
    newer = _newer(installed, releases, role, max_generation)
    return _gate(installed, newer, role, peer)[1] if newer else None


def chain_need(installed: tuple, releases: list, target: Release, role: str,
               max_generation: Optional[int] = None) -> Optional[tuple]:
    """Какая версия другой стороны нужна, чтобы дойти до target: наибольшее
    условие по цепочке от установленного. None — условий нет."""
    return _chain_need(installed, _newer(installed, releases, role, max_generation), target, role)


def skipped(installed: tuple, releases: list) -> list:
    """Релизы новее установленного, которые не рассматриваются: метаданные не
    прочитаны (битый ассет или его нет с META_ONLY_FROM). Чьи они — не знаем:
    адресаты как раз в метаданных; первый из них — барьер для выбора цели
    (_barrier)."""
    return [r for r in releases if r.version > installed and not r.usable()]
