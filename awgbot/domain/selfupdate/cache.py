"""cache.py — часть пакета selfupdate (см. __init__): сохранённый список
релизов, кэш метаданных, выбор цели под замком и пересчёт без сети.

Проверка по расписанию ходит на GitHub раз в день, неделю или месяц, а версии
другой стороны меняются когда угодно: шлюз обновился, сервер прислал новую
версию в `role`. Ходить на GitHub по каждому такому событию — маячок, ждать
расписания — месяц не видеть обновлённых шлюзов. Поэтому проверка сохраняет
список релизов новее установленного (с метаданными и телами — уведомлению
нужен список изменений), а событие пересчитывает цель по нему (update_regate).

Один замок на выбор и запись. Плановая проверка, пересчёт по снимку и кнопка
решают в разных потоках; выбор цели и запись его результата
(update_available_tag, update_waiting, отметка уведомления) — под одним
замком процесса, иначе поздний поток перетёр бы свежий вывод устаревшим.
Версии другой стороны читаются ПОСЛЕ ответа GitHub, внутри замка: сеть
отвечает секунды, и снятые до неё версии к решению уже устарели бы.

Метаданные выпущенного тега не меняются — их кэш (update_meta) держится по
тегу и digest ассета; хранится только для релизов новее установленного.
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
# записан там) и не отложено ли решение окном после старта.
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
        """У роли есть другая сторона — выбор идёт полным путём (метаданные с
        кэшем, замок, ожидание); без неё — как до сверки."""
        return callable(getattr(self, "update_peers", None))

    def _update_meta_cache(self) -> dict:
        data = self.db.get_state_json(self._META_KEY, {})
        return data if isinstance(data, dict) else {}

    def _update_store(self, releases: list, meta_cache) -> None:
        """Скачанный список (только новее установленного) и кэш метаданных — в
        state: по ним пересчёт без сети и следующая проверка без лишних
        запросов."""
        installed = updates.parse_version(config.INSTALLED_VERSION)
        newer = [r for r in releases if installed is not None and r.version > installed]
        tags = {r.tag for r in newer}
        if isinstance(meta_cache, dict):
            self.db.set_state(self._META_KEY, json.dumps(
                {t: v for t, v in meta_cache.items() if t in tags}, ensure_ascii=False))
        self.db.set_state(self._RELEASES_KEY, json.dumps({
            "installed": config.INSTALLED_VERSION,
            "releases": [{"tag": r.tag, "title": r.title, "body": (r.body or "")[:_BODY_MAX],
                          "asset_url": r.asset_url, "sha256": r.sha256,
                          "meta": r.meta, "meta_bad": r.meta_bad} for r in newer],
        }, ensure_ascii=False))

    def _update_cached_releases(self):
        """Сохранённый список (Release по возрастанию) или None: проверки не
        было или она была на другой установленной версии."""
        data = self.db.get_state_json(self._RELEASES_KEY, {})
        if not isinstance(data, dict) or data.get("installed") != config.INSTALLED_VERSION:
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
                                       meta_bad=str(r.get("meta_bad") or "")))
        out.sort(key=lambda x: x.version)
        return out

    def _update_choose(self, installed: tuple, releases: list, role: str, ceiling):
        """(цель, отложено) — выбор и запись тега и ожидания; зовётся под
        замком. Окно после старта открыто — решение отложено, записи нет."""
        if self.update_hold() is not None:
            return None, True
        gate = self._update_gate(self._update_peers())     # версии — сейчас, после сети
        target = updates.pick_target(installed, releases, role, ceiling, gate)
        self.db.set_state(self._AVAILABLE_KEY, target.tag if target is not None else "")
        self._update_note_waiting(installed, releases, role, ceiling, gate)
        return target, False

    def _update_decide(self, installed: tuple, releases: list, role: str, ceiling):
        """decide для updates.next_release: список пришёл из сети — сохранить и
        выбрать под замком."""
        with _LOCK:
            _TL.decided = True
            self._update_store(releases, getattr(_TL, "meta_cache", None))
            target, held = self._update_choose(installed, releases, role, ceiling)
            _TL.held = held
            for r in updates.skipped(installed, releases):
                log.warning("обновление: %s пропущен — %s", r.tag, r.skip_reason())
            return target

    def update_regate(self, notify: bool = True):
        """Версии другой стороны изменились — пересчитать цель по сохранённому
        списку, без сети: тег «⬆️ Доступна vX» и ожидание. Вернуть Release, о
        котором надо уведомить (допустимая цель новее объявленной — по
        правилам update_to_notify), иначе None; notify=False — только
        пересчитать (вызывающему нечем уведомить — уведомит следующий
        пересчёт). Списка нет или идёт окно после старта — ничего не трогаем."""
        if not self._update_gated():
            return None
        with _LOCK:
            releases = self._update_cached_releases()
            installed = updates.parse_version(config.INSTALLED_VERSION)
            if releases is None or installed is None:
                return None
            before = self.db.get_state(self._AVAILABLE_KEY) or ""
            target, held = self._update_choose(installed, releases, self._update_role(),
                                               self._generation_ceiling())
            if held:
                return None
            now = target.tag if target is not None else ""
            if now != before:
                log.info("обновление: цель пересчитана по версиям другой стороны — %s", now or "нет")
            if not notify or target is None:
                return None
            return self.update_to_notify(target)
