"""Броадкаст: адресаты (дедуп клиент+друг, исключение админа, только активные
друзья), отчёт доставки, счётчик успех/провал."""
import pytest

from awgbot.bot import notifier


def test_recipients_dedup_and_exclude_admin(services, make_active_client):
    import awgbot.core.config as cfg
    # админ (tg == ADMIN_ID) + два клиента; служебный клиент tg=None не в счёт
    make_active_client(name="admin", tg_id=cfg.ADMIN_ID)
    make_active_client(name="c2", tg_id=2002)
    make_active_client(name="c3", tg_id=2003)
    ids = services.db.broadcast_recipients(exclude_tg_id=cfg.ADMIN_ID)
    assert cfg.ADMIN_ID not in ids            # админ исключён
    assert set(ids) == {2002, 2003}           # только реальные клиенты, без служебного


@pytest.mark.asyncio
async def test_broadcast_counts_ok_and_failed():
    sent = []

    class Bot:
        async def send_message(self, tg_id, text, **kw):
            if tg_id == 99:
                raise RuntimeError("blocked")
            sent.append(tg_id)

    ok, failed = await notifier.broadcast(Bot(), [1, 2, 99, 3], "привет")
    assert ok == 3 and failed == 1
    assert sent == [1, 2, 3]


@pytest.mark.asyncio
async def test_broadcast_empty_list():
    class Bot:
        async def send_message(self, *a, **k): raise AssertionError("не должно зваться")
    ok, failed = await notifier.broadcast(Bot(), [], "x")
    assert ok == 0 and failed == 0


@pytest.mark.asyncio
async def test_broadcast_pacing_and_order_preserved():
    """Порядок доставки сохранён, все не-нулевые адресаты обойдены."""
    seen = []

    class Bot:
        async def send_message(self, tg_id, text, **kw):
            seen.append(tg_id)

    ok, failed = await notifier.broadcast(Bot(), [5, 0, 7, None, 9], "x")
    assert seen == [5, 7, 9]          # 0/None пропущены
    assert ok == 3 and failed == 0


# ── адресаты объявления по набору профилей ───────────────────────────────────────

def test_client_recipients_include_owner_and_active_friends(services, make_active_client):
    """Друзья входят потому, что устройство у них от ЭТОГО профиля.

    Объявление вида «профиль ставится на паузу» касается их напрямую, а узнать
    иначе им неоткуда: владелец пересказывать не обязан.
    """
    import awgbot.core.config as cfg
    c = make_active_client(name="c1", tg_id=3001)
    dc = services.add_device(c.id, "Телефон")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=3099)

    ids = services.db.broadcast_recipients_for_clients([c.id], exclude_tg_id=cfg.ADMIN_ID)
    assert ids == [3001, 3099]


def test_client_recipients_exclude_other_profiles(services, make_active_client):
    """Изоляция — суть фичи: соседний профиль объявления видеть не должен."""
    import awgbot.core.config as cfg
    c1 = make_active_client(name="c1", tg_id=3001)
    make_active_client(name="c2", tg_id=3002)

    ids = services.db.broadcast_recipients_for_clients([c1.id], exclude_tg_id=cfg.ADMIN_ID)
    assert ids == [3001]


def test_pending_friend_is_not_a_recipient(services, make_active_client):
    """Приглашённый, но не подключившийся друг — не адресат: tg_id у него ещё
    нет, а если бы и был, слать некуда."""
    import awgbot.core.config as cfg
    c = make_active_client(name="c1", tg_id=3001)
    dc = services.add_device(c.id, "Телефон")
    services.make_device_friendly(dc.device_id)          # код выдан, не активирован

    ids = services.db.broadcast_recipients_for_clients([c.id], exclude_tg_id=cfg.ADMIN_ID)
    assert ids == [3001]


