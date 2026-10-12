"""Снимки админа: настройки до итога — мастер почты, бэкапы и восстановление из файла, обновления."""
from __future__ import annotations

import json

from awgbot.bot.callbacks import SetCB, UpdateCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (_email_on, _fw, _people, _raise, _restarts, _upd_found,
                                              _upd_hold, _upd_offline, _upd_silent, _upd_skipped,
                                              _upd_waits)


# ── шаг 3: диалоги до конца, файл копии, события без действия ───────────────

def _archive(role: str = "main", db: bytes | None = None) -> bytes:
    """Резервная копия как её собирает make_backup: метка роли и даты, база —
    по желанию (без базы проверять схему нечего)."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json",
            json.dumps({"role": role, "created_at": "2026-09-01T03:00:00+03:00"}).encode())
        if db is not None:
            add("state/bot.db", db)
    return buf.getvalue()


def _old_db_bytes() -> bytes:
    """База со схемой ниже 3.2.0: в clients нет колонки kind."""
    import os
    import sqlite3
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE clients(id INTEGER PRIMARY KEY, tg_name TEXT);"
                          "CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER);")
        con.commit()
        con.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


_BK_OK = _archive()
_BK_GW = _archive("gw")
_BK_OLD = _archive(db=_old_db_bytes())
_BK_NAME = "awg-bot-backup-2026-09-01.tgz"


def _restore_ready(services):
    """Восстановление из файла в чате: раскладка на диск и запуск awg-bot
    restore — заглушки (они останавливают сервис на хосте)."""
    who = _people(services)
    services.prepare_restore = lambda plain: "/nonexistent/DUMMY-restore.tgz"
    services.launch_restore = lambda path: None
    return who


def _mail_check(ok: bool):
    """Мастер почты: вход по IMAP и SMTP — без сети (подменены сами проверки
    протоколов, итог запоминает настоящий email_check), удачный или отказ."""
    def build(services, mp):
        from awgbot.infra import mail
        who = _people(services)

        def imap(acc):
            if not ok:
                raise mail.MailError("IMAP: [AUTHENTICATIONFAILED] Invalid credentials")
        mp.setattr(mail, "check_imap", imap)
        mp.setattr(mail, "check_smtp", lambda acc: None)
        return who
    return build


_MAIL_KNOWN = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "admin@gmail.com")]
_MAIL_OWN = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "admin@example.org"),
             ("text", "imap.example.org:993")]
_PHRASE = [("press", SetCB(sec="backup", act="do", key="enc_set")), ("text", "DUMMY-passphrase-1234")]


# ── шаг 3: диалоги до итога ─────────────────────────────────────────────────
DIALOGS = [
    Shot("adm.set.email.saved", role="admin", steps=_MAIL_KNOWN + [("text", "DUMMY-app-password")],
         data=_mail_check(True), title="мастер почты: ящик подключён"),
    Shot("adm.set.email.refused", role="admin", steps=_MAIL_KNOWN + [("text", "DUMMY-app-password")],
         data=_mail_check(False), title="мастер почты: вход не прошёл"),
    Shot("adm.set.email.smtp", role="admin", steps=_MAIL_OWN, data=_mail_check(True),
         title="мастер почты: SMTP-сервер"),
    Shot("adm.set.email.password", role="admin", steps=_MAIL_OWN + [("text", "smtp.example.org:465")],
         data=_mail_check(True), title="мастер почты: пароль своего сервера"),
    Shot("adm.set.email.own.saved", role="admin",
         steps=_MAIL_OWN + [("text", "smtp.example.org:465"), ("text", "DUMMY-password")],
         data=_mail_check(True), title="мастер почты: свой сервер подключён"),
    Shot("adm.set.backup.enc_set.done", role="admin", steps=_PHRASE + [("text", "DUMMY-passphrase-1234")],
         data=_people, title="фраза совпала — шифрование включено"),
    Shot("adm.set.backup.enc_set.mismatch", role="admin", steps=_PHRASE + [("text", "DUMMY-passphrase-9999")],
         data=_people, title="фраза не совпала — заново"),
]


RESTORE = [
    # ── A190: восстановление из файла в чате ────────────────────────────────
    Shot("adm.restore", role="admin", steps=[("file", _BK_NAME, _BK_OK)], data=_restore_ready,
         title="копия в чате: восстановить?"),
    Shot("adm.restore.yes", role="admin",
         steps=[("file", _BK_NAME, _BK_OK), ("press", SetCB(sec="backup", act="do", key="restore!"))],
         data=_restore_ready, title="восстановление запущено"),
    Shot("adm.restore.drop", role="admin",
         steps=[("file", _BK_NAME, _BK_OK), ("press", SetCB(sec="backup", act="do", key="restore_drop"))],
         data=_restore_ready, title="файл отброшен"),
    Shot("adm.restore.junk", role="admin", steps=[("file", _BK_NAME, b"DUMMY")], data=_restore_ready,
         title="не резервная копия"),
    Shot("adm.restore.gw", role="admin", steps=[("file", _BK_NAME, _BK_GW)], data=_restore_ready,
         title="копия агента шлюза"),
    Shot("adm.restore.old", role="admin", steps=[("file", _BK_NAME, _BK_OLD)], data=_restore_ready,
         title="копия со схемой ниже 3.2.0"),
]


# ── шаг 5: разделы настроек — ввод, почта, бэкапы, обновления, сервис ───────

def _mailbox(services):
    """Ящик сохранён мастером, вход ещё не проверялся."""
    who = _people(services)
    services.email_save("box@icloud.com", "DUMMY", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    return who


def _mail_fails(services):
    """Ящик подключён, но вход не проходит (итог записан, как пишет настоящая
    проверка), а тест-письмо отвергает SMTP; причины — с угловыми скобками."""
    from awgbot.infra import mail
    from awgbot.util import timeutil
    who = _email_on(services)

    def check(acc=None):
        services.db.set_state(services._MAIL_CHECK_KEY, f"fail|{timeutil.now_iso()}|IMAP: <auth> отказ")
        return False, "IMAP: <auth> отказ"

    def send():
        raise mail.MailError("SMTP 535 <bad>")
    services.email_check = check
    services.email_send_test = send
    return who


def _upd_scan_failed(services):
    """Проверка обновлений не дошла до списка релизов (сеть)."""
    who = _people(services)
    del services.update_scan                    # настоящая: признак сбоя ставит update_next

    def nxt():
        services.update_scan_failed = True
    services.update_next = nxt
    return who


def _upd_blocked(services):
    """Следующая версия найдена, но ставить её сейчас нельзя."""
    who = _upd_found(services)
    services.update_block_reason = lambda f: "идёт переезд на поколение 2"
    return who


def _upd_huge(services):
    """Список изменений следующей версии не влезает в сообщение."""
    import types
    who = _upd_found(services)
    found = types.SimpleNamespace(tag="v1.3.0", name="v1.3.0", url="https://example.org/r/v1.3.0",
                                  body="\n".join(f"- пункт номер {i} с подробным текстом" for i in range(150)))
    services.update_scan = lambda: found
    services.update_next = lambda: found
    return who


def _awg_restart_fails(services):
    who = _restarts(services)
    services.restart_service = _raise("runtime", "docker: <no such container>")
    return who


_MAIL_CORP = [("press", SetCB(sec="email", act="do", key="setup")), ("text", "box@corp.example")]
_BACKUP_WHEN = [("press", SetCB(sec="backup", act="edit", key="backup_when"))]


SECTIONS = [
    Shot("adm.set.mon.edit", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], data=_people,
         conf={"app.scheduler.monitor_minutes": 3}, title="частота опроса: приглашение с текущим и границами"),
    Shot("adm.set.mon.edit.bad", role="admin",
         press=[SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes")], text="0", data=_people,
         title="частота опроса: 0 — переспрос"),
    Shot("adm.set.mon.quiet", role="admin", press=[SetCB(sec="mon")], data=_people,
         conf={"app.scheduler.monitor_minutes": 3, "app.monitoring.alert_streak": 5,
               "app.monitoring.service_failure_alert_minutes": 5,
               "app.monitoring.service_failure_alert_loud": False},
         title="мониторинг: простой AWG — по правилам тихих часов"),
    Shot("adm.set.fw.add.many", role="admin", press=[SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow")],
         data=_fw(raw_allow=[f"203.0.113.{i}" for i in range(1, 8)], allow=[f"203.0.113.{i}" for i in range(1, 8)]),
         title="адрес: приглашение — пять текущих и «и ещё 2»"),
    Shot("adm.set.email.unchecked", role="admin", press=[SetCB(sec="email")], data=_mailbox,
         conf={"email.resume_enabled": False},
         title="e-mail: вход ещё не проверялся, аварийный выход выключен"),
    Shot("adm.set.email.bad_host", role="admin", steps=_MAIL_CORP + [("text", "imap.corp.example:0")],
         data=_people, title="мастер почты: негодный порт в «сервер:порт» — переспрос"),
    Shot("adm.set.email.bare_host", role="admin", steps=_MAIL_CORP + [("text", "imap.corp.example")],
         data=_people, title="мастер почты: голое имя сервера — шаг порта"),
    Shot("adm.set.email.check.fail", role="admin", press=[SetCB(sec="email", act="do", key="check")],
         data=_mail_fails, title="проверка соединения: отказ в шапке раздела"),
    Shot("adm.set.email.test.fail", role="admin", press=[SetCB(sec="email", act="do", key="test")],
         data=_mail_fails, title="тестовое письмо: отказ первой строкой раздела"),
    Shot("adm.set.email.cycle.poll", role="admin",
         press=[SetCB(sec="email", act="cycle", key="email.poll_interval_sec")], data=_email_on,
         title="опрос ящика циклом: 1 → 5 мин"),
    Shot("adm.set.email.cycle.code", role="admin",
         press=[SetCB(sec="email", act="cycle", key="email.resume_code_len")], data=_email_on,
         title="длина кода циклом: 8 → 12"),
    Shot("adm.set.backup.when.bad", role="admin", steps=_BACKUP_WHEN + [("text", "31 12")], data=_people,
         title="день и час: день за 28 — переспрос"),
    Shot("adm.set.upd.failed", role="admin", press=[SetCB(sec="upd")], data=_upd_scan_failed,
         title="обновления: проверка не удалась"),
    Shot("adm.set.upd.blocked", role="admin", press=[SetCB(sec="upd")], data=_upd_blocked,
         title="обновления: новая есть, но сейчас недоступна — строка, а не кнопка"),
    Shot("adm.set.upd.huge", role="admin", press=[SetCB(sec="upd")], data=_upd_huge,
         title="обновления: список изменений обрезан со ссылкой на релиз"),
    Shot("adm.set.upd.wait", role="admin", press=[SetCB(sec="upd")], data=_upd_waits("1.2.3"),
         title="обновления: актуальна, v1.3.0 ждёт шлюз — строка ожидания"),
    Shot("adm.set.upd.wait.many", role="admin", press=[SetCB(sec="upd")],
         data=_upd_waits("1.2.3", "1.2.0", "", "1.2.1"),
         title="обновления: ждёт четыре шлюза — три и «и ещё 1», версия неизвестна"),
    Shot("adm.set.upd.silent", role="admin", press=[SetCB(sec="upd")], data=_upd_silent,
         title="обновления: цель есть, выше ждёт шлюз, двое молчат — предупреждения"),
    Shot("adm.upd.install.wait", role="admin", press=[UpdateCB(action="install", tag="v1.3.0")],
         data=_upd_waits("1.2.3"),
         title="«⬆️ Обновить»: условие закрылось между проверкой и нажатием — alert с причиной"),
    Shot("adm.upd.install.changed", role="admin", press=[UpdateCB(action="install", tag="v1.2.9")],
         data=_upd_found, title="«⬆️ Обновить»: цель пересчитана другой — alert, установки нет"),
    Shot("adm.upd.install.old_button", role="admin", press=["upd:install"], data=_upd_found,
         title="«⬆️ Обновить» прежнего образца без тега — alert, установки нет"),
    Shot("adm.upd.install.offline", role="admin", press=[UpdateCB(action="install", tag="v1.3.0")],
         data=_upd_offline, title="«⬆️ Обновить»: GitHub не ответил — своя всплывашка"),
    Shot("adm.upd.install.hold", role="admin", press=[UpdateCB(action="install", tag="v1.3.0")],
         data=_upd_hold, title="«⬆️ Обновить» в окно ожидания после старта"),
    Shot("adm.set.upd.skipped", role="admin", press=[SetCB(sec="upd")], data=_upd_skipped,
         title="обновления: цель есть, v1.4.0 пропущен — метаданные не прочитаны"),
    Shot("adm.set.upd.hold", role="admin", press=[SetCB(sec="upd")], data=_upd_hold,
         title="обновления: окно ожидания после старта — жду шлюзы"),
    Shot("adm.set.svc.awg.fail", role="admin", press=[SetCB(sec="svc", act="do", key="awg!")],
         data=_awg_restart_fails, title="AWG не перезапущен — первой строкой раздела"),
]


# ── шаг 5: почта и бэкапы у основного — ветки из e2e ───────────────────────

def _backup_to_mail(services):
    """Бэкапы на e-mail: ящик подключён, шифрование включено; копия и письмо
    — без диска и сети."""
    who = _email_on(services)
    services.backup_set_passphrase("DUMMY-passphrase-1234")
    services.make_backup = lambda: ["/nonexistent/DUMMY-a.enc", "/nonexistent/DUMMY-b.enc"]
    services.email_send_backup = lambda paths: None
    return who


_SETUP = [("press", SetCB(sec="email", act="do", key="setup"))]


MAIL_BACKUP = [
    Shot("adm.set.email.setup.icloud", role="admin", steps=_SETUP + [("text", "box@icloud.com")],
         data=_people, title="мастер почты: iCloud — пароль приложения"),
    Shot("adm.set.email.port.bad", role="admin",
         steps=_MAIL_CORP + [("text", "imap.corp.example"), ("text", "99999")], data=_people,
         title="мастер почты: порт IMAP вне диапазона — переспрос"),
    Shot("adm.set.backup.now.email", role="admin", press=[SetCB(sec="backup", act="do", key="now")],
         data=_backup_to_mail, conf={"app.scheduler.backup_channel": "email"},
         title="копия сейчас — письмом на ящик"),
    Shot("adm.set.backup.enc_set.short", role="admin", steps=_PHRASE[:1] + [("text", "abc")], data=_people,
         title="фраза короче нужного — переспрос"),
]
