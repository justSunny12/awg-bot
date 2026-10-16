"""
Общее для test_gw_lan_script_*.py: текст routing-gw-setup.sh, вшитые
скрипты (списки, свои домены, сервисы соседних сетей) во временном каталоге,
подставные ip/curl/nft/systemctl/dig, правило домена канона.

Локальная сеть без VPN на шлюзе — раздел 5
routing-gw-setup.sh и два вшитых в него скрипта: списки и персональные домены.

Гоняем настоящий код скриптов с подменёнными ip/curl/nft/systemctl/dig: то,
что уедет на шлюз, а не грепаем исходник там, где можно исполнить.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from tests.conftest import SH, fake_cmd


SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _heredoc(script: str, var: str, tag: str) -> str:
    """Тело `cat > "$VAR.new" <<'TAG' … TAG` (хелпер пишется во временный файл и mv)."""
    return script.split(f'cat > "${var}.new" <<\'{tag}\'\n', 1)[1].split(f"\n{tag}\n", 1)[0] + "\n"


def _fake(bin_dir: Path, name: str, body: str) -> None:
    fake_cmd(bin_dir / name, body)


def _sh(prog: str, *, env: dict, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([SH, "-c", prog], capture_output=True, text=True, env=env, cwd=cwd)


# ── персональные списки: awg-lan-domain.sh ───────────────────────────────────

@pytest.fixture()
def domain_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    log = tmp_path / "log"
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "sleep", "")
    _fake(bin_dir, "id", "echo 0\n")
    _fake(bin_dir, "dig", "case \"$*\" in *example.com*) echo 198.51.100.34; echo 198.51.100.35 ;; esac\n")
    _fake(bin_dir, "nft", f'echo "nft $*" >> {log}\n')
    # dnsmasq есть в PATH — скрипт проверяет свои списки `dnsmasq --test` до рестарта
    _fake(bin_dir, "dnsmasq", f'echo "dnsmasq $*" >> {log}; exit "${{DNSMASQ_TEST_RC:-0}}"\n')
    conf = tmp_path / "awg0.conf"; conf.write_text("[Peer]\nEndpoint = vps.example.net:51820\n", encoding="utf-8")
    tool = tmp_path / "awg-lan-domain.sh"
    tool.write_text(_heredoc(script, "LAN_DOMAIN", "DOMEOF"), encoding="utf-8"); tool.chmod(0o755)
    # AWG_LAN_DUMP — каталог блокировки lists.lock и исходника фида vpn-feed.src
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_UPLINK_CONF": str(conf),
           "AWG_LAN_DUMP": str(tmp_path / "dump"), "AWG_HTML": "1"}     # как зовёт агент: домены в <code>
    return tool, dns_d, log, env


def _run(tool, env, *args):
    return subprocess.run([SH, str(tool), *args], capture_output=True, text=True, env=env)


# ── списки: awg-lan-lists.sh ─────────────────────────────────────────────────

@pytest.fixture()
def lists_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    dump = tmp_path / "dump"
    log = tmp_path / "log"
    feeds = tmp_path / "feeds"; feeds.mkdir()
    lines = ["ipset=/youtube.com/googlevideo.com/vpn_domains", "ipset=/shop.ru/vpn_domains",
             "ipset=/x.com/vpn_domains", "<html>заглушка провайдера</html>", "# comment"]
    lines += [f"ipset=/site{i}.org/vpn_domains" for i in range(12)]
    (feeds / "domains").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (feeds / "telegram").write_text("91.108.4.0/22\n149.154.160.0/20\n", encoding="utf-8")
    (feeds / "goog").write_text('{"prefixes":[{"ipv4Prefix": "8.8.8.0/24"},{"ipv6Prefix":"2001::/32"}]}', encoding="utf-8")
    # curl: URL → файл фикстуры; неизвестный — отказ
    _fake(bin_dir, "curl",
          'out=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift ;; http*) url="$1" ;; esac; shift; done\n'
          f'case "$url" in *inside-dnsmasq-ipset*) f={feeds}/domains ;; *Subnets/IPv4/telegram*) f={feeds}/telegram ;;'
          f' *goog.json*) f={feeds}/goog ;; *) exit 22 ;; esac\n'
          'if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi\n')
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "nft", f'if [ "$1" = "-f" ]; then echo "nft -f: $(cat)" >> {log}; else echo "nft $*" >> {log}; echo "elements = {{ 1.2.3.0/24 }}"; fi\n')
    _fake(bin_dir, "dnsmasq", f'echo "dnsmasq $*" >> {log}; exit "${{DNSMASQ_TEST_RC:-0}}"\n')
    tool = tmp_path / "awg-lan-lists.sh"
    tool.write_text(_heredoc(script, "LAN_LISTS", "LISTSEOF"), encoding="utf-8"); tool.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_LAN_DUMP": str(dump),
           "AWG_LAN_SUBNET_SERVICES": "telegram"}
    return tool, dns_d, dump, log, env


# ── списки из канала линка ──────────────────

@pytest.fixture()
def channel_env(lists_env, tmp_path):
    """Те же списки, но фиды привёз канал: каталог с domains.lst и nets.lst, а
    curl записывает каждый вызов — адрес локальной сети не должен ходить за фидами."""
    tool, dns_d, dump, log, env = lists_env
    bin_dir = Path(env["PATH"].split(":", 1)[0])
    _fake(bin_dir, "curl", f'echo "curl $*" >> {log}\nexit 22\n')
    feed = tmp_path / "channel-feed"; feed.mkdir()
    (feed / "domains.lst").write_text(
        "".join(f"ipset=/chan{i}.org/vpn_domains\n" for i in range(12))
        + "ipset=/shop.ru/vpn_domains\n<html>мусор</html>\n", encoding="utf-8")
    (feed / "nets.lst").write_text("91.108.4.0/22\n8.8.8.0/24\nне подсеть\n", encoding="utf-8")
    return tool, dns_d, dump, log, {**env, "AWG_LAN_FROM": str(feed)}, feed


def _failing_restart(bin_dir: Path, log: Path, fails: int = 1) -> None:
    """systemctl, у которого первые `fails` рестартов dnsmasq не проходят."""
    cnt = log.parent / "restarts"
    _fake(bin_dir, "systemctl",
          f'echo "systemctl $*" >> {log}\n'
          f'if [ "$1" = restart ]; then n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}\n'
          f'  [ "$n" -le {fails} ] && exit 1; fi\nexit 0\n')


# ── сервисы соседних сетей: awg-lan-services.sh ─────────────────────────────

def _svc_file() -> str:
    """Файл так, как его соберёт агент-получатель: из общего модуля."""
    from awgbot.domain import gwservices as gs
    items = [{"t": "_smb._tcp", "n": "NASPi5", "h": "naspi5", "p": 445, "a": "192.168.1.10"},
             {"t": "_smb._tcp", "n": "Time Machine", "h": "naspi5", "p": 10445, "a": "192.168.1.10"}]
    return gs.render_dnsmasq(items, ["192.168.68.0/24"])


@pytest.fixture()
def svc_env(script, tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    dns_d = tmp_path / "dnsmasq.d"; dns_d.mkdir()
    dump = tmp_path / "dump"
    log = tmp_path / "log"; log.write_text("", encoding="utf-8")
    _fake(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\n')
    _fake(bin_dir, "dnsmasq", f'echo "dnsmasq $*" >> {log}; exit "${{DNSMASQ_TEST_RC:-0}}"\n')
    _fake(bin_dir, "id", 'echo "${FAKE_UID:-0}"\n')
    tool = tmp_path / "awg-lan-services.sh"
    tool.write_text(_heredoc(script, "LAN_SERVICES", "SVCEOF"), encoding="utf-8"); tool.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "AWG_DNSMASQ_D": str(dns_d), "AWG_LAN_DUMP": str(dump)}
    new = tmp_path / "peer-services.conf.new"

    def go(text: str | None, **extra) -> subprocess.CompletedProcess:
        args = []
        if text is not None:
            new.write_text(text, encoding="utf-8")
            args = [str(new)]
        return subprocess.run([SH, str(tool), *args], capture_output=True, text=True,
                              env={**env, **extra})
    return go, dns_d / "awg-gw-peer-services.conf", log


OLD_SVC = "# прежние записи\nlocal=/awg.internal/\n"


# ── свои списки: блокировка, правило домена, вычитание, наборы, sync ────────
#

VPN_USER = "awg-gw-vpn-user.conf"
RU_USER = "awg-gw-ru-user.conf"
FEED = "awg-gw-vpn-feed.conf"

# dig: у каждого домена свой адрес — по логу nft видно, чей адрес куда лёг
_DIG_MAP = {"example.com": ["198.51.100.34", "198.51.100.35"], "shop.ru": ["10.1.1.1"],
            "bank.ru": ["10.2.2.2"], "news.org": ["10.3.3.3"], "alpha.com": ["10.4.4.4"],
            "zeta.com": ["10.5.5.5"]}


def _vpn_line(d: str) -> str:
    return f"nftset=/{d}/inet#awg_lan#lan_vpn4"


def _ru_line(d: str) -> str:
    return f"nftset=/{d}/inet#awg_lan#lan_ru4"


def _own(dns_d: Path, vpn=(), ru=()) -> None:
    """Состояние своих списков до вызова — как их оставила прежняя версия."""
    (dns_d / VPN_USER).write_text("".join(_vpn_line(d) + "\n" for d in vpn), encoding="utf-8")
    (dns_d / RU_USER).write_text("".join(_ru_line(d) + "\n" for d in ru), encoding="utf-8")


def _bin(env: dict) -> Path:
    return Path(env["PATH"].split(":", 1)[0])


def _mapped_dig(env: dict) -> None:
    body = 'case "$*" in\n' + "".join(
        f'  *" {d} "*) ' + "; ".join(f"echo {ip}" for ip in ips) + " ;;\n" for d, ips in _DIG_MAP.items()
    ) + "esac\n"
    _fake(_bin(env), "dig", body)


def _stateful_nft(env: dict, log: Path, sets: Path) -> None:
    """nft с памятью: наборы — файлы адресов в `sets`. add принимает список
    через запятую (`{ a,b,c }` — так наполняет fill_set), delete несуществующего
    адреса — ошибка, как у настоящего nft; list set — вывод для слепка."""
    sets.mkdir(exist_ok=True)
    body = r"""echo "nft $*" >> @LOG@
