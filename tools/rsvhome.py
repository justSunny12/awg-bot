"""
rsvhome.py — `sudo awg-bot reserve home --force`: рычаг «вернуть пользователей без
резерва» из терминала — запасной путь, когда бот в Telegram недоступен (вебхук его
бота у мёртвого резерва, Telegram отдаёт обновления туда).

Делает то же, что рычаг из меню, — руками запущенного бота (runtime/rsvhome):
кладёт запрос `reserve/home.req` в каталог данных и ждёт итог `reserve/home.res`.
Бот берёт запрос тактом переезда (3 с): проверки рычага (пользователи у резерва,
он не на связи дольше 10 мин, по getWebhookInfo бот на резерве в Telegram не
отвечает), пульт ещё не забран — сначала забирает его (`deleteWebhook` без
сброса очереди), затем запись на себя и Telegram здесь. Сам процесс ни базы, ни
Telegram не трогает: гонки с тактами бота нет.

Коды: 0 — рычаг пошёл (итог строкой); 3 — отказ бота с причиной; 4 — бот не взял
запрос за PICK_S (не запущен? `systemctl status awg-bot`), запрос убран — позже
он не сработает; 5 — бот взял запрос, а итога нет за DONE_S (смотри карточку
резерва и `journalctl -u awg-bot`); 2 — без `--force`.
"""
from __future__ import annotations

import json
import secrets
import sys
import time

PICK_S = 30           # бот берёт запрос тактом в 3 с — запас на загруженный цикл
DONE_S = 60           # рычаг: getWebhookInfo и deleteWebhook по 10 с, запись DNS, Telegram
POLL_S = 0.5
USAGE = ("awg-bot reserve home --force — вернуть пользователей без резерва (то же, что «⚠️ Вернуть без …» "
         "в карточке резерва); обычный возврат — кнопкой «↩️ Вернуть пользователей» в боте")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args != ["--force"]:
        print(USAGE, file=sys.stderr)
        return 2
    from awgbot.infra import rsvzone
    from awgbot.runtime import rsvhome
    req, res = rsvhome.req_path(), rsvhome.res_path()
    rid = secrets.token_hex(8)
    try:
        res.unlink(missing_ok=True)                          # итог прошлого запроса — не этот
        rsvzone.write_private(req, json.dumps({"id": rid, "until": time.time() + PICK_S}))
    except OSError as e:
        print(f"запрос не записан: {e}", file=sys.stderr)
        return 5
    print("запрос передан боту — жду…", flush=True)
    picked = False
    deadline = time.monotonic() + PICK_S
    while True:
        out = rsvzone.read_json(res)
        if str(out.get("id") or "") == rid:
            res.unlink(missing_ok=True)
            rc = int(out.get("rc") or 0)
            print(str(out.get("text") or ""), file=sys.stdout if rc == 0 else sys.stderr)
            return rc
        if not picked and not req.exists():
            picked = True
            deadline = time.monotonic() + DONE_S
        if time.monotonic() >= deadline:
            if picked:
                break
            try:
                req.unlink()                                 # не взят — убрать, чтобы позже он не сработал
            except FileNotFoundError:
                picked = True                                # бот взял его в это самое мгновение — ждать итог
                deadline = time.monotonic() + DONE_S
                continue
            except OSError:
                pass
            print(f"бот не взял запрос за {PICK_S} с — запущен ли сервис? systemctl status awg-bot; запрос убран",
                  file=sys.stderr)
            return 4
        time.sleep(POLL_S)
    print(f"бот взял запрос, а итога нет за {DONE_S} с — смотри карточку резерва и journalctl -u awg-bot",
          file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
