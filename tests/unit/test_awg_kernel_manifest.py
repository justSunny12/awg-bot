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
    # В сборке — только uninstall: дерево прежнего тега остаётся для отката.
    # remove живёт в отдельном режиме prune и зовётся, когда откат уже не нужен.
    build = kernel.split("step_module() {", 1)[1].split("\n}\n", 1)[0]
    assert "dkms uninstall" in build and "dkms remove" not in build, \
        "remove в сборке снёс бы дерево прежней версии — откат станет невозможен"


def test_prune_refuses_until_the_new_module_actually_runs(tmp_path):
    """Убирать прежнюю сборку можно, только когда откат к ней уже не нужен —
    то есть когда работает именно то, что собрано. Иначе снесём единственный
    путь назад ровно перед тем, как он понадобится."""
    ver = "3.1.20260812"
    res = _run_kernel(tmp_path, "prune", module=ver, loaded=ver, tools=ver,
                      built_tag="v3.1.20260906", tools_built="v3.1.20260812",
                      src_disk="НОВЫЙ", src_loaded="СТАРЫЙ", as_root=True)
    assert res.returncode != 0
    assert "reload" in res.stderr


def test_prune_removes_every_other_dkms_version(tmp_path):
    """Сюда попадает и апстримное «1.0.0» с хостов, собранных руками."""
    import os
    ver = "3.1.20260812"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    (bin_dir / "dkms").write_text(
        '#!/bin/sh\n'
        f'echo "dkms $*" >> "{calls}"\n'
        'if [ "$1" = status ]; then printf "amneziawg/1.0.0, 5.15.0, x86_64: installed\\n'
        'amneziawg/3.1.20260906, 5.15.0, x86_64: installed\\n"; fi\n', encoding="utf-8")
    (bin_dir / "depmod").write_text('#!/bin/sh\necho "depmod $*" >> "' + str(calls) + '"\n',
                                    encoding="utf-8")
    for f in ("dkms", "depmod"):
        (bin_dir / f).chmod(0o755)
    src_root = tmp_path / "usr_src"
    (src_root / "amneziawg-1.0.0").mkdir(parents=True)
    (src_root / "amneziawg-3.1.20260906").mkdir(parents=True)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    os.environ["DKMS_SRC_ROOT"] = str(src_root)
    try:
        res = _run_kernel(tmp_path, "prune", module=ver, loaded=ver, tools=ver,
                          built_tag="v3.1.20260906", tools_built="v3.1.20260812", as_root=True)
    finally:
        os.environ["PATH"] = old_path
        del os.environ["DKMS_SRC_ROOT"]
    assert res.returncode == 0, res.stdout + res.stderr
    log = calls.read_text(encoding="utf-8")
    assert "dkms remove -m amneziawg -v 1.0.0 --all" in log
    assert "dkms remove -m amneziawg -v 3.1.20260906" not in log, "снесли текущую сборку"
    assert not (src_root / "amneziawg-1.0.0").exists()
    assert (src_root / "amneziawg-3.1.20260906").exists()


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
                built_tag="", tools_built="", src_disk="AAA", src_loaded=None,
                as_root=False):
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
    if as_root:
        # prune/reload требуют root; в тесте подменяем проверку, а не права
        src = src.replace('[[ "$PLAN" -eq 1 || "${EUID:-$(id -u)}" -eq 0 ]] || die "нужен root"',
                          ': # root check disabled in test')
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


_BOT_HELPERS = ("awg_state_get", "awg_state_set", "yaml_get", "yaml_set_soft",
                "yaml_set", "lock_get")


