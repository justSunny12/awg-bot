#!/usr/bin/env python3
"""
manage_secrets.py — шифрование резервных копий (BACKUP_KEY / BACKUP_PASSPHRASE).

Бэкап содержит приватные ключи всех устройств и уходит через Telegram (стороннее
облако) — храним его только зашифрованным. Скрипт настраивает ключ/фразу в .env.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

try:
    from awgbot.util import secrets_util as su
    from awgbot.core import config
    from awgbot.infra.db import Database
except ModuleNotFoundError as e:
    sys.exit(f"Запускайте из каталога бота, установив зависимости "
             f"(pip install -r requirements.txt). Причина: {e}")

# Корень репозитория: tools/manage_secrets.py → parents[1]. Для dev-fallback путей
# (.env, файл ключа) — рядом с проектом, а не внутри tools/.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Секреты пишем туда же, откуда их читает config: AWG_BOT_ENV / /etc/awg-bot/env
# (боевой FHS-путь) / <корень>/.env (для запуска из исходников).
def _env_path() -> Path:
    v = os.environ.get("AWG_BOT_ENV")
    if v:
        return Path(v)
    if Path("/etc/awg-bot").is_dir():
        return Path("/etc/awg-bot/env")
    return _REPO_ROOT / ".env"


ENV_PATH = _env_path()
DEFAULT_KEY_FILE = _REPO_ROOT / "agent_hb_key.b64"


# ─────────────────────────────────────────────────────────────────────────────
# .env (ключи шифрования бэкапов)
# ─────────────────────────────────────────────────────────────────────────────

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        s = raw.strip()
        key = s.split("=", 1)[0].strip() if ("=" in s and not s.startswith("#")) else None
        if key in updates:
            seen.add(key)
            if updates[key] is not None:
                out.append(f"{key}={updates[key]}")
        else:
            out.append(raw)
    for key, val in updates.items():
        if key not in seen and val is not None:
            out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Диалог
# ─────────────────────────────────────────────────────────────────────────────

def ask_choice(prompt: str, options: dict[str, str], default: str) -> str:
    print("\n" + prompt)
    for k, label in options.items():
        print(f"  {k}) {label}" + (" (по умолчанию)" if k == default else ""))
    while True:
        ans = input("Выбор: ").strip().lower() or default
        if ans in options:
            return ans
        print("Нет такого варианта.")


def ask_passphrase(what: str) -> str:
    while True:
        p1 = getpass.getpass(f"Пассфраза для {what} (ввод скрыт): ")
        if len(p1) < 8:
            print("Минимум 8 символов.")
            continue
        if p1 != getpass.getpass("Повторите: "):
            print("Не совпало.")
            continue
        return p1


def setup_backup(current: dict) -> dict[str, str]:
    have_key = current.get("BACKUP_KEY", "")
    have_pass = current.get("BACKUP_PASSPHRASE", "")
    status = ("случайный ключ" if have_key else "пассфраза" if have_pass else "НЕ шифруется")
    choice = ask_choice(
        f"2/2. Шифрование бэкапов — сейчас: {status}.\n"
        "     В БД приватные ключи устройств — на бою шифровать стоит.",
        {"s": "пропустить", "r": "СЛУЧАЙНЫЙ ключ (сохрани его сам)",
         "p": "ПАССФРАЗА", "x": "выключить"},
        default="s")
    if choice == "s":
        return {}
    if choice == "x":
        print("→ Шифрование выключено.")
        return {"BACKUP_KEY": None, "BACKUP_PASSPHRASE": None}
    if choice == "r":
        key_b64 = su.b64e(su.gen_random_key())
        print("\n→ Случайный ключ бэкапов. Отпечаток:", su.fingerprint(key_b64))
        print("  СОХРАНИ вне сервера — без него бэкап не восстановить:")
        print("    BACKUP_KEY=" + key_b64)
        return {"BACKUP_KEY": key_b64, "BACKUP_PASSPHRASE": None}
    passphrase = ask_passphrase("шифрования бэкапов")
    print("\n→ Шифрование по пассфразе. Запомни фразу — restore_backup.py спросит её.")
    return {"BACKUP_PASSPHRASE": passphrase, "BACKUP_KEY": None}


def main() -> None:
    print("=" * 68)
    print(" Настройка секретов awg-bot")
    print("=" * 68)
    db = Database(config.DB_PATH)
    db.init_schema()

    env_updates = setup_backup(read_env(ENV_PATH))
    if env_updates:
        write_env(ENV_PATH, env_updates)
        print("\nОбновлён .env:", ", ".join(sorted(env_updates)))
        _restart_bot_if_running()


def _restart_bot_if_running() -> None:
    """Подхватить новый .env: если awg-bot стоит сервисом и активен — рестартуем
    сами (не заставляя пользователя лезть в systemctl); иначе (первичная
    настройка, сервиса ещё нет) — просто подсказываем команду."""
    import shutil
    import subprocess
    if shutil.which("systemctl") is None:
        return
    active = subprocess.run(["systemctl", "is-active", "--quiet", "awg-bot"]).returncode == 0
    if not active:
        print("Изменения подхватятся при старте бота "
              "(вручную: systemctl restart awg-bot).")
        return
    print("Перезапускаю awg-bot, чтобы подхватил изменения…")
    r = subprocess.run(["systemctl", "restart", "awg-bot"])
    print("✓ awg-bot перезапущен." if r.returncode == 0
          else "Не удалось перезапустить автоматически — вручную: systemctl restart awg-bot")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
