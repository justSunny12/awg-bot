"""
epoch.py — часть пакета journal (см. __init__): эпоха линии данных.

Держатель эпохи пишет решения; основа, чьи пользователи и бот у резерва, —
следует: база отказывает ей в записи решений (временные триггеры, util/rsvjrn),
задания решений в планировщике выключены. Отдельного голосования нет: роль
выводится из состояния автомата «мои пользователи» (`rsv_out`) и права
отвечать в Telegram (`rsv_poll`) — одним правилом (`_follow_wanted`), которое
сверяется при каждой смене состояния и на каждом такте, ветки по порядку:

  следование  leaving — бот передан (шаг released: реплика прежнего пути или
              `released{epoch, last}` журнала — handover); away — пользователи и
              бот у резерва, его гость не остановлен и это не «вернуть без
              резерва» (await_merge); returning — слияние (merge, пишет сырым
              соединением) и просьба реплики (back_req) при живом госте, кроме
              возврата после «вернуть без резерва»; home — живой резерв говорит
              `serving` строго после моего перехода домой (переезд мимо меня,
              `_peer_serving_after_home` — то же правило у автомата) при любом
              поллинге; иначе слово Telegram, сказанное уже дома, «вебхук у
              резерва» — пока резерв не на связи, его `st` старый или он несёт моих
  держатель   всё остальное: дома с поллингом и без serving, вебхук у резерва без
              гостя (живой `st` после перехода домой: idle), реплика возврата пришла
              (гость остановлен), отказ слияния, «⚠️ Да, вернуть», срыв переезда,
              пары нет; ограждение кворума (jrn_fence, 4.2.0) такт не снимает

На журнальном пути (reserve_jrn_moved) реплики и слияния нет: уход в следование
берёт эпоху и точку гостя из `st.jrn.keep`, возврат — по `released{epoch, last}`.
Факт Telegram сам по себе роль не меняет: вебхук, оставшийся у резерва без
гостя (после возврата, срыва переезда, отказа слияния), — остаток, а не держатель.
"""
from __future__ import annotations

from awgbot.util.rsvjrn import schema_too_old

from ...gateway_link.common import log
from ..state import HOSTING
from .ship import epoch_sane


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _earlier(a: dict, b: dict) -> bool:
    """Отметка a поставлена не позже b: по at_ts (доли секунды), без него — по since."""
    if a.get("at_ts") is not None and b.get("at_ts") is not None:
        return float(a["at_ts"]) <= float(b["at_ts"])
    return str(a.get("since") or "") <= str(b.get("since") or "")


