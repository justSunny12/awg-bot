"""
awglock.py — манифест версии AmneziaWG (install/awg.lock) и ПОКОЛЕНИЕ, на
котором стоит этот хост (docs/ROADMAP.md, п.8).

Поколение — класс совместимости протокола. Версия ядра прибита к поставке, и
обновление бота ядро меняет; пока поколение то же, модуль подменяется на месте
и никто ничего не замечает. Растёт поколение — значит новое ядро не обслужит
клиентов прежнего, и нужен переезд профилей: рядом со старым интерфейсом
поднимается новый, люди переносят конфиги, старый гасится на финале.

ДВА ЧИСЛА, А НЕ ОДНО.
  • `generation()` — что привезла ПОСТАВКА (файл awg.lock рядом с кодом);
  • `applied_generation()` — на чём стоит ОСНОВНОЙ интерфейс (файл состояния
    хоста). Растёт только на финале переезда, не при обновлении.
  • `target_generation()` — куда едет ИДУЩИЙ переезд. Нужен, чтобы отличить
    «поставка совпадает с целью переезда» (обновляться можно) от «поставка
    ушла ещё дальше» (нельзя: профили размазались бы по трём интерфейсам).

Файл состояния — не вторая копия конфига: это состояние ХОСТА, как
DKMS-дерево. В app.yaml ему не место, иначе его пришлось бы мигрировать у
всех уже работающих установок.

УСЫНОВЛЕНИЕ. Нет файла состояния — считаем, что хост стоит на поколении
поставки. Это верно по построению: обновления идут по одной ступени
(updates.next_release), значит поставка, которая ВВОДИТ файл, приезжает
раньше любой, которая меняет поколение.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from awgbot.core import config

log = logging.getLogger("awgbot.awglock")

LOCK_PATH = config.BASE_DIR / "install" / "awg.lock"
# Рядом с conf, а не внутри: conf переживает обновление и уезжает в бэкап, а это
# состояние железа этого конкретного хоста.
STATE_PATH = Path(config.CONF_DIR).parent / "awg.state"

_KEY_APPLIED = "AWG_GENERATION_APPLIED"
_KEY_TARGET = "AWG_GENERATION_TARGET"


def _read_kv(path) -> dict:
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def lock() -> dict:
    """Манифест поставки. Пусто — поставка без манифеста (до v2.10.0)."""
    return _read_kv(LOCK_PATH)


def _int(raw: Optional[str]) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def generation() -> int:
    """Поколение ПОСТАВКИ. 0 — манифеста нет, поколениями не управляем."""
    return _int(lock().get("AWG_GENERATION"))


def module_tag() -> str:
    """Тег модуля из манифеста. Именно ТЕГ — тождество ведётся по нему:
    апстрим не бампает version.h, и у v3.1.20260812…0906 строка версии одна."""
    return lock().get("AWG_MODULE_TAG", "")


def built_module_tag() -> str:
    """Тег, из которого собран модуль на этом хосте (пишет установщик). Пусто —
    собирали не мы, и правды о теге неоткуда взять."""
    return _read_kv(STATE_PATH).get("AWG_MODULE_TAG_BUILT", "")


def protocol_id() -> str:
    """Идентификатор протокола для приложения из манифеста поставки. Уезжает в
    vpn:// и заморожен на поколение."""
    return lock().get("AWG_PROTOCOL_ID", "")


def applied_generation() -> int:
    """Поколение ОСНОВНОГО интерфейса. Нет файла состояния — усыновляем
    поколение поставки (см. докстринг модуля)."""
    st = _read_kv(STATE_PATH)
    if _KEY_APPLIED in st:
        return _int(st[_KEY_APPLIED])
    return generation()


def target_generation() -> int:
    """Поколение, куда едет идущий переезд. Нет — равно применённому."""
    st = _read_kv(STATE_PATH)
    if _KEY_TARGET in st:
        return _int(st[_KEY_TARGET])
    return applied_generation()


def write_state(applied: Optional[int] = None, target: Optional[int] = None) -> None:
    """Переписать файл состояния. None — оставить поле как есть; чтобы СНЯТЬ
    цель переезда, передай target=0."""
    st = _read_kv(STATE_PATH)
    if applied is not None:
        st[_KEY_APPLIED] = str(int(applied))
    if target is not None:
        if int(target) <= 0:
            st.pop(_KEY_TARGET, None)
        else:
            st[_KEY_TARGET] = str(int(target))
    body = ("# awg-bot: поколение AmneziaWG на этом хосте. Файл ведут установщик\n"
            "# и финал переезда профилей; руками править незачем.\n"
            + "".join(f"{k}={v}\n" for k, v in sorted(st.items())))
    try:
        Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_PATH).write_text(body, encoding="utf-8")
    except OSError as e:
        log.warning("awg.state не записан (%s): %s", STATE_PATH, e)


def needs_migration() -> bool:
    """Поставка привезла поколение новее, чем у основного интерфейса."""
    g = generation()
    return bool(g) and g > applied_generation()


def blocks_update(release_generation: int, migration_running: bool) -> bool:
    """Обновление недоступно? Только один случай: переезд ИДЁТ, а поставка
    ушла дальше его цели. Профили на трёх интерфейсах мигрировать нечем."""
    if not migration_running or not release_generation:
        return False
    return release_generation > target_generation()
