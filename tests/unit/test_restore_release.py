"""Восстановление = переход на выпуск бэкапа (domain/fleet/restore.py, §3.5):
версия бэкапа та же — как раньше; другая — только выпуск vV с GitHub, sha256
ассета которого равен digest метки; иначе отказ Т-В2 словами. План несёт
строку версии (Т-В1) первой после заголовка, у снятого выпуска — хвост.
После «♻️ Восстановить» бот качает ассет до запуска и передаёт его в
`awg-bot restore --release`; итог и отказ перехода — строки Т-В3.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import awgbot.core.config as cfg
from awgbot.domain.fleet import restore as fr
from awgbot.infra import updates

pytestmark = pytest.mark.unit

BLOB = b"DUMMY-release-asset"
HEX = hashlib.sha256(BLOB).hexdigest()


def _rel(tag: str, *, sha: str = HEX, withdrawn: str = "", meta: dict | None = None, bad: str = ""):
    return updates.Release(tag=tag, version=updates.parse_version(tag), body="", asset_url="DUMMY",
                           sha256=sha, meta={"awg_gen": 1} if meta is None and not bad else meta,
                           meta_bad=bad, withdrawn=withdrawn)


@pytest.fixture()
def github(monkeypatch):
    """Выпуски на GitHub без сети; calls — сколько раз спросили."""
    state = {"releases": [_rel("v3.4.1"), _rel("v3.4.2"), _rel("v3.5.0")], "down": False, "calls": 0,
             "fetched": []}

    def releases(*a, **kw):
        state["calls"] += 1
        if state["down"]:
            raise updates.UpdateError("HTTP 503")
        return list(state["releases"])

    def download(r):
        state["fetched"].append(r.tag)
        if state.get("fetch_fails"):
            raise updates.UpdateError("HTTP 502")
        return BLOB
    monkeypatch.setattr(updates, "list_releases", releases)
    monkeypatch.setattr(updates, "download_asset", download)
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.2")
    return state


def _label(ver: str, digest: str | None = "sha256:" + HEX) -> dict:
    return {"version": ver, **({"digest": digest} if digest is not None else {})}


def test_the_same_version_needs_no_github(github):
    for ver in ("3.4.2", "3.4.2.1"):        # хотфикс и локальная сборка поверх той же X.Y.Z — та же версия
        r = fr.plan_release(_label(ver, digest=""))
        assert r["kind"] == fr.SAME and not r["error"] and fr.version_lines(r) == []
    assert github["calls"] == 0, "та же версия спросила GitHub"


def test_other_version_without_digest_is_refused_before_the_network(github):
    r = fr.plan_release(_label("3.4.1", digest=""))
    assert r["error"] == "бэкап снят на сборке не из выпуска (v3.4.1) — восстановить его можно только на той же версии"
    assert github["calls"] == 0


@pytest.mark.parametrize("case, want", [
    ("none", "выпуска v3.4.1 на GitHub нет — восстановить этот бэкап можно только на v3.4.1, поставив её вручную"),
    ("digest", "выпуск v3.4.1 на GitHub не совпадает с тем, на котором снят бэкап (sha256 другой) — "
               "переходить на него не буду"),
    ("down", "не удалось проверить выпуск v3.4.1 на GitHub — попробуй позже"),
    ("meta", "метаданные выпуска v3.4.1 на GitHub не прочитаны — переходить на него не буду"),
])
def test_refusals_t_v2(github, case, want):
    if case == "none":
        github["releases"] = [_rel("v3.4.2")]               # тег удалён вопреки правилу выпуска
    elif case == "digest":
        github["releases"] = [_rel("v3.4.1", sha="e" * 64)]
    elif case == "down":
        github["down"] = True
    else:
        github["releases"] = [_rel("v3.4.1", bad="sha256 метаданных релиза не совпал")]
    r = fr.plan_release(_label("3.4.1"))
    assert r["error"] == want and r["release"] is None


def test_down_and_up_lines_t_v1(github):
    down = fr.plan_release(_label("3.4.1"))
    assert down["kind"] == fr.DOWN and not down["error"] and down["release"].tag == "v3.4.1"
    assert fr.version_lines(down) == [
        "♻️ Версия: сейчас v3.4.2, бэкап снят на v3.4.1 — сервер AWG сначала вернётся на v3.4.1 "
        "(выпуск с GitHub, sha256 сверю с бэкапом), затем восстановится; шлюзы перейдут на v3.4.1 следом"]
    up = fr.plan_release(_label("3.5.0"))
    assert up["kind"] == fr.UP and fr.version_lines(up) == [
        "♻️ Версия: сейчас v3.4.2, бэкап снят на v3.5.0 — сервер AWG сначала обновится до v3.5.0, затем "
        "восстановится; шлюзы перейдут на v3.5.0 следом"]
    assert "release" not in fr.public(up) and json.dumps(fr.public(up))     # в план вопроса — без объекта


def test_a_withdrawn_release_is_a_target_with_a_tail(github):
    github["releases"] = [_rel("v3.4.1", withdrawn="ломает маршруты шлюза"), _rel("v3.4.2")]
    r = fr.plan_release(_label("3.4.1"))
    assert not r["error"] and r["withdrawn"] == "ломает маршруты шлюза"
    assert fr.version_lines(r)[1] == ("⚠️ Выпуск v3.4.1 снят: ломает маршруты шлюза. Восстановить можно, но после "
                                      "восстановления лучше обновиться кнопкой «⬆️ Обновить»")


def test_inspect_carries_the_version_line_and_the_newer_refusal_is_gone(services, github, monkeypatch):
    """Осмотр файла: прежний отказ «обнови бота до …» снят — новее кода
    теперь переход вверх; строка версии — первой после заголовка вопроса."""
    from awgbot.bot import texts
    from awgbot.domain.backup.plan import Plan
    from tests.unit.test_backup_crypto import PHRASE, _archive, _meta
    from awgbot.util import secrets_util
    from awgbot.infra import awglock
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    services.backup_set_passphrase(PHRASE)
    db = services.db_snapshot_bytes(services.db.path)
    blob = secrets_util.encrypt(_archive(_meta(version="3.5.0", digest="sha256:" + HEX, awg_gen=1),
                                         env=f"ADMIN_ID={cfg.ADMIN_ID}\n", db=db), passphrase=PHRASE)
    info = services.inspect_backup(blob, "b.tgz.enc")
    assert info["ok"], info
    assert info["plan"]["release"]["kind"] == "up" and info["plan"]["release"]["tag"] == "v3.5.0"
    text = texts.restore_offer_plan(Plan.from_dict(info["plan"]), "")
    first, second = text.split("\n")[:2]
    assert first.startswith("♻️ Восстановить бэкап от ") and second.startswith("♻️ Версия: сейчас v3.4.2")
    github["down"] = True
    assert services.inspect_backup(blob, "b.tgz.enc")["error"] == \
        "не удалось проверить выпуск v3.5.0 на GitHub — попробуй позже"


def test_the_asset_is_fetched_before_the_launch_and_passed_to_restore(services, github, monkeypatch, tmp_path):
    plan = {"release": fr.public(fr.plan_release(_label("3.4.1")))}
    assert services.prepare_restore_release({"release": {"kind": "same"}}) == ""
    assert github["fetched"] == []
    path = services.prepare_restore_release(plan)
    assert github["fetched"] == ["v3.4.1"] and open(path, "rb").read() == BLOB
    import os
    assert os.stat(path).st_mode & 0o777 == 0o600
    from awgbot.infra import detach
    spawned = []
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, unit_prefix: spawned.append(argv))
    services.launch_restore("/x/restore-pending.tgz.enc", path)
    assert spawned[0][2:] == ["restore", "--yes", "--release", path, "/x/restore-pending.tgz.enc"]
    services.launch_restore("/x/restore-pending.tgz.enc")
    assert spawned[1][2:] == ["restore", "--yes", "/x/restore-pending.tgz.enc"]


def test_a_failed_download_launches_nothing(services, github):
    github["fetch_fails"] = True
    plan = {"release": fr.public(fr.plan_release(_label("3.4.1")))}
    with pytest.raises(updates.UpdateError) as e:
        services.prepare_restore_release(plan)
    assert str(e.value) == "выпуск v3.4.1 с GitHub не скачался — попробуй позже"
    github["fetch_fails"] = False
    github["releases"] = [_rel("v3.4.1", sha="e" * 64)]          # выпуск подменили между вопросом и кнопкой
    with pytest.raises(updates.UpdateError) as e:
        services.prepare_restore_release(plan)
    assert "sha256 другой" in str(e.value)


async def test_the_reports_t_v3(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers.restore import report_restore_result
    services.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.2", "state": "done", "steps": []})
    services.pop_restore_failed_info = lambda: {}
    services.pop_restore_done = lambda: {"created_at": "2026-10-05T14:00:00+03:00", "version": "3.4.1",
                                         "merge": {"line": "same", "slots": [], "lost": []}}
    monkeypatch.setattr(services, "restore_wait_start", lambda *a, **kw: None, raising=False)
    await report_restore_result(fake_bot, services)
    text = next(r[2] for r in fake_bot.records if r[0] == "send_message")
    assert text.split("\n")[1] == "Версия: v3.4.1 — как в бэкапе"
    assert services.fleet_run() == {}, "ход прежнего выпуска пережил переход на выпуск бэкапа"


async def test_a_failed_transition_says_nothing_changed(services, fake_bot):
    from awgbot.bot.handlers.restore import report_restore_result
    services.pop_restore_failed_info = lambda: {"failed": "слияние базы не удалось", "release": "3.4.1",
                                                "from": "3.4.2"}
    services.pop_restore_done = lambda: None
    await report_restore_result(fake_bot, services)
    text = next(r[2] for r in fake_bot.records if r[0] == "send_message")
    assert text == "⚠️ Переход на v3.4.1 не удался: слияние базы не удалось — ничего не изменилось, работает v3.4.2"


def test_the_terminal_tool(github, tmp_path, capsys):
    """tools.restore_release: та же версия — «same»; другая — версия первой
    строкой и строки Т-В1 без HTML (Т-В4); архив от бота — только сверка sha256."""
    from tools import restore_release as tool
    meta = tmp_path / "backup-meta.json"
    out = tmp_path / "release.tgz"
    meta.write_text(json.dumps(_label("3.4.2")), encoding="utf-8")
    assert tool.main(["--meta", str(meta), "--out", str(out)]) == 0
    assert capsys.readouterr().out == "same\n"
    meta.write_text(json.dumps(_label("3.4.1")), encoding="utf-8")
    assert tool.main(["--meta", str(meta), "--out", str(out)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "3.4.1" and lines[1].startswith("♻️ Версия: сейчас v3.4.2, бэкап снят на v3.4.1")
    assert out.read_bytes() == BLOB
    given = tmp_path / "given.tgz"
    given.write_bytes(BLOB)
    calls = github["calls"]
    assert tool.main(["--meta", str(meta), "--out", str(tmp_path / "o2.tgz"), "--archive", str(given)]) == 0
    assert capsys.readouterr().out == "3.4.1\n" and github["calls"] == calls, "архив от бота — снова в сеть"
    given.write_bytes(b"DUMMY-other")
    assert tool.main(["--meta", str(meta), "--out", str(tmp_path / "o3.tgz"), "--archive", str(given)]) == 1
    assert "sha256 другой" in capsys.readouterr().err
    github["releases"] = []
    assert tool.main(["--meta", str(meta), "--out", str(out)]) == 1
    assert "на GitHub нет" in capsys.readouterr().err


async def test_the_gateway_chat_entry_gets_the_same_version_line(services, github, fake_bot, monkeypatch):
    """Бэкап из чата бота шлюза: осмотр и вопрос — в чате бота сервера, с той
    же строкой версии первой после заголовка."""
    from awgbot.bot.handlers import restore as rs
    from tests.conftest import FakeState
    from tests.unit.test_backup_crypto import PHRASE, _archive, _meta
    from awgbot.util import secrets_util
    from awgbot.infra import awglock
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    services.backup_set_passphrase(PHRASE)
    db = services.db_snapshot_bytes(services.db.path)
    blob = secrets_util.encrypt(_archive(_meta(version="3.4.1", digest="sha256:" + HEX, awg_gen=1),
                                         env=f"ADMIN_ID={cfg.ADMIN_ID}\n", db=db), passphrase=PHRASE)
    assert await rs.relay_restore(fake_bot, services, FakeState(), blob, "b.tgz.enc") == ""
    asked = [r for r in fake_bot.records if r[0] == "send_message"][-1][2]
    assert asked.split("\n")[1].startswith("♻️ Версия: сейчас v3.4.2, бэкап снят на v3.4.1 — сервер AWG сначала "
                                           "вернётся на v3.4.1")


def test_another_generation_is_refused_before_github(services, github, monkeypatch):
    from tests.unit.test_backup_crypto import PHRASE, _archive, _meta
    from awgbot.util import secrets_util
    from awgbot.infra import awglock
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(awglock, "generation", lambda: 2)
    services.backup_set_passphrase(PHRASE)
    db = services.db_snapshot_bytes(services.db.path)
    blob = secrets_util.encrypt(_archive(_meta(version="3.4.1", digest="sha256:" + HEX, awg_gen=1),
                                         env=f"ADMIN_ID={cfg.ADMIN_ID}\n", db=db), passphrase=PHRASE)
    assert "другом поколении AmneziaWG" in services.inspect_backup(blob, "b.tgz.enc")["error"]
    assert github["calls"] == 0
