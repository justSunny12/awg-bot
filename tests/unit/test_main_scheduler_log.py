"""Unit: журнал основного процесса (runtime/main) — планировщик тише INFO.

Задания резерва идут раз в 3–5 с; их «Running job» / «executed successfully»
на INFO за минуты вытесняют из журнала всё остальное, и при разборе сбоя
читать нечего. После настройки журнала при запуске у логгера apscheduler —
только предупреждения и ошибки.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_PROBE = """
import logging
import awgbot.runtime.main
print(logging.getLevelName(logging.getLogger("apscheduler").getEffectiveLevel()))
"""


def test_the_scheduler_logger_is_quiet_after_the_main_process_sets_up_its_log():
    # свежий процесс: в этом уровень мог уже выставить чужой импорт
    env = {**os.environ, "BOT_TOKEN": os.environ.get("BOT_TOKEN", "12345:test-token"),
           "ADMIN_ID": os.environ.get("ADMIN_ID", "1")}
    proc = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=120,
                          env=env, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "WARNING", f"планировщик пишет в журнал на уровне {proc.stdout.strip()}"
