"""awg-bot.sh: часовой пояс при установке и обновлении.

Установщик пояс в app.yaml больше не пишет (без ключа бот берёт пояс хоста);
шаг обновления adopt_timezone ключ снимает всегда — комментирует, а не
очищает (пустое значение не дало бы стартовать прежней версии при откате), и
оставляет метку с прежним значением для уведомления на первом старте.
Функции прогоняются bash'ем из настоящего скрипта, timedatectl подменён."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "awg-bot.sh"
TEMPLATE = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


def _run(script: str, tmp_path: Path, app_text: str, host_tz: str = "Asia/Yekaterinburg"):
    conf, data, bin_ = tmp_path / "conf", tmp_path / "data", tmp_path / "bin"
    for d in (conf, bin_):
        d.mkdir(exist_ok=True)
    (conf / "app.yaml").write_text(app_text, encoding="utf-8")
    td = bin_ / "timedatectl"
    td.write_text(f"#!/bin/sh\necho '{host_tz}'\n", encoding="utf-8")
    td.chmod(0o755)
    prog = "\n".join([
        "set -euo pipefail",                     # как в настоящем скрипте
        'log() { echo "LOG $*"; }',
        f'CONF_DIR="{conf}"; DATA_DIR="{data}"',
        _func(script, "yaml_get"), _func(script, "host_timezone"), _func(script, "adopt_timezone"),
        "adopt_timezone",
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                       env={"PATH": f"{bin_}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    marker = data / ".tz-adopted"
    return ((conf / "app.yaml").read_text(encoding="utf-8"),
            marker.read_text(encoding="utf-8") if marker.exists() else None, r.stdout)


_APP = ('role: "client"\ntimezone: "{tz}"          # UTC+3, без переходов на летнее время\n'
        'docker:\n  runtime: "host"\n')


@pytest.mark.parametrize("old", ["Europe/Moscow", "Europe/London", "Asia/Yekaterinburg"])
def test_update_always_comments_the_key_out(script, tmp_path, old):
    """Совпадает ли ключ с поясом хоста или нет — снимается: пояс бота — пояс хоста."""
    text, marker, out = _run(script, tmp_path, _APP.format(tz=old))
    assert not re.search(r"^\s*timezone:", text, re.M), text
    assert f'# timezone: "{old}"' in text
    loaded = yaml.safe_load(text)
    assert "timezone" not in loaded and loaded["docker"]["runtime"] == "host" and loaded["role"] == "client"
    assert marker == old + "\n"
    assert f"пояс хоста (Asia/Yekaterinburg); ключ timezone в app.yaml снят (был: {old})" in out


def test_no_key_nothing_to_do(script, tmp_path):
    app = 'role: "client"\n# timezone: "Europe/Moscow"\n'
    text, marker, out = _run(script, tmp_path, app)
    assert text == app and marker is None and out == ""


def test_second_run_changes_nothing_and_keeps_the_marker(script, tmp_path):
    first, marker1, _ = _run(script, tmp_path, _APP.format(tz="Europe/Moscow"))
    conf = tmp_path / "conf" / "app.yaml"
    ino = os.stat(conf).st_ino
    prog = "\n".join([
        "set -euo pipefail",                     # как в настоящем скрипте
        'log() { echo "LOG $*"; }',
        f'CONF_DIR="{tmp_path / "conf"}"; DATA_DIR="{tmp_path / "data"}"',
        _func(script, "yaml_get"), _func(script, "host_timezone"), _func(script, "adopt_timezone"),
        "adopt_timezone"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                       env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout == ""
    assert conf.read_text(encoding="utf-8") == first and os.stat(conf).st_ino == ino
    assert (tmp_path / "data" / ".tz-adopted").read_text(encoding="utf-8") == marker1, \
        "повторный прогон не должен стирать прежнее значение"


def test_the_key_from_the_template_is_also_commented(script, tmp_path):
    """Прежний шаблон с активным ключом — и после шага бот берёт пояс хоста."""
    old_template = TEMPLATE.read_text(encoding="utf-8").replace(
        '# timezone: "Europe/Moscow"', 'timezone: "Europe/Moscow"', 1)
    text, marker, _ = _run(script, tmp_path, old_template)
    assert "timezone" not in yaml.safe_load(text) and marker == "Europe/Moscow\n"


def test_the_installer_no_longer_writes_the_zone(script):
    body = _func(script, "configure_topology_auto")
    assert "timezone" not in body.replace("ключа timezone", "")
    assert not re.search(r"yaml_set\w*\s+\S+\s+timezone", script), "пояс в app.yaml пишется установщиком"


def test_the_step_runs_in_post_update(script):
    post = _func(script, "cmd_post_update")
    assert post.index("adopt_timezone") < post.index("validate_config")


def test_logs_show_journald_time_with_offset(script):
    assert re.search(r'^cmd_logs\(\) \{ exec journalctl -u "\$SERVICE" -n 200 -f -o short-iso; \}', script, re.M)
    for hint in re.findall(r"journalctl -u (?:\$SERVICE|awg-bot) -e[^\"<']*", script):
        assert "-o short-iso" in hint, hint


def _prog(script: str, tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    conf, data, bin_ = tmp_path / "conf", tmp_path / "data", tmp_path / "bin"
    prog = "\n".join([
        "set -euo pipefail", 'log() { echo "LOG $*"; }', 'warn() { echo "WARN $*"; }',
        f'CONF_DIR="{conf}"; DATA_DIR="{data}"',
        _func(script, "yaml_get"), _func(script, "host_timezone"), _func(script, "adopt_timezone"),
        _func(script, "adopt_timezone_undo"), body])
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                          env={"PATH": f"{bin_}:/usr/bin:/bin"})


def test_interrupted_update_gets_the_key_back(script, tmp_path):
    """Прервалось обновление — ловушка возвращает прежний код, а с ним и
    app.yaml с ключом; метки уведомления нет."""
    app = _APP.format(tz="Europe/Moscow")
    _run(script, tmp_path, app)                              # заготовка каталогов и подмены timedatectl
    (tmp_path / "conf" / "app.yaml").write_text(app, encoding="utf-8")
    (tmp_path / "data" / ".tz-adopted").unlink()
    r = _prog(script, tmp_path, "adopt_timezone\nadopt_timezone_undo")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "conf" / "app.yaml").read_text(encoding="utf-8") == app
    assert not (tmp_path / "data" / ".tz-adopted").exists()
    assert "ключ timezone в app.yaml возвращён" in r.stdout


def test_undo_without_adopting_does_nothing(script, tmp_path):
    _run(script, tmp_path, 'role: "client"\n')
    r = _prog(script, tmp_path, "adopt_timezone_undo")
    assert r.returncode == 0 and r.stdout == ""


def test_quiet_mode_after_restore_leaves_no_marker(script, tmp_path):
    _run(script, tmp_path, 'role: "client"\n')
    (tmp_path / "conf" / "app.yaml").write_text(_APP.format(tz="Europe/Moscow"), encoding="utf-8")
    r = _prog(script, tmp_path, "adopt_timezone quiet")
    assert r.returncode == 0, r.stderr
    assert "timezone" not in yaml.safe_load((tmp_path / "conf" / "app.yaml").read_text(encoding="utf-8"))
    assert not (tmp_path / "data" / ".tz-adopted").exists()


def test_single_quotes_and_nested_keys(script, tmp_path):
    app = "role: 'client'\ntimezone: 'Europe/Moscow'\ngateway:\n  timezone: x\n"
    text, marker, _ = _run(script, tmp_path, app)
    assert marker == "Europe/Moscow\n", "одинарные кавычки — не часть значения"
    assert "\n  timezone: x\n" in text, "ключ не верхнего уровня не трогается"
    assert "# timezone: 'Europe/Moscow'" in text


def test_rescue_restore_and_wipe_wiring(script):
    rescue = _func(script, "post_update_rescue")
    back = rescue[rescue.index('restore_prev_code "$AWG_UPDATE_PREV"'):]
    assert back.index("adopt_timezone_undo") < back.index("else"), "ключ возвращается только вместе с прежним кодом"
    assert 'rm -f "$TZ_ADOPT_UNDO"' in _func(script, "update_done")
    post = _func(script, "cmd_post_update")
    assert '"$DATA_DIR/.tz-adopted"' in post[post.index('if [[ "$wipe" -eq 1 ]]'):], "wipe убирает и метку"
    restore = _func(script, "cmd_restore")
    i_had, i_conf = restore.index("tz_had=1"), restore.index('for f in "$copy/state/conf"/*.yaml')
    assert i_had < i_conf < restore.index('[[ "$tz_had" -eq 1 ]] || adopt_timezone quiet')
