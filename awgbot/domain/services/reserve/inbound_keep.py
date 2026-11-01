"""
inbound_keep.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — запись DNS уже сменил процесс открытия, а приём сорвался.

Правило одно (`_take_fail` → `_take_keep`): после того как процесс открытия сменил
запись на этот сервер (аварийный путь — по точкам кворума или по коду: шаг
`record_set`, `record_fact`, `done` без `record=skipped` или итог `fail` с полем
`record=set`), срыв зону не снимает никогда: пользователи уже идут сюда, а обратный
переезд к мёртвой цели смысла не имеет. Факт смены бот помнит в `rsv_in.rec_moved`
(open.status живёт в /run, итог `fail` затирает шаг). На пути с K основы запись
меняет основа — там срыв прежний. Дальше — по состоянию зоны:

- клоны стоят (фаза зоны taking | serving, интерфейс есть) — serving без
  `tg_taken`, не сделанное доделывает такт serving: факт записи не подтвердился —
  проверка у авторитетного сервера раз в KEEP_RETRY_S ± 40 % (по факту — отметка
  `tg_take`), режим serve зоны не встал — его повтор, бот не взял Telegram —
  inbound_tgwait;
- клонов нет — перезапуск процесса открытия тем же входом (точки ключа кворума
  или код держит только память бота, в базу он не пишется) не чаще раза в
  KEEP_RETRY_S ± 40 %, зона не снимается. Перед каждым перезапуском — основа: жива
  (её живой `st` после срыва, `_peer_alive`) — приём кончается прежним срывом и
  `down{rec}`, запись основа вернёт себе сама (outbound_keep); не жива — запись
  не трогается. Входа нет (бот перезапущен) — прежний срыв без `rec`: зона
  свободна, кворум откроет снова по свежим точкам, запись не трогается.

Админу резерва — громкое уведомление на каждый новый вид сбоя и строка в карточке;
основе — поле `st.in_keep{k, why, at}` (на связи — её админу уведомление,
outbound_keep). Снимают зону только `back` основы и `awg-bot reserve teardown`.
"""
from __future__ import annotations

import random
import time

from awgbot.core import config
from awgbot.util import livestep, timeutil

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import _LOCK

KEEP_RETRY_S = 60                 # повтор не доделанного после смены записи (± 40 %)
KEEP_KINDS = ("record", "serve", "relaunch", "tg")
# поля эпизода — сбрасываются в начале нового приёма и при срыве
KEEP_RESET = {"rec_moved": False, "rec_name": "", "rec_ip": "", "rec_fact": False, "keep_k": "", "keep_why": "",
              "keep_at": "", "keep_at_ts": 0, "keep_told": "", "keep_mono": 0, "serve_due": False, "relaunch": False,
              "relaunch_n": 0}


