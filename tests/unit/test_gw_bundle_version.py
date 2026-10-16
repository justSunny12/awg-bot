"""
Файл первого применения против агента на шлюзе: скрипт обвязки — той же
версии, что агент. Агент новее файла — скрипт из поставки агента; старше —
отказ с подсказкой обновить агента из этого же файла (--install); агента нет
или версия файла не известна — скрипт из файла. Прогоняется НАСТОЯЩЕЕ тело
файла из скрипта линка.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.conftest import SH

ROOT = Path(__file__).resolve().parents[2]


def _body() -> str:
    src = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
    return src.split("cat <<'BODYEOF'\n", 1)[1].split("\nBODYEOF", 1)[0]


def _run(tmp_path, *, file_ver: str, agent_ver: str | None, argv=()):
    """Файл версии file_ver на шлюзе с агентом agent_ver (None — агента нет);
    вместо применения — какой скрипт обвязки запустился бы."""
    agent = tmp_path / "opt"
    if agent_ver is not None:
        (agent / "awgbot").mkdir(parents=True); (agent / "install").mkdir()
        (agent / "awgbot" / "__version__.py").write_text(f'__version__ = "{agent_ver}"\n', encoding="utf-8")
        (agent / "install" / "routing-gw-setup.sh").write_text("#!/bin/sh\necho agent-script\n", encoding="utf-8")
    dest = tmp_path / "apply"; dest.mkdir()
    body = (_body()
            .replace('[ "$(id -u)" = "0" ] || { echo "нужен root: sudo sh $0"; exit 1; }', ":")
            .replace('DEST="$(mktemp -d /root/awg-gw-apply.XXXXXX)"', f'DEST="{dest}"')
            .replace('_agent_dir="/opt/awg-bot"', f'_agent_dir="{agent}"'))
    assert f'DEST="{dest}"' in body and f'_agent_dir="{agent}"' in body
    text = (f'#!/bin/sh\nBUNDLE_VERSION="{file_ver}"\n' + body
            + '\n"$DEST/routing-gw-setup.sh"\nexit 0\n#__GW_SETUP_BELOW__\n#!/bin/sh\necho file-script\n')
    f = tmp_path / "awg-gw-bundle.sh"; f.write_text(text, encoding="utf-8")
    return subprocess.run([SH, str(f), *argv], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}), f, dest


@pytest.mark.parametrize("file_ver, agent_ver, ran", [
    ("3.3.1", "3.3.1", "file-script"),
    ("3.3.0", "3.3.1", "agent-script"),           # сервер 3.3.0, агент 3.3.1: скрипт агента
    ("3.3.1", "3.3.1.2", "agent-script"),          # локальная сборка новее файла
    ("3.3.1", None, "file-script"),                # агента нет — чистая машина
    ("", "3.3.0", "file-script"),                  # версия файла не известна — как прежде
])
def test_the_plumbing_script_matches_the_agent(tmp_path, file_ver, agent_ver, ran):
    r, _f, _dest = _run(tmp_path, file_ver=file_ver, agent_ver=agent_ver)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == ran, r.stdout


def test_an_agent_older_than_the_file_is_sent_to_install(tmp_path):
    """Сервер 3.3.1, агент 3.3.0: скрипт 3.3.1 поверх агента 3.3.0 поставил бы
    таблицу нового имени рядом с прежней — отказ до применения и подсказка
    обновить агента из этого же файла."""
    r, f, dest = _run(tmp_path, file_ver="3.3.1", agent_ver="3.3.0")
    assert r.returncode == 1
    assert "file-script" not in r.stdout and "agent-script" not in r.stdout
    assert f"старше файла (3.3.1) — сначала обнови агента из этого же файла: sudo sh {f} --install" in r.stdout
    assert not dest.exists(), "временный каталог с ключом остался"


def test_rollback_is_not_held_by_the_version(tmp_path):
    r, _f, _dest = _run(tmp_path, file_ver="3.3.1", agent_ver="3.3.0", argv=("--rollback",))
    assert r.returncode == 0 and r.stdout.strip().endswith("file-script"), r.stdout


def test_the_file_carries_the_server_version(tmp_path):
    """Версию файл берёт из поставки рядом со скриптом линка."""
    from tests.unit.test_gw_bundle import _emit_bundle
    d = tmp_path / "srv"; d.mkdir(); (d / "awgbot").mkdir()
    (d / "awgbot" / "__version__.py").write_text('__version__ = "3.3.1"\n', encoding="utf-8")
    text = _emit_bundle(d)
    assert 'BUNDLE_VERSION="3.3.1"\n' in text.split("#__GW_SETUP_BELOW__", 1)[0]
