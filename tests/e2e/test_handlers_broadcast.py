"""E2E: объявления админа — частичный выбор адресатов, черновик с фото и
альбомами, лимиты подписи, отчёт о доставке.

Экраны объявления (вход с главной, адресаты с частичным выбором, приглашение,
переспрос на пустое, превью одному и всем, рассылка с отчётом по формам —
профиль, профили, держатели, недоставленные — и панелью следом) — в эталоне
tests/screens/admin.txt (adm.bc.*). Черновик с картинками снимком не снять
(сообщение с фото обвязка эталонов не шлёт): его отказы и превью — здесь."""
import asyncio

import pytest

from tests.conftest import FakeState

pytestmark = pytest.mark.e2e


# ── объявление: адресаты и черновик ─────────────────────────────────────────

async def test_broadcast_keeps_telegram_formatting(services, make_active_client, fake_bot):
    """Форматирование, сделанное средствами Telegram, обязано попасть в
    черновик — из него строятся и превью, и рассылка.

    Жирный/курсив/ссылки живут не в тексте сообщения, а в entities. Пока
    читали `message.text`, объявление уходило голым — и превью тоже, поэтому
    заметить это до отправки было невозможно.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7001)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msg = FakeMessage(text="Профилактика в ночь на 12-е",
                      html_text="Профилактика <b>в ночь на 12-е</b>",
                      chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    assert (await state.get_data())["text"] == "Профилактика <b>в ночь на 12-е</b>"


def _photo(file_id):
    """PhotoSize-лесенка, как её отдаёт Telegram: последний размер — оригинал."""
    import types
    return [types.SimpleNamespace(file_id=f"{file_id}_small"),
            types.SimpleNamespace(file_id=file_id)]


async def _settled(admin_h, chat_id):
    """Дождаться отложенного рендера превью (пауза на хвост альбома)."""
    task = admin_h._bc_render_tasks.get(chat_id)
    if task:
        await task


async def test_photos_without_text_get_a_real_preview_not_a_demand(
        services, make_active_client, fake_bot, monkeypatch):
    """Картинки без текста — это уже превью, а не требование «пришли текст».

    Подпись едет на одном из апдейтов альбома, и медленный аплоад растягивает
    их на десятки секунд: требовать текст в этом зазоре — значит требовать то,
    что админ уже отправил. Превью строится сразу, блок подтверждения прямо
    говорит про пустой текст, и отправить можно как есть.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7010)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"FILE{n}")) for n in (1, 2)]
    for m in msgs:
        await admin_h.broadcast_receive(m, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "превью-альбом не построен без текста"
    assert [m.media for m in albums[0][2]] == ["FILE1", "FILE2"]
    said = [t for m in msgs for kind, t, _ in m.sent if kind == "answer"]
    confirm = [t for t in said if t.startswith("👆 <b>Так увидят получатели</b>")]
    assert len(confirm) == 1, said
    assert "✍️ Текста нет — уйдут только картинки" in confirm[0], "блок молчит про пустой текст"
    assert not any("Пришли текст" in t for t in said), \
        "второй шаг вернулся: бот требует текст, который мог ещё не доехать"


async def test_one_batch_renders_the_preview_exactly_once(
        services, make_active_client, fake_bot, monkeypatch):
    """Пачка апдейтов одного альбома — РОВНО ОДИН рендер превью.

    Ранняя отмена ожидающего рендера (в начале обработки апдейта) с
    конкурентной пачкой не справляется: второй апдейт выполняет её раньше, чем
    первый создал таску, — отменять нечего, выживают обе, и превью видимо
    пересоздаётся по разу на апдейт. Единственность живой таски обязана давать
    атомарная пара cancel+create в конце обработки: в каком бы порядке ни
    финишировали апдейты, остаётся таска последнего.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0.2)
    renders = []
    real_preview = admin_h._bc_preview

    async def counting_preview(message, state, services_):
        renders.append(1)
        await real_preview(message, state, services_)

    monkeypatch.setattr(admin_h, "_bc_preview", counting_preview)
    c = make_active_client(name="Ксюша", tg_id=7021)
    state = FakeState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"B{n}"), caption="текст" if n == 0 else None)
            for n in range(3)]
    await asyncio.gather(*(admin_h.broadcast_receive(m, state, services)
                           for m in msgs))
    await asyncio.sleep(0.5)          # дать пережившим таскам отработать

    assert len(renders) == 1, f"превью рендерилось {len(renders)} раз(а) на одну пачку"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "альбом-превью пересоздавался"


async def test_late_caption_joins_the_preview(services, make_active_client,
                                              fake_bot, monkeypatch):
    """Подпись со снимка, пришедшего после превью, вливается пересборкой.

    Страховочный путь: в норме альбом приезжает пачкой (клиент шлёт его одним
    запросом после загрузки всех файлов), но если апдейт добрался позже
    рендера — превью-огрызок заменяется полным без единого действия админа.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7017)
    state = FakeState()
    await state.update_data(targets=[c.id])

    first = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo("SLOW1"))
    await admin_h.broadcast_receive(first, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)
    old_ids = (await state.get_data())["preview_ids"]
    assert old_ids, "превью без текста не показано"

    late = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                       photo=_photo("SLOW2"), caption="Важно! Обновление")
    await admin_h.broadcast_receive(late, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert set(old_ids) <= set(deleted), "старое превью-огрызок остался в чате"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    media = albums[-1][2]
    assert [m.media for m in media] == ["SLOW1", "SLOW2"]
    assert media[0].caption == "Важно! Обновление"
    data = await state.get_data()
    assert data["text"] == "Важно! Обновление"


async def test_broadcast_sends_photos_without_text(services, make_active_client,
                                                   fake_bot):
    """Объявление из одних картинок отправляется: превью прямо спрашивало про
    пустой текст, и «Отправить» — легитимный ответ на этот вопрос."""
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7018)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["A", "B"])

    cb = FakeCallback(message=FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                                          bot=fake_bot),
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_send(cb, state, services)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert albums and albums[-1][1] == 7018, "объявление без текста не ушло"
    assert albums[-1][2][0].caption is None, "пустая строка уехала подписью"


