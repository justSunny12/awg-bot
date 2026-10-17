"""Снимки агента шлюза: восстановление шлюза кнопкой, файлы в чат — файлы
конфигурации шлюза и бэкапы (отказ словами, сообщение убрано).

Своих бэкапов у шлюза нет: его настройки — в бэкапе сервера AWG, новое
устройство получает их по каналу конфигурации. Бэкап в чате бота шлюза — шлюза
прежних версий или сервера — не скачивается и не осматривается. Шифрованный
файл перевыпуска прежних выпусков — заглушка с подписью формата
(bundlecrypt.MAGIC): с 3.3.1 агент отказывает ему словами.
"""
from __future__ import annotations

from awgbot.util import bundlecrypt
from awgbot.bot.callbacks import GwCB

from tests.screens.shots_gateway_common import _status
from tests.screens.shots_gateway_agent import _shot


# ── файлы в чат ──────────────────────────────────────────────────────────────

_BUNDLE = bundlecrypt.MAGIC + b"DUMMY-bundle"          # формат проверяется по подписи
_BUNDLE_NAME = "awg-gw-NASPi.bin"
_BACKUP = b"AWGBK1DUMMY-backup"                        # содержимое не читается — отказ по имени


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


BACKUPS: list = []     # раздела «💾 Бэкапы» у шлюза нет; его кнопки прежних версий — «устарела» (e2e)


FILES = [
    # ── W50–W52 файлы конфигурации в чате ────────────────────────────────────
    _shot("gw.bundle.reissue", steps=[_file(_BUNDLE_NAME, _BUNDLE)],
          title="шифрованный файл перевыпуска из истории чата — отказ словами, файл убран"),
    _shot("gw.bundle.reissue.carries", steps=[("start", ""), _file(_BUNDLE_NAME, _BUNDLE)], carries=True,
          tick=_status(), title="то же у шлюза, несущего трафик: панель выше остаётся живой"),
    _shot("gw.bundle.first_run", steps=[_file("awg-gw-bundle.sh", b"#!/bin/sh\n# DUMMY\n")],
          title="файл первого применения по имени — убран из чата"),
    _shot("gw.bundle.first_run.renamed", steps=[_file("setup.sh", b"#!/bin/sh\n# awg-gw-bundle DUMMY\n")],
          title="файл первого применения по содержимому"),

    # ── бэкап в чате шлюза: своих бэкапов нет ───────────────────────────────
    _shot("gw.backup.old", steps=[_file("awg-bot-backup-gw-20260914_030000.tgz.enc", _BACKUP)],
          title="бэкап шлюза прежней версии — отказ словами, файл убран"),
    _shot("gw.backup.old.carries", steps=[("start", ""), _file("awg-gw-pi-2026-09-14.tgz", _BACKUP)],
          carries=True, tick=_status(), title="открытый архив прежних версий у шлюза, несущего трафик"),
    _shot("gw.backup.server", steps=[_file("awg-bot-backup-main-20261017_120000.tgz.enc", _BACKUP)],
          title="бэкап сервера AWG в чате шлюза — тот же ответ"),
]
