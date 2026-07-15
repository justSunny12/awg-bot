"""Единый ГОРЯЧИЙ доступ к conf/*.yaml.

YAML — единственный источник истины (БД-настроек не заводим). Все conf/*.yaml
грузятся в один in-memory кэш; `get(dotted_key)` читает из кэша, а `set()` и
вотчдог его обновляют — значение всегда свежее без рестарта.

Dotted-ключ = «<файл>.<путь-внутри-файла>»: первый сегмент — имя conf-файла без
расширения, дальше вложенный путь. Примеры:
    limits.traffic_bonus_gb          → conf/limits.yaml  → traffic_bonus_gb
    app.network.ssh_port             → conf/app.yaml     → network → ssh_port
    quiet_hours.quiet_hours_start    → conf/quiet_hours.yaml → quiet_hours_start

Применение изменений — пофайловый diff: `apply_changes()` возвращает список
ИЗМЕНИВШИХСЯ dotted-ключей, и runtime дёргает on-change реакции только по ним
(«нехер дёргать неизменённое»). Классы применения (живут в реестре on_change, не
здесь): (1) читается в точке использования → get() уже отдаёт новое, реакции
нет; (2) захвачено в живом объекте (job APScheduler) → on-change перестраивает;
(3) вплавлено в инициализацию (TZ/деплой/секреты) → сюда не входит, только
рестарт.

Запись — через ruamel.yaml (round-trip: комментарии и порядок целы), атомарно
(temp+rename) и под меткой самозаписи, чтобы вотчдог conf/ не считал её внешним
изменением.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import yaml  # чтение (быстро, стдлиб-совместимо); запись — ruamel ниже

log = logging.getLogger("awgbot.settings")

# ── состояние модуля ─────────────────────────────────────────────────────────
_lock = threading.RLock()
_cache: dict[str, dict] = {}          # stem файла → распарсенный dict
_conf_dir: Optional[Path] = None
_self_writing = threading.Event()     # True, пока пишем сами (вотчдог игнорит)

# реестр on-change: (ключ-или-префикс) → колбэки. Префикс матчит по «key.»/«key».
_on_change: list[tuple[str, Callable[[str, Any], None]]] = []


# ── загрузка/кэш ─────────────────────────────────────────────────────────────
_PARSE_ERROR = object()      # маркер «файл не распарсился» (кэш не трогаем)


def _read_yaml(path: Path):
    """dict файла; {} если файла нет; _PARSE_ERROR при битом YAML (лог-warning).
    Битый файл НЕ роняет init/reload и не затирает прежние значения в кэше —
    правишь yaml дальше, вотчдог подхватит валидную версию."""
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("settings: %s не распарсился (%s) — прежние значения сохранены",
                    path.name, e)
        return _PARSE_ERROR


def init(conf_dir) -> None:
    """Однократная инициализация: запомнить каталог conf и загрузить все *.yaml."""
    global _conf_dir
    with _lock:
        _conf_dir = Path(conf_dir)
        _cache.clear()
        for p in sorted(_conf_dir.glob("*.yaml")):
            data = _read_yaml(p)
            _cache[p.stem] = {} if data is _PARSE_ERROR else data
        log.info("settings: загружено файлов конфигурации: %d", len(_cache))


def _split(key: str) -> tuple[str, list[str]]:
    parts = key.split(".")
    if len(parts) < 2:
        raise KeyError(f"dotted-ключ должен быть '<файл>.<путь>', получено: {key!r}")
    return parts[0], parts[1:]


def _traverse(d: dict, path: list[str]) -> tuple[bool, Any]:
    cur: Any = d
    for seg in path:
        if not isinstance(cur, dict) or seg not in cur:
            return False, None
        cur = cur[seg]
    return True, cur


def get(key: str, default: Any = None) -> Any:
    """Значение по dotted-ключу из кэша; default, если пути нет."""
    with _lock:
        stem, path = _split(key)
        found, val = _traverse(_cache.get(stem, {}), path)
        return val if found else default


def get_int(key: str, default: int = 0) -> int:
    v = get(key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key, default)
    return bool(v) if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def get_float(key: str, default: float = 0.0) -> float:
    v = get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ── diff (для точечного применения) ──────────────────────────────────────────
def _flatten(d: Any, prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}.{k}"))
    else:
        out[prefix] = d
    return out


def _diff_stem(stem: str, old: dict, new: dict) -> list[str]:
    """Изменившиеся/добавленные/удалённые dotted-ключи для одного файла."""
    fo, fn = _flatten(old, stem), _flatten(new, stem)
    changed = []
    for k in fo.keys() | fn.keys():
        if fo.get(k) != fn.get(k):
            changed.append(k)
    return changed


# ── on-change реестр ─────────────────────────────────────────────────────────
def on_change(key_or_prefix: str, callback: Callable[[str, Any], None]) -> None:
    """Зарегистрировать реакцию на изменение ключа (или префикса — матчит и сам
    ключ, и вложенные под ним). Колбэк получает (dotted_key, new_value)."""
    _on_change.append((key_or_prefix, callback))


def _fire(changed: list[str]) -> None:
    for key in changed:
        val = get(key)
        for pat, cb in _on_change:
            if key == pat or key.startswith(pat + "."):
                try:
                    cb(key, val)
                except Exception as e:                        # noqa: BLE001
                    log.warning("on_change(%s) упал: %s", key, e)


# ── перезагрузка (вотчдог/ручная правка) ─────────────────────────────────────
def reload() -> list[str]:
    """Перечитать все conf/*.yaml, обновить кэш, применить on-change ТОЛЬКО по
    изменившимся ключам. Возвращает список изменённых dotted-ключей."""
    if _conf_dir is None:
        return []
    with _lock:
        changed: list[str] = []
        stems = set(_cache.keys())
        for p in sorted(_conf_dir.glob("*.yaml")):
            stems.add(p.stem)
        for stem in stems:
            new = _read_yaml(_conf_dir / f"{stem}.yaml")
            if new is _PARSE_ERROR:
                continue                     # битый файл: кэш не трогаем
            old = _cache.get(stem, {})
            if new != old:
                changed.extend(_diff_stem(stem, old, new))
                _cache[stem] = new
    if changed:
        log.info("settings: изменено ключей: %d (%s)", len(changed), ", ".join(sorted(changed)))
        _fire(changed)
    return changed


# ── запись (ruamel round-trip) ───────────────────────────────────────────────
def is_self_writing() -> bool:
    """True, пока settings сам пишет YAML — вотчдог conf/ должен это игнорить."""
    return _self_writing.is_set()


def set_value(key: str, value: Any) -> list[str]:
    """Записать значение по dotted-ключу в соответствующий conf-файл, сохранив
    комментарии и порядок (ruamel), атомарно. Обновляет кэш, применяет on-change
    по изменившимся ключам, возвращает их список. Файл обязан существовать."""
    from ruamel.yaml import YAML          # ленивый импорт — не тянем на старте
    stem, path = _split(key)
    if _conf_dir is None:
        raise RuntimeError("settings.init() не вызван")
    fpath = _conf_dir / f"{stem}.yaml"
    if not fpath.exists():
        # Файл ещё не досеян в conf_dir (например, добавлен новой версией, а
        # апдейтер не скопировал его в /etc). Не роняем хендлер KeyError'ом —
        # создаём файл: значение станет источником, дефолты остальных ключей
        # берутся из get(..., default) в точках использования. Досев из поставки
        # (seed_conf) при следующем апдейте просто не тронет уже существующий.
        log.warning("settings: conf/%s.yaml отсутствовал — создаю под ключ %r",
                    stem, key)
        fpath.write_text("", encoding="utf-8")

    ruamel = YAML()
    ruamel.preserve_quotes = True
    with _lock:
        with fpath.open(encoding="utf-8") as f:
            doc = ruamel.load(f) or {}
        # спуститься по пути, создавая недостающие уровни
        cur = doc
        for seg in path[:-1]:
            if seg not in cur or not isinstance(cur[seg], dict):
                cur[seg] = {}
            cur = cur[seg]
        cur[path[-1]] = value

        _self_writing.set()
        try:
            tmp = fpath.with_suffix(".yaml.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                ruamel.dump(doc, f)
            os.replace(tmp, fpath)          # атомарная замена
        finally:
            _self_writing.clear()

        old = _cache.get(stem, {})
        new = _read_yaml(fpath)
        if new is _PARSE_ERROR:              # немыслимо после нашей же записи
            raise RuntimeError(f"conf/{stem}.yaml после записи не парсится")
        changed = _diff_stem(stem, old, new)
        _cache[stem] = new
    if changed:
        _fire(changed)
    return changed
