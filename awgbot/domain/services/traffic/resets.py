"""resets.py — месячные сбросы лимитов."""
from __future__ import annotations

import datetime
import logging

import html as _html

from awgbot.util import timeutil
from awgbot.core.blocks import DeviceBlock, ClientBlock
from awgbot.core.enums import FriendStatus
from awgbot.domain.services.types import Notification

from awgbot.domain.notices.traffic import _gb_limit, _reset_client_text, _reset_friend_text

log = logging.getLogger("awgbot.services")


class ResetsMixin:
    # ── Сбросы ───────────────────────────────────────────────────────────────

    def reset_monthly_traffic(self) -> list["Notification"]:
        """1-е число: обнулить месячные счётчики + доп.квоту + трафик-метки, и
        снять причину TRAFFIC со всех клиентов и устройств (разблокировать, если
        не осталось других причин). Причину EXPIRY НЕ трогаем — подписка живёт
        своим циклом. Возвращает уведомления о сбросе (профилям и друзьям);
        безлимитные позиции не показываем, пустые уведомления не шлём."""
        # аудит-метрика: снимок потребления завершившегося месяца ПЕРЕД обнулением.
        # Метка месяца — предыдущий календарный (сброс идёт 1-го числа за прошлый).
        _now = timeutil.now()
        _prev_month = (_now.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
        notes: list[Notification] = []
        friend_devs: dict[int, list] = {}     # friend_tg → [device rows] для их уведомлений
        # Одной транзакцией: и один fsync вместо ~5N+4NM, и атомарность —
        # падение посередине не оставит половину клиентов сброшенной.
        with self.db.transaction():
          self.db.snapshot_monthly_traffic(_prev_month)
          rf = self.rf_month_total()
          self.db.snapshot_server_rf(_prev_month, rf["rx"], rf["tx"])
          self.db.set_state(self._RF_MONTH_RX_KEY, "0")
          self.db.set_state(self._RF_MONTH_TX_KEY, "0")
          self.db.reset_month_traffic_all()
          twins = self.db.twins_by_origin()
          for client in self.db.list_clients(include_service=False):
            self.db.update_client_fields(
                client.id, bonus_bytes=0, bonus_granted_month=0)
            self.db.reset_traffic_notified(client.id)
            if int(client.block_reason) & int(ClientBlock.TRAFFIC_CLIENT):
                self._client_clear_block(client.id, ClientBlock.TRAFFIC_CLIENT)
            own_lines = []                    # лимитные СВОИ (не переданные) устройства
            for dev in self.db.list_devices(client.id):
                # месячный сброс снимает ОБЕ трафик-причины (свою и каскад клиента)
                for _tbit in (DeviceBlock.TRAFFIC_USER, DeviceBlock.TRAFFIC_CLIENT):
                    if int(dev.block_reason) & int(_tbit):
                        self._device_clear_block(dev.id, _tbit, twins)
                lim = int(dev.traffic_limit)
                if dev.friend_status == FriendStatus.ACTIVE and dev.friend_tg_id:
                    if lim > 0:               # друг увидит в своём уведомлении
                        friend_devs.setdefault(dev.friend_tg_id, []).append(dev)
                elif lim > 0:
                    own_lines.append(f"{_html.escape(dev.name, quote=False)} — {_gb_limit(lim)}")
            # профилю шлём, если есть что показать: лимит профиля ИЛИ лимитные устройства
            total_limit = int(client.traffic_limit)
            if client.tg_id and (total_limit > 0 or own_lines):
                notes.append(Notification(
                    client.tg_id, _reset_client_text(total_limit, own_lines)))
        # друзьям — по их лимитным устройствам
        for friend_tg, devs in friend_devs.items():
            lines = [f"{_html.escape(d.name, quote=False)} — {_gb_limit(int(d.traffic_limit))}" for d in devs]
            notes.append(Notification(friend_tg, _reset_friend_text(lines)))
        return notes
