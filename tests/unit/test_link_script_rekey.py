"""
Режимы смены ключей линка в install/routing-link-setup.sh на ВПС:
--rekey-stage, --rekey-commit, --rekey-revert, --rekey-drop.

Бот ведёт переход каналом: stage кладёт новые ключи обеих сторон в `.next`,
commit переключает ВПС, revert возвращает прежние, drop убирает хвосты после
фиксации. Цена ошибки — линк до шлюза, то есть РФ-доступ у всех клиентов и
сам канал, по которому чинить: stage, тронувший живой конфиг, рвёт линк до
подтверждения человека; commit без `.prev` не даёт вернуться; revert, не
поднявший прежние ключи, оставляет ВПС на ключах, которых у шлюза нет; новый
конфиг с другим портом, адресом или Endpoint шлюз отвергнет.

Скрипт гоняется целиком под sh: `id`, `awg`, `awg-quick`, `ip`, `iptables` —
подставные команды, которые пишут вызовы в журнал; файлы — во временном
каталоге (CONF_DIR, GW_CONF_OUT). Ключи — заглушки DUMMY-…, по счётчику.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-link-setup.sh"

VPS_LIVE = """# Линк ВПС ↔ шлюз
[Interface]
Address = 10.99.99.1/30
ListenPort = 51821
PrivateKey = DUMMY-VPS-OLD-PRIVATE-KEY=
Table = off
Jc = 4
S1 = 10
H1 = 1

[Peer]
PublicKey = DUMMY-GW-OLD-PUBLIC-KEY=
PresharedKey = DUMMY-OLD-PSK=
AllowedIPs = 0.0.0.0/0
"""

GW_LIVE = """# Линк до ВПС
[Interface]
Address = 10.99.99.2/30
PrivateKey = DUMMY-GW-OLD-PRIVATE-KEY=
Jc = 4
S1 = 10
H1 = 1

[Peer]
PublicKey = DUMMY-VPS-OLD-PUBLIC-KEY=
PresharedKey = DUMMY-OLD-PSK=
Endpoint = vps.example.org:51821
AllowedIPs = 10.8.1.0/24, 10.99.99.0/30
PersistentKeepalive = 25-35
"""


def _kv(text: str) -> dict:
    """«Ключ = значение» конфига — словарём (секции не различаем: ключи в них разные)."""
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


class _Vps:
    """ВПС для скрипта линка: каталог конфигов, файл шлюзовой стороны и
    подставные команды. `up_fail` — сколько первых `awg-quick up` упадут."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"; self.bin.mkdir()
        self.conf_dir = tmp_path / "amneziawg"; self.conf_dir.mkdir()
        self.root = tmp_path / "root"; self.root.mkdir()
        self.log = tmp_path / "calls.log"; self.log.write_text("", encoding="utf-8")
        self.conf = self.conf_dir / "awglink.conf"
        self.gw = self.root / "gw-awglink.conf"
        self.conf.write_text(VPS_LIVE, encoding="utf-8"); self.conf.chmod(0o600)
        self.gw.write_text(GW_LIVE, encoding="utf-8"); self.gw.chmod(0o600)
        n = tmp_path / "keyno"; n.write_text("0", encoding="utf-8")
        fails = tmp_path / "up.fail"; fails.write_text("0", encoding="utf-8")
        fake_cmd(self.bin / "id", 'echo 0\n')
        # genkey/genpsk — новый ключ по счётчику; pubkey — «публичный» от прочитанного приватного
        fake_cmd(self.bin / "awg",
                 f'echo "awg $*" >> {self.log}\n'
                 f'case "$1" in\n'
                 f'  genkey|genpsk) i=$(( $(cat {n}) + 1 )); echo $i > {n}; echo "DUMMY-NEW-$1-$i=" ;;\n'
                 f'  pubkey) read k; echo "DUMMY-PUB-OF-$k" ;;\n'
                 f'esac\nexit 0\n')
        fake_cmd(self.bin / "awg-quick",
                 f'echo "awg-quick $*" >> {self.log}\n'
                 f'if [ "$1" = up ]; then f=$(cat {fails}); if [ "$f" -gt 0 ]; then echo $((f - 1)) > {fails}; exit 1; fi; fi\n'
                 'exit 0\n')
        fake_cmd(self.bin / "ip", f'echo "ip $*" >> {self.log}\nexit 0\n')
        nat = tmp_path / "nat.fail"; nat.write_text("0", encoding="utf-8")
        # nat.fail = 1 — исключения нет и вставить его не выходит (-C и -I отказывают)
        fake_cmd(self.bin / "iptables", f'echo "iptables $*" >> {self.log}\n'
                                        f'case " $* " in *" -C "*|*" -I "*) exit $(cat {nat}) ;; esac\nexit 0\n')
        self._fails = fails
        self._nat = nat

    def nat_fail(self, on: bool = True) -> None:
        self._nat.write_text("1" if on else "0", encoding="utf-8")

    def up_fail(self, n: int) -> None:
        self._fails.write_text(str(n), encoding="utf-8")

    def run(self, mode: str, **env) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "LINK_IF": "awglink", "LINK_CIDR": "10.99.99.0/30",
                "LINK_PORT": "51821", "CONF_DIR": str(self.conf_dir), "GW_CONF_OUT": str(self.gw),
                "_APP_YAML": str(self.tmp / "нет.yaml"), "CLIENT_SUBNET": "10.8.1.0/24",
                "LINK_KEEPALIVE": "25-35"}
        base.update(env)
        self.log.write_text("", encoding="utf-8")
        return subprocess.run([SH, str(SCRIPT), mode], capture_output=True, text=True, env=base)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def p(self, path: Path, suffix: str) -> Path:
        return path.with_name(path.name + suffix)

    def files(self) -> set[str]:
        """Что лежит рядом с живыми конфигами обеих сторон."""
        return {f.name for d in (self.conf_dir, self.root) for f in d.iterdir()}


