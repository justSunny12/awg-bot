"""
Облегчённый визард установки: один вопрос, опознание админа кодом, файервол
одной строкой, конфигурация первого устройства в терминал (ROADMAP п.8).

Установщик запускают ровно один раз, на чужом хосте, и ошибка в нём видна
только там. Проверяем чтением то, что чтением проверяется.
"""
from __future__ import annotations

import pathlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def script() -> str:
    return (ROOT / "awg-bot.sh").read_text(encoding="utf-8")


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
    """Экран шифрования бэкапов есть в боте. В визарде это был бы вопрос ровно
    там, где человек ещё не знает, что такое бэкап этого бота."""
    body = script.split("setup_secrets() {", 1)[1].split("\n}\n", 1)[0]
    questions = [m.group(0) for m in re.finditer(r'(?:confirm|ask \w+) "[^"]+"', body)]
    assert questions, "секреты без единого вопроса — визард сломан"
    assert all(("BOT_TOKEN" in q) or ("ADMIN_ID" in q) or ("Telegram ID" in q) for q in questions), \
        f"в setup_secrets лишние вопросы: {questions}"


def test_admin_is_identified_by_a_code_with_a_manual_fallback(script):
    """Числовой Telegram ID у человека под рукой не лежит — за ним идут к
    стороннему боту. На основном пути визард ID не спрашивает: опознание кодом
    через самого Telegram. Ручной вопрос остаётся запасным — под --advanced и
    когда код не дождались (Ctrl+C, нет сети)."""
    body = script.split("setup_secrets() {", 1)[1].split("\n}\n", 1)[0]
    assert "pair_admin" in body
    assert "@userinfobot" in body, "ручной запасной путь убирать нельзя"
    assert body.index('ADVANCED" != "1"') < body.index("pair_admin") < body.index("@userinfobot")
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


# ── применение правил файервола: подтверждение в чате, а не в SSH ────────────

# Поведение таймера и отката проверяется вызовами — tests/unit/test_firewall_cli.py.

def test_chat_buttons_match_what_the_bot_handles():
    """Кнопка из другого процесса обязана попасть в тот же обработчик, что и
    кнопка, нарисованная ботом: разойдись формат — нажатие уходит в никуда."""
    from awgbot.bot.callbacks import SetCB
    from tools.firewall import _fw_cb
    # Бот эти кнопки больше не рисует (из чата таймера нет), но их колбэки
    # по-прежнему ведёт _firewall_action по ключам confirm / rollback.
    assert _fw_cb("confirm") == SetCB(sec="fw", act="do", key="confirm").pack()
    assert _fw_cb("rollback") == SetCB(sec="fw", act="do", key="rollback").pack()


def test_tgsend_sends_html_with_one_button_per_row(monkeypatch):
    """Сломайся сериализация кнопок — сообщение про откат придёт БЕЗ них, когда
    SSH уже отрезан, и подтверждать будет нечем."""
    import json
    import urllib.parse
    import urllib.request
    from awgbot.core import config
    from awgbot.infra import tgsend
    seen: dict = {}

    class Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"ok": true, "result": {}}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["params"] = dict(urllib.parse.parse_qsl(req.data.decode()))
        return Resp()

    monkeypatch.setattr(config, "BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "ADMIN_ID", 777)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert tgsend.send("<b>тест</b>", buttons=[("Да", "set:fw:do:confirm:"),
                                               ("Нет", "set:fw:do:rollback:")]) is True
    assert seen["url"].endswith("/bot123:abc/sendMessage")
    p = seen["params"]
    assert p["chat_id"] == "777" and p["parse_mode"] == "HTML" and p["text"] == "<b>тест</b>"
    rows = json.loads(p["reply_markup"])["inline_keyboard"]
    assert rows == [[{"text": "Да", "callback_data": "set:fw:do:confirm:"}],
                    [{"text": "Нет", "callback_data": "set:fw:do:rollback:"}]]


def test_tgsend_swallows_network_errors(monkeypatch):
    """Отправка из установщика не должна ронять установку."""
    import urllib.error
    import urllib.request
    from awgbot.core import config
    from awgbot.infra import tgsend

    def boom(req, timeout=None):
        raise urllib.error.URLError("нет сети")

    monkeypatch.setattr(config, "BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "ADMIN_ID", 777)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert tgsend.send("что-то") is False


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
    assert 'rm -f "$INSTALL_DIR/install/awg-bot-install.sh"' not in bootstrap, \
        "установщик остаётся в /opt: из него бот собирает поставку для шлюза"


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


def _run_cleanup(tmp_path, what, tgz=""):
    """Прогоняет cleanup_delivery настоящим bash: это rm -rf, и проверять его
    сверкой подстрок — ровно тот случай, когда тест зелёный, а каталог снесён."""
    import subprocess
    script = (ROOT / "awg-bot.sh").read_text(encoding="utf-8")
    body = script.split("cleanup_delivery() {", 1)[1].split("\n}\n", 1)[0]
    prog = (
        'log(){ printf "[log] %s\\n" "$*"; }\n'
        'cleanup_delivery() {' + body + '\n}\n'
        f'cleanup_delivery "{what}" "{tgz}"\n'
    )
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True)


