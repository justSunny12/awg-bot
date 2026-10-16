"""settings.py — сверка выдаваемого с установленным по снимку канала."""
from __future__ import annotations

from .common import _cut


class SettingsMixin:
    # ── сверка выданного с установленным ─────────────────────────────────────

    def gwlink_config_drift_items(self, gw) -> list[tuple[str, str, str, str]]:
        """Чем установленное на шлюзе расходится с тем, что ВПС выдал: (ключ,
        человеческое имя, у сервера, на шлюзе) — тексты рисуют его по-своему (подсети в <code>, режим словами).
        До 64 значений с каждой стороны — значение обрезается, иначе экран с
        несколькими расхождениями перерос бы лимит сообщения. Снимка нет —
        расхождений не выдумываем: пустой список значит «сказать нечего»."""
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

    def gwlink_issued_env(self, gw) -> dict:
        """Что ВПС выдал бы слоту сейчас — по ключам юнита обвязки, нормализовано.
        Те самые значения, из которых собираются документ конфигурации и файл
        первого применения: второй схемы нет, и расходиться им не с чего."""
        from awgbot.util import gwlink
        env = dict(self._lan_env(gw))
        env["ADMIN_IPS"] = " ".join(self._gw_ssh_allow())
        return {k: " ".join(str(env.get(k, "")).split()) for k in gwlink.BUNDLE_KEYS}
