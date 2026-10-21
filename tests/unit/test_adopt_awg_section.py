"""awg-bot.sh: раздел параметров AmneziaWG в app.yaml — под именем awg (с 4.0.0).

Выпуски до 4.0.0 держали эти параметры в разделе, названном по снятому
контейнерному режиму, с ключами runtime/container/app_container/awg_dir. Новый
бот читает только `awg:` с protocol_id/interface/conf_dir: не переименуй
обновление раздел на живой установке — бот не стартует, а protocol_id, зашитый
во все выданные ссылки, пропадёт из конфига. Обратно: откат кода ниже 4.0.0
возвращает разделу прежнее имя и ключи (adopt_awg_section_revert), иначе не
стартует уже возвращённый бот. Функции гоняются bash'ем из настоящего скрипта
на временном app.yaml."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.botscript import flat

pytestmark = pytest.mark.unit

TEMPLATE = Path(__file__).resolve().parents[2] / "conf" / "app.yaml"


@pytest.fixture(scope="module")
def script() -> str:
    return flat()


def _func(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}$", text, re.S | re.M)
    assert m, f"в awg-bot.sh нет функции {name}()"
    return m.group(0)


# app.yaml выпуска 3.4.0: шапка, раздел параметров AmneziaWG дословно из
# шаблона того выпуска (с комментариями), соседние разделы с парой ключей
_HEAD = ('# conf/app.yaml — инфраструктура приложения (не бизнес-правила).\n'
         '# timezone: "Europe/Moscow"\n'
         '\n')
_OLD_SECTION = """\
# Docker / AmneziaWG — deploy-параметры
docker:
  # host — awg установлен на самой машине (так ставит установщик с v2.10.0);
  # docker — команды идут через `docker exec` в контейнер Amnezia (рудимент,
  # для установок прежней схемы). Работают оба, дефолт — host.
  # Переезд с docker на host — install/awg-host-migrate.sh.
  runtime: "host"                # "docker" — только для установок при контейнере Amnezia (рудимент)
  container: "amnezia-awg2"
  # Идентификатор протокола для приложения AmneziaVPN: уезжает в vpn:// и
  # разбирается на телефоне. Совпадает с container исторически, но живёт своей
  # жизнью — после переезда на хост docker-имя исчезнет, а это значение зашито
  # во ВСЕ выданные ссылки. Менять только вместе с перевыпуском профилей всем.
  app_container: "amnezia-awg2"
  interface: "awg0"
  awg_dir: "/etc/amnezia/amneziawg"   # host: каталог awg-quick; у старых докерных установок /opt/amnezia/awg

  # ── Переезд профилей (docs/ROADMAP.md, п.3) ────────────────────────────────
  # Второй интерфейс с новыми параметрами, поднятый рядом со старым на время
  # переезда: пока люди по одному переносят конфиги, работают оба.
  #
  # ПУСТО ⇒ переезда нет вовсе: рычаг в обслуживании не показывается, строк
  # прогресса нет, бот ходит на один интерфейс, как ходил. Это же и аварийный
  # рубильник — очистил ключи, перезапустил бота.
  #
  # Само наличие ключей переезд НЕ начинает: они лишь делают рычаг доступным.
  # Обязательны ОБА: интерфейс без подсети проверять нечем, подсеть без
  # интерфейса не к чему привязать.
  #
  # Своя подсеть у второго интерфейса не для красоты: комплекты пиров тогда не
  # пересекаются, аллокатор нового конфига видит только свои адреса, и забота
  # «двойной комплект должен помещаться в /24» исчезает.
  migration_interface: ""          # напр. "awg1"
  migration_subnet_prefix: ""      # напр. "10.9.1" → подсеть 10.9.1.0/24
  # DNS двойников в окне переезда: адрес резолвера на новом интерфейсе
  # (<новая подсеть>.1). Пишет бот при включении рычага, финал переносит в
  # client_config.dns1/dns2 и очищает. Пусто — двойники берут dns1/dns2.
  migration_dns: ""
