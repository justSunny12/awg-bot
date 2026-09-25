"""E2E: admin-хендлеры (прямой вызов с фейками) — панель, клиенты, создание,
удаление, обещание после перезапуска, окна и финишеры обновления."""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import admin as admin_h
from awgbot.bot.callbacks import ClientCB, ConfirmCB, PeriodCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(services, bot, data=""):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(data=data, message=nav, user_id=ADMIN, bot=bot), nav


# ── панель ───────────────────────────────────────────────────────────────────
async def test_admin_start_shows_panel_and_menu_opens_it_again(services, fake_bot,
                                                              make_active_client):
    """/start рисует панель и запоминает её как активное меню; кнопка «В меню»
    с любого экрана рисует ту же панель поверх текущего сообщения."""
    services.ensure_admin_client()
    make_active_client(tg_id=6000, name="Клиент")
    msg = FakeMessage(text="/start", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_h.admin_start(msg, services, FakeState())
    assert any(s[0] == "answer" for s in msg.sent)
    assert services.db.get_nav_message_id(ADMIN) is not None

    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.admin_main_menu(cb, services, FakeState())
    assert any(s[0] == "edit_text" and s[2] is not None for s in nav.sent)


# ── список / карточка клиента ────────────────────────────────────────────────
async def test_clients_list_with_client(services, make_active_client, fake_bot):
    make_active_client(name="Ося", tg_id=7000)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.clients_list(cb, services)
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and "Профили" in shown[-1][1]
    labels = [b.text for row in shown[-1][2].inline_keyboard for b in row]
    assert any("Ося" in t for t in labels), "профиль должен быть кнопкой в списке"
    assert cb.answers


async def test_clients_list_hides_admin_profile(services, fake_bot, make_active_client):
    """Профиля админа в списке нет — он дублировал главную.

    Весь его функционал (свои устройства, конфиги, РФ-доступ) всегда на главной,
    а карточка была урезана до тех же кнопок: строка в списке только путала, кто
    тут кем управляет. Единственный профиль-админ ⇒ список пуст.
    """
    services.ensure_admin_client()
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.clients_list(cb, services)
    assert any("Профилей пока нет" in s[1] for s in nav.sent if s[0] == "edit_text")

    make_active_client(name="Ксюша", tg_id=4242)
    cb2, nav2 = _admin_cb(services, fake_bot)
    await admin_h.clients_list(cb2, services)
    shown = "".join(s[1] for s in nav2.sent if s[0] == "edit_text")
    assert "Профили" in shown


async def test_client_open_card(services, make_active_client, fake_bot):
    client = make_active_client(name="Ким", tg_id=7001)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_open(cb, ClientCB(action="open", client_id=client.id), services)
    text, labels = last_screen(nav)
    assert "Ким" in text, "карточка без имени профиля"
    assert any("Удалить" in l for l in labels) and any("Продлить" in l for l in labels)


# ── создание клиента (FSM: имя → лимит → трафик → период) ─────────────────────
async def test_create_client_full_fsm(services, fake_bot):
    services.ensure_admin_client()
    state = FakeState()
    m = lambda text: FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)

    await admin_h.add_client_name(m("Новичок"), services, state)
    assert (await state.get_data())["name"] == "Новичок"
    await admin_h.add_client_limit(m("3"), services, state)
    assert (await state.get_data())["limit"] == 3
    await admin_h.add_client_traffic(m("100"), services, state)
    assert (await state.get_data())["traffic_gb"] == 100

    # выбор периода → создание клиента
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, state)
    names = [c.name for c in services.db.list_clients(include_service=False)]
    assert "Новичок" in names
    # выданы приветствие + шаблон приглашения со ссылкой на бота
    assert any("t.me/test_bot" in s[1] for s in nav.sent if s[0] == "answer")


async def test_create_client_period_stale_dialog(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _admin_cb(services, fake_bot)
    # пустой state (диалог устарел) → алерт, не падаем
    await admin_h.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, FakeState())
    assert cb.answers and cb.answers[0][1] is True    # show_alert


# ── регенерация инвайта / удаление ───────────────────────────────────────────
async def test_regen_invite(services, fake_bot):
    created = services.create_client("Пенд", 1, "year")
    old = created.invite_code
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.regen_invite(cb, ClientCB(action="regen_invite", client_id=created.client_id), services)
    new_code = services.db.get_client(created.client_id).invite_code
    assert new_code != old
    assert any("t.me/test_bot" in s[1] for s in nav.sent if s[0] == "answer")