@pytest.fixture()
def vps(tmp_path):
    return _Vps(tmp_path)


def _last(r: subprocess.CompletedProcess) -> str:
    return r.stdout.rstrip().splitlines()[-1] if r.stdout.strip() else ""


# ── --rekey-stage ────────────────────────────────────────────────────────────

def test_stage_writes_new_keys_for_both_sides_and_leaves_the_live_link_alone(vps):
    """Стадия — до согласия человека: живые конфиги обеих сторон не тронуты,
    линк не перезапускался; новые конфиги лежат рядом в `.next`."""
    r = vps.run("--rekey-stage")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == "rekey: staged", r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE, "живой конфиг ВПС тронут стадией"
    assert vps.gw.read_text(encoding="utf-8") == GW_LIVE, "живой конфиг шлюзовой стороны тронут стадией"
    assert vps.files() == {"awglink.conf", "awglink.conf.next", "gw-awglink.conf", "gw-awglink.conf.next"}
    assert not [c for c in vps.calls() if c.startswith(("awg-quick", "iptables", "ip "))], (
        f"стадия трогала линк: {vps.calls()}")


def test_staged_files_are_private(vps):
    """В `.next` — приватные ключи и PSK линка: читает их только root."""
    vps.run("--rekey-stage")
    for path in (vps.p(vps.conf, ".next"), vps.p(vps.gw, ".next")):
        assert path.stat().st_mode & 0o777 == 0o600, f"{path.name}: права {oct(path.stat().st_mode & 0o777)}"