def _bot_func(script: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", script, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


def _run_bot_func(tmp_path, name, *, lock_gen="2", applied="", target="",
                  app_yaml="", confs=(), init_rc=0, prune_rc=0):
    """Прогоняет функцию awg-bot.sh с настоящими хелперами (yaml_*/awg_state_*)
    и подставными скриптами install/: серверный init и уборка ядра лишь
    отмечаются в журнале. Возвращает (proc, state_text, app_yaml_text, log)."""
    import os
    import subprocess
    script = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    tmp_path.mkdir(parents=True, exist_ok=True)
    inst = tmp_path / "install"; inst.mkdir(exist_ok=True)
    journal = tmp_path / "journal"
    (inst / "awg-server-init.sh").write_text(
        f'#!/bin/bash\necho "INIT $AWG_IF $SUBNET_PREFIX $AWG_QUICK_DIR" >> "{journal}"\nexit {init_rc}\n',
        encoding="utf-8")
    (inst / "awg-kernel-install.sh").write_text(
        f'#!/bin/bash\necho "KERNEL $1" >> "{journal}"\nexit {prune_rc}\n', encoding="utf-8")
    conf = tmp_path / "conf"; conf.mkdir(exist_ok=True)
    conf_dir = tmp_path / "awg"; conf_dir.mkdir(exist_ok=True)
    for name_ in confs:
        (conf_dir / name_).write_text("[Interface]\nAddress = 10.9.1.1/24\n", encoding="utf-8")
    app = conf / "app.yaml"
    app.write_text(app_yaml.replace("__AWG_DIR__", str(conf_dir)), encoding="utf-8")
    lock = tmp_path / "awg.lock"; lock.write_text(f"AWG_GENERATION={lock_gen}\n", encoding="utf-8")
    state = tmp_path / "awg.state"
    lines = [f"AWG_GENERATION_APPLIED={applied}"] if applied else []
    if target:
        lines.append(f"AWG_GENERATION_TARGET={target}")
    if lines:
        state.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # BSD sed не понимает `-i -E` в том виде, как пишет скрипт под GNU
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sed").write_text(
        '#!/bin/sh\nif /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed "$@"; fi\n'
        'if [ "$1" = "-i" ] && [ "$2" = "-E" ]; then shift 2; exec /usr/bin/sed -i "" -E "$@"; fi\n'
        'exec /usr/bin/sed "$@"\n', encoding="utf-8")
    (bin_dir / "sed").chmod(0o755)
    prog = "\n".join([
        "set -e",
        'log(){ printf "[log] %s\\n" "$*"; }', 'warn(){ printf "[warn] %s\\n" "$*"; }',
        'ok(){ printf "[ok] %s\\n" "$*"; }', 'die(){ printf "[die] %s\\n" "$*"; exit 1; }',
        f'AWG_LOCK_FILE="{lock}"', f'AWG_STATE="{state}"', f'CONF_DIR="{conf}"',
        f'INSTALL_DIR="{tmp_path}"',
        *(_bot_func(script, h) for h in _BOT_HELPERS),
        _bot_func(script, name), name, "echo ДОШЛИ",
    ])
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    proc = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    read = lambda p: p.read_text(encoding="utf-8") if p.exists() else ""
    return proc, read(state), read(app), read(journal)


_APP_YAML = """docker:
  runtime: "host"
  interface: "awg0"
  awg_dir: "__AWG_DIR__"
  migration_interface: ""
  migration_subnet_prefix: ""
network:
  subnet_prefix: "10.8.1"
  ip_host_start: 1
"""


def test_missing_state_file_means_the_floor_generation(tmp_path):
    """Обновление без файла состояния приехало с домантифестной версии — хост
    на первом поколении. Поставка того же поколения: файл заведён, переезда нет;
    поставка нового поколения: заведён и переезд объявлен — усыновить её
    поколение значило бы записать хост переехавшим, не переезжая."""
    proc, state, app, journal = _run_bot_func(tmp_path, "ensure_awg_generation",
                                              lock_gen="1", app_yaml=_APP_YAML)
    assert proc.returncode == 0 and "ДОШЛИ" in proc.stdout, proc.stdout + proc.stderr
    assert "AWG_GENERATION_APPLIED=1" in state
    assert "AWG_GENERATION_TARGET" not in state and "INIT" not in journal
    assert 'migration_interface: ""' in app

    proc, state, app, journal = _run_bot_func(tmp_path / "gen2", "ensure_awg_generation",
                                              lock_gen="2", app_yaml=_APP_YAML)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "AWG_GENERATION_APPLIED=1" in state and "AWG_GENERATION_TARGET=2" in state
    assert "INIT awg1" in journal and 'migration_interface: "awg1"' in app


