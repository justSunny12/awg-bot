"""Self-update из ПУБЛИЧНОГО GitHub-репо — на стандартной библиотеке.

Модель — «последний, с обязательными ступенями»:
  • тянем СПИСОК релизов (`/releases`), не `/latest`;
  • парсим теги как semver, находим установленную версию (__version__) среди них;
  • цель = ПОСЛЕДНИЙ релиз новее установленного, адресованный этой роли. Одна
    сборка ядра и один переезд профилей вместо цепочки: ядро прибито к поставке,
    схема БД у минимума полная, conf досеивается — промежуточные ступени ничего
    не добавляют;
  • адресат и обязательная ступень — одной строкой на роль: `#requires_main_X.Y.Z`
    (основной бот), `#requires_gw_X.Y.Z` (агент шлюза). Есть строка — релиз
    роли адресован; X.Y.Z — минимум, с которого на него можно прыгнуть напрямую.
    Хост ниже сначала едет на X.Y.Z (ближайший релиз для роли не ниже него), и
    только потом дальше. Так снимаются шимы совместимости: всё, что старше
    объявленного минимума, проходит через него и получает миграции там;
  • пропущенные ступени едут в уведомление списком — changelog не теряется;
  • установленной версии нет среди тегов релизов — обновления выключены
    (next_release() → None); исключение — локальная сборка X.Y.Z.N при релизе
    с той же базой X.Y.Z: она считается релизной и обновляется (_is_release_build).

Репо публичный → анонимный GET, никаких токенов. Целостность поставки — сверкой
sha256 с полем `assets[].digest` (GitHub считает его сам при загрузке ассета;
формат «sha256:<hex>»). Нет digest → обновление отклоняем (не «доверяем молча»).

Сеть — urllib (без requests). Ошибки заворачиваем в UpdateError; вызывающий
(сервис) их гасит и логирует, фоновая задача от них не падает.

Части: model — релиз, разбор версий и хэштегов тела; pick — выбор цели по
списку релизов; github — запросы к GitHub, скачивание и запуск апдейтера.
Пакет переотдаёт имена частей: `from awgbot.infra import updates` работает
как раньше. Сетевые вызовы частей идут через пакет — подмена имени пакета
(`updates._request`, `updates.list_releases`) видна им всем.
"""
from __future__ import annotations

from awgbot.core import config

from .model import (
    UpdateError, Release, parse_version, version_at_least,
    _SEMVER_RE, _REQUIRES_RE, _AUDIENCE_RE, _ROLE_KEY, _ROLE_TAG, _GENERATION_RE)
from .github import (
    _AWG_BOT_BIN, _API, _TIMEOUT, _request, _digest_hex, list_releases, _strip_tag,
    download_asset, apply, release_body)
from .pick import next_release, _is_release_build, pick_target

__all__ = [
    "config", "UpdateError", "Release", "parse_version", "version_at_least",
    "_SEMVER_RE", "_REQUIRES_RE", "_AUDIENCE_RE", "_ROLE_KEY", "_ROLE_TAG", "_GENERATION_RE",
    "_AWG_BOT_BIN", "_API", "_TIMEOUT", "_request", "_digest_hex", "list_releases",
    "_strip_tag", "download_asset", "apply", "release_body",
    "next_release", "_is_release_build", "pick_target"]
