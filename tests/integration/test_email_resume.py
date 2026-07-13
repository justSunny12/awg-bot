def test_email_resume_flow(services, fake_awg):
    from awgbot.util import timeutil
    from awgbot.core.blocks import ClientBlock
    from datetime import datetime, timedelta
    end = timeutil.to_iso(datetime.now(timeutil.TZ) + timedelta(days=200))
    cid = services.db.create_client("Vac", 1, timeutil.now_iso(), end, "c", period_kind="year")
    services.db.activate_client("c", 555)
    services.add_device(cid, "phone")
    # вход в паузу → выдаётся код
    ok, reserved, notes, code = services.enter_pause(cid, 7)
    assert ok and code and len(code) == 8
    c = services.db.get_client(cid)
    assert int(c.block_reason) & int(ClientBlock.PAUSED)
    assert c.pause.resume_code == code
    # выход по коду
    ok2, notes2 = services.resume_by_email_code(code)
    assert ok2
    c2 = services.db.get_client(cid)
    assert not (int(c2.block_reason) & int(ClientBlock.PAUSED))   # пауза снята
    # код одноразовый — повторно не сработает
    ok3, _ = services.resume_by_email_code(code)
    assert not ok3
    # неизвестный код — молчок
    ok4, _ = services.resume_by_email_code("ZZZZZZZZ")
    assert not ok4


def test_poll_once_disabled_returns_zero(monkeypatch):
    """Фича выключена (нет кредов) → poll_once ничего не делает."""
    from awgbot.infra import email_resume
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "EMAIL_RESUME_ENABLED", False)
    assert email_resume.poll_once(lambda code: True) == 0


def test_decode_subject_plain_and_mime():
    from awgbot.infra import email_resume as er
    assert er._decode_subject("AbCd2345") == "AbCd2345"
    # MIME-encoded (=?UTF-8?...?=) должен раскодироваться
    assert er._decode_subject("=?utf-8?B?QWJDZDIzNDU=?=") == "AbCd2345"


def test_generate_code_safe_alphabet():
    from awgbot.infra import email_resume as er
    for _ in range(50):
        code = er.generate_code()
        assert len(code) == 8
        assert not (set(code) & set("0O1lI"))


def test_poll_once_matches_code_and_marks_seen(monkeypatch):
    """poll_once: непрочитанное письмо с кодом в теме → on_code вызван, письмо
    помечено \\Seen, при успехе шлётся ответ."""
    from awgbot.infra import email_resume as er
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "EMAIL_RESUME_ENABLED", True)
    monkeypatch.setattr(cfg, "EMAIL_RESUME_LOGIN", "box@icloud.com")
    monkeypatch.setattr(cfg, "EMAIL_RESUME_PASSWORD", "app-pass")

    stored, replied = [], []

    class FakeIMAP:
        # письмо лежит в INBOX; папки «спам» существуют, но пусты
        _MSG = {"INBOX": [b"1"]}

        def __init__(self, *a, **k):
            self._box = None
        def login(self, u, p): assert u == "box@icloud.com"
        def select(self, box):
            self._box = box
            return "OK", [b"1"]
        def search(self, charset, criterion):
            assert criterion == "UNSEEN"
            return "OK", self._MSG.get(self._box, [b""])
        def fetch(self, num, spec):
            raw = b"Subject: AbCd2345\r\nFrom: user@example.com\r\n\r\n"
            return "OK", [(b"1 (BODY[HEADER] {}", raw)]
        def store(self, num, flag, val):
            stored.append((num, val))
        def logout(self): pass

    monkeypatch.setattr(er.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(er, "send_success_reply", lambda to: replied.append(to))

    seen_codes = []
    def on_code(code):
        seen_codes.append(code)
        return code == "AbCd2345"

    accepted = er.poll_once(on_code)
    assert accepted == 1
    assert seen_codes == ["AbCd2345"]
    assert stored and stored[0][1] == "\\Seen"        # помечено прочитанным
    assert replied == ["user@example.com"]            # ответ отправлен


def test_poll_once_finds_code_in_spam(monkeypatch):
    """Письмо с кодом попало в «спам» (не INBOX) — poll_once его всё равно
    находит и обрабатывает. Несуществующие папки молча пропускаются."""
    from awgbot.infra import email_resume as er
    import awgbot.core.config as cfg
    import imaplib
    monkeypatch.setattr(cfg, "EMAIL_RESUME_ENABLED", True)
    monkeypatch.setattr(cfg, "EMAIL_RESUME_LOGIN", "box@icloud.com")
    monkeypatch.setattr(cfg, "EMAIL_RESUME_PASSWORD", "app-pass")

    class FakeIMAP:
        # INBOX пуст; письмо в «Junk»; часть кандидатов-папок не существует
        _EXISTS = {"INBOX", "Junk"}
        _MSG = {"Junk": [b"7"]}

        def __init__(self, *a, **k):
            self._box = None
        def login(self, u, p): pass
        def select(self, box):
            if box not in self._EXISTS:
                raise imaplib.IMAP4.error("no such mailbox")
            self._box = box
            return "OK", [b"1"]
        def search(self, charset, criterion):
            return "OK", self._MSG.get(self._box, [b""])
        def fetch(self, num, spec):
            raw = b"Subject: AbCd2345\r\nFrom: user@example.com\r\n\r\n"
            return "OK", [(b"7 (BODY[HEADER] {}", raw)]
        def store(self, num, flag, val): pass
        def logout(self): pass

    monkeypatch.setattr(er.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(er, "send_success_reply", lambda to: None)

    accepted = er.poll_once(lambda code: code == "AbCd2345")
    assert accepted == 1                              # найдено в «спам»
