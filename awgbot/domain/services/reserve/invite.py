"""
invite.py — часть пакета reserve (см. __init__): приглашение в пару
(пригласивший) и его отмена.

«📤 Пригласить»: линк пары поднимается на ключах приглашения
(`reserve-pair-setup.sh --stage`: ключи обеих сторон, обфускация, штамп
«пара <id>»; файервол с линка пары пускает только канал пары — и до кода, и
после), запись `rsv_invite` в
`server_state`, строка `AWGP1.…` — админу в живое сообщение. Срок — час;
вышел, отменили, третий неверный код — линк снимается (`--rollback`). Новое
приглашение убивает прежнее. Закрытый ключ стороны принявшего в базу не
пишется: он живёт только в приглашении (и в памяти до его сборки).
"""
from __future__ import annotations

import os
import secrets
import subprocess
import threading

from awgbot.core import config, settings
from awgbot.domain.services.selfhost import own_host
from awgbot.domain.services.types import ServiceError, ServiceErrorHtml
from awgbot.util import pairtoken, timeutil

from ..gateway_link.common import log
from .state import _LOCK

INVITE_TTL = 3600
MAX_SESSIONS = 10
MAX_ATTEMPTS = 3
OPEN = frozenset({"waiting", "connected"})
PAIRING = OPEN | {"accepted"}
LIVE = PAIRING | {"closing"}
DEFAULT_PAIR_PORT = 47101

# {"id", "pair_id", "code", "kp", "eph", "addr", "name"} — только в памяти процесса
_LIVE: dict = {}
_LIVE_LOCK = threading.RLock()


def _live() -> dict:
    with _LIVE_LOCK:
        return dict(_LIVE)


def _live_set(data: dict | None) -> None:
    with _LIVE_LOCK:
        _LIVE.clear()
        if data:
            _LIVE.update(data)


