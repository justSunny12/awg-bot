"""
merge/ — слияние распакованного бэкапа с текущим состоянием хоста перед
раскладкой (`awg-bot restore`, после остановки сервиса): data — база, recheck
— пересчёт по фактическому времени, files — app.yaml, env, конфиги
интерфейсов и линков, report — итог для маркера восстановления.

Читает остановленное живое состояние, пишет только в распакованный бэкап:
отказ на любом шаге — живое не тронуто. План — тот же, что бот показал в
вопросе (backup/plan.py).
"""
from __future__ import annotations

import json
import os
import sqlite3

from awgbot.core import config

from .. import meta as _meta
from .. import plan as _plan
from . import data, files, recheck
from .report import REPORT_NAME, build_report


class MergeError(RuntimeError):
    """Слияние не прошло: живое не тронуто."""


def _finalize(db_path: str) -> None:
    """База — одним файлом: раскладка кладёт только bot.db."""
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode = DELETE")
    finally:
        con.close()


def run(copy_dir: str, *, cur_db: str | None = None, conf_dir: str | None = None,
        env_path: str | None = None, awg_dir: str | None = None, root_dir: str | None = None,
        client_if: str | None = None, now=None) -> dict:
    """Слить бэкап в copy_dir с текущим; итог — в copy_dir/restore-merge.json."""
    cur_db = cur_db or str(config.DB_PATH)
    conf_dir = conf_dir or str(config.CONF_DIR)
    env_path = env_path or (str(config.ENV_PATH) if config.ENV_PATH is not None else "")
    awg_dir = awg_dir or config.AWG_DIR
    root_dir = root_dir or _meta.ROOT_DIR
    client_if = client_if or config.AWG_INTERFACE
    try:
        with open(os.path.join(copy_dir, _meta.META_NAME), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = None
    err = _meta.check(meta, chat=False)
    if err:
        raise MergeError(err)
    if not os.path.exists(os.path.join(copy_dir, "state", "bot.db")):
        raise MergeError("в бэкапе нет базы")
    p = _plan.plan_for_dir(meta, copy_dir, cur_db=cur_db, conf_dir=conf_dir, awg_dir=awg_dir,
                           root_dir=root_dir, now=now)
    out = {"lost": [], "recheck": {}}

    def build(result: str, copy_db: str) -> None:
        if p.same_line:
            data.merge_same(result, copy_db, cur_db, p)
        else:
            out["lost"] = data.merge_other(result, copy_db, cur_db, p)
            out["recheck"] = recheck.recheck(result)
        _finalize(result)

    try:
        db_path = data.replace_db(copy_dir, build)
        files.merge_app_yaml(copy_dir, conf_dir, p.same_line)
        files.merge_env(copy_dir, env_path, p)
        files.merge_link_files(copy_dir, p, awg_dir=awg_dir, root_dir=root_dir)
        moved = set()
        if not p.same_line and p.line != _plan.UNREADABLE and os.path.exists(cur_db):
            con = sqlite3.connect(cur_db)
            try:
                for n in p.current_slots():
                    for dev in data._slot_devices(con, n):
                        row = con.execute("SELECT public_key FROM devices WHERE id = ?", (dev,)).fetchone()
                        if row is not None:
                            moved.add(row[0])
            finally:
                con.close()
        files.merge_iface_confs(copy_dir, p, awg_dir=awg_dir, link_ifs={s.link_if for s in p.slots},
                                client_if=client_if, transplanted_pubs=moved,
                                lost_pubs={d.get("pub", "") for d in out["lost"]})
    except (sqlite3.Error, OSError, RuntimeError) as e:
        raise MergeError(f"слияние не прошло: {e}") from e
    report = build_report(p, db_path, out["lost"], out["recheck"])
    with open(os.path.join(copy_dir, REPORT_NAME), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False)
    return report


__all__ = ["run", "MergeError", "REPORT_NAME"]
