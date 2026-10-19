"""
После восстановления — ожидание факта: шлюз вышел на связь (hello слота) —
одно уведомление без звука; не вышел за 15 минут — одно уведомление на слот,
свой текст для пустого и непустого сервера, дальше ожидание снято. Снятый
после бэкапа слот не ждём.
"""
from __future__ import annotations

import pytest

from awgbot.util import timeutil

pytestmark = pytest.mark.integration

REPORT = {"empty": False, "slots": [
    {"slot": 1, "case": "same", "name": "NASPi", "label": "", "link_if": "awglink"},
    {"slot": 2, "case": "rekeyed", "name": "Pi4", "label": "дача", "link_if": "awglink2"},
    {"slot": 3, "case": "removed", "name": "Pi3", "label": "", "link_if": "awglink3"}]}


def _late(services, minutes: int) -> None:
    import json
    w = json.loads(services.db.get_state("restore_wait"))
    w["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))
    services.db.set_state("restore_wait", json.dumps(w))


def test_hello_after_a_restore_is_reported_once_without_sound(services):
    services.restore_wait_start(REPORT)
    notes = services.restore_wait_hello(2)
    assert len(notes) == 1 and notes[0].silent
    assert notes[0].text == "🟢 «Pi4» (дача) на связи после восстановления"
    assert services.restore_wait_hello(2) == [], "второе «на связи»"
    assert services.restore_wait_hello(3) == [], "снятый после бэкапа слот ждали"


def test_after_fifteen_minutes_one_notice_per_silent_slot(services):
    services.restore_wait_start(REPORT)
    services.restore_wait_hello(1)
    _late(services, 14)
    assert services.restore_wait_tick() == [], "срок ещё не вышел"
    _late(services, 16)
    notes = services.restore_wait_tick()
    assert [n.text for n in notes] == [
        "⚠️ «Pi4» (дача) не вышел на связь за 15 минут после восстановления — связь с ним не менялась, "
        "загляни в «🩺 Здоровье» в чате бота шлюза"]
    assert notes[0].silent and services.restore_wait_tick() == []
    assert services.restore_wait_hello(2) == [], "ожидание не снято"


def test_on_an_empty_server_the_notice_names_the_reconnect_path(services):
    """Пустой сервер: ключи линка на шлюзе могут быть новее бэкапа — путь
    один, «🔁 Настроить заново», без файлов и команд."""
    services.restore_wait_start({**REPORT, "empty": True})
    _late(services, 16)
    texts = [n.text for n in services.restore_wait_tick()]
    assert texts[1] == ("⚠️ «Pi4» (дача) не вышел на связь за 15 минут после восстановления. Если на шлюзе ключи "
                        "линка новее бэкапа — подключи его заново: карточка шлюза → «✏️ Изменить» → «🔁 Заменить» "
                        "→ «🔁 Настроить заново»")
    assert all("gw-bundle" not in t and "awg-gw-bundle" not in t for t in texts), texts


def test_nothing_to_wait_for_when_no_slot_got_the_copy(services):
    services.restore_wait_start({"slots": [{"slot": 3, "case": "removed", "name": "Pi3"}]})
    assert not services.db.get_state("restore_wait") and services.restore_wait_tick() == []
