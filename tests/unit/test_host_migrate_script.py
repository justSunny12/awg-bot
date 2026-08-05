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


def test_our_own_failsafe_hook_is_left_alone(migrate):
    """AWGBOT_SSH в PostUp — наш fail-closed страж, а не наследие Amnezia.

    Закомментировать его значило бы снять защиту, и вдобавок бессмысленно: бот
    возвращает строку на реассерте. Скрипт обязан его узнавать.
    """
    assert "AWGBOT_SSH" in migrate
    assert "sed -i" not in migrate, "правка хуков вслепую снимает собственную защиту"


def test_foreign_hooks_stop_the_migration(migrate):
    """Чужие PostUp/PostDown не комментируются молча.

    Что они сделают на хосте — неизвестно, и среди них может оказаться то, без
    чего сервер не работает. Молча отключить их значит узнать об этом от
    пользователей.
    """
    assert "FOREIGN" in migrate
    body = migrate.split("FOREIGN=", 1)[1]
    assert 'exit 1' in body, "при чужих хуках apply обязан остановиться"


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


# ── режим показа обязан показывать правду ────────────────────────────────────

def test_hooks_are_read_from_the_container_not_the_host_copy(migrate):
    """В режиме показа копирования ещё не было — файла на хосте нет.

    Читай скрипт хостовую копию, проверка хуков в режиме показа молча
    докладывала бы «нечего переносить» — а ради неё этот режим и запускают.
    Источник один и тот же, контейнер на этом шаге ещё жив в обоих режимах.
    """
    block = migrate.split('step "3. Хуки', 1)[1].split("step ", 1)[0]
    assert "docker exec" in block, "хуки читаются мимо контейнера"
    assert 'if [ -f "$CONF" ]' not in block


# ── подъём после перезагрузки ────────────────────────────────────────────────

def test_reboot_autostart_is_ensured(migrate):
    """Контейнер поднимал docker; на хосте это делать больше некому.

    Сервер, который работает до первой перезагрузки, кладёт всех клиентов разом
    и без всякой связи с каким-либо действием — худший из возможных отказов.
    """
    assert "awg-quick@" in migrate
    block = migrate.split('step "7.', 1)[1]
    assert "systemctl enable awg-quick@$AWG_IF" in block
    assert "list-unit-files" in block, "наличие шаблона надо проверять, а не полагать"
    assert "ExecStart=/usr/bin/env awg-quick up %i" in block, \
        "при отсутствии шаблона его надо поставить, а не только предупредить"


def test_rollback_disables_the_autostart(migrate):
    """Забытый enable поднял бы awg0 после ребута параллельно контейнеру.

    Оба захотели бы один порт, и что именно поднимется — как повезёт.
    """
    rollback = migrate.split('if [ "$MODE" = "rollback" ]', 1)[1]
    assert "disable awg-quick@" in rollback


# ── админ не должен рубить сук, на котором сидит ─────────────────────────────

def test_migration_refuses_when_ssh_comes_through_the_tunnel(migrate):
    """Шаг 5 гасит контейнер — и оборвёт сессию, если она идёт через него.

    Скрипт тогда умирает на середине: контейнер остановлен, awg0 ещё не поднят,
    сервис лежит целиком. Именно так и случилось в бою.
    """
    assert "SSH_CLIENT" in migrate or "SSH_CONNECTION" in migrate
    assert "TUNNEL_SRC" in migrate
    guard = migrate.split("TUNNEL_SRC", 1)[1]
    assert "exit 1" in guard, "при туннельной сессии скрипт обязан отказаться"


def test_tunnel_guard_runs_before_anything_is_touched(migrate):
    """Проверка до первого изменения, а не после.

    Откажись скрипт после остановки бота — он оставил бы систему в состоянии
    хуже исходного, ничего не перенеся.
    """
    guard = migrate.index("TUNNEL_SRC")
    first_change = migrate.index("systemctl stop $BOT_SERVICE")
    assert guard < first_change


def test_apply_asks_for_a_multiplexer(migrate):
    """Обрыв связи на шагах 5–6 оставляет сервис лежащим.

    Прямой SSH тоже может моргнуть, а доделывать шаги некому.
    """
    assert "TMUX" in migrate and "STY" in migrate


# ── то, что переставало работать после переезда ──────────────────────────────

def test_client_port_is_opened_in_the_firewall(migrate):
    """Пока порт публиковал docker, правил в файрволе не требовалось.

    Публикация docker ставит DNAT и свои цепочки FORWARD, обходя INPUT и ufw
    целиком. На хосте awg слушает напрямую, пакет идёт в INPUT — и при политике
    DROP клиенты просто не подключаются, а причина ни на что не похожа.
    """
    assert "ListenPort" in migrate
    assert "ufw allow" in migrate


def test_stale_client_route_is_removed(migrate):
    """Маршрут через контейнер переживает переезд и перекрывает connected.

    Наружу всё уходит, ответы клиентам отправляются в мёртвую docker-сеть, а
    диагностика зелёная — маршрут ведь существует.
    """
    body = migrate.split('step "6b.', 1)[1]
    assert "ip route del" in body
    assert "$AWG_IF" in body
