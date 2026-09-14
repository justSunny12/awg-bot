"""Скрипты обвязки прописывают СЕБЯ в systemd-юнит. Путь обязан быть постоянным.

Бандл шлюза распаковывается во временный каталог, а systemd-tmpfiles вычищает
его через десять дней. Пока в юнит уходил `readlink -f "$0"`, автозапуск линка
умирал молча: интерфейс уже стоял, RemainAfterExit держал юнит «активным», и
отказ всплывал только при первой перезагрузке — как «за шлюзом нет интернета»,
без связи с каким-либо действием. Та же дыра была у обвязки ВПС и у линка: на
ВПС не выстрелило только потому, что скрипты запускали из постоянного места.

Одна проверка на все три скрипта: install_self прогоняется по-настоящему с
подставным install(1), и ExecStart юнита обязан идти от её результата.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ["routing-gw-setup.sh", "routing-host-setup.sh", "routing-link-setup.sh"]


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"нет функции {name}()"
    return m.group(0)


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_self_copies_to_a_permanent_place_and_returns_that_path(name, tmp_path):
    src = (ROOT / "install" / name).read_text(encoding="utf-8")
    fn = _func(src, "install_self")

    shim = tmp_path / "bin"; shim.mkdir()
    log = tmp_path / "install.log"
    (shim / "install").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n', encoding="utf-8")
    (shim / "install").chmod(0o755)
    (shim / "mkdir").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (shim / "mkdir").chmod(0o755)
    me = tmp_path / "tmp-extract" / name
    me.parent.mkdir(); me.write_text("#!/bin/sh\n", encoding="utf-8")

    r = subprocess.run(["sh", "-c", fn + "\ninstall_self", str(me)],
                       capture_output=True, text=True,
                       env={"PATH": f"{shim}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"/usr/local/sbin/{name}", "в stdout — только постоянный путь"
    assert log.read_text().split() == ["-m", "0755", str(me), f"/usr/local/sbin/{name}"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_unit_execstart_uses_the_installed_copy(name):
    src = (ROOT / "install" / name).read_text(encoding="utf-8")
    assert 'SELF="$(install_self)"' in src
    assert 'SELF="$(readlink -f "$0")"' not in src, "вернулся путь запуска"
    exec_lines = [ln for ln in src.splitlines() if ln.startswith("ExecStart=")]
    assert exec_lines and all("$SELF" in ln for ln in exec_lines), exec_lines
    assert src.index('SELF="$(install_self)"') < src.index("ExecStart="), \
        "юнит пишется раньше, чем скрипт лёг на постоянное место"
