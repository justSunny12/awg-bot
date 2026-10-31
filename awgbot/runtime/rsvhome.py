"""
rsvhome.py — запасной путь рычага «вернуть без резерва» из терминала:
`sudo awg-bot reserve home --force` (tools/rsvhome.py) кладёт запрос, его
забирает и исполняет запущенный бот — такт переезда (3 с), — итог отдаётся
файлом.

Почему через бот, а не самой командой: поллинг Telegram (runtime/pollctl), автомат
«мои пользователи» под замком процесса (`_LOCK`) и живые сообщения хода — в
процессе бота. Отдельный процесс со своим `deleteWebhook` и записью `rsv_out`
гонялся бы с тактами бота (замок у них разный), а поллинг бота о снятом вебхуке
не узнал бы до перепроверки (до 7 мин) — бот был бы нем.

Запрос — `reserve/home.req` в каталоге данных: `{"id", "until"}` (секунды эпохи;
просроченный бот выбрасывает — CLI уже сказал «не взят»). Бот запрос сразу
убирает (CLI видит «взят») и делает то же, что рычаг из меню: `reserve_force_check`,
вердикт getWebhookInfo (`reserve_force_webhook_check`, вебхук не снимается), а
пульт ещё не забран — сначала забирает его (`deleteWebhook` без сброса очереди,
`reserve_pult_taken`), затем `reserve_force_home`. Итог — `reserve/home.res`:
`{"id", "rc", "text"}`; rc 0 — рычаг пошёл, 3 — отказ с причиной.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from awgbot.core import config

log = logging.getLogger(__name__)

REQ_NAME = "home.req"
RES_NAME = "home.res"
RC_OK, RC_REFUSED = 0, 3


def req_path() -> Path:
    return Path(config.DATA_DIR) / "reserve" / REQ_NAME


def res_path() -> Path:
    return Path(config.DATA_DIR) / "reserve" / RES_NAME


def _take_request() -> dict | None:
    """Запрос, если он есть и не просрочен; файл убирается сразу (взят)."""
    p = req_path()
    if not p.exists():
        return None
    try:
        req = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        req = {}
    try:
        p.unlink()
    except OSError as e:
        log.warning("резерв: запрос awg-bot reserve home не убран: %s", e)
        return None
    if not isinstance(req, dict):
        return None
    rid = str(req.get("id") or "")
    if not rid or len(rid) > 64 or not rid.isalnum():
        log.warning("резерв: запрос awg-bot reserve home без номера — пропущен")
        return None
    try:
        until = float(req.get("until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if until < time.time():
        log.info("резерв: запрос awg-bot reserve home просрочен — пропущен")
        return None
    return {"id": rid}


def _write_result(rid: str, rc: int, text: str) -> None:
    from awgbot.infra import rsvzone
    rsvzone.write_private(res_path(), json.dumps({"id": rid, "rc": int(rc), "text": str(text)}, ensure_ascii=False))


def _done_text(d: dict) -> str:
    name = str(d.get("name") or "резерв")
    rec, own = str(d.get("record") or ""), str(d.get("own") or "")
    if rec and not d.get("manual"):
        head = f"запись {rec} → {own or 'адрес этого сервера'}, Telegram у этого сервера"
    elif rec:
        head = f"Telegram у этого сервера; запись {rec} смени у провайдера на {own or 'адрес этого сервера'}"
    else:
        head = "Telegram у этого сервера"
    return f"пользователи возвращаются сюда: {head}; изменения с «{name}» придут, когда он выйдет на связь"


async def force_home(bot, svc, pc) -> tuple[int, str]:
    """Рычаг «вернуть без резерва» с теми же проверками, что из меню; пульт не забран — сначала он."""
    from awgbot.domain.services import ServiceError
    from awgbot.runtime import pollctl, rsvactions
    why = await asyncio.to_thread(svc.reserve_force_check)
    if why:
        return RC_REFUSED, why
    info = await pollctl.webhook_info(bot, "awg-bot reserve home") if bot is not None else None
    try:
        await asyncio.to_thread(svc.reserve_force_webhook_check, info)
    except ServiceError as e:
        return RC_REFUSED, str(e)
    if not await asyncio.to_thread(svc.reserve_pult_on):
        if pc is None or not await pc.take():
            return RC_REFUSED, "Telegram не принял deleteWebhook — бот на резерве по-прежнему держит Telegram; повтори позже"
        await rsvactions.run(await asyncio.to_thread(svc.reserve_pult_taken, "cli"))
    data = await asyncio.to_thread(svc.reserve_force_dialog_data)
    try:
        actions = await asyncio.to_thread(svc.reserve_force_home)
    except ServiceError as e:
        return RC_REFUSED, str(e)
    await rsvactions.run(actions)
    log.warning("резерв: рычаг «вернуть без резерва» — по запросу awg-bot reserve home")
    return RC_OK, _done_text(data)


async def tick() -> None:
    """Такт переезда: запрос CLI есть — исполнить и отдать итог."""
    req = _take_request()
    if req is None:
        return
    from awgbot.runtime import rsvactions
    bot, svc, pc = rsvactions._ctx.get("bot"), rsvactions._ctx.get("services"), rsvactions._ctx.get("pollctl")
    if svc is None:
        return
    try:
        rc, text = await force_home(bot, svc, pc)
    except Exception as e:                                   # noqa: BLE001
        log.warning("резерв: запрос awg-bot reserve home: %s", e)
        rc, text = RC_REFUSED, f"бот не выполнил запрос: {e}"
    try:
        _write_result(req["id"], rc, text)
    except OSError as e:
        log.warning("резерв: итог awg-bot reserve home не записан: %s", e)
