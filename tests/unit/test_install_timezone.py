"""awg-bot.sh: часовой пояс при установке и время в журнале.

Установщик пояс в app.yaml не пишет: без ключа бот берёт пояс хоста, ключ
timezone — переопределение человека, обновление его не трогает. Журнал бота
показывается временем journald со смещением."""
from __future__ import annotations

import re

import pytest

from tests.botscript import flat

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


def test_the_installer_no_longer_writes_the_zone(script):
    body = _func(script, "configure_topology_auto")
    assert "timezone" not in body.replace("ключа timezone", "")
    assert not re.search(r"yaml_set\w*\s+\S+\s+timezone", script), "пояс в app.yaml пишется установщиком"


def test_no_step_of_the_update_or_the_restore_touches_the_key(script):
    for name in ("update_env_steps", "restore_apply", "post_update_rescue"):
        assert "timezone" not in _func(script, name), name


def test_logs_show_journald_time_with_offset(script):
    assert re.search(r'^cmd_logs\(\) \{ exec journalctl -u "\$SERVICE" -n 200 -f -o short-iso; \}', script, re.M)
    for hint in re.findall(r"journalctl -u (?:\$SERVICE|awg-bot) -e[^\"<']*", script):
        assert "-o short-iso" in hint, hint
