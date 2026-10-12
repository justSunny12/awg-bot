"""Self-update из ПУБЛИЧНОГО GitHub-репо — на стандартной библиотеке.

Модель — «последний, с обязательными ступенями»:
  • тянем СПИСОК релизов (`/releases`), не `/latest`;
  • парсим теги как semver, находим установленную версию (__version__) среди них;
  • цель = ПОСЛЕДНИЙ релиз новее установленного, адресованный этой роли. Одна
    сборка ядра и один переезд профилей вместо цепочки: ядро прибито к поставке,
    схема БД у минимума полная, conf досеивается — промежуточные ступени ничего
    не добавляют;
  • адресат и обязательная ступень — на роль: `roles.<main|gw>.requires` в
    метаданных (у релизов до перехода — строкой тела `#requires_main_X.Y.Z` /
    `#requires_gw_X.Y.Z`). Есть роль — релиз ей адресован; X.Y.Z — минимум, с
    которого на него можно прыгнуть напрямую.
    Хост ниже сначала едет на X.Y.Z (ближайший релиз для роли не ниже него), и
    только потом дальше. Так снимаются шимы совместимости: всё, что старше
    объявленного минимума, проходит через него и получает миграции там;
  • пропущенные ступени едут в уведомление списком — changelog не теряется;
  • установленной версии нет среди тегов релизов — обновления выключены
    (next_release() → None); исключение — локальная сборка X.Y.Z.N при релизе
    с той же базой X.Y.Z: она считается релизной и обновляется (_is_release_build);
  • условия релиза — в метаданных: второй ассет `awg-bot-release.json`
    (meta.py) с адресатами, минимумами, поколением ядра и условиями на
    версию другой роли (`roles.gw.needs_main` — агенту нужен сервер AWG не
    ниже; `roles.main.needs_gw` — основному боту нужны шлюзы не ниже). Тело
    — запасной путь только для релизов моложе META_ONLY_FROM (переход с
    хэштегов); битый ассет или его нет с META_ONLY_FROM — релиз пропущен;
  • условие на другую роль идёт по цепочке — цель допустима, если выполнены
    условия всех адресованных роли релизов от установленного до неё; иначе
    берётся допустимый ниже, а новейший недопустимый «ждёт» (waiting_target).
    Версии другой стороны знает вызывающий (peer или decide); без них
    условий нет вовсе.

Репо публичный → анонимный GET, никаких токенов. Целостность поставки — сверкой
sha256 с полем `assets[].digest` (GitHub считает его сам при загрузке ассета;
формат «sha256:<hex>»). Нет digest → обновление отклоняем (не «доверяем молча»).

Сеть — urllib (без requests). Ошибки заворачиваем в UpdateError; вызывающий
(сервис) их гасит и логирует, фоновая задача от них не падает.

Части: model — релиз и разбор версий; meta — метаданные релиза (схема,
проверка, строки тела на время перехода); pick — выбор цели по списку
релизов; github — запросы к GitHub (список релизов с метаданными и кэшем по
тегу), скачивание и запуск апдейтера.
Пакет переотдаёт имена частей: `from awgbot.infra import updates` работает
как раньше. Сетевые вызовы частей идут через пакет — подмена имени пакета
(`updates._request`, `updates.list_releases`) видна им всем.
"""
from __future__ import annotations

from awgbot.core import config

from .model import (
    UpdateError, Release, parse_version, version_at_least,
    _SEMVER_RE, _REQUIRES_RE, _AUDIENCE_RE, _ROLE_KEY, _ROLE_TAG, _GENERATION_RE)
from . import meta
from .meta import MetaError, META_ONLY_FROM
from .github import (
    _AWG_BOT_BIN, _API, _TIMEOUT, _request, _digest_hex, list_releases, _strip_tag,
    download_asset, apply, release_body)
from .pick import next_release, _is_release_build, pick_target, waiting_target, chain_need, skipped

__all__ = [
    "config", "UpdateError", "Release", "parse_version", "version_at_least",
    "_SEMVER_RE", "_REQUIRES_RE", "_AUDIENCE_RE", "_ROLE_KEY", "_ROLE_TAG", "_GENERATION_RE",
    "meta", "MetaError", "META_ONLY_FROM",
    "_AWG_BOT_BIN", "_API", "_TIMEOUT", "_request", "_digest_hex", "list_releases",
    "_strip_tag", "download_asset", "apply", "release_body",
    "next_release", "_is_release_build", "pick_target", "waiting_target", "chain_need", "skipped"]
