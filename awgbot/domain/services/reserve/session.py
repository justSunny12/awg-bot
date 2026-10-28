"""
session.py — часть пакета reserve (см. __init__): что сервисы отдают каналу
пары и что принимают из него.

Роль в линке: пригласивший слушает на своём адресе в /30 (`listener = 1` в
строке пары, в сопряжении — всегда), принявший подключается к адресу
пригласившего. Секрет канала — PSK линка пары после сопряжения, закрытый ключ
стороны принявшего до него. `st` — что сторона говорит о себе: готовность,
имя, версия, возможности, поколение AWG, порты пользователей, собственный адрес,
состояния направлений, что знаю о его пакете, `admin_id`, открыл ли его
админ мой бот. Уходит в начале сессии и по смене любого поля.
"""
from __future__ import annotations

import re
import time

from awgbot.core import config, settings
from awgbot.domain.services.types import Notification
from awgbot.util import pairproto, rsvjrn, timeutil

from ..gateway_link.common import log
from awgbot.domain.notices import reserve as rn

from . import compat
from .state import _LOCK

PAIR_CONFIRM_S = 10 * 60          # пара без первой сессии на новых ключах дольше — снимается


def _compat_reason(why: str) -> bool:
    """Причина про версию или возможности, а не про готовность, шлюз или пакет."""
    return any(w in why for w in ("верси", "выпуск", "младше", "обнов", "не умеет", "возможност"))


