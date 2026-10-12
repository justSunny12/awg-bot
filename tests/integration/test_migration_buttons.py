"""Переезд профилей на второй интерфейс: кнопки раздела переезда —
«Начать переезд» только спрашивает, устаревшие кнопки отказывают, каждое
событие оставляет запись в чате.
"""
import pytest

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


async def test_stale_finish_button_is_refused(services, mig, make_active_client,
                                              fake_bot):
    """«finish!» из старого сообщения в истории чата, нажатый после отмены, снёс
    бы старые пиры орфанов и заархивировал ровно то, что отмена сохранила."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage

    c = make_active_client(name="c", tg_id=7053)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.migration_cancel()                          # орфан остался жить

    cb = FakeCallback(message=FakeMessage(chat_id=1, user_id=1, bot=fake_bot),
                      user_id=1, bot=fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)
    assert services.db.get_device(dc.device_id) is not None, \
        "устаревшая кнопка выполнила завершение"
    assert twin.public_key in mig.peers_by_iface["awg1"]


def _press(services, key, fake_bot):
    """Нажать кнопку раздела переезда. Возвращает (cb, message)."""
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot.handlers import settings as sh
    from tests.conftest import FakeCallback, FakeMessage
    msg = FakeMessage(chat_id=1, user_id=1, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=1, bot=fake_bot)
    return sh.migration_action(cb, SetCB(sec="mig", act="do", key=key), services), cb, msg


async def test_start_only_asks_and_does_not_change_issue_yet(services, mig, fake_bot,
                                                             make_active_client):
    """Нажатие «Начать переезд» само переезд НЕ начинает.

    Старт обратим, но с его секунды меняется ВЫДАЧА: каждый следующий
    попросивший конфиг получает новый. Промахнуться в такое мимо соседней
    кнопки быть не должно, поэтому спрашиваем — как у обоих выходов.
    """
    c = make_active_client(name="c", tg_id=7090)
    services.add_device(c.id, "Тел")

    coro, cb, msg = _press(services, "start", fake_bot)
    await coro
    assert services.migration_running() is False, "переезд начался без подтверждения"
    shown = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert shown and shown[0].startswith("🚚 Начать переезд?"), shown


async def test_start_confirmation_names_the_cohort_before_it_is_frozen(
        services, mig, fake_bot, make_active_client):
    """В подтверждении назван знаменатель, который заморозит старт.

    Считаем его тем же способом, что и сам старт: увидеть состав когорты
    впервые уже в прогрессе — значит узнать постфактум, за кого мы взялись.
    """
    c = make_active_client(name="c", tg_id=7091)
    d1 = services.add_device(c.id, "Живой")
    services.add_device(c.id, "Молчун")
    _seen(services, d1.device_id, ago_days=0)

    clients, devices, to_birth = services.migration_start_preview()
    assert (clients, devices, to_birth) == (1, 1, 2), "молчун попал в когорту"

    coro, cb, msg = _press(services, "start!", fake_bot)
    await coro
    p = services.migration_progress()
    assert (p.clients_total, p.devices_total) == (clients, devices)


async def test_stale_start_button_during_migration_is_refused(services, mig, fake_bot,
                                                              make_active_client):
    """«start!» со старого подтверждения, нажатый уже во время переезда.

    Зеркало сторожа у выходов: колбэк живёт в истории чата столько же, сколько
    сообщение, и пересобирать выдачу вслепую он не должен.
    """
    c = make_active_client(name="c", tg_id=7092)
    services.add_device(c.id, "Тел")
    services.migration_start()

    coro, cb, msg = _press(services, "start!", fake_bot)
    await coro
    assert cb.answers and cb.answers[-1][1] is True, "предупреждения не было"
    assert not [t for kind, t, _ in msg.sent if kind == "edit_text"]


@pytest.mark.parametrize("key, mark", [("start!", "Переезд начат"),
                                       ("cancel!", "Переезд отменён"),
                                       ("finish!", "Переезд завершён")])
async def test_each_migration_event_leaves_a_record_in_the_chat(
        services, mig, fake_bot, make_active_client, key, mark):
    """Начало, отмена и завершение остаются в чате отдельным сообщением.

    Ветка настроек живёт до следующей навигации: экран переписывается, и от
    итога не остаётся ничего — а спрашивают о нём потом, «когда начали и чем
    кончилось». Поэтому запись без кнопок (иначе в чате два живых меню), а
    раздел приходит следующим сообщением.
    """
    c = make_active_client(name="c", tg_id=7093)
    dc = services.add_device(c.id, "Тел")
    if key != "start!":
        services.migration_start()
        _seen(services, _twin(services, dc.device_id).id, ago_days=0)

    coro, cb, msg = _press(services, key, fake_bot)
    await coro

    edits = [(t, m) for kind, t, m in msg.sent if kind == "edit_text"]
    assert edits and mark in edits[-1][0], "итог не показан"
    assert edits[-1][1] is None, "на записи остались кнопки — второе живое меню"
    assert [t for kind, t, _ in msg.sent if kind == "answer"], "раздел не пришёл следом"
