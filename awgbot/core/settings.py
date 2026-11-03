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

Раздел, переехавший в свой файл (`_MOVED_SECTIONS`: `app.reserve` →
`conf/reserve.yaml`, 4.2.0), `init()` переносит до первого чтения: значения
ложатся поверх шаблона нового файла, раздел в прежнем остаётся копией для отката
с меткой `moved` и отпечатками значений. Шим — снять, когда и UPDATE_FROM, и
COMPAT_FLOOR (core/compat) не ниже 4.2.0, шагом снятия копии из живых app.yaml.
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


class SettingsWriteError(RuntimeError):
    """Не записать значение: файл на диске не разбирается.

    Отдельный тип, потому что причина всегда одна и та же — файл правили руками
    и сломали, — а адресат сообщения человек, а не лог. Чтение это переживает
    (PyYAML на дубле ключа молча берёт последний, битый файл не трогает кэш),
    запись — нет: ruamel обязан разобрать документ целиком, чтобы сохранить
    комментарии. Асимметрия и делала отказ незаметным — бот работал на прежних
    значениях, а каждая кнопка настроек молча не срабатывала.
    """


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


# Разделы, переехавшие в свой файл: (файл, раздел, новый файл). Перенос — при init, до
# чтения: значения раздела ложатся поверх шаблона нового файла (досеян обновлением). Значения
# читаются тем же разбором, что у бота (PyYAML: дубль ключа — последний), ruamel — только на
# запись. Раздел в прежнем файле остаётся копией для отката на выпуск линии ниже (его код
# читает только прежнее место) с меткой `moved` и отпечатками значений по ключам `moved_sum`
# на момент переноса. Ключ копии, разошедшийся с отпечатком (после отката код прежней линии
# писал туда), переносится заново и отпечаток обновляется; раздел без метки (восстановлен
# бэкап прежнего выпуска) переносится целиком. Прежний файл не разбирается ruamel (дубль ключа
# после ручной правки) — метку не поставить: значения всё равно ложатся в новый файл, а
# отпечатки — ключом `moved_from` на его верхнем уровне; он главнее метки в копии и снимается,
# когда метка в прежнем файле записана. Шим из двух частей: перенос нужен, пока установки
# ниже 4.2.0 обновляются сюда напрямую (UPDATE_FROM в core/compat), копия и повторный перенос —
# пока откат и бэкапы ниже 4.2.0 допустимы (COMPAT_FLOOR). Снимать, когда и UPDATE_FROM, и
# COMPAT_FLOOR не ниже 4.2.0, — и тем же выпуском шагом снять копию раздела из живых app.yaml
# (drop_keys `app.reserve`): сама она с хостов не уйдёт.
_MOVED_SECTIONS: tuple[tuple[str, str, str], ...] = (("app", "reserve", "reserve"),)
_MOVED_MARK = "moved"            # в копии: имя нового файла
_MOVED_SUM = "moved_sum"         # в копии: {ключ: отпечаток значения} на момент переноса
_MOVED_ASIDE = "moved_from"      # в новом файле: {прежний файл: отпечатки}, когда копию не записать
_move_failures: list[tuple[str, str, str, str]] = []   # (раздел, файл, новый файл, причина) — init


def move_failures() -> list[tuple[str, str, str, str]]:
    """Сбои переноса разделов последнего init — для замечаний при запуске админу."""
    return list(_move_failures)


def _merge_into(dst, src) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge_into(dst[k], v)
        else:
            dst[k] = v


def _sums(sec: dict) -> dict[str, str]:
    """Отпечатки значений раздела по ключам верхнего уровня (метки — мимо)."""
    import hashlib
    import json
    out = {}
    for k, v in sec.items():
        if k in (_MOVED_MARK, _MOVED_SUM):
            continue
        raw = json.dumps(v, sort_keys=True, default=str, ensure_ascii=False)
        out[str(k)] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return out


def _flow(sums: dict):
    from ruamel.yaml.comments import CommentedMap
    m = CommentedMap(sums)
    m.fa.set_flow_style()        # одной строкой: служебное, не для правки руками
    return m


def _dump(ruamel, path: Path, doc) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        ruamel.dump(doc, f)
    os.replace(tmp, path)


def _same_sums(a, b) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and \
        {str(k): str(v) for k, v in a.items()} == {str(k): str(v) for k, v in b.items()}