async def test_client_delete_apply(services, make_active_client, fake_bot):
    client = make_active_client(name="НаУдаление", tg_id=7002)
    services.add_device(client.id, "d")
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_delete_apply(
        cb, ConfirmCB(action="del_client", ref=client.id, yes=True), services)
    assert services.db.get_client(client.id) is None
    assert any("удал" in s[1].lower() for s in nav.sent if s[0] == "edit_text")


async def test_client_delete_cancel_shows_card(services, make_active_client, fake_bot):
    client = make_active_client(name="Остаётся", tg_id=7003)
    cb, nav = _admin_cb(services, fake_bot)
    await admin_h.client_delete_apply(
        cb, ConfirmCB(action="del_client", ref=client.id, yes=False), services)
    assert services.db.get_client(client.id) is not None   # не удалён
    assert cb.answers


def test_period_choices_has_cancel_both_contexts():
    """Баг-фикс: диалог выбора срока не тупик — есть кнопка отмены."""
    from awgbot.bot import keyboards as kb
    ext = [b.text for r in kb.period_choices("extend", ref=7).inline_keyboard for b in r]
    cre = [b.text for r in kb.period_choices("create").inline_keyboard for b in r]
    assert any("Отмена" in t for t in ext)
    assert any("Отмена" in t for t in cre)
    # extend-отмена ведёт к карточке клиента, create — в меню
    ext_cb = [b.callback_data for r in kb.period_choices("extend", ref=7).inline_keyboard
              for b in r if "Отмена" in b.text][0]
    assert ext_cb == "c:open:7"


# ── обещание вернуться после перезапуска ─────────────────────────────────────

async def test_restart_promise_is_kept_by_the_new_process(services, fake_bot):
    """«Вернётся через несколько секунд» обещает уходящий процесс, а исполняет
    новый: обещание подменяется отчётом, следом приходит панель.

    Прежде не исполнял никто — после старта в чат никто не пишет, и админ
    оставался с мёртвым сообщением без кнопок, пока сам не слал /start.
    """
    from awgbot.bot import texts
    services.set_restart_wait(ADMIN, 4242)
    await admin_h.restore_panel_after_restart(fake_bot, services)

    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert len(edits) == 1 and edits[0][1] == ADMIN
    assert edits[0][2] == texts.BOT_RESTARTED, "обещание не сменилось отчётом"

    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1, "панель не пришла отдельным сообщением"
    assert services.db.get_nav_message_id(ADMIN) != 4242, \
        "активным меню осталось отчётное сообщение — два живых меню в чате"


async def test_restart_promise_is_one_shot(services, fake_bot):
    """Флаг одноразовый: иначе каждый следующий старт переписывал бы давно
    отработавшее сообщение — в том числе спустя недели."""
    services.set_restart_wait(ADMIN, 4242)
    await admin_h.restore_panel_after_restart(fake_bot, services)
    fake_bot.records.clear()
    await admin_h.restore_panel_after_restart(fake_bot, services)
    assert fake_bot.records == []


async def test_ordinary_start_says_nothing(services, fake_bot):
    """Перезапуск не из чата (ребут хоста, падение, systemctl руками) — молчим.
    Панель без спроса была бы шумом, которого админ не заказывал."""
    await admin_h.restore_panel_after_restart(fake_bot, services)
    assert fake_bot.records == []


async def test_restart_panel_survives_an_unavailable_message(services, fake_bot, monkeypatch):
    """Сообщение удалили или оно старше суток — отчёт потерян, но панель обязана
    прийти всё равно: остаться без навигации админ не должен."""
    async def boom(*a, **k):
        raise RuntimeError("message to edit not found")
    monkeypatch.setattr(fake_bot, "edit_message_text", boom)

    services.set_restart_wait(ADMIN, 4242)
    await admin_h.restore_panel_after_restart(fake_bot, services)

    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == ADMIN
    assert services.db.get_nav_message_id(ADMIN) != 4242, "нав указывает на мёртвое сообщение"

