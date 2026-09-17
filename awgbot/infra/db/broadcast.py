"""broadcast.py — адресаты объявлений: владельцы выбранных профилей и
держатели их устройств.
"""

from __future__ import annotations


class BroadcastMixin:

    # ── Адресаты объявлений ──────────────────────────────────────────────────

    def broadcast_has_friends(self, client_ids, exclude_tg_id: int) -> bool:
        """Есть ли среди адресатов активные друзья.

        Нужно только для формулировки: предупреждать про гостевой доступ, когда
        его ни у кого нет, значит приучать пропускать это предложение мимо глаз
        — и не заметить его в тот раз, когда оно важно.
        """
        ids_list = sorted({int(c) for c in (client_ids or ())})
        if not ids_list:
            return False
        ph = ",".join("?" * len(ids_list))
        row = self._connection().execute(
            f"SELECT h.tg_id FROM devices d JOIN clients h ON h.id = d.holder_client_id "
            f" WHERE d.client_id IN ({ph}) AND h.tg_id IS NOT NULL AND h.tg_id != ? "
            f" LIMIT 1", (*ids_list, exclude_tg_id)).fetchone()
        return row is not None

    def broadcast_recipients_for_clients(self, client_ids, exclude_tg_id: int,
                                         owners_only: bool = False) -> list[int]:
        """Адресаты объявления по НАБОРУ профилей: владельцы + активные друзья.

        Друзья входят потому, что устройство у них от этих профилей: объявление
        вида «профиль ставится на паузу» касается их напрямую, а узнать иначе им
        неоткуда — владелец пересказывать не обязан. Админ выбирает профили, а
        не людей, поэтому про друзей его предупреждают на экране выбора.

        DISTINCT-инвариант: один человек
        может оказаться и владельцем, и другом чужого устройства, а доставка
        обязана быть одна.
        """
        ids_list = sorted({int(c) for c in (client_ids or ())})
        if not ids_list:
            return []
        ph = ",".join("?" * len(ids_list))
        q = (f"SELECT tg_id FROM clients "
             f" WHERE id IN ({ph}) AND tg_id IS NOT NULL AND is_service = 0 ")
        params: list = list(ids_list)
        if not owners_only:          # объявление с продлением — только владельцам
            q += (f"UNION "
                  f"SELECT h.tg_id FROM devices d JOIN clients h ON h.id = d.holder_client_id "
                  f" WHERE d.client_id IN ({ph}) AND h.tg_id IS NOT NULL")
            params += ids_list
        rows = self._connection().execute(q, params).fetchall()
        ids = {int(r["tg_id"]) for r in rows}
        ids.discard(int(exclude_tg_id))
        return sorted(ids)
