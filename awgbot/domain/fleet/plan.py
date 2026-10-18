"""plan.py — часть пакета fleet (см. __init__): выпуск набора, версии ролей в
выпуске, цель кнопки, участники и план для экрана и диалога.

Выпуск набора — версия сервера AWG (Р17): шлюзы следуют за ней вверх и вниз
в линии совместимости. Одно уточнение: выпуск только для шлюзов не двигает
версию сервера, и после зафиксированного хода к нему выпуском набора
остаётся его тег — пока версия сервера в нём та же, что стоит.

Версия роли в выпуске T — новейший выпуск ≤ T, адресованный роли
(`applies_to`); выпуск только для сервера шлюзы не трогает и наоборот. Цель
кнопки — новейший рассматриваемый выпуск для любой роли выше выпуска набора
(барьер пропущенных и потолок поколения — как у выбора цели роли).

Доступный шлюз — с живой сессией канала сейчас (Р22). Участник хода —
доступный, с устройством, умеющий команду (`upd1`) и на сборке из выпуска;
прочие (агент 3.3.x, своя сборка) никого не держат, их строки — в плане.
"""
from __future__ import annotations

from typing import Optional

from awgbot.core import config
from awgbot.infra import updates

from .state import RELEASE_KEY


def role_target(releases: list, role: str, upto: tuple):
    """Версия роли в выпуске: новейший выпуск ≤ upto, адресованный роли; None —
    такого нет."""
    best = None
    for r in releases:
        if r.version <= upto and r.applies_to(role) and (best is None or r.version > best.version):
            best = r
    return best


def _tag(ver: str) -> str:
    v = str(ver or "").strip()
    return v if not v or v.startswith("v") else f"v{v}"


