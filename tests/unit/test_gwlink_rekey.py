"""
Смена ключей линка на стороне ВПС: domain/services/gateway_link/rekey.py
(стадия, ответы шлюза, переключение, фиксация по hello новым ключом, сроки,
возврат на старте) и runtime/linkserver/rekey.py (link_next, link_go,
переключение через пару секунд, link_cancel).

Переход ключей рвёт линк до шлюза — а с ним РФ-доступ у всех и канал, по
которому шлюз узнал бы, что делать дальше. Поэтому ошибки здесь дорогие:
переход без согласия человека, два перехода сразу (оба линка лежат),
переход, который не возвращается сам по сроку, ВПС, оставшийся после
перезапуска на ключах, которых у шлюза нет, повторный вопрос в чате агента
после разрыва сессии, уведомления, приходящие дважды или не приходящие.

Скрипт линка подменён моделью `_Vps` с теми же файловыми эффектами, что у
настоящего (его режимы проверяет test_link_script_rekey.py): живые конфиги
обеих сторон, `.next`, `.prev`. Ключи — настоящие X25519, рождаются в тесте:
по ним шлюз (test_link_txn_search_*.py) считает отпечаток и узнаёт, поднялся
ли бы линк. Часы ВПС — `timeutil.now` со сдвигом.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import types

import pytest
from nacl.public import PrivateKey

from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.domain.services.gateway_link import rekey as rekeymod
from awgbot.domain.services.types import ServiceError
from awgbot.runtime import linkserver
from awgbot.runtime.linkserver import rekey as lsrekey
from awgbot.util import gwlink, timeutil, wgconf

pytestmark = pytest.mark.unit

ADMIN = config.ADMIN_ID


def keypair() -> tuple[str, str]:
    """Настоящая пара X25519 (приватный, публичный) — на лету, не литералом."""
    k = PrivateKey.generate()
    return base64.b64encode(bytes(k)).decode(), base64.b64encode(bytes(k.public_key)).decode()


def vps_conf(priv: str, peer_pub: str, psk: str, port: int = 51821) -> str:
    return (f"# Линк ВПС ↔ шлюз\n[Interface]\nAddress = 10.99.99.1/30\nListenPort = {port}\nPrivateKey = {priv}\n"
            f"Table = off\nJc = 4\nS1 = 10\nH1 = 1\n\n[Peer]\nPublicKey = {peer_pub}\nPresharedKey = {psk}\n"
            "AllowedIPs = 0.0.0.0/0\n")


def gw_conf(priv: str, peer_pub: str, psk: str, endpoint: str = "vps.example.org:51821") -> str:
    """Конфиг шлюзовой стороны — тот же вид, что ставит документ (test_wgconf_allowed.LINK)."""
    return (f"# awg-bot: контракт линка 1\n[Interface]\nAddress = 10.99.99.2/30\nPrivateKey = {priv}\n"
            "Jc = 4\nJmin = 40\nJmax = 70\nS1 = 10\nS2 = 20\nH1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\n\n"
            f"[Peer]\nPublicKey = {peer_pub}\nPresharedKey = {psk}\nEndpoint = {endpoint}\n"
            "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30\nPersistentKeepalive = 25-35\n")


class _Vps:
    """ВПС для перехода: живой конфиг линка (config.AWG_DIR/<линк>.conf),
    конфиг шлюзовой стороны (/root/gw-<линк>.conf — чтение в модуле rekey
    перенаправлено во временный каталог) и скрипт линка моделью тех же
    файловых эффектов. `fail` — {режим: причина}: режим отказывает, ничего не
    меняя (настоящий скрипт при отказе коммита сам возвращает прежние).
    `runs` — (режим, окружение) по порядку."""

    def __init__(self, services, tmp_path, monkeypatch):
        self.awg = tmp_path / "awg"; self.awg.mkdir()
        self.root = tmp_path / "root"; self.root.mkdir()
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        real_open = open

        def _open(path, *a, **kw):
            p = str(path)
            if p.startswith("/root/"):
                p = str(self.root / p[len("/root/"):])
            return real_open(p, *a, **kw)
        monkeypatch.setattr(rekeymod, "open", _open, raising=False)
        self.vps_priv, self.vps_pub = keypair()
        self.gw_priv, self.gw_pub = keypair()
        self.live = self.awg / "awglink.conf"
        self.gw = self.root / "gw-awglink.conf"
        self.live.write_text(vps_conf(self.vps_priv, self.gw_pub, "DUMMY-LINK-PSK="), encoding="utf-8")
        self.gw.write_text(gw_conf(self.gw_priv, self.vps_pub, "DUMMY-LINK-PSK="), encoding="utf-8")
        self.fail: dict[str, str] = {}
        self.runs: list[tuple[str, dict]] = []
        self.new: dict = {}                    # ключи последней стадии
        self._n = 0
        monkeypatch.setattr(services, "_run_link_script", self.run)
        # ключ канала слота сервер выводит из /root/gw-<линк>.conf — из файла модели
        from awgbot.util import bundlecrypt
        monkeypatch.setattr(services, "_link_privkey",
                            lambda gw=None: bundlecrypt.read_privkey(self.gw.read_text(encoding="utf-8")))

    @staticmethod
    def _p(path, suffix):
        return path.with_name(path.name + suffix)

    def run(self, mode: str, env: dict | None = None) -> None:
        self.runs.append((mode, dict(env or {})))
        if mode in self.fail:
            raise ServiceError(f"скрипт линка ({mode}) не отработал: {self.fail[mode]}")
        live, gw = self.live, self.gw
        if mode == "--rekey-stage":
            self._n += 1
            vp, vpub = keypair()
            gp, gpub = keypair()
            psk = f"DUMMY-NEW-PSK-{self._n}="
            self.new = {"vps_priv": vp, "vps_pub": vpub, "gw_priv": gp, "gw_pub": gpub}
            endpoint = wgconf.parse(gw.read_text(encoding="utf-8")).get("Peer", "Endpoint")
            self._p(live, ".next").write_text(vps_conf(vp, gpub, psk), encoding="utf-8")
            self._p(gw, ".next").write_text(gw_conf(gp, vpub, psk, endpoint), encoding="utf-8")
        elif mode == "--rekey-commit":
            for path in (live, gw):
                self._p(path, ".prev").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                path.write_text(self._p(path, ".next").read_text(encoding="utf-8"), encoding="utf-8")
                self._p(path, ".next").unlink()
        elif mode == "--rekey-revert":
            for path in (live, gw):
                self._p(path, ".next").unlink(missing_ok=True)
                if self._p(path, ".prev").exists():
                    path.write_text(self._p(path, ".prev").read_text(encoding="utf-8"), encoding="utf-8")
                    self._p(path, ".prev").unlink()
        elif mode == "--rekey-drop":
            for path in (live, gw):
                self._p(path, ".next").unlink(missing_ok=True)
                self._p(path, ".prev").unlink(missing_ok=True)

    def session_key(self) -> bytes:
        """Ключ сессии канала, которым шлюз подпишет hello на ключах из файла сейчас."""
        return gwlink.channel_key(wgconf.parse(self.gw.read_text(encoding="utf-8")).get("Interface", "PrivateKey"))

    @property
    def modes(self) -> list[str]:
        return [m for m, _ in self.runs]

    def extra_files(self) -> set[str]:
        return {f.name for d in (self.awg, self.root) for f in d.iterdir()} - {"awglink.conf", "gw-awglink.conf"}

    def peer_pub(self) -> str:
        """Какой ключ шлюза ВПС сейчас принимает."""
        return wgconf.parse(self.live.read_text(encoding="utf-8")).get("Peer", "PublicKey")

    def live_pub(self) -> str:
        """Публичный ключ живой стороны ВПС."""
        return wgconf.pubkey_of(wgconf.parse(self.live.read_text(encoding="utf-8")).get("Interface", "PrivateKey"))


class _Clock:
    """Часы ВПС: сдвиг в секундах от момента создания."""

    def __init__(self, monkeypatch):
        self.base = timeutil.now()
        self.shift = 0.0
        monkeypatch.setattr(timeutil, "now", lambda: self.base + datetime.timedelta(seconds=self.shift))

    def at(self, seconds: float) -> None:
        self.shift = float(seconds)


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Слот 1 с агентом 3.3.0 (cfg1, confirm1) на связи; политика слота — со счётчиком."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 51821, "10.99.99.0/30", slot_id=1)
    services.gwlink_session_opened(1, "3.3.0", gwlink.PROTO, caps=("cfg1", "confirm1"))
    services.policy_calls = 0
    real = services._ensure_gateway_policy

    def policy():
        services.policy_calls += 1
        return real()
    monkeypatch.setattr(services, "_ensure_gateway_policy", policy)
    return services.db.gateway(1)


@pytest.fixture()
def vps(services, slot, tmp_path, monkeypatch):
    return _Vps(services, tmp_path, monkeypatch)


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


def _second_slot(services):
    admin = services.db.get_client_by_tg(ADMIN)
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.gwlink_session_opened(2, "3.3.0", gwlink.PROTO, caps=("cfg1", "confirm1"))


def _texts(notes) -> list[str]:
    return [n.text for n in notes]


def _asked(services, vps) -> dict:
    st = services.gateway_rekey_stage(1)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "asked"})
    return st


def _switched(services, vps) -> dict:
    """Переход дошёл до переключения ВПС: стадия, «спросил», «готов», link_go, коммит."""
    st = _asked(services, vps)
    action, _ = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    assert action == "go"
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_commit(1) == (True, [])
    return st


# ── стадия ───────────────────────────────────────────────────────────────────

def test_stage_needs_an_agent_not_below_330(services, slot, vps):
    """Агент ниже 3.3.0 (вернулся после молчания, документа не принимает) не
    знает link_next: новые ключи он не примет, а ВПС переключился бы — линк
    мёртв. Отказ до скрипта, состояния нет."""
    services.gwlink_session_opened(1, "3.2.1", gwlink.PROTO, caps=())
    with pytest.raises(ServiceError, match="3.3.0"):
        services.gateway_rekey_stage(1)
    assert vps.runs == [] and services.gateway_rekey(1) == {}
    assert services.gateway_rekey_active(1) is False


def test_stage_prepares_new_keys_and_remembers_the_transition(services, slot, vps):
    """«🔑 Сменить»: скрипт линка в режиме стадии с окружением слота (имя,
    порт, /30, keepalive клиентов); живые конфиги не тронуты; состояние
    staged с id и отпечатком пары новых ключей."""
    live_before = vps.live.read_text(encoding="utf-8")
    st = services.gateway_rekey_stage(1)
    assert vps.modes == ["--rekey-stage"]
    env = vps.runs[0][1]
    assert (env["LINK_IF"], env["LINK_PORT"], env["LINK_CIDR"]) == ("awglink", "51821", "10.99.99.0/30"), env
    assert env.get("LINK_KEEPALIVE"), "keepalive линка не передан — стадия взяла бы прибитые 25 с"
    assert vps.live.read_text(encoding="utf-8") == live_before
    assert st["state"] == "staged" and st["id"] and st["at"]
    assert st["fp"] == gwcfg.key_fp(vps.new["gw_pub"], vps.new["vps_pub"]), "отпечаток не той пары ключей"
    assert services.gateway_rekey(1) == st and services.gateway_rekey_active(1) is True


def test_link_next_carries_the_gateway_side_with_the_stamp_and_the_same_fingerprint(services, slot, vps):
    """Тело link_next: конфиг шлюзовой стороны новыми ключами — штамп
    контракта первой строкой и никаких пояснений из файла; отпечаток тот же,
    что в состоянии: человек сверяет «ab12 cd34» в карточке ВПС и в чате
    агента глазами."""
    st = services.gateway_rekey_stage(1)
    body = services.gateway_rekey_next_body(1)
    assert body["id"] == st["id"] and body["fp"] == st["fp"]
    assert body["conf"].startswith("# awg-bot: контракт линка 1\n")
    assert body["conf"].count("#") == 1, "в конфиг для шлюза ушли комментарии источника"
    conf = wgconf.parse(body["conf"])
    assert conf.get("Interface", "PrivateKey") == vps.new["gw_priv"]
    assert conf.get("Peer", "PublicKey") == vps.new["vps_pub"]
    # шлюз считает отпечаток по своему приватному и ключу ВПС — должно совпасть
    assert gwcfg.key_fp(wgconf.pubkey_of(conf.get("Interface", "PrivateKey")), conf.get("Peer", "PublicKey")) == st["fp"]


def test_one_transition_at_a_time_across_slots(services, slot, vps):
    """Пока у одного слота идёт переход, второй не начинается — один заход
    не кладёт оба линка; и тот же слот второй раз — тоже отказ."""
    _second_slot(services)
    services.gateway_rekey_stage(1)
    with pytest.raises(ServiceError, match="уже идёт"):
        services.gateway_rekey_stage(1)
    with pytest.raises(ServiceError, match="Идёт смена ключей у"):
        services.gateway_rekey_stage(2)
    assert vps.modes == ["--rekey-stage"], "скрипт звали для отказанного перехода"
    assert services.gateway_rekey(2) == {}


def test_a_failing_stage_leaves_no_transition(services, slot, vps):
    """Скрипт отказал (нет awg, диск) — ошибка с причиной, состояния нет:
    следующая попытка начнётся с нуля."""
    vps.fail["--rekey-stage"] = "awg: команда не найдена"
    with pytest.raises(ServiceError, match="Новые ключи не сгенерированы"):
        services.gateway_rekey_stage(1)
    assert services.gateway_rekey(1) == {} and not services.gateway_rekey_active(1)


def test_unreadable_staged_keys_are_dropped(services, slot, vps, monkeypatch):
    """Стадия прошла, но `.next` не читается — новые ключи убираются
    (drop), состояния нет: висящий `.next` с приватным ключом ни к чему."""
    real = vps.run

    def run(mode, env=None):
        real(mode, env)
        if mode == "--rekey-stage":
            vps._p(vps.gw, ".next").write_text("мусор\n", encoding="utf-8")
    monkeypatch.setattr(services, "_run_link_script", run)
    with pytest.raises(ServiceError, match="не прочитаны"):
        services.gateway_rekey_stage(1)
    assert vps.modes == ["--rekey-stage", "--rekey-drop"] and vps.extra_files() == set()
    assert services.gateway_rekey(1) == {}


def test_link_next_body_exists_only_while_the_gateway_has_to_answer(services, slot, vps):
    """link_next уходит в staged и asked; после переключения или без перехода
    — нечего слать."""
    assert services.gateway_rekey_next_body(1) is None
    st = _asked(services, vps)
    assert services.gateway_rekey_next_body(1)["id"] == st["id"]
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_next_body(1) is None


# ── ответы шлюза ─────────────────────────────────────────────────────────────

def test_asked_moves_the_transition_and_tells_nobody(services, slot, vps):
    st = services.gateway_rekey_stage(1)
    action, notes = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "asked"})
    assert (action, notes) == ("", [])
    assert services.gateway_rekey(1)["state"] == "asked"


def test_an_answer_about_another_transition_is_ignored(services, slot, vps):
    """Ответ с чужим id (запоздавший от прошлого перехода) не трогает текущий."""
    st = services.gateway_rekey_stage(1)
    assert services.gateway_rekey_ack_in(1, {"id": "abc123", "state": "declined"}) == ("", [])
    assert services.gateway_rekey(1) == st and vps.modes == ["--rekey-stage"]


def test_ready_asks_for_link_go_once(services, slot, vps):
    """«Готов» — сигнал переключаться; повторный «готов» после link_go (сервер
    повторил link_next, шлюз ответил своим состоянием) второго link_go не даёт."""
    st = _asked(services, vps)
    assert services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})[0] == "go"
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})[0] == "", "второй link_go"


def test_declined_in_the_chat_drops_the_new_keys_and_tells_the_admin(services, slot, vps):
    """«Отмена» в чате агента: новые ключи убраны, перехода нет, админу —
    одно уведомление без звука о том, что отклонили."""
    st = _asked(services, vps)
    action, notes = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"})
    assert action == "" and vps.modes[-1] == "--rekey-drop" and vps.extra_files() == set()
    left = services.gateway_rekey(1)
    assert left["state"] == "declined" and left["error"] == "" and left["at"] and "id" not in left, left
    assert services.gateway_rekey_active(1) is False, "отказ держит слот «в переходе»"
    assert len(notes) == 1 and notes[0].tg_id == ADMIN and not notes[0].critical
    assert "смену ключей линка отклонили в чате бота шлюза" in notes[0].text, notes[0].text
    assert services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"}) == ("", []), "второе уведомление"


def test_a_gateway_refusal_names_the_reason_escaped(services, slot, vps):
    """Шлюз не принял новые ключи (проверка) — причина в уведомлении; текст
    шлюза экранируется: `<` в нём иначе уронил бы отправку целиком."""
    st = services.gateway_rekey_stage(1)
    _, notes = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined",
                                                 "error": "Endpoint другой <b>x</b>"})
    assert "шлюз не принял новые ключи линка: Endpoint другой &lt;b&gt;x&lt;/b&gt;" in notes[0].text, notes[0].text
    assert services.gateway_rekey(1)["error"] == "Endpoint другой <b>x</b>", "причина не сохранена для карточки"
    assert vps.extra_files() == set()


# ── переключение и фиксация ──────────────────────────────────────────────────

def test_commit_switches_the_vps_and_relays_the_slot_policy(services, slot, vps):
    """Через секунды после link_go: ВПС на новых ключах, прежние — в `.prev`,
    политика слота заново (исключение из MASQUERADE, маршруты); состояние —
    переключено, ждём hello новым ключом. Повтор коммита ничего не делает."""
    _switched(services, vps)
    assert vps.modes == ["--rekey-stage", "--rekey-commit"]
    assert vps.peer_pub() == vps.new["gw_pub"], "ВПС не на новых ключах"
    st = services.gateway_rekey(1)
    assert st["state"] == "switched" and st["committed"] is True and st["go_at"]
    assert services.policy_calls == 1, "политика слота после смены ключей не доведена"
    assert services.gateway_rekey_commit(1) == (False, []) and vps.modes.count("--rekey-commit") == 1


def test_a_failed_commit_keeps_the_old_keys_and_says_so(services, slot, vps):
    """Скрипт не поднял линк на новых ключах (сам вернул прежние): перехода
    больше нет, админу — «вернул прежние»; шлюз, если переключился,
    вернётся сам по своему сроку и найдёт прежние поиском."""
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    services.gateway_rekey_go(1)
    vps.fail["--rekey-commit"] = "awg-quick up: ошибка"
    switched, notes = services.gateway_rekey_commit(1)
    assert switched is False, "отказ коммита назван переключением — слушатель порвал бы сессию зря"
    assert _texts(notes) and "новые ключи линка не поднялись — вернул прежние" in notes[0].text, _texts(notes)
    assert vps.modes[-2:] == ["--rekey-commit", "--rekey-revert"], vps.modes
    assert vps.extra_files() == set(), "новые ключи остались в .next после отказа коммита"
    assert services.policy_calls == 1, "после возврата политика слота не доведена"
    assert services.gateway_rekey(1) == {} and vps.peer_pub() == vps.gw_pub


def test_hello_with_the_new_key_fixes_the_transition_once(services, slot, vps):
    """hello слота после коммита — это hello новым ключом: прежние ключи
    убираются (drop), админу — «ключи линка сменены» один раз."""
    _switched(services, vps)
    res = services.gateway_rekey_hello(1)
    assert res["hold"] is False and res["rekeyed"] is True, res
    assert len(res["notes"]) == 1 and "ключи линка сменены" in res["notes"][0].text, _texts(res["notes"])
    assert vps.modes[-1] == "--rekey-drop" and vps.extra_files() == set(), "прежние ключи остались на диске"
    assert services.gateway_rekey(1) == {} and vps.peer_pub() == vps.new["gw_pub"]
    assert services.gateway_rekey_hello(1) == {"notes": [], "hold": False, "rekeyed": False}, (
        "второе «сменены» на следующем hello")


def test_hello_before_the_vps_switched_is_not_a_proof(services, slot, vps):
    """hello между link_go и коммитом (2 с) пришёл ещё прежним ключом — он
    ничего не доказывает, а сессия на прежнем ключе пережила бы переключение
    ВПС: её закрывают молча (hold), шлюз придёт снова. В staged/asked hello —
    обычное подключение."""
    st = _asked(services, vps)
    assert services.gateway_rekey_hello(1) == {"notes": [], "hold": False, "rekeyed": False}
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    services.gateway_rekey_go(1)
    assert services.gateway_rekey_hello(1) == {"notes": [], "hold": True, "rekeyed": False}
    assert services.gateway_rekey(1)["state"] == "switched" and "--rekey-drop" not in vps.modes


# ── сроки и старт ────────────────────────────────────────────────────────────

def test_a_day_without_a_decision_cancels_the_transition(services, slot, vps, clock):
    """Сутки без решения человека: новые ключи убраны, шлюзу — link_cancel с
    id перехода, админу — одно уведомление; до суток — тишина."""
    st = _asked(services, vps)
    clock.at(24 * 3600 - 60)
    assert services.gateway_rekey_tick() == ([], [])
    clock.at(24 * 3600 + 60)
    cancel, notes = services.gateway_rekey_tick()
    assert cancel == [(1, st["id"])]
    assert len(notes) == 1 and "отменена — за сутки её не подтвердили" in notes[0].text, _texts(notes)
    assert vps.modes[-1] == "--rekey-drop" and vps.extra_files() == set()
    assert services.gateway_rekey(1) == {}
    assert services.gateway_rekey_tick() == ([], []), "отмена повторилась на следующем такте"


def test_no_hello_within_three_minutes_after_link_go_reverts(services, slot, vps, clock):
    """180 с после link_go без hello новым ключом — прежние ключи на ВПС
    (шлюз найдёт их поиском), политика слота заново, админу — одно
    уведомление; до срока — ждём."""
    _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS - 5)
    assert services.gateway_rekey_tick() == ([], [])
    assert services.gateway_rekey(1)["state"] == "switched"
    clock.at(rekeymod.SWITCH_SECONDS + 5)
    cancel, notes = services.gateway_rekey_tick()
    assert cancel == [] and len(notes) == 1
    assert "не поднялись за 3 мин — вернул прежние" in notes[0].text, _texts(notes)
    assert vps.modes[-1] == "--rekey-revert" and vps.peer_pub() == vps.gw_pub, "ВПС не на прежних ключах"
    assert vps.extra_files() == set() and services.gateway_rekey(1) == {}
    assert services.policy_calls == 2, "после возврата политика слота не доведена"
    assert services.gateway_rekey_tick() == ([], [])


def test_restart_in_the_middle_of_the_switch_puts_the_old_keys_back(services, slot, vps):
    """Бот перезапустился в switched: что стало со шлюзом, сервер не знает —
    прежние ключи на место до слушателей, админу — уведомление."""
    _switched(services, vps)
    notes = services.gateway_rekey_startup()
    assert len(notes) == 1 and "перезапуск бота посреди смены ключей линка" in notes[0].text, _texts(notes)
    assert vps.modes[-1] == "--rekey-revert" and vps.peer_pub() == vps.gw_pub
    assert services.gateway_rekey(1) == {}
    assert services.gateway_rekey_startup() == []


def test_restart_before_the_switch_keeps_the_question_open(services, slot, vps):
    """В asked перезапуск ничего не ломает: линк на прежних ключах, вопрос
    у человека висит — переход продолжается после подключения."""
    st = _asked(services, vps)
    assert services.gateway_rekey_startup() == []
    assert services.gateway_rekey(1)["state"] == "asked" and services.gateway_rekey(1)["id"] == st["id"]
    assert "--rekey-revert" not in vps.modes


def test_a_new_device_in_the_slot_forgets_the_transition(services, slot, vps):
    """Слот забыт (новое устройство, снятие) — переход тоже: иначе новый слот
    с тем же номером унаследовал бы «идёт смена ключей»."""
    _asked(services, vps)
    services._slot_forget(1)
    assert services.gateway_rekey(1) == {} and not services.gateway_rekey_active(1)


# ── отказ, переключение и возврат: новые правила ─────────────────────────────

def test_a_refusal_stays_on_the_card_for_a_day_and_does_not_block_a_new_try(services, slot, vps, clock):
    """Отказ шлюза — строка карточки на сутки (состояние declined), но не
    «переход идёт»: новую смену можно начать сразу; через сутки строка
    снимается тактом молча — без link_cancel и уведомлений."""
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined", "error": "Endpoint другой"})
    clock.at(rekeymod.DECLINED_HOURS * 3600 - 60)
    assert services.gateway_rekey_tick() == ([], []) and services.gateway_rekey(1)["state"] == "declined"
    clock.at(rekeymod.DECLINED_HOURS * 3600 + 60)
    assert services.gateway_rekey_tick() == ([], []), "снятие строки отказа объявлено или отменено у шлюза"
    assert services.gateway_rekey(1) == {}
    assert vps.modes.count("--rekey-drop") == 1, "строку отказа снимали скриптом"


def test_a_new_change_can_start_right_after_a_refusal(services, slot, vps):
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"})
    again = services.gateway_rekey_stage(1)
    assert again["state"] == "staged" and again["id"] != st["id"], again


def test_link_go_only_follows_a_question_in_progress(services, slot, vps):
    """link_go фиксируется только из staged/asked: после отказа или без
    перехода «переключено» не появляется — иначе срок 180 с вернул бы
    ключи, которых никто не менял."""
    services.gateway_rekey_go(1)
    assert services.gateway_rekey(1) == {}
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"})
    services.gateway_rekey_go(1)
    assert services.gateway_rekey(1)["state"] == "declined"
    assert services.gateway_rekey_commit(1) == (False, []) and "--rekey-commit" not in vps.modes


def test_a_gateway_that_could_not_raise_new_keys_after_the_switch_brings_the_vps_back_at_once(
        services, slot, vps):
    """ВПС уже переключился, а шлюз ответил declined (link_apply не встал) —
    прежние ключи на ВПС сразу, не через 3 минуты; политика слота заново;
    админу — одно «шлюз не поднял новые ключи линка: причина — вернул прежние»."""
    st = _switched(services, vps)
    calls = services.policy_calls
    action, notes = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined",
                                                      "error": "awg-quick up <x>"})
    assert action == "" and vps.modes[-1] == "--rekey-revert" and vps.peer_pub() == vps.gw_pub, vps.modes
    assert vps.extra_files() == set()
    assert services.policy_calls == calls + 1, "после возврата политика слота не доведена"
    assert _texts(notes) == [f"⚠️ {services._gw_link(slot)}: шлюз не поднял новые ключи линка: "
                             "awg-quick up &lt;x&gt; — вернул прежние"], _texts(notes)
    assert services.gateway_rekey(1)["state"] == "declined"
    assert services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"}) == ("", []), "второе уведомление"


def test_a_refusal_between_link_go_and_the_switch_only_drops_the_new_keys(services, slot, vps):
    """Шлюз отказал раньше, чем ВПС переключился (коммит не прошёл): ВПС на
    прежних — возвращать нечего, новые ключи прочь; коммит после этого не
    переключит ВПС."""
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    services.gateway_rekey_go(1)
    _, notes = services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "declined"})
    assert vps.modes[-1] == "--rekey-drop" and "--rekey-revert" not in vps.modes, vps.modes
    assert services.policy_calls == 0 and vps.extra_files() == set()
    assert _texts(notes) == [f"⚠️ {services._gw_link(slot)}: шлюз не поднял новые ключи линка — вернул прежние"]
    assert services.gateway_rekey_commit(1) == (False, []) and vps.peer_pub() == vps.gw_pub


def test_the_startup_return_runs_once_per_process(services, slot, vps):
    """Возврат на старте зовут и старт слушателя, и первый такт живости —
    кто раньше. Второй вызов не возвращает переход, начатый уже после старта."""
    assert services.gateway_rekey_startup() == []
    _switched(services, vps)
    assert services.gateway_rekey_startup() == [], "возврат на старте сработал второй раз за процесс"
    assert services.gateway_rekey(1)["state"] == "switched" and "--rekey-revert" not in vps.modes


def test_replacing_the_slot_keys_another_way_aborts_the_transition(services, slot, vps, clock):
    """Ключи слота меняет замена устройства или файл (--rekey): переход
    каналом снимается вместе с .next — иначе возврат по сроку поставил бы
    прежние ключи поверх нового устройства. Без перехода — скрипт не зовут."""
    services.gateway_rekey_abort(slot)
    assert vps.runs == []
    _switched(services, vps)
    services.gateway_rekey_abort(slot)
    assert vps.modes[-1] == "--rekey-drop" and services.gateway_rekey(1) == {}
    clock.at(rekeymod.SWITCH_SECONDS + 60)
    assert services.gateway_rekey_tick() == ([], []) and "--rekey-revert" not in vps.modes


def test_removing_the_slot_removes_the_staged_keys(services, slot, vps, monkeypatch):
    """Слот снят посреди перехода — .next с приватными ключами не остаются на ВПС."""
    monkeypatch.setattr(services, "_ensure_gateway_policy", lambda: None)
    services.gateway_rekey_stage(1)
    assert vps.extra_files(), "модель: стадия не положила .next"
    services.gateway_remove(1)
    assert "--rekey-drop" in vps.modes and vps.extra_files() == set(), vps.extra_files()


def test_the_link_script_failure_reason_is_its_error_line(services, monkeypatch):
    """Причина отказа скрипта линка — его строка «ОШИБКА…» из stdout, а не
    хвост stderr с шумом команд: она уходит в уведомление и карточку."""
    import subprocess
    out = "── Стадия\n  ОШИБКА: порт 51821 занят\n  подробности ниже\n".encode()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, stdout=out, stderr=b"awg: noise\n"))
    with pytest.raises(ServiceError) as e:
        services._run_link_script("--rekey-stage", {})
    assert str(e.value).endswith("ОШИБКА: порт 51821 занят"), str(e.value)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a, 1, stdout=b"--- \n", stderr=b"awg: command not found\n"))
    with pytest.raises(ServiceError, match="awg: command not found"):
        services._run_link_script("--rekey-stage", {})


# ── слушатель канала ─────────────────────────────────────────────────────────

class _Wire:
    """Сессия слота на слушателе без сокета: `send` записывает, что ушло
    шлюзу. Уведомления админу — через крючок пакета."""

    def __init__(self, services, monkeypatch, caps=("cfg1", "confirm1")):
        self.server = linkserver.LinkServer(services)
        self.sent: list[tuple[str, dict]] = []
        self.notes: list[str] = []
        self.open(caps)

        async def send(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
            self.sent.append((kind, dict(body or {})))
            return True
        self.server.send = send

        async def on_notes(notes):
            self.notes += [n.text for n in notes]
        monkeypatch.setattr(linkserver, "_on_notes", on_notes)
        monkeypatch.setattr(lsrekey, "COMMIT_DELAY", 0)

    def open(self, caps=("cfg1", "confirm1")) -> None:
        """Новая сессия слота 1 (после hello)."""
        self.server._sessions[1] = types.SimpleNamespace(hello=True, caps=list(caps), rekey_sent="")

    def kinds(self) -> list[str]:
        return [k for k, _ in self.sent]


async def _settle(cond, timeout: float = 3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def test_link_next_goes_once_per_session(services, slot, vps, monkeypatch):
    """link_next в сессии — один раз: доставка зовётся на каждом подключении и
    такте, и без памяти шлюз получал бы его снова и снова. Новая сессия
    (разрыв между asked и ready) — снова тот же id: шлюз узнает его и не
    спросит второй раз."""
    wire = _Wire(services, monkeypatch)
    st = services.gateway_rekey_stage(1)
    assert await wire.server.deliver_rekey(1) is True
    assert await wire.server.deliver_rekey(1) is False
    assert wire.kinds() == ["link_next"] and wire.sent[0][1]["id"] == st["id"]
    wire.open()
    await wire.server.deliver_rekey(1)
    assert wire.kinds() == ["link_next", "link_next"] and wire.sent[1][1]["id"] == st["id"]


def test_a_slot_with_an_agent_below_330_is_not_offered_the_rekey(services, slot):
    services.gwlink_session_opened(1, "3.2.1", gwlink.PROTO, caps=())
    assert services.gateway_rekey_slots() == []
    services.gwlink_session_opened(1, "3.3.0", gwlink.PROTO, caps=("cfg1", "confirm1", "token1"))
    assert [s for s, _n in services.gateway_rekey_slots()] == [1]


async def test_ready_sends_link_go_and_switches_the_vps_after_the_delay(services, slot, vps, monkeypatch):
    """«Готов» от шлюза: link_go с id перехода, затем (через COMMIT_DELAY,
    здесь 0) — переключение ВПС; уведомлений нет, пока hello не доказал."""
    wire = _Wire(services, monkeypatch)
    st = _asked(services, vps)
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "ready"})
    assert wire.sent == [("link_go", {"id": st["id"]})]
    assert await _settle(lambda: services.gateway_rekey(1).get("committed")), "ВПС не переключился"
    assert vps.peer_pub() == vps.new["gw_pub"] and wire.notes == []
    await wire.server.rekey_hello(1)
    assert len(wire.notes) == 1 and "ключи линка сменены" in wire.notes[0], wire.notes


async def test_declined_reaches_the_admin_without_link_go(services, slot, vps, monkeypatch):
    wire = _Wire(services, monkeypatch)
    st = _asked(services, vps)
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "declined"})
    assert wire.sent == [] and len(wire.notes) == 1 and "отклонили" in wire.notes[0]
    assert vps.peer_pub() == vps.gw_pub


async def test_the_liveness_tick_cancels_an_expired_question_on_the_gateway(services, slot, vps, monkeypatch, clock):
    """Сутки вышли — шлюзу link_cancel (снять приглашение в его чате),
    админу — уведомление крючком."""
    wire = _Wire(services, monkeypatch)
    st = _asked(services, vps)
    clock.at(24 * 3600 + 60)
    await wire.server.rekey_tick()
    assert wire.sent == [("link_cancel", {"id": st["id"]})]
    assert len(wire.notes) == 1 and "за сутки" in wire.notes[0]
    await wire.server.rekey_tick()
    assert len(wire.sent) == 1 and len(wire.notes) == 1, "отмена ушла дважды"


async def test_link_go_that_did_not_leave_changes_nothing(services, slot, vps, monkeypatch):
    """link_go не ушёл в сокет (сессия умерла) — ВПС не переключается и
    состояние не трогается; память «link_next уже ушёл» сброшена: новая
    сессия повторит link_next с тем же id, шлюз ответит ready."""
    wire = _Wire(services, monkeypatch)
    st = _asked(services, vps)
    wire.server._sessions[1].rekey_sent = st["id"]

    async def dead(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
        wire.sent.append((kind, dict(body or {})))
        return False
    wire.server.send = dead
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "ready"})
    await asyncio.sleep(0.05)
    assert services.gateway_rekey(1)["state"] == "asked", "переход сдвинулся без ушедшего link_go"
    assert "--rekey-commit" not in vps.modes and vps.peer_pub() == vps.gw_pub
    assert wire.server._sessions[1].rekey_sent == "", "link_next не повторится в новой сессии"


async def test_a_switched_vps_drops_the_session_so_the_gateway_comes_with_a_new_hello(services, slot, vps,
                                                                                       monkeypatch):
    """ВПС переключился — слушатель рвёт сессию слота сам (с записью
    закрытия): TCP по линку пережил бы переподъём, и hello новым ключом не
    пришло бы никогда."""
    wire = _Wire(services, monkeypatch)
    dropped: list[tuple[int, bool]] = []

    async def drop(slot_id, *, record=False):
        dropped.append((slot_id, record))
    wire.server._drop = drop
    st = _asked(services, vps)
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "ready"})
    assert await _settle(lambda: dropped), "сессию после переключения не порвали"
    assert dropped == [(1, True)] and services.gateway_rekey(1)["committed"] is True


async def test_a_failed_switch_keeps_the_session(services, slot, vps, monkeypatch):
    wire = _Wire(services, monkeypatch)
    dropped = []

    async def drop(slot_id, *, record=False):
        dropped.append(slot_id)
    wire.server._drop = drop
    vps.fail["--rekey-commit"] = "awg-quick up: ошибка"
    st = _asked(services, vps)
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "ready"})
    assert await _settle(lambda: wire.notes), "о неудаче переключения не сказали"
    assert dropped == [] and "вернул прежние" in wire.notes[0]


async def test_the_rekeyed_hook_fires_once_when_hello_fixes_the_transition(services, slot, vps, monkeypatch):
    """После фиксации main поднимает зависящее от ключей (крючок пакета) —
    один раз; hello вне перехода крючок не дёргает."""
    wire = _Wire(services, monkeypatch)
    seen: list[int] = []

    async def on_rekeyed(slot_id):
        seen.append(slot_id)
    monkeypatch.setattr(linkserver, "_on_rekeyed", on_rekeyed)
    assert await wire.server.rekey_hello(1) is True and seen == []
    _switched(services, vps)
    assert await wire.server.rekey_hello(1) is True
    assert await wire.server.rekey_hello(1) is True
    assert seen == [1], seen


async def test_the_startup_return_is_reported_once_whoever_calls_first(services, slot, vps, monkeypatch):
    """rekey_boot зовут старт слушателя и такт живости: возврат и уведомление — один раз."""
    _switched(services, vps)
    wire = _Wire(services, monkeypatch)
    await wire.server.rekey_boot()
    await wire.server.rekey_tick()
    await wire.server.rekey_boot()
    assert vps.modes.count("--rekey-revert") == 1, vps.modes
    assert len(wire.notes) == 1 and "перезапуск бота посреди смены ключей линка" in wire.notes[0], wire.notes


def _hello_msg() -> dict:
    return {"t": "hello", "proto": gwlink.PROTO, "agent": "3.3.0", "caps": ["cfg1", "confirm1"],
            "lists_hash": "", "svc_hash": ""}


async def test_hello_on_the_old_key_mid_switch_is_closed_silently(services, slot, vps, monkeypatch):
    """hello между link_go и переключением ВПС: сессию закрывают молча (с
    записью закрытия), ни одного слова сервера — первое слово закрепило бы на
    шлюзе ключи, которых ВПС ещё не поставил."""
    from awgbot.runtime.linkserver.common import _Session
    wire = _Wire(services, monkeypatch)
    dropped = []

    async def drop(slot_id, *, record=False):
        dropped.append((slot_id, record))
    wire.server._drop = drop
    st = _asked(services, vps)
    services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
    services.gateway_rekey_go(1)
    sess = wire.server._sessions[1] = _Session(None, b"")
    await wire.server._handle(services.db.gateway(1), sess, _hello_msg())
    assert dropped == [(1, True)] and wire.sent == [], wire.kinds()


async def test_hello_on_the_new_key_is_fixed_before_the_first_server_word(services, slot, vps, monkeypatch):
    """hello после переключения ВПС: переход зафиксирован ДО первого слова
    сервера в сессии — шлюз закрепляет выбор по нему."""
    from awgbot.runtime.linkserver.common import _Session
    wire = _Wire(services, monkeypatch)
    _switched(services, vps)
    new_key = vps.session_key()
    state_at_first_word = []

    async def send(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
        if not state_at_first_word:
            state_at_first_word.append(services.gateway_rekey(1))
        wire.sent.append((kind, dict(body or {})))
        return True
    wire.server.send = send
    sess = wire.server._sessions[1] = _Session(None, new_key)
    await wire.server._handle(services.db.gateway(1), sess, _hello_msg())
    assert state_at_first_word == [{}], f"первое слово ушло до фиксации: {state_at_first_word}"
    assert len(wire.notes) == 1 and "ключи линка сменены" in wire.notes[0], wire.notes



# ── hello прежним ключом, отзыв в очереди, сессия коммита ────────────────────

def test_hello_signed_with_the_old_key_after_the_switch_is_held(services, slot, vps):
    """ВПС переключился, а пришёл hello, подписанный ПРЕЖНИМ ключом (ключ
    сессии прочитан до подмены файла): это не доказательство — сессия
    закрывается молча, переход ждёт hello новым ключом; им — фиксация."""
    old_key = vps.session_key()
    _switched(services, vps)
    res = services.gateway_rekey_hello(1, old_key)
    assert res == {"notes": [], "hold": True, "rekeyed": False}, res
    assert services.gateway_rekey(1)["state"] == "switched" and "--rekey-drop" not in vps.modes
    fixed = services.gateway_rekey_hello(1, vps.session_key())
    assert fixed["rekeyed"] is True and services.gateway_rekey(1) == {}


def test_a_return_queues_link_cancel_for_the_next_session(services, slot, vps, clock):
    """ВПС вернул прежние по сроку — шлюз, у которого link_go потерялся,
    сутки держал бы «согласие дано»: link_cancel ждёт ближайшей сессии,
    выдаётся один раз."""
    st = _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS + 5)
    services.gateway_rekey_tick()
    assert services.gateway_rekey_cancel_take(1) == st["id"]
    assert services.gateway_rekey_cancel_take(1) == "", "отзыв выдан дважды"


@pytest.mark.parametrize("how", ["startup", "commit_failed", "day"])
def test_every_return_queues_link_cancel(services, slot, vps, clock, how):
    if how == "startup":
        st = _switched(services, vps)
        services.gateway_rekey_startup()
    elif how == "commit_failed":
        st = _asked(services, vps)
        services.gateway_rekey_ack_in(1, {"id": st["id"], "state": "ready"})
        services.gateway_rekey_go(1)
        vps.fail["--rekey-commit"] = "awg-quick up: ошибка"
        services.gateway_rekey_commit(1)
    else:
        st = _asked(services, vps)
        clock.at(rekeymod.ASK_HOURS * 3600 + 60)
        services.gateway_rekey_tick()
    assert services.gateway_rekey_cancel_take(1) == st["id"], how


def test_a_new_stage_forgets_the_previous_withdrawal(services, slot, vps, clock):
    """Новая смена ключей — прежний отзыв не о ней: его id шлюзу ни к чему."""
    _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS + 5)
    services.gateway_rekey_tick()
    services.gateway_rekey_stage(1)
    assert services.gateway_rekey_cancel_take(1) == ""


async def test_a_queued_withdrawal_goes_first_in_the_next_session(services, slot, vps, monkeypatch, clock):
    """Отзыв, ждавший сессии, уходит первым делом при доставке перехода —
    и больше не повторяется."""
    st = _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS + 5)
    services.gateway_rekey_tick()
    wire = _Wire(services, monkeypatch)
    await wire.server.deliver_rekey(1)
    assert wire.sent == [("link_cancel", {"id": st["id"]})], wire.sent
    wire.open()
    await wire.server.deliver_rekey(1)
    assert len(wire.sent) == 1, "отзыв ушёл второй раз"


async def test_the_liveness_tick_tells_a_live_session_about_the_return_at_once(services, slot, vps, monkeypatch,
                                                                                clock):
    """Возврат по сроку при живой сессии (шлюз на прежних, link_go пропал) —
    link_cancel уходит в том же такте, не ждёт переподключения; не ушёл —
    остаётся в очереди."""
    wire = _Wire(services, monkeypatch)
    st = _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS + 5)

    async def dead(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
        wire.sent.append((kind, dict(body or {})))
        return False
    wire.server.send = dead
    await wire.server.rekey_tick()
    assert ("link_cancel", {"id": st["id"]}) in wire.sent
    assert services.gateway_rekey_cancel_take(1) == st["id"], "не ушедший отзыв выпал из очереди"


async def test_the_liveness_tick_removes_a_delivered_withdrawal_from_the_queue(services, slot, vps, monkeypatch,
                                                                                clock):
    wire = _Wire(services, monkeypatch)
    st = _switched(services, vps)
    clock.at(rekeymod.SWITCH_SECONDS + 5)
    await wire.server.rekey_tick()
    assert wire.kinds().count("link_cancel") == 1 and wire.sent[0][1] == {"id": st["id"]}, wire.sent
    assert services.gateway_rekey_cancel_take(1) == ""


async def test_a_session_that_came_during_the_commit_is_not_dropped(services, slot, vps, monkeypatch):
    """Шлюз успел прийти новым ключом, пока шёл commit: рвать надо прежнюю
    сессию, а не новую — иначе фиксация ушла бы на следующий круг."""
    wire = _Wire(services, monkeypatch)
    dropped = []

    async def drop(slot_id, *, record=False):
        dropped.append(slot_id)
    wire.server._drop = drop
    real = services.gateway_rekey_commit

    def commit(slot_id):
        res = real(slot_id)
        wire.open()                                        # новая сессия поверх прежней
        return res
    monkeypatch.setattr(services, "gateway_rekey_commit", commit)
    st = _asked(services, vps)
    await wire.server.rekey_ack_in(1, {"t": "link_ack", "id": st["id"], "state": "ready"})
    assert await _settle(lambda: services.gateway_rekey(1).get("committed"))
    await asyncio.sleep(0.05)
    assert dropped == [], "порвана сессия, пришедшая уже новым ключом"