"""
_TAIL = ('\n'
         'network:\n'
         '  subnet_prefix: "10.8.1"\n'
         '  server_host: "198.51.100.7"           # внешний адрес awg-сервера\n'
         '  server_port: 51820\n'
         '\n'
         'routing:\n'
         '  enabled: false\n'
         '  gw_interface: ""                 # интерфейс линк-туннеля до шлюза (напр. "awggw")\n')
_OLD_APP = _HEAD + _OLD_SECTION + _TAIL


def _run(script: str, tmp_path: Path, app_text: str | None = None, *,
         funcs=("adopt_awg_section",), call: str = "adopt_awg_section",
         pre: str = "") -> tuple[str, str]:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if app_text is not None:
        (conf / "app.yaml").write_text(app_text, encoding="utf-8")
    prog = "\n".join([
        "set -euo pipefail",                     # как в настоящем скрипте
        'log() { echo "LOG $*"; }',
        f'CONF_DIR="{conf}"; DATA_DIR="{tmp_path / "data"}"; INSTALL_DIR="{tmp_path / "opt"}"',
        pre,
        *(_func(script, f) for f in funcs),
        call,
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return (conf / "app.yaml").read_text(encoding="utf-8"), r.stdout


def test_old_section_is_renamed_with_values_and_neighbours_intact(script, tmp_path):
    """app.yaml 3.4.0 после шага читается новым ботом: раздел awg, прежние
    значения под новыми ключами — иначе бот не стартует после обновления, а
    protocol_id (он во всех выданных ссылках) молча сменится на умолчание."""
    text, _ = _run(script, tmp_path, _OLD_APP)
    assert re.search(r"^awg:$", text, re.M), text
    assert not re.search(r"^docker:", text, re.M), "раздел под прежним именем остался"
    assert not re.search(r"^\s*(runtime|container|app_container|awg_dir):", text, re.M), \
        "ключи снятого режима или прежние имена остались в файле"
    assert "docker" not in text and "Docker" not in text, \
        "комментарии прежнего шаблона про контейнерный режим остались:\n" + text
    assert text.startswith(_HEAD) and text.endswith(_TAIL), \
        "соседние разделы и шапка должны остаться побайтово прежними"
    loaded = yaml.safe_load(text)
    awg = loaded["awg"]
    assert awg["protocol_id"] == "amnezia-awg2", "идентификатор протокола из выданных ссылок потерян"
    assert awg["interface"] == "awg0"
    assert awg["conf_dir"] == "/etc/amnezia/amneziawg"
    assert awg["migration_interface"] == "" and awg["migration_subnet_prefix"] == "" \
        and awg["migration_dns"] == "", awg
    assert set(awg) == {"protocol_id", "interface", "conf_dir", "migration_interface",
                        "migration_subnet_prefix", "migration_dns"}, awg
    assert loaded["network"]["server_host"] == "198.51.100.7"
    assert loaded["routing"] == {"enabled": False, "gw_interface": ""}


def test_filled_values_survive_the_rename(script, tmp_path):
    """Живая установка с заполненными ключами (свой каталог, идёт переезд
    профилей) — значения переносятся как есть, а не берутся из шаблона."""
    app = (_OLD_APP
           .replace('app_container: "amnezia-awg2"', 'app_container: "DUMMY-proto"')
           .replace('awg_dir: "/etc/amnezia/amneziawg"', 'awg_dir: "/opt/amnezia/awg"')
           .replace('migration_interface: ""', 'migration_interface: "awg1"')
           .replace('migration_dns: ""', 'migration_dns: "203.0.113.1"'))
    text, _ = _run(script, tmp_path, app)
    awg = yaml.safe_load(text)["awg"]
    assert awg["protocol_id"] == "DUMMY-proto" and awg["conf_dir"] == "/opt/amnezia/awg", awg
    assert awg["migration_interface"] == "awg1" and awg["migration_dns"] == "203.0.113.1", awg


def test_second_run_changes_nothing(script, tmp_path):
    """Обновление повторяют (прервалось, позвали снова) — второй проход не
    должен ни трогать файл, ни писать строку в журнал."""
    first, _ = _run(script, tmp_path, _OLD_APP)
    app = tmp_path / "conf" / "app.yaml"
    st = os.stat(app)
    second, out = _run(script, tmp_path)
    assert second == first, "повторный проход изменил уже переименованный файл"
    assert os.stat(app).st_mtime_ns == st.st_mtime_ns, "повторный проход переписал файл"
    assert out == "", out


def test_current_template_is_left_byte_for_byte(script, tmp_path):
    """Свежая установка 4.0.0 (шаблон уже с awg:) — шаг файл не трогает."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    text, out = _run(script, tmp_path, tpl)
    assert text == tpl and out == ""


