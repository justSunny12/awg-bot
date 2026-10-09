"""Сервисы соседних сетей на экранах: строка «🗂 SMB: свои — N, извне — M»
(нулевая часть не выводится) и состояние записей на шлюзе строкой сразу под
ней — функциями текстов, без имени шлюза и в краевых случаях. Строка в
карточке слота основного бота во всех состояниях (в пути, доступны, отказ с
экранированной ошибкой и без неё, поломка, перевыпуск, экранированное имя,
«не найдены», связь подсетей выключена) и диалог выключения связи подсетей —
в эталоне tests/screens/admin.txt (adm.gw.card.smb.*, adm.gw.card.peers,
adm.gw.peer_ask.off); раскладка экранов агента — в tests/screens/gateway.txt.

Цена ошибки: «шлюза шлюза» и двойной пробел в строке без имени; «свои — 0»
читается как поломка там, где серверов просто нет; поломка диска на малине,
названная отказом, отправляет человека искать ошибку в записях.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e
HEAD = "🗂 SMB: извне — 1"


@pytest.mark.parametrize("error,note", [
    ("ошибка записи файла: нет места на диске", "⚠️ Шлюз: ошибка записи файла: нет места на диске"),
    ("dnsmasq: bad option", "⚠️ Шлюз не смог принять записи: dnsmasq: bad option"),
    # «ошибка записи файла» не в начале — это чужой текст, а не поломка агента
    ("rc=1: ошибка записи файла", "⚠️ Шлюз не смог принять записи: rc=1: ошибка записи файла"),
])
def test_the_breakage_branch_without_a_name_reads_whole(error, note):
    """Без имени шлюза фраза поломки целая — «Шлюз: …», без двойного пробела;
    ветку выбирает только начало текста ошибки."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": 0, "peer": 1, "state": "failed", "error": error}) == HEAD + "\n" + note


@pytest.mark.parametrize("state,note", [
    ("reissue", "⚠️ Необходим перевыпуск конфигурации шлюза"),
    ("old_agent", "⚠️ Необходимо обновить шлюз"),
    ("failed", "⚠️ Шлюз не смог принять записи"),
    ("pending", "⏳ Синхронизация с другими шлюзами…"),
])
def test_without_a_name_the_note_does_not_repeat_the_word_gateway(state, note):
    """Имени нет — фраза целая, без «шлюза шлюза» и двойного пробела."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": 0, "peer": 1, "state": state}) == HEAD + "\n" + note


@pytest.mark.parametrize("own, peer, line", [
    (1, 2, "🗂 SMB: свои — 1, извне — 2"), (1, 0, "🗂 SMB: свои — 1"), (0, 2, "🗂 SMB: извне — 2"),
    (0, 0, "🗂 SMB: не найдены"),
])
def test_smb_line_does_not_print_zeros(own, peer, line):
    """Нулевая часть не выводится, обе нулевые — «не найдены»: «свои — 0»
    читается как поломка там, где серверов просто нет."""
    from awgbot.bot.texts.routing import services_line
    assert services_line({"own": own, "peer": peer, "state": "applied"}) == (
        line + (" · 🟢 доступны" if peer else ""))


# ── агент ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("svc, line", [
    ({"own": ["x", "y"], "peer": [], "ever": True}, "🗂 SMB: свои — 2"),
    ({"own": [], "peer": ["a"], "ever": True}, "🗂 SMB: извне — 1"),
    ({"own": [], "peer": [], "ever": True}, "🗂 SMB: не найдены"),
    ({"avahi": True, "browse": False, "own": [], "peer": ["a"], "ever": True}, "🗂 SMB: извне — 1"),
], ids=["own-only", "peer-only", "none", "no-browse"])
def test_smb_line_does_not_print_zeros_on_the_agent_either(svc, line):
    """Как в карточке слота: нулевая часть не выводится, обе нулевые — «не
    найдены»; avahi-browse нет — своя подсеть не посчитана и в строку не
    идёт."""
    from awgbot.bot.texts.gateway import smb_line
    assert smb_line(svc) == line


