"""
inbound_back.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — их возврат основе и пересылка хвоста (вынесено из inbound).

Ожидание реплики гостя после `stop` (`_tick_releasing`: молчит — `rep.bin` с
диска или `released` без реплики). После `released` без `back`: основа сказала `away`
(слияние у неё сорвалось) или прислала `resume` — гость снова у меня
(`reserve_resume`: юнит гостя, serving), иначе бот нем с обеих сторон.
`back` основы: пересылка хвоста (`--relay`, файервол с DNAT, `relay_flush`;
адрес основы — IP, имя резолвится сейчас; срыв открытия до подъёма клона — без
хвоста, сразу idle), учёт, `relay{until}`, `--wipe`, прежний пакет — прочь,
`wiped`, Т-РУ5 → relay до `relay_max_days` или тишины
`relay_idle_hours` (по счётчику правила DNAT — nftguard.relay_packets) → idle.
Новое открытие при relay — пересылка уступает (`_relay_yield`); открытие
сорвалось — возвращается (`_relay_restore`).
"""
from __future__ import annotations

import subprocess
import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .outbound import RELEASED_WAIT_S
from .state import _LOCK

RESUME_AFTER_S = RELEASED_WAIT_S + 60   # released отдан, back нет, основа в away — слияние у неё сорвалось
RESUME_RETRY_S = 60                     # юнит гостя не поднялся — повтор не чаще


