"""
rsvbusy.py — сторож для скриптов поставки: занят ли сервер резервом так, что
обновление или восстановление сейчас нельзя. Код 0 — свободен; 3 — занят,
причина в stdout (одной строкой; при ожидании — теми же словами, что отказ
«⬆️ Обновить» в боте, journal/tail OWN_TAIL_REFUSED / C9_WAIT_REFUSED, с хвостом
«; только ждать нельзя — AWG_RESERVE_IGNORE_WAIT=1 перед командой (правки с
резерва не придут)»). Читает только базу бота — ни сети, ни Telegram.

    awg-bot update / restore зовут его до остановки сервиса (bot_tool tools.rsvbusy).

Занят: мои пользователи у резерва или едут (leaving / away / returning) — код и
данные уедут из-под гостя при возврате; у меня пользователи и бот резерва (taking /
serving / releasing) — код в /opt меняется под работающим ботом основы; ждём
правки бота на резерве после «вернуть без резерва» или мой хвост не перенесён —
выпуск без этого пути их бы не принял (как отказ бота, journal/tail). Причина
одна, в этом порядке: отъезд, гость, ожидание.

Базы нет или таблицы пары нет (старая база) — свободен. База есть, а проверка
хвоста упала не от старой схемы (замок, сбой диска) — занят («состояние
резерва не прочитано … — повтори через минуту»): пустить обновление поверх
непрочитанного хвоста нельзя. AWG_RESERVE_IGNORE_WAIT=1 в окружении снимает
только ожидание (хвост, правки после «вернуть без резерва», непрочитанное
состояние) — строкой в stderr «резерв: … — ПРОДОЛЖАЮ по AWG_RESERVE_IGNORE_WAIT=1:
правки с резерва не придут»; отъезд и гостя не снимает. Базу не открыть вовсе —
свободен, строкой в stderr «резерв: база не прочитана (…) — считаю свободным».
stderr при кодах 0 и 3 reserve_busy_guard (install/lib/update.sh) показывает человеку.
"""
from __future__ import annotations

import json
import os
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

        def st(key: str) -> dict:
            raw = db.get_state(key) or ""
            try:
                data = json.loads(raw) if raw.startswith("{") else {}
            except ValueError:
                data = {}
            return data if isinstance(data, dict) else {}
        out, inn = str(st("rsv_out").get("state") or ""), str(st("rsv_in").get("state") or "")
        # порядок и правила — как у бота (reserve_refuse_update_if_waiting): свой хвост, потом С9
        waiting = ""
        peer = db.reserve_peer() or {}
        name = str(peer.get("name") or "резерв")
        try:
            from awgbot.domain.services.reserve.journal.tail import (C9_WAIT_REFUSED, OWN_TAIL_REFUSED, c9_wait_raw,
                                                                     own_tail_blocks_raw)
            from awgbot.util.rsvjrn import schema_too_old
            if own_tail_blocks_raw(db):                   # те же слова, что у отказа «⬆️ Обновить» в боте
                waiting = OWN_TAIL_REFUSED.format(name=name, what="обновление и восстановление")
            elif c9_wait_raw(db):
                waiting = C9_WAIT_REFUSED.format(name=name)
        except Exception as e:                            # noqa: BLE001
            if not schema_too_old(e):                     # старая база — не ждём; ошибка базы — не пускать
                waiting = f"состояние резерва не прочитано ({e}) — повтори через минуту"
        if waiting and os.environ.get("AWG_RESERVE_IGNORE_WAIT") == "1":
            print(f"резерв: {waiting} — ПРОДОЛЖАЮ по AWG_RESERVE_IGNORE_WAIT=1: правки с резерва не придут",
                  file=sys.stderr)
            waiting = ""
    finally:
        try:
            db.close()
        except Exception:                                 # noqa: BLE001
            pass
    if out in ("leaving", "away", "returning"):
        print(f"пользователи этого сервера сейчас у резерва «{name}» ({out}) — сначала их возврат")
        return 3
    if inn in ("taking", "serving", "releasing"):             # state.HOSTING (импорт домена — только под try выше)
        print(f"здесь сейчас пользователи и бот «{name}» ({inn}) — сначала их возврат к нему")
        return 3
    if waiting:
        print(f"{waiting}; только ждать нельзя — AWG_RESERVE_IGNORE_WAIT=1 перед командой (правки с резерва не придут)")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
