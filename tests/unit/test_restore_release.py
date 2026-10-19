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
                           sha256=sha, meta={"awg_gen": 1, "roles": {"main": {"requires": "3.4.0"}}}
                           if meta is None and not bad else meta,
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


# ── правки по ревью (FIXES-uupd-2: restore-1…12 и вне оси) ───────────────────

def test_a_plan_from_another_version_is_read_tolerantly():
    """restore-1: вторую половину перехода исполняет код другой версии — план с
    полями, которых этот код не знает (и без полей, которые знает), читается:
    чужое отброшено, недостающее — по умолчанию; подпись — та же."""
    from awgbot.domain.backup.plan import Plan
    d = {"line": "same", "slots": [{"slot": 1, "case": "same", "name": "Pi", "future": 7}],
         "replaced": [0, 0], "release": {}, "new_field_of_3_9": {"x": 1}}
    p = Plan.from_dict(d)
    assert p.line == "same" and p.slots[0].name == "Pi" and p.slots[0].link_if == ""
    assert p.signature() == ("same", False, ((1, "same"),))
    assert Plan.from_dict({"line": "clean"}).slots == []


def test_an_upgrade_below_the_release_minimum_is_refused(github):
    """restore-2: повышение на выпуск, который ставится только с минимума выше
    текущей версии, — отказ словами, без прыжка через минимум."""
    github["releases"] = [_rel("v3.4.1"), _rel("v3.4.2"),
                          _rel("v3.6.0", meta={"awg_gen": 1, "roles": {"main": {"requires": "3.5.0"}}})]
    r = fr.plan_release(_label("3.6.0"))
    assert r["error"] == ("выпуск v3.6.0 ставится только с v3.5.0 и новее — сначала обнови сервер AWG до v3.5.0 "
                          "кнопкой «⬆️ Обновить», затем восстанови бэкап"), r["error"]
    github["releases"][-1] = _rel("v3.6.0", meta={"awg_gen": 1, "roles": {"main": {"requires": "3.4.0"}}})
    assert fr.plan_release(_label("3.6.0"))["error"] == ""


def test_a_release_of_another_awg_generation_is_refused(github, monkeypatch):
    """restore-12: поколение AmneziaWG выпуска бэкапа сверяется с хостом."""
    from awgbot.infra import awglock
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    github["releases"] = [_rel("v3.4.1", meta={"awg_gen": 2, "roles": {"main": {"requires": "3.4.0"}}}),
                          _rel("v3.4.2")]
    r = fr.plan_release(_label("3.4.1"))
    assert "поколения 2" in r["error"] and "поколения 1" in r["error"], r


def test_the_backup_release_metadata_is_read_even_on_a_prerelease_build(github, monkeypatch):
    """restore-7: на предрелизной сборке метаданные выпусков не читаются — а
    метаданные выпуска бэкапа нужны: plan_release просит их по тегу."""
    seen = []
    real = updates.list_releases

    def releases(*a, **kw):
        seen.append(kw.get("meta_tag"))
        return real(*a, **kw)
    monkeypatch.setattr(updates, "list_releases", releases)
    fr.plan_release(_label("3.4.1"))
    assert seen == ["v3.4.1"]


def test_list_releases_reads_the_named_tag_on_a_non_release_build(monkeypatch):
    """Сам list_releases: установка не из выпуска (X.Y.Z.N), meta_tag — метаданные
    этого тега прочитаны, остальных — нет."""
    from awgbot.infra.updates import github as gh
    raw = json.dumps([{"tag_name": t, "name": t, "body": "", "prerelease": False,
                       "assets": [{"name": "awg-bot.tgz", "url": "DUMMY", "digest": "sha256:" + HEX},
                                  {"name": "release-meta.json", "url": "DUMMY-META", "digest": "sha256:" + HEX}]}
                      for t in ("v3.4.1", "v3.4.2")]).encode()
    monkeypatch.setattr(gh._up(), "_request", lambda url, accept: raw)
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.3.1")          # предрелизная сборка 3.4.3
    read = []
    monkeypatch.setattr(gh, "_read_meta", lambda tag, asset, cache: (read.append(tag), ({"awg_gen": 1}, None))[1])
    monkeypatch.setattr(cfg, "UPDATES_META_ASSET_NAME", "release-meta.json")
    monkeypatch.setattr(cfg, "UPDATES_ASSET_NAME", "awg-bot.tgz")
    gh.list_releases()
    assert read == [], "предрелизная сборка читала метаданные всех выпусков"
    rels = gh.list_releases(meta_tag="v3.4.1")
    assert read == ["v3.4.1"] and next(r for r in rels if r.tag == "v3.4.1").meta == {"awg_gen": 1}


def test_a_finished_run_of_the_same_release_does_not_hold_the_gateways(services):
    """restore-6: после перехода остаётся только ИДУЩИЙ ход на ту же версию;
    законченный (rollback_failed) держал бы следование шлюзов."""
    services.fleet_run_set({"id": "a1", "tag": "v3.4.1", "state": "rollback_failed", "steps": []})
    services.fleet_after_restore("3.4.1")
    assert services.fleet_run() == {}
    services.fleet_run_set({"id": "a2", "tag": "v3.4.1", "state": "running", "steps": []})
    services.fleet_after_restore("3.4.1")
    assert services.fleet_run()["id"] == "a2"


