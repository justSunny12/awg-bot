"""
Проверки routing-host-setup.sh — обвяза ВПС под условную маршрутизацию.

Скрипт правит iptables и /etc, гонять его в CI негде. Но самые дорогие ошибки в
нём текстовые: правило встаёт не в том порядке, откат не снимает то, что ставил
apply, подсказка оператору называет несуществующий набор. Всё это ловится
чтением исходника, а цена — выезд на боевой сервер.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from tests.conftest import SH, fake_cmd

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-host-setup.sh"


def _render_unit(script: str, tmp_path) -> str:
    """Прогоняет ветку --install-unit НАСТОЯЩИМ кодом скрипта и возвращает
    юнит: проверять надо то, что доедет до systemd, вместе с подстановками."""
    block = script[script.index('if [ "$MODE" = "unit" ]'):]
    block = block[:block.index("\nfi\n") + 4]
    unit = tmp_path / "awg-bot-routing.service"
    block = block.replace("UNIT=/etc/systemd/system/awg-bot-routing.service", f'UNIT="{unit}"')
    prog = "\n".join([
        "set -e", 'say(){ :; }', 'step(){ :; }', 'systemctl(){ :; }',
        'install_self(){ printf "/usr/local/sbin/routing-host-setup.sh"; }',
        "MODE=unit", block,
    ])
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return unit.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


# ── обход перехвата DNS ──────────────────────────────────────────────────────

# Имена, которые клиент обязан отрезолвить обычным DNS, прежде чем уйти в DoH.
# Ловить DoH по адресам бесполезно: эндпоинты живут на CDN, а не на анкасте
# резолвера, — поэтому единственная точка перехвата именно здесь.
DOH_ENDPOINTS = [
    "cloudflare-dns.com",
    "chrome.cloudflare-dns.com",
    "dns.google",
    "dns.quad9.net",
]


@pytest.mark.parametrize("host", DOH_ENDPOINTS)
def test_doh_endpoint_is_nxdomained(script, host):
    """DoH-эндпоинты гасятся в dnsmasq.

    Набор наполняет ТОЛЬКО наш резолвер: готовых списков российских подсетей
    не существует, всё держится на директивах ipset= в dnsmasq. Клиент, ушедший
    в DoH, спрашивает мимо нас и не добавляет в набор ничего — режим у него
    просто не работает. Отказ заметен (российские сервисы ругаются на адрес),
    но со стороны пользователя необъясним, поэтому закрываем его на входе.
    """
    assert f"address=/{host}/" in script


def test_dot_is_rejected_not_dropped(script):
    """DoT (853) режем REJECT'ом.

    DROP заставил бы клиента ждать таймаута на каждом запросе — резолв подвисал
    бы вместо того, чтобы откатиться на :53.
    """
    assert "--dport 853" in script
    assert "-j REJECT --reject-with tcp-reset" in script
    assert re.search(r"--dport 853[^\n]*\n?[^\n]*-j DROP", script) is None


# Шаги 4–4a гоняются настоящим кодом скрипта (ensure_rule/drop_rule/top_rule,
# run через sh -c) с подставным iptables: цепочка — файл, строка — правило в
# том виде, как его передал скрипт; -C/-I [n]/-A/-D честно. Проверяется ИТОГ
# в цепочке, а не порядок строк в файле: на хосте с правилами прежнего выпуска
# порядок вызовов ничего не гарантирует.

SUBNET = "10.8.1.0/24"
REJECT = f"-s {SUBNET} -p tcp --dport 853 -j REJECT --reject-with tcp-reset"
TAGGED = [f"-s {SUBNET} -m comment --comment awg-bot -j ACCEPT",
          f"-d {SUBNET} -m comment --comment awg-bot -j ACCEPT"]
# прежний выпуск ставил те же разрешения без метки и тем же порядком вызовов,
# то есть REJECT над ними
OLD_RELEASE = [REJECT, f"-d {SUBNET} -j ACCEPT", f"-s {SUBNET} -j ACCEPT"]


def _fake_iptables(tmp_path):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    chains = tmp_path / "chains"; chains.mkdir(exist_ok=True)
    fake_cmd(bin_dir / "iptables",
             f'C={chains}\n'
             't=filter; [ "$1" = -w ] && shift\n'
             '[ "$1" = -t ] && { t="$2"; shift 2; }\n'
             'op="$1"; ch="$2"; shift 2; R="$C/$t.$ch"; touch "$R"\n'
             'case "$op" in\n'
             '  -C) grep -qxF -- "$*" "$R"; exit $? ;;\n'
             '  -D) grep -qxF -- "$*" "$R" || exit 1\n'
             '      awk -v r="$*" \'!d && $0 == r {d = 1; next} {print}\' "$R" > "$R.t" && mv "$R.t" "$R"; exit 0 ;;\n'
             '  -A) printf "%s\\n" "$*" >> "$R"; exit 0 ;;\n'
             '  -I) n=1; case "$1" in [0-9]*) n="$1"; shift ;; esac\n'
             '      awk -v r="$*" -v n="$n" \'NR == n {print r; done = 1} {print} END {if (!done) print r}\' "$R" > "$R.t"\n'
             '      mv "$R.t" "$R"; exit 0 ;;\n'
             '  -S) sed "s/^/-A $ch /" "$R"; exit 0 ;;\n'
             'esac\nexit 2\n')
    # таблицы бота нет — прежняя схема, MASQUERADE ставит скрипт
    fake_cmd(bin_dir / "nft", "exit 1\n")
    return bin_dir, chains


def _apply_forward(script: str, tmp_path, start: list[str], *, mode: str = "apply") -> list[str]:
    bin_dir, chains = _fake_iptables(tmp_path)
    (chains / "filter.FORWARD").write_text("".join(r + "\n" for r in start), encoding="utf-8")
    helpers = script[script.index("\nsay()  {") + 1:script.index("\n# Юнит обязан ссылаться")]
    fns = script[script.index("\nensure_rule() {") + 1:script.index("\n# Имя awg-интерфейса нужно")]
    block = script[script.index("# 4) выход наружу"):script.index("# 5) чужой маршрут")]
    prog = "\n".join(["set -e", f"MODE={mode}", f"CLIENT_SUBNET={SUBNET}", "AWG_IF=awg0",
                      "LINK_IF=awglink", "MIGRATION_IF=", helpers, fns, block])
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    return [ln for ln in (chains / "filter.FORWARD").read_text(encoding="utf-8").splitlines() if ln]


def _reject_above_every_accept(chain: list[str]) -> bool:
    accepts = [i for i, r in enumerate(chain) if r.endswith("-j ACCEPT") and SUBNET in r]
    return REJECT in chain and all(chain.index(REJECT) < i for i in accepts)


@pytest.mark.parametrize("start", [
    pytest.param([], id="clean-host"),
    pytest.param(OLD_RELEASE, id="old-release-untagged"),
    pytest.param(OLD_RELEASE[1:] + [REJECT], id="old-accepts-above-reject"),
])
def test_dot_reject_ends_up_above_every_accept_in_the_chain(script, tmp_path, start):
    """REJECT 853 после --apply — выше всех ACCEPT подсети, на любом исходном
    состоянии. На хосте с правилами прежнего выпуска -C находил REJECT и не
    трогал его, а помеченные ACCEPT вставали через -I над ним: DoT проходил,
    Android Private DNS обходил резолвер бота, маршрутизация по DNS-наборам у
    таких устройств молча не работала."""
    chain = _apply_forward(script, tmp_path, start)
    assert _reject_above_every_accept(chain), chain
    assert chain.count(REJECT) == 1, chain
    for r in TAGGED:
        assert chain.count(r) == 1, chain
    # прежние без метки остаются: отличить их от правил AmneziaWG нечем
    for r in start:
        assert r in chain, chain


def test_repeated_apply_over_its_own_rules_keeps_the_order_and_duplicates_nothing(script, tmp_path):
    first = _apply_forward(script, tmp_path, OLD_RELEASE)
    again = _apply_forward(script, tmp_path, first)
    assert again == first, (first, again)
    assert _reject_above_every_accept(again)


def test_the_plan_changes_nothing_and_terminates(script, tmp_path):
    """План (без --apply) на хосте с правилами: run ничего не снимает, и цикл
    снятия обязан выйти, а не крутиться вечно."""
    start = OLD_RELEASE[1:] + [REJECT] + TAGGED
    assert _apply_forward(script, tmp_path, start, mode="plan") == start


def test_drop_rule_in_the_plan_does_not_spin_on_an_existing_rule(script, tmp_path):
    """drop_rule снимает правило, пока -C его находит. В плане run не исполняет
    ничего, и на хосте с правилом (RETURN резолвера админа, шаг 3) план
    крутился вечно, печатая одно и то же «would: … -D …»."""
    bin_dir, chains = _fake_iptables(tmp_path)
    rule = f"-s {SUBNET} -d 10.255.53.53 -p udp --dport 53 -j RETURN"
    (chains / "nat.PREROUTING").write_text(rule + "\n", encoding="utf-8")
    helpers = script[script.index("\nsay()  {") + 1:script.index("\n# Юнит обязан ссылаться")]
    fns = script[script.index("\nensure_rule() {") + 1:script.index("\n# Имя awg-интерфейса нужно")]
    prog = "\n".join(["set -e", "MODE=plan", helpers, fns, f"drop_rule nat PREROUTING {rule}"])
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True, timeout=10,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("would: iptables -t nat -D PREROUTING") == 1, r.stdout
    assert (chains / "nat.PREROUTING").read_text(encoding="utf-8") == rule + "\n"


def test_rollback_removes_what_apply_installs(script):
    """Откат снимает правило DoT.

    Забытое в откате правило переживает rollback и продолжает резать 853 уже
    после того, как фичу сняли, — отказ, который никто не свяжет с ботом.
    """
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1]
    assert "--dport 853" in rollback


# ── подсказки оператору ──────────────────────────────────────────────────────

def test_hint_does_not_name_a_set_nobody_creates(script):
    """В подсказках не должно быть ru_base/vpn_base.

    Общего базового набора в схеме нет: база вливается в каждый vpn_u<N>.
    Подсказка с несуществующим именем давала ноль на исправном хосте и
    отправляла чинить CAP_NET_ADMIN, который в порядке.
    """
    assert "ru_base" not in script
    assert "vpn_base" not in script


def test_fill_hint_uses_a_domain_that_will_be_in_the_set(script):
    """Домен в проверке наполнения обязан быть тем, что реально попадёт в набор.

    Набор перечисляет РОССИЙСКИЕ сервисы — то, что уходит на шлюз. Подсказка с
    заграничным доменом показывала бы ноль на здоровой системе и толкала чинить
    исправное. Раньше здесь стоял huggingface.co: он был верен для упразднённой
    обратной модели, где набор означал «за границу».

    Сверять со списком нельзя — он скачивается, а не зашит. Проверяем то, что
    проверяемо: домен в российской зоне.
    """
    hints = re.findall(r"""dig \+short @\$DNS_ADDR ([^\s"']+)""", script)
    assert hints, "в скрипте нет подсказки с dig — проверка наполнения потерялась"
    # example.com — проверка живости резолвера, она вне списков
    checked = [h for h in hints if h != "example.com"]
    assert checked, "нет ни одной проверки наполнения набора"
    for host in checked:
        assert host.endswith((".ru", ".рф", ".su")), (
            f"{host} не в российской зоне — в набор он не попадёт, "
            f"и подсказка будет врать")


# ── юнит автоприменения ──────────────────────────────────────────────────────

def test_unit_depends_only_on_the_network(script, tmp_path):
    """Юнит обвяза не требует чужих сервисов: иначе после ребута он не поднялся
    бы без них, а фича молча не завелась."""
    unit = _render_unit(script, tmp_path)
    directives = [ln for ln in unit.splitlines() if ln and not ln.startswith("#")]
    assert not any(ln.startswith(("Requires=", "BindsTo=")) for ln in directives), directives
    assert "After=network-online.target" in directives


def test_unit_execstart_points_at_the_installed_script(script, tmp_path):
    """Юнит пишется один раз, а срабатывает после каждого ребута — ExecStart
    обязан звать постоянный путь скрипта."""
    unit = _render_unit(script, tmp_path)
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "/usr/local/sbin/routing-host-setup.sh --apply" in exec_line


# ── маршрут и DNS: два отказа переезда ───────────────────────────────────────

def test_stale_route_is_removed(script):
    """Мало НЕ ставить свой маршрут — надо снять чужой.

    Чужой via-маршрут перекрывает connected: наружу всё уходит, а ответы
    пользователям отправляются в мёртвую сеть. Диагностика при этом зелёная —
    маршрут ведь есть.
    """
    body = script.split('step "5.', 1)[1].split("step ", 1)[0]
    assert "ip route del" in body
    assert "AWG_IF" in body, "чужой маршрут отличается от connected по интерфейсу"


def test_dnatted_dns_is_allowed_into_input(script):
    """После DNAT назначение — НАШ адрес, и пакет идёт в INPUT, а не в FORWARD.

    При политике DROP он там умирает: пользователь подключается, ходит по голым
    адресам (телеграм работает), а всё, что требует резолва, мертво. В
    """
    assert 'ensure_rule filter INPUT -s "$CLIENT_SUBNET" -d "$DNS_ADDR" -p udp --dport 53 -j ACCEPT' in script
    dnat = script.index("3. Перехват DNS")
    allow = script.index('step "3a.')
    forward = script.index("# 4) выход наружу")
    assert dnat < allow < forward, "разрешение в INPUT должно идти сразу за DNAT"


def test_input_allow_is_rolled_back(script):
    """Откат снимает и разрешения INPUT для DNS пользователей — иначе после отката
    на хосте остаётся дыра к резолверу, которую никто не помнит."""
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1].split("exit 0", 1)[0]
    drops = [ln.strip() for ln in rollback.splitlines() if ln.strip().startswith("drop_rule filter INPUT")]
    assert any("--dport 53" in ln and "-p udp" in ln for ln in drops)
    assert any("--dport 53" in ln and "-p tcp" in ln for ln in drops)