def _why(e: Exception) -> str:
    """Причина одной строкой: у ruamel суть — строка «found …», первая лишь «while …»."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    found = [ln for ln in lines if ln.startswith("found")]
    return ((found or lines or [type(e).__name__])[0])[:160]


def _move_one(conf_dir: Path, src_stem: str, section: str, dst_stem: str) -> None:
    from ruamel.yaml import YAML
    src_path, dst_path = conf_dir / f"{src_stem}.yaml", conf_dir / f"{dst_stem}.yaml"
    src_name = src_path.name
    try:
        with src_path.open(encoding="utf-8") as f:
            plain = yaml.safe_load(f)
    except yaml.YAMLError:
        return                                   # файл не читает и бот — скажет его же чтение в init
    sec = plain.get(section) if isinstance(plain, dict) else None
    if not isinstance(sec, dict):
        return
    ruamel = YAML()
    ruamel.preserve_quotes = True
    try:
        with src_path.open(encoding="utf-8") as f:
            src = ruamel.load(f) or {}
        rsec = src.get(section) if isinstance(src, dict) else None
    except Exception as e:                       # noqa: BLE001 — ruamel-иерархия
        log.warning("settings: conf/%s не разбирается на запись (%s) — метка переноса раздела %s "
                    "ляжет в conf/%s.yaml", src_name, _why(e), section, dst_stem)
        src = rsec = None
    writable = isinstance(rsec, dict)
    dst = {}
    if dst_path.exists():
        with dst_path.open(encoding="utf-8") as f:
            dst = ruamel.load(f) or {}
    if not isinstance(dst, dict):
        raise ValueError(f"conf/{dst_stem}.yaml — не словарь")
    aside_all = dst.get(_MOVED_ASIDE)
    aside = aside_all.get(src_name) if isinstance(aside_all, dict) else None
    sums = _sums(sec)
    marked = bool(sec.get(_MOVED_MARK))
    if isinstance(aside, dict):
        base = aside                             # записан, когда копию было не записать, — новее метки
    elif marked:
        base = sec.get(_MOVED_SUM)
        if not isinstance(base, dict):
            base = sums                          # метка без отпечатков: копия — та, что перенесли
    else:
        base = None                              # первый перенос или бэкап прежнего выпуска
    keys = [k for k in sec if str(k) in sums and (base is None or str(base.get(k)) != sums[str(k)])]
    moved = {}
    for k in keys:
        # узел ruamel — если он о том же (кавычки и вид значения целы), иначе значение бота
        moved[k] = rsec[k] if writable and k in rsec and rsec[k] == sec[k] else sec[k]
    dst_dirty = bool(moved)
    _merge_into(dst, moved)
    if not writable and not _same_sums(aside, sums):
        if not isinstance(dst.get(_MOVED_ASIDE), dict):
            dst[_MOVED_ASIDE] = {}
        dst[_MOVED_ASIDE][src_name] = _flow(sums)
        dst_dirty = True
    if dst_dirty:
        _dump(ruamel, dst_path, dst)
    if writable:
        if not marked or not _same_sums(rsec.get(_MOVED_SUM), sums):
            if not marked:
                rsec[_MOVED_MARK] = f"{dst_stem}.yaml"
                if hasattr(src, "yaml_set_comment_before_after_key"):
                    src.yaml_set_comment_before_after_key(
                        section, before=f"Раздел перенесён в {dst_stem}.yaml — бот читает его там; здесь копия "
                                        f"для отката на прежний выпуск.\nПравьте {dst_stem}.yaml: ключ, "
                                        f"изменённый здесь, бот перенесёт туда при следующем старте")
            rsec[_MOVED_SUM] = _flow(sums)
            _dump(ruamel, src_path, src)
        if aside is not None:                    # метка в копии на месте — запасная не нужна
            del dst[_MOVED_ASIDE][src_name]
            if not dst[_MOVED_ASIDE]:
                del dst[_MOVED_ASIDE]
            _dump(ruamel, dst_path, dst)
    if moved:
        log.info("settings: раздел %s перенесён из conf/%s в conf/%s.yaml (%s; копия для отката — на месте)",
                 section, src_name, dst_stem,
                 "целиком" if base is None else "заново ключи " + ", ".join(map(str, moved)))


def _move_sections(conf_dir: Path) -> None:
    """Перенести разделы `_MOVED_SECTIONS` в свои файлы (ruamel round-trip, атомарно).
    Сначала пишется новый файл, потом метка в прежнем: оборванный перенос повторится
    на следующем старте с тем же итогом. Сбой — строка в журнал и запись для замечаний
    при запуске (`move_failures`), бот работает на новом файле как есть."""
    _move_failures.clear()
    for src_stem, section, dst_stem in _MOVED_SECTIONS:
        if not (conf_dir / f"{src_stem}.yaml").exists():
            continue
        try:
            _move_one(conf_dir, src_stem, section, dst_stem)
        except Exception as e:                     # noqa: BLE001 — ruamel-иерархия и ОС
            why = _why(e)
            log.warning("settings: раздел %s из conf/%s.yaml в conf/%s.yaml не перенесён: %s",
                        section, src_stem, dst_stem, e)
            _move_failures.append((section, f"{src_stem}.yaml", f"{dst_stem}.yaml", why))


def init(conf_dir) -> None:
    """Однократная инициализация: запомнить каталог conf, перенести переехавшие
    разделы и загрузить все *.yaml."""
    global _conf_dir
    with _lock:
        _conf_dir = Path(conf_dir)
        _move_sections(_conf_dir)
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
        try:
            with fpath.open(encoding="utf-8") as f:
                doc = ruamel.load(f) or {}
        except Exception as e:                     # noqa: BLE001 — ruamel-иерархия
            # Самый частый случай — продублированный ключ верхнего уровня после
            # правки руками (ruamel на дубле падает, PyYAML при чтении молчит).
            # Подробность ruamel (номера строк) — в лог: она многострочная и в
            # алерт Telegram всё равно не влезет (лимит 200 символов).
            log.warning("settings: conf/%s.yaml не разбирается на запись: %s", stem, e)
            raise SettingsWriteError(
                f"⚠️ conf/{stem}.yaml сломан — настройка НЕ сохранена. "
                f"Обычно это дубль ключа после ручной правки; подробности в "
                f"логе бота. Прежние значения целы, бот работает на них."
            ) from e
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


def drop_keys(keys) -> list[str]:
    """Снять ключи из conf-файлов (ruamel round-trip, атомарно, под меткой
    самозаписи): ключ, которого больше нет в коде, не висит мёртвым. Нет файла
    или ключа — ничего. Файл не разбирается — SettingsWriteError, как у
    set_value. Возвращает изменившиеся dotted-ключи."""
    from ruamel.yaml import YAML
    if _conf_dir is None:
        raise RuntimeError("settings.init() не вызван")
    by_stem: dict[str, list[list[str]]] = {}
    for key in keys:
        stem, path = _split(key)
        by_stem.setdefault(stem, []).append(path)
    changed: list[str] = []
    ruamel = YAML()
    ruamel.preserve_quotes = True
    with _lock:
        for stem, paths in by_stem.items():
            fpath = _conf_dir / f"{stem}.yaml"
            if not fpath.exists():
                continue
            try:
                with fpath.open(encoding="utf-8") as f:
                    doc = ruamel.load(f) or {}
            except Exception as e:                 # noqa: BLE001 — ruamel-иерархия
                log.warning("settings: conf/%s.yaml не разбирается на запись: %s", stem, e)
                raise SettingsWriteError(f"⚠️ conf/{stem}.yaml сломан — ключи не сняты") from e
            hit = False
            for path in paths:
                cur = doc
                for seg in path[:-1]:
                    cur = cur.get(seg) if isinstance(cur, dict) else None
                if isinstance(cur, dict) and path[-1] in cur:
                    del cur[path[-1]]
                    hit = True
            if not hit:
                continue
            _self_writing.set()
            try:
                tmp = fpath.with_suffix(".yaml.tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    ruamel.dump(doc, f)
                os.replace(tmp, fpath)
            finally:
                _self_writing.clear()
            old = _cache.get(stem, {})
            new = _read_yaml(fpath)
            if new is _PARSE_ERROR:
                raise RuntimeError(f"conf/{stem}.yaml после записи не парсится")
            changed += _diff_stem(stem, old, new)
            _cache[stem] = new
    if changed:
        _fire(changed)
    return changed
