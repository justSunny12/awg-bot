"""
pledges.py — часть пакета quorum (см. __init__): снятие обязательства «нет»
у голосующего, который снова видит лидера, когда кандидат ничего не решает
(концепт «кворум» §3.4.3, §3.5.1).

Шлюз, отдав `qview{uns}`, держит обязательство: его пробы лидеру идут `held`, пока
кандидат не скажет `qrel{e}`/`qtake{e}` или не выйдет `pledge_minutes`. Такт кандидата
снимает его `rel`, только когда шёл отсчёт; одиночное «не вижу» без большинства (ложное
при включении голосования — зонд спросил раньше первого ответа лидера, короткий сбой
связи одного шлюза) держало «да» живого лидера до срока. Здесь — `qrel` таким узлам, одно
на обязательство, и только без всякого решения и исполнения: `qrel{e}` при отданном
`qtake` откатил бы у шлюза поднятую эпоху e + 1 к e, и прежний лидер набрал бы «да» при
живом держателе. Два кандидата — о линии второй стороны (`_qcand`) и о своём клоне в
отъезде (Q1, `_qback`).
"""
from __future__ import annotations

from awgbot.util.quorum.candidate import Candidate

from ...gateway_link.common import log


class ReserveQuorumPledgesMixin:

    def _quorum_seen_again(self, c: Candidate) -> list[tuple]:
        """Шлюз сказал «нет» и снова прислал «вижу», а я ничего не решаю — `qrel{e}` ему, одно
        на обязательство (§3.5.1: «нет» больше нет). Без этого его пробы лидеру идут `held` до
        `pledge_minutes`: ложное «не вижу» при включении голосования (зонд шлюза спросил раньше
        первого ответа лидера) или короткий сбой связи одного шлюза — у живого лидера на голос
        «да» меньше. Только без всякого решения и исполнения: ни отсчёта, ни «забираю» (`decided`),
        ни отданного `qtake` (`_quo_took`), ни исполнения (`_quo_exec`), ни ввода кода, зона
        свободна — иначе `qrel{e}` откатил бы у шлюза поднятую по qtake эпоху e + 1 к e, и прежний
        лидер набрал бы «да» при живом держателе. Эпоха — по фактам (`_quorum_known_e`, как в
        `_quorum_rel`) и только та, о которой шлюз сам сказал в «вижу»: обязательство под ней,
        поднятой эпохи у него нет — откатывать нечего."""
        if (self.__dict__.get("_quo_took") or self.__dict__.get("_quo_exec") or not self.reserve_zone_free()
                or (self.rsv_get("rsv_view").get("code") or {}).get("pending")):
            return []
        nodes = c.seen_again()
        if not nodes:
            return []
        e = self._quorum_known_e(c)
        out: list[tuple] = []
        done = []
        for node in nodes:
            if int(c.votes[node].e) != e:
                continue                                      # эпохи расходятся — обязательство снимет срок или факт
            out += self._quorum_to_gw(node, "qrel", {"l": c.l, "e": e})
            done.append(node)
        c.unpledge(done)
        if done:
            log.info("кворум: «%s» снова видят %s — обязательство «нет» снято (qrel)", self.reserve_peer_name(), ",".join(done))
        return out

    def _quorum_back_seen_again(self, c: Candidate, v) -> list[tuple]:
        """Как у кандидата о второй линии (`_quorum_seen_again`): мой шлюз сказал «нет» о клоне
        и снова прислал «вижу», а я ничего не решаю — `qrel{e}` ему, одно на обязательство;
        иначе его пробы гостю идут `held` до `pledge_minutes`. Только без отсчёта и решения,
        пока мои пользователи у резерва и аварийного возврата нет (`await_merge` — рычаг или
        возврат по кворуму уже пошёл: `qtake` отдан, шлюзы держат поднятую эпоху); эпоха — та
        же, что в `rel` Q1 (`reserve_quorum_back_tick`), и только совпавшая со словом шлюза в «вижу»."""
        if self.reserve_out() != "away" or self.rsv_get("rsv_out").get("await_merge"):
            return []
        nodes = c.seen_again()
        e = int(v.epoch) if v is not None else 0
        done = [n for n in nodes if int(c.votes[n].e) == e]
        out: list[tuple] = []
        for node in done:
            out += self._quorum_to_own_gw(node, "qrel", {"l": c.l, "e": e})
        c.unpledge(done)
        if done:
            log.info("кворум: мой бот на «%s» снова видят %s — обязательство «нет» снято (qrel)", self.reserve_peer_name(),
                     ",".join(done))
        return out


__all__ = ["ReserveQuorumPledgesMixin"]