def test_generation_bump_raises_a_second_interface_that_does_not_collide(tmp_path):
    """Смена поколения: второй интерфейс поднимается сам, имя и подсеть
    подбираются мимо занятых — ровно то, где админ ошибётся, а отказ вылезет у
    клиентов. Пул адресов начинается с .2, цель поколения — в состоянии."""
    proc, state, app, journal = _run_bot_func(
        tmp_path, "ensure_awg_generation", lock_gen="3", applied="2",
        app_yaml=_APP_YAML, confs=("awg1.conf",))     # awg1 и 10.9.1.x заняты
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INIT awg2 10.10.1 " in journal, journal
    assert 'migration_interface: "awg2"' in app and 'migration_subnet_prefix: "10.10.1"' in app
    assert "ip_host_start: 2" in app
    assert "AWG_GENERATION_TARGET=3" in state and "AWG_GENERATION_APPLIED=2" in state


def test_generation_bump_without_free_names_warns_instead_of_dying(tmp_path):
    proc, state, app, journal = _run_bot_func(
        tmp_path, "ensure_awg_generation", lock_gen="3", applied="2",
        app_yaml=_APP_YAML, confs=tuple(f"awg{i}.conf" for i in range(1, 10)))
    assert proc.returncode == 0 and "ДОШЛИ" in proc.stdout
    assert "не подобрать имя/подсеть" in proc.stdout and "INIT" not in journal
    assert "AWG_GENERATION_TARGET" not in state


def test_failed_interface_init_leaves_no_half_state(tmp_path):
    proc, state, app, journal = _run_bot_func(
        tmp_path, "ensure_awg_generation", lock_gen="3", applied="2",
        app_yaml=_APP_YAML, init_rc=1)
    assert proc.returncode == 0 and "INIT awg1" in journal
    assert 'migration_interface: ""' in app and "AWG_GENERATION_TARGET" not in state


def test_installer_never_retargets_a_running_migration(tmp_path):
    """Обратный запрет: идущий переезд нельзя объявить целью смены поколения.
    Двойники на нём рождены под прежнее ядро, и финал записал бы хост
    перешедшим на поколение, которого он не видел."""
    running = _APP_YAML.replace('migration_interface: ""', 'migration_interface: "awg1"') \
                       .replace('migration_subnet_prefix: ""', 'migration_subnet_prefix: "10.9.1"')
    proc, state, app, journal = _run_bot_func(
        tmp_path, "ensure_awg_generation", lock_gen="3", applied="2", app_yaml=running)
    assert proc.returncode == 0 and "ДОШЛИ" in proc.stdout
    assert "завершиться первым" in proc.stdout
    assert "AWG_GENERATION_TARGET" not in state and "INIT" not in journal
    assert 'migration_interface: "awg1"' in app, "идущий переезд не тронут"


@pytest.mark.parametrize("applied, target, lock_gen, pruned", [
    ("2", "", "2", True),      # совместимая смена тега — убираем после старта
    ("2", "3", "3", False),    # переезд объявлен — старое ядро ждёт финала
    ("2", "", "3", False),     # поколение выросло, цели ещё нет — тоже ждём
])
def test_old_builds_are_pruned_only_when_no_generation_change_is_pending(
        tmp_path, applied, target, lock_gen, pruned):
    """Смена поколения: старый интерфейс ещё обслуживает людей, и путь назад
    к прежнему ядру должен оставаться до финала переезда. Совместимая смена —
    прежняя сборка убирается сразу после успешного старта на новом модуле."""
    proc, state, app, journal = _run_bot_func(
        tmp_path, "prune_old_kernel_builds", lock_gen=lock_gen, applied=applied,
        target=target, app_yaml=_APP_YAML)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("KERNEL prune" in journal) is pruned, journal + proc.stdout


def test_prune_runs_after_a_successful_start(bot_sh):
    post = bot_sh.split("cmd_post_update() {", 1)[1].split("\n}\n", 1)[0]
    assert post.index('systemctl is-active --quiet "$SERVICE"') < post.index("prune_old_kernel_builds"), \
        "уборка раньше успешного старта"


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




# ── свой DNS-резолвер в установщике ──────────────────────────────────────────