def test_the_terminal_downloads_into_its_own_place(github, tmp_path, monkeypatch, capsys):
    """restore-9: терминал качает выпуск сразу в свой --out — файл бота в
    каталоге бэкапов не трогает; архив от бота не читается — отказ словами, без
    трейсбека."""
    from tools import restore_release as tool
    backups = tmp_path / "backups"; backups.mkdir()
    monkeypatch.setattr(cfg, "BACKUP_DIR", backups)
    bot_file = backups / fr.RELEASE_FILE
    bot_file.write_bytes(b"DUMMY-bot")
    meta = tmp_path / "backup-meta.json"
    meta.write_text(json.dumps(_label("3.4.1")), encoding="utf-8")
    out = tmp_path / "op" / "release.tgz"
    out.parent.mkdir()
    assert tool.main(["--meta", str(meta), "--out", str(out)]) == 0
    assert out.read_bytes() == BLOB and bot_file.read_bytes() == b"DUMMY-bot"
    capsys.readouterr()
    assert tool.main(["--meta", str(meta), "--out", str(out), "--archive", str(tmp_path / "нет.tgz")]) == 1
    assert "не прочитан" in capsys.readouterr().err


async def test_a_busy_install_lock_answers_in_the_chat_and_launches_nothing(services, monkeypatch):
    """restore-8: замок установки занят — restore отказал бы сам и перезапустил
    бота ради отчёта; бот отвечает сразу, вопрос остаётся живым."""
    from awgbot.bot import texts
    from awgbot.bot.handlers import restore as rs
    from awgbot.domain.selfupdate import marks
    from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
    monkeypatch.setattr(marks, "install_busy", lambda: True)
    state = FakeState()
    await state.update_data(restore_blob="RFVNTVk=", restore_plan={}, restore_fp="aaaaaaaaaaaa")
    launched = []
    services.launch_restore = lambda path, release="": launched.append(path)
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await rs.run_restore(cb, services, state, "aaaaaaaaaaaa")
    assert launched == [] and (await state.get_data())["restore_blob"], "вопрос сброшен"
    assert msg.sent[-1][1] == "⚠️ Восстановление не запущено: " + texts.RESTORE_INSTALL_BUSY
    assert not getattr(services, "_restore_running", False)


async def test_a_downloaded_release_is_removed_when_the_launch_fails(services, monkeypatch, tmp_path):
    """restore-9: выпуск скачан, а запуск не удался — файл выпуска прочь."""
    from awgbot.bot.handlers import restore as rs
    from awgbot.domain.selfupdate import marks
    from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
    monkeypatch.setattr(marks, "install_busy", lambda: False)
    rel = tmp_path / "restore-pending.release.tgz"
    rel.write_bytes(b"DUMMY")
    services.prepare_restore_release = lambda plan: str(rel)
    services.prepare_restore = lambda blob, plan=None: "/tmp/DUMMY"

    def launch(path, release=""):
        raise OSError("systemd-run: нет")
    services.launch_restore = launch
    state = FakeState()
    await state.update_data(restore_blob="RFVNTVk=", restore_plan={}, restore_fp="aaaaaaaaaaaa")
    bot = FakeBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    with pytest.raises(OSError):
        await rs.run_restore(cb, services, state, "aaaaaaaaaaaa")
    assert not rel.exists()


def test_withdrawn_and_preliminary_releases_are_not_the_roles_version():
    """Вне оси: version роли в выпуске пропускает снятые и предварительные
    (prerelease без «снят:») ниже upto; сам upto годится и снятым (Р17)."""
    from awgbot.domain.fleet.plan import role_target
    rels = [_rel("v3.4.1"), _rel("v3.4.2", withdrawn="DUMMY"),
            updates.Release(tag="v3.4.3", version=(3, 4, 3), body="", asset_url="DUMMY", sha256=HEX,
                            meta={"awg_gen": 1, "roles": {"gw": {"requires": "3.4.0"}}}, prerelease=True),
            _rel("v3.4.4", withdrawn="DUMMY")]
    for r in rels:
        object.__setattr__(r, "meta", {"awg_gen": 1, "roles": {"gw": {"requires": "3.4.0"}}})
    assert role_target(rels, "gateway", (3, 4, 3)).tag == "v3.4.3", "сам выпуск набора"
    assert role_target(rels, "gateway", (3, 4, 4)).tag == "v3.4.4"
    assert role_target(rels, "gateway", (3, 4, 5)).tag == "v3.4.1", "снятый или предварительный — версия роли"


def test_a_preliminary_release_is_not_a_button_target(services, monkeypatch):
    """Вне оси: prerelease без «снят:» — тоже не цель кнопки набора и не ступень."""
    from awgbot.infra.updates import pick
    meta = {"awg_gen": 1, "roles": {"main": {"requires": "3.4.0"}, "gw": {"requires": "3.4.0"}}}
    rels = [updates.Release(tag=t, version=updates.parse_version(t), body="", asset_url="DUMMY", sha256=HEX,
                            meta=meta, prerelease=pre) for t, pre in (("v3.4.2", False), ("v3.5.0", True))]
    assert services.fleet_pick((3, 4, 2), rels, None) is None
    assert pick._newer((3, 4, 2), rels, "main", None) == []
    rels[1] = updates.Release(tag="v3.5.0", version=(3, 5, 0), body="", asset_url="DUMMY", sha256=HEX, meta=meta)
    assert services.fleet_pick((3, 4, 2), rels, None).tag == "v3.5.0"


def test_the_preliminary_flag_survives_the_saved_list(services):
    meta = {"awg_gen": 1, "roles": {"main": {"requires": "3.4.0"}}}
    rel = updates.Release(tag="v3.5.0", version=(3, 5, 0), body="", asset_url="DUMMY", sha256=HEX, meta=meta,
                          prerelease=True)
    services._update_store([rel], {})
    got = next(r for r in services._update_cached_releases(strict=False) if r.tag == "v3.5.0")
    assert got.prerelease is True and got.not_target