class FleetPlanMixin:
    """Выпуск набора, участники, цель и план."""

    # ── выпуски ──────────────────────────────────────────────────────────────

    def fleet_releases(self) -> Optional[list]:
        """Сохранённый список выпусков (без сети) или None — проверки не было."""
        return self._update_cached_releases(strict=False)

    def fleet_release_of(self, tag: str):
        return next((r for r in self.fleet_releases() or [] if r.tag == tag), None)

    def fleet_server_release_build(self) -> bool:
        inst = updates.parse_version(config.INSTALLED_VERSION)
        releases = self.fleet_releases()
        return inst is not None and releases is not None and updates._is_release_build(inst, releases)

    def fleet_release(self) -> str:
        """Выпуск набора — тег («v3.4.0»); пусто — сервер не на сборке из
        выпуска или выпуски ещё не проверялись (набора нет)."""
        inst = updates.parse_version(config.INSTALLED_VERSION)
        releases = self.fleet_releases()
        if inst is None or releases is None or not updates._is_release_build(inst, releases):
            return ""
        # выпуск только для шлюзов поверх сервера — его пишет только фиксация
        # хода (неудачный следующий ход его не трогает); сервер сменил версию
        # сам — ключ не про него и не в счёт
        kept = str(self.db.get_state(RELEASE_KEY) or "")
        want = updates.parse_version(kept)
        if want is not None and want > inst:
            main = role_target(releases, "main", want)
            if main is not None and main.version == inst:
                return _tag(kept)
        return _tag(config.INSTALLED_VERSION)

    def fleet_gw_target(self, release: str = ""):
        """Версия агента в выпуске (Release) или None — шлюзы выпуск не трогает."""
        rel = release or self.fleet_release()
        ver = updates.parse_version(rel)
        releases = self.fleet_releases()
        if ver is None or releases is None:
            return None
        return role_target(releases, "gateway", ver)

    def fleet_pick(self, installed: tuple, releases: list, ceiling) -> Optional[object]:
        """Цель кнопки по списку (зовёт выбор цели под замком, cache.py):
        новейший рассматриваемый выпуск для любой роли выше выпуска набора."""
        base = updates.parse_version(self.fleet_release() or "") or installed
        barrier = next((r.version for r in releases if r.version > installed and not r.usable()), None)
        best = None
        for r in releases:
            if r.version <= base or (barrier is not None and r.version >= barrier):
                continue
            if r.withdrawn or not (r.applies_to("main") or r.applies_to("gateway")):
                continue                                  # снятый выпуск — не цель кнопки
            if ceiling is not None and r.awg_generation() > ceiling:
                continue
            if best is None or r.version > best.version:
                best = r
        return best

    def fleet_target(self):
        """Цель кнопки без сети — по сохранённому списку; None — обновлять не на что."""
        inst = updates.parse_version(config.INSTALLED_VERSION)
        releases = self.fleet_releases()
        if inst is None or releases is None or not updates._is_release_build(inst, releases):
            return None
        return self.fleet_pick(inst, releases, self._generation_ceiling())

    # ── шлюзы ────────────────────────────────────────────────────────────────

    def fleet_gateways(self) -> list[dict]:
        """Слоты для набора, по порядку хода: резервные (по номеру), активный
        последним. Поля: slot, name, label, version, online, seen, caps,
        active, device, upd (умеет команду), release (сборка из выпуска),
        participant."""
        releases = self.fleet_releases() or []
        active = self.active_gateway()
        out = []
        for gw in self.db.gateways():
            sess = self.gwlink_session(gw.id)
            ver = str(sess.get("agent") or "") if sess else ""
            if updates.parse_version(ver) is None:
                ver = str(self.gwlink_snapshot(gw.id).get("agent_version") or "")
            parsed = updates.parse_version(ver)
            if parsed is None:
                ver = ""
            device = self.db.get_device(gw.device_id) is not None
            caps = self.gwlink_agent_caps(gw.id)
            release = parsed is not None and bool(releases) and updates._is_release_build(parsed, releases)
            online = bool(sess) and device
            row = {"slot": gw.id, "name": self._gw_name(gw), "label": gw.label or "", "version": ver,
                   "online": online, "seen": self.db.get_state(self._gwlink_key(self._GWLINK_SEEN_KEY, gw.id)) or "",
                   "caps": caps, "active": active is not None and active.id == gw.id, "device": device,
                   "upd": "upd1" in caps, "release": release}
            row["participant"] = online and row["upd"] and release
            out.append(row)
        out.sort(key=lambda r: (r["active"], r["slot"]))
        return out

    def fleet_server_step(self, target):
        """(ступень сервера, нужны ли сначала шлюзы) к его версии в выпуске
        target. Ступень — тем же выбором цели роли, что и раньше, под потолком
        «≤ target»; условие `needs_gw` держат только участники хода (Р22).
        (None, False) — сервер уже на своей версии в выпуске."""
        inst = updates.parse_version(config.INSTALLED_VERSION)
        releases = self.fleet_releases() or []
        if inst is None or target is None:
            return None, False
        want = role_target(releases, "main", target.version)
        if want is None or want.version <= inst:
            return None, False
        capped = [r for r in releases if r.version <= target.version]
        from awgbot.domain.selfupdate import UpdatePeer
        peers = [UpdatePeer(slot=g["slot"], name=g["name"], version=g["version"], fresh=True)
                 for g in self.fleet_gateways() if g["participant"]]
        ceiling = self._generation_ceiling()
        step = updates.pick_target(inst, capped, "main", ceiling, self._update_gate(peers))
        if step is not None:
            return step, False
        return None, updates.pick_target(inst, capped, "main", ceiling) is not None

    # ── план для экрана и диалога ───────────────────────────────────────────

    def fleet_plan(self, target=None) -> dict:
        """Что покажут раздел и диалог: выпуск набора, цель, строки хостов.
        target — Release цели или None (раздел «актуально»)."""
        release = self.fleet_release()
        gw_now = self.fleet_gw_target(release)
        gw_want = self.fleet_gw_target(target.tag) if target is not None else None
        main_want = None
        needs_gw = False
        needs_ver = ""
        if target is not None:
            releases = self.fleet_releases() or []
            mt = role_target(releases, "main", target.version)
            inst = updates.parse_version(config.INSTALLED_VERSION)
            if mt is not None and inst is not None and mt.version > inst:
                main_want = mt.tag
                _step, needs_gw = self.fleet_server_step(target)
                need = mt.needs("main") if needs_gw else None
                needs_ver = "v" + ".".join(str(x) for x in need) if need else ""
        rows = []
        for g in sorted(self.fleet_gateways(), key=lambda r: r["slot"]):      # на экране — по номеру слота
            st = self.fleet_slot(g["slot"])
            want = gw_want.tag if gw_want is not None else (gw_now.tag if gw_now is not None else "")
            rows.append({**g, "want": want, "upd_state": st})
        return {"release": release, "tag": target.tag if target is not None else "",
                "body": getattr(target, "body", "") if target is not None else "",
                "server": _tag(config.INSTALLED_VERSION), "server_want": main_want,
                "gw_changes": gw_want is not None and (gw_now is None or gw_want.tag != gw_now.tag),
                "needs_gw": needs_gw, "needs_gw_ver": needs_ver, "gateways": rows, "run": self.fleet_run()}

    def fleet_run_plan(self) -> dict:
        """План для живого сообщения хода: цель — тег хода."""
        run = self.fleet_run()
        plan = self.fleet_plan(self.fleet_release_of(str(run.get("tag") or "")))
        plan["server_settled"] = self.fleet_server_settled()
        return plan

    def fleet_progress(self):
        """{"tag", "n", "total"} — строка главной, пока идёт ход; иначе None."""
        from .state import ACTIVE_RUN
        run = self.fleet_run()
        if run.get("state") not in ACTIVE_RUN:
            return None
        target = self.fleet_release_of(str(run.get("tag") or ""))
        steps = run.get("steps") or []
        gw_done = {int(s[1]) for s in steps if s[0] == "gw"}
        main = any(s[0] == "main" for s in steps)
        want = self.fleet_gw_target(target.tag) if target is not None else None
        left = 0
        if want is not None:
            left = sum(1 for g in self.fleet_gateways() if g["participant"] and g["slot"] not in gw_done
                       and updates.parse_version(g["version"]) != want.version)
        server_left = 0
        if not main and target is not None:
            step, _ = self.fleet_server_step(target)
            server_left = 1 if step is not None else 0
        done = len(gw_done) + (1 if main else 0)
        total = max(1, done + left + server_left)
        return {"tag": run.get("tag") or "", "n": min(done + 1, total), "total": total}

    def fleet_card(self, slot_id: int) -> dict:
        """Шлюз в наборе для карточки слота: строка плана, выпуск набора, ход."""
        release = self.fleet_release()
        want = self.fleet_gw_target(release)
        row = next((g for g in self.fleet_gateways() if g["slot"] == int(slot_id)), None)
        if row is None:
            return {}
        return {"row": {**row, "want": want.tag if want is not None else "", "upd_state": self.fleet_slot(slot_id)},
                "release": release, "run": self.fleet_run()}
