"""
rsvbusy.py — сторож для скриптов поставки: занят ли сервер резервом так, что
обновление или восстановление сейчас нельзя. Код 0 — свободен; 3 — занят,
причина в stdout (одной строкой). Читает только базу бота — ни сети, ни Telegram.

    awg-bot update / restore зовут его до остановки сервиса (bot_tool tools.rsvbusy).

Занят: мои пользователи у резерва или едут (leaving / away / returning) — код и
данные уедут из-под гостя при возврате; у меня пользователи и бот резерва (taking /
serving / releasing) — код в /opt меняется под работающим ботом основы; ждём
правки бота на резерве после «вернуть без резерва» или мой хвост не перенесён —
выпуск без этого пути их бы не принял (как отказ бота, journal/tail).
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        from awgbot.core import config
        from awgbot.infra.db import Database
        db = Database(str(config.DATA_DIR / "bot.db"))
    except Exception as e:                                # noqa: BLE001 — базы нет: не занят
        print(f"резерв: база не прочитана ({e}) — считаю свободным", file=sys.stderr)
        return 0
    try:
        try:
            paired = db.reserve_peer() is not None
        except Exception:                                 # noqa: BLE001 — таблицы нет (старая база)
            return 0
        if not paired:
            return 0

        def st(key: str) -> str:
            raw = db.get_state(key) or ""
            try:
                data = json.loads(raw) if raw.startswith("{") else {}
            except ValueError:
                data = {}
            return str(data.get("state") or "")
        out, inn = st("rsv_out"), st("rsv_in")
        waiting = ""
        try:
            from awgbot.domain.services.reserve.journal.tail import own_tail_blocks_raw
            raw_out = json.loads(db.get_state("rsv_out") or "{}")
            if isinstance(raw_out, dict) and raw_out.get("await_merge"):
                waiting = "ждём правки бота с резерва после «вернуть без резерва»"
            elif own_tail_blocks_raw(db):
                waiting = "свой хвост правок ещё не перенесён на резерв"
        except Exception:                                 # noqa: BLE001 — старая база: не ждём
            pass
        peer = db.reserve_peer() or {}
        name = str(peer.get("name") or "резерв")
    finally:
        try:
            db.close()
        except Exception:                                 # noqa: BLE001
            pass
    if out in ("leaving", "away", "returning"):
        print(f"пользователи этого сервера сейчас у резерва «{name}» ({out}) — сначала их возврат")
        return 3
    if inn in ("taking", "serving", "releasing"):
        print(f"здесь сейчас пользователи и бот «{name}» ({inn}) — сначала их возврат к нему")
        return 3
    if waiting:
        print(f"{waiting} («{name}») — обновление и восстановление после этого")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
