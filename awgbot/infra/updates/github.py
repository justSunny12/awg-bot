"""github.py — часть пакета updates (см. __init__): запросы к GitHub (список
релизов, тело релиза), скачивание поставки со сверкой sha256 и запуск апдейтера
вне своего cgroup."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from typing import Optional

from awgbot.core import config

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


def list_releases() -> list[Release]:
    """Все релизы репо, отсортированные по возрастанию версии. Нерелизные теги
    (не semver) и черновики отбрасываются."""
    raw = _up()._request(f"{_API}/repos/{config.UPDATES_REPO}/releases",
                         "application/vnd.github+json")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise UpdateError(f"некорректный ответ GitHub: {e}")
    out: list[Release] = []
    for r in data:
        if r.get("draft"):
            continue
        ver = parse_version(r.get("tag_name", ""))
        if ver is None:
            continue
        asset_url = sha256 = None
        for a in r.get("assets", []):
            if a.get("name") == config.UPDATES_ASSET_NAME:
                asset_url = a.get("url")
                sha256 = _digest_hex(a)
                break
        out.append(Release(tag=r["tag_name"], version=ver,
                           body=(r.get("body") or "").strip(),
                           asset_url=asset_url, sha256=sha256,
                           title=_strip_tag(r.get("name") or "", r["tag_name"])))
    out.sort(key=lambda x: x.version)
    return out


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
        for r in _up().list_releases():
            if r.tag == tag:
                return r.body
    except UpdateError:
        pass
    return ""
