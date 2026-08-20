"""Бандл для шлюза: сборка одним файлом из routing-link-setup.sh.

Бандл едет на ЧУЖУЮ машину, несёт приватный ключ линка и правит там systemd.
Ошибка в нём обнаруживается на домашнем шлюзе, куда ещё надо дойти. Поэтому
собираем его здесь настоящим скриптом и проверяем результат, а не исходник.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINK = ROOT / "install" / "routing-link-setup.sh"
GW = ROOT / "install" / "routing-gw-setup.sh"

_CONF = """[Interface]
Address = 10.99.99.2/30
PrivateKey = SECRETPRIVKEY==
H1 = 12345-67890

[Peer]
PublicKey = SRVPUB==
PresharedKey = PSKPSK==
Endpoint = 203.0.113.10:443
AllowedIPs = 10.8.1.0/24, 10.99.99.0/30
"""


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> str:
    """Собирает бандл настоящим скриптом и отдаёт его текст."""
    d = tmp_path_factory.mktemp("gwb")
    inst = d / "install"; inst.mkdir()
    # копии рядом: emit_gw_bundle ищет gw-скрипт по соседству с собой
    src = LINK.read_text(encoding="utf-8").replace(
        '[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }', ":", 1)
    (inst / "routing-link-setup.sh").write_text(src, encoding="utf-8")
    (inst / "routing-gw-setup.sh").write_text(GW.read_text(encoding="utf-8"),
                                              encoding="utf-8")
    conf = d / "gw.conf"; conf.write_text(_CONF, encoding="utf-8")
    out = d / "awg-gw-bundle.sh"

    r = subprocess.run(
        ["sh", str(inst / "routing-link-setup.sh"), "--bundle"],
        cwd=d, capture_output=True, text=True, errors="replace",
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
             "GW_CONF_OUT": str(conf), "GW_BUNDLE_OUT": str(out)})
    assert r.returncode == 0, r.stderr
    assert out.exists(), r.stdout
    return out.read_text(encoding="utf-8")


def test_bundle_is_valid_shell(bundle, tmp_path):
    f = tmp_path / "b.sh"; f.write_text(bundle, encoding="utf-8")
    r = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_bundle_carries_the_link_config_verbatim(bundle):
    """Конфиг должен доехать байт в байт: это ключи, а не текст."""
    for line in _CONF.strip().splitlines():
        assert line in bundle, line


def test_bundle_embeds_the_gw_script_byte_for_byte(bundle, tmp_path):
    """Вложенный скрипт извлекается тем же sed, что и на шлюзе."""
    f = tmp_path / "b.sh"; f.write_text(bundle, encoding="utf-8")
    r = subprocess.run(
        ["sh", "-c", f"sed -n '/^#__GW_SETUP_BELOW__$/,$p' {f} | tail -n +2"],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == GW.read_text(encoding="utf-8")


def test_bundle_installs_to_a_stable_path(bundle):
    """Скрипт настройки прописывает СЕБЯ в systemd-юнит по своему пути.

    Разложи его во временный каталог — юнит будет указывать на файл, которого
    после уборки нет. Автозапуск умрёт молча и обнаружится только после ребута,
    выглядя как «шлюз сам отвалился».
    """
    assert 'DEST="/opt/awg-gw"' in bundle
    assert "mktemp" not in bundle, "временный каталог ломает автозапуск"
    assert re.search(r'exec "\$DEST/routing-gw-setup\.sh"', bundle)


def test_bundle_defaults_to_apply_and_passes_rollback_through(bundle):
    """Без аргумента — применить; --rollback обязан доехать до скрипта."""
    assert '"${1:---apply}"' in bundle
    r = subprocess.run(
        ["sh", "-c", 'f() { echo "$1"; }; f "${1:---apply}"', "x", "--rollback"],
        capture_output=True, text=True)
    assert r.stdout.strip() == "--rollback"


def test_bundle_keeps_the_root_check(bundle):
    """Без root он сделает половину и оставит шлюз в промежуточном состоянии."""
    assert 'id -u' in bundle and "нужен root" in bundle


def test_bundle_stamps_the_link_contract(bundle):
    """Штамп версии контракта — единственное, по чему человек на шлюзе поймёт,
    чем этот линк ставили. Автоматики тут нет намеренно: несовпадение обфускации
    ломает хендшейк, и об этом бот сообщает сам."""
    assert re.search(r"Контракт линка: \d+", bundle)
    assert re.search(r"# awg-bot: контракт линка \d+", bundle)


def test_bundle_is_marked_secret(bundle):
    """Внутри приватный ключ и psk — файл обязан себя объявить и сказать,
    что с ним делать после установки."""
    assert "ПРИВАТНЫЙ КЛЮЧ" in bundle
    assert "chmod 0600" in bundle


def test_link_setup_unit_points_at_a_permanent_path():
    """Третья копия той же дыры — юнит линка на стороне ВПС."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "install"
           / "routing-link-setup.sh").read_text(encoding="utf-8")
    assert 'SELF="$(install_self)"' in src
    assert 'SELF="$(readlink -f "$0")"' not in src
    assert "/usr/local/sbin" in src
