"""github.py — часть пакета updates (см. __init__): запросы к GitHub (список
релизов с метаданными, тело релиза), скачивание поставки со сверкой sha256 и
запуск апдейтера вне своего cgroup."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from typing import Optional

from awgbot.core import config

from . import meta as _meta
from .model import Release, UpdateError, parse_version

_AWG_BOT_BIN = "/usr/local/bin/awg-bot"

_API = "https://api.github.com"
_TIMEOUT = 20


def _up():
    """Пакет updates: сетевые вызовы частей идут через него, чтобы подмена
    имени пакета (updates._request, updates.list_releases) была видна всем."""
    from awgbot.infra import updates
    return updates


def _request(url: str, accept: str) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "awg-bot-updater")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise UpdateError(f"GitHub HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UpdateError(f"сеть недоступна: {e}")


def _digest_hex(asset: dict) -> Optional[str]:
    """assets[].digest = 'sha256:<hex>' → '<hex>' (иначе None)."""
    d = asset.get("digest") or ""
    return d[7:].strip().lower() if d.startswith("sha256:") else None


def list_releases(meta_cache: Optional[dict] = None, fetch_meta: bool = True) -> list[Release]:
    """Все релизы репо, отсортированные по возрастанию версии. Нерелизные теги
    (не semver) и черновики отбрасываются.

    Метаданные (ассет config.UPDATES_META_ASSET_NAME) качаются только
    релизной сборке (_is_release_build), только для релизов новее
    установленного и только мимо кэша: meta_cache —
    {тег: {"digest", "meta"}}, его даёт и хранит вызывающий (здесь БД не
    знают); тот же digest — метаданные из кэша, другой — перечитать.
    Выпущенный тег не меняется, поэтому обычно за проверку уходит ноль или
    один лишний запрос. Битый ассет (нет digest, не совпал, не JSON, не та
    схема или версия) — Release.meta_bad с причиной; сеть — UpdateError."""
    raw = _up()._request(f"{_API}/repos/{config.UPDATES_REPO}/releases",
                         "application/vnd.github+json")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError(f"некорректный ответ GitHub: {e}")
    installed = parse_version(config.INSTALLED_VERSION)
    found = []
    for r in data:
        if r.get("draft"):
            continue
        ver = parse_version(r.get("tag_name", ""))
        if ver is None:
            continue
        asset_url = sha256 = None
        meta_asset = None
        for a in r.get("assets", []):
            if a.get("name") == config.UPDATES_ASSET_NAME:
                asset_url = a.get("url")
                sha256 = _digest_hex(a)
            elif a.get("name") == config.UPDATES_META_ASSET_NAME:
                meta_asset = a
        found.append((r, ver, asset_url, sha256, meta_asset))
    # метаданные — только релизной сборке: нерелизная не обновляется вовсе, и
    # ходить ей за ассетами незачем
    release_build = installed is not None and _up()._is_release_build(installed, [f[1] for f in found])
    out: list[Release] = []
    for r, ver, asset_url, sha256, meta_asset in found:
        meta = bad = None
        if fetch_meta and release_build and meta_asset is not None and ver > installed:
            meta, bad = _read_meta(r["tag_name"], meta_asset, meta_cache)
        out.append(Release(tag=r["tag_name"], version=ver,
                           body=(r.get("body") or "").strip(),
                           asset_url=asset_url, sha256=sha256,
                           title=_strip_tag(r.get("name") or "", r["tag_name"]),
                           meta=meta, meta_bad=bad or ""))
    out.sort(key=lambda x: x.version)
    return out


def _read_meta(tag: str, asset: dict, cache: Optional[dict]):
    """(метаданные, None) или (None, причина) — ассет метаданных релиза."""
    digest = _digest_hex(asset)
    if not digest:
        return None, "у метаданных релиза нет digest"
    hit = (cache or {}).get(tag)
    if isinstance(hit, dict) and hit.get("digest") == digest and isinstance(hit.get("meta"), dict):
        return hit["meta"], None
    url = asset.get("url")
    if not url:
        return None, "у метаданных релиза нет адреса"
    if int(asset.get("size") or 0) > _meta.META_MAX:
        return None, "метаданные релиза больше 16 КБ"
    blob = _up()._request(url, "application/octet-stream")
    if hashlib.sha256(blob).hexdigest() != digest:
        return None, "sha256 метаданных релиза не совпал"
    try:
        meta = _meta.parse(blob, tag)
    except _meta.MetaError as e:
        return None, f"метаданные релиза: {e}"
    if cache is not None:
        cache[tag] = {"digest": digest, "meta": meta}
    return meta, None


def _strip_tag(name: str, tag: str) -> str:
    """«v2.17.2 — ядро опознаётся по тегу» → «ядро опознаётся по тегу»."""
    name = name.strip()
    if name.startswith(tag):
        name = name[len(tag):].lstrip(" —–-:")
    return name


def download_asset(release: Release) -> bytes:
    """Скачать ассет-поставку релиза и проверить sha256 против assets[].digest.
    Нет ассета/нет digest/несовпадение — UpdateError (обновление не применяем)."""
    if not release.asset_url:
        raise UpdateError(f"в релизе {release.tag} нет ассета "
                          f"{config.UPDATES_ASSET_NAME}")
    if not release.sha256:
        raise UpdateError(f"у ассета релиза {release.tag} нет sha256-digest — "
                          f"целостность не проверить, обновление отклонено")
    blob = _up()._request(release.asset_url, "application/octet-stream")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != release.sha256:
        raise UpdateError(f"sha256 не совпал (ожидалось {release.sha256[:12]}…, "
                          f"получено {actual[:12]}…) — обновление отклонено")
    return blob


def apply(blob: bytes) -> None:
    """Записать поставку во временный файл и запустить `awg-bot update <tgz>`
    ОТДЕЛЬНО от нашего процесса, чтобы пережить `systemctl stop awg-bot` внутри
    апдейтера.

    Под systemd бот живёт в cgroup своего юнита; любой дочерний процесс,
    оставленный в этом cgroup, будет убит вместе с сервисом на его остановке.
    Поэтому запускаем апдейтер транзиентным юнитом через `systemd-run` — он
    выходит из нашего cgroup и доживёт до конца. stdin апдейтера — /dev/null:
    интерактивный `confirm` про wipe получит EOF и возьмёт дефолт «нет» (данные
    не трогаем). Fallback без systemd-run — setsid+новая сессия (best effort).

    Не бросает штатно (кроме записи файла): вызывающий уже сообщил пользователю
    «обновляюсь», а сам процесс вот-вот будет заменён.
    """
    fd, path = tempfile.mkstemp(prefix="awg-bot-update-", suffix=".tgz")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
    except OSError as e:
        raise UpdateError(f"не удалось записать поставку: {e}")

    # вне нашего cgroup (infra.detach); AWG_UPDATE_CLEANUP: post_update уберёт
    # архив (только из /tmp) — иначе поставка на десятки мегабайт оставалась
    # после каждого обновления
    from awgbot.infra.detach import spawn_detached
    spawn_detached([_AWG_BOT_BIN, "update", path], unit_prefix="awg-bot-selfupdate",
                   env={"AWG_UPDATE_CLEANUP": path})


def release_body(tag: str) -> str:
    """Тело релиза по тегу (для итога после рестарта). Сеть/отсутствие → ''."""
    try:
        for r in _up().list_releases(fetch_meta=False):
            if r.tag == tag:
                return r.body
    except UpdateError:
        pass
    return ""