async def test_every_draft_prompt_offers_a_way_out(services, make_active_client,
                                                   fake_bot):
    """Из любой отбивки черновика есть выход кнопкой — тупиков не бывает.

    «Отмена» текстом бот понять не обязан (и не пытается: слово ушло бы в
    рассылку), значит кнопка обязана быть на каждом сообщении, где диалог
    чего-то ждёт: и на отказе по длине (черновик с картинками — снимком его
    не снять), и на «жду текст или картинку» (снимок adm.bc.blank).
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7019)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["A"])

    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    over = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                       bot=fake_bot)
    await admin_h.broadcast_receive(over, state, services)
    markups = [mk for kind, t, mk in over.sent if kind == "answer"]
    assert markups and markups[-1] is not None, "отказ по длине — тупик без кнопки"


async def test_broadcast_album_with_caption_is_one_action(
        services, make_active_client, fake_bot, monkeypatch):
    """Снимки + текст, набранный в окне вложений, — готовое превью без
    дополнительных шагов. Подпись приезжает на ОДНОМ из сообщений альбома, и
    черновик обязан подхватить её с любого."""
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7013)
    state = FakeState()
    await state.update_data(targets=[c.id])

    first = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo("A"), caption="Переезд начался")
    second = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                         photo=_photo("B"))
    await admin_h.broadcast_receive(first, state, services)
    await admin_h.broadcast_receive(second, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    assert len(albums) == 1, "превью-альбом не отправлен или отправлен дважды"
    media = albums[0][2]
    assert [m.media for m in media] == ["A", "B"]
    assert media[0].caption == "Переезд начался"
    confirms = [t for m in (first, second) for kind, t, mk in m.sent
                if kind == "answer" and mk is not None]
    assert confirms, "нет блока подтверждения"   # его шапка — снимок adm.bc.preview.one
    ids = (await state.get_data())["preview_ids"]
    assert len(ids) == 3, "в preview_ids не альбом плюс блок подтверждения"


async def test_broadcast_photos_after_text_rebuild_the_preview(
        services, make_active_client, fake_bot, monkeypatch):
    """Обратный порядок — текст, потом картинки — тоже одно объявление.

    Прежнее превью при этом убирается: иначе в чате остались бы два живых блока
    подтверждения и запись, неотличимая от разосланной.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7014)
    state = FakeState()
    await state.update_data(targets=[c.id])

    txt = FakeMessage(text="Переезд начался", chat_id=cfg.ADMIN_ID,
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(txt, state, services)
    old_ids = (await state.get_data())["preview_ids"]
    assert old_ids, "текстовое превью не показано"

    pic = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                      photo=_photo("A"))
    await admin_h.broadcast_receive(pic, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)

    deleted = [r[2] for r in fake_bot.records if r[0] == "delete_message"]
    assert set(old_ids) <= set(deleted), "старое превью осталось в чате"
    albums = [r for r in fake_bot.records if r[0] == "send_media_group"]
    photos_sent = [r for r in fake_bot.records if r[0] == "send_photo"]
    assert albums or photos_sent, "превью не пересобрано с картинкой"