class ReserveEpochMixin:

    def reserve_following(self) -> bool:
        """Основа следует за ботом на резерве: решения здесь не пишутся."""
        try:
            return bool(self.db.jrn_following())
        except Exception as e:                                # noqa: BLE001
            if schema_too_old(e):                             # база старше эпохи (копия, CLI) — держатель
                return False
            log.warning("резерв: роль не прочитана (%s) — на этот такт считаю следование", e)
            return True                                       # безопаснее не писать, чем писать не держателем

    def _follow_wanted(self) -> tuple[bool, str]:
        """(следовать ли, почему) — по состоянию пары, переезда и права отвечать."""
        if not self.reserve_paired():
            return False, "пары нет"
        out = self.rsv_get("rsv_out")
        state, step = str(out.get("state") or "home"), str(out.get("step") or "")
        if state == "leaving":
            return step == "released", ("передача бота" if step == "released" else "переезд ещё не передал бот")
        if state == "away":
            if out.get("guest_stopped"):
                return False, "бот на резерве остановлен"
            if out.get("await_merge"):
                return False, "вернуть без резерва"
            return True, "пользователи и бот у резерва"
        if state == "returning":
            # после «вернуть без резерва» (return_why=force) основа давно решает сама: просьба реплики
            # у вернувшегося резерва право записи не отдаёт
            forced = str(out.get("return_why") or "") == "force"
            if step == "merge" or (step == "back_req" and not out.get("guest_stopped") and not forced):
                return True, "возврат: ждём реплику и слияние"
            return False, "возврат: бот на резерве остановлен"
        # home: резерв несёт моих (переезд мимо меня; такт home переведёт и автомат в away) —
        # одним правилом с автоматом (`_peer_serving_after_home`); иначе решает слово Telegram,
        # сказанное уже дома (прежнее «away» — до возврата)
        peer = self.reserve_peer_state()
        serving = self._peer_serving_after_home(out, peer)
        if self._quorum_rules_home():
            # кворум (4.2.0): роль дома — только по факту: эпоха больше моей (проба, qstate) и
            # лишение кворума (jrn_fence); слово Telegram — только остановка поллинга
            return (True, "резерв несёт моих пользователей") if serving else (False, "кворум: решает большинство")
        poll = self.rsv_get("rsv_poll")
        if self.reserve_poll() != "away" or _earlier(poll, out):
            return (True, "резерв несёт моих пользователей") if serving else (False, "пользователи и Telegram здесь")
        fresh = str(peer.get("at") or "") >= str(out.get("since") or "")
        if self._peer_alive(peer) and fresh and str(peer.get("inn") or "idle") not in HOSTING:
            return False, "вебхук у резерва без гостя"
        return True, "Telegram: вебхук у резерва"

    def _quorum_rules_home(self) -> bool:
        """Линия с включённым кворумом и решениями по нему (quorum/members, решение 9)."""
        try:
            return bool(self.reserve_quorum_active() and self.reserve_quorum_act())
        except Exception:                                     # noqa: BLE001
            return False

    def _peer_alive(self, peer: dict) -> bool:
        """Резерв на связи живым словом: сессия есть, живость не `down`, и его `st` получен
        после последней потери связи. Один флаг TCP-сеанса — не то: сеанс мёртвого линка
        держится до ~23 мин (keepalive), живость падает через ~60 с, а `st`, полученный до
        потери связи (`st_stale`), после её восстановления старый, пока не дошёл новый."""
        return bool(self.reserve_online() and self.reserve_peer_seen() != "down" and not peer.get("st_stale"))

    def _peer_serving_after_home(self, out: dict, peer: dict) -> bool:
        """Резерв несёт моих пользователей — факт, сказанный живым `st` строго после моего
        перехода домой (переезд по взглядам, пока я лежал; `taking` — ещё не факт: клон может
        не встать); после «вернуть без резерва» — нет. Одно правило и для роли
        (`_follow_wanted`), и для автомата (`_tick_home` → away)."""
        if out.get("await_merge") or str(out.get("return_why") or "") == "force":
            return False
        if not self._peer_alive(peer) or str(peer.get("inn") or "idle") != "serving":
            return False
        if peer.get("at_ts") is not None and out.get("at_ts") is not None:
            return float(peer["at_ts"]) > float(out["at_ts"])
        return str(peer.get("at") or "") > str(out.get("since") or "")   # строго после перехода домой

    def reserve_epoch_sync(self) -> None:
        """Роль — по правилу; переход — передача эпохи (эпоха + 1, synchronous = FULL).
        Бот основы на резерве эпоху берёт и отдаёт по журналу (guestj), не по паре."""
        if getattr(self, "guest", False):
            return
        want, why = self._follow_wanted()
        if want == self.reserve_following():
            return
        if want:
            # эпоха и точка расхождения бота на резерве, если он открылся сам (авария), — его, а не
            # моя + 1 и мой последний номер; не знаю — подтверждённое резервом (дальше — хвост)
            keep = self.reserve_peer_state().get("jrn_keep") or {}
            guest_e, fork = _int(keep.get("guest_e")), _int(keep.get("guest_fork"))
            meta_now = self.db.jrn_meta()
            if guest_e and not epoch_sane(guest_e, int(meta_now["epoch"])):
                guest_e = 0
            last_i = self.db.jrn_last_i()
            if fork and fork > max(last_i, int(meta_now["acked"])) + 1:
                # у гостя есть мои события, которых здесь уже нет (пропажа питания после отправки):
                # применяю с того, что база правда имеет, недостающее попрошу — резерв отдаст из хранилища
                log.warning("резерв: точка гостя %s дальше моих номеров (здесь по %s) — база потеряла хвост, "
                            "применяю с %s", fork, last_i, last_i)
                fork = last_i
            if fork and meta_now["holder"] == "peer" and fork < int(meta_now["applied"]):
                fork = int(meta_now["applied"])               # уже применял его события дальше (мигание роли по старому st)
            elif fork and fork < int(meta_now["acked"]):
                if int(keep.get("gap") or 0):
                    # гость открылся с разрывом журнала (резерв сказал): его точка раньше моего подтверждённого —
                    # мои решения между ними он не видел, его — поверх; админу об этом строкой в Т-РУ5б
                    self.rsv_update("rsv_out", jrn_lost_mine=[fork + 1, int(meta_now["acked"])])
                    log.warning("резерв: гость открылся с разрывом — точка %s раньше подтверждённого %s, мои решения "
                                "между ними — под его", fork, meta_now["acked"])
                else:
                    log.warning("резерв: точка гостя %s раньше подтверждённого %s — беру подтверждённое",
                                fork, meta_now["acked"])
                    fork = 0
            meta = self.db.jrn_pass(holder="peer", role="follower", why=why, epoch=guest_e or None,
                                    fork_i=fork or int(meta_now["acked"]))
            if meta is not None:
                self.rsv_update("rsv_out", jrn_report=None)   # сводка эпохи для Т-РУ5б — с начала следования
                self.db.jrn_seen_reset(f"{self.db.jrn_line()}:b")   # новый гость — счёт его пачек заново
                if fork:                                      # точка гостя известна — хвост готовить сразу
                    self.reserve_tail_finalize(fork)
                log.warning("резерв: эпоха %s у «%s» — здесь следование (%s)", meta["epoch"],
                            self.reserve_peer_name(), why)
        else:
            meta_now = self.db.jrn_meta()
            if meta_now["holder"] == "self" and meta_now["role"] == "follower":
                return                                        # ограждение кворума (4.2.0, jrn_fence): снимает только jrn_unfence
            if self.reserve_out() != "home":
                self.__dict__.pop("_quo_adopted", None)
            elif meta_now["holder"] == "peer" and self.__dict__.get("_quo_adopted") and self._quorum_rules_home():
                return                                        # эпоха принята по кворуму (jrn_adopt): вернёт правило лидера
            applied = int(meta_now["applied"])
            prev_e = int(meta_now["epoch"]) if meta_now["holder"] == "peer" else 0
            meta = self.db.jrn_pass(holder="self", role="holder", why=why, fork_i=applied)   # его события — по applied
            self.db.jrn_tail_drop_held()                      # отложенный хвост: точки уже не будет, правки в базе
            if meta is not None:
                log.warning("резерв: эпоха %s здесь (%s, применено по %s)", meta["epoch"], why, applied)
                self.reserve_follow_clear()
                if prev_e and not self.rsv_get("rsv_c9"):
                    # эпоху взял у бота на резерве без его released (срыв, зависший вебхук, «вернуть без
                    # резерва»): его правки после applied — хвостом при встрече, одним путём (rsv_c9)
                    self.reserve_c9_start(prev_e)

    def reserve_epoch_check(self) -> None:
        """Такт: страховка — роль снова сверяется с состоянием."""
        self.reserve_epoch_sync()
