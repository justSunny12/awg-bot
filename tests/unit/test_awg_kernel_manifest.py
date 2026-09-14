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
def bot_sh() -> str:
    return (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


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


def test_lock_tags_match_their_urls(lock):
    """Ссылка обязана вести на объявленный ТЕГ. Версия с тегом не сверяется
    намеренно: апстрим не бампает version.h, и у тегов v3.1.20260812…0906 она
    одна и та же — требование их совпадения запрещало бы обновляться вовсе."""
    for kind in ("MODULE", "TOOLS"):
        tag, url = lock[f"AWG_{kind}_TAG"], lock[f"AWG_{kind}_URL"]
        assert tag.startswith("v"), f"{kind}: тег без v — {tag}"
        assert url.endswith(f"{tag}.tar.gz"), f"{kind}: {url} не про {tag}"
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
    assert 'dkms build -m "$MODULE" -v "$mtag" -k "$k"' in body
    assert 'dkms install -m "$MODULE" -v "$mtag" -k "$k"' in body


def test_kernel_dkms_tree_carries_the_real_version(kernel):
    """Апстримный dkms.conf у ВСЕХ сборок объявляет 1.0.0 — дерево одно, и
    новая установка затирает старую вместе с возможностью отката. Кладём дерево
    под версией и правим PACKAGE_VERSION."""
    assert 'tree="$DKMS_SRC_ROOT/$MODULE-$mtag"' in kernel, "дерево снова по версии"
    assert 'make -C "$tmp/src" dkms-install DKMSDIR="$tree"' in kernel
    assert 'PACKAGE_VERSION=\\"$mtag\\"' in kernel
    assert "dkms uninstall" in kernel and "dkms remove" not in kernel, \
        "remove снёс бы дерево прежней версии — откат станет невозможен"


def _lock_file(tmp_path, *, mod_tag="v3.1.20260906", tools_tag="v3.1.20260812",
               mod_ver="3.1.20260812", tools_ver="3.1.20260812"):
    f = tmp_path / "awg.lock"
    f.write_text(
        f"AWG_GENERATION=1\nAWG_PROTOCOL_ID=p\n"
        f"AWG_MODULE_TAG={mod_tag}\nAWG_MODULE_VERSION={mod_ver}\n"
        f"AWG_MODULE_URL=https://x/{mod_tag}.tar.gz\nAWG_MODULE_SHA256=a\n"
        f"AWG_TOOLS_TAG={tools_tag}\nAWG_TOOLS_VERSION={tools_ver}\n"
        f"AWG_TOOLS_URL=https://x/t.tar.gz\nAWG_TOOLS_SHA256=b\n", encoding="utf-8")
    return f


def _run_kernel(tmp_path, mode, *, module="", loaded="", tools="", lock=None,
                built_tag="", tools_built="", src_disk="AAA", src_loaded=None):
    """Запускает awg-kernel-install.sh с подменёнными modinfo/awg и состоянием.

    Скрипт решает, пересобирать ли ядро, — и решение видно только по коду
    возврата. Сверка подстрок пропустила бы и перепутанные ветки, и потерянный
    exit 3, ради которого status вообще существует.

    srcversion задаётся отдельно от строки версии намеренно: апстрим не бампает
    version.h, и у разных тегов она одинакова — именно поэтому тождество ведётся
    по тегу, а «работает ли собранное» по srcversion.
    """
    import os
    import subprocess
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modinfo").write_text(
        '#!/bin/sh\n'
        f'[ -n "{module}" ] || exit 1\n'
        f'case "$2" in srcversion) printf "%s\\n" "{src_disk}" ;; '
        f'*) printf "%s\\n" "{module}" ;; esac\n', encoding="utf-8")
    (bin_dir / "awg").write_text(
        f'#!/bin/sh\n[ -n "{tools}" ] || exit 1\nprintf "amneziawg-tools v{tools} - x\\n"\n',
        encoding="utf-8")
    for f in ("modinfo", "awg"):
        (bin_dir / f).chmod(0o755)
    sysver = tmp_path / "loaded"
    sysver.write_text(loaded, encoding="utf-8")
    syssrc = tmp_path / "loaded_src"
    syssrc.write_text(src_disk if src_loaded is None else src_loaded, encoding="utf-8")
    state = tmp_path / "awg.state"
    lines = []
    if built_tag:
        lines.append(f"AWG_MODULE_TAG_BUILT={built_tag}")
    if tools_built:
        lines.append(f"AWG_TOOLS_TAG_BUILT={tools_built}")
    state.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    lock_file = lock or _lock_file(tmp_path)
    src = (KERNEL.read_text(encoding="utf-8")
           .replace('"/sys/module/$MODULE/version"', f'"{sysver}"')
           .replace('"/sys/module/$MODULE/srcversion"', f'"{syssrc}"'))
    run_me = tmp_path / "kernel.sh"
    run_me.write_text(src, encoding="utf-8")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               AWG_LOCK=str(lock_file), AWG_STATE=str(state))
    return subprocess.run(["bash", str(run_me), mode], capture_output=True, text=True, env=env)


def test_status_is_quiet_when_the_built_tag_matches(tmp_path):
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "status", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260906", tools_built="v3.1.20260812")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "совпадает с манифестом" in res.stdout


def test_status_returns_three_when_the_manifest_moved_to_another_tag(tmp_path):
    """Тег в манифесте сменился, а собрано прежнее. Строка версии у обоих тегов
    ОДНА И ТА ЖЕ — сравнивай мы её, смена тега прошла бы незамеченной, и
    «прибито гвоздями» держалось бы на честном слове."""
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "status", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260812", tools_built="v3.1.20260812")
    assert res.returncode == 3, res.stdout
    assert "РАСХОДИТСЯ" in res.stdout