async def test_broadcast_refuses_caption_over_limit_and_keeps_the_draft(
        services, make_active_client, fake_bot, monkeypatch):
    """С картинками лимит 1024: текст едет подписью, а длинная подпись — это
    привилегия Premium-аккаунта, которым бот быть не может.

    Отказ обязан сохранять черновик: заставить пересылать десять картинок
    заново из-за одного лишнего абзаца — худший из возможных ответов.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7011)
    state = FakeState()
    await state.update_data(targets=[c.id], photos=["FILE1"])

    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    msg = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                      bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    said = " ".join(t for _, t, _ in msg.sent)
    assert str(cfg.TG_CAPTION_MAX) in said and str(cfg.TG_CAPTION_MAX + 1) in said, \
        "не названы ни лимит, ни фактическая длина"
    assert "Так увидят получатели" not in said, "показано превью сверх лимита"
    assert (await state.get_data())["photos"] == ["FILE1"], "черновик потерян"

    # тот же текст БЕЗ картинок в лимит укладывается — лимита два, и они разные
    state2 = FakeState()
    await state2.update_data(targets=[c.id])
    msg2 = FakeMessage(text=long_text, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                       bot=fake_bot)
    await admin_h.broadcast_receive(msg2, state2, services)
    assert any(t.startswith("👆 <b>Так увидят получатели</b>") for _, t, _ in msg2.sent)


async def test_broadcast_send_revalidates_the_limit(services, make_active_client,
                                                    fake_bot):
    """Кнопка «Отправить» перепроверяет лимит по итоговому черновику.

    Картинка, добавленная после законного длинного текста, меняет лимит задним
    числом. Уйди такое в Telegram — каждый получатель вернул бы Bad Request, а
    отчёт записал бы всех в «заблокировали бота».
    """
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7015)
    state = FakeState()
    long_text = "я" * (cfg.TG_CAPTION_MAX + 1)
    await state.update_data(targets=[c.id], photos=["FILE1"],
                            text=long_text, text_len=len(long_text))

    cb = FakeCallback(message=FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID,
                                          bot=fake_bot),
                      user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_send(cb, state, services)
    assert cb.answers and cb.answers[-1][1] is True, "отправка не остановлена"
    assert not [r for r in fake_bot.records if r[0] == "send_media_group"], \
        "объявление ушло сверх лимита"


async def test_broadcast_concurrent_album_updates_lose_nothing(
        services, make_active_client, fake_bot, monkeypatch):
    """Апдейты альбома aiogram обрабатывает ПАРАЛЛЕЛЬНО (handle_as_tasks).

    Без замка два конкурентных read-modify-write по FSM читают одинаковый
    список, и один снимок молча затирает другой — альбом уходит неполным.

    Хранилище здесь НАРОЧНО уступает петлю на каждом вызове, как это делает
    любое сетевое (Redis). На MemoryStorage чтение и запись стоят вплотную без
    точки переключения, и гонка не складывается СЛУЧАЙНО — замок делает
    целостность свойством кода, а не удачным свойством хранилища.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    class NetworkishState(FakeState):
        async def get_data(self):
            await asyncio.sleep(0)
            return await super().get_data()

        async def update_data(self, **kw):
            await asyncio.sleep(0)
            await super().update_data(**kw)

    async def direct_call(fn, *a, **k):
        return fn(*a, **k)          # без to_thread: детерминированный интерливинг

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    monkeypatch.setattr(admin_h, "call", direct_call)
    c = make_active_client(name="Ксюша", tg_id=7016)
    state = NetworkishState()
    await state.update_data(targets=[c.id])

    msgs = [FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                        photo=_photo(f"F{n}")) for n in range(4)]
    await asyncio.gather(*(admin_h.broadcast_receive(m, state, services)
                           for m in msgs))
    await _settled(admin_h, cfg.ADMIN_ID)
    assert sorted((await state.get_data())["photos"]) == ["F0", "F1", "F2", "F3"], \
        "конкурентные апдейты потеряли снимок"


