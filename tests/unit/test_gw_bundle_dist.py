"""
Файл первого применения везёт поставку с собой.

Шлюз стоит в России, где GitHub без туннеля недоступен, а туннель как раз и
ставится этим файлом: установщик по ссылке с raw.githubusercontent.com качал
поставку с releases и вставал на «качаю поставку» навсегда (наступили на
чистой машине 19.09.2026). Поставку собирает основной бот из СВОЕЙ установки
и вшивает в бандл; режим `--install` бандла распаковывает её и передаёт
управление установщику из неё.
"""
from __future__ import annotations

import base64
import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from awgbot.util import dist

ROOT = Path(__file__).resolve().parents[2]


# ── сборка поставки из установки ─────────────────────────────────────────────

def test_dist_archive_matches_release_layout():
    """Состав — как у build_release.sh: код, установщик, скрипты обвязки; без
    venv, кэша и чего-либо из /etc и /var."""
    blob = dist.archive(ROOT)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    for must in ("./awgbot/__main__.py", "./awg-bot.sh", "./install/awg-bot-install.sh",
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
    отказ с советом, а не бандл без половины."""
    (tmp_path / "awgbot").mkdir(); (tmp_path / "awgbot" / "__main__.py").write_text("")
    (tmp_path / "awg-bot.sh").write_text("")
    with pytest.raises(dist.DistError, match="awg-bot-install.sh"):
        dist.archive(tmp_path)


def test_greenfield_keeps_the_installer_in_opt():
    """Поставку для шлюза бот собирает из /opt; установщик обязан там остаться."""
    src = (ROOT / "install" / "awg-bot-install.sh").read_text(encoding="utf-8")
    assert 'rm -f "$INSTALL_DIR/install/awg-bot-install.sh"' not in src


# ── вшивание в бандл ─────────────────────────────────────────────────────────

@pytest.fixture()
def svc(tmp_path):
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    db = Database(tmp_path / "t.db"); db.init_schema()
    return Services(db)


_HEAD = b"#!/bin/sh\nset -e\necho body\nexit 0\n"
_MARK = b"#__GW_SETUP_BELOW__\n#!/bin/sh\necho gw-setup\n"


def test_plain_bundle_carries_the_distribution(svc):
    out = svc._bundle_with_dist(_HEAD + _MARK)
    assert out.startswith(_HEAD) and out.endswith(_MARK), "тело и скрипт обвязки не тронуты"
    m = re.search(rb"^#__AWG_BOT_TGZ_BELOW__\n(.*?)^#__AWG_BOT_TGZ_END__\n", out, re.S | re.M)
    assert m and out.index(b"#__AWG_BOT_TGZ_BELOW__") < out.index(b"#__GW_SETUP_BELOW__"), \
        "поставка — до маркера скрипта обвязки, иначе уедет в routing-gw-setup.sh"
    assert base64.b64decode(m.group(1)) == dist.archive()
    assert all(len(ln) <= 76 for ln in m.group(1).splitlines()), "строками, а не одной простынёй"


def test_bundle_without_marker_is_left_alone(svc):
    assert svc._bundle_with_dist(b"garbage") == b"garbage"


def test_only_the_first_application_file_carries_it(monkeypatch, svc):
    """Шифрованный бандл для чата — машине, где агент уже стоит: поставку не
    везёт, иначе каждое перевыпуск тянул бы мегабайты через Telegram."""
    from types import SimpleNamespace
    gw = SimpleNamespace(id=1, link_if="awglink")
    monkeypatch.setattr(svc, "_gw_slot", lambda slot_id=None: gw)
    monkeypatch.setattr(svc, "_gw_bundle_build", lambda g: (_HEAD + _MARK, "K"))
    monkeypatch.setattr("awgbot.util.bundlecrypt.encrypt", lambda plain, priv: plain)
    plain, _ = svc.gw_bundle_plain()
    enc, _ = svc.gw_bundle_encrypted()
    assert b"#__AWG_BOT_TGZ_BELOW__" in plain and b"#__AWG_BOT_TGZ_BELOW__" not in enc


# ── режим --install бандла: прогоняем НАСТОЯЩЕЕ тело бандла ───────────────────

def _bundle_body() -> str:
    """Тело бандла из скрипта линка — от `set -e` до `BODYEOF`."""
    src = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
    return src.split("cat <<'BODYEOF'\n", 1)[1].split("\nBODYEOF", 1)[0]


def _fake_bundle(tmp_path, with_dist: bool) -> Path:
    body = _bundle_body().split("# Раскладываем в ПОСТОЯННЫЙ каталог", 1)[0]
    body = body.replace('[ "$(id -u)" = "0" ] || { echo "нужен root: sudo sh $0"; exit 1; }', ":")
    parts = ["#!/bin/sh\n", body, "echo APPLY-MODE\nexit 0\n"]
    if with_dist:
        stage = tmp_path / "stage"; (stage / "install").mkdir(parents=True)
        (stage / "install" / "awg-bot-install.sh").write_text(
            '#!/bin/bash\necho "INSTALLER $*"\n', encoding="utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(stage, arcname=".")
        parts.append("#__AWG_BOT_TGZ_BELOW__\n" + base64.encodebytes(buf.getvalue()).decode()
                     + "#__AWG_BOT_TGZ_END__\n")
    parts.append("#__GW_SETUP_BELOW__\n#!/bin/sh\necho gw-setup\n")
    f = tmp_path / "awg-gw-bundle.sh"
    f.write_text("".join(parts), encoding="utf-8")
    return f


def test_install_mode_unpacks_and_hands_over_to_the_installer(tmp_path):
    f = _fake_bundle(tmp_path, with_dist=True)
    r = subprocess.run(["sh", str(f), "--install"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"INSTALLER --skip-verify --role gateway --bundle {f}", \
        "установщик из поставки, без сверки с GitHub, бандл — абсолютным путём"


def test_install_mode_without_distribution_says_so(tmp_path):
    """Бандл из `awg-bot gw-bundle` поставки не везёт — честный отказ, а не
    пустой архив."""
    f = _fake_bundle(tmp_path, with_dist=False)
    r = subprocess.run(["sh", str(f), "--install"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 1 and "нет поставки" in r.stdout


def test_plain_run_ignores_the_embedded_distribution(tmp_path):
    """Обычный запуск (установщик зовёт `sh bundle`): поставка после exit не
    исполняется и в скрипт обвязки не попадает."""
    f = _fake_bundle(tmp_path, with_dist=True)
    r = subprocess.run(["sh", str(f)], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout.strip() == "APPLY-MODE"
    tail = subprocess.run(["sh", "-c", f"sed -n '/^#__GW_SETUP_BELOW__$/,$p' '{f}' | tail -n +2"],
                          capture_output=True, text=True).stdout
    assert "AWG_BOT_TGZ" not in tail and tail.strip().endswith("echo gw-setup")


def test_instructions_do_not_send_the_gateway_to_github():
    from types import SimpleNamespace
    from awgbot.bot import texts
    text = texts.gateway_install_instructions(SimpleNamespace(name="Pi", address="10.9.0.5/32"))
    assert "--install" in text and "githubusercontent" not in text
    src = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    stop = src.split("нет файла первого применения", 1)[1].split('"', 1)[0]
    assert "--install" in stop and "githubusercontent" not in stop