def test_masquerade_is_skipped_when_the_bot_owns_it(script):
    """NAT пользователей переехал в таблицу бота (awg_bot_guard), которая живёт и
    при выключенном файерволе. Два маскарада рядом трафик не ломают, но
    «единственная точка» перестаёт быть единственной — поэтому свои правила
    только там, где таблицы бота нет: на установках прежней схемы."""
    assert "guard_masquerades()" in script
    assert "nft -j list table inet awg_bot_guard" in script
    assert script.index("guard_masquerades()") < \
        script.index('ensure_rule nat POSTROUTING -s "$CLIENT_SUBNET" -j MASQUERADE')
    # откат по-прежнему снимает СВОИ правила — иначе прежняя схема не убирается
    assert 'drop_rule nat POSTROUTING -s "$CLIENT_SUBNET" -j MASQUERADE' in script


def test_peer_to_peer_traffic_is_not_masqueraded(script):
    """Пир → пир в тот же awg-интерфейс без MASQUERADE: иначе устройство админа
    приходит к шлюзу с адресом сервера, а файервол шлюза различает пиров по
    настоящему адресу. Исключение вставляется ПОСЛЕ маскарада (ensure_rule -I
    ставит его выше)."""
    assert 'ensure_rule nat POSTROUTING -s "$CLIENT_SUBNET" -o "$AWG_IF" -j ACCEPT' in script
    assert script.index('-s "$CLIENT_SUBNET" -j MASQUERADE') < script.index('-o "$AWG_IF" -j ACCEPT')


