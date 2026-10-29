"""
quoprobe.py — обёртка install/quorum-probe-setup.sh: пространство `awg-quo` и
интерфейсы зондов кворума резерва; обе роли (сервер AWG и агент шлюза).

Скрипт делает, модуль зовёт и читает состояние (как infra/rsvzone и
infra/gwguard/rsv). Конфиги зондов лежат в каталоге роли (`QUO_CONF_DIR`);
имена интерфейсов — `awgq<линия><цель>`. Сокет пробы процесс открывает в
пространстве сам (`util/netns.listen_in`).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from awgbot.core import config

NS = os.environ.get("QUO_NS", "awg-quo")
SCRIPT_TIMEOUT = 120


class QuoError(RuntimeError):
    """Скрипт зондов отказал; текст — его последние строки."""


def conf_dir() -> Path:
    env = os.environ.get("QUO_CONF_DIR")
    if env:
        return Path(env)
    if config.ROLE == "gateway":
        return Path("/var/lib/awg-gw/quorum")
    return Path(config.DATA_DIR) / "quorum"


def _script() -> Path:
    return config.BASE_DIR / "install" / "quorum-probe-setup.sh"


def port() -> int:
    """Порт ответчика — `app.reserve.quorum.port`; настройки недоступны — умолчание."""
    from awgbot.util.quorum.timing import DEFAULT_PORT
    try:
        from awgbot.core import settings
        return int((settings.get("app.reserve.quorum", {}) or {}).get("port") or DEFAULT_PORT)
    except Exception:                                     # noqa: BLE001 — настройки недоступны
        return DEFAULT_PORT


def _env(extra: dict | None = None) -> dict:
    return {**os.environ, "QUO_NS": NS, "QUO_CONF_DIR": str(conf_dir()), "QUO_PORT": str(port()), **(extra or {})}


def run(mode: str, *args: str, env: dict | None = None, timeout: int = SCRIPT_TIMEOUT) -> str:
    argv = ["sh", str(_script()), f"--{mode.lstrip('-')}", *args]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=_env(env))
    except (OSError, subprocess.SubprocessError) as e:
        raise QuoError(f"скрипт зондов не запустился: {e}") from e
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise QuoError("\n".join(out.splitlines()[-3:]) or f"код {proc.returncode}")
    return out


def _kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def status() -> dict:
    """{ns: present|absent, ifaces: [...], hs: {имя: секунд | None}}; скрипт не ответил — пусто."""
    try:
        kv = _kv(run("status", timeout=30))
    except QuoError:
        return {}
    ifaces = [i for i in str(kv.get("ifaces") or "").split() if i]
    hs: dict = {}
    for i in ifaces:
        v = str(kv.get(f"hs_{i}") or "never")
        hs[i] = int(v) if v.isdigit() else None
    return {"ns": kv.get("ns", "absent"), "ifaces": ifaces, "hs": hs}


def up(name: str, conf_text: str) -> str:
    """Конфиг зонда → скрипту (временный файл 0600): интерфейс поднят или обновлён."""
    fd, tmp = tempfile.mkstemp(prefix="awgq-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conf_text if conf_text.endswith("\n") else conf_text + "\n")
        os.chmod(tmp, 0o600)
        return run("up", tmp, env={"QUO_IF": str(name)})
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def down(name: str) -> str:
    return run("down", str(name))


def wipe() -> str:
    return run("wipe")


def selftest() -> str:
    return run("selftest", timeout=60)


def ns_path() -> str:
    from awgbot.util import netns
    return netns.path_of(NS)


def present(name: str) -> bool:
    return (conf_dir() / f"{name}.conf").exists()


def conf_text(name: str) -> str:
    try:
        return (conf_dir() / f"{name}.conf").read_text(encoding="utf-8")
    except OSError:
        return ""


def standing() -> list[str]:
    """Имена зондов, чьи конфиги лежат в каталоге (без вызова скрипта)."""
    try:
        return sorted(p.stem for p in conf_dir().glob("awgq*.conf"))
    except OSError:
        return []
