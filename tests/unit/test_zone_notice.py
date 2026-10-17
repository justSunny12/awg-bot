"""Уведомление на первом старте после обновления, снявшего ключ timezone
(runtime/main._announce_zone): одно сообщение админу по метке шага
обновления; метка снимается только тогда, когда сообщение ушло."""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.runtime import main

pytestmark = pytest.mark.unit


@pytest.fixture()
def sent(monkeypatch):
    out: list = []

    async def notify_one(bot, chat_id, text, **kw):
        out.append((chat_id, text))
        return object() if out[-1] else None
    monkeypatch.setattr(main, "notify_one", notify_one)
    return out


def _marker(tmp_path, prev="Europe/Moscow"):
    p = tmp_path / main.ZONE_ADOPTED_MARKER
    p.write_text(prev + "\n", encoding="utf-8")
    return p


async def test_once_then_the_marker_is_gone(tmp_path, sent):
    m = _marker(tmp_path)
    await main._announce_zone(None, str(tmp_path))
    assert len(sent) == 1 and sent[0][0] == config.ADMIN_ID
    assert sent[0][1].startswith("🕐 Время теперь показываю в поясе сервера: Asia/Yekaterinburg (UTC+5), "
                                 "раньше был Europe/Moscow (UTC+3)\n")
    assert not sent[0][1].endswith("."), "уведомление — без точки в конце"
    assert not m.exists()
    await main._announce_zone(None, str(tmp_path))
    assert len(sent) == 1, "второй старт молчит"


@pytest.mark.parametrize("prev", ["Asia/Yekaterinburg", "Etc/GMT-5"])
async def test_zone_not_actually_changed_no_message(tmp_path, sent, prev):
    """Ключ совпадал с поясом хоста — по имени или по правилам (синоним, тот
    же сдвиг без переходов): говорить нечего, метка снимается молча."""
    m = _marker(tmp_path, prev)
    await main._announce_zone(None, str(tmp_path))
    assert sent == [] and not m.exists()


async def test_unknown_previous_value_no_previous_part(tmp_path, sent):
    _marker(tmp_path, "Europe/Moskva")
    await main._announce_zone(None, str(tmp_path))
    assert len(sent) == 1 and "раньше" not in sent[0][1]


async def test_not_delivered_the_marker_stays(tmp_path, monkeypatch):
    async def notify_one(*a, **k):
        return None
    monkeypatch.setattr(main, "notify_one", notify_one)
    m = _marker(tmp_path)
    await main._announce_zone(None, str(tmp_path))
    assert m.exists(), "не ушло — скажем на следующем старте"


async def test_key_put_back_by_hand_nothing_to_say(tmp_path, sent, monkeypatch):
    monkeypatch.setattr(config, "TZ_SOURCE", "conf")
    m = _marker(tmp_path)
    await main._announce_zone(None, str(tmp_path))
    assert sent == [] and not m.exists()


async def test_no_marker_no_message(tmp_path, sent):
    await main._announce_zone(None, str(tmp_path))
    assert sent == []


def test_gateway_says_its_own_host():
    from awgbot.bot import roles
    from awgbot.bot.texts.zone import zone_adopted
    text = zone_adopted(roles.GATEWAY, "Europe/Moscow")
    assert text.startswith("🕐 Время теперь показываю в поясе шлюза: ") and " на шлюзе и " in text
    assert "«🔁 Перезапуск бота»" in text, "кнопка — как называется"
