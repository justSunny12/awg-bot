"""Точка входа пакета: `python -m awgbot`.

Тонкая обёртка над awgbot.runtime.main.main() — вся сборка и запуск там.
Ровно то же поведение, что было у прежнего `python main.py`.
"""
import asyncio

from awgbot.runtime.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
