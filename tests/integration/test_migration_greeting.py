"""Переезд профилей на второй интерфейс: поздравление пользователю, когда
устройство подключилось к новому интерфейсу.
"""
import pytest

from awgbot.infra import awg as infra_awg

from tests.integration._migration_common import _twin

pytestmark = pytest.mark.integration


# ── поздравление пользователю ────────────────────────────────────────────────

def _connect(services, monkeypatch, device, ago_days=0):
    """Провести хендшейк ЧЕРЕЗ ОПРОС — так, как это происходит в бою.

    Не через прямую запись в БД: поздравление рождается на переходе «хендшейка
    не было → есть», который видит именно опрос. Подмени запись напрямую — и
    проверять будет нечего.
    """
    from awgbot.util import timeutil
    ts = int(timeutil.now().timestamp()) - int(ago_days * 86400)
    dev = services.db.get_device(device if isinstance(device, int) else device.id)
    monkeypatch.setattr(
        infra_awg, "show_dump",
        lambda iface=None: ([{"public_key": dev.public_key, "rx": 1, "tx": 1,
                              "last_handshake": ts}]
                            if iface == dev.iface or (iface == "awg0" and not dev.iface)
                            else []))
    return services.poll_traffic()


def test_user_is_greeted_the_moment_the_device_connects(services, mig, monkeypatch,
                                                        make_active_client):
    """Поздравление рождается в момент, когда опрос увидел первый хендшейк.

    Не по расписанию обхода: тот и опаздывал бы, и перепроверял бы всех впустую
    до конца переезда. Переход «было пусто → стало значение» случается один раз
    по определению, поэтому и хранить отметки не нужно.
    """
    c = make_active_client(name="c", tg_id=7070)
    dc = services.add_device(c.id, "Ноут")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    notes = _connect(services, monkeypatch, twin)
    assert len(notes) == 1 and notes[0].tg_id == 7070
    assert "Ноут" in notes[0].text and "Всё получилось" in notes[0].text

    assert _connect(services, monkeypatch, twin) == [], "поздравление повторилось"


def _watch(services, monkeypatch, device, seen=None):
    """Провести хендшейк через ЧАСТЫЙ тик — основной путь поздравления в бою.

    В seen (если передан) складываются интерфейсы, которые тик реально
    опросил: «сколько раз спросили» — половина смысла этого тика.
    """
    from awgbot.util import timeutil
    ts = int(timeutil.now().timestamp())
    dev = services.db.get_device(device if isinstance(device, int) else device.id)

    def dump(iface=None):
        if seen is not None:
            seen.append(iface)
        return ([{"public_key": dev.public_key, "rx": 1, "tx": 1, "last_handshake": ts}]
                if iface == dev.iface else [])

    monkeypatch.setattr(infra_awg, "show_dump", dump)
    return services.migration_watch()


def test_fast_tick_greets_without_waiting_for_the_traffic_poll(services, mig, monkeypatch,
                                                               make_active_client):
    """Поздравление приходит с частого тика, а не с пятиминутного опроса.

    Ядро о хендшейках не уведомляет — узнать можно только спросив, и «сразу»
    упирается в частоту вопроса. Пять минут между вопросом и ответом означали
    бы «всё получилось» человеку, который уже пять минут как в интернете.
    """
    c = make_active_client(name="c", tg_id=7080)
    dc = services.add_device(c.id, "Ноут")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    notes = _watch(services, monkeypatch, twin)
    assert len(notes) == 1 and notes[0].tg_id == 7080
    assert "Ноут" in notes[0].text


