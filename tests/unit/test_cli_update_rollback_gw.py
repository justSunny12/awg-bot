"""
awg-bot.sh: откат прерванного обновления возвращает и копии скриптов в
/usr/local/sbin (обвязка и линк к резерву у шлюза, обвязка, линк и линк пары
у ВПС) — их выполняют юниты при загрузке, и прежний бот при скриптах новой
версии бесконечно реассертил бы чужую обвязку. Ядро AmneziaWG откат не
возвращает — и говорит об этом строкой, а не молча.
"""
from __future__ import annotations

import subprocess

import pytest

from tests.botscript import flat
from tests.unit.test_cli_script import _extract_func



@pytest.fixture(scope="module")
def script() -> str:
    return flat()


_OLD_GW = "#!/bin/sh\n# обвязка прежней версии\n"
_NEW_GW = "#!/bin/sh\n# обвязка новой версии\n"


def _host(tmp_path, *, copy: str | None, name: str = "routing-gw-setup.sh",
          role: str = "gateway", lock: tuple[str, str] = ("AWG_TAG=a\n", "AWG_TAG=a\n")):
    """Установка после подмены кода: в $INSTALL_DIR новая поставка, в
    .prev-code — прежняя, в sbin — копия скрипта name (или её нет);
    lock — awg.lock (прежний, новый)."""
    inst = tmp_path / "opt"; (inst / "install").mkdir(parents=True)
    (inst / "install" / name).write_text(_NEW_GW, encoding="utf-8")
    (inst / "install" / "awg.lock").write_text(lock[1], encoding="utf-8")
    (inst / "awg-bot.sh").write_text("new", encoding="utf-8")
    old_env = inst / ".venvs" / "A"; old_env.mkdir(parents=True)
    new_env = inst / ".venvs" / "B"; new_env.mkdir()
    (inst / "venv").symlink_to(new_env)
    (inst / ".venvs" / ".prev").write_text(str(old_env) + "\n", encoding="utf-8")
    prev = inst / ".prev-code"; (prev / "install").mkdir(parents=True)
    (prev / "install" / name).write_text(_OLD_GW, encoding="utf-8")
    (prev / "install" / "awg.lock").write_text(lock[0], encoding="utf-8")
    (prev / "awg-bot.sh").write_text("old", encoding="utf-8")
    (prev / "awgbot").mkdir(); (prev / "awgbot" / "__main__.py").write_text("# old\n", encoding="utf-8")
    sbin = tmp_path / "sbin"; sbin.mkdir()
    if copy is not None:
        (sbin / name).write_text(copy, encoding="utf-8")
        (sbin / name).chmod(0o755)
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "app.yaml").write_text(f'role: "{role}"\n', encoding="utf-8")
    return inst, prev, sbin


def _run(script, tmp_path, inst, prev, sbin, *, trap: str):
    log = tmp_path / "log"
    funcs = ["yaml_get", "venv_rollback", "sbin_copies", "sbin_copy_sync", "restore_prev_code", trap]
    prog = "\n".join([
        f'INSTALL_DIR="{inst}"; SERVICE=awg-bot; AWG_UPDATE_PREV="{prev}"; UPDATE_PREV="{prev}"',
        f'ENV_FILE=/x; CONF_DIR="{tmp_path / "conf"}"; DATA_DIR=/x',
        'warn() { echo "WARN $*"; }',
        f'systemctl() {{ echo "systemctl $*" >> "{log}"; }}',
        *(_extract_func(script, f).replace("/usr/local/sbin", str(sbin)) for f in funcs
          if f"\n{f}() {{" in script),
        f"false; {trap}",
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin"})
    return r


@pytest.mark.parametrize("trap", ["post_update_rescue", "update_rescue"])
def test_a_failed_update_brings_back_the_plumbing_copy(script, tmp_path, trap):
    """Вторая половина успела положить скрипт обвязки новой версии и упала:
    вместе с кодом возвращается и копия в /usr/local/sbin, с битом исполнения."""
    inst, prev, sbin = _host(tmp_path, copy=_NEW_GW)
    _run(script, tmp_path, inst, prev, sbin, trap=trap)
    copy = sbin / "routing-gw-setup.sh"
    assert (inst / "awg-bot.sh").read_text(encoding="utf-8") == "old"
    assert copy.read_text(encoding="utf-8") == _OLD_GW, "копия обвязки осталась от новой версии"
    assert copy.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("role,name", [
    ("gateway", "reserve-gw-setup.sh"),
    ("client", "routing-host-setup.sh"),
    ("client", "routing-link-setup.sh"),
    ("client", "reserve-pair-setup.sh"),
])
def test_a_failed_update_brings_back_every_script_copy_of_the_role(script, tmp_path, role, name):
    """Откат возвращал только копию обвязки шлюза: копии обвязки и линков ВПС
    и линка к резерву шлюза, освежённые обновлением, оставались новой версии
    при прежнем коде."""
    inst, prev, sbin = _host(tmp_path, copy=_NEW_GW, name=name, role=role)
    r = _run(script, tmp_path, inst, prev, sbin, trap="post_update_rescue")
    assert (sbin / name).read_text(encoding="utf-8") == _OLD_GW, f"{name} осталась от новой версии"
    assert "WARN не удалось вернуть" not in r.stdout


def test_without_a_plumbing_copy_the_rollback_puts_none(script, tmp_path):
    """Обвязку тут не разворачивали (сервер AWG, агент без файла конфигурации):
    откат копию не создаёт."""
    inst, prev, sbin = _host(tmp_path, copy=None)
    r = _run(script, tmp_path, inst, prev, sbin, trap="post_update_rescue")
    assert not (sbin / "routing-gw-setup.sh").exists()
    assert "WARN не удалось вернуть" not in r.stdout


def test_the_rollback_says_the_kernel_stays_new(script, tmp_path):
    """Ядро AmneziaWG и утилиты собраны по поставке новой версии до подмены
    кода, и откат их не возвращает: при другой версии ядра в прежней поставке —
    строка об этом, иначе админ считал бы, что вернулось всё."""
    inst, prev, sbin = _host(tmp_path, copy=None, lock=("AWG_TAG=old\n", "AWG_TAG=new\n"))
    r = _run(script, tmp_path, inst, prev, sbin, trap="post_update_rescue")
    assert "WARN ядро AmneziaWG и утилиты awg не откатываются" in r.stdout, r.stdout


def test_the_rollback_keeps_quiet_about_an_unchanged_kernel(script, tmp_path):
    inst, prev, sbin = _host(tmp_path, copy=None)
    r = _run(script, tmp_path, inst, prev, sbin, trap="post_update_rescue")
    assert "ядро" not in r.stdout, r.stdout
