"""
Проверки routing-gw-setup.sh — обвяза шлюза (малины).

Шлюз стоит в чужом доме, за NAT, и попасть на него сложнее, чем на ВПС. Его
отказы видны только со стороны сервера и выглядят одинаково — «интернета за
шлюзом нет», — поэтому цена молчаливой поломки здесь особенно высока.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_ip_forward_is_set_explicitly(script):
    """Без форвардинга правила NAT стоят и не работают — без единой ошибки.

    Раньше это держалось побочным эффектом docker: он выставляет ip_forward при
    старте. То есть шлюз работал на удаче — не запустился docker или его убрали,
    и он молча переставал быть шлюзом.
    """
    assert "net.ipv4.ip_forward" in script
    assert "sysctl -w net.ipv4.ip_forward=1" in script


def test_ip_forward_survives_reboot(script):
    """Значение sysctl живёт до перезагрузки.

    Вернувшийся из ребута шлюз выглядел бы исправным и не пропускал ни пакета.
    """
    assert "/etc/sysctl.d/" in script
    apply_part = script.split('step "1a.', 1)[1]
    assert "SYSCTL_CONF" in apply_part


def test_rollback_removes_the_sysctl_drop_in(script):
    rollback = script.split('MODE" = "rollback"', 1)[1].split("exit 0", 1)[0]
    assert "SYSCTL_CONF" in rollback


def test_unit_does_not_depend_on_docker(script):
    """Линк поднимает хостовой awg-quick, а не утилиты из образа Amnezia.

    С Requires=docker.service не стартовавший или снесённый docker уносил за
    собой весь обвяз шлюза — молча, и обнаруживалось это со стороны ВПС как
    «интернета за шлюзом нет».
    """
    unit = script.split("cat > \"$UNIT\"", 1)[1].split("UNITEOF", 2)[1]
    # только директивы: в комментариях docker поминается намеренно — там
    # объясняется, почему зависимости больше нет
    directives = [ln for ln in unit.splitlines()
                  if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("docker" in ln for ln in directives), \
        "юнит шлюза не должен зависеть от docker"
    assert any("network-online.target" in ln for ln in directives)


def test_link_subnet_is_masqueraded_too(script):
    """Зонд живости идёт с адреса линка, а не клиента.

    Без маскарада линк-подсети его пакеты уходят наружу немаскараженными и не
    возвращаются — бот считает исправный шлюз непроходимым.
    """
    assert "LINK_CIDR" in script
    assert '-s $LINK_CIDR -o $WAN_IF -j MASQUERADE' in script


# ── скрипт не должен умирать молча ───────────────────────────────────────────

def test_container_detection_always_returns_zero(script):
    """Функция обязана завершаться успехом, даже не найдя контейнер.

    Без явного `return 0` она отдаёт статус последней команды цикла — неудачного
    `docker exec` на последнем контейнере. Присваивание из подстановки получает
    ненулевой статус, и при set -e скрипт умирает МОЛЧА, не дойдя даже до
    сообщения об ошибке. Ровно так он и «отработал», не поставив ни правила.
    """
    fn = script.split("detect_container() {", 1)[1].split("\n}", 1)[0]
    assert fn.rstrip().endswith("return 0"), "detect_container может вернуть ненулевой статус"


def test_missing_container_is_not_an_error(script):
    """Контейнер шлюзу больше не нужен: линк поднимают хостовые утилиты.

    Требование его наличия делало скрипт неработоспособным ровно там, куда мы и
    идём — на шлюзе без Amnezia.
    """
    assert "не нашёл контейнер с awg" not in script


def test_container_commands_are_guarded(script):
    """`docker exec` зовётся только когда контейнер найден."""
    for i, line in enumerate(script.splitlines()):
        if "docker exec $CONTAINER" in line:
            preceding = "\n".join(script.splitlines()[max(0, i - 3):i])
            assert '[ -n "$CONTAINER" ]' in preceding, \
                f"незащищённый docker exec в строке {i + 1}"


def test_same_source_and_destination_do_not_abort(script):
    """Повторный прогон «поверх» уже установленного конфига — обычное дело.

    `install` с совпадающими путями падает с «are the same file» и при set -e
    уносит весь остальной обвяз, который как раз и надо доставить.
    """
    assert "readlink -f" in script
    assert "конфиг уже на месте" in script


def test_unit_points_at_a_permanent_path(script):
    """ExecStart обязан ссылаться на постоянный путь, а не на «откуда запустили».

    Бандл шлюза распаковывается во временный каталог, а systemd-tmpfiles
    вычищает его через десять дней. Пока в юнит уходил `readlink -f "$0"`,
    автозапуск линка умирал молча: интерфейс уже стоял, RemainAfterExit держал
    юнит «активным», и отказ всплывал только при первой перезагрузке — как «за
    шлюзом нет интернета», без связи с каким-либо действием. Шлюз стоит в чужом
    доме за NAT, и разбираться с этим приходится вслепую.
    """
    assert 'SELF="$(install_self)"' in script
    assert 'SELF="$(readlink -f "$0")"' not in script, "вернулся путь запуска"
    assert "/usr/local/sbin" in script


def test_install_self_is_idempotent_and_quiet_in_place(script):
    """Запуск уже из постоянного места не копирует сам в себя: `install a a`
    затёр бы файл, который в этот момент исполняется. И путь обязан уходить в
    stdout ОДИН — его подхватывает подстановка, любая лишняя строка уехала бы
    в ExecStart."""
    body = script.split("install_self() {", 1)[1].split("\n}", 1)[0]
    assert '[ "$_src" != "$_dst" ]' in body, "нет защиты от копирования в себя"
    assert body.count("printf '%s' \"$_dst\"") == 1
    assert ">&2" in body, "сообщение об установке уйдёт в stdout вместе с путём"
