"""restore.py — часть пакета fleet (см. __init__): восстановление бэкапа =
переход на выпуск бэкапа (§3.5).

Версия бэкапа V против установленной C: V = C — как раньше; иначе нужен
выпуск vV с GitHub, и только тот, на котором снят бэкап: sha256 ассета =
digest метки. Бэкап без digest (сборка не из выпуска) — только на той же
версии. Списку выпусков из сохранённого не верим: в нём выпуски ниже
установленного могут быть без метаданных, а сравнить digest надо с тем, что
GitHub отдаёт сейчас, — запрос в сеть при осмотре. Снятый выпуск годится, с
предупреждением в вопросе (Р20); удалённый тег — отказ.

Функции модульные: их зовут и осмотр бэкапа в боте (в потоке), и терминал
(`tools.restore_release` до остановки сервиса).
"""
from __future__ import annotations

import logging
import os

from awgbot.core import config
from awgbot.domain.notices import fleet as notes_
from awgbot.infra import updates

log = logging.getLogger(__name__)

SAME, UP, DOWN = "same", "up", "down"
RELEASE_FILE = "restore-pending.release.tgz"     # выпуск бэкапа, скачанный ботом до запуска restore


def same_version(ver: str, cur: str) -> bool:
    """Версия бэкапа та же, что у кода: совпадают X.Y.Z (хотфикс и локальная
    сборка поверх неё схему не меняют — compat.newer_than); не разобрана одна из
    двух (рабочая копия без версии) — как раньше, без перехода."""
    want, have = updates.parse_version(str(ver or "")), updates.parse_version(str(cur or ""))
    return want is None or have is None or want[:3] == have[:3]


def plan_release(label: dict, installed: str | None = None, meta_cache: dict | None = None) -> dict:
    """{"kind": same|up|down, "tag", "version", "from", "digest", "withdrawn",
    "release" (Release или None), "error" (отказ Т-В2 словами, без HTML)}.
    meta_cache — кэш метаданных проверки обновлений (тот же digest — без
    повторного запроса за ассетом метаданных)."""
    cur = str(installed or config.INSTALLED_VERSION)
    ver = str(label.get("version") or "")
    out = {"kind": SAME, "tag": f"v{ver}" if ver and not ver.startswith("v") else ver, "version": ver,
           "from": cur, "digest": str(label.get("digest") or ""), "withdrawn": "", "release": None, "error": ""}
    if same_version(ver, cur):
        return out
    want, have = updates.parse_version(ver), updates.parse_version(cur)
    out["kind"] = UP if want > have else DOWN
    digest = out["digest"]
    hexd = digest[7:] if digest.startswith("sha256:") else ""
    if not hexd:
        out["error"] = notes_.restore_not_release(ver)
        return out
    try:
        releases = (updates.list_releases(meta_cache=dict(meta_cache)) if meta_cache
                    else updates.list_releases())
    except updates.UpdateError:
        out["error"] = notes_.restore_github_down(ver)
        return out
    rel = next((r for r in releases if r.version == want), None)
    if rel is None:
        out["error"] = notes_.restore_no_release(ver)
        return out
    if not rel.sha256 or rel.sha256 != hexd:
        out["error"] = notes_.restore_other_digest(ver)
        return out
    if rel.meta_bad or not rel.usable():
        out["error"] = notes_.restore_meta_bad(ver)
        return out
    out.update(release=rel, withdrawn=rel.withdrawn)
    return out


def version_lines(rel: dict) -> list[str]:
    """Т-В1 по итогу plan_release: строка версии и, у снятого выпуска, хвост."""
    if not rel or rel.get("kind") == SAME or rel.get("error"):
        return []
    out = [notes_.restore_version_line(rel["kind"], rel.get("from") or "", rel.get("version") or "")]
    if rel.get("withdrawn"):
        out.append(notes_.restore_withdrawn_line(rel.get("version") or "", rel["withdrawn"]))
    return out


def public(rel: dict) -> dict:
    """Итог plan_release без объекта выпуска — в план вопроса и в JSON."""
    return {k: v for k, v in (rel or {}).items() if k != "release"}


def fetch_release(rel: dict, dest_dir: str | None = None) -> str:
    """Скачать ассет выпуска бэкапа со сверкой sha256 (download_asset сверяет
    с digest ассета, равным digest метки) в каталог бэкапов, 0600. Путь;
    сеть или sha256 — UpdateError."""
    release = rel.get("release")
    if release is None:
        raise updates.UpdateError(notes_.restore_github_down(rel.get("version") or ""))
    try:
        blob = updates.download_asset(release)
    except updates.UpdateError as e:
        log.warning("выпуск бэкапа %s не скачался: %s", release.tag, e)
        raise updates.UpdateError(notes_.restore_fetch_failed(rel.get("version") or "")) from e
    d = dest_dir or str(config.BACKUP_DIR)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, RELEASE_FILE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    return path


class FleetRestoreMixin:
    """Набор после восстановления."""

    def fleet_after_restore(self, version: str) -> None:
        """Сервер перешёл на выпуск бэкапа: выпуск набора — его версия (Р17),
        прежний ход ничего не значит; шлюзы пойдут следом по hello (вне хода,
        по одному)."""
        run = self.fleet_run()
        if run and str(run.get("tag") or "").lstrip("v") != str(version).lstrip("v"):
            self.fleet_run_set({})
