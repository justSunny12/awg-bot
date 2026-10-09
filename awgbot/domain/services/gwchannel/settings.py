"""settings.py — сверка выданного с установленным и настройки по каналу."""
from __future__ import annotations



from .common import _cut, drift_lines, log


class SettingsMixin:
    # ── сверка выданного с установленным ─────────────────────────────────────

    def gwlink_config_drift(self, gw, with_keys: bool = False):
        """Чем установленное на шлюзе расходится с тем, что ВПС выдал.

        Сравниваем своим же набором значений, из которых собирается бандл, —
        второй схемы сериализации для этого не нужно. Снимка нет — расхождений
        не выдумываем: пустой список значит «сказать нечего», а не «всё сошлось».
        """
        items = self.gwlink_config_drift_items(gw)
        keys = [k for k, _h, _m, _t in items]
        out = drift_lines(items, html=False)
        return (out, keys) if with_keys else out

    def gwlink_config_drift_items(self, gw) -> list[tuple[str, str, str, str]]:
        """То же расхождение структурой: (ключ, человеческое имя, у сервера, на
        шлюзе) — тексты рисуют его по-своему (подсети в <code>, режим словами).
        До 64 значений с каждой стороны — значение обрезается, иначе экран с
        несколькими расхождениями перерос бы лимит сообщения."""
        from awgbot.util import gwlink
        snap = self.gwlink_snapshot(gw.id)
        got = snap.get("bundle") if isinstance(snap.get("bundle"), dict) else None
        if not got:
            return []
        want = self.gwlink_issued_env(gw)
        items = []
        for key in gwlink.BUNDLE_KEYS:
            mine = want[key]
            theirs = " ".join(str(got.get(gwlink.snap_field(key), "")).split())
            if mine != theirs:
                items.append((key, gwlink.KEY_HUMAN[key], _cut(mine), _cut(theirs)))
        return items

    # ── настройки по каналу (этап 2) ─────────────────────────────────────────
    _GWLINK_ACK_KEY = "gwlink_ack"

    def gwlink_issued_env(self, gw) -> dict:
        """Что ВПС выдал бы слоту сейчас — по всем ключам бандла, нормализовано.
        Те самые значения, из которых собирается бандл: второй схемы нет, и
        расходиться им не с чего."""
        from awgbot.util import gwlink
        env = dict(self._lan_env(gw))
        env["ADMIN_IPS"] = " ".join(self._gw_ssh_allow())
        return {k: " ".join(str(env.get(k, "")).split()) for k in gwlink.BUNDLE_KEYS}

    def gwlink_settings_want(self, gw) -> dict:
        """Желаемые настройки слота — только ключи, которые везёт канал."""
        from awgbot.util import gwlink
        env = self.gwlink_issued_env(gw)
        return {k: env[k] for k in gwlink.SETTINGS_KEYS}

    def gwlink_settings_due(self, gw) -> dict | None:
        """Что отправить шлюзу сейчас, или None — отправлять нечего.

        Решает РАСХОЖДЕНИЕ, а не событие на ВПС: снимок показывает, что реально
        стоит в юните обвязки, и пока оно не совпало с выдаваемым, есть что
        доставлять. Кнопку, поменявшую настройку, это ловит так же, как бандл,
        применённый руками из старого файла. Снимка нет — молчим: судить не по
        чему, а слать вслепую значит рестартить обвязку без нужды."""
        from awgbot.util import gwlink
        _lines, keys = self.gwlink_config_drift(gw, with_keys=True)
        if not any(k in gwlink.SETTINGS_KEYS for k in keys):
            return None
        return self.gwlink_settings_want(gw)

    def gwlink_ack_in(self, slot_id: int, body: dict) -> None:
        """Итог применения настроек на шлюзе — тем же форматом, что прочие ответы."""
        ack = self._gwlink_ack_put(self._GWLINK_ACK_KEY, slot_id, body)
        if not ack["ok"]:
            log.warning("канал линка: слот %s не применил настройки: %s", slot_id, ack["error"])

    def gwlink_ack(self, slot_id: int) -> dict:
        return self._gwlink_ack_get(self._GWLINK_ACK_KEY, slot_id)
