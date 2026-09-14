"""
Опознание админа кодом (tools/pair.py) и показ первого устройства в терминале
(tools/first_device.py) — поведением, а не чтением исходника.

Оба исполняются ровно один раз, на чужом хосте, в момент, когда Telegram у
человека ещё может не открываться.
"""
from __future__ import annotations

import re
import urllib.error

from tools import first_device as fd
from tools import pair


class _Api:
    """Подмена Telegram: очередь ответов getUpdates, запись отправленного."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, params=None, timeout=35):
        self.calls.append((method, dict(params or {})))
        if method == "getUpdates":
            return {"result": self.batches.pop(0) if self.batches else []}
        if method == "getMe":
            return {"result": {"username": "test_bot"}}
        return {"ok": True}


def _msg(update_id, text, user_id=555, name="Админ"):
    return {"update_id": update_id,
            "message": {"text": text, "from": {"id": user_id, "first_name": name}}}


def test_first_sender_of_the_code_becomes_admin(monkeypatch, capsys):
    api = _Api([[_msg(10, "привет")], [_msg(11, "ABCD-1234", user_id=777)]])
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setattr(pair, "api", api)
    assert pair.main(["--code", "ABCD-1234"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out == ["ADMIN_ID=777"], "в stdout должен уйти РОВНО id — его читает установщик"
    methods = [m for m, _ in api.calls]
    assert "sendMessage" in methods, "человеку не подтвердили, что код принят"
    # getUpdates при живом вебхуке всегда отвечает 409 — у повторной установки
    # это выглядело бы как «Telegram не отвечает»
    assert methods.index("deleteWebhook") < methods.index("getUpdates")
    # offset продвигается и КОММИТИТСЯ: иначе тот же код прилетит уже
    # запущенному боту, и первым в чате будет ответ на установочный код
    offsets = [p.get("offset") for m, p in api.calls if m == "getUpdates"]
    assert offsets[-1] == 12 and len(offsets) >= 3


def test_code_is_matched_ignoring_case_and_dashes(monkeypatch, capsys):
    api = _Api([[_msg(1, " abcd1234 ")]])
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setattr(pair, "api", api)
    assert pair.main(["--code", "ABCD-1234"]) == 0
    assert "ADMIN_ID=555" in capsys.readouterr().out


def test_foreign_messages_do_not_pair(monkeypatch):
    """Ошибётся один символ — никто не станет админом, а ожидание продолжится."""
    api = _Api([[_msg(1, "ABCD-1235")], [], []])
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setattr(pair, "api", api)
    monkeypatch.setattr(pair.time, "time", lambda _c=iter([0, 1, 2, 3, 999]): next(_c))
    assert pair.main(["--code", "ABCD-1234", "--timeout", "5"]) == 2


def test_rejected_token_gives_up_at_once(monkeypatch):
    def boom(token, method, params=None, timeout=35):
        raise urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    monkeypatch.setenv("BOT_TOKEN", "плохой")
    monkeypatch.setattr(pair, "api", boom)
    assert pair.main([]) == 1, "с неверным токеном ждать бессмысленно"


def test_network_blip_is_waited_out(monkeypatch, capsys):
    """Сеть моргнула — ждём дальше (с паузой), а не сдаёмся как на 401."""
    state = {"n": 0}
    good = _Api([[_msg(1, "ABCD-1234")]])

    def flaky(token, method, params=None, timeout=35):
        if method == "getUpdates" and state["n"] == 0:
            state["n"] += 1
            raise urllib.error.URLError("temporary failure")
        return good(token, method, params, timeout)

    naps = []
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setattr(pair, "api", flaky)
    monkeypatch.setattr(pair.time, "sleep", naps.append)
    assert pair.main(["--code", "ABCD-1234"]) == 0
    assert naps == [3], "после сбоя сети — пауза, а не немедленный повтор"
    assert "ADMIN_ID=555" in capsys.readouterr().out


def test_code_alphabet_has_no_lookalikes():
    assert not (set("01OIL") & set(pair.ALPHABET)), "0/O и 1/I/L в коде, который диктуют вслух"
    code = pair.make_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert pair.normalize(" ab-cd 12 ") == "ABCD12", "код сверяется без пробелов и дефисов"
    assert pair.make_code() != pair.make_code(), "код обязан быть случайным"


def test_token_comes_only_from_the_environment(monkeypatch):
    """Токен в аргументах виден в `ps` любому пользователю хоста — из argv он
    не читается, без переменной окружения — отказ."""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(pair, "api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("вызов API без токена")))
    assert pair.main([]) == 1
    assert pair.main(["--token", "123:abc", "123:abc"]) == 1


# ── первое устройство в терминале ────────────────────────────────────────────

def test_first_device_reports_missing_pieces(monkeypatch, tmp_path, capsys):
    """Три тупика, и каждый должен называться своими словами: базы нет,
    профиля нет, устройств нет."""
    from awgbot.core import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "нет.db")
    assert fd.main([]) == 1
    assert "не запускался" in capsys.readouterr().err


def test_first_device_prints_the_link_and_saves_the_conf(monkeypatch, tmp_path, capsys):
    from awgbot.core import config
    db_file = tmp_path / "bot.db"
    db_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "DB_PATH", db_file)

    class _Dev:
        id, name, address, private_key = 7, "Админ", "10.8.1.2", "priv"

    class _DB:
        def __init__(self, *a, **kw):
            pass
        def list_devices(self, cid):
            return [_Dev()]
        def close(self):
            pass

    class _Svc:
        def __init__(self, db):
            pass
        def admin_client(self):
            return type("C", (), {"id": 1})()
        def generate_config(self, did):
            return {"vpn": "vpn://AAAA", "conf": "[Interface]\\nPrivateKey = x\\n"}

    monkeypatch.setattr("awgbot.infra.db.Database", _DB)
    monkeypatch.setattr("awgbot.domain.services.Services", _Svc)
    monkeypatch.setattr(fd, "print_qr", lambda link: False)
    out_file = tmp_path / "first.conf"
    monkeypatch.setattr(fd, "Path", lambda p="": out_file if str(p).endswith(".conf") else __import__("pathlib").Path(p))
    assert fd.main(["--no-qr"]) == 0
    printed = capsys.readouterr().out
    assert "vpn://AAAA" in printed and "Админ" in printed and "10.8.1.2" in printed
    assert "в боте" in printed, "не сказано, что то же самое есть в чате"
    # файл с ключами — только root, и человеку сказано его убрать
    assert out_file.exists() and (out_file.stat().st_mode & 0o777) == 0o600
    assert "удали после импорта" in printed
    # ничего не создаётся: у подставных сервисов нет add_device, и вызов упал бы
