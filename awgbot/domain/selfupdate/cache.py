"""cache.py — часть пакета selfupdate (см. __init__): сохранённый список
релизов и пересчёт цели без сети.

Проверка по расписанию ходит на GitHub раз в день, неделю или месяц, а версии
другой стороны меняются когда угодно: шлюз обновился, сервер прислал новую
версию в `role`. Ходить на GitHub по каждому такому событию — маячок, ждать
расписания — месяц не видеть обновлённых шлюзов. Поэтому проверка сохраняет
список релизов новее установленного (с телами — уведомлению нужен список
изменений), а событие пересчитывает цель по нему (update_regate).
"""
from __future__ import annotations

import json
import logging

from awgbot.core import config
from awgbot.infra import updates

log = logging.getLogger("awgbot.selfupdate")

# Тело релиза в сохранённом списке — не длиннее: хэштеги стоят в его начале,
# а уведомлению больше лимита Telegram и не нужно.
_BODY_MAX = 4096


class SelfUpdateCacheMixin:
    """Сохранённый список релизов и пересчёт цели по нему."""

    _RELEASES_KEY = "update_releases"

    def _update_keep(self, releases: list) -> None:
        """Скачанный список релизов — в state (только новее установленного), и
        сразу — что ждёт другую сторону."""
        installed = updates.parse_version(config.INSTALLED_VERSION)
        newer = [r for r in releases if installed is not None and r.version > installed]
        self.db.set_state(self._RELEASES_KEY, json.dumps({
            "installed": config.INSTALLED_VERSION,
            "releases": [{"tag": r.tag, "title": r.title, "body": (r.body or "")[:_BODY_MAX],
                          "asset_url": r.asset_url, "sha256": r.sha256} for r in newer],
        }, ensure_ascii=False))
        self._update_note_waiting(newer)

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
            out.append(updates.Release(tag=r["tag"], version=ver, body=str(r.get("body") or ""),
                                       asset_url=r.get("asset_url"), sha256=r.get("sha256"),
                                       title=str(r.get("title") or "")))
        out.sort(key=lambda x: x.version)
        return out

    def update_regate(self):
        """Версии другой стороны изменились — пересчитать цель по сохранённому
        списку, без сети: тег «⬆️ Доступна vX» и ожидание. Вернуть Release, о
        котором надо уведомить (появилась новая допустимая цель — один раз на
        тег, по правилам update_to_notify), иначе None. Списка нет — ничего не
        трогаем: его принесёт ближайшая проверка."""
        releases = self._update_cached_releases()
        installed = updates.parse_version(config.INSTALLED_VERSION)
        if releases is None or installed is None:
            return None
        peers = self._update_peers()
        gate = self._update_gate(peers)
        if gate is None:
            return None
        target = updates.pick_target(installed, releases, self._update_role(),
                                     self._generation_ceiling(), gate)
        before = self.update_available_tag()
        self.db.set_state(self._AVAILABLE_KEY, target.tag if target is not None else "")
        self._update_note_waiting(releases, peers)
        if (target.tag if target is not None else "") != before:
            log.info("обновление: цель пересчитана по версиям другой стороны — %s",
                     target.tag if target is not None else "нет")
        return self.update_to_notify(target) if target is not None else None