class ReserveInboundKeepMixin:

    @staticmethod
    def _in_by_k(inn: dict) -> bool:
        """Открытие по K основы (кнопка и просьба кворума dead | rf): запись меняет основа."""
        return str(inn.get("by") or ("k" if str(inn.get("kind") or "") == "button" else "")) == "k"

    def _rec_observe(self, inn: dict, st: dict) -> dict:
        """Запомнить в `rsv_in`, что процесс открытия сменил запись (и её имя, адрес, факт),
        пока open.status это ещё говорит."""
        if self._in_by_k(inn):
            return inn
        step, rec = str(st.get("step") or ""), str(st.get("record") or "")
        if not (step in ("record_set", "record_fact") or (step == "done" and rec != "skipped") or rec == "set"):
            return inn
        upd: dict = {}
        if not inn.get("rec_moved"):
            upd["rec_moved"] = True
            log.info("резерв: процесс открытия сменил запись DNS на этот сервер — зона больше не снимается срывом")
        for k, v in (("rec_name", str(st.get("name") or "")), ("rec_ip", str(st.get("ip") or ""))):
            if v and v != inn.get(k):
                upd[k] = v
        if step in ("record_fact", "done") and not inn.get("rec_fact"):
            upd["rec_fact"] = True
        return self.rsv_update("rsv_in", **upd) if upd else inn

    def _rec_moved(self, inn: dict) -> bool:
        from awgbot.infra import rsvzone
        if self._in_by_k(inn):
            return False
        return bool(inn.get("rec_moved") or self._rec_observe(inn, rsvzone.open_status()).get("rec_moved"))

    @staticmethod
    def _clones_up() -> bool:
        from awgbot.infra import rsvzone
        zst = rsvzone.state()
        return str(zst.get("phase") or "") in ("taking", "serving") and bool(zst.get("iface") or zst.get("ifaces"))

    def _open_secret_drop(self) -> None:
        self.__dict__.pop("_rsvopen_payload", None)

    # ── срыв после смены записи ─────────────────────────────────────────────
    def _take_keep(self, reason: str) -> list[tuple]:
        """Вместо срыва (под отметкой `_take_fail`): зона остаётся. Клоны стоят — serving
        и доделать; нет — перезапуск открытия по сроку (`_tick_relaunch`)."""
        try:
            self._rsvopen_stop()                              # завис (срок) — дальше доделываю я
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        inn = self._inn()
        name = self.reserve_peer_name()
        mark = {"keep_why": str(reason)[:200], "keep_at": timeutil.now_iso(), "keep_at_ts": time.time(),
                "keep_mono": time.monotonic()}
        if not self._clones_up():
            with _LOCK:
                self.rsv_update("rsv_in", relaunch=True, keep_k="relaunch", step="", served=False, done_mono=0, **mark)
            log.warning("резерв: запись DNS пользователей «%s» уже здесь, а клона нет: %s — перезапускаю открытие, "
                        "зону не снимаю", name, reason)
            return [("fw",)] + self._keep_told("relaunch", reason)
        fact, served = bool(inn.get("rec_fact")), bool(inn.get("served"))
        if fact and served:
            return self._take_keep_vpn(reason, mark=mark)     # не доделано только взятие Telegram
        kind = "serve" if fact else "record"
        with _LOCK:
            self.reserve_set_in("serving", kind=str(inn.get("kind") or ""), gen=int(inn.get("gen") or 0),
                                tg_taken=False, served=served, serve_due=not served, tg_wait="", keep_k=kind,
                                taken_at=timeutil.now_iso(), step="", relaunch=False, **mark)
        self._open_secret_drop()
        log.warning("резерв: пользователи «%s» у меня (запись уже здесь, клон стоит), а приём сорвался: %s — зона "
                    "остаётся, %s", name, reason, "жду факта записи" if kind == "record" else "повторяю режим serve")
        return [("fw",)] + self._keep_told(kind, reason)

    def _keep_told(self, kind: str, reason: str) -> list[tuple]:
        """Админу резерва — громко, один раз на вид сбоя в эпизоде."""
        from awgbot.domain.notices import reserve_keep as rk
        if str(self._inn().get("keep_told") or "") == kind:
            return []
        self.rsv_update("rsv_in", keep_told=kind)
        return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                         text=rk.kept(self.reserve_peer_name(), kind, reason))])]

    # ── такт: перезапуск открытия (клонов нет) ──────────────────────────────
    def _tick_relaunch(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        from .inbound import OPEN_WAIT_S
        now = time.monotonic()
        if not livestep.mono_due(inn.get("keep_mono"), now, KEEP_RETRY_S * random.uniform(0.6, 1.4)):
            return []
        self.rsv_update("rsv_in", keep_mono=now)
        reason, name = str(inn.get("keep_why") or "клон не поднят"), self.reserve_peer_name()
        if self._primary_back_ok(inn):
            # основа жива и сказала это после срыва — пользователи к ней: запись она вернёт себе сама
            log.warning("резерв: клона нет, а «%s» на связи — приём кончаю, запись вернёт она", name)
            return self._take_fail(f"{reason} — «{name}» на связи, пользователи возвращаются к ней", keep=False,
                                   rec_back=True)
        payload = self.__dict__.get("_rsvopen_payload")
        if not payload:
            return self._take_fail(f"{reason} — открыть снова нечем: вход процесса открытия не сохранился "
                                   "после перезапуска бота", keep=False)
        if hasattr(self, "reserve_zone_shell_ensure"):
            self.reserve_zone_shell_ensure()
        n = int(inn.get("relaunch_n") or 0) + 1
        with _LOCK:
            if str(self._inn().get("state") or "") != "taking":
                return []
            rsvzone.open_status_write("launching")           # прежний итог не должен сойти за ход нового процесса
            self._rsvopen_forget()                            # и код прежнего процесса — за его итог
            # up_sent — заново: на clone_up нового процесса такт перерисует файервол хоста под его клоны
            self.rsv_update("rsv_in", relaunch=False, relaunch_n=n, deadline=now + OPEN_WAIT_S, served=False,
                            done_mono=0, up_sent=False, step="open", step_at=timeutil.now_iso())
        try:
            self._rsvopen_launch(dict(payload))
        except ServiceError as e:
            self.rsv_update("rsv_in", relaunch=True, keep_mono=now)
            log.warning("резерв: процесс открытия не перезапущен: %s — повтор через минуту", e)
            return []
        log.warning("резерв: процесс открытия перезапущен (%d-й раз): клона нет — %s", n, reason)
        return [("fw",)]

    def _primary_back_ok(self, inn: dict) -> bool:
        """Основа жива: живой `st` (`_peer_alive`), полученный после срыва."""
        peer = self.reserve_peer_state()
        if not self._peer_alive(peer):
            return False
        try:
            return float(peer.get("at_ts") or 0) > float(inn.get("keep_at_ts") or 0) > 0
        except (TypeError, ValueError):
            return False

    # ── такт serving: не доделанное после смены записи ──────────────────────
    def _tick_keep(self, inn: dict) -> list[tuple]:
        if str(inn.get("keep_k") or "") == "record":
            return self._tick_rec_wait(inn)
        if inn.get("serve_due"):
            return self._tick_serve_retry(inn)
        return [] if inn.get("tg_taken") else self._tick_tg_wait(inn)

    def _keep_due(self, inn: dict) -> bool:
        now = time.monotonic()
        if not livestep.mono_due(inn.get("keep_mono"), now, KEEP_RETRY_S * random.uniform(0.6, 1.4)):
            return False
        self.rsv_update("rsv_in", keep_mono=now)
        return True

    def _tick_rec_wait(self, inn: dict) -> list[tuple]:
        """Факт записи у авторитетного сервера (как процесс открытия): есть — отметка `tg_take`
        (бот основы ставит вебхук), дальше serve и Telegram."""
        from awgbot.infra import rsvzone
        from awgbot.util import dnsapi
        if not self._keep_due(inn):
            return []
        name, ip = str(inn.get("rec_name") or ""), str(inn.get("rec_ip") or "")
        if name and ip:
            try:
                if ip not in dnsapi.read_authoritative(name):
                    return []
            except dnsapi.DnsError as e:
                log.info("резерв: факт записи %s не прочитан: %s", name, e)
                return []
        else:
            log.warning("резерв: имя или адрес сменённой записи не известны — факт не проверить, отметка tg_take без него")
        try:
            rsvzone.write_private(rsvzone.TG_TAKE_FILE, "1")
        except OSError as e:
            log.warning("резерв: отметка tg_take для бота основы не записана: %s", e)
            return []
        self.rsv_update("rsv_in", rec_fact=True, keep_k="serve" if inn.get("serve_due") else "", keep_mono=0)
        log.info("резерв: факт записи %s → %s есть — бот основы ставит вебхук", name or "?", ip or "?")
        return self._tick_keep(self._inn())

    def _tick_serve_retry(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        if not self._keep_due(inn):
            return []
        try:
            rsvzone.serve(self._webhook_port())
        except rsvzone.ZoneError as e:
            log.warning("резерв: зона снова не приняла режим serve: %s — повтор через минуту", e)
            return []
        self.rsv_update("rsv_in", serve_due=False, served=True, keep_k="", keep_mono=0)
        log.info("резерв: зона приняла режим serve — вход вебхука есть")
        if hasattr(self, "reserve_tg_pool_refresh_soon"):
            self.reserve_tg_pool_refresh_soon(5)
        return [("fw",)]

    # ── вид для карточки и `st` ──────────────────────────────────────────────
    def reserve_in_keep(self) -> dict:
        """Сбой после смены записи, ещё не доделанный: {k, why, at}; иначе пусто."""
        inn = self._inn()
        k = str(inn.get("keep_k") or "")
        if k not in KEEP_KINDS or str(inn.get("state") or "") not in ("taking", "serving"):
            return {}
        if inn.get("tg_taken") and not inn.get("serve_due"):
            return {}
        return {"k": k, "why": str(inn.get("keep_why") or "")[:120], "at": str(inn.get("keep_at") or "")}
