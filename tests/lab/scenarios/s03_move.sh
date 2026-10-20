#!/bin/bash
# s03_move — право отвечать в Telegram при переезде кнопкой и возврате: основа поллит, «резерв» ставит
# вебхук, основа получает 409 и замолкает за один запрос, обновление доставляется на вебхук, при возврате
# вебхук снимается без сброса очереди и накопившееся получает основа опросом.
#
# ЧЕРНОВИК: набросан по образцу s02_guest, на стенде не прогонялся. Что нужно от стенда: venv с
# зависимостями (~/venv пользователя sudo), openssl, curl. Bot API — макет tests/lab/botapi.py на
# петле хоста; основа — настоящий PollControl поверх настоящего aiogram (скрипт ниже), «бот основы на
# резерве» здесь заменён приёмником вебхука на python: зона и клон для этого сценария не нужны
# (их ведёт s02_guest). Полный сценарий пары на двух узлах стенда (переезд кнопкой через канал пары,
# запись DNS, пересылка хвоста на клиентах cA/cB) — следующий шаг: узлы topo.sh, бот A и бот B как
# отдельные процессы на макете, клиент cA с конфигом основы.
#
# Проверяет:
#   1. пока вебхука нет, основа получает обновление опросом (в журнале обработчика — оно);
#   2. вебхук поставлен (setWebhook) — следующий getUpdates основы отвечает 409, поллинг остановлен
#      один раз (число getUpdates в журнале макета больше не растёт), право отвечать — «у резерва»;
#   3. обновление, пришедшее при вебхуке, доставляется на приёмник резерва с верным секретом, основе
#      оно не попадает;
#   4. возврат: deleteWebhook(drop_pending_updates=false) — обновление, пришедшее между закрытием
#      приёмника и deleteWebhook, получает основа опросом, ни одно не потеряно и не обработано дважды.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(cd "$HERE/../.." && pwd)"
USER_="${SUDO_USER:-$(id -un)}"; HOME_="$(getent passwd "$USER_" | cut -d: -f6)"
PY="${LAB_PY:-$HOME_/venv/bin/python}"
WORK=/var/tmp/awglab-move; JOURNAL=$WORK/botapi.jsonl; LOG=$WORK/primary.log
TOKEN="880000:LAB-DUMMY-token"; API=http://127.0.0.1:8081
BOTAPI_PID=""; PRIMARY_PID=""; HOOK_PID=""
cleanup() {
    for p in "$PRIMARY_PID" "$HOOK_PID" "$BOTAPI_PID"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done
    rm -rf "$WORK"
}
trap cleanup EXIT
cleanup; mkdir -p "$WORK"
cd "$ROOT" || exit 1

"$PY" -m tests.lab.botapi --listen 127.0.0.1:8081 --journal "$JOURNAL" >/dev/null 2>&1 & BOTAPI_PID=$!
sleep 1
inject() { curl -s -X POST "$API/inject/$TOKEN" -H 'Content-Type: application/json' \
    -d "{\"message\": {\"message_id\": $1, \"date\": 1, \"chat\": {\"id\": 1, \"type\": \"private\"}, \"from\": {\"id\": 1, \"is_bot\": false, \"first_name\": \"x\"}, \"text\": \"$2\"}}" >/dev/null; }

# ── основа: настоящий PollControl, обработчик пишет текст обновления в журнал ─────────────────────
cat > "$WORK/primary.py" <<'PYEOF'
import asyncio, os, sys
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from awgbot.runtime import pollctl

class Svc:
    """Сервисы основы: пара есть, право отвечать — в файле."""
    def __init__(self, path): self.path = path; self.write("mine")
    def write(self, s): open(self.path, "w").write(s)
    def reserve_paired(self): return True
    def reserve_poll(self): return open(self.path).read()
    def reserve_set_poll(self, state, **why): self.write(state)

async def main():
    work = os.environ["WORK"]
    bot = Bot(os.environ["TOKEN"], session=AiohttpSession(api=TelegramAPIServer.from_base(os.environ["API"])))
    dp = Dispatcher()
    @dp.message()
    async def on_message(m):
        open(work + "/handled.log", "a").write(m.text + "\n")
    pollctl.POLL_TIMEOUT = 2
    ctl = pollctl.PollControl(dp, bot, Svc(work + "/poll.state"))
    task = asyncio.create_task(ctl.run())
    # команды сценария — файлами: take / release
    while True:
        await asyncio.sleep(0.3)
        if os.path.exists(work + "/cmd.take"):
            os.unlink(work + "/cmd.take"); await ctl.take()
        if os.path.exists(work + "/cmd.release"):
            os.unlink(work + "/cmd.release"); await ctl.release()
        if task.done(): break
