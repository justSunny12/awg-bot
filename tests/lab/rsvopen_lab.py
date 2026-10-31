"""Процесс открытия (tools/rsvopen.py) с миром DNS стенда — для LAB_OPEN_RECORD=1 у pairnode.

На хостах аварийное открытие (по точкам кворума или коду) меняет запись само: провайдер из
конверта DNS пакета, затем ждёт факта у авторитетного сервера и кладёт отметку `tg_take`;
итог — `done` без `record=skipped`. У стенда нет ни провайдера, ни авторитетного DNS: здесь
провайдер и оба чтения записи — файл `dns.json` общего каталога стенда (pairnode.LabDns,
LAB_SHARED и LAB_RECORD — окружением юнита), остальное — настоящий модуль без изменений.

Запускает его обёртка `base-rec/tools/rsvopen.py` (pairnode._rec_base) в той же песочнице
systemd-run, что у бота резерва.
"""
from __future__ import annotations

import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    from awgbot.util import dnsapi
    from tests.lab.pairnode import LabDns
    dns = LabDns(Path(os.environ["LAB_SHARED"]) / "dns.json")
    for name in ("read_system", "read_authoritative", "read_authoritative_ttl", "provider_for"):
        setattr(dnsapi, name, getattr(dns, name))
    runpy.run_path(str(ROOT / "tools" / "rsvopen.py"), run_name="__main__")


if __name__ == "__main__":
    main()