async def test_only_the_last_update_finisher_keeps_its_menu_button(
        services, fake_bot, monkeypatch):
    """Цепочка ступеней self-update — живая кнопка «В меню» только у последнего
    финишера. У прежнего она снимается при отправке следующего; текст его при
    этом не трогается — история «какая ступень чем закончилась» остаётся.
    """
    from awgbot.runtime.main import report_update_result
    from awgbot.domain.services import Notification
    from awgbot.bot import keyboards as kb
    import awgbot.core.config as cfg

    step = {"n": 0}

    def fake_confirm():
        step["n"] += 1
        return Notification(cfg.ADMIN_ID, f"обновлён, ступень {step['n']}",
                            reply_markup=kb.update_done_menu())

    monkeypatch.setattr(services, "confirm_applied_update", fake_confirm)
    monkeypatch.setattr(services, "pop_update_wait", lambda: None)

    await report_update_result(fake_bot, services)       # ступень 1
    await report_update_result(fake_bot, services)       # ступень 2 (рестарт)
    await report_update_result(fake_bot, services)       # ступень 3

    stripped = [mid for kind, chat, mid in fake_bot.records if kind == "edit_markup"]
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 3, "финишеры не отправлены"
    # у двух прошлых кнопки сняты, у последнего — нет; история знает только его
    assert len(stripped) == 2, "снято не у всех прошлых (или у лишнего)"
    remaining = services.pop_update_reports()
    assert len(remaining) == 1 and remaining[0][1] not in stripped


async def test_admin_start_purges_menu_history(services, fake_bot):
    """/start админа удаляет все прошлые меню чата (история ведётся send_menu/edit_nav)."""
    from awgbot.bot.handlers import admin as admin_h
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    chat = cfg.ADMIN_ID
    for mid in (101, 102, 103):
        services.db.nav_touch(chat, mid)
    msg = FakeMessage(chat_id=chat, user_id=chat, bot=fake_bot)
    await admin_h.admin_start(msg, services, FakeState())
    deleted = sorted(r[2] for r in fake_bot.records if r[0] == "delete_message")
    assert deleted == [101, 102, 103]
    assert services.db.pop_nav_history(chat) != [101, 102, 103], "история не очищена"

async def test_menu_button_dismisses_every_other_update_window(services, fake_bot):
    """«В меню» на любом окне обновления снимает кнопки и у всех остальных —
    в цепочке ступеней живой должна остаться одна."""
    from awgbot.bot.handlers import admin as admin_h
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    chat = cfg.ADMIN_ID
    for mid in (501, 502, 503):
        services.remember_update_report(chat, mid)
    msg = FakeMessage(chat_id=chat, user_id=chat, bot=fake_bot, message_id=503)
    cb = FakeCallback(message=msg, user_id=chat, bot=fake_bot)
    await admin_h.update_menu(cb, services, FakeState())
    # фильтр ДО распаковки: у записей фейка разная длина
    stripped = sorted(r[2] for r in fake_bot.records if r[0] == "edit_markup")
    assert stripped == [501, 502], "остальные окна — через бота, по одному разу"
    assert ("edit_reply_markup", chat) in fake_bot.records, "текущее — своим методом"
    assert services.pop_update_reports() == [], "история не очищена"


# ── РФ-часть потребления на главной ──────

GB = 1024 ** 3