def test_missing_file_is_not_an_error(script, tmp_path):
    """Нет app.yaml (не установлено) — шаг молча выходит, обновление не валится."""
    text_path = tmp_path / "conf" / "app.yaml"
    conf = tmp_path / "conf"
    conf.mkdir()
    prog = "\n".join(["set -euo pipefail", 'log() { echo "LOG $*"; }', f'CONF_DIR="{conf}"',
                      _func(script, "adopt_awg_section"), "adopt_awg_section"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout == "", r.stderr
    assert not text_path.exists()


def test_update_renames_before_seeding_conf(script):
    """Переименование — до seed_conf и validate_config: проверка конфига на
    прежнем имени раздела уронила бы вторую половину обновления."""
    body = _func(script, "update_env_steps")
    assert re.search(r"^\s*adopt_awg_section\b", body, re.M), "обновление не зовёт adopt_awg_section"
    assert body.index("adopt_awg_section") < body.index("seed_conf") < body.index("validate_config")


def test_template_has_only_the_awg_section():
    """Шаблон поставки — сразу с новым именем: иначе свежая установка получит
    раздел, который новый бот не читает."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"^docker:", tpl, re.M), "в шаблоне остался раздел docker:"
    for key in ("runtime", "container", "app_container", "awg_dir"):
        assert not re.search(rf"^\s*{key}:", tpl, re.M), f"в шаблоне остался ключ {key}"
    awg = yaml.safe_load(tpl)["awg"]
    for key in ("protocol_id", "interface", "conf_dir"):
        assert key in awg, f"в разделе awg шаблона нет ключа {key}"


def test_update_writes_the_log_line_on_rename(script, tmp_path):
    """Переименование на обновлении — всегда строкой в журнал: по ней видно,
    что конфиг тронут, когда разбирают, почему поменялся app.yaml."""
    _, out = _run(script, tmp_path, _OLD_APP)
    assert "LOG app.yaml: раздел параметров AmneziaWG переименован в awg" in out, out


def test_restore_does_not_rename_the_section(script):
    """Восстановление копии ниже версии совместимости 4.0.0 невозможно —
    файл копии всегда уже с разделом awg; шаг в восстановлении был бы лишним
    проходом по чужому app.yaml, и прежний вызов `quiet` у функции больше
    не понимается."""
    body = _func(script, "restore_apply")
    assert not re.search(r"\badopt_awg_section", body), \
        "восстановление снова зовёт переименование раздела"


# ── обратная сторона: откат кода ниже 4.0.0 ──────────────────────────────────

_REVERT = ("adopt_awg_section_revert",)


def _below(script: str) -> str:
    """Строка AWG_SECTION_BELOW=… из настоящего скрипта (переменная вне функций)."""
    m = re.search(r"^AWG_SECTION_BELOW=.*$", script, re.M)
    assert m, "в awg-bot.sh нет AWG_SECTION_BELOW"
    return m.group(0)


def _revert(script: str, tmp_path: Path, app_text: str | None = None) -> tuple[str, str]:
    return _run(script, tmp_path, app_text, funcs=_REVERT, call="adopt_awg_section_revert",
                pre=_below(script))


def _as_340(loaded: dict) -> dict:
    """Значения так, как их прочёл бы config.py выпуска 3.4.0 (раздел
    docker:, умолчания того выпуска) — тот код и вернётся откатом."""
    d = loaded.get("docker", {}) or {}
    container = d.get("container", "amnezia-awg2")
    return {
        "runtime": str(d.get("runtime") or "host").strip().lower(),
        "app_container": d.get("app_container") or container,
        "interface": d.get("interface", "awg0"),
        "awg_dir": d.get("awg_dir", "/etc/amnezia/amneziawg"),
        "migration_interface": d.get("migration_interface", ""),
        "migration_subnet_prefix": d.get("migration_subnet_prefix", ""),
        "migration_dns": d.get("migration_dns", "") or "",
    }


def _custom_template() -> str:
    """Шаблон поставки 4.0.0 с нестандартными значениями — живая установка
    со своим идентификатором протокола, каталогом и идущим переездом."""
    tpl = TEMPLATE.read_text(encoding="utf-8")
    out = (tpl.replace('protocol_id: "amnezia-awg2"', 'protocol_id: "proto-x"')
              .replace('  interface: "awg0"', '  interface: "awg7"')
              .replace('conf_dir: "/etc/amnezia/amneziawg"   # каталог awg-quick',
                       'conf_dir: "/srv/awg"   # каталог awg-quick')
              .replace('migration_interface: ""', 'migration_interface: "awg8"')
              .replace('migration_subnet_prefix: ""', 'migration_subnet_prefix: "10.9.1"')
              .replace('migration_dns: ""', 'migration_dns: "10.9.1.1"'))
    awg = yaml.safe_load(out)["awg"]
    assert awg == {"protocol_id": "proto-x", "interface": "awg7", "conf_dir": "/srv/awg",
                   "migration_interface": "awg8", "migration_subnet_prefix": "10.9.1",
                   "migration_dns": "10.9.1.1"}, f"шаблон поменялся, подмена значений не легла: {awg}"
    return out


def _section(text: str, name: str) -> tuple[int, int]:
    """Границы раздела верхнего уровня: от строки `name:` до следующего
    ключа верхнего уровня."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == f"{name}:")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^[A-Za-z_]", lines[i])), len(lines))
    return start, end