def test_status_rebuilds_when_nobody_recorded_the_tag(tmp_path):
    """Собирали руками или до появления состояния — правды о теге нет, и
    узнать её неоткуда: version.h у всех тегов одинаков. Честнее пересобрать."""
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "status", module=ver, loaded=ver, tools=ver)
    assert res.returncode == 3
    assert "собирали не мы" in res.stdout


def test_status_flags_a_built_but_not_running_module(tmp_path):
    """Собрано по манифесту, а работает модуль других исходников: до подмены
    или перезагрузки клиентов обслуживает прежнее ядро. Ловится ТОЛЬКО по
    srcversion — строки версий тут совпадают."""
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "status", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260906", tools_built="v3.1.20260812",
                      src_disk="НОВЫЙ", src_loaded="СТАРЫЙ")
    assert res.returncode == 3
    assert "reload" in res.stdout and "других исходников" in res.stdout


def test_install_does_nothing_when_the_tag_matches(tmp_path):
    """Хосты, собранные из того же тега, пересборки не получают — иначе каждое
    обновление гасило бы туннели без причины."""
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "plan", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260906", tools_built="v3.1.20260812")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "уже собран" in res.stdout
    assert "would" not in res.stdout, "сборка запланирована там, где нечего делать"


def test_plan_schedules_a_rebuild_when_the_tag_moved(tmp_path):
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "plan", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260812", tools_built="v3.1.20260812")
    assert res.returncode == 0, res.stdout + res.stderr
    plan = res.stdout + res.stderr
    assert "would" in plan and "dkms" in plan
    assert "3.1.20260906" in plan, "дерево DKMS обязано зваться по ТЕГУ"


def _run_module_check(tmp_path, *, disk_src, loaded_src, reload_rc=0):
    """Прогоняет ensure_awg_module_loaded с подменёнными modinfo и /sys."""
    import os
    import subprocess
    script = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    body = script.split("ensure_awg_module_loaded() {", 1)[1].split("\n}\n", 1)[0]
    syssrc = tmp_path / "loaded_src"
    syssrc.write_text(loaded_src, encoding="utf-8")
    body = body.replace("/sys/module/amneziawg/srcversion", str(syssrc))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modinfo").write_text(f'#!/bin/sh\nprintf "%s\\n" "{disk_src}"\n',
                                     encoding="utf-8")
    (bin_dir / "modinfo").chmod(0o755)
    inst = tmp_path / "install"
    inst.mkdir(exist_ok=True)
    (inst / "awg-kernel-install.sh").write_text(
        f'#!/bin/bash\necho "RELOAD $1"\nexit {reload_rc}\n', encoding="utf-8")
    (tmp_path / "awg.lock").write_text("AWG_MODULE_TAG=v3.1.20260906\n", encoding="utf-8")
    prog = ('set -e\nlog(){ printf "[log] %s\\n" "$*"; }\n'
            'warn(){ printf "[warn] %s\\n" "$*"; }\n'
            f'AWG_LOCK_FILE="{tmp_path / "awg.lock"}"\nINSTALL_DIR="{tmp_path}"\n'
            'ensure_awg_module_loaded() {' + body + '\n}\n'
            'ensure_awg_module_loaded\necho ДОШЛИ\n')
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)


def test_module_is_reloaded_only_when_the_running_sources_differ(tmp_path):
    """Подмена работающего модуля гасит ВСЕ интерфейсы — на каждом обновлении
    незачем. Сравниваем srcversion, а не строку версии: у разных тегов апстрима
    она одинакова, и сравнение версий подмены бы не заметило."""
    same = _run_module_check(tmp_path, disk_src="AAA", loaded_src="AAA")
    assert same.returncode == 0 and "RELOAD" not in same.stdout, same.stdout

    diff = _run_module_check(tmp_path, disk_src="НОВЫЙ", loaded_src="СТАРЫЙ")
    assert diff.returncode == 0, diff.stdout + diff.stderr
    assert "RELOAD reload" in diff.stdout, "новые исходники не применены"
    assert "интерфейсы лягут" in diff.stdout, "молчаливый обрыв туннелей"


def test_failed_reload_does_not_break_the_update(tmp_path):
    """Не подменился модуль — обновление кода всё равно доведено: применится
    после перезагрузки, а человеку сказано как."""
    res = _run_module_check(tmp_path, disk_src="НОВЫЙ", loaded_src="СТАРЫЙ", reload_rc=1)
    assert res.returncode == 0 and "ДОШЛИ" in res.stdout, res.stdout + res.stderr
    assert "awg-bot awg reload" in res.stdout or "перезагрузк" in res.stdout


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


def test_installer_never_retargets_a_running_migration(bot_sh):
    """Обратный запрет: идущий переезд нельзя объявить целью смены поколения.
    Двойники на нём рождены под прежнее ядро, и финал записал бы хост
    перешедшим на поколение, которого он не видел."""
    body = bot_sh.split("ensure_awg_generation() {", 1)[1].split("\n}\n", 1)[0]
    branch = body.split('if [[ -n "$mig_if" ]]; then', 1)[1].split("fi", 1)[0]
    assert "awg_state_set" not in branch, "цель переезда всё-таки переписывается"
    assert "warn" in branch and "завершиться первым" in branch
    assert "return 0" in branch
