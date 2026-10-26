"""Скрипт линка: что он кладёт в конфиг шлюзовой стороны — keepalive линка в
ритме пользователей, AllowedIPs с префиксами соседей, обфускация без коллизии
S1 + 56; и сторож прежних файлов конфигурации в скрипте обвязки шлюза.

Файла первого применения больше нет (шлюз подключается командой из бота),
поэтому блоки скрипта прогоняются как есть — настоящим sh, а не по исходнику.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.conftest import SH

ROOT = Path(__file__).resolve().parents[2]
LINK = (ROOT / "install" / "routing-link-setup.sh").read_text(encoding="utf-8")
GW = (ROOT / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")


def _keepalive(env: dict) -> str:
    """Блок разбора keepalive из скрипта линка — его итог."""
    start = LINK.index('_APP_YAML="${_APP_YAML:-')
    end = LINK.index("\nesac\n", LINK.index('LINK_KEEPALIVE="${LINK_KEEPALIVE')) + 6
    prog = LINK[start:end] + 'printf "%s" "$LINK_KEEPALIVE"\n'
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **env})
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_link_keepalive_comes_from_the_same_key_as_client_peers(tmp_path):
    """Ритм линка и ритм пользователей задаёт один ключ конфига бота: поменял
    client_config.keepalive_seconds — поменялось и у линка, иначе линк остаётся
    единственным узнаваемым метрономом на сервере."""
    (tmp_path / "app.yaml").write_text(
        'client_config:\n  mtu: 1376\n  keepalive_seconds: "20-40"\n', encoding="utf-8")
    assert _keepalive({"_APP_YAML": str(tmp_path / "app.yaml")}) == "20-40"
    assert "PersistentKeepalive = $LINK_KEEPALIVE" in LINK, "конфиг шлюза берёт не этот ритм"


@pytest.mark.parametrize("raw, expect", [
    ("25", "25"),              # одиночное число законно: пользователи дореформенного поколения
    ("20-40", "20-40"),
    ("", "25-35"),             # ключа нет или он пуст
    ("много", "25-35"),        # мусор
    ("-5", "25-35"),           # диапазон без начала
    ("5-", "25-35"),           # диапазон без конца
    ("1-2-3", "25-35"),        # не диапазон вовсе
])
def test_broken_keepalive_in_the_config_falls_back_to_the_default_range(tmp_path, raw, expect):
    """Мусор в ключе не должен доехать до конфига шлюза: awg-quick на шлюзе
    отвергнет неразбираемое значение целиком, линк не поднимется, и увидят это
    уже на той стороне. Непонятное значение — диапазон по умолчанию."""
    (tmp_path / "app.yaml").write_text(
        f'client_config:\n  keepalive_seconds: "{raw}"\n', encoding="utf-8")
    assert _keepalive({"_APP_YAML": str(tmp_path / "app.yaml")}) == expect, f"из {raw!r} получилось не то"


def test_keepalive_can_be_overridden_by_the_environment(tmp_path):
    """Окружение сильнее конфига — как у порта и подсети: бот передаёт значение
    сам (_slot_env), а YAML без кавычек awk здесь не видит."""
    (tmp_path / "app.yaml").write_text(
        'client_config:\n  keepalive_seconds: "20-40"\n', encoding="utf-8")
    assert _keepalive({"_APP_YAML": str(tmp_path / "app.yaml"), "LINK_KEEPALIVE": "31-47"}) == "31-47"


def test_neighbour_prefixes_land_in_the_allowed_ips(services, make_active_client):
    """Подсети соседей у слота с VPN-транзитом — префикс слота соседа в
    туннеле (10.241.0.0/16), а не его настоящие подсети: так их получает
    скрипт линка (PEER_LAN_NETS) и так они встают в AllowedIPs пира ВПС."""
    from awgbot.core import config
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.db.gateway_update(1, lan_subnets=["192.168.1.0/24"], lan_mode=1)
    services.db.gateway_update(2, lan_subnets=["192.168.68.0/24"], lan_mode=1)
    env = services._lan_env(services.db.gateway(1))
    assert env["PEER_LAN_NETS"] == "10.241.0.0/16"
    start = LINK.index('PEER_LAN_NETS="$(printf')
    end = LINK.index("\n}\n", LINK.index("gw_allowed_ips() {")) + 3
    r = subprocess.run([SH, "-c", LINK[start:end] + "gw_allowed_ips\n"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "CLIENT_SUBNET": "10.8.1.0/24",
                            "LINK_CIDR": "10.99.99.0/30", "PEER_LAN_NETS": env["PEER_LAN_NETS"]})
    assert r.stdout == "10.8.1.0/24, 10.99.99.0/30, 10.241.0.0/16", r.stdout + r.stderr


def test_link_obfuscation_avoids_the_s1_plus_56_collision():
    """S1 + 56 == S2 ломает разбор рукопожатия (длина init совпадает с
    response): ~0,4 % линков молча не вставали. Генератор переизбирает S2."""
    assert 'while [ $((S1 + 56)) -eq "$S2" ]; do S2=$(rnd 15 150); done' in LINK


def test_the_link_script_builds_no_file_for_the_gateway():
    """Файла конфигурации для шлюза скрипт больше не собирает: ни режима
    --bundle, ни тела файла, ни инструкции с копированием; прежний файл —
    только снимается вместе с линком."""
    assert "--bundle)" not in LINK and "emit_gw_bundle" not in LINK and "print_gw_instructions" not in LINK
    assert "BODYEOF" not in LINK
    rollback = LINK.split('if [ "$MODE" = "rollback" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "$GW_BUNDLE_OUT" in rollback, "прежний файл с ключами не снимается с линком"


def test_an_older_file_is_refused_by_the_plumbing_script_itself(tmp_path):
    """Файл прежнего выпуска, запущенный руками на шлюзе, старее применённого
    — отказ скрипта обвязки до первого шага; AWG_GW_ALLOW_OLD=1 — применяется;
    успешное применение двигает метку только вперёд. Функции — из
    НАСТОЯЩЕГО routing-gw-setup.sh."""
    start = GW.index('ISSUED_FILE="$GW_ETC/issued_at"')
    block = GW[start:GW.index("issued_check || exit 3", start)]
    etc = tmp_path / "etc"; etc.mkdir()

    def run(issued, have, *, allow="", record=False):
        if have is None:
            (etc / "issued_at").unlink(missing_ok=True)
        else:
            (etc / "issued_at").write_text(f"{have}\n", encoding="utf-8")
        prog = (f'GW_ETC="{etc}"\nsay() {{ echo "$*"; }}\n' + block
                + ("issued_record; echo RECORDED\n" if record else "issued_check && echo PASS || echo REFUSED\n"))
        env = {"PATH": "/usr/bin:/bin", "BUNDLE_ISSUED_AT": str(issued)}
        if allow:
            env["AWG_GW_ALLOW_OLD"] = allow
        r = subprocess.run(["sh", "-c", prog], capture_output=True, text=True, timeout=10, env=env)
        return r.stdout

    assert "PASS" in run(1_800_000_000, None), "чистая машина — сверять не с чем"
    out = run(1_700_000_000, 1_800_000_000)
    assert "REFUSED" in out and "ОТКАЗ: файл конфигурации выпущен " in out and ", текущая конфигурация от " in out, out
    assert "PASS" in run(1_700_000_000, 1_800_000_000, allow="1")
    assert "PASS" in run(1_800_000_001, 1_800_000_000)
    run(1_800_000_005, 1_800_000_000, record=True)
    assert (etc / "issued_at").read_text(encoding="utf-8").strip() == "1800000005"
    run(1_700_000_000, 1_800_000_005, record=True)
    assert (etc / "issued_at").read_text(encoding="utf-8").strip() == "1800000005", "метка ушла назад"
