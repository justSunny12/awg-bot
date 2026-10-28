"""
epoch.py — часть пакета journal (см. __init__): эпоха линии данных.

Держатель эпохи пишет решения; основа, чьи пользователи и бот у резерва, —
следует: база отказывает ей в записи решений (временные триггеры, util/rsvjrn),
задания решений в планировщике выключены. Отдельного голосования нет: роль
выводится из состояния автомата «мои пользователи» (`rsv_out`) и права
отвечать в Telegram (`rsv_poll`) — одним правилом (`_follow_wanted`), которое
сверяется при каждой смене состояния и на каждом такте:

  следование  финальная реплика собрана (шаг released передачи бота); пользователи
              и бот у резерва, а его гость не остановлен; возврат до реплики
              (back_req) и на время слияния (merge — пишет сырым соединением);
              дома, но Telegram отдал право резерву и резерв несёт пользователей
              (или молчит — тогда верим Telegram)
  держатель   всё остальное: дома с поллингом, реплика возврата пришла (гость
              остановлен), отказ слияния, «⚠️ Да, вернуть», срыв переезда, пары нет

Факт Telegram сам по себе роль не меняет: вебхук, оставшийся у резерва без
гостя (после возврата, срыва переезда, отказа слияния), — остаток, а не держатель.
"""
from __future__ import annotations

from ...gateway_link.common import log
from .ship import epoch_sane

_HOSTING = ("taking", "serving", "releasing")


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
        except Exception:                                     # noqa: BLE001 — база старше эпохи (тесты, CLI)
            return False

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
        # home: резерв несёт моих — serving, его st свежее моего перехода домой (переезд мимо
        # меня; такт home переведёт и автомат в away; taking — ещё не факт: клон может не
        # встать); после «вернуть без резерва» — нет
        peer = self.reserve_peer_state()
        fresh = str(peer.get("at") or "") >= str(out.get("since") or "")
        later = (float(peer["at_ts"]) > float(out["at_ts"]) if peer.get("at_ts") is not None and out.get("at_ts") is not None
                 else str(peer.get("at") or "") > str(out.get("since") or ""))   # строго после перехода домой
        serving = (self.reserve_online() and later and str(peer.get("inn") or "idle") == "serving"
                   and not out.get("await_merge") and str(out.get("return_why") or "") != "force")
        # иначе решает слово Telegram, сказанное уже дома (прежнее «away» — до возврата)
        poll = self.rsv_get("rsv_poll")
        if self.reserve_poll() != "away" or _earlier(poll, out):
            return (True, "резерв несёт моих пользователей") if serving else (False, "пользователи и Telegram здесь")
        if self.reserve_online() and fresh and str(peer.get("inn") or "idle") not in _HOSTING:
            return False, "вебхук у резерва без гостя"
        return True, "Telegram: вебхук у резерва"

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
            if fork and not int(meta_now["acked"]) <= fork <= max(self.db.jrn_last_i(), int(meta_now["acked"])) + 1:
                log.warning("резерв: точка гостя %s вне моих номеров (подтверждено %s) — беру подтверждённое",
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
            meta = self.db.jrn_pass(holder="self", role="holder", why=why)
            if meta is not None:
                log.warning("резерв: эпоха %s здесь (%s)", meta["epoch"], why)
                self.reserve_follow_clear()

    def reserve_epoch_check(self) -> None:
        """Такт: страховка — роль снова сверяется с состоянием."""
        self.reserve_epoch_sync()
