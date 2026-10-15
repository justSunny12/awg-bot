"""gwtoken.py — «🤖 Токен бота» в «⚙️ Настройки → 🔧 Сервис»: выбор шлюза, ввод нового токена (прежнего или нового бота), отправка шлюзу каналом.

Смена аварийная (токен утёк и отозван в BotFather): бот шлюза на мёртвом
токене сам ничего не примет, поэтому кнопка — на сервере, а на шлюз токен
везёт управляющий канал (раздел token документа, агент с token1). На экранах
шлюза и в агенте кнопок нет. Регистрируется раньше общего do (actions)."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers.common import ask_here, ask_tracked, back_to_context, call, edit
from awgbot.bot.states import GatewayBotToken
from awgbot.domain.services.types import ServiceError

from ._router import router


@router.callback_query(SetCB.filter((F.sec == "gtk") & (F.act == "do")))
async def gwtoken_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    """pick — выбор шлюза (один — сразу приглашение к вводу); ask — приглашение по слоту."""
    key = callback_data.key
    if key == "pick":
        slots = await call(services.gateway_token_slots)
        if not slots:
            await cb.answer(ui.toast("Отправить токен по каналу конфигурации некому: нужен агент шлюза v3.3.0"), show_alert=True)
            return
        if len(slots) > 1:
            await edit(cb, texts.GATEWAY_TOKEN_PICK, kb.gateway_token_pick(slots))
            await cb.answer()
            return
        await _ask(cb, services, state, *slots[0])
        return
    if key == "ask":
        try:
            slot = int(callback_data.val or 0)
        except ValueError:
            slot = 0
        names = dict(await call(services.gateway_token_slots))
        if slot in names:
            await _ask(cb, services, state, slot, names[slot])
            return
    await cb.answer(ui.Toast.stale, show_alert=True)


async def _ask(cb: CallbackQuery, services, state: FSMContext, slot: int, name: str) -> None:
    await cb.answer()
    await state.set_state(GatewayBotToken.value)
    await state.update_data(gw_slot=slot, gw_name=name)
    await ask_here(cb, services, state, texts.gateway_token_prompt(name), "set_svc", 0)


@router.message(GatewayBotToken.value)
async def gwtoken_received(message: Message, state: FSMContext, services):
    """Новый токен: сообщение сразу прочь (секрет), getMe новым токеном с ВПС
    (токен живой), бот не сервера и не другого шлюза — запись в env ВПС;
    шлюзу — каналом документом, сейчас или при подключении. Бот новый —
    напомнить нажать у него «Старт». Ошибка — переспрос, ввод остаётся."""
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:                                     # noqa: BLE001
        pass
    data = await state.get_data()
    slot = int(data.get("gw_slot") or 0)
    try:
        bot_id, username = await _getme(token)
        check = await call(services.gateway_token_check, slot, token, bot_id, username)
        if await state.get_state() != GatewayBotToken.value.state:
            return                                        # «✖️ Отмена», пока ждали Telegram, — не записываем
        await call(services.gateway_token_replace, slot, token)
    except ServiceError as e:
        await ask_tracked(message, services, texts.gateway_token_refused(str(e)))
        return
    await state.clear()
    from awgbot.runtime import gwbotme, linkserver
    await gwbotme.refresh(services, slot)
    srv = linkserver.current()
    online = bool(srv is not None and srv.online(slot))
    if srv is not None:
        srv.deliver_soon()                                # документ с новым токеном — без ожидания такта
    note = texts.GATEWAY_TOKEN_SENT if online else texts.GATEWAY_TOKEN_QUEUED
    if check.get("new_bot"):
        note += "\n" + texts.gateway_token_new_bot(check.get("username") or "")
    await back_to_context(message, services, data, "admin", note=note)


async def _getme(token: str) -> tuple[int, str]:
    """getMe новым токеном с ВПС: (id, username). Не похоже на токен, Telegram
    не принял или не ответил — ServiceError."""
    import re
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", token):
        raise ServiceError("это не похоже на токен бота — жду строку вида 123456789:AA…")
    from aiogram import Bot
    from aiogram.exceptions import TelegramUnauthorizedError
    bot = Bot(token)
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        raise ServiceError("Telegram не принял этот токен") from None
    except Exception:                                     # noqa: BLE001 — текст ошибки aiohttp несёт URL с токеном
        raise ServiceError("Telegram не ответил на проверку") from None
    finally:
        try:
            await bot.session.close()
        except Exception:                                 # noqa: BLE001
            pass
    return int(me.id), str(me.username or "")
