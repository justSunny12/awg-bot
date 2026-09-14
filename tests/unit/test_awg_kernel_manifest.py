"""
Манифест версии AmneziaWG и скрипты, которые по нему ставят ядро и поднимают
сервер с нуля (docs/ROADMAP.md, п.8).

Скрипты собирают модуль ядра и создают боевой конфиг сервера — гонять их в CI
негде, а ошибка в них доезжает до чистого хоста и оставляет его без VPN.
Поэтому то, что ловится чтением, ловим чтением.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "install" / "awg.lock"
KERNEL = ROOT / "install" / "awg-kernel-install.sh"
SERVER = ROOT / "install" / "awg-server-init.sh"


@pytest.fixture(scope="module")
def lock() -> dict:
    out = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


@pytest.fixture(scope="module")
def kernel() -> str:
    return KERNEL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def server() -> str:
    return SERVER.read_text(encoding="utf-8")


# ── манифест ─────────────────────────────────────────────────────────────────

def test_lock_has_every_field_the_installer_reads(lock, kernel):
    """Скрипт падает на пустом поле осознанно — но список полей в скрипте и в
    манифесте обязан совпадать, иначе отказ приходит на чистом хосте."""
    block = kernel.split("for v in ", 1)[1].split("; do", 1)[0]
    required = [w for w in block.replace("\\", " ").split() if w.startswith("AWG_")]
    assert set(required) <= set(lock), f"в awg.lock нет: {sorted(set(required) - set(lock))}"
    for k in required:
        assert lock[k], f"{k} пуст"


def test_lock_versions_match_their_urls(lock):
    """Тег в ссылке и версия — одно и то же: разойдись они, соберётся не то,
    что объявлено, и `awg --version` навсегда разойдётся с манифестом."""
    for kind in ("MODULE", "TOOLS"):
        ver, url = lock[f"AWG_{kind}_VERSION"], lock[f"AWG_{kind}_URL"]
        assert f"/v{ver}." in url or url.endswith(f"v{ver}.tar.gz"), f"{kind}: {url} не про {ver}"
        assert re.fullmatch(r"[0-9a-f]{64}", lock[f"AWG_{kind}_SHA256"]), f"{kind}: sha256 не 64 hex"


def test_lock_declares_a_generation_and_protocol_id(lock):
    """Поколение — класс совместимости протокола; идентификатор уезжает в
    vpn:// и заморожен на поколение. Оба обязаны быть, иначе апдейтеру нечем
    решать, нужен ли переезд профилей."""
    assert lock["AWG_GENERATION"].isdigit() and int(lock["AWG_GENERATION"]) >= 1
    assert lock["AWG_PROTOCOL_ID"]


# ── сборка ядра ──────────────────────────────────────────────────────────────

def test_kernel_never_adds_a_repository(kernel):
    """PPA не подключаем никогда: apt однажды поднял бы ядро мимо поставки, и
    клиенты прежнего поколения перестали бы ходить без всякого объяснения."""
    for forbidden in ("add-apt-repository", "ppa:amnezia", "apt-key", "sources.list"):
        assert forbidden not in kernel, f"в скрипте появился {forbidden}"


def test_kernel_verifies_sha256_before_installing(kernel):
    """Тарбол ставится только после сверки с манифестом — иначе «прибитая
    версия» держится на честном слове CDN."""
    fetch = kernel.split("fetch() {", 1)[1].split("\n}", 1)[0]
    assert "sha256sum -c" in fetch and "die" in fetch
    assert 'rm -f "$f.part"' in fetch, "битый файл не должен оставаться в кэше"


def test_kernel_builds_for_every_installed_kernel(kernel):
    """`dkms install` без -k ставит модуль только под текущее ядро: загрузка в
    другое оставила бы хост без awg. Перебор — по ядрам с заголовками."""
    assert "kernels_with_headers()" in kernel
    body = kernel.split("step_module() {", 1)[1]
    assert "for k in $(kernels_with_headers)" in body
    assert 'dkms build -m "$MODULE" -v "$AWG_MODULE_VERSION" -k "$k"' in body
    assert 'dkms install -m "$MODULE" -v "$AWG_MODULE_VERSION" -k "$k"' in body


def test_kernel_dkms_tree_carries_the_real_version(kernel):
    """Апстримный dkms.conf у ВСЕХ сборок объявляет 1.0.0 — дерево одно, и
    новая установка затирает старую вместе с возможностью отката. Кладём дерево
    под версией и правим PACKAGE_VERSION."""
    assert 'tree="$DKMS_SRC_ROOT/$MODULE-$AWG_MODULE_VERSION"' in kernel
    assert 'make -C "$tmp/src" dkms-install DKMSDIR="$tree"' in kernel
    assert 'PACKAGE_VERSION=\\"$AWG_MODULE_VERSION\\"' in kernel
    assert "dkms uninstall" in kernel and "dkms remove" not in kernel, \
        "remove снёс бы дерево прежней версии — откат станет невозможен"


def test_kernel_is_idempotent_and_reports_drift(kernel):
    """Хосты, где эта же версия стояла до появления манифеста, пересборки не
    получают; а установленный, но ещё не работающий модуль — не «всё хорошо»."""
    assert 'ok "AmneziaWG $AWG_MODULE_VERSION уже стоит — ничего не делаю"' in kernel
    assert "exit 3" in kernel, "status обязан отличать расхождение кодом возврата"
    assert "awg-bot awg reload" in kernel


def test_kernel_reload_lowers_and_raises_every_interface(kernel):
    """Подмена работающего модуля: rmmod откажет, пока жив хоть один
    интерфейс, а не поднятый обратно линк оставляет шлюз без Telegram."""
    body = kernel.split('if [[ "$MODE" == "reload" ]]; then', 1)[1].split("\n# ── install", 1)[0]
    assert "awg show interfaces" in body
    assert body.index("run awg-quick down") < body.index("run rmmod") < body.index("run modprobe")
    assert body.index("run modprobe") < body.rindex("run awg-quick up")


# ── сервер с нуля ────────────────────────────────────────────────────────────

def test_server_takes_dot_one_and_gives_clients_dot_two(server):
    """Раскладка новых установок: сервер .1, клиенты с .2. На .1 садится
    dnsmasq условной маршрутизации — приватный резолвер получает адрес, на
    котором кто-то отвечает."""
    assert "Address = ${SUBNET_PREFIX}.1/24" in server
    assert 'SERVER_HOST_OCTET="1"' in server
    # У доставшегося от докерной Amnezia сервера адрес .0 — отдаём его как есть,
    # иначе первый клиент получил бы адрес самого сервера.
    assert 'SERVER_HOST_OCTET="${addr##*.}"' in server


def test_server_obfuscation_avoids_the_two_known_traps(server):
    """S1 + 56 == S2 делает длину init-пакета равной response, а повтор среди
    H1..H4 — два типа пакетов с одним заголовком: приёмник их не различит."""
    assert "$((S1 + 56))" in server and 'S2="$(rnd 15 150)"' in server
    h = server.split('H=""', 1)[1].split("read -r H1", 1)[0]
    assert "for x in $H" in h and "dup=1" in h


def test_server_is_idempotent(server):
    """Повторный запуск на живом сервере обязан быть безвредным: конфиг есть —
    читаем из него топологию и выходим, ключи не перегенерируем."""
    head = server.split('if [[ -f "$CONF" ]]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "не трогаю" in head and "exit 0" in head
    assert head.index("ListenPort") < head.index("exit 0")
    # Порт и подсеть живого сервера обязаны уехать наружу: установщик пишет их
    # в app.yaml, и без них бот падает на валидации «не задан server_port».
    assert "emit" in head and head.index("SUBNET_PREFIX=") < head.index("emit")


def test_server_rolls_back_a_config_that_does_not_come_up(server):
    """Конфиг, с которым интерфейс не поднялся, остаться не должен: иначе
    следующий запуск сочтёт сервер настроенным и пройдёт мимо поломки."""
    tail = server.split("systemctl enable --now", 1)[1]
    assert 'rm -f "$CONF"' in tail and "die" in tail


def test_server_enables_autostart(server):
    """В host-режиме интерфейс поднимает awg-quick@, и его надо включить:
    ребут ВПС уже оставлял хост без туннелей ровно из-за этого."""
    assert 'systemctl enable --now "awg-quick@$AWG_IF"' in server
    assert "net.ipv4.ip_forward" in server and "/etc/sysctl.d/" in server


def test_server_refuses_without_the_kernel(server):
    """Без модуля и тулз конфиг создавать бессмысленно — отказ должен звать
    установку ядра, а не оставлять полуфабрикат."""
    assert "awg-bot awg install" in server
    assert "modprobe amneziawg" in server


# ── апдейтер: фазы и поколения ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def bot_sh() -> str:
    return (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


def test_update_builds_the_kernel_before_swapping_code(bot_sh):
    """Сборка — ПЕРВЫМ делом, пока на диске рабочая установка. Не собралось —
    код не подменён, версия прежняя, сервис поднят обратно. Иначе хост остался
    бы с новым кодом и старым ядром: состояние, которого никто не проверяет."""
    body = bot_sh.split("cmd_update() {", 1)[1].split("\n}\n", 1)[0]
    i_build = body.index("awg-kernel-install.sh")
    i_swap = body.index("find \"$INSTALL_DIR\" -mindepth 1")
    assert i_build < i_swap, "ядро собирается после подмены кода"
    fail = body[i_build:i_swap]
    assert "systemctl start" in fail and "die" in fail, "при отказе сервис не поднимается обратно"
    assert 'AWG_LOCK="$src/install/awg.lock"' in body, "манифест берётся из НОВОЙ поставки"


def test_module_is_reloaded_only_when_the_running_one_differs(bot_sh):
    """Подмена работающего модуля гасит все интерфейсы — делать её на каждом
    обновлении незачем: тег тот же, значит и подменять нечего."""
    body = bot_sh.split("ensure_awg_module_loaded() {", 1)[1].split("\n}\n", 1)[0]
    assert "/sys/module/amneziawg/version" in body
    assert '[[ -n "$have" && "$have" != "$want" ]] || return 0' in body
    assert "reload" in body and "warn" in body, "неудачная подмена не должна ронять обновление"


def test_generation_bump_raises_a_second_interface(bot_sh):
    """Растёт поколение — рядом со старым интерфейсом поднимается второй, на
    новых параметрах, и переезд ждёт админа в UI."""
    body = bot_sh.split("ensure_awg_generation() {", 1)[1].split("\n}\n", 1)[0]
    assert 'yaml_get "$app" role' in body and "gateway" in body, "у шлюза клиентов нет"
    assert '[[ "$want" -gt "$applied" ]] || return 0' in body
    assert "awg-server-init.sh" in body
    assert 'yaml_set_soft "$app" migration_interface' in body
    assert 'yaml_set_soft "$app" migration_subnet_prefix' in body
    assert "awg_state_set AWG_GENERATION_TARGET" in body
    # пул адресов: .1 у нового интерфейса занимает сервер
    assert 'yaml_set_soft "$app" ip_host_start 2' in body
    # Мягкий вариант не случаен: правка идёт ВНУТРИ обновления, и ключ,
    # появившийся в шаблоне позже, у боевого конфига просто отсутствует.
    # Смерть здесь означала бы подменённый код и неподнятый сервис.
    assert 'yaml_set "$app"' not in body, "жёсткий yaml_set внутри обновления"


def test_missing_state_file_adopts_the_delivery_generation(bot_sh):
    """Первая поставка с манифестом усыновляет текущее поколение, а не объявляет
    переезд: обновления идут по одной ступени, значит ввод файла случается
    раньше любой смены поколения."""
    body = bot_sh.split("ensure_awg_generation() {", 1)[1].split("\n}\n", 1)[0]
    adopt = body.split('if [[ -z "$applied" ]]; then', 1)[1].split("fi", 1)[0]
    assert "awg_state_set AWG_GENERATION_APPLIED" in adopt and "return 0" in adopt


def test_second_interface_name_and_subnet_do_not_collide(bot_sh):
    """Имя и подсеть подбираются сами: занятое имя или пересечение подсетей —
    ровно то, где админ ошибётся, а отказ вылезет у клиентов."""
    body = bot_sh.split("ensure_awg_generation() {", 1)[1].split("\n}\n", 1)[0]
    assert '[[ -e "$conf_dir/awg$i.conf" || "awg$i" == "$base_if" ]] && continue' in body
    assert 'grep -rqs "$cand\\." "$conf_dir"' in body
    assert "не подобрать имя/подсеть" in body, "без свободных имён — предупреждение, а не тишина"


def test_installer_writes_topology_even_for_a_pre_existing_server(bot_sh):
    """Установка на хост с готовым awg0.conf (переустановка, образ с уже
    поднятой AmneziaWG) прежде умирала на валидации «не задан server_port» —
    топология писалась только для сервера, созданного нами."""
    body = bot_sh.split("ensure_awg_server() {", 1)[1].split("\n}\n", 1)[0]
    for key in ("server_port", "subnet_prefix", "subnet_cidr", "interface", "runtime"):
        assert f'yaml_set "$app" {key}' in body, f"{key} не пишется"
    assert body.index('yaml_set "$app" server_port') < body.index('if [[ "$created" == "1" ]]'), \
        "запись топологии снова спрятана под created"
    # первый клиентский адрес — следующий за адресом сервера, а не константа
    assert "BASH_REMATCH[1]} + 1" in body and "ip_host_start" in body
