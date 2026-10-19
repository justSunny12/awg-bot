#!/usr/bin/env python3
"""
tools/join.py — `awg-bot join <токен>` на шлюзе: подключение к серверу AWG
командой из его бота (§3.6 концепта «единые обновления»).

    python -m tools.join <токен>     линк по токену, агент в режиме сопряжения, код на экран
    python -m tools.join --status    ход и код ещё раз (из файла хода агента)

Разбор и проверка токена (util/jointoken: контрольная сумма, формат, срок),
конфиг линка на ключах токена (без PSK и I1 — их ставит смена ключей после
кода), `routing-gw-setup.sh --join` — только линк и карантин, файл
сопряжения /etc/awg-gw/join.json (0600), перезапуск агента (без токена бота
он стартует в режиме сопряжения). Дальше — ход по файлу хода агента
/run/awg-bot/join.status: рамка с кодом (Т-Т3), неверный код (Т-Т4), ход
после кода (Т-Т5), отказ (Т-Т6–Т-Т8). Закрыть терминал можно: подключение
продолжится, код повторит `awg-bot join --status` (подключения нет — так и
говорит, код 1).

Повтор той же команды (тот же id в файле сопряжения, ход уже идёт) линк и
агента не трогает — только печатает ход. Токен с вышедшим сроком — отказ до
первого действия. Агент не перезапустился — отказ сразу: без него кода не
будет. Вызывают установщик (reconfigure --join, после update —
AWG_UPDATE_THEN_JOIN) и `awg-bot join` руками.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

POLL_S = 1.0
RULE = "─" * 42


def _p(text: str) -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(f"[awg-bot:ОШИБКА] {text}", file=sys.stderr, flush=True)


def _until(expires: int) -> str:
    """«13:07 (UTC+3)»; не сегодня — с датой."""
    from datetime import datetime
    from awgbot.util import timeutil
    dt = datetime.fromtimestamp(int(expires), timeutil.TZ)
    if dt.date() == timeutil.now().astimezone(timeutil.TZ).date():
        return timeutil.fmt_clock_ui(dt)
    return timeutil.fmt_dt_ui(dt)


def _link_conf(tok) -> str:
    """Конфиг шлюзовой стороны на ключах токена: Jc…H4 из токена, без PSK и
    без I1; AllowedIPs — только /30 линка (канал до ВПС), Endpoint — сервер."""
    from awgbot.domain import gwcfg
    from awgbot.util import wgconf
    obf = tok.obfuscation()
    conf = wgconf.Conf()
    conf.sections.append(("Interface", [("Address", f"{tok.gw_addr}/30"), ("PrivateKey", tok.gw_priv_b64),
                                        *[(k, obf[k]) for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4",
                                                               "H1", "H2", "H3", "H4")]]))
    conf.sections.append(("Peer", [("PublicKey", tok.vps_pub_b64), ("Endpoint", f"{tok.host}:{tok.link_port}"),
                                   ("AllowedIPs", tok.cidr), ("PersistentKeepalive", "25")]))
    return wgconf.render(conf, "link", stamp=gwcfg.LINK_CONTRACT)


def _status() -> dict:
    from awgbot.domain.gateway.join import STATUS_FILE
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _frame(code: str, until: str) -> None:
    _p(RULE)
    _p(f"  Код подтверждения:   {code}")
    _p("  Введи его в боте сервера AWG:")
    _p("  «✏️ Ввести код» в сообщении подключения")
    _p(RULE)
    _p(f"Жду до {until}. Закроешь терминал — подключение продолжится;")
    _p("код покажет sudo awg-bot join --status")


_WHY = {
    "code": "сервер AWG закрыл подключение: код не подтверждён трижды.\n"
            "Выпусти новую команду в боте сервера AWG и запусти её здесь",
    "cancel": "сервер AWG отменил подключение",
}
_GONE = ("сервер AWG не отвечает по этой команде: она истекла, использована или отменена — выпусти новую в "
         "боте сервера AWG")


def _follow(expires: int, *, once: bool = False) -> int:
    """Ход сопряжения по файлу хода: печать каждой смены; 0 — подключён."""
    seen: tuple = ()
    until = _until(expires)
    while True:
        st = _status()
        state = st.get("state", "")
        key = (state, st.get("code"), st.get("left"))
        if key != seen:
            seen = key
            name = st.get("name") or "шлюз"
            if state in ("", "waiting") and once:
                # --status до первого ответа сервера — не молчим (flow-7)
                _p(f"[awg-bot] линк по команде подключения поднят — жду ответа сервера AWG до {until}")
            elif state == "code":
                _frame(st.get("code", ""), until)
            elif state == "wrong":
                _p(f"[awg-bot] код введён на сервере AWG неверно — осталось попыток: {st.get('left', 0)}")
            elif state == "accepted":
                _p("[awg-bot] код подтверждён — ставлю постоянные ключи линка…")
            elif state == "linked":
                _p("[awg-bot] получаю настройки шлюза: аплинк, бот шлюза, обвязка…")
            elif state == "done":
                bot = st.get("bot") or ""
                tail = f" Открой бота шлюза @{bot} и нажми «Запустить»" if bot else ""
                _p(f"[awg-bot] ✅ Готово: шлюз «{name}» подключён.{tail}")
                return 0
            elif state == "refused":
                _err(_WHY.get(st.get("why", ""), _GONE))
                return 1
        if once:
            return 0
        if int(time.time()) > int(expires) and state in ("", "waiting", "code", "wrong"):
            _err(_GONE)
            return 1
        time.sleep(POLL_S)


def cmd_join(text: str) -> int:
    from awgbot.core import config
    from awgbot.domain.gateway.join import JOIN_FILE, STATUS_FILE
    from awgbot.infra import gwguard
    from awgbot.util import jointoken
    from awgbot.util.fsatomic import write_private
    try:
        tok = jointoken.decode(jointoken.find(text) or text)
    except jointoken.TokenError:
        _err("команда подключения повреждена — скопируй её из бота сервера AWG целиком")
        return 1
    if int(time.time()) > tok.expires:
        _err(_GONE)
        return 1
    st = {}
    try:
        with open(JOIN_FILE, encoding="utf-8") as f:
            st = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        st = {}
    if st.get("id") == tok.join_id and (st.get("state") in ("accepted", "linked", "done")
                                        or (st.get("state") == "pairing" and _status().get("state"))):
        # повтор той же команды (терминал закрыли): идёт — только ход и код,
        # линк и агент не трогаются (flow-11)
        return _follow(tok.expires)
    os.makedirs(os.path.dirname(JOIN_FILE), exist_ok=True)
    conf_path = os.path.join(os.path.dirname(JOIN_FILE), "join.conf")
    write_private(conf_path, _link_conf(tok))
    write_private(JOIN_FILE, json.dumps({
        "id": tok.join_id, "slot": tok.slot, "version": tok.version, "expires": tok.expires,
        "host": tok.host, "channel_port": tok.channel_port, "link_port": tok.link_port,
        "state": "pairing", "name": ""}, ensure_ascii=False))
    try:
        os.remove(STATUS_FILE)                            # ход прошлой команды — не о ней
    except OSError:
        pass
    script = os.path.join(str(config.BASE_DIR), "install", "routing-gw-setup.sh")
    ok, out = gwguard.join_link(conf_path, tok.channel_port, script=script)
    try:
        os.remove(conf_path)
    except OSError:
        pass
    if not ok:
        # файл сопряжения — прочь: иначе агент на следующем перезапуске ушёл бы в
        # режим сопряжения без линка, а клиент канала — на порт из файла
        for path in (JOIN_FILE, STATUS_FILE):
            try:
                os.remove(path)
            except OSError:
                pass
        _err("линк по команде подключения не поднялся: " + (out.splitlines()[-1] if out else "скрипт отказал"))
        return 1
    try:
        rc = subprocess.run(["systemctl", "restart", "awg-bot"], check=False, capture_output=True,
                            timeout=120).returncode
    except (OSError, subprocess.SubprocessError) as e:
        rc, out = 1, str(e)
    if rc != 0:
        # агент не встал в режим сопряжения — ждать кода бессмысленно (flow-10)
        _err("агент awg-bot не перезапустился — линк поднят, но сопряжения не будет; "
             "journalctl -u awg-bot -e -o short-iso")
        return 1
    _p("[awg-bot] линк до сервера AWG поднят по команде подключения — жду ответа…")
    return _follow(tok.expires)


def cmd_status() -> int:
    from awgbot.domain.gateway.join import JOIN_FILE
    try:
        with open(JOIN_FILE, encoding="utf-8") as f:
            st = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        st = {}
    if not st and not _status():
        _p("[awg-bot] подключения командой нет — запусти команду из бота сервера AWG")
        return 1
    return _follow(int(st.get("expires") or 0) or int(time.time()) + 1, once=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("использование: awg-bot join <команда подключения> | --status", file=sys.stderr)
        return 2
    if args[0] == "--status":
        return cmd_status()
    return cmd_join(" ".join(args))


if __name__ == "__main__":
    sys.exit(main())