asyncio.run(main())
PYEOF
WORK=$WORK TOKEN=$TOKEN API=$API PYTHONPATH="$ROOT" "$PY" "$WORK/primary.py" >"$LOG" 2>&1 & PRIMARY_PID=$!
sleep 3
kill -0 "$PRIMARY_PID" 2>/dev/null || { echo "основа не запустилась: $(tail -n5 "$LOG")"; exit 1; }

# ── 1. обычный опрос ─────────────────────────────────────────────────────────────────────────────
inject 1 "первое"
for _ in $(seq 1 10); do grep -qx "первое" "$WORK/handled.log" 2>/dev/null && break; sleep 1; done
grep -qx "первое" "$WORK/handled.log" 2>/dev/null || { echo "основа не получила обновление опросом: $(tail -n5 "$LOG")"; exit 1; }
[ "$(cat "$WORK/poll.state")" = "mine" ] || { echo "право отвечать не у основы до переезда"; exit 1; }

# ── 2. вебхук резерва → 409 → основа замолкает ───────────────────────────────────────────────────
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$WORK/k.pem" -out "$WORK/c.pem" -days 1 -subj "/CN=127.0.0.1" \
    -addext "subjectAltName=IP:127.0.0.1" >/dev/null 2>&1
cat > "$WORK/hook.py" <<'PYEOF'
import json, os, ssl
from aiohttp import web
SECRET = "lab-secret"
async def h(request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        return web.Response(status=403)
    upd = await request.json()
    open(os.environ["WORK"] + "/webhook.log", "a").write(upd["message"]["text"] + "\n")
    return web.Response(text="ok")
app = web.Application(); app.router.add_post("/hook", h)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(os.environ["WORK"] + "/c.pem", os.environ["WORK"] + "/k.pem")
web.run_app(app, host="127.0.0.1", port=8443, ssl_context=ctx, print=None)
PYEOF
WORK=$WORK "$PY" "$WORK/hook.py" >/dev/null 2>&1 & HOOK_PID=$!
sleep 2
curl -s -X POST "$API/bot$TOKEN/setWebhook" -F "url=https://127.0.0.1:8443/hook" -F "secret_token=lab-secret" \
    -F "certificate=@$WORK/c.pem" >/dev/null
for _ in $(seq 1 15); do [ "$(cat "$WORK/poll.state")" = "away" ] && break; sleep 1; done
[ "$(cat "$WORK/poll.state")" = "away" ] || { echo "основа не ушла в away после 409: $(tail -n5 "$LOG")"; exit 1; }
n1="$(grep -c '"getUpdates"' "$JOURNAL" 2>/dev/null || echo 0)"
sleep 6
n2="$(grep -c '"getUpdates"' "$JOURNAL" 2>/dev/null || echo 0)"
[ "$n1" = "$n2" ] || { echo "после 409 основа продолжает опрос: $n1 → $n2"; exit 1; }

# ── 3. доставка на вебхук ────────────────────────────────────────────────────────────────────────
inject 2 "при вебхуке"
for _ in $(seq 1 15); do grep -qx "при вебхуке" "$WORK/webhook.log" 2>/dev/null && break; sleep 1; done
grep -qx "при вебхуке" "$WORK/webhook.log" 2>/dev/null || { echo "обновление не дошло до приёмника резерва"; exit 1; }
grep -qx "при вебхуке" "$WORK/handled.log" && { echo "обновление обработано и основой"; exit 1; }

# ── 4. возврат: приёмник закрыт → обновление копится → deleteWebhook без сброса → основа забирает ──
kill "$HOOK_PID" 2>/dev/null; HOOK_PID=""
sleep 1
inject 3 "между закрытием и снятием"
touch "$WORK/cmd.take"
for _ in $(seq 1 20); do grep -qx "между закрытием и снятием" "$WORK/handled.log" 2>/dev/null && break; sleep 1; done
grep -qx "между закрытием и снятием" "$WORK/handled.log" 2>/dev/null || { echo "накопленное обновление потеряно при снятии вебхука"; exit 1; }
[ "$(cat "$WORK/poll.state")" = "mine" ] || { echo "право отвечать не вернулось основе"; exit 1; }
[ "$(sort "$WORK/handled.log" "$WORK/webhook.log" | uniq -d | wc -l)" = "0" ] || { echo "обновление обработано дважды"; exit 1; }
echo ok
