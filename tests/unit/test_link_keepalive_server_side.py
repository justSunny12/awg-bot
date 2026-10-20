"""Keepalive стороны сервера AWG на линках слотов: шаблон конфига в
install/routing-link-setup.sh и шаг обновления `adopt_link_keepalive`
(awg-bot.sh, вызывается из update_env_steps).

С резервом шлюзу нужен взгляд на сервер за секунды, а до 4.0.0 keepalive
слал только шлюз: сервер видел шлюз, шлюз сервер — лишь по рукопожатию раз
в ~2 мин. Поэтому сторона сервера в `[Peer]` несёт `PersistentKeepalive`
тем же диапазоном, что у клиентов (`client_config.keepalive_seconds`), а
шаг обновления дописывает строку в живые конфиги без неё и ставит её на
живой интерфейс (`awg set`), без перезапуска линка.

Скрипт линка гоняется целиком под sh с подставными awg/ip/systemctl (как в
соседних тестах скрипта линка); шаг обновления — bash'ем из настоящего
awg-bot.sh, `awg` подменён функцией с журналом.

Цена ошибки: строки нет — шлюз узнаёт о падении сервера минутами позже, и
голосование резерва опаздывает; строка не в `[Peer]` — awg-quick отвергает
конфиг и линк не поднимается после рестарта; шаг, который дописывает строку
при каждом обновлении, — дубли и тот же отказ awg-quick; кривой диапазон из
настроек в конфиге — то же.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat
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


# ── шаг обновления на живых конфигах ─────────────────────────────────────────

SCRIPT_FUNCS = ("yaml_get", "adopt_link_keepalive")

LINK_OLD = """# awg-bot: линк слота 1
[Interface]
Address = 10.99.99.1/30
ListenPort = 443
PrivateKey = DUMMY-vps-priv=
Table = off
Jc = 4