def test_only_one_claimant_wins_the_first_handshake(services, mig, make_active_client):
    """Первый хендшейк достаётся ровно одному заявителю.

    Проверка на уровне БД, а не сценария: последовательный вызов до этого места
    просто не доходит (поле уже заполнено, и опрос сворачивает раньше). Гонка
    же настоящая — за строкой следят два задания с РАЗНЫМИ соединениями
    (threading.local), и оба могут прочитать пустое поле в одну секунду. Без
    условия внутри UPDATE оба сочли бы себя первыми, и человек получил бы два
    одинаковых поздравления.
    """
    c = make_active_client(name="c", tg_id=7084)
    dc = services.add_device(c.id, "Тел")

    assert services.db.claim_first_handshake(dc.device_id, 111) is True
    assert services.db.claim_first_handshake(dc.device_id, 222) is False
    assert services.db.get_device(dc.device_id).last_handshake == 111, "перезаписали чужой"


def test_poll_does_not_greet_after_the_fast_tick_already_did(services, mig, monkeypatch,
                                                             make_active_client):
    """Поздравив с частого тика, опрос трафика не поздравляет во второй раз."""
    c = make_active_client(name="c", tg_id=7081)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    assert len(_watch(services, monkeypatch, twin)) == 1
    assert _connect(services, monkeypatch, twin) == [], "поздравление повторилось"
    assert _watch(services, monkeypatch, twin) == []


def test_fast_tick_asks_nothing_when_everyone_has_moved(services, mig, monkeypatch,
                                                        make_active_client):
    """Переехали все — тик не делает ни одного exec.

    Это нормальное состояние почти всего окна переезда: спрашивать про тех, у
    кого хендшейк уже записан, незачем. Иначе частый такт превратился бы в
    постоянные перепроверки, от которых мы и уходили.
    """
    c = make_active_client(name="c", tg_id=7082)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    seen: list = []
    _watch(services, monkeypatch, twin, seen)
    assert seen, "первый раз спросить было обязано — двойник ещё не подключался"
    seen.clear()
    assert _watch(services, monkeypatch, twin, seen) == []
    assert seen == [], "спросили про того, кто уже переехал"


def test_fast_tick_is_idle_outside_the_migration_window(services, mig, monkeypatch,
                                                        make_active_client):
    """Вне окна переезда задание висит, но не работает: ни exec, ни поздравлений.
    Поэтому его и не приходится регистрировать по флагу."""
    c = make_active_client(name="c", tg_id=7083)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.migration_cancel()

    seen: list = []
    assert _watch(services, monkeypatch, twin, seen) == []
    assert seen == []


def test_greeting_is_silent_in_quiet_hours(services, mig, monkeypatch,
                                           make_active_client):
    """Не помечено громким — значит в тихие часы уйдёт беззвучно. Событие
    приятное, но реакции не требует: будить ради него незачем."""
    c = make_active_client(name="c", tg_id=7071)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    notes = _connect(services, monkeypatch, _twin(services, dc.device_id))
    assert notes[0].force_sound is False


def test_greeting_goes_to_the_friend_not_the_owner(services, mig, monkeypatch,
                                                   make_active_client):
    """Для расшаренного устройства адресат — ДРУГ: конфиг в приложении у него,
    и просьба удалить старый профиль осмысленна только для него."""
    c = make_active_client(name="Хозяин", tg_id=7072)
    dc = services.add_device(c.id, "Общее")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=7099)
    services.migration_start()

    notes = _connect(services, monkeypatch, _twin(services, dc.device_id))
    assert len(notes) == 1 and notes[0].tg_id == 7099, "поздравили не того"


def test_old_device_connecting_is_not_greeted(services, mig, monkeypatch,
                                              make_active_client):
    """Подключение на СТАРОМ пире — не переезд. Человек просто пользуется тем,
    что у него было, и поздравлять его не с чем."""
    c = make_active_client(name="c", tg_id=7073)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    assert _connect(services, monkeypatch, dc.device_id) == []


def test_no_greeting_when_migration_is_not_running(services, mig, monkeypatch,
                                                   make_active_client):
    """Отмена прошла — поздравлять не с чем: выдаются снова старые конфиги, и
    двойник, на котором кто-то подключился позже, это не переезд."""
    c = make_active_client(name="c", tg_id=7074)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.migration_cancel()
    assert _connect(services, monkeypatch, twin) == []
