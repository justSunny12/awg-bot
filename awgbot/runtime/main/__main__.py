"""__main__.py — часть пакета main (см. __init__): запуск модулем `python -m awgbot.runtime.main`."""

import asyncio

from awgbot.runtime.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