def test_temp_directory_is_removed(tmp_path):
    """Поставка распакована во временный каталог — после установки от неё не
    должно остаться ничего: внутри и код, и установщик, и архив с ключами."""
    d = pathlib.Path("/tmp") / f"awg-bot-install.{tmp_path.name}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "awgbot").mkdir(exist_ok=True)
    tgz = d / "awg-bot.tgz"
    tgz.write_text("архив", encoding="utf-8")
    res = _run_cleanup(tmp_path, str(d), str(tgz))
    assert res.returncode == 0, res.stderr
    assert not d.exists(), "временный каталог остался"


def test_a_home_directory_is_never_removed(tmp_path):
    """Человек мог распаковать поставку и к себе домой — удалять её там мы не
    вправе, и молчать об этом тоже нельзя."""
    d = tmp_path / "awg-bot-delivery"
    (d / "awgbot").mkdir(parents=True)
    res = _run_cleanup(tmp_path, str(d))
    assert res.returncode == 0, res.stderr
    assert d.exists(), "снесли каталог вне /tmp"
    assert "не временный" in res.stdout


def test_cleanup_removes_the_installer_file_and_the_archive(tmp_path):
    """Старый путь: рядом лежат скачанный установщик и архив."""
    inst = tmp_path / "awg-bot-install.sh"
    inst.write_text("#!/bin/sh\n", encoding="utf-8")
    tgz = tmp_path / "awg-bot.tgz"
    tgz.write_text("архив", encoding="utf-8")
    res = _run_cleanup(tmp_path, str(inst), str(tgz))
    assert res.returncode == 0 and not inst.exists() and not tgz.exists()


def test_cleanup_never_fails_the_installation(tmp_path):
    """Уборка идёт последним шагом уже успешной установки: её отказ не должен
    ронять результат под `set -e`."""
    res = _run_cleanup(tmp_path, str(tmp_path / "которого-нет"), str(tmp_path / "тоже-нет"))
    assert res.returncode == 0, res.stderr


# ── установка одной командой ─────────────────────────────────────────────────

def test_bootstrap_downloads_the_delivery_when_piped(bootstrap):
    """По ссылке едет не архив, а этот скрипт: curl отдаёт его в sudo bash, а
    поставку он качает себе сам. Иначе «одна команда» превращается в четыре."""
    assert "PIPED=1" in bootstrap and '-f "${BASH_SOURCE[0]:-}"' in bootstrap
    piped = bootstrap.split('if [[ "$PIPED" -eq 1', 1)[1].split("\nelif", 1)[0]
    assert "mktemp -d /tmp/awg-bot-install." in piped
    assert "curl -fsSL" in piped and "tar xzf" in piped
    assert piped.count('rm -rf "$SRC_ROOT"') >= 3, "на каждом отказе временное убирается"
    assert "releases/latest/download/awg-bot.tgz" in bootstrap


def test_piped_bootstrap_hands_over_to_the_installer_from_the_delivery(bootstrap):
    """Логика установки принадлежит ПОСТАВКЕ и едет вместе с ней. Выполняйся
    на хосте установщик из ветки main, а код ставься из релиза — эти двое
    разъезжались бы молча, и отладка начиналась бы с вопроса «а чей это код?»."""
    piped = bootstrap.split('if [[ "$PIPED" -eq 1', 1)[1].split("\nelif", 1)[0]
    assert 'bash "$SRC_ROOT/install/awg-bot-install.sh"' in piped
    assert "--skip-verify" in piped, "второй запрос к API ни к чему: sha256 уже сверен"
    assert piped.index('verify_archive "$TGZ"') < piped.index('bash "$SRC_ROOT'), \
        "код из архива запускается до сверки целостности"
    assert 'ORIG_ARGS[@]' in piped, "аргументы (--role gateway и прочее) обязаны доехать"
    # каталог создали мы — мы и убираем, если установка сорвалась; через exec
    # ловушку не унести, а отказ бывает до того, как установщик что-то поймёт
    assert "exec bash" not in piped and "__rc" in piped
    assert piped.index("__rc=$?") < piped.index('rm -rf "$SRC_ROOT" && log')


def test_every_question_is_read_from_the_terminal():
    """stdin занят текстом скрипта: обычный read «прочитал» бы его вместо
    ответа и молча ушёл по умолчанию — с токеном бота из случайной строки кода."""
    for path in ((ROOT / "awg-bot.sh"), (ROOT / "install" / "awg-bot-install.sh")):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            if not re.search(r"\bread\b.*-p ", line):
                continue
            assert "/dev/tty" in line, f"{path.name}: вопрос читает stdin, а не терминал: {line.strip()}"


