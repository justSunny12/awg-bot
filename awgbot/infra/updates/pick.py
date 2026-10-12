"""pick.py — часть пакета updates (см. __init__): выбор цели обновления роли
по списку релизов — минимумы, потолок поколения, релизная ли сборка."""
from __future__ import annotations

from typing import Optional

from awgbot.core import config

from .model import Release, parse_version
from .github import _up


def next_release(role: Optional[str] = None,
                 max_generation: Optional[int] = None) -> Optional[Release]:
    """Цель обновления для ЭТОЙ РОЛИ, или None.

    Цель — последний релиз новее установленного, адресованный роли (хэштег
    роли); чужие не в счёт: агент шлюза на 2.4.5 при релизах 2.4.6 (только
    main) и 2.4.7 (gw) идёт на 2.4.7. Минимум роли (`#requires_<роль>_X`) у
    любого из пропускаемых релизов, которого установленная версия не достигла,
    опускает цель до ближайшего релиза роли не ниже него; у той ступени может
    быть свой минимум — цепочка.

    max_generation — потолок поколения AmneziaWG (идёт переезд: поставка дальше
    его цели недопустима, профили на трёх интерфейсах мигрировать нечем); релизы
    выше потолка не рассматриваются вовсе.

    None означает «обновлять не на что / не от чего»:
      • установленная версия НЕ найдена среди тегов релизов → молчим навсегда
        (нерелизная сборка);
      • нет релиза новее установленной, адресованного этой роли (и под потолком).
    """
    installed = parse_version(config.INSTALLED_VERSION)
    if installed is None:
        return None
    role = (role if role is not None else config.ROLE) or ""
    releases = _up().list_releases()
    if not _is_release_build(installed, releases):
        return None                      # нерелизная сборка → не трогаем
    return pick_target(installed, releases, role, max_generation)


def _is_release_build(installed: tuple, releases: list) -> bool:
    """Установленная версия — из релизов? Точное совпадение тега либо та же
    база X.Y.Z у любого хотфикса X.Y.Z.N: снятый с GitHub релиз (так убрали
    v2.19.0 с битым усыновлением резолвера) не должен глушить обновления у
    тех, кто его успел поставить, — иначе они застревают на нём навсегда,
    а именно им хотфикс и нужен. Кастомная сборка с чужим номером по-прежнему
    молчит."""
    if any(r.version == installed for r in releases):
        return True
    base = installed[:3]
    return any(r.version[:3] == base and r.version != installed for r in releases)


def pick_target(installed: tuple, releases: list, role: str,
                max_generation: Optional[int] = None) -> Optional[Release]:
    """Чистый выбор цели по списку (см. next_release). releases — по возрастанию."""
    newer = [r for r in releases if r.version > installed and r.applies_to(role)]
    if max_generation is not None:
        newer = [r for r in newer if r.awg_generation() <= max_generation]
    if not newer:
        return None
    target = newer[-1]
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
