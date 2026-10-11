"""cfg.py — документ конфигурации слота: желаемое, поколение, что доставить, ответ шлюза."""
from __future__ import annotations

import html
import json
import re
import socket

from awgbot.core import config, settings
from awgbot.domain import gwcfg
from awgbot.domain.services.types import Notification
from awgbot.util import gwlink, timeutil, wgconf

from .common import log

_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class CfgMixin:
    """Сервер — источник желаемого: документ каждый раз вычисляется из тех же
    источников, из которых собирается файл первого применения. Второй схемы
    нет — расходиться ей не с чем."""
    _GWCFG_GEN_KEY = "gwcfg_gen"
    _GWCFG_HASH_KEY = "gwcfg_hash"
    _GWCFG_ACK_KEY = "gwcfg_ack"
    _GWCFG_LINK_ERR_KEY = "gwcfg_link_err"     # почему желаемый конфиг линка не собран (К5)
    _GWCFG_UPLINK_ERR_KEY = "gwcfg_uplink_err"  # почему раздел uplink не едет: не прошёл проверку агента

    # ── желаемое ─────────────────────────────────────────────────────────────

    def gwcfg_env(self, gw) -> dict:
        """Раздел env — в именах документа."""
        lan = self._lan_env(gw)
        return {"CLIENT_SUBNET": str(settings.get("app.network.subnet_cidr", f"{config.SUBNET_PREFIX}.0/24") or ""),
                "ADMIN_IPS": " ".join(self._gw_ssh_allow()),
                "LAN_MODE": lan["LAN_MODE"],
                "LAN_SUBNETS": lan["HOME_SUBNETS"],
                "RESOLVER": lan["RESOLVER"],
                "PEER_LAN_NETS": lan["PEER_HOME_NETS"],
                "LINK_CHANNEL_PORT": str(gwlink.channel_port())}

    def _gwcfg_live_port(self, gw) -> int:
        """Порт линка — факт из живого конфига ВПС, как при сборке файла."""
        try:
            with open(f"{config.AWG_DIR}/{gw.link_if}.conf", encoding="utf-8") as f:
                m = re.search(r"^ListenPort\s*=\s*(\d+)", f.read(), re.M)
            if m:
                return int(m.group(1))
        except OSError:
            pass
        return int(gw.link_port)

    def gwcfg_link_conf(self, gw, env: dict) -> str:
        """Желаемый конфиг линка шлюзовой стороны: источник `/root/gw-<линк>.conf`
        с теми же поправками, что делает сборка файла (порт Endpoint — из живого
        ListenPort, хост — network.server_host, AllowedIPs — по правилу документа,
        PersistentKeepalive — из настроек клиентов). Текст канонический, со
        штампом контракта. Не собрался (нет источника, правило AllowedIPs не
        пропускает подсеть соседа) — пусто, причина — в gwcfg_link_error."""
        try:
            with open(f"/root/gw-{gw.link_if}.conf", encoding="utf-8") as f:
                conf = wgconf.parse(f.read())
            conf.comments = []                      # источник начинается с пояснений — в документ они не идут
            wgconf.check_keys(conf, "link")
        except (OSError, wgconf.WgConfError) as e:
            self._gwcfg_link_error_set(gw.id, f"конфиг линка не прочитан: {e}")
            return ""
        host = str(settings.get("app.network.server_host", config.SERVER_HOST) or "").strip()
        old_host = conf.get("Peer", "Endpoint").rpartition(":")[0]
        if not (host and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)):
            host = old_host
        wgconf.set_value(conf, "Peer", "Endpoint", f"{host}:{self._gwcfg_live_port(gw)}")
        want = wgconf.allowed_ips_want(client_subnet=env["CLIENT_SUBNET"], link_cidr=gw.link_cidr,
                                       peer_nets=env["PEER_LAN_NETS"].split())
        wgconf.set_value(conf, "Peer", "AllowedIPs", ", ".join(want))
        ka = str(settings.get("app.client_config.keepalive_seconds", config.KEEPALIVE_SECONDS) or "25-35")
        if not re.fullmatch(r"\d{1,5}(-\d{1,5})?", ka):
            ka = "25-35"
        wgconf.set_value(conf, "Peer", "PersistentKeepalive", ka)
        text = wgconf.render(conf, "link", stamp=gwcfg.LINK_CONTRACT)
        try:
            # то же правило, что проверяет шлюз: желаемого, которое он заведомо
            # отвергнет, сервер не шлёт — показывает отказом в карточке
            gwcfg.check_link(text, env, link_cidr=gw.link_cidr)
        except gwcfg.CfgError as e:
            self._gwcfg_link_error_set(gw.id, str(e))
            return ""
        self._gwcfg_link_error_set(gw.id, "")
        return text

    def _gwcfg_link_error_set(self, slot_id: int, text: str) -> None:
        key = self._gwlink_key(self._GWCFG_LINK_ERR_KEY, slot_id)
        if (self.db.get_state(key) or "") != text:
            self.db.set_state(key, text)

    def gwcfg_link_error(self, slot_id: int) -> str:
        return self.db.get_state(self._gwlink_key(self._GWCFG_LINK_ERR_KEY, slot_id)) or ""

    def gwcfg_uplink(self, gw) -> dict | None:
        """Раздел uplink — только агентам с confirm1 (подтверждение в чате): конфиг
        устройства-шлюза в форме для шлюза и ключи признания; в окне переезда —
        двойник, прежний ключ отдельно (как в файле первого применения). Нет
        устройства или конфиг не собран — None."""
        if "confirm1" not in self.gwlink_agent_caps(gw.id) or not gw.device_id:
            return None
        dev = self.db.get_device(gw.device_id)
        if dev is None:
            return None
        twin = self.db.twin_of_device(dev.id)
        target = twin or dev
        try:
            conf = self.gateway_uplink_conf(target)
        except Exception as e:                            # noqa: BLE001 — устройство без конфига: раздела нет
            log.warning("документ конфигурации: слот %s, аплинк не собран: %s", gw.id, e)
            return None
        value = {"conf": conf, "pubkey": target.public_key, "prev_pubkey": dev.public_key if twin else ""}
        # та же проверка, что сделает агент: отказ одного раздела агент
        # считает отказом документа целиком — env, линк, почта встали бы тоже
        key = self._gwlink_key(self._GWCFG_UPLINK_ERR_KEY, gw.id)
        try:
            gwcfg.check_uplink(value)
        except gwcfg.CfgError as e:
            if (self.db.get_state(key) or "") != str(e):
                self.db.set_state(key, str(e))
                log.warning("документ конфигурации: слот %s, аплинк не едет: %s", gw.id, e)
            return None
        if self.db.get_state(key):
            self.db.set_state(key, "")
        return value

    def gwcfg_old_uplink(self) -> list[tuple[str, dict]]:
        """Финал переезда: слоты с confirm1, чей шлюз ещё не поднимал аплинк
        двойника (новый конфиг ждёт «Применить» в его чате) — после финала
        прежний пир снимется, и бот шлюза потеряет Telegram. [(имя, бот)]."""
        out = []
        for gw in self.db.gateways():
            if not gw.device_id or "confirm1" not in self.gwlink_agent_caps(gw.id):
                continue
            twin = self.db.twin_of_device(gw.device_id)
            if twin is not None and not twin.last_handshake:
                out.append((self._gw_display(gw), self.gw_bot_identity(gw.id)))
        return out

    def gwcfg_token(self, gw) -> dict | None:
        """Раздел token — только агентам с token1 и только токен, введённый
        аварийной кнопкой («🔧 Сервис → 🤖 Токен бота»): токен в env ВПС с
        настройки мог устареть (меняли на шлюзе руками), и слать его — значит
        получить вечный отказ «Telegram не принял». Значение не прошло ту же проверку, что
        сделает агент, — раздела нет (отказ одного раздела агент считает
        отказом всего документа)."""
        if "token1" not in self.gwlink_agent_caps(gw.id):
            return None
        token = self.gw_bot_token(gw.id)
        if not token or not self.gateway_token_pushed(gw.id, token):
            return None                                   # не вводили кнопкой — env с настройки мог устареть
        try:
            return gwcfg.check_token({"value": token})
        except gwcfg.CfgError as e:
            log.warning("документ конфигурации: слот %s, токен не едет: %s", gw.id, e)
            return None

    def gwcfg_mail(self) -> dict | None:
        acc = self.email_account()
        if acc is None:
            return None
        return {"login": acc.login, "password": acc.password, "imap_host": acc.imap_host,
                "imap_port": acc.imap_port, "smtp_host": acc.smtp_host, "smtp_port": acc.smtp_port,
                gwcfg.MAIL_HINT: self.backup_channel()}

    def gwcfg_backup(self) -> dict | None:
        if self.backup_encryption_mode() != "passphrase":
            return None
        return {"phrase": self.db.get_state(self._BK_PASSPHRASE_KEY) or ""}

    @staticmethod
    def gwcfg_server() -> dict | None:
        try:
            name = socket.gethostname().strip()
        except OSError:
            name = ""
        m = _NAME_RE.match(name)
        return {"name": m.group(0)} if m else None

    def gwcfg_desired(self, gw) -> dict:
        """Разделы документа слота: None — у сервера раздела нет (своё на шлюзе
        не трогается). Аплинк — агентам с confirm1, токен — с token1.
        Раздел env не прошёл ту же проверку, что делает шлюз, — документа нет
        вовсе (без env агент отверг бы его целиком), причина — в карточке."""
        env = self.gwcfg_env(gw)
        try:
            gwcfg.check_env(env)
        except gwcfg.CfgError as e:
            self._gwcfg_link_error_set(gw.id, f"раздел env: {e}")
            return {name: None for name in gwcfg.SECTIONS}
        link = self.gwcfg_link_conf(gw, env)
        return {"env": env, "link": {"conf": link} if link else None, "uplink": self.gwcfg_uplink(gw),
                "mail": self.gwcfg_mail(), "backup": self.gwcfg_backup(), "token": self.gwcfg_token(gw),
                "server": self.gwcfg_server()}

    @staticmethod
    def gwcfg_prints(sections: dict) -> dict:
        """Отпечатки разделов желаемого — пусто у отсутствующего раздела."""
        out = {}
        for name in gwcfg.SECTIONS:
            sec = sections.get(name)
            if sec is None:
                out[name] = ""
            elif name == "env":
                out[name] = gwcfg.env_print(sec)
            elif name == "link":
                out[name] = gwcfg.link_print(sec.get("conf", ""))
            elif name == "uplink":
                out[name] = gwcfg.uplink_print(sec.get("conf", ""), sec.get("pubkey", ""), sec.get("prev_pubkey", ""))
            elif name == "mail":
                out[name] = gwcfg.mail_print(sec)
            elif name == "backup":
                out[name] = gwcfg.backup_print(sec.get("phrase", ""))
            elif name == "token":
                out[name] = gwcfg.token_print(sec.get("value", ""))
            elif name == "server":
                out[name] = gwcfg.server_print(sec.get("name", ""))
        return out

    # ── поколение и документ ─────────────────────────────────────────────────

    def gwcfg_doc(self, gw) -> dict:
        """Документ слота с поколением: {gen, hash, sections, prints}. Поколение
        растёт, когда отпечаток документа отличается от запомненного; само по
        себе оно ничего не решает — шлюз применяет по отпечаткам."""
        sections = self.gwcfg_desired(gw)
        prints = self.gwcfg_prints(sections)
        digest = gwcfg.doc_print(prints)
        gen_key = self._gwlink_key(self._GWCFG_GEN_KEY, gw.id)
        hash_key = self._gwlink_key(self._GWCFG_HASH_KEY, gw.id)
        gen = int(self.db.get_state(gen_key) or 0)
        if (self.db.get_state(hash_key) or "") != digest or not gen:
            gen += 1
            with self.db.transaction():
                self.db.set_state(gen_key, str(gen))
                self.db.set_state(hash_key, digest)
            log.info("документ конфигурации: слот %s — поколение %s", gw.id, gen)
        return {"gen": gen, "hash": digest, "sections": sections, "prints": prints}

    def gwcfg_ack(self, slot_id: int) -> dict:
        raw = self.db.get_state(self._gwlink_key(self._GWCFG_ACK_KEY, slot_id)) or ""
        if not raw.startswith("{"):
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def gwcfg_ack_in(self, slot_id: int, body: dict) -> dict:
        """Ответ шлюза на документ — одним JSON в state: что применено, что ждёт
        человека (раздел → отпечаток серверного значения), что отклонено, что
        не применилось и почему, временный ли отказ."""
        ack = gwcfg.ack_parse(body)
        old = self.gwcfg_ack(slot_id)
        if old and old.get("hash") == ack["hash"] and not ack["retry"]:
            # ответ человека по одному разделу (или повторный ответ на тот же документ)
            # дополняет сохранённый, а не стирает ответы по остальным разделам
            done = set(ack["applied"]) | set(ack["declined"]) | set(ack["failed"])
            ack["applied"] = [s for s in gwcfg.SECTIONS if s in set(old.get("applied") or []) | set(ack["applied"])
                              and s not in set(ack["declined"]) | set(ack["failed"])]
            ack["wait"] = {s: p for s, p in {**(old.get("wait") or {}), **ack["wait"]}.items() if s not in done}
            ack["declined"] = {s: p for s, p in {**(old.get("declined") or {}), **ack["declined"]}.items()
                               if s not in set(ack["applied"]) | set(ack["failed"])}
            ack["failed"] = {s: w for s, w in {**(old.get("failed") or {}), **ack["failed"]}.items()
                             if s not in set(ack["applied"]) | set(ack["declined"])}
        ack["at"] = timeutil.to_iso(timeutil.now())
        self.db.set_state(self._gwlink_key(self._GWCFG_ACK_KEY, slot_id), json.dumps(ack, ensure_ascii=False))
        if ack["failed"]:
            log.warning("документ конфигурации: слот %s не применил %s", slot_id,
                        "; ".join(f"{k}: {v}" for k, v in ack["failed"].items()))
        return ack

    def gwcfg_ack_take(self, slot_id: int, body: dict) -> tuple[dict, list[Notification]]:
        """cfg_ack от слушателя: ответ сохранён (gwcfg_ack_in) и уведомления о
        нём — Н5, когда в отказах впервые появился аплинк с этим отпечатком
        (повторный ответ с тем же отказом молчит). О каком отпечатке уже
        сказали — своим ключом: сохранённый ответ заменяется ответами с retry и
        ответами на другой документ, и по нему «впервые» не определить."""
        ack = self.gwcfg_ack_in(slot_id, body)
        notes = []
        told_key = self._gwlink_key("gwcfg_uplink_declined_told", slot_id)
        declined = ack["declined"].get("uplink")
        if declined and declined != (self.db.get_state(told_key) or ""):
            self.db.set_state(told_key, declined)
            gw = self.db.gateway(slot_id)
            if gw is not None:
                notes.append(Notification(config.ADMIN_ID,
                                          f"🔐 {self._gw_link(gw)}: новый конфиг аплинка отклонили в чате бота шлюза"))
        failed_token = (ack.get("failed") or {}).get("token")
        tok_key = self._gwlink_key("gwcfg_token_failed_told", slot_id)
        if failed_token and not ack.get("retry") and (self.db.get_state(tok_key) or "") != ack["hash"]:
            self.db.set_state(tok_key, ack["hash"])
            gw = self.db.gateway(slot_id)
            if gw is not None:
                notes.append(Notification(config.ADMIN_ID, f"⚠️ {self._gw_link(gw)}: новый токен бота не применён: "
                                                            f"{html.escape(str(failed_token), quote=False)}"))
        return ack, notes

    def gwcfg_due(self, gw) -> dict | None:
        """Что отправить слоту сейчас, или None. Решает расхождение отпечатков
        снимка с желаемым, не событие на ВПС; раздел, который шлюз в последнем
        ответе назвал wait или declined с тем же отпечатком, повторно не шлётся —
        у почты и фразы это и есть «оставить своё». Снимка с cfg нет — молчим."""
        snap = self.gwlink_snapshot(gw.id)
        if not isinstance(snap.get("cfg"), dict):
            return None
        doc = self.gwcfg_doc(gw)
        if not self.gwcfg_diff(gw, doc, snap["cfg"]):
            return None
        return gwcfg.build(doc["gen"], doc["hash"], doc["sections"])

    def gwcfg_diff(self, gw, doc: dict, have: dict) -> list[str]:
        """Разделы, в которых желаемое расходится с отпечатками снимка и которые
        шлюз не держит в wait/declined с тем же отпечатком."""
        ack = self.gwcfg_ack(gw.id)
        held = {**(ack.get("wait") or {}), **(ack.get("declined") or {})}
        # отказ на этот же документ (не временный) держится до смены отпечатка: повтор
        # того же документа дал бы тот же отказ, ещё один прогон обвязки и уведомление
        failed = set(ack.get("failed") or {}) if ack.get("hash") == doc["hash"] and not ack.get("retry") else set()
        if "*" in failed:
            return []
        out = []
        for name in gwcfg.SECTIONS:
            want = doc["prints"].get(name, "")
            if not want:
                continue                                  # у сервера раздела нет — своё на шлюзе не трогаем
            if want == str(have.get(name) or ""):
                continue
            if held.get(name) == want or name in failed:
                continue                                  # ждёт человека, отклонено или отвергнуто — тот же документ
            out.append(name)
        return out

    def gwcfg_card(self, gw, snap: dict) -> dict:
        """Что карточка слота знает о документе: агент принимает ли документ
        (cfg1), поколение на шлюзе, что расходится, что отказано, что ждёт
        человека, что отклонено, когда применён текущий документ, почему не
        собран конфиг линка. Без запросов к шлюзу — только state."""
        caps = self.gwlink_agent_caps(gw.id)
        cfg1 = "cfg1" in caps
        have = snap.get("cfg") if isinstance(snap.get("cfg"), dict) else {}
        out = {"cfg1": cfg1, "confirm1": "confirm1" in caps, "gen": int(have.get("gen") or 0) if have else 0,
               "due": [], "failed": {}, "wait": [], "declined": [], "applied_at": "", "link_err": "", "uplink_err": "", "uplink_fp": ""}
        if not cfg1 or not snap:
            return out
        doc = self.gwcfg_doc(gw)
        ack = self.gwcfg_ack(gw.id)
        # временный отказ («повтори») — не отказ: ни строки «не применил», ни даты применения
        current = bool(ack) and ack.get("hash") == doc["hash"] and not ack.get("retry")
        out["due"] = self.gwcfg_diff(gw, doc, have) if have else []
        out["failed"] = dict(ack.get("failed") or {}) if current else {}
        out["wait"] = [s for s, p in (ack.get("wait") or {}).items() if p == doc["prints"].get(s)] if current else []
        out["declined"] = [s for s, p in (ack.get("declined") or {}).items() if p == doc["prints"].get(s)]
        out["applied_at"] = str(ack.get("at") or "") if current and not out["failed"] else ""
        out["link_err"] = self.gwcfg_link_error(gw.id)
        out["uplink_err"] = self.db.get_state(self._gwlink_key(self._GWCFG_UPLINK_ERR_KEY, gw.id)) or ""
        if "uplink" in out["wait"] or "uplink" in out["declined"]:
            up = doc["sections"].get("uplink") or {}
            try:
                conf = wgconf.parse(up.get("conf", ""))
                out["uplink_fp"] = gwcfg.key_fp(wgconf.pubkey_of(conf.get("Interface", "PrivateKey")),
                                                conf.get("Peer", "PublicKey"))
            except wgconf.WgConfError:
                pass
        return out

    def gwcfg_all_capable(self) -> bool:
        """Все слоты с устройством принимают документ (cfg1): подсети соседей и
        конфиг линка доедут каналом, файл не нужен. Без слотов — ложь."""
        slots = [g for g in self.db.gateways() if g.device_id]
        return bool(slots) and all("cfg1" in self.gwlink_agent_caps(g.id) for g in slots)

    # ── порт канала: смена без обрыва ─────────────────────────────────────────
    _CHPORT_CUR_KEY = "gw_chport_cur"
    _CHPORT_PREV_KEY = "gw_chport_prev"            # «порт|когда»: прежний порт слушаем ещё неделю
    CHPORT_PREV_DAYS = 7

    def gwcfg_channel_port_tick(self, cur: int | None = None) -> None:
        """Заметить смену routing.link_channel_port: прежний порт остаётся в
        слушателях и файерволе, пока шлюзы не переедут на новый (документ
        везёт порт в env), но не дольше недели."""
        cur = int(gwlink.channel_port() if cur is None else cur)
        known = self.db.get_state(self._CHPORT_CUR_KEY) or ""
        if known == str(cur):
            return
        with self.db.transaction():
            if known.isdigit() and int(known) != cur:
                self.db.set_state(self._CHPORT_PREV_KEY, f"{known}|{timeutil.to_iso(timeutil.now())}")
                log.info("канал линка: порт сменился %s → %s, прежний слушаем ещё %s дн", known, cur, self.CHPORT_PREV_DAYS)
            self.db.set_state(self._CHPORT_CUR_KEY, str(cur))

    def gwcfg_channel_ports(self, cur: int | None = None) -> list[int]:
        """Порты канала для слушателя и файервола: текущий и прежний, пока тот
        не отслужил неделю."""
        cur = int(gwlink.channel_port() if cur is None else cur)
        raw = self.db.get_state(self._CHPORT_PREV_KEY) or ""
        port, _, since = raw.partition("|")
        if port.isdigit() and int(port) != cur and since:
            try:
                age = (timeutil.now() - timeutil.parse_iso(since)).total_seconds()
            except ValueError:
                age = None
            if age is not None and age < self.CHPORT_PREV_DAYS * 86400:
                return [cur, int(port)]
            self.db.set_state(self._CHPORT_PREV_KEY, "")
        return [cur]

    def gwcfg_forget(self, slot_id: int) -> None:
        for key in (self._GWCFG_GEN_KEY, self._GWCFG_HASH_KEY, self._GWCFG_ACK_KEY, self._GWCFG_LINK_ERR_KEY,
                    self._GWCFG_UPLINK_ERR_KEY, "gwcfg_uplink_declined_told", "gwcfg_token_failed_told"):
            self.db.set_state(self._gwlink_key(key, slot_id), "")
