"""quarantine.py — часть пакета gwjoin (см. __init__): признак сопряжения для
остального бота (§3.7).

Новый слот до кода строки в `gateways` не имеет — его нет нигде. Слот в
замене строку имеет, поэтому: для автомата недоступен, политики маршрута нет
(снята при выпуске команды), в VPN-транзит и подсети соседей не входит,
ручная смена ключей и прочие доставки до него не доходят (сессии на прежних
ключах не будет — ключи токена у другого устройства).
"""
from __future__ import annotations

import glob
import os

from awgbot.util import timeutil

from ..gateway_link import slotkeys
from ..gateway_link.common import log

# прежние файлы первого применения (выпуски до 3.4.0): внутри ключи линка,
# токен бота шлюза и ADMIN_ID — на сервере им больше незачем лежать
LEGACY_BUNDLES = "/root/awg-gw-bundle*.sh"
LEGACY_KEYS = ("gw_bundle_issued_at", "gw_bundle_ssh_allow")


class GwJoinQuarantineMixin:
    """Кто сейчас в сопряжении."""

    def gwjoin_active(self) -> bool:
        """Идёт сопряжение (команда жива или смена ключей после кода не
        зафиксирована) — переход линков на I1 и прочие автоматические смены
        ключей ждут."""
        return any(st.get("state") in ("waiting", "connected", "accepted") for st in self.gwjoin_all())

    def gwjoin_replacing(self, slot_id: int) -> bool:
        """Слот в замене устройства до фиксации смены ключей после кода."""
        if slot_id is None:
            return False                                  # слотов нет: линк обвязки без строки
        st = self.gwjoin(slot_id)
        return st.get("kind") in ("replace", "same") and st.get("state") in ("waiting", "connected", "accepted")

    def gwjoin_session_barred(self, slot_id: int) -> bool:
        """Обычная сессия слота (hello без join) не открывается: слот в
        сопряжении — любой, и новый, и в замене или «Настроить заново» (ключ
        канала уже из ключей токена). Можно только после переключения ВПС на
        постоянные ключи (смена после кода зафиксирована ВПС): hello новым
        ключом и есть фиксация. Без записи — по штампу линка (запись снята
        сроком, а ключи токена стоят)."""
        st = self.gwjoin(slot_id)
        state = st.get("state")
        if state in ("waiting", "connected", "closing"):
            return True
        if state == "accepted":
            rk = self.gateway_rekey(slot_id) if hasattr(self, "gateway_rekey") else {}
            return not (rk.get("state") == "switched" and rk.get("committed"))
        gw = self.db.gateway(slot_id)
        link_if = gw.link_if if gw is not None else st.get("link_if", "")
        return bool(link_if) and self._gwjoin_stamped(link_if)

    @staticmethod
    def _gwjoin_stamped(link_if: str) -> bool:
        from awgbot.core import config
        from awgbot.infra.nftguard.collect import JOIN_STAMP
        try:
            with open(f"{config.AWG_DIR}/{link_if}.conf", encoding="utf-8") as f:
                head = [next(f, "") for _ in range(5)]
        except OSError:
            return False
        return any(line.startswith(JOIN_STAMP) for line in head)

    def gwjoin_new_cards(self) -> list[dict]:
        """Новые слоты в сопряжении (строки в `gateways` у них нет) — для
        экрана «🛰 Шлюзы»: [{"slot", "name", "state", "until"}]."""
        out = []
        for st in self.gwjoin_all():
            if st.get("kind") != "new":
                continue
            card = self.gwjoin_card(int(st["slot"]))
            if card:
                out.append({"slot": int(st["slot"]), "name": str(st.get("name") or ""), "id": st.get("id", ""),
                            **card})
        return out

    def gwjoin_card(self, slot_id: int) -> dict:
        """Для карточки и списка (Т-П15): {"state": waiting | code | replace,
        "until": ISO} или пусто."""
        st = self.gwjoin(slot_id)
        if st.get("state") not in ("waiting", "connected"):
            return {}
        try:
            if timeutil.elapsed(timeutil.parse_iso(str(st.get("expires") or ""))) >= 0:
                return {}
        except ValueError:
            return {}
        if st.get("kind") in ("replace", "same") and st.get("state") == "waiting":
            return {"state": "replace", "until": st["expires"]}
        return {"state": "code" if st.get("state") == "connected" else "waiting", "until": st["expires"]}

    def gwjoin_boot(self) -> int:
        """Старт сервера: прежние файлы первого применения — прочь (§8.4), их
        метки в базе — тоже. Идемпотентно; число удалённых файлов."""
        n = 0
        for path in glob.glob(LEGACY_BUNDLES):
            try:
                os.remove(path)
                n += 1
            except OSError as e:
                log.warning("прежний файл конфигурации шлюза %s не удалён: %s", path, e)
        for g in self.db.gateways():
            slotkeys.forget(self.db, g.id, LEGACY_KEYS)
        if n:
            log.info("прежних файлов конфигурации шлюза удалено: %s", n)
        return n