class ReserveSessionMixin:

    # ── адреса и ключ ───────────────────────────────────────────────────────

    def reserve_link_role(self) -> str:
        """`inviter` — слушаю, `acceptor` — подключаюсь, пусто — пары и сопряжения нет."""
        st = self.reserve_invite()
        if st.get("id") and st.get("state") in ("waiting", "connected", "accepted"):
            return "acceptor" if st.get("role") == "acceptor" else "inviter"
        peer = self.reserve_peer()
        if peer is None:
            return ""
        return "inviter" if int(peer.get("listener") or 0) else "acceptor"

    def _pair_cidr(self) -> str:
        peer = self.reserve_peer()
        if peer is not None:
            return str(peer.get("pair_cidr") or "")
        return str(self.reserve_invite().get("cidr") or "")

    @staticmethod
    def reserve_channel_port() -> int:
        return int(settings.get_int("app.reserve.pair_channel_port", pairproto.DEFAULT_PAIR_PORT) or pairproto.DEFAULT_PAIR_PORT)

    def reserve_listen_address(self) -> tuple[str, int] | None:
        _acc, inv = pairproto.pair_hosts(self._pair_cidr())
        return (inv, self.reserve_channel_port()) if inv else None

    def reserve_connect_address(self) -> tuple[str, int] | None:
        _acc, inv = pairproto.pair_hosts(self._pair_cidr())
        return (inv, self.reserve_channel_port()) if inv else None

    def reserve_channel_secret(self) -> str:
        """PSK линка пары (после сопряжения) или закрытый ключ стороны принявшего
        (в сопряжении: у принявшего — свой, у пригласившего — из вывода --stage)."""
        from awgbot.util import wgconf
        conf = self._pair_conf_text()
        if not conf:
            return ""
        try:
            parsed = wgconf.parse(conf)
            psk = parsed.get("Peer", "PresharedKey")
            if psk:
                return psk
        except Exception as e:                            # noqa: BLE001
            log.info("резерв: конфиг линка пары не разобран (%s) — ключ канала по приглашению", e)
        st = self.reserve_invite()
        if st.get("role") == "acceptor":
            m = re.search(r"^\s*PrivateKey\s*=\s*(\S+)", conf, re.M)
            return m.group(1) if m else ""
        return str(st.get("peer_priv") or "")

    def _pair_conf_text(self) -> str:
        env = self._pair_env()
        path = f"{config.AWG_DIR}/{env['PAIR_IF']}.conf"
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    # ── hello и st ──────────────────────────────────────────────────────────

    def reserve_hello_body(self) -> dict:
        return {**self._my_hello_fields(), "seen": ""}

    def reserve_st_body(self) -> dict:
        peer_admin = self.rsv_get("rsv_peer_admin")
        pkg_in = self.rsv_get("rsv_pkg_in")
        me = self._my_hello_fields()
        return {"ready": settings.get_bool("app.reserve.ready", False), "name": me["name"], "version": me["version"],
                "caps": me["caps"], "gen": me["gen"], "ports": me["ports"], "self": self._own_host_or_empty(),
                "out": self.reserve_out(), "in": self.reserve_in(), "gw": self.reserve_has_gateway(),
                "healthy": bool(self.reserve_away() and self._healthy_safe()), "can_return": bool(self._can_return_safe()),
                "pkg": {"gen": int(pkg_in.get("gen") or 0), "fp": str(pkg_in.get("fp") or "")},
                "in_kind": str(self.rsv_get("rsv_in").get("kind") or ""),
                "voters": self._voters_body(), "admin_id": int(config.ADMIN_ID), "admin_opened": bool(peer_admin.get("opened")),
                "mem_free": self.reserve_mem_free_mb(), "online": int(self.__dict__.get("_rsv_guest_online") or 0),
                "in_by": str(self.rsv_get("rsv_in").get("by") or ""), "zone": str(self._my_side().get("zone") or ""),
                **({"jrn": jrn} if (jrn := self._jrn_st_part()) else {})}

    def _jrn_st_part(self) -> dict:
        """Журнал в `st`: своя линия (эпоха, держатель, применённое, подтверждённое) и что
        храню о линии второй стороны (`keep`; с эпохой её бота у меня, если он поднят)."""
        if not hasattr(self, "reserve_keep_view") or not self.reserve_jrn_on():
            return {}
        try:
            meta = self.db.jrn_meta()
        except Exception as e:                                # noqa: BLE001
            if rsvjrn.schema_too_old(e):
                return {}
            raise
        out = {"mine": {"line": self.db.jrn_line(), "e": int(meta["epoch"]), "holder": str(meta["holder"]),
                        "applied": int(meta["applied"]), "acked": int(meta["acked"])}}
        keep = self.reserve_keep_view()
        if keep:
            ge = self._keep_guest_int("e")
            if ge:
                keep["guest_e"], keep["guest_fork"] = ge, self._keep_guest_int("fork")
                keep["guest_last"] = self._keep_guest_int("last")
            out["keep"] = keep
        return out

    @staticmethod
    def reserve_mem_free_mb() -> int:
        """Доступная память хоста (MemAvailable), МБ; не прочитать — 0 (без строки)."""
        try:
            with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0

    def reserve_guest_online_set(self, n: int) -> None:
        """Число устройств основы на связи у гостя (локальный канал) — в st основе («там M»)."""
        self.__dict__["_rsv_guest_online"] = int(n or 0)

    def _voters_body(self) -> list[dict]:
        """Мои шлюзы с агентом rsv1 — только номера слотов: имена устройств и
        мест второй стороне не раскрываются, в её текстах шлюз — «шлюз 2»."""
        try:
            return [{"slot": int(g["slot"])} for g in self._reserve_gateways_view() if g.get("rsv")][:16]
        except Exception:                                     # noqa: BLE001
            return []

    def reserve_st_if_changed(self) -> dict | None:
        """Тело `st`, если оно изменилось с последней отправки (направления,
        здоровье, рычаг, пакет…), иначе None: второй стороне нужна каждая смена,
        а не только начало сессии."""
        body = self.reserve_st_body()
        packed = self._st_packed(body)
        if self.__dict__.get("_rsv_st_sent") == packed:
            return None
        self.__dict__["_rsv_st_sent"] = packed
        return body

    def reserve_st_sent_reset(self) -> None:
        """Отправка не прошла: отпечаток обнулить — такт отправит снова."""
        self.__dict__.pop("_rsv_st_sent", None)

    def reserve_st_body_sent(self) -> dict:
        """Тело st для отправки сессией — с отметкой отпечатка, чтобы такт не слал дубль."""
        body = self.reserve_st_body()
        self.__dict__["_rsv_st_sent"] = self._st_packed(body)
        return body

    @staticmethod
    def _st_packed(body: dict) -> str:
        """Отпечаток смены `st`: без памяти и онлайна; у журнала — только линии, эпохи и
        снимок (номера растут с каждой правкой — st на каждую пачку был бы лишним)."""
        import json
        out = {k: v for k, v in body.items() if k not in ("mem_free", "online", "jrn")}
        jrn = body.get("jrn") or {}
        out["jrn"] = {"mine": {k: (jrn.get("mine") or {}).get(k) for k in ("line", "e", "holder")},
                      "keep": {k: (jrn.get("keep") or {}).get(k) for k in ("line", "e", "snap_i", "guest_e")}}
        return json.dumps(out, sort_keys=True)

    def _healthy_safe(self) -> bool:
        try:
            return bool(self.reserve_healthy())
        except Exception:                                     # noqa: BLE001
            return False

    def _can_return_safe(self) -> bool:
        try:
            if hasattr(self, "reserve_tail_return_why") and self.reserve_tail_return_why():
                return False                                  # мой хвост ещё переносится — возврат отказал бы
            return bool(self.reserve_can_return())
        except Exception:                                     # noqa: BLE001
            return False

    def reserve_st_in(self, body: dict) -> list[Notification]:
        """`st` другой стороны: запомнить, пересчитать готовность, заметить смену
        `admin_id` и адреса."""
        notes: list[Notification] = []
        with _LOCK:
            prev = self.reserve_peer_state()
            peer = compat.side(name=str(body.get("name") or ""), version=str(body.get("version") or ""),
                               caps=list(body.get("caps") or []), gen=int(body.get("gen") or 0),
                               ports=[int(p) for p in body.get("ports") or [] if isinstance(p, int)],
                               ready=bool(body.get("ready")),
                               has_gateway=bool(body.get("gw")) if "gw" in body else (bool(body.get("voters")) or bool(prev.get("gw"))),
                               zone=str(body.get("zone") or ""))
            peer.update(out=str(body.get("out") or ""), inn=str(body.get("in") or ""), self_host=str(body.get("self") or ""),
                        pkg=dict(body.get("pkg") or {}), voters=list(body.get("voters") or []),
                        admin_opened=bool(body.get("admin_opened")), at=timeutil.now_iso(), at_ts=time.time(),
                        healthy=bool(body.get("healthy")), can_return=bool(body.get("can_return")),
                        in_kind=str(body.get("in_kind") or ""),
                        mem_free=int(body.get("mem_free") or 0), there=int(body.get("online") or 0),
                        in_by=str(body.get("in_by") or ""), st_stale=False)
            # своё о сессии и связи (session, session_at, down_since, linked_msg …) — не из st, не стирать
            self.rsv_put("rsv_peer_state", {**prev, **peer})
            if hasattr(self, "reserve_pkg_resend_if_stale"):
                self.reserve_pkg_resend_if_stale()           # у резерва не тот пакет (или никакого) — отметка заново
            if hasattr(self, "reserve_jrn_keep_in"):
                jrn = body.get("jrn") if isinstance(body.get("jrn"), dict) else {}
                self.rsv_update("rsv_peer_state", jrn_keep=dict(jrn.get("keep") or {}) if isinstance(jrn.get("keep"), dict) else {})
                self.reserve_jrn_keep_in(jrn.get("keep") if isinstance(jrn.get("keep"), dict) else None)
                self.reserve_keep_mine_in(jrn.get("mine") if isinstance(jrn.get("mine"), dict) else None)
            row = self.reserve_peer() or {}
            fields = {}
            if peer["name"] and peer["name"] != row.get("name"):
                fields["name"] = peer["name"]
            if peer["self_host"] and peer["self_host"] != row.get("host"):
                fields["host"] = peer["self_host"]
            new_admin = int(body.get("admin_id") or 0)
            if new_admin and new_admin != int(row.get("peer_admin_id") or 0):
                fields["peer_admin_id"] = new_admin
                self.rsv_update("rsv_peer_admin", admin_id=new_admin, opened=False, bound_at=timeutil.now_iso())
                try:
                    from awgbot.core import access_cache
                    access_cache.invalidate_all()
                except Exception:                         # noqa: BLE001
                    pass
                if row.get("peer_admin_id"):
                    notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.peer_admin_changed(peer["name"] or row.get("name") or "резерв"),
                                              silent=True))
            if fields:
                self.db.reserve_peer_update(**fields)
            notes.extend(self._reserve_compat_notes(peer))
        return notes

    def _reserve_compat_notes(self, peer: dict) -> list[Notification]:
        """Т-РУ9: направление выключилось по версии или возможностям — один раз
        на расхождение, в обе стороны; ловит и своё обновление (сравнение с
        последним сосчитанным состоянием, а не с прежним `st`); вернулось —
        «снова совместим» один раз."""
        mine = self._my_side()
        out_ok, out_why = compat.direction_ready(mine, peer)
        in_ok, in_why = compat.direction_ready(peer, mine)
        marks = self.rsv_get("rsv_view").get("compat") or {}
        now = {"out": "" if out_ok or not _compat_reason(out_why) else out_why,
               "in": "" if in_ok or not _compat_reason(in_why) else in_why}
        if marks == now and "out" in marks:
            return []
        self.rsv_update("rsv_view", compat=now)
        name = peer.get("name") or "резерв"
        notes = []
        for key in ("out", "in"):
            if now[key] and now[key] != marks.get(key):
                notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.direction_off(name, now[key])))
        if "out" in marks and (marks.get("out") or marks.get("in")) and not (now["out"] or now["in"]):
            notes.append(Notification(tg_id=int(config.ADMIN_ID), text=rn.direction_on(name)))
        return notes

    def _my_side(self) -> dict:
        return compat.my_side(ready=settings.get_bool("app.reserve.ready", False),
                              has_gateway=self.reserve_has_gateway(), ports=self._client_ports())

    def reserve_directions(self) -> dict:
        """{"out": (готово, причина), "in": (готово, причина)} по последнему st."""
        peer = self.reserve_peer_state()
        if not peer.get("version"):
            return {"out": (False, "второй сервер ещё не вышел на связь"), "in": (False, "второй сервер ещё не вышел на связь")}
        mine = self._my_side()
        sent = self.rsv_get("rsv_pkg_sent")
        fresh = bool(sent.get("at")) and timeutil.elapsed(timeutil.parse_iso(sent["at"])) < 2 * 3600 if sent.get("at") else False
        pkg_here = self.reserve_pkg_in_present() if hasattr(self, "reserve_pkg_in_present") else True
        inn = compat.direction_ready(peer, mine)
        if inn[0] and not pkg_here:
            inn = (False, f"данных для переезда от «{peer.get('name') or 'резерв'}» ещё нет")
        return {"out": compat.direction_ready(mine, peer, pkg_fresh=fresh or not sent), "in": inn}

    # ── сессия ───────────────────────────────────────────────────────────────

    def reserve_session_opened(self, hello: dict) -> list[Notification]:
        """Сессия на линке пары открыта: пара подтверждена (первая сессия на новых
        ключах), стороны зоны — заново при каждой сессии (резерв мог быть
        восстановлен из копии без них; `slots` идемпотентен)."""
        self.rsv_update("rsv_peer_state", session=True, session_at=timeutil.now_iso(), confirmed=True)
        self.rsv_update("rsv_view", slots_sent=[], slots_at="")
        if hasattr(self, "_jrn_sess_reset"):
            self._jrn_sess_reset()                            # журнал: ничего не слать до сверки по st резерва
        return self.reserve_liveness_word()

    def reserve_pair_confirm_tick(self) -> list[Notification]:
        """Пара записана, а сессии на новых ключах за PAIR_CONFIRM_S не было:
        сопряжение разошлось (pair_admin ушёл в сокет, но не дошёл) — пару
        снять, админу сказать; вторая сторона снимет своё тем же тактом."""
        if not self.reserve_paired():
            return []
        ps = self.reserve_peer_state()
        if ps.get("confirmed", True) or not ps.get("linked_at"):
            return []
        try:
            if timeutil.elapsed(timeutil.parse_iso(str(ps["linked_at"]))) < PAIR_CONFIRM_S:
                return []
        except ValueError:
            return []
        name = self.reserve_peer_name()
        log.warning("резерв: пара с «%s» не подтвердилась сессией за %d с — снимаю", name, PAIR_CONFIRM_S)
        try:
            self.reserve_unpair("unconfirmed")
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: неподтверждённая пара не снята: %s", e)
            return []
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.pair_unconfirmed(name))]

    def reserve_session_closed(self) -> None:
        self.rsv_update("rsv_peer_state", session=False)

    def reserve_last_word(self) -> list[Notification]:
        return self.reserve_liveness_word()

    def reserve_linked_msg(self) -> list:
        """Живое сообщение приглашения ([chat, mid]) — итогу Т-Р8 после сопряжения; одноразово."""
        ps = self.reserve_peer_state()
        msg = list(ps.get("linked_msg") or [])
        if msg:
            self.rsv_update("rsv_peer_state", linked_msg=[])
        return msg

    def reserve_online(self) -> bool:
        return bool(self.reserve_peer_state().get("session"))

    def reserve_peer_gone(self) -> list[Notification]:
        """`gone` от второй стороны: он удалил пару — снимаю у себя."""
        name = self.reserve_peer_name()
        try:
            self.reserve_unpair("peer")
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: пара не снята по слову второй стороны: %s", e)
            return []
        return [Notification(tg_id=int(config.ADMIN_ID), text=rn.peer_unpaired(name))]

