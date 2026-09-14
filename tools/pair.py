"""
pair.py — опознать админа по одноразовому коду вместо вопроса «твой Telegram ID?».

ЗАЧЕМ. Числовой ID у человека под рукой не лежит: за ним идут к стороннему боту
вроде @userinfobot, то есть установка упирается в чужой сервис и в ручное
списывание цифр. А бот к этому моменту уже создан и токен уже введён — значит
спросить «кто ты» можно у самого Telegram: кто пришлёт код, тот и админ.

КАК. Печатаем код, забираем обновления бота длинным опросом (getUpdates) и ждём
сообщение с этим кодом. Пришло — отвечаем человеку в чат, печатаем его id в
stdout и выходим. Всё остальное (приглашения, подсказки, ошибки) идёт в stderr:
установщик читает ровно одну строку `ADMIN_ID=<id>`.

БЕЗОПАСНОСТЬ. Код одноразовый, живёт минуты и набирается из алфавита без
похожих символов. Бот в этот момент никому не известен: он только что создан, а
его имя знает лишь тот, кто его создал. Токен берём из окружения, а не из
аргументов: аргументы видны в `ps` любому пользователю хоста.

Запуск:  BOT_TOKEN=… python -m tools.pair [--timeout 600] [--code ABCD-1234]
Коды выхода: 0 — опознали, 1 — токен не принят/сеть, 2 — не дождались,
130 — прервано с клавиатуры.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
# Без 0/O, 1/I/L — код диктуют вслух и набирают с телефона.
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
POLL_TIMEOUT = 25                      # длинный опрос: одно соединение на 25 с


def say(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def make_code() -> str:
    rnd = random.SystemRandom()
    raw = "".join(rnd.choice(ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def api(token: str, method: str, params: dict | None = None, timeout: int = 35) -> dict:
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fatal_token(e: urllib.error.HTTPError) -> bool:
    """401/404 — токен неверный или бот удалён: ждать бессмысленно."""
    return e.code in (401, 404)


def main(argv: list[str]) -> int:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        say("[pair] нет BOT_TOKEN в окружении")
        return 1
    deadline_s = 600
    code = ""
    for i, a in enumerate(argv):
        if a == "--timeout" and i + 1 < len(argv):
            deadline_s = int(argv[i + 1])
        if a == "--code" and i + 1 < len(argv):
            code = argv[i + 1]
    code = code or make_code()

    # Вебхук и длинный опрос несовместимы: getUpdates при живом вебхуке всегда
    # отвечает 409. У свежего бота его нет, но установка бывает и повторной.
    try:
        api(token, "deleteWebhook", {"drop_pending_updates": "false"})
        me = api(token, "getMe").get("result", {})
    except urllib.error.HTTPError as e:
        say(f"[pair] Telegram не принял токен ({e.code}) — проверь его у @BotFather")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        say(f"[pair] нет связи с Telegram: {e}")
        return 1

    name = me.get("username") or me.get("first_name") or "бот"
    say("")
    say(f"  Открой в Telegram своего бота  @{name}")
    say(f"  и отправь ему код:   {code}")
    say("")
    say(f"  Жду до {deadline_s // 60} мин. Кто пришлёт код — тот и админ.")
    say("  Прервать — Ctrl+C (тогда спрошу Telegram ID вручную).")

    offset = 0
    deadline = time.time() + deadline_s
    want = normalize(code)
    while time.time() < deadline:
        try:
            resp = api(token, "getUpdates",
                       {"offset": offset, "timeout": POLL_TIMEOUT,
                        "allowed_updates": json.dumps(["message"])},
                       timeout=POLL_TIMEOUT + 10)
        except urllib.error.HTTPError as e:
            if fatal_token(e):
                say(f"[pair] Telegram отклонил токен ({e.code})")
                return 1
            time.sleep(3)
            continue
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(3)                       # сеть моргнула — просто ждём дальше
            continue

        for upd in resp.get("result", []):
            offset = max(offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or {}
            frm = msg.get("from") or {}
            if not frm.get("id") or normalize(msg.get("text", "")) != want:
                continue
            who = frm.get("username") or frm.get("first_name") or frm["id"]
            try:
                api(token, "sendMessage", {
                    "chat_id": frm["id"],
                    "text": ("✅ Код принят — ты администратор этого бота.\n\n"
                             "Установка продолжается; когда бот запустится, он "
                             "напишет сюда сам."),
                })
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                pass                            # опознали — а ответ не главное
            say(f"[pair] код принял {who} (id {frm['id']})")
            print(f"ADMIN_ID={frm['id']}")
            return 0
    say("[pair] не дождался кода")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        say("")
        sys.exit(130)
