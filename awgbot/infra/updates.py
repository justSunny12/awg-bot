"""Self-update из ПУБЛИЧНОГО GitHub-репо — на стандартной библиотеке.

Модель (сознательно простая, «по одной ступени вверх»):
  • тянем СПИСОК релизов (`/releases`), не `/latest`;
  • парсим теги как semver, находим установленную версию (__version__) среди них;
  • «следующая» = минимальный тег, строго больший установленной. Не последняя —
    ровно одна ступень, чтобы каждый changelog был показан один раз;
  • если установленной версии НЕТ среди тегов релизов (локальная/кастомная
    сборка) — обновления полностью выключены (next_release() → None).

Репо публичный → анонимный GET, никаких токенов. Целостность поставки — сверкой
sha256 с полем `assets[].digest` (GitHub считает его сам при загрузке ассета;
формат «sha256:<hex>»). Нет digest → обновление отклоняем (не «доверяем молча»).

Сеть — urllib (без requests). Ошибки заворачиваем в UpdateError; вызывающий
(сервис) их гасит и логирует, фоновая задача от них не падает.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from awgbot.core import config

_AWG_BOT_BIN = "/usr/local/bin/awg-bot"

_API = "https://api.github.com"
_TIMEOUT = 20
# vMAJOR.MINOR.PATCH (ведущее «v» необязательно). Пре-релизные суффиксы не
# поддерживаем сознательно — релизы строго числовые.
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateError(Exception):
    """Сетевая/протокольная ошибка обновления (гасится вызывающим)."""


@dataclass(frozen=True)
class Release:
    tag: str                    # как в GitHub, напр. "v1.2.0"
    version: tuple              # (1, 2, 0) — для сравнения
    body: str                   # тело релиза (changelog этой версии, без заголовка)
    asset_url: Optional[str]    # API-URL ассета-поставки (для octet-stream)
    sha256: Optional[str]       # эталонный sha256 из assets[].digest (hex)


def parse_version(tag: str) -> Optional[tuple]:
    """'v1.2.0'|'1.2.0' → (1,2,0); иначе None (нерелизный тег игнорируем)."""
    m = _SEMVER_RE.match(tag.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


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
    raw = _request(f"{_API}/repos/{config.UPDATES_REPO}/releases",
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
                           asset_url=asset_url, sha256=sha256))
    out.sort(key=lambda x: x.version)
    return out


def next_release() -> Optional[Release]:
    """Следующая ступень за установленной версией, или None.

    None означает «обновлять не на что / не от чего»:
      • установленная версия НЕ найдена среди тегов релизов → молчим навсегда
        (нерелизная сборка);
      • установленная — самая свежая → актуальны;
      • нет релиза строго больше установленной.
    """
    installed = parse_version(config.INSTALLED_VERSION)
    if installed is None:
        return None
    releases = list_releases()
    tags = {r.version for r in releases}
    if installed not in tags:            # нас нет в списке релизов → не трогаем
        return None
    higher = [r for r in releases if r.version > installed]
    return higher[0] if higher else None      # минимальный больший = следующий


def download_asset(release: Release) -> bytes:
    """Скачать ассет-поставку релиза и проверить sha256 против assets[].digest.
    Нет ассета/нет digest/несовпадение — UpdateError (обновление не применяем)."""
    if not release.asset_url:
        raise UpdateError(f"в релизе {release.tag} нет ассета "
                          f"{config.UPDATES_ASSET_NAME}")
    if not release.sha256:
        raise UpdateError(f"у ассета релиза {release.tag} нет sha256-digest — "
                          f"целостность не проверить, обновление отклонено")
    blob = _request(release.asset_url, "application/octet-stream")
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

    if shutil.which("systemd-run"):
        # транзиентный юнит вне нашего cgroup; --collect уберёт его после выхода.
        # Имя уникальное — повторный запуск не упадёт об «unit already exists».
        unit = f"awg-bot-selfupdate-{int(time.time())}-{os.getpid()}"
        subprocess.Popen(
            ["systemd-run", "--collect", "--quiet",
             f"--unit={unit}", _AWG_BOT_BIN, "update", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
    else:
        # без systemd-run: хотя бы отвяжемся в новую сессию (best effort)
        subprocess.Popen(
            [_AWG_BOT_BIN, "update", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def release_body(tag: str) -> str:
    """Тело релиза по тегу (для итога после рестарта). Сеть/отсутствие → ''."""
    try:
        for r in list_releases():
            if r.tag == tag:
                return r.body
    except UpdateError:
        pass
    return ""
