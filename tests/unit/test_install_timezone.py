"""awg-bot.sh: часовой пояс при установке и обновлении.

Установщик пояс в app.yaml больше не пишет (без ключа бот берёт пояс хоста);
adopt_timezone ключ снимает — комментирует, а не очищает (пустое значение не
дало бы стартовать прежней версии при откате), и оставляет метку с прежним
значением для уведомления на первом старте. Обновление зовёт его один раз —
на переходе с выпуска ниже 3.4.0 (их шаблон и установщик писали ключ сами);
с 3.4.0 ключ — переопределение человека, обновление его не трогает.
Функции прогоняются bash'ем из настоящего скрипта, timedatectl подменён."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat
import yaml

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "awg-bot.sh"
TEMPLATE = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


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
        'awg:\n  interface: "awg0"\n')


@pytest.mark.parametrize("old", ["Europe/Moscow", "Europe/London", "Asia/Yekaterinburg"])
def test_update_always_comments_the_key_out(script, tmp_path, old):
    """Совпадает ли ключ с поясом хоста или нет — снимается: пояс бота — пояс хоста."""
    text, marker, out = _run(script, tmp_path, _APP.format(tz=old))
    assert not re.search(r"^\s*timezone:", text, re.M), text
    assert f'# timezone: "{old}"' in text
    loaded = yaml.safe_load(text)
    assert "timezone" not in loaded and loaded["awg"]["interface"] == "awg0" and loaded["role"] == "client"
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
    post = _func(script, "update_env_steps")
    assert "update_env_steps" in _func(script, "cmd_post_update")
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
    # сторож и откат набора ключа не касаются: переход с выпуска ниже 3.4.0 идёт
    # через версию совместимости, где сторожа нет
    for name in ("update_guard_rollback", "cmd_update_revert", "update_hold_take"):
        assert "adopt_timezone" not in _func(script, name) and "app.yaml" not in _func(script, name), name
    post = _func(script, "cmd_post_update")
    assert '"$DATA_DIR/.tz-adopted"' in post[post.index('if [[ "$wipe" -eq 1 ]]'):], "wipe убирает и метку"
    restore = _func(script, "cmd_restore") + _func(script, "restore_apply")   # до остановки — и после неё
    i_had, i_conf = restore.index("tz_had=1"), restore.index('for f in "$copy/state/conf"/*.yaml')
    assert i_had < i_conf < restore.index('[[ "$tz_had" -eq 1 ]] || adopt_timezone quiet')


# ── ключ timezone снимается один раз: на переходе с выпуска ниже 3.4.0 ──────

def _steps(script: str, tmp_path: Path, prev: str | None, mode: str = "") -> tuple[str, subprocess.CompletedProcess]:
    """update_env_steps настоящего скрипта (шаги сборки — заглушки) после
    обновления с прежней версии prev (None — прежнего кода нет)."""
    _run(script, tmp_path, 'role: "client"\n')                # каталоги и подмена timedatectl
    app = _APP.format(tz="Europe/Moscow")
    (tmp_path / "conf" / "app.yaml").write_text(app, encoding="utf-8")
    marker = tmp_path / "data" / ".tz-adopted"
    marker.unlink(missing_ok=True)
    prev_dir = tmp_path / "prev-code"
    if prev is not None:
        (prev_dir / "awgbot").mkdir(parents=True, exist_ok=True)
        (prev_dir / "awgbot" / "__version__.py").write_text(f'__version__ = "{prev}"\n', encoding="utf-8")
    stubs = "\n".join(f"{n}() {{ :; }}" for n in ("ensure_python", "ensure_ping", "build_venv", "install_unit",
                                                   "ensure_host_autostart", "ensure_awg_module_loaded",
                                                   "ensure_awg_generation", "adopt_resolver", "adopt_awg_section",
                                                   "adopt_link_keepalive", "seed_conf", "validate_config"))
    tz_below = re.search(r'^TZ_ADOPT_BELOW="[^"]*"', script, re.M).group(0)
    body = "\n".join([stubs, tz_below, _func(script, "tree_version"), _func(script, "version_lt"),
                      _func(script, "update_adopts_timezone"), _func(script, "update_env_steps"),
                      f'export AWG_UPDATE_PREV="{prev_dir}"', f"update_env_steps {mode}"])
    r = _prog(script, tmp_path, body)
    assert r.returncode == 0, r.stderr
    return (tmp_path / "conf" / "app.yaml").read_text(encoding="utf-8"), r


@pytest.mark.parametrize("prev", ["3.3.1", "3.3.9.4"])
def test_the_update_from_a_release_that_wrote_the_key_takes_it_off(script, tmp_path, prev):
    text, r = _steps(script, tmp_path, prev)
    assert "timezone" not in yaml.safe_load(text), "ключ шаблона прежнего выпуска остался"
    assert (tmp_path / "data" / ".tz-adopted").read_text(encoding="utf-8") == "Europe/Moscow\n"


@pytest.mark.parametrize("prev", ["3.4.0", "3.4.1", "3.5.0"])
def test_an_update_within_the_line_keeps_the_key_set_by_hand(script, tmp_path, prev):
    """С 3.4.0 ключ в app.yaml — переопределение человека: обновление (и
    догон набора) его не снимает и об этом не уведомляет."""
    text, r = _steps(script, tmp_path, prev)
    assert text == _APP.format(tz="Europe/Moscow"), "ключ, вписанный руками, снят обновлением"
    assert not (tmp_path / "data" / ".tz-adopted").exists() and "timezone" not in r.stdout


def test_without_the_old_code_or_in_a_restore_the_key_is_left_alone(script, tmp_path):
    """Прежнего кода нет (версия не читается) — снимать не по чему; второй
    половине восстановления пояс решает restore_apply после замены conf."""
    for prev, mode in ((None, ""), ("3.3.1", "restore")):
        text, _r = _steps(script, tmp_path, prev, mode)
        assert text == _APP.format(tz="Europe/Moscow"), (prev, mode)
