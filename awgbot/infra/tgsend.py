"""
tgsend.py — отправить админу одно сообщение ИЗ ПРОЦЕССА, где бота нет.

Нужен ровно в одном классе случаев: что-то важное происходит не в боте, а в
установщике, CLI или systemd-таймере, и узнать об этом человек обязан сразу.
Первый такой случай — файервол: правила применяются с таймером отката, и
подтвердить их надо в чате, потому что чат работает независимо от того,
заперли вы себе SSH или нет.

Никакого aiogram: стандартная библиотека и один POST. Здесь не нужен ни
планировщик, ни тихие часы (сообщение про обратный отсчёт не откладывают), ни
ретраи — не доставили, значит остаётся путь через CLI, о котором тут же и
написано.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from awgbot.core import config

log = logging.getLogger("awgbot.tgsend")
_API = "https://api.telegram.org"
_TIMEOUT = 10


def send(text: str, buttons: list[tuple[str, str]] | None = None,
         chat_id: int | None = None) -> bool:
    """Сообщение админу. buttons — [(подпись, callback_data)], по кнопке в
    строке. True — доставлено. Любая ошибка гасится: вызывающий не должен
    падать из-за недоступного Telegram."""
    token, chat = config.BOT_TOKEN, chat_id or config.ADMIN_ID
    if not token or not chat:
        return False
    params = {"chat_id": str(chat), "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if buttons:
        params["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": t, "callback_data": d}] for t, d in buttons]})
    try:
        req = urllib.request.Request(f"{_API}/bot{token}/sendMessage",
                                     data=urllib.parse.urlencode(params).encode(),
                                     method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")).get("ok", False)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("не отправлено админу: %s", e)
        return False
