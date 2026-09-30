"""ownlists.py — свои списки, общие для всех шлюзов, и повтор после поломки."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from awgbot.util import timeutil
from awgbot.domain import gwownlists
from awgbot.domain.gwchecks import WRITE_ERROR, failure_detail
from awgbot.util import gwlink  # noqa: E402
from awgbot.domain.gateway.base import GwCheck, log


class OwnListsMixin:
    """Свои списки, общие для всех шлюзов, и повтор после поломки."""
    # ── свои списки, общие для всех шлюзов ──
    # Не настраивается: действует при режиме без VPN и канале линка. Правки
    # (кнопка, `awg-bot lan`, файл руками) сверкой становятся событиями и уходят
    # серверу; его канон применяется скриптом `sync` — только когда все свои
    # правки в нём учтены, иначе правки уходят ещё раз.
    _OWN_BASE_KEY = "gw_own_base"            # последний применённый канон: {gen, ver, hash, items}
    _OWN_PENDING_KEY = "gw_own_pending"      # свои правки, ещё не в каноне: {run, n, ev, sent_at}
    _OWN_FP_KEY = "gw_own_fp"                # отпечаток файлов на момент последней сверки
    _OWN_ERR_KEY = "gw_own_err"              # хвост ошибки последнего применения
    _OWN_NUDGE_KEY = "gw_own_nudge"          # «отказ с … / просили повтор в …» — просьба к серверу прислать канон снова
    _OWN_REJ_KEY = "gw_own_rej"              # что сервер отверг из последнего пакета: [[домен, причина]]
    _OWN_DEFER_KEY = "gw_own_defer"          # канон, отложенный из-за обвязки старого образца
    _OWN_STALE_SERVER_S = 600                # правки без ответа сервера дольше — «обнови основной бот»
    _own_run: str = ""                       # метка запуска: номера правок — в её пределах

    def own_active(self) -> bool:
        from awgbot.infra import gwguard
        return gwguard.lan_mode() and gwguard.unit_env("LINK_CHANNEL") == "1"

    def own_base(self) -> dict:
        return self.db.get_state_json(self._OWN_BASE_KEY, {})

    def _own_pending_raw(self) -> dict:
        p = self.db.get_state_json(self._OWN_PENDING_KEY, {})
        if not isinstance(p.get("ev"), list):
            p["ev"] = []
        return p

    def _own_run_id(self) -> str:
        if not self._own_run:
            type(self)._own_run = secrets.token_hex(6)
        return self._own_run

    def _own_pending_set(self, p: dict) -> None:
        """Очередь правок. Пустая очередь хранит run и n: номера правок в
        пределах запуска только растут, иначе после применения канона следующая
        правка ушла бы с n=1 и сервер отбросил бы её как повтор."""
        keep = {"run": p.get("run") or "", "n": int(p.get("n") or 0), "ev": p.get("ev") or []}
        if p.get("sent_at") and keep["ev"]:
            keep["sent_at"] = p["sent_at"]
        self.db.set_state(self._OWN_PENDING_KEY, json.dumps(keep, ensure_ascii=False)
                          if (keep["ev"] or keep["run"]) else "")

    def _own_files_fp(self) -> str:
        """sha256 обоих файлов своих списков: два чтения, без exec."""
        from awgbot.infra import gwguard
        h = hashlib.sha256()
        for name in gwguard.OWN_LIST_FILES:
            try:
                with open(os.path.join(gwguard.DNSMASQ_D, name), "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"-")
            h.update(b"\0")
        return h.hexdigest()

    def own_local(self) -> dict[str, str] | None:
        """{домен: вид} из файлов через скрипт; None — файлов нет (режим
        выключен, локальная сеть без VPN не применилась): отсутствие файла удалением не считается."""
        from awgbot.infra import gwguard
        if not all(os.path.exists(os.path.join(gwguard.DNSMASQ_D, n)) for n in gwguard.OWN_LIST_FILES):
            return None
        ok, out = gwguard.run_lan_domain("list", [])
        return gwownlists.parse_list(out) if ok else None

    def _own_add_events(self, changes, init: bool = False) -> list:
        """Дописать правки в pending под текущей меткой запуска; вернуть их."""
        p = self._own_pending_raw()
        run = self._own_run_id()
        if p.get("run") != run:
            # неотправленные правки прошлого запуска — под новую метку, с единицы
            p = {"run": run, "n": 0, "ev": [[i + 1, e[1], e[2], e[3] if len(e) > 3 else False]
                                            for i, e in enumerate(p.get("ev") or []) if len(e) >= 3]}
            p["n"] = len(p["ev"])
        new = []
        for d, kind in changes:
            p["n"] = int(p.get("n") or 0) + 1
            ev = [p["n"], d, kind, bool(init)]
            p["ev"].append(ev)
            new.append(ev)
        if new:
            p.pop("sent_at", None)
        self._own_pending_set(p)
        return new

    def own_unsent(self) -> bool:
        """Есть правки, которые ещё не уходили серверу в этой сессии: уже
        отправленные повторно на каждом тике не шлём — это был бы маячок;
        их повторит подключение (hello) или ответ сервера."""
        p = self._own_pending_raw()
        return bool(p.get("ev")) and not p.get("sent_at")

    def own_reconcile(self) -> bool:
        """Сверка файлов с базой ⊕ pending (тик монитора, после кнопки): нашлась
        правка мимо канона — событием в pending. True — нашлись новые правки.
        Базы нет — не сверяем: первый список уйдёт целиком с init по канону."""
        if not self.own_active():
            return False
        fp = self._own_files_fp()
        if fp == (self.db.get_state(self._OWN_FP_KEY) or ""):
            return False
        base = self.own_base()
        local = self.own_local()
        if local is None:
            return False
        self.db.set_state(self._OWN_FP_KEY, fp)
        if not base:
            return False
        expected = gwownlists.apply_events(base.get("items") or {}, self._own_pending_raw().get("ev"))
        changes = gwownlists.diff(local, expected)
        if changes:
            self._own_add_events(changes)
            log.info("свои списки: правок мимо канона — %s, уходят серверу", len(changes))
        return bool(changes)

    def own_pending_events(self) -> tuple[str, list]:
        """(run, события) для отправки серверу; пусто — ("", []). Момент первой
        отправки запоминается: по нему считается «сервер не отвечает»."""
        p = self._own_pending_raw()
        if not p.get("ev"):
            return "", []
        if p.get("run") != self._own_run_id():
            self._own_add_events([])          # перенумеровать под текущую метку
            p = self._own_pending_raw()
        if not p.get("sent_at"):
            p["sent_at"] = timeutil.to_iso(timeutil.now())
            self._own_pending_set(p)
        return str(p.get("run") or ""), [list(e) for e in p["ev"]]

    def own_applied_hash(self) -> str:
        return str(self.own_base().get("hash") or "")

    def own_rejected_in(self, rej) -> None:
        rows = [[str(r[0])[:64], str(r[1])[:64]] for r in (rej or [])
                if isinstance(r, (list, tuple)) and len(r) >= 2][:20]
        self.db.set_state(self._OWN_REJ_KEY, json.dumps(rows, ensure_ascii=False) if rows else "")

    def apply_own_lists(self, body: dict) -> dict:
        """Канон от сервера → файлы через `sync`: {ok, hash, n, error, skipped}.
        skipped — канон не применён по правилам сверки (правки не учтены, первая
        синхронизация, откат сервера): свои правки уходят ещё раз, ack не шлётся."""
        from awgbot.infra import gwguard
        digest = gwlink.clean_hex(body.get("hash"))
        gen = str(body.get("gen") or "")[:32]
        try:
            ver = int(body.get("ver") or 0)
        except (TypeError, ValueError):
            ver = 0
        items = gwownlists.clean(body.get("items") if isinstance(body.get("items"), list) else [])
        upto = body.get("upto") if isinstance(body.get("upto"), (list, tuple)) else ["", 0]
        self.own_rejected_in(body.get("rej"))
        if not gwguard.lan_mode():
            return self._own_store(digest, None, False, "VPN-транзит на шлюзе выключен")
        base = self.own_base()
        local = self.own_local()
        if local is None:
            return self._own_store(digest, None, False,
                                   "не найдены файлы своих списков — VPN-транзит недоступен")
        # первая синхронизация или откат сервера: весь свой список — событиями
        # init, один раз; когда сервер их разберёт (upto покроет), канон применится.
        # Список пуст — отправлять нечего, канон применяется сразу: иначе такой
        # шлюз после переустановки сервера не применил бы его никогда
        p = self._own_pending_raw()
        first = bool(local) and (not base or gen != base.get("gen") or ver < int(base.get("ver") or 0))
        if first and not any(len(e) > 3 and e[3] for e in p.get("ev") or []):
            self._own_add_events(sorted(local.items()), init=True)
            log.info("свои списки: первая синхронизация — %s доменов уходят серверу", len(local))
            return {"ok": False, "skipped": "init", "hash": digest, "n": 0, "error": ""}
        # свои правки, которых в каноне ещё нет, — канон не применяем
        run, n_up = str(upto[0] or ""), int(upto[1] or 0)
        if any(e for e in p.get("ev") or [] if p.get("run") != run or int(e[0]) > n_up):
            return {"ok": False, "skipped": "pending", "hash": digest, "n": 0, "error": ""}
        if not gwguard.lan_domain_has_sync():
            err = self._defer_for_reassert("скрипт своих списков без sync", self._OWN_DEFER_KEY, body,
                                           "скрипт старого образца")
            if not gwguard.lan_domain_has_sync():
                return self._own_store(digest, None, False, err)
            self.db.set_state(self._OWN_DEFER_KEY, "")     # реассерт уже положил скрипт
        canon = {"gen": gen, "ver": ver, "hash": digest, "items": items}
        if items == local:
            return self._own_store(digest, canon, True, "")
        try:
            os.makedirs(os.path.dirname(gwguard.OWN_LISTS_NEW), mode=0o700, exist_ok=True)
            with open(gwguard.OWN_LISTS_NEW, "w", encoding="utf-8") as f:
                f.write(gwownlists.render_sync(items))
        except OSError as e:
            return self._own_store(digest, None, False,
                                   f"{WRITE_ERROR}: {gwguard.os_error_text(e)}")
        ok, tail = gwguard.run_lan_domain("sync", [gwguard.OWN_LISTS_NEW])
        if ok:
            # база — то, что реально записано: скрипт мог отбросить хост Endpoint
            written = self.own_local()
            canon["items"] = items if written is None else written
        result = self._own_store(digest, canon if ok else None, ok,
                                 "" if ok else (tail or "скрипт своих списков отказал без объяснений"))
        if ok:
            log.info("свои списки: применён канон сервера (%s)", len(canon["items"]))
            # адреса новых доменов «в туннель» — в набор фоном (клиент канала
            # зовёт `fill`): dig по каждому не должен держать ни блокировку, ни канал
            new_vpn = sorted(d for d, k in canon["items"].items() if k == "vpn" and local.get(d) != "vpn")
            if new_vpn:
                try:
                    with open(gwguard.OWN_FILL_NEW, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_vpn) + "\n")
                    result["fill"] = gwguard.OWN_FILL_NEW
                except OSError as e:
                    log.warning("свои списки: файл для fill не записан: %s", gwguard.os_error_text(e))
        return result

    def own_fill(self, path: str) -> tuple[bool, str]:
        """Адреса новых доменов «в туннель» — в набор lan_vpn4 (режим fill
        скрипта): долго (dig по каждому), поэтому зовётся фоном после own_ack."""
        from awgbot.infra import gwguard
        return gwguard.run_lan_domain("fill", [path], timeout=gwguard.OWN_FILL_TIMEOUT)

    def _defer_for_reassert(self, why: str, key: str, payload, noun: str) -> str:
        """Обвязка старого образца (обновление агента положило новые скрипты, а
        юнит их не запускал): перевыставить обвязку — не чаще раза в 10 минут —
        и отложить присланное под key до повтора на тике; сервер второй раз за
        сессию то же не пришлёт. Возвращает текст ошибки для панели."""
        fixed = self._reassert_throttled(why)
        self.db.set_state(key, json.dumps(payload, ensure_ascii=False))
        return f"{noun} — обвязка перевыставляется" if fixed else f"{noun} — обвязка перевыставится в ближайшие минуты"

    def _own_store(self, digest: str, canon: dict | None, ok: bool, err: str) -> dict:
        with self.db.transaction():
            if ok and canon is not None:
                self.db.set_state(self._OWN_BASE_KEY, json.dumps(canon, ensure_ascii=False))
                p = self._own_pending_raw()
                self._own_pending_set({"run": p.get("run"), "n": p.get("n"), "ev": []})
                self.db.set_state(self._OWN_DEFER_KEY, "")
                self.db.set_state(self._OWN_FP_KEY, self._own_files_fp())
            self.db.set_state(self._OWN_ERR_KEY, err)
            self._nudge_note(self._OWN_NUDGE_KEY, err)
        return {"ok": ok, "hash": digest, "n": len((canon or {}).get("items") or {}), "error": err}


    # ── повтор после поломки на шлюзе ───────────────────────────────────────
    # Сервер за сессию один и тот же канон (записи SMB) второй раз не шлёт.
    # Отказ из-за поломки на шлюзе (нет места, диск только для чтения) чинят
    # руками, и после починки шлюз ждал бы переподключения канала. Поэтому
    # через _NUDGE_AFTER_S после отказа агент сам просит повтор — и дальше
    # не чаще того же интервала, пока отказ не снят.
    _NUDGE_AFTER_S = 600

    def _nudge_note(self, key: str, err: str) -> None:
        """Запомнить время отказа (нового) или снять, если отказа нет."""
        if not err:
            self.db.set_state(key, "")
            return
        cur = self.db.get_state_json(key, {})
        if cur.get("err") != err:
            self.db.set_state(key, json.dumps({"err": err, "since": timeutil.to_iso(timeutil.now()), "asked": ""}))

    def _nudge_due(self, key: str) -> bool:
        """Пора ли просить повтор: отказ стоит дольше интервала и с прошлой
        просьбы прошёл интервал. Только для поломки на шлюзе («непредвиденная
        ошибка…»: нет места, диск только для чтения) — её чинят руками, и после
        починки то же присланное применится. Отказ по содержимому (проверка
        строк, dnsmasq --test) или из-за обвязки повтором не лечится, и просьба
        каждые 10 минут была бы маячком в туннеле."""
        cur = self.db.get_state_json(key, {})
        if not str(cur.get("err") or "").startswith(WRITE_ERROR):
            return False
        now = timeutil.now()
        try:
            since = timeutil.parse_iso(cur.get("since") or "")
            asked = timeutil.parse_iso(cur["asked"]) if cur.get("asked") else None
        except ValueError:
            return False
        if (now - since).total_seconds() < self._NUDGE_AFTER_S:
            return False
        if asked is not None and (now - asked).total_seconds() < self._NUDGE_AFTER_S:
            return False
        cur["asked"] = timeutil.to_iso(now)
        self.db.set_state(key, json.dumps(cur))
        return True

    def own_nudge_due(self) -> bool:
        return self._nudge_due(self._OWN_NUDGE_KEY)

    def services_nudge_due(self) -> bool:
        return self._nudge_due(self._SVC_NUDGE_KEY)

    def own_retry(self) -> dict | None:
        """Отложенный канон (обвязка была старого образца): скрипт обновился —
        применить; None — повторять нечего."""
        from awgbot.infra import gwguard
        raw = self.db.get_state(self._OWN_DEFER_KEY) or ""
        if not raw:
            return None
        if not gwguard.lan_domain_has_sync():
            # первый реассерт мог не пройти (юнит был занят, троттлинг) —
            # пробуем снова, пока отложенное ждёт
            self._reassert_throttled("скрипт своих списков без sync")
            if not gwguard.lan_domain_has_sync():
                return None
        body = self.db.get_state_json(self._OWN_DEFER_KEY, {})
        self.db.set_state(self._OWN_DEFER_KEY, "")
        if not body.get("gen"):
            return None                               # мусор или пустая запись — применять нечего
        result = self.apply_own_lists(body)
        return None if result.get("skipped") else result

    def own_status(self) -> tuple[dict, list[GwCheck]]:
        """(блок панели, проверки группы «own»): видны в мониторе, уведомлений не
        шлют. state: synced | pending | no_link | stale_server | failed | rejected | old_script | off."""
        from awgbot.infra import gwguard
        info: dict = {"active": self.own_active()}
        checks: list[GwCheck] = []
        if not info["active"]:
            info["state"] = "off"
            # режим есть, канала нет — экран списков скажет, почему они только этого шлюза
            info["no_channel"] = gwguard.lan_mode() and gwguard.unit_env("LINK_CHANNEL") != "1"
            return info, checks
        p = self._own_pending_raw()
        info["pending"] = len(p.get("ev") or [])
        info["err"] = self.db.get_state(self._OWN_ERR_KEY) or ""
        info["rej"] = self.db.get_state_json(self._OWN_REJ_KEY, [])
        info["synced"] = bool(self.own_base())
        online = bool(getattr(getattr(self, "channel", None), "online", False))
        info["online"] = online
        waited = 0.0
        if p.get("sent_at"):
            try:
                waited = (timeutil.now() - timeutil.parse_iso(p["sent_at"])).total_seconds()
            except ValueError:
                waited = 0.0
        from awgbot.bot.texts.fmt import plural_ru
        n = info["pending"]
        n_word = f"{n} " + plural_ru(n, "правка", "правки", "правок")
        if not gwguard.lan_domain_has_sync():
            info["state"] = "old_script"
            checks.append(GwCheck("свои списки", None, "скрипт старого образца — обвязка перевыставляется"))
        elif info["err"]:
            info["state"] = "failed"
            checks.append(GwCheck("свои списки", False, failure_detail("не применились", info["err"])))
        elif n and online is False:
            info["state"] = "no_link"
            checks.append(GwCheck("свои списки", None, f"ждут синхронизации ({n_word}): нет связи с сервером AWG"))
        elif n and waited > self._OWN_STALE_SERVER_S:
            info["state"] = "stale_server"
            checks.append(GwCheck("свои списки", None,
                                  f"сервер AWG не отвечает на правки {int(waited // 60)} мин: обнови бот сервера AWG"))
        elif n:
            info["state"] = "pending"
            checks.append(GwCheck("свои списки", None, f"ждут синхронизации ({n_word}): сервер AWG ещё не ответил"))
        elif info["rej"]:
            info["state"] = "rejected"
            k = len(info["rej"])
            d, why = info["rej"][0]
            checks.append(GwCheck("свои списки", None,
                                  f"сервер AWG не принял {k} " + plural_ru(k, "домен", "домена", "доменов")
                                  + f": {d} — {why}"))
        else:
            info["state"] = "synced"
            checks.append(GwCheck("свои списки", True, "синхронизированы с сервером AWG"
                                  if info["synced"] else "ждут первой синхронизации с сервером AWG"))
        for c in checks:
            c.group = "own"
        return info, checks
