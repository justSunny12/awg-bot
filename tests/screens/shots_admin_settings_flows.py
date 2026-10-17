"""Снимки админа: настройки до итога — мастер почты, бэкапы и восстановление из файла, обновления."""
from __future__ import annotations

import json

from awgbot.bot.callbacks import SetCB, UpdateCB

from tests.screens.base import Shot
from tests.screens.shots_admin_common import (_email_on, _fw, _people, _raise, _restarts, _upd_found,
                                              _upd_hold, _upd_offline, _upd_silent, _upd_skipped,
                                              _upd_waits)


# ── шаг 3: диалоги до конца, файл копии, события без действия ───────────────

_KEY = bytes(range(32))     # случайный ключ прежней схемы: без KDF фразы, как в снимках шлюза


def _db_bytes() -> bytes:
    """Пустая база боевой схемы — база внутри бэкапа для плана."""
    import os
    import tempfile
    from awgbot.infra.db import Database
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        db.init_schema()
        db.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        for x in (path, path + "-wal", path + "-shm"):
            if os.path.exists(x):
                os.remove(x)


def _archive(meta: dict, *, env: str = "", db: bytes | None = None) -> bytes:
    """Резервная копия: метка внутри архива, env с ADMIN_ID и база по желанию."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json", json.dumps(meta).encode())
        if env:
            add("state/env", env.encode())
        if db is not None:
            add("state/bot.db", db)
    return buf.getvalue()


def _enc(raw: bytes) -> bytes:
    from awgbot.util import secrets_util
    return secrets_util.encrypt(raw, key=_KEY)


def _meta(**kw) -> dict:
    m = {"schema": 2, "role": "main", "created_at": "2026-10-05T14:00:00+03:00", "version": "3.4.0",
         "awg_gen": 1, "db_line": "", "gateways": []}
    m.update(kw)
    return m


_BK_OPEN = _archive(_meta())
_BK_GW = _enc(_archive({"role": "gw", "created_at": "2026-09-01T03:00:00+05:00"}))
_BK_OLD = _enc(_archive({"role": "main", "created_at": "2026-09-01T03:00:00+05:00", "version": "3.3.1"}))
_BK_NEWER = _enc(_archive(_meta(version="3.5.0")))
_BK_ADMIN = _enc(_archive(_meta(), env="ADMIN_ID=4242\n"))
_BK_PLAN = _enc(_archive(_meta(), db=_db_bytes()))
_BK_NAME = "awg-bot-backup-main-20261005_140000.tgz.enc"


def _restore_ready(services, mp):
    """Восстановление из файла в чате: секрет задан, версия кода — 3.4.0;
    раскладка на диск и запуск awg-bot restore — заглушки (они останавливают
    сервис на хосте)."""
    import base64
    from awgbot.core import config
    who = _people(services)
    mp.setattr(config, "INSTALLED_VERSION", "3.4.0")
    services.db.set_state(services._BK_KEY_KEY, base64.b64encode(_KEY).decode())
    services.prepare_restore = lambda blob: "/nonexistent/DUMMY-restore-pending.tgz.enc"
    services.launch_restore = lambda path: None
    return who


def _slot(n, case, name, label=""):
    return {"slot": n, "case": case, "name": name, "label": label, "link_if": "awglink" if n == 1 else f"awglink{n}"}


def _plan(**plan):
    """Вопрос с планом: осмотр файла настоящий (фраза, метка), план — сцена."""
    def build(services, mp):
        who = _restore_ready(services, mp)
        real = services.inspect_backup

        def inspect(blob, name=""):
            info = real(blob, name)
            if info.get("ok"):
                info["plan"] = {"line": "same", "empty": False, "slots": [], "replaced": [0, 0],
                                "expired_now": 0, "host_copy": "", "host_cur": "", "first_slot_back": False,
                                "created_at": info["created_at"], **plan}
            return info
        services.inspect_backup = inspect
        return who
    return build


_SAME_SLOTS = [_slot(1, "same", "NASPi"), _slot(2, "rekeyed", "Pi4", "дача"), _slot(3, "device", "Pi3"),
               _slot(4, "removed", "Шлюз 4"), _slot(5, "added", "Pi5"), _slot(6, "twin", "Pi6")]


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
    # ── восстановление из файла в чате: вопрос с планом ─────────────────────
    Shot("adm.restore", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)], data=_plan(),
         title="бэкап в чате: та же линия, шлюзов нет"),
    Shot("adm.restore.slots", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)], data=_plan(slots=_SAME_SLOTS),
         title="та же линия: шлюзы по каждому случаю"),
    Shot("adm.restore.host", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)],
         data=_plan(slots=_SAME_SLOTS[:1], host_copy="old.example.org", host_cur="vpn.example.org"),
         title="адрес сервера отличается от бэкапа"),
    Shot("adm.restore.clean", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)],
         data=_plan(line="clean", empty=True, expired_now=3, host_copy="vpn.example.org",
                    slots=[_slot(1, "restored", "NASPi"), _slot(2, "restored", "Pi4", "дача")]),
         title="новый сервер: шлюзы из бэкапа, подписки истекли за время без сервера"),
    Shot("adm.restore.unreadable", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)],
         data=_plan(line="unreadable", first_slot_back=True,
                    slots=[_slot(1, "same", "NASPi"), _slot(2, "rekeyed", "Pi4", "дача"),
                           _slot(3, "removed", "Pi3")]),
         title="текущая база не читается: шлюзы по файлам линков"),
    Shot("adm.restore.foreign", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)],
         data=_plan(line="foreign", replaced=[12, 31],
                    slots=[_slot(1, "added", "NASPi"), _slot(2, "removed", "Pi4")]),
         title="бэкап другого сервера на сервер со своими данными"),
    Shot("adm.restore.many", role="admin", steps=[("file", _BK_NAME, _BK_PLAN)],
         data=_plan(host_copy="old-server-name.example.org", host_cur="new-server-name.example.org",
                    slots=[_slot(n, ("same", "rekeyed", "device", "removed", "added", "twin")[n % 6],
                                 f"Шлюз с длинным именем {n}", f"подпись места номер {n}") for n in range(1, 16)]),
         title="лимит Telegram: 15 шлюзов в вопросе"),
    Shot("adm.restore.yes", role="admin",
         steps=[("file", _BK_NAME, _BK_PLAN), ("press", SetCB(sec="backup", act="do", key="restore!"))],
         data=_plan(), title="восстановление запущено"),
    Shot("adm.restore.drop", role="admin",
         steps=[("file", _BK_NAME, _BK_PLAN), ("press", SetCB(sec="backup", act="do", key="restore_drop"))],
         data=_plan(), title="файл отброшен"),
    Shot("adm.restore.junk", role="admin", steps=[("file", _BK_NAME, b"DUMMY")], data=_restore_ready,
         title="не резервная копия"),
    Shot("adm.restore.open", role="admin", steps=[("file", _BK_NAME, _BK_OPEN)], data=_restore_ready,
         title="бэкап не зашифрован"),
    Shot("adm.restore.gw", role="admin", steps=[("file", _BK_NAME, _BK_GW)], data=_restore_ready,
         title="бэкап шлюза прежних версий"),
    Shot("adm.restore.old", role="admin", steps=[("file", _BK_NAME, _BK_OLD)], data=_restore_ready,
         title="бэкап версии до 3.4.0"),
    Shot("adm.restore.newer", role="admin", steps=[("file", _BK_NAME, _BK_NEWER)], data=_restore_ready,
         title="бэкап новее кода"),
    Shot("adm.restore.admin", role="admin", steps=[("file", _BK_NAME, _BK_ADMIN)], data=_restore_ready,
         title="бэкап другого админа"),
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
    services.email_send_backup = lambda paths, caption="": None
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