def test_bootstrap_without_root_prints_the_whole_command(bootstrap):
    """Из трубы «$0» — это «bash», и совет «sudo bash» выглядел бы издевательством."""
    body = bootstrap.split('if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "curl -fsSL" in body and "| sudo bash" in body
    assert 'die "нужен root: sudo $0 $*"' in body, "для файла остаётся короткий совет"


# ── шлюз: установка без единого вопроса ──────────────────────────────────────

def test_gateway_install_takes_secrets_from_the_bundle(script):
    """Файл первого применения несёт всё: ключи линка, конфиг аплинка, список
    устройств админа, токен агента и ADMIN_ID. Значит спрашивать на шлюзе
    нечего — есть файл или его нет."""
    body = script.split('if [[ "$role" == "gateway" ]]; then', 1)[1].split("\n    fi\n", 1)[0]
    assert "find_gw_bundle" in body and "gw_bundle_secrets" in body
    assert "apply_gw_bundle" in body
    assert '[[ "$from_bundle" -eq 1 ]] || setup_secrets' in body, \
        "с файлом вопросы про секреты не задаются"
    assert body.index("ensure_awg_kernel") < body.index("apply_gw_bundle"), \
        "бандл применяется до ядра — awg-quick ещё нет"
    assert body.index("apply_gw_bundle") < body.index("build_venv"), \
        "агенту нужен уже поднятый аплинк: без него Telegram недоступен"


def test_gateway_install_without_a_bundle_says_exactly_what_to_do(script):
    """Тупик «нет файла» обязан кончаться двумя командами, а не советом
    почитать документацию."""
    body = script.split('if [[ "$role" == "gateway" ]]; then', 1)[1].split("\n    fi\n", 1)[0]
    stop = body.split('elif [[ -z "$(env_get BOT_TOKEN)" ]]; then', 1)[1].split("fi", 1)[0]
    assert "scp awg-gw-bundle.sh" in stop and "--install" in stop
    assert "Назначить шлюз" in stop


def test_bundle_secrets_are_read_as_data_not_executed(script):
    """Строки лежат в бандле после exec: их читают sed'ом, а не исполняют."""
    body = script.split("gw_bundle_secrets() {", 1)[1].split("\n}\n", 1)[0]
    assert "sed -nE" in body and "AGENT_BOT_TOKEN" in body and "AGENT_ADMIN_ID" in body
    assert "source" not in body and ". \"$f\"" not in body
    assert "env_set BOT_TOKEN" in body and "env_set ADMIN_ID" in body


def test_bundle_is_searched_where_scp_puts_it(script):
    body = script.split("find_gw_bundle() {", 1)[1].split("\n}\n", 1)[0]
    assert "/root/awg-gw-bundle.sh" in body, "инструкция бота кладёт файл именно туда"
    assert "SUDO_USER" in body and '$PWD/awg-gw-bundle.sh' in body
    assert '[[ -n "$GW_BUNDLE" ]]' in body, "явный --bundle важнее поиска"


def test_bundle_flag_travels_through_the_bootstrap(bootstrap):
    assert "--bundle) " in bootstrap and "EXTRA+=(--bundle" in bootstrap


def test_gateway_install_finish_line_matches_how_the_admin_was_identified(script):
    """Опознание кодом: админ боту уже писал, агент пришлёт панель сам. Из
    файла первого применения диалога нет, первым бот написать не может —
    «напиши /start». Одно «Готово» на оба случая врало в одном из них."""
    body = script.split('if [[ "$role" == "gateway" ]]; then', 1)[1].split("\n    fi\n", 1)[0]
    tail = body.split("cleanup_delivery", 1)[1]
    assert 'if [[ "$from_bundle" -eq 1 ]]' in tail
    assert "напиши /start" in tail and "напишет сам" in tail


def test_install_from_the_bundle_leaves_the_first_start_marker_where_the_agent_looks(script):
    """Установка по файлу первого применения оставляет метку рядом с базой
    агента: по ней первый запуск говорит серверу «установлен», даже если база
    от прежней установки уцелела. Разойдись имя или каталог — метку никто не
    увидит, а файл с ключом линка так и провисит в чате сервера."""
    from awgbot.runtime import main
    body = script.split('if [[ "$role" == "gateway" ]]; then', 1)[1].split("\n    fi\n", 1)[0]
    ok_branch = body.split("if gw_bundle_secrets", 1)[1].split("else", 1)[0]
    assert f': > "$DATA_DIR/{main.FRESH_INSTALL_MARKER}"' in ok_branch, ok_branch
    assert 'DATA_DIR="/var/lib/awg-bot"' in script
    assert "Environment=AWG_BOT_DATA_DIR=$DATA_DIR" in script, "база агента — не в том каталоге, где метка"