class ReserveInviteMixin:

    # ── скрипт линка пары ───────────────────────────────────────────────────

    @staticmethod
    def _pair_script() -> str:
        return str(config.BASE_DIR / "install" / "reserve-pair-setup.sh")

    def _run_pair_script(self, mode: str, env: dict | None = None) -> dict:
        """Запустить скрипт линка пары; строки `key=value` из stdout — словарём.
        Отказ — ServiceError с причиной из строки «ОШИБКА: …»."""
        try:
            proc = subprocess.run(["sh", self._pair_script(), mode], capture_output=True, timeout=120,
                                  env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired:
            raise ServiceError(f"скрипт линка пары ({mode}) не уложился в 120 с")
        out = proc.stdout.decode(errors="replace")
        if proc.returncode != 0:
            why = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip().startswith("ОШИБКА")), "")
            raise ServiceError(f"скрипт линка пары ({mode}) не отработал: "
                               + (why or proc.stderr.decode(errors="replace").strip())[-200:])
        kv = {}
        for ln in out.splitlines():
            if "=" in ln and not ln.startswith("#") and " " not in ln.split("=", 1)[0]:
                k, v = ln.split("=", 1)
                kv[k.strip()] = v.strip()
        return kv

    def _pair_env(self) -> dict:
        inv = self.rsv_get("rsv_invite")
        peer = self.reserve_peer() or {}
        return {"PAIR_IF": peer.get("pair_if") or inv.get("pair_if") or "awgpair",
                "PAIR_CIDR": peer.get("pair_cidr") or inv.get("cidr") or str(settings.get("app.reserve.pair_cidr", "10.99.98.0/30")),
                "PAIR_PORT": str(peer.get("pair_port") or inv.get("port") or DEFAULT_PAIR_PORT),
                "PAIR_ID": str(inv.get("id") or "")}

    # ── приглашение ─────────────────────────────────────────────────────────

    def reserve_invite(self) -> dict:
        return self.rsv_get("rsv_invite")

    def reserve_invite_live(self) -> bool:
        st = self.reserve_invite()
        return bool(st.get("id")) and st.get("state") in LIVE and not self._invite_expired(st)

    def reserve_pin_self_host(self) -> str:
        """Собственный адрес сервера (`network.self_host`) — IP, на который сейчас
        указывает запись для клиентов: линк пары и линки шлюзов смотрят на него,
        а имя записи при переезде уходит на резерв. Пусто — зафиксировать по
        резолву; имя не резолвится — отказ."""
        import ipaddress
        own = str(settings.get("app.network.self_host", "") or "").strip()
        if own:
            return own
        host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip()
        try:
            ipaddress.ip_address(host)
            return host                                       # адрес для клиентов — IP, он и есть свой
        except ValueError:
            pass
        from awgbot.util import dnsapi
        try:
            addrs = [a for a in dnsapi.read_system(host) if a and ":" not in a] if host else []
        except Exception:                                     # noqa: BLE001 — резолвер недоступен
            addrs = []
        if not addrs:
            log.warning("резерв: имя %s не резолвится — собственный адрес сервера не зафиксирован (network.self_host)", host)
            return ""
        settings.set_value("app.network.self_host", addrs[0])
        log.info("резерв: собственный адрес сервера зафиксирован как %s (запись %s)", addrs[0], host)
        return addrs[0]

    @staticmethod
    def reserve_require_zone_tools() -> None:
        """Гостевой зоне нужен dnsmasq (резолвер зоны) — без него резерв принять
        клиентов не сможет; план скрипта зоны говорит об этом заранее."""
        from awgbot.infra import rsvzone
        plan = rsvzone.plan()
        if str(plan.get("dnsmasq") or "yes") == "no":
            log.warning("резерв: на сервере нет dnsmasq — резолвер гостевой зоны не поднимется, принять клиентов не выйдет")

    @staticmethod
    def _invite_expired(st: dict) -> bool:
        try:
            return timeutil.parse_iso(str(st.get("expires") or "")) <= timeutil.now()
        except ValueError:
            return True

    def reserve_invite_issue(self) -> dict:
        """«📤 Пригласить»: линк на ключах приглашения, запись, строка приглашения.
        {"state", "token", "stale": сообщение прежнего приглашения или None}."""
        if self.reserve_paired():
            raise ServiceError("Резерв уже подключён — сначала удалить пару")
        host = own_host()
        if not host:
            raise ServiceErrorHtml("Не задан адрес сервера (<code>network.server_host</code> или "
                                   "<code>network.self_host</code>) — второму серверу некуда подключаться")
        self.reserve_pin_self_host()
        self.reserve_require_zone_tools()
        host = own_host()
        if not settings.get_bool("app.reserve.ready", False):
            raise ServiceError("Сначала включи «Готов быть резервом» — без готовности пара не нужна")
        if not self.reserve_has_gateway():
            log.info("резерв: приглашение без своего шлюза — направление «мои клиенты» будет не готово")
        with _LOCK:
            old = self.reserve_invite()
            stale = old.get("msg") if old.get("msg") else None
            if old.get("id"):
                self._invite_rollback("replaced")
            pid = secrets.token_hex(8)
            port = int(settings.get("app.reserve.pair_port", DEFAULT_PAIR_PORT) or DEFAULT_PAIR_PORT)
            cidr = str(settings.get("app.reserve.pair_cidr", "10.99.98.0/30") or "10.99.98.0/30")
            env = {"PAIR_IF": "awgpair", "PAIR_CIDR": cidr, "PAIR_PORT": str(port), "PAIR_ID": pid}
            try:
                kv = self._run_pair_script("--stage", env)
            except ServiceError:
                # конфиг со штампом мог лечь до отказа — снять, иначе линк-сирота
                try:
                    self._run_pair_script("--rollback", env)
                except ServiceError as e:
                    log.warning("резерв: линк после отказа --stage не снят: %s", e)
                raise
            try:
                import base64
                tok = pairtoken.PairToken(
                    version=str(config.INSTALLED_VERSION).lstrip("v"), pair_id=pid,
                    expires=int(timeutil.shift(timeutil.now(), INVITE_TTL).timestamp()), host=host,
                    pair_port=port, peer_addr=pairtoken_peer_addr(cidr),
                    inviter_pub=base64.b64decode(kv["PAIR_VPS_PUB"]), peer_priv=base64.b64decode(kv["PAIR_PEER_PRIV"]),
                    jc=int(kv["JC"]), jmin=int(kv["JMIN"]), jmax=int(kv["JMAX"]),
                    s=tuple(int(kv[f"S{i}"]) for i in range(1, 5)),
                    h=tuple(tuple(int(x) for x in kv[f"H{i}"].split("-", 1)) for i in range(1, 5)),
                    awg_gen=self._awg_generation())
                token = pairtoken.encode(tok)
            except (KeyError, ValueError, pairtoken.TokenError) as e:
                self._run_pair_script("--rollback", env)
                raise ServiceError(f"Приглашение не собралось: {e}")
            now = timeutil.now()
            st = {"id": pid, "pair_if": "awgpair", "port": port, "cidr": cidr, "host": host,
                  "vps_pub": kv["PAIR_VPS_PUB"], "peer_pub": kv["PAIR_PEER_PUB"],
                  # закрытый ключ стороны принявшего — секрет канала до кода (он же в приглашении);
                  # запись живёт час и снимается с сопряжением
                  "peer_priv": kv["PAIR_PEER_PRIV"], "token": token,
                  "issued": timeutil.to_iso(now), "expires": timeutil.to_iso(timeutil.shift(now, INVITE_TTL)),
                  "sessions": 0, "attempts": 0, "endpoints": [], "msg": [], "state": "waiting"}
            self.rsv_put("rsv_invite", st)
            _live_set(None)
        try:
            self._gw_firewall_refresh(strict=True)        # карантин линка пары — сразу
        except ServiceError as e:
            self._invite_rollback("firewall")
            raise ServiceError(f"Приглашение не выдано — {e}") from None
        log.info("резерв: приглашение выпущено (id %s…)", pid[:4])
        return {"state": st, "token": token, "stale": stale}

    @staticmethod
    def _awg_generation() -> int:
        from awgbot.infra import awglock
        try:
            return int(awglock.generation())
        except Exception:                                 # noqa: BLE001
            return 0

    def reserve_invite_set_message(self, chat_id: int, message_id: int) -> None:
        self.rsv_update("rsv_invite", msg=[int(chat_id), int(message_id)])

    def reserve_invite_cancel(self, why: str = "cancel") -> dict:
        """Отмена приглашения (кнопка, срок, третий неверный код): линк прочь."""
        with _LOCK:
            st = self.reserve_invite()
            if not st.get("id"):
                raise ServiceError("Приглашения нет")
            self._invite_rollback(why)
            return st

    def _invite_rollback(self, why: str) -> None:
        env = self._pair_env()
        try:
            self._run_pair_script("--rollback", env)
        except ServiceError as e:
            log.warning("резерв: линк приглашения не снят (%s): %s", why, e)
        self.rsv_put("rsv_invite", None)
        _live_set(None)
        try:
            self._gw_firewall_refresh()
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв: файервол после снятия приглашения не перерисован: %s", e)
        log.info("резерв: приглашение снято (%s)", why)

    def reserve_invite_tick(self) -> dict | None:
        """Такт: срок вышел — снять; вернуть запись для сообщения или None."""
        st = self.reserve_invite()
        if not st.get("id") or st.get("state") not in PAIRING:
            return None
        if self._invite_expired(st):
            self._invite_rollback("expired")
            st["event"] = "expired"
            return st
        return None

    def reserve_has_gateway(self) -> bool:
        """Есть хотя бы один шлюз с агентом новой линии (возможность `rsv1`)."""
        for gw in self.db.gateways():
            caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
            if "rsv1" in (caps or []):
                return True
        return False


def pairtoken_peer_addr(cidr: str) -> str:
    """Адрес принявшего в /30 линка пары — первый хост (пригласивший — второй)."""
    import ipaddress
    hosts = list(ipaddress.ip_network(cidr, strict=False).hosts())
    return str(hosts[0])
