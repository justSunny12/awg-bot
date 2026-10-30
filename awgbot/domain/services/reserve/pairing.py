"""
pairing.py — часть пакета reserve (см. __init__): сопряжение серверов кодом.

Две роли одного механизма:

- **принявший** («📥 Принять приглашение»): разбор строки `AWGP1.…` и
  проверки (Т-Р4), диалог, линк пары на ключах приглашения
  (`reserve-pair-setup.sh --join`), эфемерная пара X25519 в памяти, `hello`
  по каналу пары; из ответа `pair_eph` — код для своего админа (Т-Р6); по
  `pair_ok` (под ключом сопряжения) — PSK и новые ключи линка, `admin_id`
  пригласившего; ответ `pair_admin` со своим `admin_id`; линк — на новые ключи
  (`--set-peer`), пара записана;
- **пригласивший**: `hello{pair, eph}` сессии сопряжения → `pair_eph`, тот же
  код; ввод кода в чате (Т-Р7): формат — переспрос, не совпал — попытка,
  третья — приглашение закрыто; верный — `--rekey-stage` и `pair_ok` с
  ключами под ключом сопряжения; по `pair_admin` — `--rekey-commit`, пара
  записана.

Код — SAS из обоих эфемерных ключей и общего секрета (`util/joinsas`, домен
пары); ключ сопряжения — из того же секрета. Эфемерные ключи и код в базу не
пишутся — только в памяти процесса (перезапуск рвёт сессию, новая даёт новый
код). После сопряжения у обеих сторон — первое поколение своих ключей
переезда и аварийный код своему админу (Т-Р18).
"""
from __future__ import annotations

import base64
import hashlib
import secrets

from awgbot.core import config, settings
from awgbot.domain.services.types import ServiceError
from awgbot.util import joinsas, pairtoken, timeutil

from ..gateway_link.common import log
from . import compat
from .invite import INVITE_TTL, MAX_ATTEMPTS, MAX_SESSIONS, OPEN, PAIRING, _live, _live_set
from .state import _LOCK


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(text) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text or "") + "==")
    except (ValueError, TypeError):
        return b""


def _from_ts(ts: int):
    from datetime import datetime
    return datetime.fromtimestamp(int(ts), timeutil.TZ)


