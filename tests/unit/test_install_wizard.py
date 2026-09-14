"""
Облегчённый визард установки: один вопрос, опознание админа кодом, файервол
одной строкой, конфигурация первого устройства в терминал (ROADMAP п.8).

Установщик запускают ровно один раз, на чужом хосте, и ошибка в нём видна
только там. Проверяем чтением то, что чтением проверяется.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def script() -> str:
    return (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pair_src() -> str:
    return (ROOT / "tools" / "pair.py").read_text(encoding="utf-8")


# ── вопросы визарда ──────────────────────────────────────────────────────────

def test_fresh_install_asks_nothing_about_topology(script):
    """Адрес, имя сервера и таймзона берутся сами: порт с подсетью уже записал
    ensure_awg_server, остальное правится в боте. --advanced возвращает старый
    визард целиком."""
    first = script.split('if [[ "$first_run" -eq 1 ]]; then', 1)[1].split("setup_secrets", 1)[0]
    assert "configure_topology_auto" in first and 'ADVANCED" == "1"' in first
    auto = script.split("configure_topology_auto() {", 1)[1].split("\n}\n", 1)[0]
    assert "ask " not in auto, "в автоматическом пути не должно быть вопросов"
    assert "ip route get" in auto and "timedatectl" in auto


def test_backup_encryption_is_not_asked_in_the_wizard(script):
    """Экран шифрования бэкапов есть в боте. В визарде это вопрос ровно там,
    где человек ещё не знает, что такое бэкап этого бота."""
    body = script.split("setup_secrets() {", 1)[1].split("\n}\n", 1)[0]
    assert "manage_secrets" in body and 'ADVANCED" == "1"' in body
    i_adv = body.index('ADVANCED" == "1" ]] && confirm "Настроить шифрование')
    assert i_adv < body.index("manage_secrets")


def test_admin_is_identified_by_a_code_with_a_manual_fallback(script):
    """Числовой Telegram ID у человека под рукой не лежит — за ним идут к
    стороннему боту. Спрашиваем не человека, а Telegram; не вышло — старый
    путь остаётся."""
    body = script.split("setup_secrets() {", 1)[1].split("\n}\n", 1)[0]
    assert "pair_admin" in body
    assert "@userinfobot" in body, "ручной запасной путь убирать нельзя"
    assert body.index("pair_admin") < body.index("@userinfobot")
    pair = script.split("pair_admin() {", 1)[1].split("\n}\n", 1)[0]
    assert "BOT_TOKEN=" in pair and "tools.pair" in pair
    assert 'sed -nE' in pair and "ADMIN_ID=" in pair


def test_firewall_is_one_question_with_three_answers(script):
    """«y / свой список / N», и ответ уже подставлен: адрес подключения система
    знает сама."""
    body = script.split("optional_steps() {", 1)[1].split("\n}\n", 1)[0]
    assert "SSH_CONNECTION" in body
    assert "[y / свой список адресов / N]" in body
    assert "--allow" in body and "--yes" in body, "мастер не должен переспрашивать"
    assert "--rollback-seconds 900" in body, "во время установки трёх минут мало"
    assert "read -r -p" in body and "/dev/tty" in body, "вопрос должен работать и в пайпе"


def test_first_device_is_printed_after_a_successful_start(script):
    """Telegram у человека может не открываться — он ровно за этим ставит VPN.
    Установка, кончающаяся словами «остальное в боте», оставляла бы его без
    способа туда попасть."""
    first = script.split('if [[ "$first_run" -eq 1 ]]; then', 1)[1].split("\n    else\n", 1)[0]
    assert "cmd_first_device" in first
    assert first.index('svc_state" == "active"') < first.index("cmd_first_device")
    assert "first-device) cmd_first_device" in script, "глагол не диспетчеризован"


# ── опознание админа ─────────────────────────────────────────────────────────

def test_pair_reads_the_token_from_the_environment(pair_src):
    """Токен в аргументах виден в `ps` любому пользователю хоста."""
    assert 'os.environ.get("BOT_TOKEN"' in pair_src
    assert not re.search(r"argv\[\d\].*token", pair_src, re.I)


def test_pair_code_alphabet_has_no_lookalikes():
    from tools import pair
    assert not (set("01OIL") & set(pair.ALPHABET)), "0/O и 1/I/L в коде, который диктуют вслух"
    code = pair.make_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert pair.normalize(" ab-cd 12 ") == "ABCD12", "код сверяется без пробелов и дефисов"
    assert pair.make_code() != pair.make_code(), "код обязан быть случайным"


def test_pair_prints_only_the_id_to_stdout(pair_src):
    """Установщик читает ровно одну строку; всё остальное — в stderr."""
    assert 'print(f"ADMIN_ID={frm[\'id\']}")' in pair_src
    body = pair_src.split("def main(", 1)[1]
    stdout_prints = [ln for ln in body.splitlines()
                     if ln.strip().startswith("print(") and "stderr" not in ln]
    assert len(stdout_prints) == 1, f"лишний вывод в stdout: {stdout_prints}"


def test_pair_deletes_the_webhook_before_long_polling(pair_src):
    """getUpdates при живом вебхуке всегда отвечает 409 — у повторной установки
    это выглядело бы как «Telegram не отвечает»."""
    body = pair_src.split("def main(", 1)[1]
    assert 'api(token, "deleteWebhook"' in body
    assert body.index('"deleteWebhook"') < body.index('"getUpdates"')


def test_pair_gives_up_on_a_rejected_token_but_waits_out_a_network_blip(pair_src):
    """401/404 — токен неверный, ждать бессмысленно; сеть моргнула — ждём."""
    assert "def fatal_token" in pair_src and "(401, 404)" in pair_src
    loop = pair_src.split("while time.time() < deadline:", 1)[1]
    assert "time.sleep(3)" in loop and "continue" in loop


# ── первое устройство в терминал ─────────────────────────────────────────────

def test_first_device_does_not_create_anything():
    """Устройство заводит бот; здесь только показ — иначе у админа появилось бы
    второе устройство при каждом запуске команды."""
    src = (ROOT / "tools" / "first_device.py").read_text(encoding="utf-8")
    assert "add_device" not in src and "bootstrap_admin_device" not in src
    assert "generate_config" in src


def test_first_device_qr_is_skipped_in_a_narrow_terminal(monkeypatch):
    from tools import first_device as fd
    monkeypatch.setattr(fd, "_terminal_width", lambda: 80)
    assert fd.print_qr("vpn://" + "A" * 900) is False
    monkeypatch.setattr(fd, "_terminal_width", lambda: 200)
    assert fd.print_qr("vpn://" + "A" * 900) is True


def test_first_device_file_is_root_only():
    src = (ROOT / "tools" / "first_device.py").read_text(encoding="utf-8")
    assert "0o600" in src and "удали после импорта" in src


# ── применение правил файервола: подтверждение в чате, а не в SSH ────────────

def test_arming_the_timer_sends_buttons_to_the_chat():
    """Правила могли отрезать именно этот SSH — «выполни confirm» отправляет
    человека туда, куда он уже не попадёт. Кнопка приходит в чат сама."""
    src = (ROOT / "tools" / "firewall.py").read_text(encoding="utf-8")
    arm = src.split("def _arm(", 1)[1].split("\ndef ", 1)[0]
    assert "tgsend.send" in arm and "_fw_cb(\"confirm\")" in arm and "_fw_cb(\"rollback\")" in arm
    assert "awg-bot firewall confirm" in arm, "запасной путь через CLI остаётся"
    assert arm.index("tgsend.send") < arm.index("awg-bot firewall confirm")


def test_rollback_tells_the_admin_it_happened():
    """Молчаливый откат — худший исход: человек уверен, что файервол включён,
    а он выключен. Вывод транзиентного юнита никто не читает."""
    src = (ROOT / "tools" / "firewall.py").read_text(encoding="utf-8")
    body = src.split("def cmd_rollback(", 1)[1].split("\ndef ", 1)[0]
    assert "tgsend.send" in body and "откат" in body.lower()


def test_chat_buttons_match_what_the_bot_handles():
    """Кнопка из другого процесса обязана попасть в тот же обработчик, что и
    кнопка, нарисованная ботом: разойдись формат — нажатие уходит в никуда."""
    from awgbot.bot import keyboards as kb
    from tools.firewall import _fw_cb
    st = {"rollback": True}
    drawn = [b.callback_data for row in kb.settings_firewall(st).inline_keyboard for b in row]
    assert _fw_cb("confirm") in drawn and _fw_cb("rollback") in drawn


def test_tgsend_never_raises_without_a_token(monkeypatch):
    """Отправка из установщика не должна ронять установку."""
    from awgbot.core import config
    from awgbot.infra import tgsend
    monkeypatch.setattr(config, "BOT_TOKEN", "")
    assert tgsend.send("что-то") is False
    monkeypatch.setattr(config, "BOT_TOKEN", "x:y")
    monkeypatch.setattr(config, "ADMIN_ID", 0)
    assert tgsend.send("что-то") is False


# ── поставка одним архивом ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def bootstrap() -> str:
    return (ROOT / "install" / "awg-bot-install.sh").read_text(encoding="utf-8")


def test_bootstrap_runs_from_an_unpacked_delivery(bootstrap):
    """Основной путь: архив распакован во временный каталог, установщик лежит
    внутри него. Распаковывать ещё раз нечего — код берём вокруг себя."""
    assert 'UNPACK_ROOT="$(cd "$SELF_DIR/.." && pwd)"' in bootstrap
    assert '-f "$UNPACK_ROOT/awgbot/__main__.py"' in bootstrap
    assert 'cp -a "$SRC_ROOT"/. "$INSTALL_DIR"/' in bootstrap
    assert 'rm -f "$INSTALL_DIR/install/awg-bot-install.sh"' in bootstrap, \
        "установщик в /opt не нужен: там уже awg-bot.sh"


def test_bootstrap_still_accepts_a_plain_archive(bootstrap):
    """Скрипт могли вытащить отдельно — прежний путь обязан работать."""
    assert "tar xzf \"$TGZ\" -C \"$INSTALL_DIR\"" in bootstrap
    assert "не та поставка?" in bootstrap


def test_archive_integrity_is_checked_but_never_blocks_on_network(bootstrap):
    """GitHub не ответил — ставим и говорим об этом: целостность загрузки и так
    держит TLS. Не совпало — отказываемся, но подсказываем свой путь."""
    body = bootstrap.split("verify_archive() {", 1)[1].split("\n}\n", 1)[0]
    assert "GitHub не ответил" in body and "return 0" in body
    assert "die" in body and "--skip-verify" in body
    assert "releases/tags/v$ver" in body, "сверяем с релизом ИМЕННО этой версии"


def test_temp_directory_is_removed_but_a_home_directory_is_not(bootstrap):
    """rm -rf в установщике — только по временным каталогам. Человек мог
    распаковать поставку и к себе домой; удалять её там мы не вправе."""
    assert "/tmp/*|/var/tmp/*|/private/tmp/*" in bootstrap
    script = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    body = script.split("cleanup_delivery() {", 1)[1].split("\n}\n", 1)[0]
    assert "/tmp/*|/var/tmp/*|/private/tmp/*" in body, "вторая проверка перед rm -rf"
    assert "не временный" in body
    assert body.index('case "$what" in') < body.index('rm -rf "$what"'), \
        "rm -rf стоит раньше проверки пути"