def _run_resolver_func(tmp_path, name, *, app_yaml, script_rc=0, resolver_conf_exists=False):
    """ensure_resolver / adopt_resolver с настоящими yaml_* и подставным
    install/awg-resolver-setup.sh (пишет вызов в журнал, код — script_rc)."""
    import os
    import subprocess
    script = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    tmp_path.mkdir(parents=True, exist_ok=True)
    inst = tmp_path / "install"; inst.mkdir(exist_ok=True)
    journal = tmp_path / "journal"
    (inst / "awg-resolver-setup.sh").write_text(
        f'#!/bin/bash\necho "RESOLVER $*" >> "{journal}"\nexit {script_rc}\n', encoding="utf-8")
    conf = tmp_path / "conf"; conf.mkdir(exist_ok=True)
    app = conf / "app.yaml"; app.write_text(app_yaml, encoding="utf-8")
    rconf = tmp_path / "resolver.conf"
    if resolver_conf_exists:
        rconf.write_text("bind-dynamic\nlisten-address=10.8.1.1\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sed").write_text(
        '#!/bin/sh\nif /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed "$@"; fi\n'
        'if [ "$1" = "-i" ] && [ "$2" = "-E" ]; then shift 2; exec /usr/bin/sed -i "" -E "$@"; fi\n'
        'exec /usr/bin/sed "$@"\n', encoding="utf-8")
    (bin_dir / "sed").chmod(0o755)
    body = _bot_func(script, name).replace("/etc/dnsmasq.d/awgbot-resolver.conf", str(rconf))
    prog = "\n".join([
        "set -e",
        'log(){ printf "[log] %s\\n" "$*"; }', 'warn(){ printf "[warn] %s\\n" "$*"; }',
        'ok(){ printf "[ok] %s\\n" "$*"; }', 'die(){ printf "[die] %s\\n" "$*"; exit 1; }',
        f'CONF_DIR="{conf}"', f'INSTALL_DIR="{tmp_path}"',
        _bot_func(script, "yaml_get"), _bot_func(script, "yaml_set"),
        _bot_func(script, "resolver_addr"), body, name, "echo ДОШЛИ",
    ])
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    proc = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, env=env)
    read = lambda p: p.read_text(encoding="utf-8") if p.exists() else ""
    return proc, read(app), read(journal)


_RES_YAML = """docker:
  runtime: "host"
network:
  subnet_prefix: "10.8.1"
client_config:
  dns1: "1.1.1.1"
  dns2: "1.0.0.1"
"""


def test_fresh_install_gets_its_own_resolver_in_both_dns_fields(tmp_path):
    proc, app, journal = _run_resolver_func(tmp_path, "ensure_resolver", app_yaml=_RES_YAML)
    assert proc.returncode == 0 and "ДОШЛИ" in proc.stdout, proc.stdout + proc.stderr
    assert journal.strip() == "RESOLVER install 10.8.1.1"
    assert 'dns1: "10.8.1.1"' in app and 'dns2: "10.8.1.1"' in app


def test_fresh_install_keeps_public_dns_when_the_resolver_fails(tmp_path):
    """Резолвер не поднялся — конфиги не должны указывать в пустоту: поля
    остаются публичными, установка продолжается с предупреждением."""
    proc, app, journal = _run_resolver_func(tmp_path, "ensure_resolver", app_yaml=_RES_YAML, script_rc=1)
    assert proc.returncode == 0 and "ДОШЛИ" in proc.stdout
    assert "[warn]" in proc.stdout and 'dns1: "1.1.1.1"' in app


def test_docker_mode_gets_no_resolver(tmp_path):
    proc, app, journal = _run_resolver_func(
        tmp_path, "ensure_resolver", app_yaml=_RES_YAML.replace('"host"', '"docker"'))
    assert proc.returncode == 0 and journal == "" and 'dns1: "1.1.1.1"' in app


def test_update_adopts_the_resolver_only_when_dns1_already_is_the_address(tmp_path):
    """Хост, где приватный адрес прописали руками: резолвер переходит под
    опеку бота. Публичный dns1 обновление не трогает — это решение админа."""
    proc, app, journal = _run_resolver_func(
        tmp_path / "priv", "adopt_resolver", app_yaml=_RES_YAML.replace('"1.1.1.1"', '"10.8.1.1"'))
    assert proc.returncode == 0 and journal.strip() == "RESOLVER install 10.8.1.1"

    proc, app, journal = _run_resolver_func(tmp_path / "pub", "adopt_resolver", app_yaml=_RES_YAML)
    assert proc.returncode == 0 and journal == ""

    proc, app, journal = _run_resolver_func(
        tmp_path / "done", "adopt_resolver",
        app_yaml=_RES_YAML.replace('"1.1.1.1"', '"10.8.1.1"'), resolver_conf_exists=True)
    assert proc.returncode == 0 and journal == "", "уже под опекой — повторно не ставим"
