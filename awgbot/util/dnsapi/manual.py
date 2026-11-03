"""
manual.py — запись меняет человек: API нет, автоматический переезд невозможен.

Настройка: `{provider: "manual", name: "vpn.example.com"}`. Чтение — у
авторитетных серверов зоны, чтобы факт смены был виден и в этом режиме.
"""
from __future__ import annotations

from .common import DnsError, NotSupported, read_authoritative

id = "manual"
human = "вручную"
# запись меняет человек: срок, который ему дан (common.apply_wait_s; как шаг записи переезда
# кнопкой у основы — outbound RECORD_WAIT_S)
APPLY_S = 1800


def read(cfg: dict) -> str:
    addrs = read_authoritative(str(cfg.get("name", "")))
    if not addrs:
        raise DnsError(f"записи {cfg.get('name')} нет")
    return addrs[0]


def set(cfg: dict, ip: str) -> None:  # noqa: A001
    raise NotSupported("запись меняется вручную у провайдера")


def check(cfg: dict) -> str:
    return read(cfg)