def test_staged_keys_are_new_and_pair_up_across_the_sides(vps):
    """Новые ключи обеих сторон и PSK — не прежние; публичный ключ каждой
    стороны в `[Peer]` другой — от её нового приватного; PSK у сторон общий.
    Разойдись пара — линк на новых ключах не встанет ни при каком подтверждении."""
    vps.run("--rekey-stage")
    v = _kv(vps.p(vps.conf, ".next").read_text(encoding="utf-8"))
    g = _kv(vps.p(vps.gw, ".next").read_text(encoding="utf-8"))
    assert v["PrivateKey"] != "DUMMY-VPS-OLD-PRIVATE-KEY=" and g["PrivateKey"] != "DUMMY-GW-OLD-PRIVATE-KEY="
    assert v["PrivateKey"] != g["PrivateKey"], "одна пара ключей на обе стороны"
    assert g["PublicKey"] == f"DUMMY-PUB-OF-{v['PrivateKey']}", "шлюз ждёт не тот ключ ВПС"
    assert v["PublicKey"] == f"DUMMY-PUB-OF-{g['PrivateKey']}", "ВПС ждёт не тот ключ шлюза"
    assert v["PresharedKey"] == g["PresharedKey"] != "DUMMY-OLD-PSK=", "PSK разный у сторон или прежний"


def test_staged_confs_keep_port_addresses_and_endpoint(vps):
    """Порт, адреса /30 и Endpoint — прежние: шлюз проверяет это и иначе
    откажет. Порт — из живого ListenPort, а не из окружения: бот мог передать
    порт строки слота, а живёт линк на том, что в конфиге."""
    r = vps.run("--rekey-stage", LINK_PORT="443")
    assert r.returncode == 0, r.stdout
    v = _kv(vps.p(vps.conf, ".next").read_text(encoding="utf-8"))
    g = _kv(vps.p(vps.gw, ".next").read_text(encoding="utf-8"))
    assert v["ListenPort"] == "51821", "порт линка ВПС взят не из живого конфига"
    assert v["Address"] == "10.99.99.1/30" and g["Address"] == "10.99.99.2/30"
    assert v["Table"] == "off", "без Table = off весь трафик ВПС ушёл бы в шлюз"
    assert g["Endpoint"] == "vps.example.org:51821", "Endpoint шлюзовой стороны сменился"
    assert g["AllowedIPs"] == "10.8.1.0/24, 10.99.99.0/30" and g["PersistentKeepalive"] == "25-35"


def test_staged_endpoint_follows_the_server_host_like_the_bundle_does(vps):
    """Бот передаёт стадии ENDPOINT_HOST (network.server_host). Шлюзу с
    документом конфигурации Endpoint линка правит сервер по этому же ключу,
    а `/root/gw-<линк>.conf` после смены домена остаётся с прежним хостом
    (его правит только --bundle). Возьми стадия Endpoint из файла — новые ключи
    приедут со старым хостом, и шлюз откажет «Endpoint другой»: смена ключей
    после «✏️ Домен» не проходит никогда."""
    r = vps.run("--rekey-stage", ENDPOINT_HOST="vps2.example.org")
    assert r.returncode == 0, r.stdout
    g = _kv(vps.p(vps.gw, ".next").read_text(encoding="utf-8"))
    assert g["Endpoint"] == "vps2.example.org:51821", (
        f"Endpoint новых ключей — {g['Endpoint']}, а шлюз стоит на vps2.example.org:51821")


def test_stage_without_the_link_refuses(vps):
    """Линк не настроен — менять нечего: код 1, внятная причина, файлов нет."""
    vps.gw.unlink()
    r = vps.run("--rekey-stage")
    assert r.returncode == 1 and "не настроен" in r.stdout, r.stdout
    assert "rekey:" not in r.stdout
    assert not vps.p(vps.conf, ".next").exists() and not vps.p(vps.gw, ".next").exists()


# ── --rekey-commit ───────────────────────────────────────────────────────────