class ReservePairingMixin:

    # ── общее: кто я в сопряжении ───────────────────────────────────────────

    def reserve_pairing_role(self) -> str:
        """`inviter` / `acceptor` / пусто."""
        st = self.reserve_invite()
        if not st.get("id") or st.get("state") not in PAIRING:
            return ""
        return str(st.get("role") or "inviter")

    def _my_hello_fields(self) -> dict:
        """Что о себе говорит сторона в `hello`/`pair_eph`/`st`."""
        return {"name": str(config.SERVER_NAME or ""), "version": str(config.INSTALLED_VERSION).lstrip("v"),
                "caps": compat.caps(), "gen": self._awg_generation(), "ports": self._client_ports(),
                "bot": getattr(self, "bot_username", "") or "", "gw": self.reserve_has_gateway()}

    def _client_ports(self) -> list[int]:
        ports = [int(config.SERVER_PORT or 0)]
        if config.MIGRATION_INTERFACE:
            try:
                from awgbot.infra import awg
                p = int(awg.read_server_params(iface=config.MIGRATION_INTERFACE).get("listen_port") or 0)
                if p:
                    ports.append(p)
            except Exception:                             # noqa: BLE001
                pass
        return [p for p in ports if p]

    # ── принявший: разбор и проверки ────────────────────────────────────────

    def reserve_accept_parse(self, text: str) -> dict:
        """Строка приглашения → {"token", "tok", "warn": [строки], "ask_version": bool}.
        Отказ — ServiceError с текстом Т-Р4."""
        raw = pairtoken.find(text) or str(text or "").strip()
        try:
            tok = pairtoken.decode(raw)
        except pairtoken.TokenError as e:
            msg = str(e)
            if "не приглашение" in msg:
                raise ServiceError("Это не приглашение в резерв")
            if "формата" in msg:
                raise ServiceError("Приглашение другого формата — обнови бот")
            raise ServiceError("Приглашение повреждено — попроси переслать его заново")
        if tok.expires and tok.expires <= int(timeutil.now().timestamp()):
            raise ServiceError(f"Срок приглашения вышел в {timeutil.fmt_clock_ui(_from_ts(tok.expires))} — попроси новое")
        if self.reserve_paired():
            raise ServiceError(f"Резерв уже есть — «{self.reserve_peer_name()}». Сначала удали его")
        mine = self.reserve_invite()
        if mine.get("id") == tok.pair_id:
            raise ServiceError("Это приглашение от этого же сервера")
        if not settings.get_bool("app.reserve.ready", False):
            raise ServiceError("Сначала включи «Готов быть резервом»")
        my_gen = self._awg_generation()
        if tok.awg_gen and my_gen and tok.awg_gen != my_gen:
            raise ServiceError(f"У второго сервера AmneziaWG поколения {tok.awg_gen}, здесь — {my_gen}: пользователей "
                               "друг друга серверы не обслужат. Сначала обновитесь до одного выпуска")
        warn = []
        if not self.reserve_has_gateway():
            warn.append("У этого сервера шлюзов нет — твои пользователи к нему не переедут, пока не подключишь шлюз; "
                        "его пользователей этот сервер принять сможет")
        from awgbot.core.compat import version_key
        if version_key(tok.version) and version_key(tok.version)[:3] < (4, 2, 0):
            raise ServiceError(f"«{tok.name}» на v{tok.version} — для резерва нужен 4.2.0 или новее у обоих серверов")
        ask_version = bool(version_key(tok.version) and version_key(config.INSTALLED_VERSION)
                           and version_key(tok.version)[:3] > version_key(config.INSTALLED_VERSION)[:3])
        return {"token": raw, "tok": tok, "warn": warn, "ask_version": ask_version}

    def reserve_accept_go(self, text: str, rf_via_mine: bool) -> dict:
        """«🛟 Подключиться»: линк на ключах приглашения, запись принявшего,
        эфемерная пара; подключается клиент канала пары."""
        info = self.reserve_accept_parse(text)
        tok = info["tok"]
        self.reserve_pin_self_host()
        self.reserve_require_zone_tools()
        with _LOCK:
            if self.reserve_invite().get("id"):
                self._invite_rollback("replaced")
            env = {"PAIR_IF": "awgpair", "PAIR_CIDR": tok.cidr, "PAIR_PORT": str(self._free_pair_port()),
                   "PAIR_ID": tok.pair_id, "PAIR_PRIV": tok.peer_priv_b64, "PAIR_PEER_PUB": tok.inviter_pub_b64,
                   "PAIR_ENDPOINT": f"{tok.host}:{tok.pair_port}", **tok.obfuscation()}
            env = {k.upper() if k in ("Jc", "Jmin", "Jmax") else k: v for k, v in env.items()}
            self._run_pair_script("--join", env)
            now = timeutil.now()
            st = {"id": tok.pair_id, "role": "acceptor", "pair_if": "awgpair", "port": int(env["PAIR_PORT"]),
                  "peer_port": int(tok.pair_port), "cidr": tok.cidr, "host": tok.host,
                  "peer_version": tok.version, "rf_via_mine": bool(rf_via_mine),
                  "issued": timeutil.to_iso(now), "expires": timeutil.to_iso(_from_ts(tok.expires) if tok.expires
                                                                             else timeutil.shift(now, INVITE_TTL)),
                  "sessions": 0, "attempts": 0, "msg": [], "state": "waiting"}
            self.rsv_put("rsv_invite", st)
            priv, pub = joinsas.new_pair()
            _live_set({"id": tok.pair_id, "priv": priv, "pub": pub})
        settings.set_value("app.reserve.rf_via_mine", bool(rf_via_mine)) if hasattr(settings, "set_value") else None
        try:
            self._gw_firewall_refresh(strict=True)
        except ServiceError as e:
            self._invite_rollback("firewall")
            raise ServiceError(f"Подключение не начато — {e}") from None
        log.info("резерв: принимаю приглашение %s… от %s", tok.pair_id[:4], tok.host)
        return st

    @staticmethod
    def _free_pair_port() -> int:
        return int(settings.get("app.reserve.pair_port", 47101) or 47101)

    def reserve_accept_hello_body(self) -> dict:
        """Тело `hello` сессии сопряжения принявшего."""
        st, live = self.reserve_invite(), _live()
        if st.get("role") != "acceptor" or not live.get("pub"):
            return {}
        return {"pair": st["id"], "eph": _b64u(live["pub"]), **self._my_hello_fields(),
                "seen": "", "nonce": ""}

    def reserve_accept_eph_in(self, msg: dict) -> dict:
        """`pair_eph` от пригласившего: код для своего админа. {"code", "peer"}."""
        with _LOCK:
            st, live = self.reserve_invite(), _live()
            if st.get("role") != "acceptor" or not live.get("priv"):
                raise ServiceError("Сопряжения нет")
            i_eph = _unb64u(msg.get("eph"))
            if len(i_eph) != 32:
                raise ServiceError("Ответ второго сервера повреждён")
            k = joinsas.shared(live["priv"], i_eph)
            code = joinsas.code(st["id"], None, live["pub"], i_eph, k, tag=joinsas.PAIR_SAS_TAG)
            live.update(code=code, kp=joinsas.pair_key(st["id"], k, tag=joinsas.PAIR_KEY_TAG),
                        peer=compat.side(name=str(msg.get("name") or ""), version=str(msg.get("version") or ""),
                                         caps=list(msg.get("caps") or []), gen=int(msg.get("gen") or 0),
                                         ports=list(msg.get("ports") or []), ready=True, has_gateway=bool(msg.get("gw"))))
            _live_set(live)
            st.update(state="connected", peer_name=live["peer"]["name"], peer_bot=str(msg.get("bot") or ""))
            self.rsv_put("rsv_invite", st)
            return {"code": code, "peer": live["peer"]}

    def reserve_accept_ok_in(self, msg: dict) -> dict:
        """`pair_ok` (под ключом сопряжения): PSK, новые ключи линка, `admin_id`
        пригласившего — проверить и отложить до `reserve_accept_commit`.
        Возвращает тело `pair_admin` для ответа. Линк на новые ключи переводится
        только ПОСЛЕ отправки `pair_admin`: пригласивший до него живёт на ключах
        приглашения, и сообщение, посланное в линк на новых ключах и новой
        обфускации, до него не дошло бы."""
        with _LOCK:
            st, live = self.reserve_invite(), _live()
            if st.get("role") != "acceptor" or not live.get("kp"):
                raise ServiceError("Сопряжения нет")
            data = joinsas.unseal(live["kp"], str(msg.get("sealed") or ""))
            need = ("psk", "priv", "peer_pub", "admin_id", "obf", "self_host")
            if any(k not in data for k in need):
                raise ServiceError("Секреты сопряжения не того вида")
            env = {**self._pair_env(), "PAIR_PRIV": str(data["priv"]), "PAIR_PEER_PUB": str(data["peer_pub"]),
                   "PAIR_PSK": str(data["psk"]), **{k.upper() if k in ("Jc", "Jmin", "Jmax") else k: str(v)
                                                    for k, v in dict(data["obf"]).items()}}
            reply = {"sealed": joinsas.seal(live["kp"], {"admin_id": int(config.ADMIN_ID),
                                                         "self_host": self._own_host_or_empty()})}
            live["accept"] = {"env": env, "host": str(data.get("self_host") or st.get("host") or ""),
                              "admin_id": int(data["admin_id"])}
            _live_set(live)
            return reply

    def reserve_pair_live_code(self) -> str:
        """Код сопряжения принявшего (живёт в памяти до итога) — для перерисовки экрана."""
        live = _live()
        return str((live or {}).get("code") or "")

    def reserve_accept_commit(self) -> None:
        """`pair_admin` ушёл (drain) — пара записана, линк на новые ключи (`--set-peer`)."""
        with _LOCK:
            st, live = self.reserve_invite(), _live()
            acc = live.get("accept") if live else None
            if st.get("role") != "acceptor" or not acc:
                raise ServiceError("Сопряжения нет")
            self._pair_linked(st, peer_name=str(st.get("peer_name") or live.get("peer", {}).get("name") or ""),
                              host=str(acc["host"]), peer_admin_id=int(acc["admin_id"]),
                              bot_username=str(st.get("peer_bot") or ""), listener=False,
                              peer_port=int(st.get("peer_port") or 0), peer=live.get("peer") or {})
            self._run_pair_script("--set-peer", acc["env"])

    def reserve_accept_abort(self, why: str) -> None:
        """`pair_admin` не ушёл: пары нет, линк приглашения прочь (пригласивший
        снимет своё по сроку или по следующей сессии)."""
        with _LOCK:
            if self.reserve_invite().get("id"):
                self._invite_rollback(why)

    # ── пригласивший: сессия и код ──────────────────────────────────────────

    def reserve_pair_hello(self, msg: dict) -> dict:
        """`hello{pair, eph}` сессии сопряжения у пригласившего. {"ok", "body"
        (pair_eph), "sid", "event": connected | reconnected | two, "why"}."""
        with _LOCK:
            st = self.reserve_invite()
            if not st.get("id") or str(msg.get("pair") or "") != st["id"] or st.get("state") not in PAIRING \
                    or st.get("role") not in (None, "", "inviter"):
                return {"ok": False, "why": "gone"}
            if self._invite_expired(st):
                st.update(state="closing", why="expired")
                self.rsv_put("rsv_invite", st)
                return {"ok": False, "why": "expired", "st": st}
            if int(st.get("sessions") or 0) >= MAX_SESSIONS:
                return {"ok": False, "why": "sessions"}
            a_eph = _unb64u(msg.get("eph"))
            if len(a_eph) != 32:
                return {"ok": False, "why": "bad"}
            if not bool(msg.get("gw")) and not self.reserve_has_gateway():
                # ни у кого нет шлюза — переезд невозможен ни в одну сторону (Т-Р4)
                return {"ok": False, "why": "nogw"}
            if "quo1" not in (msg.get("caps") or []):
                # второй сервер ниже 4.2.0: переезд решает кворум, прежних правил нет — пара не создаётся;
                # приглашение живо — код можно ввести после обновления
                return {"ok": False, "why": "old"}
            if st.get("state") == "accepted" and st.get("eph") != hashlib.sha256(a_eph).hexdigest():
                return {"ok": False, "why": "gone"}
            st["sessions"] = int(st.get("sessions") or 0) + 1
            addr = str(msg.get("_peer_addr") or "")
            was = list(st.get("endpoints") or [])
            if addr and addr not in was:
                st["endpoints"] = (was + [addr])[-4:]
            i_priv, i_pub = joinsas.new_pair()
            k = joinsas.shared(i_priv, a_eph)
            sid = secrets.token_hex(4)
            peer = compat.side(name=str(msg.get("name") or ""), version=str(msg.get("version") or ""),
                               caps=list(msg.get("caps") or []), gen=int(msg.get("gen") or 0),
                               ports=list(msg.get("ports") or []), ready=True, has_gateway=bool(msg.get("gw")))
            _live_set({"id": st["id"], "sid": sid, "eph": hashlib.sha256(a_eph).hexdigest(),
                       "kp": joinsas.pair_key(st["id"], k, tag=joinsas.PAIR_KEY_TAG),
                       "code": joinsas.code(st["id"], None, a_eph, i_pub, k, tag=joinsas.PAIR_SAS_TAG),
                       "peer": peer, "addr": addr})
            event = "reconnected" if st.get("state") in ("connected", "accepted") or int(st["sessions"]) > 1 else "connected"
            if len(st.get("endpoints") or []) > 1:
                event = "two"
            if st.get("state") != "accepted":
                st["state"] = "connected"
            st.update(peer_name=peer["name"], peer_bot=str(msg.get("bot") or ""), peer_version=peer["version"],
                      peer_ports=peer["ports"], peer_gw=peer["gw"])
            self.rsv_put("rsv_invite", st)
            body = {"eph": _b64u(i_pub), **self._my_hello_fields()}
        log.info("резерв: сопряжение — сессия %s (%s)", st["sessions"], event)
        return {"ok": True, "body": body, "sid": sid, "event": event, "st": st}

    def reserve_pair_session_closed(self, sid: str) -> None:
        with _LOCK:
            if _live().get("sid") == sid:
                _live_set(None)

    def reserve_pair_wait(self) -> str:
        """«✏️ Ввести код»: ask — можно; wait — резерв не подключился; gone."""
        st = self.reserve_invite()
        if not st.get("id") or st.get("state") not in OPEN or self._invite_expired(st):
            return "gone"
        return "ask" if _live().get("code") else "wait"

    def reserve_pair_code(self, text: str) -> dict:
        """Код из чата пригласившего. {"result": format | wait | gone | expired |
        wrong | refused | ok, "left", "st", "sid"}."""
        code = joinsas.normalize(text)
        if code is None:
            return {"result": "format"}
        with _LOCK:
            st = self.reserve_invite()
            if not st.get("id") or st.get("state") not in OPEN:
                return {"result": "gone", "st": st}
            if self._invite_expired(st):
                st.update(state="closing", why="expired")
                self.rsv_put("rsv_invite", st)
                return {"result": "expired", "st": st}
            live = _live()
            if not live.get("code"):
                return {"result": "wait", "st": st}
            if joinsas.same(code, live["code"]):
                st.update(state="accepted", eph=live["eph"], accepted_at=timeutil.now_iso())
                self.rsv_put("rsv_invite", st)
                log.info("резерв: сопряжение — код верный")
                return {"result": "ok", "st": st, "sid": live.get("sid")}
            st["attempts"] = int(st.get("attempts") or 0) + 1
            left = MAX_ATTEMPTS - st["attempts"]
            if left <= 0:
                st.update(state="closing", why="code")
                self.rsv_put("rsv_invite", st)
                return {"result": "refused", "left": 0, "st": st, "sid": live.get("sid")}
            self.rsv_put("rsv_invite", st)
            return {"result": "wrong", "left": left, "st": st, "sid": live.get("sid")}

    def reserve_pair_ok_body(self) -> dict:
        """После верного кода: новые ключи линка (`--rekey-stage`) и тело
        `pair_ok` под ключом сопряжения."""
        with _LOCK:
            st, live = self.reserve_invite(), _live()
            if st.get("state") != "accepted" or not live.get("kp"):
                return {}
            kv = self._run_pair_script("--rekey-stage", self._pair_env())
            st["rekey"] = {"vps_pub": kv.get("PAIR_VPS_PUB", "")}
            self.rsv_put("rsv_invite", st)
            obf = {k: kv[k] for k in ("JC", "JMIN", "JMAX", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4") if k in kv}
            return {"sealed": joinsas.seal(live["kp"], {
                "psk": kv.get("PAIR_PSK", ""), "priv": kv.get("PAIR_PEER_PRIV", ""), "peer_pub": kv.get("PAIR_VPS_PUB", ""),
                "obf": obf, "admin_id": int(config.ADMIN_ID), "self_host": self._own_host_or_empty()})}

    def reserve_pair_admin_in(self, msg: dict) -> None:
        """`pair_admin` принявшего: его `admin_id`; линк на новые ключи
        (`--rekey-commit`), пара записана, приглашение закрыто."""
        with _LOCK:
            st, live = self.reserve_invite(), _live()
            if st.get("state") != "accepted" or not live.get("kp"):
                raise ServiceError("Сопряжения нет")
            data = joinsas.unseal(live["kp"], str(msg.get("sealed") or ""))
            self._run_pair_script("--rekey-commit", self._pair_env())
            try:
                self._run_pair_script("--rekey-drop", self._pair_env())
            except ServiceError as e:
                log.warning("резерв: прежние ключи линка пары не убраны: %s", e)
            self._pair_linked(st, peer_name=str(st.get("peer_name") or ""),
                              host=str(data.get("self_host") or live.get("addr") or ""),
                              peer_admin_id=int(data.get("admin_id") or 0), bot_username=str(st.get("peer_bot") or ""),
                              listener=True, peer_port=0, peer=live.get("peer") or {})

    # ── общее: пара записана ────────────────────────────────────────────────

    def _own_host_or_empty(self) -> str:
        from awgbot.domain.services.selfhost import own_host
        return own_host()

    def _pair_linked(self, st: dict, *, peer_name: str, host: str, peer_admin_id: int, bot_username: str,
                     listener: bool, peer_port: int, peer: dict) -> None:
        self.db.reserve_peer_set(name=peer_name or "резерв", host=host, pair_if=st.get("pair_if") or "awgpair",
                                 pair_port=int(st.get("port") or 0), peer_port=int(peer_port or 0),
                                 pair_cidr=str(st.get("cidr") or ""), listener=listener,
                                 bot_username=bot_username, peer_admin_id=int(peer_admin_id))
        # пара подтверждается первой сессией на новых ключах (confirmed); не дождались —
        # такт снимает её (reserve_pair_confirm_tick)
        self.rsv_put("rsv_peer_state", {**peer, "at": timeutil.now_iso(), "linked_at": timeutil.now_iso(), "confirmed": False})
        self.rsv_put("rsv_peer_admin", {"admin_id": int(peer_admin_id), "opened": False, "bound_at": timeutil.now_iso()})
        self.reserve_keys_init()
        self.reserve_set_out("home")
        self.reserve_set_in("idle")
        self.rsv_update("rsv_peer_state", linked_msg=list(st.get("msg") or []))   # живое сообщение приглашения — итогу
        self.rsv_put("rsv_invite", None)
        _live_set(None)
        self.reserve_pkg_mark()                          # первый пакет — в ближайшие секунды
        try:
            self._gw_firewall_refresh()
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: файервол после сопряжения не перерисован: %s", e)
        self.backup_event_mark("reserve") if hasattr(self, "backup_event_mark") else None
        log.info("резерв: пара с «%s» (%s) записана", peer_name, host)