S=@SETS@
case "$1 $2" in
  "add element") for ip in $(echo "$6" | tr -d '{} ' | tr ',' ' '); do
          grep -qxF "$ip" "$S/$5" 2>/dev/null || echo "$ip" >> "$S/$5"; done ;;
  "delete element") ip=$(echo "$6" | tr -d '{} ')
      grep -qxF "$ip" "$S/$5" 2>/dev/null || { echo "Error: element does not exist" >&2; exit 1; }
      grep -vxF "$ip" "$S/$5" > "$S/$5.x"; mv "$S/$5.x" "$S/$5" ;;
  "flush set") : > "$S/$5" ;;
  "list set") echo "table inet awg_lan {"; echo "  set $5 {"
      echo "    elements = { $(sort "$S/$5" 2>/dev/null | paste -sd, - | sed 's/,/, /g') }"; echo "  }"; echo "}" ;;
esac
exit 0
"""
    _fake(_bin(env), "nft", body.replace("@LOG@", str(log)).replace("@SETS@", str(sets)))


def _set(sets: Path, name: str) -> set[str]:
    f = sets / name
    return set(f.read_text().split()) if f.exists() else set()


def _fill(sets: Path, name: str, ips) -> None:
    (sets / name).write_text("".join(ip + "\n" for ip in ips), encoding="utf-8")


@pytest.fixture()
def own_env(domain_env):
    """domain_env с dig по таблице и nft с памятью; dump — каталог блокировки,
    исходника фида и слепка lan_vpn4 (тот же, что у скрипта фидов на шлюзе)."""
    tool, dns_d, log, env = domain_env
    _mapped_dig(env)
    dump = Path(env["AWG_LAN_DUMP"]); dump.mkdir(exist_ok=True)
    _stateful_nft(env, log, dump.parent / "sets")
    return tool, dns_d, dump, log, env


def _sets_of(dump: Path) -> Path:
    return dump.parent / "sets"


def _log(log: Path) -> str:
    return log.read_text() if log.exists() else ""


# Исходник фида так, как его оставляет awg-lan-lists.sh: после перевода
# ipset= → nftset=, до вычитания исключений, с мусором и комментарием
_SRC = ["# фид", _vpn_line("youtube.com/googlevideo.com"), _vpn_line("shop.ru"), _vpn_line("x.com"),
        "<html>заглушка</html>"]


def _feed_from(src_lines, minus=()) -> str:
    """Фид, каким его собрал бы скрипт фидов: исходник минус исключения, только
    наши директивы и комментарии."""
    out = []
    for ln in src_lines:
        if ln.startswith("#"):
            out.append(ln); continue
        if not ln.startswith("nftset=/"):
            continue
        parts = ln.split("/")
        doms = [d for d in parts[1:-1] if d not in minus]
        if doms:
            out.append("/".join([parts[0], *doms, parts[-1]]))
    return "".join(ln + "\n" for ln in out)


def _with_src(dns_d: Path, dump: Path, minus=()) -> None:
    (dump / "vpn-feed.src").write_text("\n".join(_SRC) + "\n", encoding="utf-8")
    (dns_d / FEED).write_text(_feed_from(_SRC, minus), encoding="utf-8")


# Правило домена: на сервере AWG (этап 2) — то же самое выражение
_CANON_DOMAIN_RE = re.compile(
    r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})")


def _long(n: int) -> str:
    """Домен ровно из n символов (метки по 63)."""
    return ".".join(["a" * 63] * 3) + "." + "b" * (n - 196) + ".com"


_DOMAIN_EXAMPLES = {
    # проходят
    "a.bc": True, "example.com": True, "sub.example.co.uk": True, "x-y.com": True,
    "123.com": True, "1.2.3.example.org": True, "xn--80ak6aa92e.com": True,
    "a" * 63 + ".com": True, _long(253): True, "a.abcdefghij": True,
    "example.xn--p1ai": True, "xn--80aswg.xn--p1ai": True,
    # не проходят
    "-x.com": False, "x-.com": False, "a..b.com": False, ".a.com": False, "a.com.": False,
    "example": False, "xn--p1ai": False, "example.xn--": False, "a.c": False, "a.b1": False, "a_b.com": False,
    "a" * 64 + ".com": False, _long(254): False, "пример.рф": False, "münchen.de": False,
    "a.co1m": False,
}


def _nft(log: Path) -> list[str]:
    return [ln for ln in _log(log).splitlines() if ln.startswith("nft ")]


def _sync(tool, env, tmp_path, text: str, name="own.txt"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return _run(tool, env, "sync", str(f))


def _fill_run(tool, env, tmp_path, text: str, name="fill.txt"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return _run(tool, env, "fill", str(f))


# ── копии для отката — вне conf-dir ──────────────────────────────────────────
# dnsmasq читает в conf-dir всё, кроме .dpkg-*: копия `*.prev.awg` рядом с
# новым файлом читалась вместе с ним, и в момент рестарта у демона было два
# набора директив (прежний и новый) — дубли, а для записей SMB ещё и две цели
# SRV на одно имя. Теперь копии лежат в $AWG_LAN_DUMP/rollback/, а подменённый
# systemctl записывает, что лежало в conf-dir именно в момент рестарта.

def _snapshot_restart(env: dict, log: Path, fails: int = 0) -> Path:
    """systemctl, который на `restart` пишет состав conf-dir и rollback/ в
    файл снимков; первые `fails` рестартов не проходят."""
    snap = log.parent / "restart-snapshots"
    cnt = log.parent / "restart-count"
    _fake(_bin(env), "systemctl",
          f'echo "systemctl $*" >> {log}\n'
          'if [ "$1" = restart ]; then\n'
          f'  echo "--" >> {snap}\n'
          f'  ls -A "$AWG_DNSMASQ_D" | sed "s|^|d:|" >> {snap}\n'
          f'  ls -A "$AWG_LAN_DUMP/rollback" 2>/dev/null | sed "s|^|rb:|" >> {snap}\n'
          f'  n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}\n'
          f'  [ "$n" -le {fails} ] && exit 1\n'
          'fi\nexit 0\n')
    return snap


def _snapshots(snap: Path) -> list[tuple[set, set]]:
    """[(файлы conf-dir, файлы rollback/)] на каждый рестарт."""
    out = []
    for block in snap.read_text().split("--\n")[1:]:
        lines = block.splitlines()
        out.append(({ln[2:] for ln in lines if ln.startswith("d:")},
                    {ln[3:] for ln in lines if ln.startswith("rb:")}))
    return out


def _no_copies(names: set) -> bool:
    return not any(".prev" in n or n.endswith((".bak", ".tmp", ".new")) for n in names)
