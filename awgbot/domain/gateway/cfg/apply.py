"""apply.py — документ целиком: проверка до первого действия, разделы по порядку, ответ cfg_ack, очередь ответа."""
from __future__ import annotations

import html
import json

from awgbot.domain import gwcfg
from awgbot.domain.gwcfg import CfgError
from awgbot.util import timeutil

from ..base import log


class CfgApplyMixin:
    _CFG_ACK_PENDING_KEY = "gwcfg_ack_pending"    # ответ, который не ушёл: линк перезапускался, сессия умерла

    def apply_cfg(self, msg: dict) -> dict:
        """Применить документ с сервера. Возвращает {"ack": тело cfg_ack,
        "note": текст уведомления или пусто, "ask": [разделы с новыми вопросами]}.

        Документ проверяется целиком до первого действия: одно значение не
        прошло — отвергается всё, шлюз не тронут. Порядок разделов: имя
        сервера; env + link одной транзакцией; почта; фраза — по правилу
        «молча или вопрос». Временный отказ транзакции (юнит обвязки ещё
        работает) — ответ с retry и без уведомления: сервер повторит."""
        parsed = gwcfg.parse(msg)
        gen, digest = parsed["gen"], parsed["hash"]
        try:
            checked = gwcfg.check_doc(parsed, link_cidr=self.cfg_link_cidr(), address=self.cfg_link_address(),
                                      fixed=self.cfg_link_fixed(), iface_nets=self._cfg_iface_nets())
        except CfgError as e:
            log.warning("gateway: документ конфигурации №%s не прошёл проверку: %s", gen, e)
            return {"ack": gwcfg.ack_build(gen, digest, failed={"*": str(e)}),
                    "note": f"⚠️ Сервер AWG прислал конфигурацию шлюза, которая не прошла проверку: "
                            f"{html.escape(str(e), quote=False)}. Ничего не менял", "ask": []}
        applied: list[str] = []
        changed: list[str] = []
        wait: dict = {}
        declined: dict = {}
        failed: dict = {}
        notes: list[str] = []
        asks: list[str] = []
        # 1. имя сервера — в БД агента
        if "server" in checked:
            if (self.db.get_state(self._SERVER_NAME_KEY) or "") != checked["server"]["name"]:
                self.db.set_state(self._SERVER_NAME_KEY, checked["server"]["name"])
                changed.append("server")
            applied.append("server")
        # 2. env + link — одна транзакция
        link_conf = checked.get("link")
        link_text = None
        if link_conf is not None:
            from awgbot.util import wgconf
            link_text = wgconf.render(link_conf, "link", stamp=link_conf.stamp or "1")
        res = self.cfg_apply_env_link(checked["env"], link_text)
        if res.get("retry"):
            return {"ack": gwcfg.ack_build(gen, digest, retry=True, failed={"env": res["error"]}), "note": "", "ask": []}
        if res["ok"]:
            applied.append("env")
            if link_conf is not None:
                applied.append("link")
            changed += res["changed"]
        else:
            failed["env"] = res["error"]
            if link_conf is not None:
                failed["link"] = res["error"]
            notes.append(f"⚠️ Сервер AWG прислал конфигурацию шлюза ({self._cfg_names(res['changed'])}), "
                         f"но она не применилась: {html.escape(res['error'], quote=False)}. Вернул прежнюю")
        # 3–4. почта и фраза — молча или вопрос
        for section in ("mail", "backup"):
            if section not in checked:
                continue
            value = checked[section]
            verdict, server_print = self.cfg_decide(section, value)
            if verdict == "apply":
                try:
                    note = self.cfg_apply_mail(value) if section == "mail" else self.cfg_apply_backup(value)
                except (ValueError, OSError) as e:
                    failed[section] = str(e)
                    continue
                applied.append(section)
                changed.append(section)
                notes += self._cfg_mail_lines(note) if section == "mail" else [
                    "🔐 Фраза с сервера AWG принята — новые копии шлюза шифруются ею"]
            elif verdict == "same":
                applied.append(section)
            elif verdict == "declined":
                declined[section] = server_print
            else:
                held = self.cfg_wait().get(section)
                self.cfg_hold(section, value, server_print, gen, digest)
                wait[section] = server_print
                if not held or held.get("server") != server_print:
                    asks.append(section)
        if not failed:
            with self.db.transaction():
                self.db.set_state(self._CFG_GEN_KEY, str(gen))
                self.db.set_state(self._CFG_HASH_KEY, digest)
        self.invalidate_static()
        head = ""
        if changed and not failed:
            head = f"⚙️ Сервер AWG прислал конфигурацию шлюза — применено: {self._cfg_names(changed)}"
        text = "\n".join([x for x in (head, *notes) if x])
        return {"ack": gwcfg.ack_build(gen, digest, applied=applied, wait=wait, declined=declined, failed=failed),
                "note": text, "ask": asks}

    @staticmethod
    def _cfg_names(keys) -> str:
        return ", ".join(gwcfg.human(k) for k in keys) or "ничего"

    @staticmethod
    def _cfg_mail_lines(note: dict) -> list[str]:
        if note.get("state") == "ok":
            lines = ["✉️ Почта с сервера AWG принята: IMAP и SMTP отвечают, вход выполнен"]
        else:
            lines = [f"✉️ Почта с сервера AWG принята, но проверка не прошла: {html.escape(str(note.get('why') or 'ошибка'), quote=False)}"]
        if note.get("backup"):
            lines.append("💾 Бэкапы теперь будут приходить на e-mail")
        return lines

    def _cfg_iface_nets(self) -> list[str]:
        """Подсети интерфейса шлюза по умолчанию — с ними AllowedIPs линка
        пересекаться не вправе."""
        from awgbot.infra import gwguard
        try:
            return list(gwguard.lan_nets())
        except Exception:                                  # noqa: BLE001 — нет ip, нет интерфейса: правило без этой части
            return []

    # ── очередь ответа: линк перезапускался, сессия умерла ───────────────────

    def cfg_ack_pending_put(self, ack: dict) -> None:
        self.db.set_state(self._CFG_ACK_PENDING_KEY, json.dumps({**ack, "at": timeutil.to_iso(timeutil.now())},
                                                                ensure_ascii=False))

    def cfg_ack_pending_get(self) -> dict:
        return self.db.get_state_json(self._CFG_ACK_PENDING_KEY, {}) or {}

    def cfg_ack_pending_clear(self) -> None:
        self.db.set_state(self._CFG_ACK_PENDING_KEY, "")

    def cfg_answer_ack(self, answer: dict) -> dict | None:
        """Ответ серверу после решения человека: раздел — в applied или
        declined с отпечатком, поколение и отпечаток — документа, который спросил."""
        if not answer.get("ok"):
            return None
        section = answer["section"]
        if answer["accepted"]:
            return gwcfg.ack_build(answer["gen"], answer["hash"], applied=[section])
        return gwcfg.ack_build(answer["gen"], answer["hash"], declined={section: answer["print"]})