class ReserveInboundBackMixin:

    def _relay_yield(self) -> bool:
        """Фаза relay (пересылка хвоста) при новом открытии: правило DNAT снимается —
        иначе оно увело бы пользователей нового клона на основу; состояние — idle.
        Что снято, помнится (`relay_prev`): открытие сорвётся — пересылка вернётся
        (`_relay_restore`); без пересылки отметка пустая — от прошлого открытия не тянется.
        Записи conntrack с DNAT хвоста живут и после снятия правила — рукопожатия устройств
        по тем же 5-кортежам ушли бы на основу до их истечения: отметка `relay_flush_due`,
        и такт на `clone_up` (клон поднят, порты клонов в состоянии зоны, файервол без DNAT
        перерисован тем же тактом) сбрасывает их (`relay_flush`). True — пересылка была и снята."""
        from awgbot.infra import rsvzone
        inn = self._inn()
        if self.reserve_in() != "relay":
            if inn.get("relay_prev") or inn.get("relay_flush_due"):
                self.rsv_update("rsv_in", relay_prev=None, relay_flush_due=False)
            return False
        prev = {k: str(inn.get(k) or "") for k in ("relay_to", "relay_until", "relay_at", "relay_last_traffic")}
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста при новом открытии снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="", relay_prev=prev if prev["relay_to"] else None,
                            relay_flush_due=bool(prev["relay_to"]))
        log.info("резерв: пересылка хвоста снята — зона нужна новому переезду")
        return bool(prev["relay_to"])

    def _relay_restore(self) -> bool:
        """Открытие сорвалось (отказ запуска, `_take_fail`), а пересылка хвоста, уступившая ему,
        была: вернуть её — устройства, сидящие на моём адресе с прошлого возврата, снова идут
        на основу, а не теряют VPN. Срок и тишину решит такт relay, как прежде; он же первым
        делом перерисует файервол с DNAT хвоста и сбросит записи conntrack (`relay_redraw`)."""
        from awgbot.infra import rsvzone
        prev = dict(self._inn().get("relay_prev") or {})
        if not prev:
            return False
        self.rsv_update("rsv_in", relay_prev=None, relay_flush_due=False)   # сброс сделает такт relay (relay_redraw)
        to = str(prev.get("relay_to") or "")
        if not to or self.reserve_in() != "idle":
            return False
        try:
            rsvzone.relay(to)
        except rsvzone.ZoneError as e:
            log.warning("резерв: пересылка хвоста после срыва открытия не возвращена: %s", e)
            return False
        self.reserve_set_in("relay", step="", relay_to=to, relay_until=str(prev.get("relay_until") or ""),
                            relay_at=str(prev.get("relay_at") or timeutil.now_iso()),
                            relay_last_traffic=str(prev.get("relay_last_traffic") or ""), relay_pkts=None,
                            relay_redraw=True)
        log.info("резерв: открытие не состоялось — пересылка хвоста на %s снова идёт", to)
        return True

    @staticmethod
    def _relay_host_ip(host: str) -> str:
        """Адрес основы для DNAT хвоста: IPv4-литерал как есть; имя — резолвится
        сейчас (первый A); не резолвится — пусто (хвоста не будет, зона снимается)."""
        import ipaddress
        import socket
        if not host:
            return ""
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("резерв: адрес основы %s для пересылки хвоста не резолвится: %s", host, e)
            return ""
        return str(infos[0][4][0]) if infos else ""

    def _tick_releasing(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        if inn.get("released_at"):
            return self._tick_released_wait(inn)
        if livestep.mono_expired(inn.get("deadline"), time.monotonic()):
            # гость не ответил — отдать последний снимок с диска
            try:
                blob = rsvzone.REP_PATH.read_bytes()
            except OSError:
                blob = b""
            if blob:
                return self.reserve_guest_replica_in(blob)
            log.warning("резерв: реплики нет ни от гостя, ни на диске — released без реплики")
            self.rsv_update("rsv_in", released_at=timeutil.now_iso())
            return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": ""})]
        return []

    def _tick_released_wait(self, inn: dict) -> list[tuple]:
        """Реплика отдана, `back` нет. Живой `st` основы после released говорит `away` —
        слияние у неё сорвалось (её поллинг стоит, гость здесь остановлен: бот нем с обеих
        сторон) — гость снова у меня. `home`/`returning` — потерянный `back`: его повторяет
        сама основа (`_home_back_resend`), здесь ждать. Срока releasing нет: истечение увело бы
        зону в idle при основе в away — пользователей не нёс бы никто."""
        if inn.get("jrn_back"):
            return []                                         # с журналом слияния нет — срыва такого вида нет
        try:
            if timeutil.elapsed(timeutil.parse_iso(str(inn.get("released_at") or ""))) < RESUME_AFTER_S:
                return []
        except ValueError:
            return []
        peer = self.reserve_peer_state()
        if str(peer.get("out") or "") != "away" or not self._peer_alive(peer):
            return []
        if str(peer.get("at") or "") <= str(inn.get("released_at") or ""):
            return []                                         # st до released — о прежнем away, не о сорванном возврате
        return self.reserve_resume("основа в away после реплики")

    def reserve_resume_in(self) -> list[tuple]:
        """`resume` основы (ret1): слияние у неё сорвалось — гость снова у меня, не дожидаясь срока."""
        return self.reserve_resume("слово основы")

    def reserve_resume(self, why: str) -> list[tuple]:
        """Возврат не состоялся: гость, остановленный на `back_req`, — снова (его данные в
        хранилище зоны целы: `wipe` — только в `reserve_back_in`), состояние serving, админу
        резерва — уведомление. Режим Telegram — прежний: до возврата гость Telegram не держал
        (VPN здесь без бота, `tg_wait`) — и после serving без `tg_taken`, захват повторяется
        (inbound_tgwait), а не «бот у меня», которого нет. Вне releasing после released —
        нечего возобновлять; юнит не поднялся — повтор не чаще RESUME_RETRY_S."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        now = time.monotonic()
        with _LOCK:
            inn = self._inn()
            if str(inn.get("state") or "") != "releasing" or not inn.get("released_at") or inn.get("jrn_back"):
                return []
            if not livestep.mono_due(inn.get("resume_mono"), now, RESUME_RETRY_S):
                return []
            self.rsv_update("rsv_in", resume_mono=now)
        name = self.reserve_peer_name()
        try:
            proc = subprocess.run(["systemctl", "start", rsvzone.GUEST_UNIT], capture_output=True, timeout=60)
            if proc.returncode != 0:
                raise subprocess.SubprocessError(f"код {proc.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("резерв: возврат «%s» не состоялся (%s), а гость не поднялся: %s", name, why, e)
            return []
        with _LOCK:
            inn = self._inn()
            if str(inn.get("state") or "") != "releasing":
                return []
            had_tg = bool(inn.get("tg_taken"))               # releasing хранит режим serving до возврата
            self.reserve_set_in("serving", kind=str(inn.get("kind") or "button"), gen=int(inn.get("gen") or 0),
                                tg_taken=had_tg, served=True, taken_at=str(inn.get("taken_at") or timeutil.now_iso()),
                                step="", released_at="", replica_at="", deadline=None, resume_mono=None,
                                **({} if had_tg else {"tg_retry_mono": now}))
        log.warning("резерв: возврат «%s» не состоялся (%s) — %s", name, why,
                    "бот основы снова у меня" if had_tg else "пользователи снова у меня, бот основы Telegram не держит")
        return [("fw",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.return_resumed(name, bot=had_tg))])]

    def reserve_back_in(self) -> list[tuple]:
        """`back` основы: пересылка хвоста, учёт, `relay{until}`, снятие, `wiped`, Т-РУ5. Бот основы
        у меня по принудительному захвату (`tg_take_forced`, вебхук у гостя) — `taken`, а не снятие."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        inn = self._inn()
        if str(inn.get("state") or "") == "relay":
            return [("send", "relay", {"until": str(inn.get("relay_until") or "")}), ("send", "wiped", {})]   # повтор
        if str(inn.get("state") or "") not in ("releasing", "serving", "taking"):
            # зоны нет — основе нечего ждать; журнал данных гостя и rep.bin под прежним K — прочь
            try:
                self.guest_jrn_store().wipe()
                rsvzone.REP_PATH.unlink(missing_ok=True)
            except OSError as e:
                log.info("резерв: остатки гостя на диске не сняты: %s", e)
            return [("send", "wiped", {})]
        if str(inn.get("state") or "") == "serving" and inn.get("tg_take_forced") and inn.get("tg_taken"):
            # основа не отдала бота и пропала — гость забрал Telegram сам и с тех пор правит её данные;
            # `back` ожившей основы по сроку шага переезда (её срыв, не узнавший об этом) снёс бы их:
            # ответ — «несу» (`taken`), зона и гость остаются; возврат — обычным путём (back_req, реплика)
            log.warning("резерв: back от «%s», а её бот у меня по принудительному захвату — отвечаю taken, гостя не снимаю",
                        self.reserve_peer_name())
            return [("send", "taken", self._word_oid(inn))]
        name = self.reserve_peer_name()
        # пересылка хвоста нужна, только если пользователи могли прийти на клон: зона обслуживала
        # или клон уже поднят — запись по кнопке меняет основа сразу после `up`, и пользователи
        # могли прийти раньше, чем процесс открытия дойдёт до record_set; срыв раньше клона — сразу
        # idle (лишний хвост безвреден: кончится по тишине relay_idle_hours)
        step = str(rsvzone.open_status().get("step") or "")
        had_clients = str(inn.get("state") or "") != "taking" or step in ("clone_up", "record_set", "record_fact", "done")
        if str(inn.get("state") or "") == "taking":
            try:
                self._rsvopen_stop()                          # переезд сорвался на открытии — юнит прочь
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        peer = self.reserve_peer_state()
        host = str(peer.get("self_host") or (self.reserve_peer() or {}).get("host") or "")
        host = self._relay_host_ip(host)                      # в правило nft — адрес: имя резолвится при загрузке таблицы
        ports = [int(p) for p in (peer.get("ports") or []) if isinstance(p, int)]
        to = f"{host}:{ports[0]}" if host and ports and had_clients else ""
        until = timeutil.to_iso(timeutil.shift(timeutil.now(), settings.get_int("app.reserve.relay_max_days", 7) * 86400))
        try:
            self.reserve_traffic_in_close()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: учёт при возврате: %s", e)
        try:
            if to:
                rsvzone.relay(to)
            else:
                rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.warning("резерв: зона при возврате: %s", e)
        actions: list[tuple] = [("fw",)] + ([("relay_flush",)] if to else []) + [("send", "relay", {"until": until})]
        try:
            rsvzone.wipe()
        except rsvzone.ZoneError as e:
            log.warning("резерв: хранилище гостя не снято: %s", e)
        self.reserve_pkg_in_drop()
        with _LOCK:
            self.reserve_set_in("relay", kind=str(inn.get("kind") or ""), relay_to=to, relay_until=until,
                                relay_at=timeutil.now_iso(), step="")
        actions.append(("send", "wiped", {}))
        tr = self._reserve_traffic_view().get("in") or ""
        actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_ended(name, tr))]))
        log.info("резерв: пользователи и бот «%s» вернулись к нему; пересылка хвоста до %s", name, until)
        return actions

    def _tick_relay(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        if inn.get("relay_redraw"):
            # пересылка вернулась после срыва открытия: DNAT хвоста — в таблицу, потом записи conntrack прочь
            self.rsv_update("rsv_in", relay_redraw=False)
            return [("fw",), ("relay_flush",)]
        until = str(inn.get("relay_until") or "")
        done = False
        try:
            done = bool(until) and timeutil.parse_iso(until) <= timeutil.now()
        except ValueError:
            done = True
        idle_h = settings.get_int("app.reserve.relay_idle_hours", 2)
        if not done and idle_h:
            last = self._relay_last_traffic(inn)
            try:
                done = bool(last) and timeutil.elapsed(timeutil.parse_iso(last)) >= idle_h * 3600
            except ValueError:
                done = True
        if not done:
            return []
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="")
        return [("fw",), ("send", "st", self.reserve_st_body())]

    def _relay_last_traffic(self, inn: dict) -> str:
        """Момент последнего пакета через правило пересылки (по счётчику правила DNAT
        в таблице хоста): счётчик вырос с прошлого такта — «сейчас»; счётчика нет —
        начало пересылки. Тишина считается от него."""
        from awgbot.infra import nftguard
        last = str(inn.get("relay_last_traffic") or inn.get("relay_at") or "")
        try:
            pkts = nftguard.relay_packets()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: счётчик пересылки хвоста не прочитан: %s", e)
            pkts = None
        if pkts is None:
            return last
        seen = inn.get("relay_pkts")
        if seen is not None and int(pkts) > int(seen):       # меньше — перерисовка таблицы обнулила счётчик
            last = timeutil.now_iso()
        if seen is None or int(pkts) != int(seen) or not inn.get("relay_last_traffic"):
            self.rsv_update("rsv_in", relay_pkts=int(pkts), relay_last_traffic=last)
        return last