def test_commit_swaps_live_and_next_and_restarts_the_link(vps):
    """Переключение ВПС: живые → `.prev` (для возврата), `.next` → живые,
    `.next` больше нет; линк переподнят, исключение из MASQUERADE на месте."""
    vps.run("--rekey-stage")
    new_v = vps.p(vps.conf, ".next").read_text(encoding="utf-8")
    new_g = vps.p(vps.gw, ".next").read_text(encoding="utf-8")
    r = vps.run("--rekey-commit")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _last(r) == "rekey: committed", r.stdout
    assert vps.conf.read_text(encoding="utf-8") == new_v and vps.gw.read_text(encoding="utf-8") == new_g
    assert vps.p(vps.conf, ".prev").read_text(encoding="utf-8") == VPS_LIVE
    assert vps.p(vps.gw, ".prev").read_text(encoding="utf-8") == GW_LIVE
    assert not vps.p(vps.conf, ".next").exists() and not vps.p(vps.gw, ".next").exists()
    for path in (vps.conf, vps.gw, vps.p(vps.conf, ".prev"), vps.p(vps.gw, ".prev")):
        assert path.stat().st_mode & 0o777 == 0o600, f"{path.name} читается не только root"
    calls = vps.calls()
    assert calls.index("awg-quick down awglink") < calls.index("awg-quick up awglink"), calls
    assert any(c.startswith("iptables -t nat -C POSTROUTING -s 10.8.1.0/24 -o awglink") for c in calls), calls


def test_commit_without_the_nat_exemption_touches_no_keys(vps):
    """Исключение линка из MASQUERADE не встало — переключаться нельзя: без
    него шлюз видит адрес ВПС вместо клиента, и РФ-трафик молча не ходит.
    Отказ ДО подмены: живые ключи прежние, `.next` на месте (бот вернёт их
    --rekey-revert), `.prev` нет, линк не дёргали; код 1 и строка ОШИБКА —
    причина для уведомления."""
    vps.run("--rekey-stage")
    vps.nat_fail()
    r = vps.run("--rekey-commit")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ОШИБКА: исключение линка из MASQUERADE не поставлено — ключи не тронуты" in r.stdout, r.stdout
    assert "rekey: committed" not in r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and vps.gw.read_text(encoding="utf-8") == GW_LIVE
    assert vps.p(vps.conf, ".next").exists() and vps.p(vps.gw, ".next").exists(), ".next потерян при отказе"
    assert not vps.p(vps.conf, ".prev").exists(), "прежние ключи легли в .prev, хотя подмены не было"
    assert not [c for c in vps.calls() if c.startswith("awg-quick")], vps.calls()


def test_commit_that_does_not_come_up_puts_the_old_keys_back(vps):
    """Новые ключи не подняли интерфейс: прежние на место и подняты, `.prev`
    не остаётся, код 1 и без строки итога — бот снимает переход."""
    vps.run("--rekey-stage")
    vps.up_fail(1)
    r = vps.run("--rekey-commit")
    assert r.returncode == 1, r.stdout
    assert "прежние ключи на месте" in r.stdout and "rekey:" not in r.stdout, r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE, "ВПС остался на не поднявшихся ключах"
    assert vps.gw.read_text(encoding="utf-8") == GW_LIVE
    assert vps.calls().count("awg-quick up awglink") == 2, f"прежние ключи не подняли: {vps.calls()}"
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}, vps.files()


def test_commit_without_staged_keys_refuses_and_touches_nothing(vps):
    """Коммит без стадии — внятный отказ, живые не тронуты, линк не дёрнут."""
    r = vps.run("--rekey-commit")
    assert r.returncode == 1 and "сначала --rekey-stage" in r.stdout, r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and not vps.calls()
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}


def test_a_second_commit_does_not_overwrite_the_old_keys(vps):
    """Повтор коммита (бот перезапустился, такт сработал дважды): `.next`
    уже нет — отказ, и `.prev` с прежними ключами цел. Перепиши его повтор
    новыми — возвращаться стало бы не на что."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    r = vps.run("--rekey-commit")
    assert r.returncode == 1, r.stdout
    assert vps.p(vps.conf, ".prev").read_text(encoding="utf-8") == VPS_LIVE, "прежние ключи ВПС потеряны"
    assert vps.p(vps.gw, ".prev").read_text(encoding="utf-8") == GW_LIVE
    assert not [c for c in vps.calls() if c.startswith("awg-quick")]


# ── --rekey-revert ───────────────────────────────────────────────────────────

def test_revert_after_commit_brings_the_old_keys_back_up(vps):
    """Срок вышел без hello новым ключом: прежние на место, линк переподнят на
    них, `.prev` убран, исключение из MASQUERADE на месте."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    r = vps.run("--rekey-revert")
    assert r.returncode == 0 and _last(r) == "rekey: reverted", r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and vps.gw.read_text(encoding="utf-8") == GW_LIVE
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}, vps.files()
    calls = vps.calls()
    assert "awg-quick up awglink" in calls, calls
    assert any(c.startswith("iptables -t nat -C POSTROUTING") for c in calls)