def test_link_and_migration_traffic_is_not_masqueraded_either(script):
    """Исключение для линка обязано стоять выше нашего MASQUERADE независимо от
    порядка юнитов: ставим его сами, после маскарада (ensure_rule -I)."""
    assert "gw_interface:" in script and "migration_interface:" in script
    for var in ("LINK_IF", "MIGRATION_IF"):
        line = f'ensure_rule nat POSTROUTING -s "$CLIENT_SUBNET" -o "${var}" -j ACCEPT'
        assert line in script, var
        assert script.index('-s "$CLIENT_SUBNET" -j MASQUERADE') < script.index(line)


# ── резолвер бота и адрес перехвата ──────────────────────────────────────────
# Блок параметров прогоняется настоящим кодом скрипта с подставным app.yaml
# и конфигом резолвера: важны ВЫЧИСЛЕННЫЕ значения, а не текст.

def _params(script: str, tmp_path, *, dns1: str, dns2: str, resolver_addrs=()) -> dict:
    app = tmp_path / "app.yaml"
    app.write_text("network:\n  subnet_cidr: \"10.8.1.0/24\"\nclient_config:\n"
                   f'  dns1: "{dns1}"\n  dns2: "{dns2}"\n', encoding="utf-8")
    rconf = tmp_path / "resolver.conf"
    if resolver_addrs:
        rconf.write_text("bind-interfaces\n" + "".join(f"listen-address={a}\n" for a in resolver_addrs),
                         encoding="utf-8")
    block = script[script.index("# ── параметры"):script.index("MODE=\"plan\"")]
    prog = (f'_APP_YAML="{app}"\nRESOLVER_CONF="{rconf}"\n' + block
            + '\nprintf "USE_DUMMY=%s\\nDNS_ADDR=%s\\nFACADE=%s\\nREFERENCED=%s\\n" '
              '"$USE_DUMMY" "$DNS_ADDR" "$CLIENT_DNS_FACADE" "$DUMMY_REFERENCED"')
    r = subprocess.run([SH, "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.strip().splitlines())


def test_with_the_bot_resolver_the_interception_targets_it_and_needs_no_dummy(script, tmp_path):
    """Свежая установка: dnsmasq бота слушает <подсеть>.1, DNAT :53 старых
    профилей ведёт туда же, dummy-интерфейс не поднимается вовсе."""
    p = _params(script, tmp_path, dns1="10.8.1.1", dns2="10.8.1.1", resolver_addrs=["10.8.1.1"])
    assert p["USE_DUMMY"] == "" and p["DNS_ADDR"] == "10.8.1.1" and p["FACADE"] == ""


def test_without_the_bot_resolver_the_old_scheme_stays(script, tmp_path):
    p = _params(script, tmp_path, dns1="1.1.1.1", dns2="1.0.0.1")
    assert p["USE_DUMMY"] == "1" and p["DNS_ADDR"] == "10.255.53.1" and p["REFERENCED"] == ""


def test_dummy_address_in_client_configs_keeps_the_dummy_alive(script, tmp_path):
    """Адрес dummy вшит в конфиги пользователей (dns2) — dummy обязан жить даже при
    резолвере бота, и перехват остаётся на нём: снять значит оставить людей с
    мёртвым вторым DNS до перевыпуска."""
    p = _params(script, tmp_path, dns1="10.9.1.1", dns2="10.255.53.1", resolver_addrs=["10.9.1.1"])
    assert p["REFERENCED"] == "1" and p["USE_DUMMY"] == "1" and p["DNS_ADDR"] == "10.255.53.1"
    assert p["FACADE"] == "1", "фасадный адрес на dummy тоже оставлен"


def test_facade_address_alone_also_counts_as_referenced(script, tmp_path):
    """Приватный dns1 на dummy без резолвера бота (хост до v2.19) — тоже
    ссылка на dummy."""
    p = _params(script, tmp_path, dns1="10.9.1.1", dns2="10.9.1.1")
    assert p["REFERENCED"] == "1" and p["USE_DUMMY"] == "1" and p["FACADE"] == "1"


def _rollback(script: str, tmp_path, *, referenced: bool) -> str:
    """Ветка --rollback с подставными командами: что она снимет, а что оставит."""
    block = script[script.index('if [ "$MODE" = "rollback" ]'):]
    block = block[:block.index("\n    exit 0\nfi\n") + len("\n    exit 0\nfi\n")]
    block = block.replace("exit 0", "true")
    prelude = "\n".join([
        'say(){ printf "%s\\n" "$*"; }', 'step(){ :; }',
        'run(){ printf "RUN %s\\n" "$*"; }', 'drop_rule(){ printf "DROP %s\\n" "$*"; }',
        'CLIENT_SUBNET=10.8.1.0/24', 'DNS_ADDR=10.255.53.1', 'CLIENT_DNS_ADDR=10.9.1.1',
        'CLIENT_DNS_FACADE=1', 'CONT_IP=""', 'DNSMASQ_CONF=/etc/dnsmasq.d/awgbot-base.conf',
        'DNSMASQ_SERVICE=dnsmasq', 'DNS_IF=awgdns0',
        f'DUMMY_REFERENCED={"1" if referenced else ""}', 'MODE=rollback',
    ])
    r = subprocess.run([SH, "-c", prelude + "\n" + block], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_rollback_removes_the_dummy_when_nothing_references_it(script, tmp_path):
    out = _rollback(script, tmp_path, referenced=False)
    assert "RUN ip link del awgdns0" in out and "RUN rm -f /etc/dnsmasq.d/awgbot-base.conf" in out


def test_rollback_keeps_the_dummy_and_its_dnsmasq_while_client_configs_point_at_it(script, tmp_path):
    """Снятие обвязки не имеет права уронить второй DNS у людей: адрес вшит в
    выданные конфиги, перепишет их только переезд — он же и уронит dummy."""
    out = _rollback(script, tmp_path, referenced=True)
    assert "ip link del" not in out and "rm -f /etc/dnsmasq.d/awgbot-base.conf" not in out
    assert "оставлены" in out
    # остальное снимается как прежде
    assert "DROP nat PREROUTING" in out and "DROP filter FORWARD" in out
    assert "RUN rm -f /etc/systemd/system/awg-bot-routing.service" in out


def test_rollback_drops_only_its_own_tagged_forward_accepts(script, tmp_path):
    """Те же разрешения FORWARD для подсети пользователей часто ставит сам
    AmneziaWG (PostUp конфига интерфейса). Откат снимал их по совпадению
    правила — и пользователи на остающемся VPN оставались без пересылки при
    политике DROP. Свои ставятся с меткой awg-bot, и откат снимает только их."""
    out = _rollback(script, tmp_path, referenced=False)
    fwd = [ln for ln in out.splitlines() if ln.startswith("DROP filter FORWARD") and "ACCEPT" in ln]
    assert sorted(fwd) == sorted([
        "DROP filter FORWARD -s 10.8.1.0/24 -m comment --comment awg-bot -j ACCEPT",
        "DROP filter FORWARD -d 10.8.1.0/24 -m comment --comment awg-bot -j ACCEPT",
    ]), fwd
    apply = script[script.index("# 4) выход наружу"):]
    for d in ("-s", "-d"):
        assert f'ensure_rule filter FORWARD {d} "$CLIENT_SUBNET" -m comment --comment awg-bot -j ACCEPT' in apply
        assert f'FORWARD {d} "$CLIENT_SUBNET" -j ACCEPT' not in script, "правило без метки — откат его не снимет"


def test_a_stale_iptables_masquerade_is_dropped_when_the_bot_leads_nat():
    """Правило прежней схемы стояло выше исключения для линка: пользователи
    уходили к шлюзу с адресом ВПС. Когда NAT ведёт таблица бота, скрипт
    снимает его, а не оставляет «рядом»."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "install" / "routing-host-setup.sh").read_text(encoding="utf-8")
    branch = src.split("if guard_masquerades; then", 1)[1].split("else", 1)[0]
    assert 'drop_rule nat POSTROUTING -s "$CLIENT_SUBNET" -j MASQUERADE' in branch


# ── резолвер админа мимо перехвата :53 ───────────────────────────────────────

def test_the_admin_resolver_is_inserted_above_the_dns_interception(script):
    """DNAT всего :53 пользователей увёл бы запросы устройств админа к резолверу
    админа на резолвер пользователей — без записей awg.internal, то есть «SMB в
    туннеле» молча не работал бы. RETURN обязан стоять в цепочке ВЫШЕ DNAT:
    -I кладёт в начало, значит в файле — после DNAT, и каждый прогон заново."""
    step3 = script[script.index('step "3. Перехват DNS'):script.index('step "3a.')]
    dnat = step3.index("-j DNAT --to-destination")
    ins = step3.index("iptables -t nat -I PREROUTING -s $CLIENT_SUBNET -d $ADMIN_DNS -p $_p --dport 53 -j RETURN")
    drop = step3.index('drop_rule nat PREROUTING -s "$CLIENT_SUBNET" -d "$ADMIN_DNS" -p "$_p" --dport 53 -j RETURN')
    assert dnat < drop < ins, "RETURN ставится раньше DNAT — окажется ниже него и не сработает"
    assert "for _p in udp tcp; do" in step3


def test_the_admin_resolver_exception_is_rolled_back(script):
    rollback = script.split('if [ "$MODE" = "rollback" ]', 1)[1].split("exit 0", 1)[0]
    for p in ("udp", "tcp"):
        assert (f'drop_rule nat PREROUTING -s "$CLIENT_SUBNET" -d "$ADMIN_DNS" -p {p} --dport 53 -j RETURN'
                in rollback), f"откат оставляет RETURN {p}"


def test_the_admin_resolver_address_comes_from_the_bot_setting(script):
    """Обвязка и бот обязаны говорить об одном адресе: ключ client_config.admin_dns."""
    assert "/^  admin_dns:/" in script and 'ADMIN_DNS="${ADMIN_DNS:-10.255.53.53}"' in script
