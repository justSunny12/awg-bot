"""
Общее для test_gw_setup_script_*.py — проверок routing-gw-setup.sh, обвязки
шлюза: путь к скрипту, фикстура его текста, прогон куска под sh, say/run.

Шлюз стоит в чужом доме, за NAT, и попасть на него сложнее, чем на ВПС. Его
отказы видны только со стороны сервера и выглядят одинаково — «интернета за
шлюзом нет», — поэтому цена молчаливой поломки здесь особенно высока.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.conftest import SH


SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


def _sh(prog: str, *, path: str = "/usr/bin:/bin") -> subprocess.CompletedProcess:
    return subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                          env={"PATH": path})


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _helpers(script: str) -> str:
    """say и run — как в скрипте: run печатает и исполняет через sh -c."""
    start = script.index("\nsay()  {") + 1
    return script[start:script.index("\n}\n", script.index("\nrun()  {", start)) + 3]
