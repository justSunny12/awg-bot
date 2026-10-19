"""`awg-bot gw-kit --stdout` — поставка для шлюза без доступа к GitHub
(tools/gwkit.py): код агента той же версии (util/dist) и исходники AmneziaWG
из кэша сервера по install/awg.lock, без ключей и токенов; `--join` в ките
передаётся установщику. Плюс сама сборка поставки из установки (util/dist):
кит собирается из неё.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.botscript import flat

from awgbot.util import dist
from tools import gwkit

ROOT = Path(__file__).resolve().parents[2]
TOOLS = b"DUMMY-amneziawg-tools-source"
MODULE = b"DUMMY-amneziawg-module-source"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """Установка-заглушка (состав, который требует dist) с подставным
    установщиком, пишущим свои аргументы, и кэш исходников AmneziaWG."""
    root = tmp_path / "opt"
    (root / "awgbot").mkdir(parents=True)
    (root / "install").mkdir()
    (root / "awgbot" / "__main__.py").write_text("", encoding="utf-8")
    (root / "awgbot" / "__version__.py").write_text('__version__ = "3.4.0"\n', encoding="utf-8")
    (root / "awg-bot.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "install" / "routing-gw-setup.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "install" / "lib").mkdir()
    for part in ("update.sh", "restore.sh"):               # части awg-bot.sh — без них он не разберётся
        (root / "install" / "lib" / part).write_text("# part\n", encoding="utf-8")
    (root / "install" / "awg-bot-install.sh").write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" "TOK=${AWG_JOIN_TOKEN-}" > "$KIT_ARGS"\n', encoding="utf-8")
    (root / "install" / "awg.lock").write_text(
        "AWG_GENERATION=1\n"
        "AWG_MODULE_TAG=v3.1.20260906\n"
        f"AWG_MODULE_SHA256={hashlib.sha256(MODULE).hexdigest()}\n"
        "AWG_TOOLS_TAG=v3.1.20260812\n"
        f"AWG_TOOLS_SHA256={hashlib.sha256(TOOLS).hexdigest()}\n", encoding="utf-8")
    cache = tmp_path / "awg-src"
    cache.mkdir()
    (cache / "amneziawg-tools-3.1.20260812.tar.gz").write_bytes(TOOLS)
    (cache / "amneziawg-linux-kernel-module-3.1.20260906.tar.gz").write_bytes(MODULE)
    return root, cache


def test_the_kit_carries_the_awg_sources_and_the_code_and_no_secrets(tmp_path):
    root, cache = _tree(tmp_path)
    kit = gwkit.build(root, cache).decode("utf-8")
    assert kit.startswith("#!/bin/sh\n") and "awg-bot v3.4.0" in kit
    for name in ("amneziawg-tools-3.1.20260812.tar.gz", "amneziawg-linux-kernel-module-3.1.20260906.tar.gz",
                 "awg-bot.tgz"):
        assert f"{gwkit.MARK}{name}\n" in kit, name
    for secret in ("PrivateKey", "PresharedKey", "BOT_TOKEN", "ADMIN_ID=", "GW_BOT_TOKEN"):
        assert secret not in kit, f"в ките {secret}"
    assert not re.search(r"AWGJ1\.[A-Za-z0-9_-]{8,}", kit), "в ките токен подключения"
    r = subprocess.run(["sh", "-n"], input=kit.split("exit 0\n", 1)[0], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_kit_refuses_sources_that_do_not_match_the_lock(tmp_path):
    root, cache = _tree(tmp_path)
    (cache / "amneziawg-tools-3.1.20260812.tar.gz").write_bytes(b"DUMMY-other")
    with pytest.raises(gwkit.KitError, match="sha256"):
        gwkit.build(root, cache)
    (cache / "amneziawg-tools-3.1.20260812.tar.gz").unlink()
    with pytest.raises(gwkit.KitError, match="awg-bot awg install"):
        gwkit.build(root, cache)


def _run_kit(tmp_path, *args):
    root, _cache = _tree(tmp_path)
    gw_cache = tmp_path / "gw-awg-src"
    kit = gwkit.build(root, _cache).decode("utf-8").replace(f'_cache="{_cache}"', f'_cache="{gw_cache}"', 1)
    kit = kit.replace('[ "$(id -u)" = "0" ] ||', "true ||", 1)          # тест не под root
    path = tmp_path / "awg-gw-kit.sh"
    path.write_text(kit, encoding="utf-8")
    out = tmp_path / "args"
    r = subprocess.run(["sh", str(path), *args], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "KIT_ARGS": str(out), "TMPDIR": str(tmp_path)})
    return r, out, gw_cache


def test_the_kit_unpacks_the_sources_and_hands_the_token_to_the_installer(tmp_path):
    """Исходники — в кэш установщика ядра на шлюзе байт в байт (его fetch
    сверит их с awg.lock и в сеть не пойдёт); установщик из поставки — со
    `--skip-verify --join <токен>`; временный каталог убран."""
    r, out, gw_cache = _run_kit(tmp_path, "--join", "AWGJ1.DUMMYTOKEN")
    assert r.returncode == 0, r.stdout + r.stderr
    # sec-2: токен — установщику окружением, не в argv
    assert out.read_text(encoding="utf-8").split() == ["--skip-verify", "--join", "TOK=AWGJ1.DUMMYTOKEN"]
    assert (gw_cache / "amneziawg-tools-3.1.20260812.tar.gz").read_bytes() == TOOLS
    assert (gw_cache / "amneziawg-linux-kernel-module-3.1.20260906.tar.gz").read_bytes() == MODULE


def test_the_kit_finds_the_token_in_a_whole_pasted_command(tmp_path):
    cmd = ("curl -fsSL https://raw.githubusercontent.com/DUMMY/awg-bot/main/install/awg-bot-install.sh "
           "| sudo bash -s -- --join AWGJ1.DUMMYTOKEN").split()
    r, out, _ = _run_kit(tmp_path, "--join", *cmd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.read_text(encoding="utf-8").split()[-1] == "TOK=AWGJ1.DUMMYTOKEN"


def test_the_kit_without_a_token_says_what_it_needs(tmp_path):
    r, out, _ = _run_kit(tmp_path)
    assert r.returncode == 2 and "токен подключения" in r.stdout and "<токен AWGJ1…" in r.stdout, r.stdout + r.stderr
    assert not out.exists(), "установщик позван без токена"


def test_the_kit_passes_yes_to_the_installer(tmp_path):
    """flow-6: у трубы кита терминала нет — «подключить заново?» снимает
    только --yes; кит передаёт его установщику."""
    r, out, _ = _run_kit(tmp_path, "--join", "AWGJ1.DUMMYTOKEN", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.read_text(encoding="utf-8").split() == ["--skip-verify", "--yes", "--join", "TOK=AWGJ1.DUMMYTOKEN"]


def test_the_kit_is_refused_when_the_set_names_another_agent_version():
    """flow-5: кит везёт код сервера; команда подключения назовёт версию
    агента в выпуске набора — другая: отказ до сборки; та же или неизвестна
    (база не читается — сверит установщик на шлюзе) — собирается."""
    with pytest.raises(gwkit.KitError, match="ставит агента v3.4.0 .* кода сервера v3.4.1"):
        gwkit.check_version("3.4.1", "3.4.0")
    gwkit.check_version("3.4.1", "3.4.1")
    gwkit.check_version("3.4.1", "")


def test_the_hints_name_the_token_not_the_whole_command():
    """flow-6: вся строка из бота с `| sudo bash` в кавычках не сработает —
    подсказки зовут только токен."""
    for text in (flat(), (ROOT / "tools" / "gwkit.py").read_text(encoding="utf-8")):
        assert "<команда из бота" not in text
        assert "--join <токен AWGJ1…>" in text


def test_the_cli_verb_is_gw_kit_and_gw_bundle_is_gone():
    sh = flat()
    assert "gw-kit)      cmd_gw_kit" in sh and "gw-bundle)" not in sh
    assert "bot_tool tools.gwkit --stdout" in sh
    assert "sudo awg-bot gw-kit --stdout | " in sh


def test_the_kit_refuses_a_terminal_as_its_output(monkeypatch):
    import sys
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert gwkit.main(["--stdout"]) == 2
    assert gwkit.main([]) == 2


# ── поставка из установки (util/dist) ────────────────────────────────────────

def test_dist_archive_matches_release_layout():
    """Состав — как у build_release.sh: код, установщик, скрипты обвязки; без
    venv, кэша и чего-либо из /etc и /var."""
    blob = dist.archive(ROOT)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    for must in ("./awgbot/__main__.py", "./awg-bot.sh", "./install/awg-bot-install.sh",
                 "./install/lib/update.sh", "./install/lib/restore.sh",
                 "./install/routing-gw-setup.sh", "./install/routing-link-setup.sh",
                 "./install/awg.lock", "./requirements.txt", "./conf/app.yaml"):
        assert must in names, must
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert not any(n.startswith("./venv") or n.startswith("./.venv") or n.startswith("./tests")
                   for n in names)


def test_dist_archive_is_built_once_per_process():
    assert dist.archive(ROOT) is dist.archive(ROOT)


def test_dist_archive_refuses_an_incomplete_install(tmp_path):
    """Установка без установщика внутри (так оставлял старый greenfield):
    отказ с советом, а не кит без половины."""
    (tmp_path / "awgbot").mkdir(); (tmp_path / "awgbot" / "__main__.py").write_text("")
    (tmp_path / "awg-bot.sh").write_text("")
    with pytest.raises(dist.DistError, match="awg-bot-install.sh"):
        dist.archive(tmp_path)


def test_greenfield_keeps_the_installer_in_opt():
    """Кит для шлюза (gw-kit) собирается из /opt; установщик обязан там остаться."""
    src = (ROOT / "install" / "awg-bot-install.sh").read_text(encoding="utf-8")
    assert 'rm -f "$INSTALL_DIR/install/awg-bot-install.sh"' not in src


def test_dist_archive_is_owned_by_root():
    """tar от root распаковывает с владельцем из архива: uid машины, где
    собрали, сделал бы код в /opt чужим и доступным на запись."""
    blob = dist.archive(ROOT)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        owners = {(m.uid, m.gid, m.uname, m.gname) for m in tar.getmembers()}
    assert owners == {(0, 0, "root", "root")}, owners


def test_dist_and_release_carry_no_unit_file():
    """Юнит пишет install_unit по конфигу хоста; файл-образец в поставке был
    докерным и по §12 README копировался руками — на host-режиме не стартовал."""
    blob = dist.archive(ROOT)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "./awg-bot.service" not in names and "awg-bot.service" not in dist._ENTRIES
    assert not (ROOT / "awg-bot.service").exists()
    build = (ROOT / "build_release.sh").read_text(encoding="utf-8")
    assert "awg-bot.service" not in build.split("build_bot() {", 1)[1].split("\n}\n", 1)[0]
    assert "git archive" in build.split("build_project() {", 1)[1], "полный проект снова пакуется из рабочего дерева"


def test_the_cli_refuses_before_building(monkeypatch, capsys):
    from awgbot.core import config
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")
    monkeypatch.setattr(gwkit, "join_version", lambda: "3.4.0")
    monkeypatch.setattr(gwkit, "build", lambda *a, **kw: pytest.fail("кит собран вопреки отказу"))
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert gwkit.main(["--stdout"]) == 1
    assert "выпуск набора" in capsys.readouterr().err
