"""awg-bot.sh: раздел параметров AmneziaWG в app.yaml — под именем awg (с 3.4.1).

Выпуски до 3.4.1 держали эти параметры в разделе, названном по снятому
контейнерному режиму, с ключами runtime/container/app_container/awg_dir. Новый
бот читает только `awg:` с protocol_id/interface/conf_dir: не переименуй
обновление раздел на живой установке — бот не стартует, а protocol_id, зашитый
во все выданные ссылки, пропадёт из конфига. adopt_awg_section гоняется bash'ем
из настоящего скрипта на временном app.yaml."""
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


def _run(script: str, tmp_path: Path, app_text: str | None = None, arg: str = "") \
        -> tuple[str, str]:
    conf = tmp_path / "conf"
    conf.mkdir(exist_ok=True)
    if app_text is not None:
        (conf / "app.yaml").write_text(app_text, encoding="utf-8")
    prog = "\n".join([
        "set -euo pipefail",                     # как в настоящем скрипте
        'log() { echo "LOG $*"; }',
        f'CONF_DIR="{conf}"; DATA_DIR="{tmp_path / "data"}"',
        _func(script, "adopt_awg_section"),
        f"adopt_awg_section {arg}".rstrip(),
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
    """Свежая установка 3.4.1 (шаблон уже с awg:) — шаг файл не трогает."""
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


def test_log_line_only_without_quiet(script, tmp_path):
    """Обновление пишет строку о переименовании; восстановление копии (quiet)
    — нет: там это не новость для человека, а деталь замены conf."""
    _, out = _run(script, tmp_path, _OLD_APP)
    assert "LOG app.yaml: раздел параметров AmneziaWG переименован в awg" in out, out
    quiet_dir = tmp_path / "quiet"
    quiet_dir.mkdir()
    text, out_q = _run(script, quiet_dir, _OLD_APP, "quiet")
    assert out_q == "", out_q
    assert "awg" in yaml.safe_load(text), "quiet глушит только строку, не само переименование"


def test_update_renames_before_seeding_conf(script):
    """Переименование — до seed_conf и validate_config: проверка конфига на
    прежнем имени раздела уронила бы вторую половину обновления."""
    body = _func(script, "update_env_steps")
    assert re.search(r"^\s*adopt_awg_section\b", body, re.M), "обновление не зовёт adopt_awg_section"
    assert body.index("adopt_awg_section") < body.index("seed_conf") < body.index("validate_config")


def test_restore_renames_after_the_conf_swap(script):
    """Копия выпуска 3.4.0 кладёт app.yaml с прежним разделом — после замены
    conf его переименовывают тихо, следом за поясом."""
    body = _func(script, "restore_apply")
    m = re.search(r"^\s*adopt_awg_section quiet\b", body, re.M)
    assert m, "восстановление не зовёт adopt_awg_section quiet"
    assert body.index("adopt_timezone quiet") < m.start(), \
        "раздел переименовывается до шага пояса"
    assert body.index("restore_swap") < m.start() < body.index("restore_finish"), \
        "переименование — после замены conf и до точки невозврата"


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