def test_revert_without_the_nat_exemption_still_brings_the_old_keys_back(vps):
    """Возврат — последний рубеж: исключение из MASQUERADE не встало — это
    предупреждение (поставит --reassert), а не отказ возврата: прежние ключи
    на месте и подняты, итог «reverted»."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    vps.nat_fail()
    r = vps.run("--rekey-revert")
    assert r.returncode == 0 and _last(r) == "rekey: reverted", r.stdout + r.stderr
    assert "ВНИМАНИЕ: исключение линка из MASQUERADE не поставлено" in r.stdout, r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and vps.gw.read_text(encoding="utf-8") == GW_LIVE
    assert "awg-quick up awglink" in vps.calls()
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}


def test_staged_endpoint_takes_the_live_port_with_the_server_host(vps):
    """Хост — из ENDPOINT_HOST, порт — из живого ListenPort, а не из
    окружения слота."""
    r = vps.run("--rekey-stage", ENDPOINT_HOST="vps2.example.org", LINK_PORT="443")
    assert r.returncode == 0, r.stdout
    assert _kv(vps.p(vps.gw, ".next").read_text(encoding="utf-8"))["Endpoint"] == "vps2.example.org:51821"


def test_revert_before_commit_only_drops_the_staged_keys(vps):
    """Отказ человека или сутки без решения — коммита не было: снимаются
    только `.next`, живой линк не перезапускается."""
    vps.run("--rekey-stage")
    r = vps.run("--rekey-revert")
    assert r.returncode == 0 and _last(r) == "rekey: reverted", r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}
    assert not [c for c in vps.calls() if c.startswith("awg-quick")], "линк дёрнут без нужды"


def test_revert_twice_is_harmless(vps):
    """Возврат на старте бота может прийти после возврата по сроку: второй
    раз — ничего не трогает и не роняет линк."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    vps.run("--rekey-revert")
    r = vps.run("--rekey-revert")
    assert r.returncode == 0 and _last(r) == "rekey: reverted"
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE and not vps.calls()


def test_revert_that_cannot_bring_the_old_link_up_says_so(vps):
    """Прежние ключи не поднялись — код 1 и причина: бот не вправе писать
    «вернул прежние», когда линк лежит."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    vps.up_fail(1)
    r = vps.run("--rekey-revert")
    assert r.returncode == 1 and "на прежних ключах не поднялся" in r.stdout, r.stdout
    assert "rekey:" not in r.stdout
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE, "прежний конфиг не на месте"


# ── --rekey-drop ─────────────────────────────────────────────────────────────

def test_drop_after_commit_removes_the_old_keys_and_keeps_the_new(vps):
    """Переход зафиксирован hello новым ключом: `.prev` (второй экземпляр
    ключей) убирается, живые — новые, линк не трогается."""
    vps.run("--rekey-stage")
    vps.run("--rekey-commit")
    live_v, live_g = vps.conf.read_text(encoding="utf-8"), vps.gw.read_text(encoding="utf-8")
    r = vps.run("--rekey-drop")
    assert r.returncode == 0 and _last(r) == "rekey: dropped", r.stdout
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}, vps.files()
    assert vps.conf.read_text(encoding="utf-8") == live_v and vps.gw.read_text(encoding="utf-8") == live_g
    assert live_v != VPS_LIVE
    assert not vps.calls(), "drop трогал линк"


def test_drop_after_stage_removes_the_staged_keys(vps):
    vps.run("--rekey-stage")
    r = vps.run("--rekey-drop")
    assert r.returncode == 0 and _last(r) == "rekey: dropped"
    assert vps.files() == {"awglink.conf", "gw-awglink.conf"}
    assert vps.conf.read_text(encoding="utf-8") == VPS_LIVE
