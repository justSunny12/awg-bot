"""Снимки агента шлюза: файлы в чат — мастер восстановления, бэкапы и шифрование,
файлы конфигурации шлюза (отказ), восстановление из копии.

Шифрованный файл перевыпуска прежних выпусков — заглушка с подписью формата
(bundlecrypt.MAGIC): с 3.3.1 агент отказывает ему словами. Резервные копии — настоящие архивы,
зашифрованные случайным ключом (`backup_key` в БД): их разбирает настоящий
inspect_backup — метка роли, схема базы, конфиги интерфейсов.
"""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile

from awgbot.util import bundlecrypt, secrets_util
from awgbot.bot.callbacks import CancelCB, GwCB

from tests.screens.base import NOW
from tests.screens.shots_gateway_common import _BACKUP_KEY, _MAIL, _status
from tests.screens.shots_gateway_agent import _p, _shot, _t


# ── файлы в чат ──────────────────────────────────────────────────────────────

_BUNDLE = bundlecrypt.MAGIC + b"DUMMY-bundle"          # формат проверяется по подписи
_BUNDLE_NAME = "awg-gw-NASPi.bin"


def _archive(role: str = "gw", *, db: bytes | None = None, iface: bool = False) -> bytes:
    """Архив резервной копии: метка роли и даты, база, конфиг интерфейса."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name)
            ti.size = len(raw)
            ti.mtime = int(NOW.timestamp())
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json",
            json.dumps({"role": role, "created_at": "2026-09-14T03:00:00+03:00"}).encode())
        if db is not None:
            add("state/bot.db", db)
        if iface:
            add("awg/awg0.conf", b"[Interface]\nPrivateKey = DUMMY\nAddress = 10.8.0.2/30\n")
    return buf.getvalue()


def _old_db() -> bytes:
    """База схемы ниже минимума: в clients нет колонки kind."""
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE clients(id INTEGER PRIMARY KEY, tg_name TEXT);"
                      "CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER);")
    raw = con.serialize()
    con.close()
    return bytes(raw)


def _enc(plain: bytes) -> bytes:
    return secrets_util.encrypt(plain, key=_BACKUP_KEY)


_COPY = _enc(_archive())
_COPY_IFACE = _enc(_archive(iface=True))
_COPY_MAIN = _enc(_archive("main"))
_COPY_OLD = _enc(_archive(db=_old_db()))
_COPY_NAME = "awg-gw-pi-2026-09-14.tgz.enc"


def _file(name: str, blob: bytes) -> tuple:
    return ("file", name, blob)


WIZARD = [
    # ── W02 мастер восстановления ───────────────────────────────────────────
    _shot("gw.reassert.ask", press=[GwCB(action="reassert")], title="с панели, шлюз трафик не несёт"),
    _shot("gw.reassert.ask.carries", press=[GwCB(action="reassert", val="health")], carries=True,
          title="со здоровья, шлюз несёт трафик — отмена ведёт в здоровье"),
    _shot("gw.reassert.done", press=[GwCB(action="reassert"), GwCB(action="reassert!")],
          title="выполнено: итог остаётся, панель следом"),
    _shot("gw.reassert.failed", press=[GwCB(action="reassert"), GwCB(action="reassert!")],
          op=(False, "routing-gw-setup.sh: не уложилось в 120 с"), title="отказ: хвост вывода"),
]


BACKUPS = [
    # ── W32–W33 бэкапы и шифрование ─────────────────────────────────────────
    _shot("gw.set.backup", press=[GwCB(action="backup")], title="по умолчанию, фразы нет"),
    _shot("gw.set.backup.off", press=[GwCB(action="backup")], conf={"app.scheduler.backup_enabled": False},
          title="автобэкапы выключены — раздел свёрнут"),
    _shot("gw.set.backup.email", press=[GwCB(action="backup")], passphrase=True, **_MAIL,
          conf={"app.scheduler.backup_channel": "email"}, title="на e-mail, фраза задана"),
    _shot("gw.set.backup.channel.no_mail", press=[GwCB(action="backup"),
                                                  GwCB(action="cyc", val="app.scheduler.backup_channel")],
          title="«Куда» → e-mail без ящика — предложение настроить"),
    _shot("gw.set.backup.channel.no_enc", press=[GwCB(action="backup"),
                                                 GwCB(action="cyc", val="app.scheduler.backup_channel")],
          **_MAIL, title="«Куда» → e-mail без фразы — alert"),
    _shot("gw.set.backup.channel.email", press=[GwCB(action="backup"),
                                                GwCB(action="cyc", val="app.scheduler.backup_channel")],
          passphrase=True, **_MAIL, title="«Куда» → e-mail"),
    _shot("gw.set.backup.when.ask", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          title="приглашение: день и час"),
    _shot("gw.set.backup.when.done", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          text="5 3", title="итог первой строкой"),
    _shot("gw.set.backup.when.bad", press=[GwCB(action="backup"), GwCB(action="edit", val="backup_when")],
          text="31 25", title="вне границ — переспрос"),
    _shot("gw.set.backup.now.nokey", press=[GwCB(action="backup"), GwCB(action="backup!")],
          title="«Сделать сейчас» без фразы — отказ alert, без «Готовлю…»"),
    _shot("gw.set.backup.now", press=[GwCB(action="backup"), GwCB(action="backup!")], passphrase=True,
          title="«Сделать сейчас» — файл в чат"),
    _shot("gw.set.backup.now.mailed", press=[GwCB(action="backup"), GwCB(action="backup!")], passphrase=True,
          **_MAIL, conf={"app.scheduler.backup_channel": "email"}, title="«Сделать сейчас» — на ящик"),
    _shot("gw.set.enc", press=[GwCB(action="backup"), GwCB(action="enc")], title="фразы нет"),
    _shot("gw.set.enc.set", press=[GwCB(action="backup"), GwCB(action="enc")], passphrase=True,
          title="фраза задана"),
    _shot("gw.set.enc.ask", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          title="приглашение: фраза"),
    _shot("gw.set.enc.first", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          text="DUMMY-passphrase", title="первая фраза принята и удалена — повтор"),
    _shot("gw.set.enc.short", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set")],
          text="DUMMY", title="короткая фраза — переспрос"),
    _shot("gw.set.enc.saved", steps=[_p(GwCB(action="backup")), _p(GwCB(action="enc")),
                                     _p(GwCB(action="enc_set")), _t("DUMMY-passphrase"), _t("DUMMY-passphrase")],
          title="повтор совпал — фраза задана, итог первой строкой бэкапов"),
    _shot("gw.set.enc.mismatch", steps=[_p(GwCB(action="backup")), _p(GwCB(action="enc")),
                                        _p(GwCB(action="enc_set")), _t("DUMMY-passphrase"), _t("DUMMY-other")],
          title="повтор не совпал — заново с первой фразы"),
    _shot("gw.set.enc.cancel", press=[GwCB(action="backup"), GwCB(action="enc"), GwCB(action="enc_set"),
                                      CancelCB(kind="set_backup")], title="«✖️ Отмена» — бэкапы"),
]


FILES = [
    # ── W60 восстановление: кнопки без файла ────────────────────────────────
    # (кнопки файла перевыпуска прежних выпусков — устаревшие: их проверяют e2e)
    _shot("gw.restore.nofile", press=[GwCB(action="restore!")], title="восстановить — файла в памяти нет"),
    _shot("gw.restore.drop", press=[GwCB(action="restore_drop")], title="отказ от восстановления"),

    # ── W50–W52 файлы конфигурации в чате ────────────────────────────────────
    _shot("gw.bundle.reissue", steps=[_file(_BUNDLE_NAME, _BUNDLE)],
          title="шифрованный файл перевыпуска из истории чата — отказ словами, файл убран"),
    _shot("gw.bundle.reissue.carries", steps=[("start", ""), _file(_BUNDLE_NAME, _BUNDLE)], carries=True,
          tick=_status(), title="то же у шлюза, несущего трафик: панель выше остаётся живой"),
    _shot("gw.bundle.first_run", steps=[_file("awg-gw-bundle.sh", b"#!/bin/sh\n# DUMMY\n")],
          title="файл первого применения по имени — убран из чата"),
    _shot("gw.bundle.first_run.renamed", steps=[_file("setup.sh", b"#!/bin/sh\n# awg-gw-bundle DUMMY\n")],
          title="файл первого применения по содержимому"),
    _shot("gw.bundle.not_ours", steps=[_file("photo.bin", b"DUMMY")], title="не конфигурация шлюза"),
    _shot("gw.bundle.too_big", steps=[_file("video.bin", b"\0" * (600 * 1024))],
          title="крупнее любого файла конфигурации — не скачивается"),

    # ── W60 восстановление из копии ─────────────────────────────────────────
    _shot("gw.restore.offer", steps=[_file(_COPY_NAME, _COPY)], backup_key=True,
          title="копия шлюза — предложение"),
    _shot("gw.restore.offer.ifaces", steps=[_file(_COPY_NAME, _COPY_IFACE)], backup_key=True, carries=True,
          title="копия перезапишет конфиг линка — предупреждение о перезапуске AWG"),
    _shot("gw.restore.run", steps=[_file(_COPY_NAME, _COPY), _p(GwCB(action="restore!"))], backup_key=True,
          title="«Восстановить» — кнопки сняты, отчёт пришлёт новый процесс"),
    _shot("gw.restore.cancel", steps=[_file(_COPY_NAME, _COPY), _p(GwCB(action="restore_drop"))],
          backup_key=True, title="отказ после файла"),
    _shot("gw.restore.rejected.plain", steps=[_file("awg-gw-pi-2026-09-14.tgz", _archive())],
          title="открытый архив — на шлюзе только шифрованные"),
    _shot("gw.restore.rejected.no_key", steps=[_file(_COPY_NAME, _COPY)],
          title="шифрованная копия, а фразы здесь нет"),
    _shot("gw.restore.rejected.garbage", steps=[_file("notes.tgz.enc", b"DUMMY-not-a-backup")], backup_key=True,
          title="не расшифровалась"),
    _shot("gw.restore.rejected.not_copy", steps=[_file("notes.tgz.enc", _enc(b"DUMMY-not-a-tar"))],
          backup_key=True, title="расшифровалась, но не копия awg-bot"),
    _shot("gw.restore.rejected.main", steps=[_file("awg-bot-2026-09-14.tgz.enc", _COPY_MAIN)], backup_key=True,
          title="копия основного бота"),
    _shot("gw.restore.rejected.old_schema", steps=[_file(_COPY_NAME, _COPY_OLD)], backup_key=True,
          title="схема базы ниже 3.2.0"),
]
