"""
dist.py — поставка awg-bot из СОБСТВЕННОЙ установки, тем же составом, что
собирает build_release.sh.

Зачем: файл первого применения шлюза везёт поставку с собой. Шлюз стоит в
России, где GitHub без туннеля недоступен, а туннель как раз и ставится этим
файлом — качать поставку с шлюза неоткуда. У ВПС же код уже есть: он и есть
поставка ровно той версии, что работает у основного бота, и агент получает её
без сети. sha256 с релизом такая сборка не совпадёт (другой tar, другое время)
— установщик зовётся с --skip-verify, доверие держит scp с машины админа.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

# Состав — как в build_release.sh (build_bot): менять синхронно.
_ENTRIES = ("awgbot", "tools", "conf", "install", "awg-bot.sh", "run.sh",
            "awg-bot.service", "requirements.txt", ".env.example", "README.md")
_MUST = ("awgbot/__main__.py", "awg-bot.sh", "install/awg-bot-install.sh",
         "install/routing-gw-setup.sh")

_cache: dict[str, bytes] = {}


class DistError(RuntimeError):
    pass


def install_root() -> Path:
    """Корень установки — каталог над пакетом awgbot (/opt/awg-bot на хосте,
    корень репозитория в разработке)."""
    import awgbot
    return Path(awgbot.__file__).resolve().parent.parent


def _skip_cache(info: tarfile.TarInfo):
    parts = info.name.split("/")
    if "__pycache__" in parts or info.name.endswith(".pyc"):
        return None
    return info


def archive(root: Path | None = None) -> bytes:
    """awg-bot.tgz из установки. Один раз на процесс: код не меняется, пока
    процесс жив (обновление его перезапускает)."""
    root = (root or install_root()).resolve()
    key = str(root)
    if key in _cache:
        return _cache[key]
    for rel in _MUST:
        if not (root / rel).is_file():
            raise DistError(f"в установке {root} нет {rel} — поставка неполная; "
                            "обнови основного бота ещё раз и повтори")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _ENTRIES:
            p = root / name
            if p.exists():
                tar.add(p, arcname=f"./{name}", filter=_skip_cache)
    _cache[key] = buf.getvalue()
    return _cache[key]


__all__ = ["archive", "install_root", "DistError"]