[Peer]
PublicKey = DUMMY-gw-pub-1=
PresharedKey = DUMMY-psk=
AllowedIPs = 0.0.0.0/0
"""


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


class _Server:
    """Каталог конфигов AWG сервера, conf бота и журнал вызовов awg."""

    def __init__(self, tmp_path: Path, keepalive: str | None = '"25-35"', head: str | None = None):
        self.tmp = tmp_path
        self.awg_dir = tmp_path / "amneziawg"
        self.awg_dir.mkdir()
        self.conf = tmp_path / "conf"
        self.conf.mkdir()
        self.log = tmp_path / "awg.log"
        ka = f"  keepalive_seconds: {keepalive}\n" if keepalive is not None else ""
        if head is None:
            head = f'awg:\n  conf_dir: "{self.awg_dir}"\n  interface: "awg0"\n'
        (self.conf / "app.yaml").write_text(
            head.format(dir=self.awg_dir) + f"client_config:\n  mtu: 1376\n{ka}", encoding="utf-8")
        self.awg_rc = 0

    def link(self, name: str, text: str) -> Path:
        p = self.awg_dir / f"{name}.conf"
        p.write_text(text, encoding="utf-8")
        return p

    def run(self) -> subprocess.CompletedProcess:
        script = flat()
        prog = "\n".join([
            "set -euo pipefail",
            'log() { echo "LOG $*"; }', 'warn() { echo "WARN $*" >&2; }',
            f'CONF_DIR="{self.conf}"',
            f'awg() {{ echo "awg $*" >> "{self.log}"; return {self.awg_rc}; }}',
            *(_func(script, n) for n in SCRIPT_FUNCS),
            "adopt_link_keepalive",
        ])
        return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                              env={"PATH": "/usr/bin:/bin"})

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


def test_the_update_adds_the_line_to_the_peer_and_sets_it_on_the_live_link(tmp_path):
    s = _Server(tmp_path, keepalive='"20-40"')
    f = s.link("awglink", LINK_OLD)
    s.link("awglink2", LINK_OLD.replace("DUMMY-gw-pub-1=", "DUMMY-gw-pub-2="))
    r = s.run()
    assert r.returncode == 0, r.stderr
    peer = _peer_section(f.read_text(encoding="utf-8"))
    assert peer[-1] == "PersistentKeepalive = 20-40", f"строка не в [Peer]: {peer}"
    assert sorted(s.calls()) == ["awg set awglink peer DUMMY-gw-pub-1= persistent-keepalive 20-40",
                                 "awg set awglink2 peer DUMMY-gw-pub-2= persistent-keepalive 20-40"]


def test_a_second_run_changes_nothing(tmp_path):
    s = _Server(tmp_path)
    f = s.link("awglink", LINK_OLD)
    assert s.run().returncode == 0
    once = f.read_text(encoding="utf-8")
    s.log.unlink()
    r = s.run()
    assert r.returncode == 0, r.stderr
    assert f.read_text(encoding="utf-8") == once, "повторное обновление дописало строку ещё раз"
    assert s.calls() == [], "повторное обновление трогает живой линк"


def test_a_link_that_already_keeps_alive_is_left_alone(tmp_path):
    s = _Server(tmp_path)
    text = LINK_OLD + "PersistentKeepalive = 30\n"
    f = s.link("awglink", text)
    assert s.run().returncode == 0
    assert f.read_text(encoding="utf-8") == text and s.calls() == [], "ручное значение переписано"


@pytest.mark.parametrize("raw", ['"много"', '"-5"', '"5-"', '"1-2-3"', '""', None, "25-35"])
def test_a_crooked_or_missing_range_in_settings_gives_the_default(tmp_path, raw):
    """Ключа нет, он пуст, мусор или без кавычек (awk читает только строку в
    двойных кавычках) — 25-35, как у скрипта линка."""
    s = _Server(tmp_path, keepalive=raw)
    f = s.link("awglink", LINK_OLD)
    assert s.run().returncode == 0
    assert _peer_section(f.read_text(encoding="utf-8"))[-1] == "PersistentKeepalive = 25-35"
    assert s.calls() == ["awg set awglink peer DUMMY-gw-pub-1= persistent-keepalive 25-35"]


def test_a_live_link_that_refuses_the_setting_does_not_stop_the_update(tmp_path):
    """Интерфейс лежит или тулзы старые: строка в конфиге остаётся (поднимется
    с ней), обновление идёт дальше с предупреждением."""
    s = _Server(tmp_path)
    s.awg_rc = 1
    f = s.link("awglink", LINK_OLD)
    r = s.run()
    assert r.returncode == 0, r.stderr
    assert "PersistentKeepalive = 25-35" in f.read_text(encoding="utf-8")
    assert "keepalive не применился" in r.stderr


def test_without_link_confs_nothing_happens(tmp_path):
    s = _Server(tmp_path)
    s.link("awg0", "[Interface]\nListenPort = 51820\n\n[Peer]\nPublicKey = DUMMY-client=\nAllowedIPs = 10.8.1.2/32\n")
    r = s.run()
    assert r.returncode == 0, r.stderr
    assert s.calls() == [] and "PersistentKeepalive" not in (s.awg_dir / "awg0.conf").read_text(encoding="utf-8"), \
        "клиентский интерфейс — не линк слота"


def test_a_conf_without_the_final_newline_gets_a_separate_line(tmp_path):
    """Конфиг, правленный руками, может кончаться без перевода строки:
    дописанная строка не должна склеиться с `AllowedIPs`."""
    s = _Server(tmp_path)
    f = s.link("awglink", LINK_OLD.rstrip("\n"))
    assert s.run().returncode == 0
    peer = _peer_section(f.read_text(encoding="utf-8"))
    assert "AllowedIPs = 0.0.0.0/0" in peer and "PersistentKeepalive = 25-35" in peer, peer


@pytest.mark.parametrize("head", [
    # раскладка до 4.0.0: раздел docker, ключ awg_dir
    'docker:\n  runtime: "host"\n  awg_dir: "{dir}"   # каталог awg-quick\n',
    # раздел gateway выше раздела awg: его conf_dir — не каталог сервера
    'gateway:\n  conf_dir: "/nonexistent/gw"\nawg:\n  interface: "awg0"\n  conf_dir: "{dir}"\n',
])
def test_the_awg_quick_dir_comes_from_the_awg_section_of_either_layout(tmp_path, head):
    s = _Server(tmp_path, head=head)
    f = s.link("awglink", LINK_OLD)
    r = s.run()
    assert r.returncode == 0, r.stderr
    assert "PersistentKeepalive = 25-35" in f.read_text(encoding="utf-8"), "каталог линков найден не тот"
    assert s.calls() == ["awg set awglink peer DUMMY-gw-pub-1= persistent-keepalive 25-35"]


def test_on_a_gateway_the_step_does_nothing(tmp_path):
    """У шлюза keepalive линка стоит с его стороны и так; его awglink.conf —
    шлюзовая сторона, трогать её шагом сервера нельзя."""
    # шаблон app.yaml у шлюза тот же: раздел awg есть и указывает туда же
    s = _Server(tmp_path, head='role: "gateway"\nawg:\n  conf_dir: "{dir}"\ngateway:\n  conf_dir: "{dir}"\n')
    f = s.link("awglink", LINK_OLD)
    r = s.run()
    assert r.returncode == 0, r.stderr
    assert f.read_text(encoding="utf-8") == LINK_OLD and s.calls() == []


def test_the_update_step_runs_before_seeding_conf():
    steps = _func(flat(), "update_env_steps")
    assert re.search(r"^\s*adopt_link_keepalive\b", steps, re.M), "шаг обновления не зовёт adopt_link_keepalive"
    assert steps.index("adopt_link_keepalive") < steps.index("seed_conf")
