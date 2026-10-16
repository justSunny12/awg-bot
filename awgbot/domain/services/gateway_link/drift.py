"""drift.py — расхождение конфигурации шлюза по снимку канала: что канал сам
не закроет, одно уведомление на набор расхождений и тихий отбой."""

from __future__ import annotations

from typing import Optional
from awgbot.core import config
from awgbot.domain.services.types import Notification


class DriftMixin:
    """Расхождение конфигурации шлюза по снимку канала."""
    # ── расхождение конфигурации по снимку канала ────────────────────────────
    _GW_DRIFT_NOTIFIED_KEY = "gw_drift_notified"
    _GW_DRIFT_MAX_LINES = 20                  # строк на группу устройств в уведомлении; дальше «…и ещё N»

    def _gw_drift_pending(self, g, items: list) -> list:
        """Что из расхождения по снимку канал сам не закроет.

        Канал обязателен, и о доставке решает он: документ конфигурации уходит
        слоту при каждом подключении. Поэтому пока шлюз не на связи — молчим,
        сколько бы он ни молчал (расхождение всё это время видно в карточке),
        а на связи ждём итога доставки. Остаётся то, что шлюз отверг в ответе
        на текущий документ (ответ несёт отпечаток документа — иначе старый
        отказ сошёл бы за отказ на новое)."""
        if not self.gwlink_session(g.id):
            return []
        ack = self.gwcfg_ack(g.id)
        if not ack or ack.get("retry") or ack.get("hash") != self.gwcfg_doc(g)["hash"] or not ack.get("failed"):
            return []                                 # нет ответа, временный отказ или ответ на прежний документ
        failed = ack["failed"]
        out = [it for it in items if "env" in failed or "*" in failed]
        if "link" in failed:
            out.append(("link", "конфиг линка", "", ""))
        return out

    def _gw_drift_note(self, g) -> Optional[Notification]:
        """Одно уведомление на набор расхождений, которые канал сам не закроет;
        сошлось после уведомления — один тихий отбой. Снимка нет — молчим:
        судить не по чему."""
        snap = self.gwlink_snapshot(g.id)
        if not isinstance(snap.get("bundle"), dict):
            return None
        items = self.gwlink_config_drift_items(g)
        key = self._gw_slot_key(self._GW_DRIFT_NOTIFIED_KEY, g.id)
        told = self.db.get_state(key) or ""
        # отказ бывает и без расхождения в bundle (конфиг линка)
        pending = self._gw_drift_pending(g, items)
        if not pending:
            if not items and told:
                self.db.set_state(key, "")
                return Notification(config.ADMIN_ID, f"✅ {self._gw_link(g)}: конфигурация актуализирована")
            return None
        # подпись — по полным значениям обеих сторон и итогу отказа: строки
        # расхождения обрезаны и слились бы у длинных списков
        import hashlib
        from awgbot.util import gwlink
        env, got = self.gwlink_issued_env(g), snap["bundle"]
        src = [f"{k}={env.get(k, '')}|{' '.join(str(got.get(gwlink.snap_field(k), '')).split())}"
               for k, _h, _m, _t in pending] + [str(self.gwcfg_ack(g.id).get("hash") or "")]
        sig = hashlib.sha256("\n".join(src).encode()).hexdigest()
        if told == sig:
            return None
        self.db.set_state(key, sig)
        return Notification(config.ADMIN_ID, self._gw_drift_text(g, pending))

    def _gw_drift_text(self, g, pending: list) -> str:
        """Текст уведомления: что разошлось — именами; отказ шлюза — второй
        строкой; устройства админа — группами по именам, прочее — «у сервера …,
        на шлюзе …»."""
        import html
        from awgbot.domain.services.gwchannel import drift_lines
        names = ", ".join(html.escape(human, quote=False) for _k, human, _m, _t in pending)
        lines = [f"🛰 {self._gw_link(g)}: конфигурация неактуальна — {names}"]
        failed = self.gwcfg_ack(g.id).get("failed") or {}
        why = "; ".join(failed.values())
        if why:
            lines.append(f"Шлюз не применил: {html.escape(why, quote=False)}")
        for key, human, mine, theirs in pending:
            if key == "ADMIN_IPS":
                lines += self._gw_admin_drift_lines(g)
            elif key == "link":
                continue                                  # конфиг линка назван в заголовке, значений у него нет
            else:
                lines.append(drift_lines([(key, human, mine, theirs)], html=True)[0])
        return "\n".join(lines)

    def _gw_admin_drift_lines(self, g) -> list[str]:
        """Разница по устройствам админа — строка на устройство: «у сервера, нет на
        шлюзе» — имена из devices по адресу; «на шлюзе, нет у сервера» — либо
        живое устройство другого профиля (адрес передан), либо удалённое — имя из
        истории, нет имени — только адрес. Адреса — из самого снимка, не из
        обрезанной строки расхождения; группа длиннее предела — «…и ещё N»."""
        import html
        from awgbot.util import gwlink
        snap = self.gwlink_snapshot(g.id)
        got = snap.get("bundle") if isinstance(snap.get("bundle"), dict) else {}
        want = set(self.gwlink_issued_env(g)["ADMIN_IPS"].split())
        have = {t for t in str(got.get(gwlink.snap_field("ADMIN_IPS"), "")).split()[:64] if gwlink._ipv4(t.split("/")[0])}

        def ip_key(a: str) -> tuple:
            import ipaddress
            try:
                return (0, int(ipaddress.ip_address(a.split("/")[0])))
            except ValueError:
                return (1, 0)

        def line(addr: str, name: str) -> str:
            code = f"<code>{html.escape(addr, quote=False)}</code>"
            return f" · {html.escape(name, quote=False)} ({code})" if name else f" · {code}"

        def group(addrs: list[str], names: dict, one: str, many: str) -> list[str]:
            if not addrs:
                return []
            rows = [one if len(addrs) == 1 else many]
            rows += [line(a, names.get(a, "")) for a in addrs[:self._GW_DRIFT_MAX_LINES]]
            if len(addrs) > self._GW_DRIFT_MAX_LINES:
                rows.append(f" · …и ещё {len(addrs) - self._GW_DRIFT_MAX_LINES}")
            return rows

        missing = sorted(want - have, key=ip_key)
        out = group(missing, self.db.device_names_by_address(missing),
                    "На шлюзе не помечено админским устройство:", "На шлюзе не помечены админскими устройства:")
        extra = sorted(have - want, key=ip_key)
        alive = self.db.device_names_by_address(extra)      # адрес живёт у устройства другого профиля
        out += group([a for a in extra if a in alive], alive,
                     "На шлюзе помечено админским устройство другого профиля:",
                     "На шлюзе помечены админскими устройства других профилей:")
        gone = [a for a in extra if a not in alive]
        out += group(gone, self.db.archived_device_names_by_address(gone),
                     "На шлюзе помечено админским удалённое устройство:",
                     "На шлюзе помечены админскими удалённые устройства:")
        return out

    def gw_bundle_drift_notes(self) -> list[Notification]:
        """Уведомления о расхождении конфигурации шлюзов — только по снимку канала:
        что реально стоит на шлюзе против того, что ВПС выдал бы сейчас. Памяти о
        выдаче больше нет: канал обязателен, и настройки обвязки он довозит сам."""
        if not config.ROUTING_ENABLED:
            return []
        notes = []
        for g in self.db.gateways():
            note = self._gw_drift_note(g)
            if note is not None:
                notes.append(note)
        return notes