async def _panel_text(services, fake_bot) -> str:
    services.ensure_admin_client()
    msg = FakeMessage(text="/start", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_h.admin_start(msg, services, FakeState())
    return [s for s in msg.sent if s[0] == "answer"][-1][1]


def _rf_world(services, fake_routing, monkeypatch, *, enabled, rx=0, tx=0, error=""):
    """Функция развёрнута (линк в конфиге) и включена/выключена; итог месяца и
    ошибка учёта — в состоянии, как их оставил опрос."""
    monkeypatch.setattr(services, "routing_provisioned", lambda: True)
    fake_routing.enabled = enabled
    services.db.set_state("rf_month_rx", str(rx))
    services.db.set_state("rf_month_tx", str(tx))
    services.db.set_state("rf_acct_error", error)


def _rf_line(text: str):
    lines = [ln for ln in text.splitlines() if ln.startswith("└ 🇷🇺 РФ-доступ:")]
    return lines[0] if lines else None


async def test_panel_shows_zero_rf_line_when_the_feature_is_on(services, fake_bot, fake_routing,
                                                              monkeypatch):
    """Функция включена — строка всегда, и «0 ГБ» тоже: ровно тогда видно, что
    маркировка не работает, хотя люди пользуются."""
    _rf_world(services, fake_routing, monkeypatch, enabled=True)
    text = await _panel_text(services, fake_bot)
    line = _rf_line(text)
    assert line == "└ 🇷🇺 РФ-доступ: 0 ГБ (↑ 0 ГБ | ↓ 0 ГБ)", text
    head = [ln for ln in text.splitlines() if f"📊 Трафик за {texts.month_label()}" in ln][0]
    assert text.splitlines().index(line) == text.splitlines().index(head) + 1, \
        "строка РФ не сразу под потреблением"


async def test_panel_hides_rf_line_when_off_and_nothing_counted(services, fake_bot, fake_routing,
                                                               monkeypatch):
    _rf_world(services, fake_routing, monkeypatch, enabled=False)
    text = await _panel_text(services, fake_bot)
    assert _rf_line(text) is None, text


async def test_panel_keeps_rf_line_when_off_but_month_has_rf(services, fake_bot, fake_routing,
                                                            monkeypatch):
    """Выключили в середине месяца — накопленное не пропадает с главной."""
    _rf_world(services, fake_routing, monkeypatch, enabled=False, rx=GB, tx=3 * GB)
    line = _rf_line(await _panel_text(services, fake_bot))
    assert line == "└ 🇷🇺 РФ-доступ: 4 ГБ (↑ 1 ГБ | ↓ 3 ГБ)"


async def test_panel_rf_line_marks_broken_accounting_and_keeps_numbers(services, fake_bot,
                                                                      fake_routing, monkeypatch):
    _rf_world(services, fake_routing, monkeypatch, enabled=True, rx=GB, tx=GB,
              error="nft не найден — поставь пакет nftables")
    line = _rf_line(await _panel_text(services, fake_bot))
    assert line == "└ 🇷🇺 РФ-доступ: 2 ГБ (↑ 1 ГБ | ↓ 1 ГБ) · ⚠️ учёт трафика РФ-доступа не идёт"
    assert "nft" not in line, "текст ошибки ядра в шапке админа"


async def test_panel_rf_line_links_to_the_rf_screen(services, fake_bot, fake_routing, monkeypatch):
    """Этап 2: «РФ-доступ» на главной — ссылка на экран по профилям. Пропадёт
    ссылка — до разбивки РФ по людям админ из главной не доберётся."""
    services.bot_username = "awg_test_bot"
    _rf_world(services, fake_routing, monkeypatch, enabled=True, rx=GB)
    text = await _panel_text(services, fake_bot)
    lines = [ln for ln in text.splitlines() if ln.startswith("└ ") and "🇷🇺 РФ-доступ" in ln]
    # флаг — внутри ссылки: кликается вся подпись «🇷🇺 РФ-доступ»
    assert lines and lines[0] == ('└ <a href="https://t.me/awg_test_bot?start=traffic_local">'
                                  '🇷🇺 РФ-доступ</a>: 1 ГБ (↑ 1 ГБ | ↓ 0 ГБ)'), text
    assert "start=traffic\"" in text, "ссылка потребления пропала"


async def test_panel_rf_line_is_plain_text_without_bot_username(services, fake_bot, fake_routing,
                                                               monkeypatch):
    """Имя бота ещё не известно — ссылку собрать не из чего: подпись простым
    текстом, а не битая ссылка «t.me/?start=…»."""
    services.bot_username = ""
    _rf_world(services, fake_routing, monkeypatch, enabled=True, rx=GB)
    line = _rf_line(await _panel_text(services, fake_bot))
    assert line == "└ 🇷🇺 РФ-доступ: 1 ГБ (↑ 1 ГБ | ↓ 0 ГБ)", line


async def test_client_and_guest_home_do_not_change_with_rf_data(services, fake_bot, fake_routing,
                                                               make_active_client, monkeypatch):
    """РФ-потребление — сведения о человеке, только админу: главная клиента и
    гостя с накопленным РФ та же, что без него."""
    from awgbot.bot.handlers import client as client_h
    from awgbot.bot.handlers import friend as fh
    owner = make_active_client(tg_id=6100, name="Вася")
    dc = services.add_device(owner.id, "Тел")
    lent = services.add_device(owner.id, "Ноут")
    res = services.activate_friend(services.make_device_friendly(lent.device_id), tg_id=96100)
    assert res.ok, res.reason

    async def homes():
        m1 = FakeMessage(text="/start", chat_id=6100, user_id=6100, bot=fake_bot)
        await client_h.start_client(m1, services.db.get_client(owner.id), services, FakeState())
        m2 = FakeMessage(text="/start", chat_id=96100, user_id=96100, bot=fake_bot)
        await fh.friend_start(m2, res.holder, services)
        return ([s[1] for s in m1.sent if s[0] == "answer"][-1],
                [s[1] for s in m2.sent if s[0] == "answer"][-1])

    before = await homes()
    _rf_world(services, fake_routing, monkeypatch, enabled=True, rx=5 * GB, tx=7 * GB)
    services.db.rf_add_bulk([(dc.device_id, GB, 2 * GB), (lent.device_id, GB, GB)])
    after = await homes()
    assert after == before, "главная клиента или гостя изменилась от РФ-данных"
    assert all("учёт" not in t and "└ 🇷🇺" not in t for t in after)
