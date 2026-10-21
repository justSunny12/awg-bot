"""install/lib/update.sh `refuse_container_runtime`: установка, где AmneziaWG
живёт в контейнере (`docker.runtime: "docker"` в app.yaml), на 4.0.0 не
обновляется — отказ во второй половине обновления до переименования
раздела (`adopt_awg_section`); `host`, раздел уже `awg:` или файла нет —
шаг молчит. Функции гоняются bash'ем из настоящего скрипта на временном
app.yaml.

Цена ошибки: контейнерная установка, пропущенная дальше, — бот 4.0.0 ходит к
awg хоста, где интерфейсов нет, клиенты без обслуживания, а раздел уже
переименован и прежний выпуск его не прочтёт; отказ на установке с host —
обновление не проходит ни у кого.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat
from tests.unit.test_adopt_awg_section import _OLD_APP

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


def _docker(runtime: str) -> str:
    """app.yaml 3.4.0 с заданной строкой runtime (как её пишет человек)."""
    out = _OLD_APP.replace('  runtime: "host"', f"  runtime: {runtime}")
    assert out != _OLD_APP or runtime == '"host"'
    return out


def _steps(script: str, tmp_path: Path, app: str | None) -> tuple[subprocess.CompletedProcess, list[str]]:
    """update_env_steps целиком: прочие шаги — журналом, refuse_container_runtime — настоящая."""
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if app is not None:
        (conf / "app.yaml").write_text(app, encoding="utf-8")
    log = tmp_path / "steps.log"
    stubs = "\n".join(f'{n}() {{ echo {n} >> "{log}"; }}' for n in (
        "ensure_python", "ensure_ping", "build_venv", "install_unit", "ensure_host_autostart",
        "ensure_awg_module_loaded", "ensure_awg_generation", "adopt_resolver", "update_adopts_timezone",
        "adopt_timezone", "adopt_awg_section", "adopt_link_keepalive", "seed_conf", "validate_config"))
    prog = "\n".join(["set -euo pipefail", 'die() { echo "DIE $*"; exit 1; }', f'CONF_DIR="{conf}"', stubs,
                      _func(script, "refuse_container_runtime"), _func(script, "update_env_steps"),
                      "update_env_steps restore", "echo continued"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20, env={"PATH": "/usr/bin:/bin"})
    return r, (log.read_text().split() if log.exists() else [])


@pytest.mark.parametrize("runtime", ['"docker"', "docker", '"docker"   # контейнер Amnezia'])
def test_a_container_install_is_refused_before_the_section_is_renamed(script, tmp_path, runtime):
    app = _docker(runtime)
    r, steps = _steps(script, tmp_path, app)
    assert r.returncode != 0 and "continued" not in r.stdout, f"контейнерная установка обновилась: {r.stdout}"
    assert "DIE AmneziaWG в контейнере (docker.runtime: docker)" in r.stdout and "awg-host-migrate" in r.stdout, r.stdout
    assert "adopt_awg_section" not in steps and "seed_conf" not in steps, \
        f"раздел переименован до отказа — прежний выпуск его не прочтёт: {steps}"
    assert (tmp_path / "conf" / "app.yaml").read_text(encoding="utf-8") == app, "отказ тронул app.yaml"


@pytest.mark.parametrize("app", [
    pytest.param(_OLD_APP, id="host-3.4.0-with-docker-words-in-comments"),
    pytest.param(TEMPLATE.read_text(encoding="utf-8"), id="awg-section-4.0.0"),
    pytest.param('docker:\n  container: "amnezia-awg2"\n', id="docker-section-without-runtime"),
    pytest.param('docker:\n  runtime: "host"\nother:\n  runtime: "docker"\n', id="runtime-docker-in-another-section"),
    pytest.param('docker:\n  # runtime: "docker"\n  runtime: "host"\n', id="commented-out-docker"),
    pytest.param(None, id="no-app-yaml"),
])
def test_a_host_install_goes_on_with_every_step(script, tmp_path, app):
    r, steps = _steps(script, tmp_path, app)
    assert r.returncode == 0 and "continued" in r.stdout, r.stdout + r.stderr
    assert "adopt_awg_section" in steps and "validate_config" in steps, steps
