"""
Агент шлюза — правки по ревью: бэкап в чате бота шлюза — только узнанный
(по имени или содержимому), прочие файлы, включая архив выпуска awg-bot.tgz,
молча пропускаются; бэкап сервера — серверу по каналу (test_gateway_relay); сбой ответа на
вопрос о настройках — честный отказ, вопрос остаётся; следы восстановления
прежних версий стираются на старте; карточка слота называет вид вопроса.
"""
from __future__ import annotations

import io
import json
import tarfile

import pytest

import awgbot.core.config as cfg
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    yield GatewayServices(d)
    d.close()


class _Doc:
    def __init__(self, name, size=100):
        self.file_name, self.file_size, self.file_id = name, size, "F"


class _DlBot(FakeBot):
    def __init__(self, blob):
        super().__init__()
        self.blob = blob

    async def download(self, doc, destination=None):
        destination.write(self.blob)


def _tar(meta: dict | None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps(meta).encode() if meta is not None else b"release"
        ti = tarfile.TarInfo("state/backup-meta.json" if meta is not None else "awg-bot/awg-bot.sh")
        ti.size = len(raw)
        tar.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


async def _send(svc, name, blob):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=_DlBot(blob))
    msg.document = _Doc(name)
    await gh.gw_bundle_document(msg, svc, FakeState())
    return msg


@pytest.mark.parametrize("name, blob", [("awg-bot.tgz", _tar(None)), ("photos.tar.gz", _tar(None)),
                                        ("notes.tgz.enc", b"DUMMY-not-a-backup")], ids=["release", "archive", "junk"])
async def test_other_files_are_ignored_silently(svc, name, blob):
    msg = await _send(svc, name, blob)
    assert msg.sent == [] and not msg.deleted, "на не-бэкап бот ответил или убрал его"


async def test_a_gateway_backup_by_its_label_is_refused_and_removed(svc):
    msg = await _send(svc, "copy.tgz", _tar({"role": "gw"}))
    assert msg.deleted and msg.sent[-1][1] == texts.GW_OLD_BACKUP_REFUSED


async def test_a_failed_keep_answer_is_refused_honestly_and_the_question_stays(svc, monkeypatch):
    from awgbot.runtime import linkclient
    svc.db.set_state(svc._KEEP_ASK_KEY, json.dumps({"why": "restore", "hash": "ab" * 8, "items": {}}))

    async def boom(services, accept):
        raise RuntimeError("канал оборвался")
    monkeypatch.setattr(linkclient, "report_keep_answer", boom)
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="keep"), svc, FakeState())
    assert cb.answers[-1] == ("⚠️ Ответ не принят: канал оборвался — вопрос остаётся, ответь ещё раз", True)
    assert svc.keep_section() == "keep", "вопрос пропал после сбоя"


def test_restore_markers_of_older_versions_are_wiped(svc, tmp_path, monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "drop_keys", lambda keys: [])
    data = tmp_path / "data"; data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    for name in ("restore-done.json", "restore-failed.json", "restore-pending.plan.json", ".restore-prune"):
        (data / name).write_text("{}", encoding="utf-8")
    (data / "gw.db").write_text("", encoding="utf-8")
    assert svc.backup_leftovers_drop() is True
    assert sorted(p.name for p in data.iterdir()) == ["gw.db"]


def test_the_card_names_what_the_question_waits_for():
    from awgbot.bot.texts.routing.card import keep_line
    assert keep_line({"state": "asked", "asked": "device"}) == ("⏳ Настройки прежнего устройства ждут решения "
                                                                "в чате бота шлюза")
    assert keep_line({"state": "asked", "asked": "restore"}) == ("⏳ Настройки бота шлюза из бэкапа ждут "
                                                                 "решения в чате бота шлюза")
    assert keep_line({"state": "asked", "asked": "same"}) == ("⏳ Настройки бота шлюза с сервера AWG ждут "
                                                              "решения в чате бота шлюза")
