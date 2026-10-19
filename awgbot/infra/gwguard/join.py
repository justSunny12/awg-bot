"""join.py — часть пакета gwguard (см. __init__): скрипт обвязки при
подключении шлюза командой.

`--join <конфиг>` — только линк по токену и карантинная таблица (ни
аплинка, ни VPN-транзита, ни юнита обвязки): зовёт `awg-bot join`
(tools/join.py) до первой сессии канала. `--apply` после смены ключей
линка, подтверждённой кодом, — полная обвязка: карантинная таблица уходит,
появляется юнит, строки которого дальше ведёт документ сервера.
"""
from __future__ import annotations

import os

from awgbot.core import config
from awgbot.infra import gwguard as pkg

JOIN_TIMEOUT = 300
FINISH_TIMEOUT = 900       # полный прогон с apt — минуты


def _run(script: str, args: list[str], env: dict, timeout: int) -> tuple[bool, str]:
    try:
        proc = pkg.subprocess.run([script, *args], capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return False, "отсутствует скрипт обвязки"
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт обвязки не ответил за {timeout} с"
    except (OSError, pkg.subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout + proc.stderr).decode(errors="replace").strip()


def _env(channel_port: int) -> dict:
    env = dict(os.environ)
    env["LINK_IF"] = config.GW_LINK_IF
    env["HOST_CONF_DIR"] = config.GW_CONF_DIR
    if int(channel_port or 0):
        env["LINK_CHANNEL_PORT"] = str(int(channel_port))
    return env


def join_link(conf_path: str, channel_port: int, script: str = "") -> tuple[bool, str]:
    """Линк по токену с карантином (`--join`). script — путь скрипта обвязки
    поставки (до первого прогона копии в /usr/local/sbin нет)."""
    return _run(script or pkg.SETUP_SCRIPT, ["--join", conf_path], _env(channel_port), JOIN_TIMEOUT)


def _client_subnet(conf_path: str) -> str:
    """Подсеть клиентов сервера — первая запись AllowedIPs конфига линка,
    который поставила смена ключей после кода (сервер пишет её первой): до
    документа шлюзу её взять больше неоткуда, а без неё обвязка встала бы на
    подсеть по умолчанию."""
    from awgbot.util import wgconf
    try:
        with open(conf_path, encoding="utf-8") as f:
            allowed = wgconf.parse(f.read()).get("Peer", "AllowedIPs") or ""
    except (OSError, wgconf.WgConfError):
        return ""
    first = allowed.split(",")[0].strip()
    return first if pkg._ADDR_CHARS_RE.fullmatch(first or "-") and "/" in first else ""


def join_finish(channel_port: int) -> tuple[bool, str]:
    """Полная обвязка после сопряжения (`--apply` стоящего конфига линка)."""
    env = _env(channel_port)
    subnet = _client_subnet(config.GW_LINK_CONF)
    if subnet:
        env["CLIENT_SUBNET"] = subnet
    return _run(pkg.SETUP_SCRIPT, ["--apply", config.GW_LINK_CONF], env, FINISH_TIMEOUT)
