"""
Проверки awg-host-migrate.sh и host-ветки harden_firewall.sh.

Скрипт переезда запускается ровно один раз, на боевом сервере, с живыми
пользователями, и в середине останавливает контейнер. Ошибка в нём стоит
простоя всем сразу, а откатывать придётся руками. Поэтому то, что ловится
чтением, ловим чтением.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATE = ROOT / "install" / "awg-host-migrate.sh"
FIREWALL = ROOT / "install" / "harden_firewall.sh"


@pytest.fixture(scope="module")
def migrate() -> str:
    return MIGRATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def firewall() -> str:
    return FIREWALL.read_text(encoding="utf-8")


# ── порядок операций ─────────────────────────────────────────────────────────

def test_bot_is_stopped_before_files_are_copied(migrate):
    """Бот пишет в awg0.conf.

    Копия, снятая под его запись, потеряла бы последнего добавленного пира — и
    человек, которому только что выдали конфиг, не подключился бы, без единой
    ошибки в логах.
    """
    stop = migrate.index("systemctl stop $BOT_SERVICE")
    copy = migrate.index("docker cp $CONTAINER")
    assert stop < copy, "копирование раньше остановки бота теряет свежих пиров"


def test_container_is_stopped_before_the_interface_goes_up(migrate):
    """Контейнер держит порт: пока он жив, хостовой awg0 на него не сядет."""
    stop = migrate.index("docker stop $CONTAINER")
    up = migrate.index("awg-quick up $AWG_IF")
    assert stop < up


def test_interface_is_verified_by_deed(migrate):
    """Код возврата awg-quick ничего не доказывает.

    При расхождении поколений он создаёт интерфейс, спотыкается на setconf и
    молча удаляет его обратно, завершаясь успешно. Здесь это означало бы, что
    сервера нет, а скрипт отрапортовал успех — при уже остановленном контейнере.
    """
    assert 'ip link show "$AWG_IF"' in migrate
    assert "--rollback" in migrate.split('ip link show "$AWG_IF"', 1)[1][:1200], \
        "при неудаче скрипт обязан назвать команду отката"


def test_empty_peer_list_is_reported(migrate):
    """Интерфейс может подняться пустым: конфиг не применился.

    Снаружи «поднят» и «поднят без пиров» неотличимы, а второе означает, что
    ни один клиент не подключится.
    """
    assert "peers" in migrate
    assert 'PEERS' in migrate


def test_rollback_restores_the_container(migrate):
    rollback = migrate.split('if [ "$MODE" = "rollback" ]', 1)[1]
    assert "awg-quick down" in rollback
    assert "docker start $CONTAINER" in rollback


# ── единственность файла конфига ─────────────────────────────────────────────

def test_awg_quick_gets_a_symlink_not_a_copy(migrate):
    """Файл должен быть один.

    Две копии разошлись бы в первый же раз, когда бот добавит пира: бот пишет в
    свой путь, а awg-quick поднимал бы старую — и новый клиент не подключился бы.
    """
    assert "ln -sfn $CONF $LINK" in migrate
    assert "cp $CONF $LINK" not in migrate


def test_container_hooks_are_disabled_not_carried_over(migrate):
    """PostUp/PostDown писались под сеть контейнера.

    На хосте те же команды тронули бы боевой NAT сервера, а MASQUERADE и FORWARD
    для клиентской подсети ставит routing-host-setup.sh. Комментируем, а не
    удаляем — чтобы было видно, что было.
    """
    assert "PostUp" in migrate and "PostDown" in migrate
    assert "ПЕРЕЕЗД НА ХОСТ" in migrate


# ── firewall ─────────────────────────────────────────────────────────────────

def test_firewall_reads_runtime_from_the_same_yaml(firewall):
    """Режим решает, что хост видит как источник туннельного трафика.

    Ошибка здесь либо запирает админа снаружи, либо открывает SSH не тем.
    """
    assert "runtime:" in firewall
    assert '"$_runtime" == "host"' in firewall


def test_firewall_uses_client_subnet_in_host_mode(firewall):
    """В host-режиме bridge-адреса между пиром и хостом больше нет.

    Оставь мы поиск docker-сети — она бы не нашлась, вайтлист остался бы без
    туннельного источника, и SSH через собственный VPN перестал бы работать
    ровно тогда, когда другого пути может не быть.
    """
    host_branch = firewall.split('"$_runtime" == "host"', 1)[1].split("else", 1)[0]
    assert "subnet_cidr" in host_branch or "subnet_prefix" in host_branch
    assert "docker inspect" not in host_branch
