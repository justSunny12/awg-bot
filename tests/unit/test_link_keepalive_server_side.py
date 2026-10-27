"""Keepalive стороны сервера AWG на линках слотов: шаблон конфига в
install/routing-link-setup.sh.

С резервом шлюзу нужен взгляд на сервер за секунды, поэтому сторона сервера
в `[Peer]` несёт `PersistentKeepalive` тем же диапазоном, что у пользователей
(`client_config.keepalive_seconds`). Скрипт линка гоняется целиком под sh с
подставными awg/ip/systemctl (как в соседних тестах скрипта линка).

Цена ошибки: строки нет — шлюз узнаёт о падении сервера минутами позже, и
голосование резерва опаздывает; строка не в `[Peer]` — awg-quick отвергает
конфиг и линк не поднимается после рестарта; кривой диапазон из настроек в
конфиге — то же.
"""
from __future__ import annotations

import pytest

from tests.unit.test_link_script_join import _Vps

pytestmark = pytest.mark.unit


def _peer_section(text: str) -> list[str]:
    lines = text.splitlines()
    i = lines.index("[Peer]")
    return [ln for ln in lines[i + 1:] if ln.strip() and not ln.startswith("#")]


# ── шаблон: новый линк ───────────────────────────────────────────────────────

@pytest.mark.parametrize("mode, env", [("--apply", {}), ("--join-stage", {"JOIN_ID": "0123456789abcdef"})])
def test_a_new_link_conf_of_the_server_side_keeps_alive_with_the_client_range(tmp_path, mode, env):
    vps = _Vps(tmp_path, live=False)
    app = tmp_path / "app.yaml"
    app.write_text('client_config:\n  keepalive_seconds: "20-40"\n', encoding="utf-8")
    r = vps.run(mode, _APP_YAML=str(app), LINK_KEEPALIVE="", **env)
    assert r.returncode == 0, r.stdout + r.stderr
    peer = _peer_section(vps.conf.read_text(encoding="utf-8"))
    assert "PersistentKeepalive = 20-40" in peer, f"в [Peer] стороны сервера нет keepalive: {peer}"
    assert sum(ln.startswith("PersistentKeepalive") for ln in vps.conf.read_text(encoding="utf-8").splitlines()) == 1


def test_a_broken_range_in_settings_gives_the_default_on_the_server_side(tmp_path):
    vps = _Vps(tmp_path, live=False)
    app = tmp_path / "app.yaml"
    app.write_text('client_config:\n  keepalive_seconds: "много"\n', encoding="utf-8")
    r = vps.run("--apply", _APP_YAML=str(app), LINK_KEEPALIVE="")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PersistentKeepalive = 25-35" in _peer_section(vps.conf.read_text(encoding="utf-8"))
