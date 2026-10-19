"""cache.py — часть пакета selfupdate (см. __init__): сохранённый список
релизов, кэш метаданных и выбор цели под замком.

Проверка по расписанию ходит на GitHub раз в день, неделю или месяц и
сохраняет список релизов новее установленного и линии совместимости (с
метаданными и телами — уведомлению нужен список изменений): по нему без сети
набор обновлений (domain/fleet) строит план, версии ролей в выпуске и цель
кнопки.

Один замок на выбор и запись. Плановая проверка и кнопка решают в разных
потоках; выбор цели и запись его результата (update_available_tag, отметка
уведомления) — под одним замком процесса, иначе поздний поток перетёр бы
свежий вывод устаревшим.

Метаданные выпущенного тега не меняются — их кэш (update_meta) держится по
тегу и digest ассета.
"""
from __future__ import annotations

import json
import logging
import threading

from awgbot.core import config
from awgbot.infra import updates

log = logging.getLogger("awgbot.selfupdate")

# Тело релиза в сохранённом списке — не длиннее: машинные строки стоят в его
# начале, а уведомлению больше лимита Telegram и не нужно.
_BODY_MAX = 4096

# Замок выбора — на процесс: сервисный объект у роли один, а потоки — свои у
# планировщика, канала и кнопок. Повторный вход — из пересчёта в отметку
# уведомления.
_LOCK = threading.RLock()
# Итог последнего update_next этого потока: решал ли выбор под замком (тег
# записан там) и был ли сбой сети (признак — потоку вызова, не экземпляру).
_TL = threading.local()


class SelfUpdateCacheMixin:
    """Список релизов и метаданные в state, выбор под замком, пересчёт."""

    _RELEASES_KEY = "update_releases"
    _META_KEY = "update_meta"

    def _update_lock(self):
        return _LOCK

    def _update_tl(self):
        return _TL

    def _update_gated(self) -> bool:
        """Роль ведёт набор (основной бот: fleet_pick) — выбор идёт полным
        путём: список сохраняется, метаданные — с кэшем, выбор — под замком."""
        return callable(getattr(self, "fleet_pick", None))

    def _update_meta_cache(self) -> dict:
        data = self.db.get_state_json(self._META_KEY, {})
        return data if isinstance(data, dict) else {}

    def _update_store(self, releases: list, meta_cache) -> None:
        """Скачанный список (только новее установленного) и кэш метаданных — в
        state: по ним пересчёт без сети и следующая проверка без лишних
        запросов."""
        from awgbot.core.compat import COMPAT_FLOOR, version_key
        installed = updates.parse_version(config.INSTALLED_VERSION)
        floor = version_key(COMPAT_FLOOR)
        # новее установленного — цель; сам установленный (и его база X.Y.Z) —
        # «сборка из выпуска»; в линии совместимости — версии ролей в выпуске
        # набора (шлюзы идут за сервером AWG и вниз)
        newer = [r for r in releases if installed is not None
                 and (r.version >= installed or r.version[:3] == installed[:3] or r.version[:3] >= floor)]
        tags = {r.tag for r in newer}
        if isinstance(meta_cache, dict):
            self.db.set_state(self._META_KEY, json.dumps(
                {t: v for t, v in meta_cache.items() if t in tags}, ensure_ascii=False))
        # digest установки пуст (переход с выпуска, чей скрипт его не писал,
        # или предрелизная сборка того же номера), а установленная версия —
        # опубликованный выпуск: digest его ассета и есть тот, что качать при
        # переходе на эту версию из бэкапа
        own = next((r for r in releases if r.version == installed and r.sha256), None)
        if own is not None:
            from .marks import adopt_release_digest
            adopt_release_digest(self.db, config.INSTALLED_VERSION, own.sha256)
        self.db.set_state(self._RELEASES_KEY, json.dumps({
            "installed": config.INSTALLED_VERSION,
            "releases": [{"tag": r.tag, "title": r.title, "body": (r.body or "")[:_BODY_MAX],
                          "asset_url": r.asset_url, "sha256": r.sha256,
                          "meta": r.meta, "meta_bad": r.meta_bad, "withdrawn": r.withdrawn,
                          "prerelease": bool(r.prerelease)} for r in newer],
        }, ensure_ascii=False))

    def _update_cached_releases(self, strict: bool = True):
        """Сохранённый список (Release по возрастанию) или None: проверки не
        было или (strict) она была на другой установленной версии. Набору
        годится и список прежней версии (strict=False): выпуски линии в нём
        те же, а цели выше установленной выбор отсеивает сам."""
        data = self.db.get_state_json(self._RELEASES_KEY, {})
        if not isinstance(data, dict) or (strict and data.get("installed") != config.INSTALLED_VERSION):
            return None
        out = []
        for r in data.get("releases") or []:
            ver = updates.parse_version(str(r.get("tag") or "")) if isinstance(r, dict) else None
            if ver is None:
                continue
            meta = r.get("meta") if isinstance(r.get("meta"), dict) else None
            out.append(updates.Release(tag=r["tag"], version=ver, body=str(r.get("body") or ""),
                                       asset_url=r.get("asset_url"), sha256=r.get("sha256"),
                                       title=str(r.get("title") or ""), meta=meta,
                                       meta_bad=str(r.get("meta_bad") or ""),
                                       withdrawn=str(r.get("withdrawn") or ""),
                                       prerelease=bool(r.get("prerelease"))))
        out.sort(key=lambda x: x.version)
        return out

    def _update_choose(self, installed: tuple, releases: list, role: str, ceiling):
        """Выбор цели и запись тега; зовётся под замком. Роль с набором —
        цель набора (fleet_pick): новейший выпуск для любой роли."""
        target = self.fleet_pick(installed, releases, ceiling)
        self.db.set_state(self._AVAILABLE_KEY, target.tag if target is not None else "")
        return target

    def _update_decide(self, installed: tuple, releases: list, role: str, ceiling):
        """decide для updates.next_release: список пришёл из сети — сохранить и
        выбрать под замком."""
        with _LOCK:
            _TL.decided = True
            self._update_store(releases, getattr(_TL, "meta_cache", None))
            target = self._update_choose(installed, releases, role, ceiling)
            for r in updates.skipped(installed, releases):
                log.warning("обновление: %s пропущен — %s", r.tag, r.skip_reason())
            return target

    def update_cached_release(self, tag: str):
        """Release из сохранённого списка по тегу или None — для раздела без
        сети (причина блокировки переездом по поколению из метаданных)."""
        return next((r for r in self._update_cached_releases() or [] if r.tag == tag), None)