def test_revert_gives_the_old_code_its_section_with_the_same_values(script, tmp_path):
    """Возвращённый откатом бот 3.4.0 ищет раздел docker: с app_container и
    awg_dir — без обратного переименования он стартует на умолчаниях: чужой
    идентификатор протокола в новых ссылках, не тот каталог конфигов, переезд
    профилей молча пропал."""
    src = _custom_template()
    text, out = _revert(script, tmp_path, src)
    loaded = yaml.safe_load(text)
    assert "awg" not in loaded and "docker" in loaded, sorted(loaded)
    assert _as_340(loaded) == {
        "runtime": "host", "app_container": "proto-x", "interface": "awg7", "awg_dir": "/srv/awg",
        "migration_interface": "awg8", "migration_subnet_prefix": "10.9.1", "migration_dns": "10.9.1.1",
    }, "код 3.4.0 прочёл бы из раздела не те значения"
    assert not re.search(r"^\s*(protocol_id|conf_dir):", text, re.M), \
        "новые имена ключей остались — 3.4.0 их не читает"
    # всё вне раздела — побайтово прежнее
    s0, e0 = _section(src, "awg")
    s1, e1 = _section(text, "docker")
    a, b = src.splitlines(keepends=True), text.splitlines(keepends=True)
    assert a[:s0] == b[:s1], "шапка файла до раздела изменилась"
    assert a[e0:] == b[e1:], "соседние разделы после раздела изменились"
    assert "LOG app.yaml: раздел параметров AmneziaWG — снова под прежним именем" in out, out


def test_runtime_host_appears_exactly_once_inside_the_old_section(script, tmp_path):
    """install_unit выпуска 3.4.0 берёт runtime первым попавшимся ключом
    файла и, не увидев host, вписывает в юнит Requires=docker.service — на
    хосте без докера вернувшийся бот не поднимется вовсе."""
    text, _ = _revert(script, tmp_path, _custom_template())
    hits = [ln for ln in text.splitlines() if re.match(r"^\s*runtime:", ln)]
    assert hits == ['  runtime: "host"'], f"строка runtime должна быть одна и такая: {hits}"
    s, e = _section(text, "docker")
    assert '  runtime: "host"\n' in "".join(text.splitlines(keepends=True)[s:e]), \
        "runtime стоит не в разделе docker:"


def test_old_install_unit_reads_runtime_host_after_revert(script, tmp_path):
    """Тем же чтением, что у install_unit 3.4.0 (его yaml_get из выпуска):
    после отката он видит host и зависимость от докера в юнит не ставит."""
    r = subprocess.run(["git", "show", "v3.4.0:awg-bot.sh"], capture_output=True, text=True,
                       cwd=TEMPLATE.parents[1])
    if r.returncode != 0:
        pytest.skip("нет тега v3.4.0 в checkout")
    old = r.stdout
    unit = _func(old, "install_unit")
    assert '"$(yaml_get "$CONF_DIR/app.yaml" runtime)" != "host"' in unit, \
        "install_unit 3.4.0 читается не так, как предполагает тест"
    text, _ = _revert(script, tmp_path, _custom_template())
    prog = "\n".join([_func(old, "yaml_get"), f'yaml_get "{tmp_path / "conf" / "app.yaml"}" runtime'])
    got = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                         env={"PATH": "/usr/bin:/bin"})
    assert got.stdout == "host", f"install_unit 3.4.0 увидел бы runtime={got.stdout!r} — юнит с докером"


