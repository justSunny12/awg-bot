"""
first_device.py — показать конфигурацию первого устройства админа в ТЕРМИНАЛЕ.

ЗАЧЕМ. Бот заводит админу первое устройство сам и присылает ссылку в чат — но
ровно в этот момент Telegram у человека может не открываться: он для того и
ставит VPN. Установка, которая заканчивается словами «остальное в боте»,
оставляла бы его без способа туда попасть.

Печатает ссылку vpn://, путь к файлу .conf и QR прямо в терминал (полублоками,
если ширина позволяет). Ничего не создаёт: устройство уже завёл бот, здесь
только показ.

Запуск:  python -m tools.first_device [--no-qr]
Коды выхода: 0 — показали, 1 — устройства ещё нет (бот не стартовал?).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from awgbot.core import config


def _terminal_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except OSError:
        return 80


def print_qr(link: str) -> bool:
    """QR полублоками: две строки модулей на строку терминала. False — не
    поместился (узкое окно) или библиотеки нет; ссылка всё равно напечатана."""
    try:
        import qrcode
    except ImportError:
        return False
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=1)
    qr.add_data(link)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) + 2 > _terminal_width():
        return False
    rows = list(matrix)
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))
    # Цвета задаём явно (чёрное на белом), а не полагаемся на тему терминала:
    # на тёмной теме код вышел бы инвертированным, а это уже лотерея сканера.
    for top, bottom in zip(rows[0::2], rows[1::2]):
        line = []
        for t, b in zip(top, bottom):
            line.append(" ▄▀█"[(1 if t else 0) * 2 + (1 if b else 0)])
        print("\033[47;30m" + "".join(line) + "\033[0m")
    return True


def main(argv: list[str]) -> int:
    from awgbot.core import settings
    from awgbot.domain.services import Services, ServiceError
    from awgbot.infra.db import Database

    if not Path(config.DB_PATH).exists():
        print("База ещё не создана — бот не запускался. Запусти сервис и повтори.",
              file=sys.stderr)
        return 1
    settings.init(config.CONF_DIR)
    db = Database(config.DB_PATH)
    try:
        services = Services(db)
        admin = services.admin_client()
        if admin is None:
            print("Профиля админа ещё нет — бот не успел стартовать.", file=sys.stderr)
            return 1
        devices = [d for d in db.list_devices(admin.id) if d.private_key]
        if not devices:
            print("У админа пока нет устройств — заведи первое в боте.", file=sys.stderr)
            return 1
        dev = devices[0]
        try:
            cfg = services.generate_config(dev.id)
        except ServiceError as e:
            print(f"Конфигурация не собрана: {e}", file=sys.stderr)
            return 1
    finally:
        db.close()

    out = Path("/root/awg-bot-first-device.conf")
    try:
        out.write_text(cfg["conf"], encoding="utf-8")
        os.chmod(out, 0o600)
        saved = str(out)
    except OSError:
        saved = ""

    print()
    print(f"  Устройство «{dev.name}» ({dev.address}) — первое, оно же админское.")
    print()
    if "--no-qr" not in argv and print_qr(cfg["vpn"]):
        print()
    else:
        print("  (QR не помещается в это окно — используй ссылку ниже)")
        print()
    print("  Ссылка для AmneziaVPN (импорт из буфера обмена):")
    print()
    print(f"  {cfg['vpn']}")
    print()
    if saved:
        print(f"  Файл конфигурации: {saved} (права 600, внутри ключи — удали после импорта)")
    print("  То же самое всегда доступно в боте: меню → устройства → карточка.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