def test_same_person_twice_gets_one_delivery(services, make_active_client):
    """Тот же DISTINCT-инвариант, что у общей рассылки: один человек — одна
    доставка, даже если он попал в выборку с двух сторон.

    Пишем связь через db напрямую: activate_friend владельцу собственного
    устройства откажет («already_user»), а проверяем мы здесь не его гейт, а
    дедуп в UNION. Совпасть адреса могут и без этого пути — например, если
    друг позже заведёт собственный профиль на тот же Telegram.
    """
    import awgbot.core.config as cfg
    c = make_active_client(name="c1", tg_id=3001)
    dc = services.add_device(c.id, "Телефон")
    services.db.set_device_friend(dc.device_id, friend_tg_id=3001,
                                  friend_status="active")

    ids = services.db.broadcast_recipients_for_clients([c.id], exclude_tg_id=cfg.ADMIN_ID)
    assert ids == [3001], "один человек получил бы объявление дважды"


def test_recipients_union_over_several_profiles(services, make_active_client):
    """Несколько отмеченных профилей — объединение без дублей.

    Мультивыбор ради этого и сделан: один текст уходит нескольким профилям
    разом, а не N раз прогоняется весь путь ввод-превью-отправка.
    """
    import awgbot.core.config as cfg
    c1 = make_active_client(name="c1", tg_id=5001)
    c2 = make_active_client(name="c2", tg_id=5002)
    dc = services.add_device(c1.id, "Телефон")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=5099)

    ids = services.db.broadcast_recipients_for_clients([c1.id, c2.id],
                                                      exclude_tg_id=cfg.ADMIN_ID)
    assert ids == [5001, 5002, 5099]


def test_empty_selection_yields_nobody(services, make_active_client):
    """Пустой выбор — пустой список, а не «все». Ошибка в другую сторону тут
    необратима: разосланное объявление уже прочитано."""
    import awgbot.core.config as cfg
    make_active_client(name="c1", tg_id=5003)
    assert services.db.broadcast_recipients_for_clients([], exclude_tg_id=cfg.ADMIN_ID) == []


def test_friends_clause_only_when_friends_exist(services, make_active_client):
    """Про гостевой доступ упоминаем ТОЛЬКО когда друзья реально есть.

    Предупреждение на каждом объявлении перестаёт читаться ровно к тому разу,
    когда оно важно.
    """
    import awgbot.core.config as cfg
    from awgbot.bot import texts

    solo = make_active_client(name="Один", tg_id=6001)
    assert services.db.broadcast_has_friends([solo.id], cfg.ADMIN_ID) is False

    prompt = texts.broadcast_prompt(["Один"], False)
    assert "Получит: <b>Один</b> — владельцу профиля." in prompt
    assert "раздал" not in prompt

    dc = services.add_device(solo.id, "Телефон")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=6099)
    assert services.db.broadcast_has_friends([solo.id], cfg.ADMIN_ID) is True

    prompt2 = texts.broadcast_prompt(["Один"], True)
    assert "Получат: <b>Один</b> — владельцу профиля и тем, кому он раздал" in prompt2


def test_pending_friend_does_not_trigger_the_clause(services, make_active_client):
    """Приглашённый, но не подключившийся другом не считается — он и объявления
    не получит."""
    import awgbot.core.config as cfg
    c = make_active_client(name="c", tg_id=6002)
    dc = services.add_device(c.id, "Телефон")
    services.make_device_friendly(dc.device_id)      # код выдан, не активирован
    assert services.db.broadcast_has_friends([c.id], cfg.ADMIN_ID) is False


def test_audience_wording_matches_number_of_profiles():
    """Единственный профиль и несколько согласуются по-разному."""
    from awgbot.bot import texts
    one = texts.broadcast_prompt(["Ксюша"], False)
    many = texts.broadcast_prompt(["Ксюша", "Дима"], False)
    assert "владельцу профиля" in one and "владельцам этих профилей" in many
    assert "Получит:" in one and "Получат:" in many