def test_forward_then_back_returns_every_value_the_old_code_reads(script, tmp_path):
    """Обновление 3.4.0 → 4.0.0 переименовало раздел, затем откат: код 3.4.0
    должен прочесть ровно то, что читал до обновления, — иначе откат меняет
    идентификатор протокола в ссылках или каталог конфигов."""
    app = (_OLD_APP
           .replace('app_container: "amnezia-awg2"', 'app_container: "DUMMY-proto"')
           .replace('awg_dir: "/etc/amnezia/amneziawg"', 'awg_dir: "/opt/amnezia/awg"')
           .replace('migration_interface: ""', 'migration_interface: "awg1"')
           .replace('migration_subnet_prefix: ""', 'migration_subnet_prefix: "10.9.1"'))
    before = yaml.safe_load(app)
    _run(script, tmp_path, app)
    text, _ = _revert(script, tmp_path)
    after = yaml.safe_load(text)
    assert _as_340(after) == _as_340(before), "после круга код 3.4.0 читает другие значения"
    assert after["network"] == before["network"] and after["routing"] == before["routing"]
    assert text.startswith(_HEAD) and text.endswith(_TAIL), "шапка и соседние разделы изменились"


def test_second_revert_changes_nothing(script, tmp_path):
    """Откат повторяют (сторож после ловушки, откат набора после сторожа) —
    второй проход не трогает файл и не пишет в журнал."""
    first, _ = _revert(script, tmp_path, _custom_template())
    app = tmp_path / "conf" / "app.yaml"
    st = os.stat(app)
    second, out = _revert(script, tmp_path)
    assert second == first, "повторный откат изменил файл"
    assert second.count("runtime:") == 1, "повторный откат добавил второй runtime"
    assert os.stat(app).st_mtime_ns == st.st_mtime_ns, "повторный откат переписал файл"
    assert out == "", out


def test_revert_leaves_a_file_without_the_new_section_alone(script, tmp_path):
    """Файл прежней раскладки (откат из первой половины обновления, раздел
    ещё не переименован) — откат его не трогает и не падает."""
    text, out = _revert(script, tmp_path, _OLD_APP)
    assert text == _OLD_APP and out == ""