async def test_broadcast_stops_at_the_album_limit(services, make_active_client,
                                                  fake_bot, monkeypatch):
    """Одиннадцатая картинка не принимается: Telegram не берёт в альбом больше
    десяти. Отбиваем на приёме, а не на отправке — иначе объявление упало бы
    целиком, после набранного текста."""
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7012)
    state = FakeState()
    await state.update_data(targets=[c.id],
                            photos=[f"F{i}" for i in range(cfg.TG_ALBUM_MAX)])

    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot,
                      photo=_photo("EXTRA"))
    await admin_h.broadcast_receive(msg, state, services)
    await _settled(admin_h, cfg.ADMIN_ID)
    assert any("лишние не приняты" in t for _, t, _ in msg.sent), msg.sent
    assert "EXTRA" not in (await state.get_data())["photos"]


async def test_announcement_is_one_message_with_caption_on_the_first_photo(fake_bot):
    """Объявление с картинками — ОДНО сообщение: альбом, подпись на первом
    вложении. Подпись на втором Telegram показал бы отдельным блоком, а текст
    отдельным сообщением дал бы в чате две записи вместо одной."""
    from awgbot.bot.notifier import send_announcement

    await send_announcement(fake_bot, 555, "текст объявления", ["A", "B", "C"])
    kind, chat, media = [r for r in fake_bot.records if r[0] == "send_media_group"][0]
    assert chat == 555 and len(media) == 3
    assert media[0].caption == "текст объявления"
    assert [m.caption for m in media[1:]] == [None, None], "подпись не только на первом"


async def test_announcement_with_one_photo_is_not_an_album(fake_bot):
    """Альбом из одного вложения Telegram не принимает — шлём обычное фото."""
    from awgbot.bot.notifier import send_announcement

    await send_announcement(fake_bot, 555, "текст", ["ONLY"])
    assert not [r for r in fake_bot.records if r[0] == "send_media_group"]
    kind, chat, caption, photo = [r for r in fake_bot.records if r[0] == "send_photo"][0]
    assert caption == "текст" and photo == "ONLY"


async def test_broadcast_draft_chain_is_cleaned_on_cancel(services, make_active_client,
                                                          fake_bot):
    """Отмена обязана стереть всю переписку с набором черновика.

    Навигация при переходе на превью лишь СНИМАЕТ кнопки с прежнего экрана
    (_dismiss_previous_nav), не удаляя его. Поэтому без явного трекинга после
    отмены висели и приглашение «пришли объявление», и само сообщение админа с
    текстом — то есть черновик оставался в чате.
    """
    from tests.conftest import FakeMessage
    from awgbot.bot.handlers.admin import broadcast as admin_h
    import awgbot.core.config as cfg

    c = make_active_client(name="Ксюша", tg_id=7010)
    chat = cfg.ADMIN_ID
    services.db.set_nav_message_id(chat, 555)          # «приглашение» — нав-экран

    state = FakeState()
    await state.update_data(targets=[c.id])
    msg = FakeMessage(text="Профилактика", chat_id=chat, user_id=chat, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)

    tracked = services.db.pop_content_msg_ids(chat)
    assert msg.message_id in tracked, "сообщение админа осталось бы висеть"
    assert 555 in tracked, "экран-приглашение осталось бы висеть"
