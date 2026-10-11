"""apply.py — документ целиком: проверка до первого действия, разделы по порядку, ответ cfg_ack, очередь ответа."""
from __future__ import annotations

import html
import json
import time

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
                self.cfg_release(section)                 # раздела у сервера больше нет — вопрос потерял смысл
                continue
            value = checked[section]
            verdict, server_print = self.cfg_decide(section, value)
            if verdict != "ask":
                self.cfg_release(section)                 # совпало, применяется молча или отклонено ранее
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
        retry = False
        # 5. токен бота — только тот же бот; перезапуск агента — после ответа
        # серверу (клиент канала), иначе ответ ушёл бы в никуда
        restart = False
        if "token" in checked:
            res = self.cfg_token_apply(checked["token"])
            if res["state"] == "same":
                applied.append("token")
            elif res["state"] == "applied":
                applied.append("token")
                changed.append("token")
                restart = True
            else:
                failed["token"] = res["error"]
                retry = retry or res["state"] == "retry"
                if res["state"] == "failed":
                    # бот на прежнем токене может быть жив (токен другого бота) — сказать
                    notes.append(f"⚠️ Токен бота с сервера AWG не применён: {html.escape(res['error'], quote=False)}")
        # 6. аплинк — с подтверждением человека; молча — только ключи признания
        # и аварийно (аплинк мёртв, конфиг того же устройства)
        if "uplink" in checked:
            # прерванная перезапуском транзакция — назад до сравнения: иначе
            # полуновое на диске сравнилось бы как «то же»
            recovered = self.cfg_uplink_recover()
            if recovered:
                notes.append(recovered)
            value = dict((parsed.get("sections") or {}).get("uplink") or {})
            verdict, server_print = self.cfg_uplink_decide(value)
            if verdict != "ask":
                self.cfg_release("uplink")
            if verdict == "same":
                applied.append("uplink")
            elif verdict == "keys":
                res = self.cfg_uplink_keys_only(value)
                if res["ok"]:
                    applied.append("uplink")
                    changed.append("uplink")
                else:
                    failed["uplink"] = res["error"]
                    retry = retry or bool(res.get("busy"))   # обвязка занята — сервер повторит
            elif verdict == "declined":
                declined["uplink"] = server_print
            elif verdict == "emergency":
                same = self.cfg_uplink_same_device(value)
                res = self.cfg_uplink_apply(value)
                if res.get("state") != "busy":
                    self.__dict__["_uplink_emergency_at"] = time.monotonic()
                if res["ok"]:
                    applied.append("uplink")
                    notes.append(self.cfg_uplink_emergency_note(same))
                else:
                    # не «отвергнуто»: временная причина (пир ещё не поднят, DNS) —
                    # раздел ждёт как вопрос, тик повторит аварийную попытку
                    notes.append(self.cfg_uplink_fail_note(res))
                    self.cfg_hold("uplink", value, server_print, gen, digest)
                    wait["uplink"] = server_print
                    asks.insert(0, "uplink")
            else:
                held = self.cfg_wait().get("uplink")
                self.cfg_hold("uplink", value, server_print, gen, digest)
                wait["uplink"] = server_print
                if not held or held.get("server") != server_print:
                    asks.insert(0, "uplink")              # аплинк — первым в очереди
        else:
            self.cfg_release("uplink")
        if not failed:
            with self.db.transaction():
                self.db.set_state(self._CFG_GEN_KEY, str(gen))
                self.db.set_state(self._CFG_HASH_KEY, digest)
        self.invalidate_static()
        head = ""
        if changed and not failed:
            head = f"⚙️ Сервер AWG прислал конфигурацию шлюза — применено: {self._cfg_names(changed)}"
        text = "\n".join([x for x in (head, *notes) if x])
        return {"ack": gwcfg.ack_build(gen, digest, applied=applied, wait=wait, declined=declined, failed=failed,
                                       retry=retry),
                "note": text, "ask": asks, "changed": changed, "restart": restart}

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

    # ── очередь ответа: линк перезапускался, сессия умерла ───────────────────

    def _cfg_ack_queue(self) -> list:
        raw = self.db.get_state_json(self._CFG_ACK_PENDING_KEY, [])
        if isinstance(raw, dict):
            raw = [raw] if raw else []
        return [a for a in (raw or []) if isinstance(a, dict)]

    def cfg_ack_pending_put(self, ack: dict) -> None:
        """В очередь, в конец: ответ на документ и ответ человека на вопрос не
        перетирают друг друга — уходят по порядку."""
        queue = self._cfg_ack_queue() + [{**ack, "at": timeutil.to_iso(timeutil.now())}]
        self.db.set_state(self._CFG_ACK_PENDING_KEY, json.dumps(queue[-8:], ensure_ascii=False))

    def cfg_ack_pending_get(self) -> dict:
        queue = self._cfg_ack_queue()
        return queue[0] if queue else {}

    def cfg_ack_pending_clear(self) -> None:
        """Первый из очереди ушёл."""
        queue = self._cfg_ack_queue()[1:]
        self.db.set_state(self._CFG_ACK_PENDING_KEY, json.dumps(queue, ensure_ascii=False) if queue else "")

    def cfg_answer_ack(self, answer: dict) -> dict | None:
        """Ответ серверу после решения человека: раздел — в applied или
        declined с отпечатком, поколение и отпечаток — документа, который спросил."""
        if not answer.get("ok"):
            return None
        section = answer["section"]
        if answer.get("failed"):
            return gwcfg.ack_build(answer["gen"], answer["hash"], failed={section: str(answer["failed"])})
        if answer["accepted"]:
            return gwcfg.ack_build(answer["gen"], answer["hash"], applied=[section])
        return gwcfg.ack_build(answer["gen"], answer["hash"], declined={section: answer["print"]})