def test_revert_without_a_file_is_not_an_error(script, tmp_path):
    """Нет app.yaml — откат кода не должен валиться на этом шаге."""
    conf = tmp_path / "conf"
    conf.mkdir()
    prog = "\n".join(["set -euo pipefail", 'log() { echo "LOG $*"; }', f'CONF_DIR="{conf}"',
                      _below(script), _func(script, "adopt_awg_section_revert"), "adopt_awg_section_revert"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=20,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout == "", r.stderr
    assert not (conf / "app.yaml").exists()


def test_revert_keeps_the_gateway_conf_dir(script, tmp_path):
    """На шлюзе раздел gateway: тоже может нести свой conf_dir (каталог
    конфигов линка). Откат переименовывает ключи только раздела параметров
    AmneziaWG: иначе вернувшийся агент 3.4.0 потеряет свой каталог и пойдёт
    по умолчанию."""
    src = _custom_template() + ('\nrole: "gateway"\n'
                                'gateway:\n'
                                '  link_interface: "awglink"\n'
                                '  conf_dir: "/srv/gw-awg"\n')
    text, _ = _revert(script, tmp_path, src)
    loaded = yaml.safe_load(text)
    assert loaded["gateway"] == {"link_interface": "awglink", "conf_dir": "/srv/gw-awg"}, \
        f"раздел gateway изменён откатом: {loaded['gateway']}"


# ── страж по версии возвращённого кода ───────────────────────────────────────

_GUARD = ("tree_version", "version_lt", "adopt_awg_section_revert", "adopt_awg_section_for_old")


@pytest.mark.parametrize("ver, reverted", [
    ("3.4.0", True), ("3.4.0.5", True), ("3.3.1", True),
    ("4.0.0", False), ("4.0.0.1", False), ("4.1.0", False), (None, False)])
def test_guard_reverts_only_under_code_below_400(script, tmp_path, ver, reverted):
    """Откат вернул код ниже 4.0.0 — раздел обратно под прежнее имя; код
    4.0.0 и новее (откат 4.1 → 4.0) раздел awg читает сам, и переименование
    его бы сломало. Версия не прочитана — не трогаем: гадать опаснее."""
    opt = tmp_path / "opt" / "awgbot"
    opt.mkdir(parents=True)
    if ver is not None:
        (opt / "__version__.py").write_text(f'__version__ = "{ver}"\n', encoding="utf-8")
    src = _custom_template()
    text, _ = _run(script, tmp_path, src, funcs=_GUARD, call="adopt_awg_section_for_old",
                   pre=_below(script))
    if reverted:
        assert re.search(r"^docker:$", text, re.M) and not re.search(r"^awg:$", text, re.M), \
            f"код {ver} вернулся, а раздел остался под новым именем"
    else:
        assert text == src, f"код {ver} читает раздел awg, а его переименовали"


def test_rollback_of_code_brings_the_section_back(script, tmp_path):
    """restore_prev_code с прежним кодом 3.4.0: после переноса кода раздел
    app.yaml — уже в раскладке 3.4.0, к старту сервиса."""
    inst = tmp_path / "opt"
    inst.mkdir()
    (inst / "awg-bot.sh").write_text("new", encoding="utf-8")
    (inst / "awgbot").mkdir()
    (inst / "awgbot" / "__version__.py").write_text('__version__ = "4.0.0"\n', encoding="utf-8")
    prev = inst / ".prev-code"
    (prev / "awgbot").mkdir(parents=True)
    (prev / "awg-bot.sh").write_text("old", encoding="utf-8")
    (prev / "awgbot" / "__main__.py").write_text("# old\n", encoding="utf-8")
    (prev / "awgbot" / "__version__.py").write_text('__version__ = "3.4.0"\n', encoding="utf-8")
    text, _ = _run(script, tmp_path, _custom_template(),
                   funcs=_GUARD + ("restore_prev_code",),
                   pre=_below(script) + '\nwarn() { echo "WARN $*"; }; gw_plumbing_sync() { return 2; }'
                       '\nreserve_gw_drop_for_old() { :; }',            # линк к резерву — test_reserve_uninstall
                   call=f'restore_prev_code "{prev}"')
    assert (inst / "awg-bot.sh").read_text(encoding="utf-8") == "old", "прежний код не вернулся"
    assert _as_340(yaml.safe_load(text))["app_container"] == "proto-x", \
        "после отката кода раздел не в раскладке 3.4.0"


def test_restore_prev_code_adapts_the_section_after_moving_the_code(script):
    """Страж смотрит версию уже возвращённого дерева: до переноса кода он
    прочёл бы новую версию и ничего не сделал. До gw_plumbing_sync — чтобы
    отказ копии обвязки не оставил раздел в чужой раскладке."""
    body = _func(script, "restore_prev_code")
    m = re.search(r"^\s*adopt_awg_section_for_old\b", body, re.M)
    assert m, "restore_prev_code не зовёт adopt_awg_section_for_old"
    assert body.index('rm -rf "$prev"') < m.start() < body.index("gw_plumbing_sync"), \
        "страж — после переноса прежнего кода и до копии обвязки"
    assert body.index('mv "$prev"/*') < m.start()


@pytest.mark.parametrize("path", ["post_update_rescue", "update_guard_rollback",
                                  "cmd_update_revert", "restore_code_back"])
def test_every_rollback_goes_through_the_single_point(script, path):
    """Все откаты кода возвращают раздел только через restore_prev_code:
    прямой вызов в одном из путей означал бы двойной проход или забытый
    путь при следующей правке."""
    body = _func(script, path)
    assert "restore_prev_code" in body, f"{path} откатывает код не через restore_prev_code"
    assert not re.search(r"adopt_awg_section_(revert|for_old)", body), \
        f"{path} зовёт обратное переименование напрямую"
