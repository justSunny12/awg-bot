"""
store.py — часть пакета reserve агента (см. __init__): раздел `reserve`
документа конфигурации на шлюзе — файл `/var/lib/awg-gw/reserve.json` (0600:
конфиг линка к резерву, адрес основы, состояние пересылки от основы с номером
переезда, адрес и имя резерва, имя записи, имя и слот основы, флаг `tg`;
точки ключа здесь нет — она в разделе `quorum`), подъём линка `awgrsv` скриптом (отказ — раздел принят, повтор
тактом раз в минуту, `link_pending`), решение «пересылка открыта или
закрыта», поле `rsv` снимка для сервера. Свои отметки хода (`_KEEP_KEYS`:
когда открыта пересылка, когда отдана точка ключа кворума, что сказано,
очередь уведомлений) новый документ не стирает.

Пересылку с `awgrsv` открывает только сам шлюз: по слову основы (`state =
away` в документе) или по `state{serving}` от резерва вместе с фактом
записи DNS на адрес резерва. Закрывает: `state = idle` от основы — только
если его номер переезда не старше последнего `serving` резерва, `state{idle}`
от резерва, раздел пропал. Факт записи «не знаю» (не прочитался, кэша нет)
решения не меняет. Источники пересылки — все AllowedIPs стороны зоны
(адрес зоны и подсети туннеля основы). Открыта ли пересылка — из файла
состояния скрипта, не вызовом скрипта. Состояние переживает перезагрузку (файл).
`state{idle, reason}` после отданной этим шлюзом точки (`qshare`) — переезд не
состоялся: админу шлюза одно уведомление (Т-РА5, со звуком), отметка
снимается. Т-РА3 — при любом открытии пересылки (по слову и по такту), один
раз на номер переезда.

Уведомления агента о резерве (Т-РА3, Т-РА5, обход Telegram) не теряются: Telegram
агента идёт по аплинку, а в момент события он может молчать. `reserve_note`
кладёт текст в очередь `pending_notes` reserve.json, доставка снимает его,
такт повторяет недоставленное раз в минуту (первая попытка — сразу, повтор —
не раньше минуты), пока оно ещё верно (`cond`: пересылка открыта — проверяется
в момент повтора) и не старше суток; Т-РА3 со звуком — ещё и
почтой (`critical`). Такт и слова резерва правят reserve.json по очереди
(замок в runtime/guestlink.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import livestep, timeutil

from ..base import log

STORE_PATH = Path("/var/lib/awg-gw/reserve.json")


def _read(path: Path = STORE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


# своё о ходе дел в reserve.json — переживает каждый новый документ
_KEEP_KEYS = ("open_at", "share_given_at", "reserve_state_at", "moved_told",
              "pending_notes", "link_pending", "link_retry_mono", "reserve_down_last",
              "tg_via", "tg_since", "reresolve_pending", "reresolved")
NOTE_TTL_S = 24 * 3600            # уведомление без доставки дольше — устарело
NOTE_RETRY_S = 60


class ReserveStoreMixin:
    _RSV_STORE = STORE_PATH

    # ── уведомления с доставкой ─────────────────────────────────────────────
    def reserve_note(self, cur: dict, text: str, *, sound: bool = False, silent: bool = False, cond: dict | None = None,
                     critical: bool = False):
        """Уведомление админу шлюза, которое не теряется: Telegram агента идёт по
        аплинку, а в момент события (основа мертва) аплинк как раз мёртв. Запись
        в очередь, Notification с on_sent — доставлено, снято; такт повторяет
        недоставленные, пока условие (`cond`) верно и не вышли сутки."""
        import secrets as _s
        import time as _t
        from awgbot.core import config
        from awgbot.domain.services import Notification
        nid = _s.token_hex(4)
        notes = [n for n in (cur.get("pending_notes") or []) if isinstance(n, dict)]
        # первая попытка — возвращаемое уведомление (уходит сейчас): повтор — не раньше NOTE_RETRY_S,
        # иначе такт слал бы второе, пока первое ещё в пути
        notes.append({"id": nid, "text": text, "sound": bool(sound), "silent": bool(silent), "critical": bool(critical),
                      "cond": dict(cond or {}), "at": timeutil.now_iso(), "tried": _t.monotonic()})
        cur["pending_notes"] = notes[-32:]
        return Notification(tg_id=int(config.ADMIN_ID), text=text, force_sound=sound, silent=silent, critical=critical,
                            on_sent=lambda nid=nid: self.reserve_note_sent(nid))

    def reserve_note_sent(self, nid: str) -> None:
        cur = self.reserve_section()
        notes = [n for n in (cur.get("pending_notes") or []) if isinstance(n, dict) and n.get("id") != nid]
        if len(notes) != len(cur.get("pending_notes") or []):
            cur["pending_notes"] = notes
            _write(cur, self._RSV_STORE)

    def reserve_notes_retry(self, now: float | None = None) -> list[tuple]:
        """Недоставленные уведомления — снова, раз в минуту, пока верны."""
        import time as _t
        from awgbot.core import config
        from awgbot.domain.services import Notification
        cur = self.reserve_section()
        notes = [n for n in (cur.get("pending_notes") or []) if isinstance(n, dict)]
        if not notes:
            return []
        now = _t.monotonic() if now is None else now
        keep, out, changed = [], [], False
        for n in notes:
            try:
                age = timeutil.elapsed(timeutil.parse_iso(str(n.get("at") or "")))
            except ValueError:
                age = NOTE_TTL_S + 1
            if age > NOTE_TTL_S:
                changed = True
                continue                                      # устарело — не слать
            if not livestep.mono_due(n.get("tried"), now, NOTE_RETRY_S):
                keep.append(n)
                continue
            # условие — только при попытке: Т-РА3 ставится в очередь до того, как runtime
            # откроет пересылку, — проверка в том же такте выбросила бы его до доставки
            if not self._note_still_true(cur, n.get("cond") or {}):
                changed = True
                continue                                      # уже не так — не слать
            keep.append(n)
            n["tried"] = now                                  # отметка попытки — в файл: иначе повтор каждый такт
            changed = True
            out.append(Notification(tg_id=int(config.ADMIN_ID), text=str(n["text"]), force_sound=bool(n.get("sound")),
                                    silent=bool(n.get("silent")), critical=bool(n.get("critical")),
                                    on_sent=lambda nid=str(n["id"]): self.reserve_note_sent(nid)))
        if changed:
            cur["pending_notes"] = keep
            _write(cur, self._RSV_STORE)
        return [("notify", out)] if out else []

    @staticmethod
    def _note_still_true(cur: dict, cond: dict) -> bool:
        if cond.get("open") and not rsvlink.is_open():
            return False                                      # Т-РА3: пересылка уже закрыта — переезд кончился
        return True

    def reserve_section(self) -> dict:
        return _read(self._RSV_STORE)

    def reserve_present(self) -> bool:
        return bool(self.reserve_section().get("conf"))

    def reserve_print(self) -> str:
        """Отпечаток стоящего раздела — в снимок (`cfg.reserve`)."""
        from awgbot.domain import gwcfg
        sec = self.reserve_section()
        if not sec.get("conf"):
            return ""
        return gwcfg.reserve_print({"link": {"conf": sec["conf"]},
                                    "state": sec.get("state") or "idle", "move": int(sec.get("move") or 0),
                                    "self": sec.get("self") or "", "chan": int(sec.get("chan") or 0), "tg": bool(sec.get("tg")),
                                    "peer_host": sec.get("peer_host") or "", "record": sec.get("record") or "",
                                    "name": sec.get("name") or "", "slot": int(sec.get("slot") or 0)})

    # ── применение раздела ──────────────────────────────────────────────────
    def reserve_apply(self, checked: dict | None) -> dict:
        """Раздел из документа (проверенный) или None — раздела у сервера нет.
        {"changed": bool, "note": str}."""
        cur = self.reserve_section()
        if checked is None:
            if not cur:
                return {"changed": False, "note": ""}
            self.reserve_remove()
            return {"changed": True, "note": "🛟 Резерв у сервера AWG снят — линк к резерву опущен"}
        new = {"conf": str(checked.get("conf_text") or ""),
               "state": str(checked.get("state") or "idle"), "move": int(checked.get("move") or 0),
               "self": str(checked.get("self") or ""), "name": str(checked.get("name") or ""),
               "peer_name": str(checked.get("peer_name") or ""),
               "slot": int(checked.get("slot") or cur.get("slot") or 0),
               "record": str(checked.get("record") or ""), "peer_host": str(checked.get("peer_host") or ""),
               "chan": int(checked.get("chan") or 0), "tg": bool(checked.get("tg")), "admins": list(self._reserve_admin_ips()),
               "peer_addr": _peer_addr(str(checked.get("conf_text") or "")),
               "reserve_state": cur.get("reserve_state") or "idle", "reserve_move": int(cur.get("reserve_move") or 0),
               "applied_at": timeutil.now_iso()}
        for k in _KEEP_KEYS:                                  # своё о ходе дел — не из документа, не стирать
            if k in cur:
                new[k] = cur[k]
        changed = any(cur.get(k) != new[k] for k in ("conf", "state", "move", "self", "chan", "tg"))
        # слово возврата: основа вернула пользователей (idle, номер не меньше номера резерва) — аплинк
        # на адрес основы по записи сразу, не через 180 с рукопожатия (концепт «кворум» §4.12, решение 17)
        done = dict(new.get("reresolved") or {})
        if new["state"] == "idle" and cur.get("state") == "away" and new["move"] >= int(new["reserve_move"] or 0) \
                and new.get("self") and int(done.get("back") or 0) < new["move"]:
            new["reresolve_pending"] = {"name": new["record"], "expect": new["self"], "why": "возврат", "move": new["move"]}
            new["reresolved"] = {**done, "back": new["move"]}
        # адреса админов сменились — тоже повод переподнять: набор rsv_admin4 заполняет только --up
        link_changed = cur.get("conf") != new["conf"] or not rsvlink.present() or cur.get("admins") != new["admins"]
        note = ""
        if link_changed:
            try:
                rsvlink.up(new["conf"], self._reserve_admin_ips())
                new.pop("link_pending", None)
                note = "🛟 Линк к резерву поднят" if not cur.get("conf") else "🛟 Линк к резерву переподнят на новых ключах"
                log.info("reserve: линк к резерву %s", "поднят" if not cur.get("conf") else "переподнят")
            except rsvlink.RsvError as e:
                # временный отказ (имя резерва не резолвится, модуль ещё не собран): раздел принят,
                # подъём повторит такт — иначе сервер считал бы раздел отвергнутым до смены отпечатка
                new["link_pending"] = str(e)[:200]
                new["link_retry_mono"] = 0.0
                note = f"⚠️ Линк к резерву не поднялся: {str(e)[:120]} — повторю"
                log.warning("reserve: линк к резерву не поднялся: %s — повтор тактом", e)
        _write(new, self._RSV_STORE)
        # пересылку по новому разделу решает такт (≤ 5 с): здесь её действия исполнить некому —
        # решение отсюда записало бы «открыта» и «Т-РА3 объявлено» без открытия и без уведомления
        return {"changed": changed, "note": note}

    def reserve_link_retry_tick(self, now: float | None = None) -> list[tuple]:
        """Линк к резерву не поднялся при применении раздела (или пропал с хоста):
        повтор раз в минуту, пока не встанет; встал — снимок серверу."""
        import time as _t
        cur = self.reserve_section()
        if not cur.get("conf"):
            return []
        if not cur.get("link_pending") and rsvlink.present():
            return []
        now = _t.monotonic() if now is None else now
        if not livestep.mono_due(cur.get("link_retry_mono"), now, 60):
            return []
        cur["link_retry_mono"] = now
        try:
            rsvlink.up(str(cur["conf"]), self._reserve_admin_ips())
        except rsvlink.RsvError as e:
            cur["link_pending"] = str(e)[:200]
            _write(cur, self._RSV_STORE)
            log.info("reserve: линк к резерву всё ещё не поднимается: %s", e)
            return []
        cur.pop("link_pending", None)
        _write(cur, self._RSV_STORE)
        log.info("reserve: линк к резерву поднят повтором")
        return [("push",)]

    def reserve_counters_cached(self) -> tuple[int, int] | None:
        """(rx, tx) линка к резерву — один раз на такт (такт и панель спрашивают
        порознь); такт сбрасывает кэш в начале (`reserve_counters_reset`)."""
        cache = self.__dict__.get("_rsv_counters")
        if cache is not None:
            return cache
        c = rsvlink.counters()
        self.__dict__["_rsv_counters"] = c
        return c

    def reserve_counters_reset(self) -> None:
        self.__dict__["_rsv_counters"] = None

    def reserve_remove(self) -> None:
        try:
            rsvlink.remove()
        except rsvlink.RsvError as e:
            log.warning("reserve: линк к резерву не снят: %s", e)
        try:
            os.unlink(str(self._RSV_STORE))
        except OSError:
            pass

    def _reserve_admin_ips(self) -> list[str]:
        from awgbot.infra import gwguard
        try:
            raw = gwguard.unit_env("ADMIN_IPS")
        except Exception:                                     # noqa: BLE001
            raw = ""
        return [a.strip() for a in str(raw or "").replace(",", " ").split() if a.strip()]

    # ── состояние резерва и пересылка ───────────────────────────────────────
    def reserve_state_in(self, state: str, move: int, reason: str = "", e: int = 0) -> list[tuple]:
        """`state{state, move[, reason]}` от резерва по гостевому каналу. `idle` с
        причиной после отданной этим шлюзом доли — переезд не состоялся: Т-РА5."""
        cur = self.reserve_section()
        if not cur:
            return []
        if state not in ("serving", "idle"):
            return []
        actions: list[tuple] = []
        if int(e or 0) and hasattr(self, "_quorum_epoch_in"):
            self._quorum_epoch_in(int(e))                     # эпоха линии от резерва (кворум) — голосующим
        if state == "idle" and reason and cur.get("share_given_at"):
            from awgbot.domain.notices import reserve as rn
            _w, since = self.quorum_own_view() if hasattr(self, "quorum_own_view") else ("", "")
            actions.append(("notify", [self.reserve_note(cur, rn.gateway_move_failed(
                str(cur.get("name") or ""), since or str(cur.get("share_given_at") or ""), reason), sound=True)]))
            cur["share_given_at"] = ""
        done = dict(cur.get("reresolved") or {})
        if state == "serving" and int(move) > int(done.get("away") or 0) and cur.get("peer_host") and cur.get("record"):
            # переезд: аплинк — на адрес второго сервера по записи сразу (§4.12); серия попыток на номер переезда —
            # слово приходит на taking, запись меняется позже, повтор ведёт tgvia
            cur["reresolve_pending"] = {"name": str(cur["record"]), "expect": str(cur["peer_host"]), "why": "переезд", "move": int(move)}
            cur["reresolved"] = {**done, "away": int(move)}
        cur["reserve_state"], cur["reserve_move"] = state, int(move)
        cur["reserve_state_at"] = timeutil.now_iso()
        _write(cur, self._RSV_STORE)
        return actions + self.reserve_forward_decide()

    def reserve_forward_decide(self) -> list[tuple]:
        """Открыть или закрыть пересылку с `awgrsv` по правилу раздела; действия
        для runtime: [("open", [подсети])] / [("close",)] / [] — без смены ничего."""
        cur = self.reserve_section()
        if not cur:
            return []
        want_open = False
        primary_away = cur.get("state") == "away"
        reserve_serving = cur.get("reserve_state") == "serving"
        is_open = rsvlink.is_open()
        if primary_away:
            want_open = True
        elif reserve_serving:
            fact = self.reserve_record_fact()
            if fact is None:
                want_open = is_open                           # факт не прочитался и кэша нет — ничего не менять
            else:
                want_open = bool(fact)
        if cur.get("state") == "idle" and int(cur.get("move") or 0) >= int(cur.get("reserve_move") or 0) and reserve_serving:
            # основа вернула пользователей и её слово новее: закрыть, даже если резерв ещё говорит serving
            want_open = False
        if want_open and not is_open:
            srcs = self._reserve_sources()
            cur["open_at"] = timeutil.now_iso()
            _write(cur, self._RSV_STORE)
            log.info("reserve: пересылка с awgrsv открыта для %s", ",".join(srcs))
            actions = [("open", srcs)]
            if hasattr(self, "_moved_notice"):
                actions += self._moved_notice(actions)        # Т-РА3 — при любом открытии, не только по слову state
            return actions
        if not want_open and is_open:
            log.info("reserve: пересылка с awgrsv закрыта")
            return [("close",)]
        return []

    def _reserve_sources(self) -> list[str]:
        """Что пускать с `awgrsv` при открытой пересылке: все AllowedIPs стороны зоны
        из конфига линка — адрес зоны (с него идут зонды живости бота основы) и
        все подсети туннеля основы (основная и окно переезда профилей); конфига
        нет — подсеть пользователей из окружения обвязки."""
        import re
        from awgbot.infra.nftguard.common import TG_OUT4
        cur = self.reserve_section()
        m = re.search(r"(?m)^\s*AllowedIPs\s*=\s*(.+)$", str(cur.get("conf") or ""))
        nets = [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []
        nets = [n for n in nets if n not in TG_OUT4]          # диапазоны Telegram — путь агента наружу, не источники
        if nets:
            return nets
        from awgbot.infra import gwguard
        try:
            net = str(gwguard.unit_env("CLIENT_SUBNET") or "").strip()
        except Exception:                                     # noqa: BLE001
            net = ""
        return [net] if net else []

    def reserve_record_fact(self) -> bool:
        """Запись для пользователей основы указывает на адрес резерва — факт у
        авторитетного сервера (кэш в record.py)."""
        from . import record
        cur = self.reserve_section()
        name = str(cur.get("record") or "")
        target = str(cur.get("peer_host") or "")
        if not name or not target:
            return False
        return record.points_to(name, target)                 # None — не прочиталось и кэша нет

    # ── панель (Т-РА1) ──────────────────────────────────────────────────────
    def reserve_panel_view(self) -> dict:
        """Строка панели и здоровье: имя резерва, связь по awgrsv (ok | suspect |
        down | absent, с какого момента), пересылка, счётчики линка, доля.
        Пусто — раздела нет."""
        cur = self.reserve_section()
        if not cur:
            return {}
        seen, since = self.reserve_seen() if hasattr(self, "reserve_seen") else ("", "")
        c = self.reserve_counters_cached() or (0, 0)
        return {"name": str(cur.get("name") or ""), "peer_name": str(cur.get("peer_name") or ""), "seen": seen or "absent", "since": since,
                "open": rsvlink.is_open(), "state": str(cur.get("reserve_state") or "idle"),
                "since_open": str(cur.get("open_at") or ""), "rx": int(c[0]), "tx": int(c[1]),
                "share": self._quorum_point_present(),             # «часть ключа» — точка ключа кворума
                "tg_via": bool(cur.get("tg_via")), "tg_since": str(cur.get("tg_since") or ""),   # Т-К23
                "uplink_ip": self._uplink_conf_ip(), "primary": str(cur.get("self") or "")}    # Т-К27

    def _quorum_point_present(self) -> bool:
        """Под кворумом доля прямой в раздел reserve не едет — точка ключа кворума лежит в разделе quorum."""
        sec = self.quorum_section() if hasattr(self, "quorum_section") else {}
        return bool((sec.get("point") or {}).get("y"))

    @staticmethod
    def _uplink_conf_ip() -> str:
        """Endpoint аплинка в конфиге — адрес, а не имя: за переездом не пойдёт (Т-К27);
        конфиг читается с диска, сети не трогает. Пусто — имя или аплинка нет."""
        from awgbot.core import config
        from awgbot.infra import gwguard
        from . import uplink
        try:
            iface = config.GW_UPLINK_IF or gwguard.uplink_interface()
        except Exception:                                     # noqa: BLE001
            iface = ""
        if not iface:
            return ""
        host, _port = uplink.conf_endpoint(iface)
        return host if host and uplink.is_ip(host) else ""

    # ── снимок для сервера ──────────────────────────────────────────────────
    def reserve_snapshot(self) -> dict | None:
        """Поле `rsv` снимка: связь с резервом, пересылка, состояние резерва. None — раздела нет."""
        cur = self.reserve_section()
        if not cur:
            return None
        seen, since = self.reserve_seen() if hasattr(self, "reserve_seen") else ("", "")
        return {"seen": seen or "absent", "since": since, "open": rsvlink.is_open(),
                "state": str(cur.get("reserve_state") or "idle")}


def _peer_addr(conf_text: str) -> str:
    """Адрес стороны зоны в /30 — первый AllowedIPs пира (/32)."""
    import re
    m = re.search(r"(?m)^\s*AllowedIPs\s*=\s*([0-9.]+)", conf_text or "")
    return m.group(1) if m else ""
