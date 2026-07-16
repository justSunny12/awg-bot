"""Точка входа пакета: `python -m awgbot`.

Тонкая обёртка над awgbot.runtime.main.main() — вся сборка и запуск там.
Ровно то же поведение, что было у прежнего `python main.py`.
"""
import asyncio
import sys

from awgbot.runtime.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        # PreflightError и прочие фатальные — печатаем ЧИСТО (без трейсбека):
        # сообщение рассчитано на чтение человеком в `journalctl -u awg-bot`.
        # Ненулевой код → systemd видит сбой (Restart/StartLimit отработают).
        from awgbot.runtime.preflight import PreflightError
        if isinstance(e, PreflightError):
            print(f"[awg-bot] {e}", file=sys.stderr)
            sys.exit(1)
        raise
