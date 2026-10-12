"""Смена ключей линка и переезд профилей на стороне основного бота — что
нажатия меняют: «⚙️ Настройки → 🔧 Сервис → 🔑 Ключи линка» (кнопка — только
при шлюзе с подтверждением; один шлюз — сразу диалог, несколько — выбор) →
диалог → «🔑 Сменить» (стадия скрипта линка, состояние перехода слота,
link_next по каналу, итог первой строкой «Сервиса»), отказы всплывашкой;
ответы шлюза на приглашение (строки К1/К4 карточки, отказ — уведомление
один раз, Н5 по аплинку); финал переезда — строка про шлюз на прежнем
аплинке в диалоге и файл конфигурации только тем слотам, чей агент не
принимает документ; ссылка и кнопка «Перевыпустить» для агента с документом
ведут в карточку, а не в файл.

Цена ошибки: стадия, запущенная дважды, затирает .next первой и шлюз
получает ключи, которых у ВПС уже нет, — линк не поднимется ни на новых, ни
после возврата; link_next, не ушедший при живой сессии, — приглашение в чате
шлюза не появится, а карточка будет уверять, что ждёт подтверждения; файл
конфигурации агенту с документом — лишний секрет в чате, который тот
применять не станет. Сами экраны — в эталоне tests/screens/admin.txt
(adm.set.svc.keys, adm.lk.*, adm.gw.card.rekey.*, adm.gw.card.uplink.*,
adm.set.mig.finish.old_uplink*, adm.note.gwcfg.cfg1, adm.link.gwcfg.cfg1).

Хост подменён: режимы скрипта линка записываются (`slots.runs`), стадия
кладёт .next обеих сторон во временный каталог (ключи — настоящие X25519 из
nacl, чтобы отпечаток пары считался по-настоящему). Слушатель канала —
боевой LinkServer с одной сессией после hello; что ушло в сокет, читается
обратно разбором подписанной строки.
"""
from __future__ import annotations

import base64

import pytest
from aiogram.filters import CommandObject
from nacl.public import PrivateKey

from awgbot.bot.callbacks import GwSlotCB, NoteCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.bot.handlers.admin.panel import admin_start
from awgbot.bot.handlers.reply_commands import on_note_action
from awgbot.bot.texts import GATEWAY_KEYS_OFFLINE, GATEWAY_KEYS_SENT
from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.runtime import linkserver
from awgbot.runtime.linkserver.common import _Session
from awgbot.util import gwlink, wgconf
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _slot1, _slot2
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID

# фикстура слотов из теста экрана «🛰 Шлюзы»: устройства, скрипт линка, пробы
slots = _slots_ui.slots


def _pair() -> tuple[str, str]:
    k = PrivateKey.generate()
    return base64.b64encode(bytes(k)).decode(), base64.b64encode(bytes(k.public_key)).decode()


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _edits(nav):
    return [(t, m) for k, t, m in nav.sent if k == "edit_text"]


class _Stage:
    """Скрипт линка ВПС в режимах перехода: --rekey-stage кладёт новые .next
    обеих сторон (ключи — свежие пары X25519; Address, Endpoint, AllowedIPs —
    как у стоящего линка), остальные режимы только записываются. fail — режим
    стадии отказывает (ServiceError, как у настоящего вызова скрипта)."""

    def __init__(self, services, tmp_path, monkeypatch):
        self.services = services
        self.vps_next = tmp_path / "awglink.conf.next"
        self.gw_next = tmp_path / "gw-awglink.conf.next"
        self.fail = ""
        self.modes: list[str] = []
        self.gw_priv, self.gw_pub = _pair()
        self.vps_priv, self.vps_pub = _pair()
        monkeypatch.setattr(services, "_rekey_paths", lambda gw: (str(self.vps_next), str(self.gw_next)))
        monkeypatch.setattr(services, "_run_link_script", self._run)

    def _run(self, mode, env=None):
        from awgbot.domain.services import ServiceError
        self.services.runs.append((mode, dict(env or {})))
        self.modes.append(mode)
        if mode == "--rekey-stage":
            if self.fail:
                raise ServiceError(self.fail)
            self.vps_next.write_text(f"[Interface]\nPrivateKey = {self.vps_priv}\nListenPort = 443\n\n"
                                     f"[Peer]\nPublicKey = {self.gw_pub}\nAllowedIPs = 10.99.99.2/32\n",
                                     encoding="utf-8")
            self.gw_next.write_text(LINK.replace("DUMMY-LINK-PRIVATE-KEY=", self.gw_priv)
                                    .replace("DUMMY-VPS-PUBLIC-KEY=", self.vps_pub), encoding="utf-8")

    @property
    def fp(self) -> str:
        return gwcfg.key_fp(self.gw_pub, self.vps_pub)


class _Wire:
    """Сокет сессии: всё, что слушатель записал, — строками."""

    def __init__(self):
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.lines.append(data)

    async def drain(self) -> None:
        pass


@pytest.fixture()
def stage(services, slots, tmp_path, monkeypatch):
    return _Stage(services, tmp_path, monkeypatch)


@pytest.fixture()
def listener(services, monkeypatch):
    """Боевой слушатель канала; сессия слота открывается вызовом open(slot)."""
    srv = linkserver.LinkServer(services)
    monkeypatch.setattr(linkserver, "current", lambda: srv)
    key = gwlink.channel_key(base64.b64encode(b"DUMMY-LINK-KEY-FOR-TESTS-32BYTES").decode())

    def open_(slot: int, caps=("cfg1", "confirm1")):
        sess = _Session(_Wire(), key)
        sess.hello, sess.caps, sess.cn = True, list(caps), gwlink.new_nonce()
        srv._sessions[slot] = sess
        return sess

    def sent(slot: int) -> list[dict]:
        sess = srv._sessions[slot]
        return [gwlink.unpack(key, line, nonce=sess.cn) for line in sess.writer.lines]
    srv.open, srv.sent = open_, sent
    return srv


async def _lk(cb, services, key: str, slot: int = 0) -> None:
    """Нажатие в «🔧 Сервис → 🔑 Ключи линка»: pick — выбор шлюза, ask — диалог
    по слоту, yes — «🔑 Сменить»."""
    await sh.linkkeys_action(cb, SetCB(sec="lk", act="do", key=key, val=str(slot) if slot else ""), services)


def _confirm(services, slot: int = 1) -> None:
    """Агент слота умеет подтверждение: последний hello — с cfg1 и confirm1."""
    services.gwlink_session_opened(slot, "3.3.0", 2, ["cfg1", "confirm1"])


# ── «🔑 Ключи линка» → диалог ────────────────────────────────────────────────

async def _service(fake_bot, services):
    """«⚙️ Настройки → 🔧 Сервис» основного бота: (текст, markup)."""
    cb, nav = _acb(fake_bot)
    await sh.open_section(cb, SetCB(sec="svc"), services, FakeState())
    return _edits(nav)[-1]


async def test_service_offers_keys_only_when_some_agent_can_take_them(services, slots, stage, fake_bot):
    """Кнопка редкая и аварийная — живёт в «🔧 Сервис», последним рядом перед
    «Назад», и только если есть шлюз с агентом, умеющим подтверждение: агенту
    3.2 она вела бы в отказ. Нажатие по устаревшей кнопке без таких шлюзов —
    всплывашка-объяснение, а не пустой экран."""
    _, pi, _ = slots
    _slot1(services, pi)
    _, markup = await _service(fake_bot, services)
    assert "🔑 Ключи линка" not in _labels(markup), "кнопка при агенте без подтверждения"
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "pick")
    assert cb.answers[-1] == ("Сменить ключи линка по упр. каналу не у кого: нужен агент шлюза v3.3.0", True), cb.answers
    assert not _edits(nav)
    _confirm(services)
    _, markup = await _service(fake_bot, services)
    rows = [[b.text for b in r] for r in markup.inline_keyboard]
    assert rows[-2:] == [["🔑 Ключи линка"], ["⬅️ Назад"]], rows
    lk = [b.callback_data for r in markup.inline_keyboard for b in r if b.text == "🔑 Ключи линка"]
    assert lk == [SetCB(sec="lk", act="do", key="pick").pack()], lk


async def test_one_gateway_opens_the_dialog_with_cancel_back_to_service(services, slots, stage, fake_bot):
    """Один шлюз с подтверждением — «🔑 Ключи линка» сразу открывает диалог
    (экран выбора из одного пункта — лишнее нажатие); «⬅️ Отмена» первой и
    ведёт обратно в «🔧 Сервис»; стадия до «🔑 Сменить» не запускается:
    случайное нажатие не генерирует ключей и ничего не шлёт шлюзу."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _confirm(services)
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "pick")
    text, markup = _edits(nav)[-1]
    assert text.startswith("🔑 Сменить ключи линка NASPi?"), text
    assert _labels(markup) == ["⬅️ Отмена", "🔑 Сменить"], _labels(markup)
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [SetCB(sec="svc").pack(), SetCB(sec="lk", act="do", key="yes", val="1").pack()], datas
    assert stage.modes == [] and services.gateway_rekey(1) == {}, "диалог уже что-то сделал"


async def test_two_gateways_get_a_pick_screen(services, slots, stage, fake_bot):
    """Два шлюза с подтверждением — экран выбора: кнопка на слот (имя с
    подписью — два одинаковых устройства различимы), «⬅️ Назад» — в «Сервис»;
    кнопка слота открывает диалог именно этого слота."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _confirm(services, 1); _confirm(services, 2)
    services.gateway_set_label(2, "север")
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "pick")
    text, markup = _edits(nav)[-1]
    assert text.startswith("🔑 <b>Ключи линка</b>"), text
    rows = [[(b.text, b.callback_data) for b in r] for r in markup.inline_keyboard]
    assert rows == [[("NASPi", SetCB(sec="lk", act="do", key="ask", val="1").pack())],
                    [("Pi2 (север)", SetCB(sec="lk", act="do", key="ask", val="2").pack())],
                    [("⬅️ Назад", SetCB(sec="svc").pack())]], rows
    assert stage.modes == [] and services.gateway_rekey(1) == {} and services.gateway_rekey(2) == {}
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 2)
    assert _edits(nav)[-1][0].startswith("🔑 Сменить ключи линка Pi2 (север)?"), _edits(nav)[-1][0]


async def test_keys_dialog_names_the_slot_with_its_label(services, slots, stage, fake_bot):
    """Два шлюза с одним именем устройства различимы только подписью:
    вопрос «Сменить ключи линка …?» называет слот с подписью, иначе человек
    подтверждает смену ключей не у того шлюза. Разметка в подписи —
    экранируется."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    services.gateway_set_label(1, "север <b>")
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    text = _edits(nav)[-1][0]
    assert text.startswith("🔑 Сменить ключи линка NASPi (север &lt;b&gt;)?"), text.splitlines()[0]


async def test_keys_dialog_advises_the_live_standby_only_for_the_active_slot(services, slots, stage, fake_bot):
    """Активный слот при живом резерве — третья строка «сначала сделай его
    активным»: без неё человек кладёт РФ-доступ всем на время перехода, хотя
    мог бы сменить ключи без обрыва. У резервного — ни совета, ни «РФ-доступ
    прервётся»: он трафик не несёт."""
    from tests.e2e.test_gateway_slots_ui import _settled
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _settled(services)
    _confirm(services, 1); _confirm(services, 2)
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    active = _edits(nav)[-1][0]
    assert "Есть живой резерв: сначала сделай его активным" in active, active
    assert "РФ-доступ прервётся на секунды" in active, active
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 2)
    standby = _edits(nav)[-1][0]
    assert "живой резерв" not in standby and "РФ-доступ прервётся" not in standby, standby
    # резерв лёг — совета у активного нет: «сделай его активным» увёл бы трафик в мёртвый
    services.probe[2] = "down"
    for _ in range(5):
        services.routing_liveness_tick()
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    assert "живой резерв" not in _edits(nav)[-1][0], _edits(nav)[-1][0]


async def test_keys_button_refuses_while_any_slot_is_mid_transition(services, slots, stage, fake_bot):
    """Переход идёт у этого слота — «Смена ключей уже идёт»; у другого — «Идёт
    смена ключей у …»: два перехода разом могут положить оба линка, и РФ-доступ
    останется без резерва. Всплывашка, а не диалог."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _confirm(services, 1); _confirm(services, 2)
    services._rekey_save(1, {"id": "a1", "state": "asked", "fp": "ab12 cd34"})
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    assert cb.answers[-1] == ("Смена ключей уже идёт", True), cb.answers
    assert not _edits(nav), "при идущем переходе нарисован диалог"
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 2)
    assert cb.answers[-1][1] is True and cb.answers[-1][0].startswith("Идёт смена ключей у NASPi"), cb.answers
    assert not _edits(nav)


# ── «🔑 Сменить» ─────────────────────────────────────────────────────────────

async def test_change_stages_keys_sends_link_next_and_shows_the_result_first(
        services, slots, stage, listener, fake_bot):
    """Стадия скрипта линка — один раз; переход слота — staged с отпечатком
    пары новых ключей; шлюзу на связи ушёл ровно один подписанный link_next с
    конфигом шлюзовой стороны (новые ключи, те же адреса); карточка — итог
    первой строкой и строка ожидания с тем же отпечатком, который человек
    сверит в чате шлюза. Всплывашки «не на связи» нет."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1)
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert stage.modes == ["--rekey-stage"], stage.modes
    st = services.gateway_rekey(1)
    assert st.get("state") == "staged" and st.get("fp") == stage.fp and st.get("id"), st
    msgs = listener.sent(1)
    assert [m["t"] for m in msgs] == ["link_next"], [m["t"] for m in msgs]
    body = msgs[0]
    assert body["id"] == st["id"] and body["fp"] == stage.fp, body
    conf = wgconf.parse(body["conf"])
    assert conf.get("Interface", "PrivateKey") == stage.gw_priv, "в link_next не новый ключ шлюза"
    assert conf.get("Peer", "PublicKey") == stage.vps_pub, "в link_next не новый ключ ВПС"
    assert conf.get("Interface", "Address") == "10.99.99.2/30", "адрес линка сменился вместе с ключами"
    text, markup = _edits(nav)[-1]
    assert text.startswith(GATEWAY_KEYS_SENT + "\n\n🔧 <b>Сервис</b>"), text.splitlines()[:2]
    assert "🔁 Перезапуск AWG" in _labels(markup), "под итогом — не «🔧 Сервис»"
    assert all(a[0] != GATEWAY_KEYS_OFFLINE for a in cb.answers), cb.answers
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    card = _edits(nav)[-1][0]
    assert f"🔑 Ключи линка ждут подтверждения в чате бота шлюза · <code>{stage.fp}</code>" in card, card


async def test_a_second_change_neither_restages_nor_resends(services, slots, stage, listener, fake_bot):
    """Двойное нажатие «🔑 Сменить» (или кнопка со старого диалога): вторая
    стадия перезаписала бы .next, и шлюз подтвердил бы ключи, которых у ВПС уже
    нет. Ответ — всплывашка «Смена ключей уже идёт», скрипт и канал не тронуты."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1)
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    first = services.gateway_rekey(1)
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert cb.answers[-1] == ("Смена ключей уже идёт", True), cb.answers
    assert stage.modes == ["--rekey-stage"], f"стадия запущена повторно: {stage.modes}"
    assert services.gateway_rekey(1) == first, "второе нажатие переписало переход"
    assert len(listener.sent(1)) == 1, "link_next ушёл второй раз"
    assert not _edits(nav), "отказ перерисовал экран"


async def test_change_offline_keeps_the_transition_and_says_keys_go_by_channel(
        services, slots, stage, fake_bot):
    """Шлюз не на связи: переход всё равно стоит (link_next уйдёт при
    подключении), карточка с итогом и строкой ожидания, всплывашка «Шлюз не на
    связи — новые ключи едут по упр. каналу» — без неё человек ждал бы
    приглашения в чате шлюза прямо сейчас."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    services.gwlink_session_closed(1)
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert services.gateway_rekey(1).get("state") == "staged"
    assert cb.answers[-1] == (GATEWAY_KEYS_OFFLINE, False), cb.answers
    text, _ = _edits(nav)[-1]
    assert text.startswith(GATEWAY_KEYS_SENT), text


async def test_change_with_a_session_but_no_confirm_does_not_send(services, slots, stage, listener, fake_bot):
    """Сессия открыта агентом без подтверждения (сессия прежней версии висит,
    пока новая не подключилась): link_next ему не шлётся — он его не поймёт;
    человек видит «не на связи», переход ждёт новой сессии."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1, caps=("cfg1",))
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert listener.sent(1) == [], "link_next ушёл агенту без confirm1"
    assert cb.answers[-1] == (GATEWAY_KEYS_OFFLINE, False), cb.answers


async def test_change_refused_by_the_service_is_an_alert_and_changes_nothing(
        services, slots, stage, listener, fake_bot):
    """Агент без подтверждения (3.2) — «Агенту шлюза нужна версия 3.3.0»;
    скрипт стадии отказал — причина. Оба раза: всплывашка-алерт, переход не
    записан, канал молчит, экран не перерисован."""
    _, pi, _ = slots
    _slot1(services, pi)
    services.gwlink_session_opened(1, "3.2.1", 2, ["cfg1"])
    listener.open(1, caps=("cfg1",))
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert cb.answers[-1] == ("Нужен агент шлюза v3.3.0", True), cb.answers
    assert stage.modes == [] and services.gateway_rekey(1) == {} and not _edits(nav)
    _confirm(services)
    stage.fail = "awg genkey: нет бинаря"
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    assert cb.answers[-1][1] is True and "Новые ключи не сгенерированы" in cb.answers[-1][0], cb.answers
    assert "awg genkey: нет бинаря" in cb.answers[-1][0], "причина отказа скрипта потеряна"
    assert services.gateway_rekey(1) == {} and not _edits(nav) and listener.sent(1) == []


# ── ответы шлюза ─────────────────────────────────────────────────────────────

async def test_gateway_answers_move_the_card_and_a_refusal_notifies_once(
        services, slots, stage, listener, fake_bot):
    """link_ack asked — карточка по-прежнему ждёт подтверждения; «⬅️ Отмена» в
    чате шлюза (declined) — .next убраны скриптом, переход снят и оставил
    отказ: в карточке вместо строки ожидания «⚠️ Шлюз отклонил новые ключи
    линка — линк на прежних» (иначе человек на сервере не узнает, почему
    ключи не сменились), админу одно уведомление без звука (Н3). Повтор того
    же отказа (ответ из очереди после переподключения) второго уведомления не
    рождает. Отказ — не идущий переход: «🔑 Ключи линка» снова открывает
    диалог, а не «уже идёт»."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1)
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    rid = services.gateway_rekey(1)["id"]
    assert services.gateway_rekey_ack_in(1, {"id": rid, "state": "asked"}) == ("", [])
    assert services.gateway_rekey(1)["state"] == "asked"
    action, notes = services.gateway_rekey_ack_in(1, {"id": rid, "state": "declined", "error": ""})
    assert action == "" and len(notes) == 1, notes
    assert "смену ключей линка отклонили в чате бота шлюза" in notes[0].text, notes[0].text
    assert notes[0].tg_id == ADMIN and not notes[0].force_sound and not notes[0].critical, \
        "отказ от ключей — уведомление админу без звука"
    assert stage.modes[-1] == "--rekey-drop", "отказ не убрал .next скриптом"
    assert services.gateway_rekey(1).get("state") == "declined", services.gateway_rekey(1)
    assert services.gateway_rekey_ack_in(1, {"id": rid, "state": "declined"}) == ("", []), \
        "повтор отказа — второе уведомление"
    assert stage.modes.count("--rekey-drop") == 1, f"повтор отказа снова дёрнул скрипт: {stage.modes}"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    card = _edits(nav)[-1][0]
    assert "Ключи линка ждут подтверждения" not in card, card
    assert "⚠️ Шлюз отклонил новые ключи линка — линк на прежних" in card, card
    cb, nav = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    assert _edits(nav) and _edits(nav)[-1][0].startswith("🔑 Сменить ключи линка"), \
        f"после отказа смену ключей не начать: {cb.answers}"


async def test_a_refusal_line_lives_a_day_and_leaves_silently(services, slots, stage, listener, fake_bot):
    """Строка отказа держится сутки и уходит без уведомления: о самом отказе
    уже сказано (Н3), второе «отменено» через сутки было бы ложной тревогой,
    а вечная строка — шумом в карточке."""
    from awgbot.util import timeutil
    from datetime import timedelta
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1)
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    rid = services.gateway_rekey(1)["id"]
    services.gateway_rekey_ack_in(1, {"id": rid, "state": "declined", "error": ""})
    modes = list(stage.modes)
    assert services.gateway_rekey_tick() == ([], []), "свежий отказ снят тактом"
    assert services.gateway_rekey(1).get("state") == "declined"
    st = services.gateway_rekey(1)
    st["at"] = timeutil.to_iso(timeutil.now() - timedelta(hours=25))
    services._rekey_save(1, st)
    assert services.gateway_rekey_tick() == ([], []), "снятие суточного отказа — с link_cancel или уведомлением"
    assert services.gateway_rekey(1) == {}
    assert stage.modes == modes, f"снятие строки отказа дёрнуло скрипт: {stage.modes[len(modes):]}"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    assert "отклонил новые ключи" not in _edits(nav)[-1][0]


async def test_a_refused_uplink_notifies_once_per_fingerprint(services, slots, listener, monkeypatch):
    """Н5: агент ответил на документ отказом по аплинку («⬅️ Отмена» в чате
    шлюза) — админу одно уведомление без звука «🔐 <шлюз>: новый конфиг
    аплинка отклонили в чате бота шлюза». Тот же ответ ещё раз (из очереди
    после переподключения, или ответ по другому разделу того же документа) —
    тишина; аплинк ждёт решения — не отказ, тишина; отказ на другой конфиг
    (новый отпечаток) — снова одно."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    sess = listener.open(1)
    got: list = []

    async def on_notes(notes):
        got.append(list(notes))
    monkeypatch.setattr(linkserver, "_on_notes", on_notes)
    gw = services.db.gateway(1)
    first = {"t": "cfg_ack", "gen": 4, "hash": "c0" * 8, "applied": ["env", "server"], "wait": {"uplink": "ab" * 8}}
    await listener._handle(gw, sess, first)
    assert got == [], f"аплинк ждёт решения, а пришло уведомление: {got}"
    declined = {"t": "cfg_ack", "gen": 4, "hash": "c0" * 8, "declined": {"uplink": "ab" * 8}}
    await listener._handle(gw, sess, declined)
    assert len(got) == 1 and len(got[0]) == 1, got
    note = got[0][0]
    assert "новый конфиг аплинка отклонили в чате бота шлюза" in note.text and "NASPi" in note.text, note.text
    assert note.tg_id == ADMIN and not note.force_sound and not note.critical, "Н5 — без звука"
    await listener._handle(gw, sess, declined)
    await listener._handle(gw, sess, {"t": "cfg_ack", "gen": 4, "hash": "c0" * 8, "applied": ["mail"]})
    assert len(got) == 1, f"тот же отказ — повторное уведомление: {got}"
    assert services.gwcfg_ack(1).get("declined") == {"uplink": "ab" * 8}, "ответ по почте стёр отказ по аплинку"
    await listener._handle(gw, sess, {"t": "cfg_ack", "gen": 5, "hash": "c1" * 8, "declined": {"uplink": "cd" * 8}})
    assert len(got) == 2, "отказ на другой конфиг аплинка промолчал"


async def test_ready_from_the_gateway_switches_and_the_card_says_so(services, slots, stage, listener, fake_bot,
                                                                   monkeypatch):
    """«🔑 Применить» в чате шлюза (ready) — ушёл link_go с тем же id, карточка
    показывает «🔑 Ключи линка: переключение…», а кнопка второго нажатия —
    «Смена ключей уже идёт» (переключение посреди не прервать)."""
    _, pi, _ = slots
    _slot1(services, pi)
    _confirm(services)
    listener.open(1)
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "yes", 1)
    rid = services.gateway_rekey(1)["id"]
    async def later(slot_id):          # переключение ВПС через 2 с — не предмет этого теста
        pass
    monkeypatch.setattr(listener, "_rekey_commit_later", later)
    await listener.rekey_ack_in(1, {"id": rid, "state": "ready"})
    kinds = [(m["t"], m.get("id")) for m in listener.sent(1)]
    assert kinds == [("link_next", rid), ("link_go", rid)], kinds
    assert services.gateway_rekey(1)["state"] == "switched"
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=1), services, FakeState())
    assert "🔑 Ключи линка: переключение…" in _edits(nav)[-1][0], _edits(nav)[-1][0]
    cb, _ = _acb(fake_bot)
    await _lk(cb, services, "ask", 1)
    assert cb.answers[-1] == ("Смена ключей уже идёт", True)


# ── «✏️ Изменить»: кнопки ключей нет ──────────────────────────────────────────

async def test_edit_screen_has_no_keys_button_for_any_agent(services, slots, fake_bot):
    """Смена ключей — только из «🔧 Сервис»: на рабочих экранах слота её нет ни
    у агента 3.2, ни у агента с подтверждением (кнопка «🔑» рядом с «✏️ Имя»
    нажимается случайно, а цена — обрыв РФ-доступа)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    for slot in (1, 2):
        cb, nav = _acb(fake_bot)
        await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=slot), services, FakeState())
        assert "🔑 Ключи линка" not in _labels(_edits(nav)[-1][1])
    _confirm(services, 1); _confirm(services, 2)
    services.gwlink_snapshot_in(1, {"agent_version": "3.3.0", "rev": 1}, 1, True)
    for slot in (1, 2):
        cb, nav = _acb(fake_bot)
        await sh.gw_slot_edit(cb, GwSlotCB(action="edit", slot=slot), services, FakeState())
        assert "🔑 Ключи линка" not in _labels(_edits(nav)[-1][1]), f"слот {slot}: кнопка ключей в «Изменить»"
        cb, nav = _acb(fake_bot)
        await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
        assert "🔑 Ключи линка" not in _labels(_edits(nav)[-1][1]), f"слот {slot}: кнопка ключей в карточке"


# ── финал переезда ───────────────────────────────────────────────────────────

def _bundles(services, monkeypatch) -> list:
    got: list = []

    def enc(slot=None):
        got.append(slot)
        return b"DUMMY-ENC", f"b{slot}.enc"
    monkeypatch.setattr(services, "gw_bundle_encrypted", enc)
    return got


async def test_finish_dialog_names_the_gateway_still_on_the_old_uplink(services, slots, fake_bot, monkeypatch):
    """Диалог «Завершить переезд?» получает строку «⚠️ … ещё на прежнем
    аплинке» по слову сервисов (gwcfg_old_uplink) — со ссылкой на бота шлюза;
    без таких шлюзов строки нет. Без неё финал молча отрезает бота шлюза от
    Telegram."""
    monkeypatch.setattr(services, "migration_available", lambda: True)
    monkeypatch.setattr(services, "migration_running", lambda: True)
    monkeypatch.setattr(services, "migration_finish_preview", lambda: (0, []))
    monkeypatch.setattr(services, "gwcfg_old_uplink",
                        lambda: [("NASPi", {"username": "naspi_gw_bot", "name": "NASPi"})])
    cb, nav = _acb(fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish"), services)
    text = _edits(nav)[-1][0]
    assert "⚠️ NASPi ещё на прежнем аплинке: после финала бот шлюза потеряет Telegram" in text, text
    assert 'href="https://t.me/naspi_gw_bot"' in text, text
    monkeypatch.setattr(services, "gwcfg_old_uplink", lambda: [])
    cb, nav = _acb(fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish"), services)
    assert "прежнем аплинке" not in _edits(nav)[-1][0]


async def test_finish_sends_the_file_only_to_slots_without_the_document(services, slots, fake_bot, monkeypatch):
    """После финала подсеть клиентов сменилась: агенту 3.2 нужен файл, агенту с
    документом (cfg1) — нет, ему подсеть и AllowedIPs доедут каналом, а файл —
    лишний секрет в чате (ключ линка внутри)."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1"])
    got = _bundles(services, monkeypatch)
    monkeypatch.setattr(services, "migration_finish", lambda: (1, 0, []))
    monkeypatch.setattr(services, "migration_available", lambda: True)
    monkeypatch.setattr(services, "migration_running", lambda: True)
    cb, nav = _acb(fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)
    assert got == [2], f"файлы ушли слотам {got}, ждали только слоту 2 (агент без документа)"
    assert sum(1 for s in nav.sent if s[0] == "document") == 1


# ── «Перевыпустить» и /start gwcfg-N ─────────────────────────────────────────

async def test_reissue_button_opens_the_card_for_an_agent_with_the_document(services, slots, fake_bot, monkeypatch):
    """Кнопка «Перевыпустить» на старом напоминании: агенту с документом файл
    не нужен — открывается карточка слота; агенту без него — файл, как раньше."""
    _, pi, _ = slots
    _slot1(services, pi)
    got = _bundles(services, monkeypatch)
    cb, nav = _acb(fake_bot)
    await on_note_action(cb, NoteCB(kind="gwcfg", ref=1), FakeState(), services, role="admin")
    assert got == [1] and any(s[0] == "document" for s in nav.sent), "агенту без документа — не файл"
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1"])
    got.clear()
    cb, nav = _acb(fake_bot)
    await on_note_action(cb, NoteCB(kind="gwcfg", ref=1), FakeState(), services, role="admin")
    assert got == [] and not any(s[0] == "document" for s in nav.sent), "агенту с документом ушёл файл"
    shown = [t for k, t, _ in nav.sent if k in ("answer", "edit_text")] + \
        [r[2] for r in fake_bot.records if r[0] == "send_message"]
    assert any(t.startswith("🛰 <b>NASPi</b>") for t in shown), shown


async def test_reissue_button_card_goes_back_to_the_main_screen(services, slots, fake_bot, monkeypatch):
    """Карточка, открытая кнопкой с уведомления, — как карточка по ссылке
    gw-<слот>: «⬅️ Назад» ведёт на главную, откуда человек пришёл, а не в
    раздел маршрутизации, которого он не открывал; и подэкраны карточки
    возвращают туда же."""
    from awgbot.bot.callbacks import Menu
    from awgbot.bot.handlers.common import card_from_main, card_is_from_main
    _, pi, _ = slots
    _slot1(services, pi)
    _bundles(services, monkeypatch)
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1"])
    card_from_main(ADMIN, False)
    cb, nav = _acb(fake_bot)
    await on_note_action(cb, NoteCB(kind="gwcfg", ref=1), FakeState(), services, role="admin")
    assert card_is_from_main(ADMIN), "карточка с уведомления не помечена «пришли с главной»"
    card_from_main(ADMIN, False)
    markups = [m for k, t, m in nav.sent if k in ("answer", "edit_text") and t.startswith("🛰 <b>NASPi</b>")]
    assert markups, nav.sent
    back = [b.callback_data for row in markups[-1].inline_keyboard for b in row if b.text == "⬅️ Назад"]
    assert back == [Menu(action="main").pack()], f"«Назад» с карточки ведёт в {back}"


async def test_gwcfg_link_opens_the_card_for_an_agent_with_the_document(services, slots, fake_bot, monkeypatch):
    """/start gwcfg-1 из уведомления: агенту с документом — карточка слота
    вместо файла; агенту без документа — файл."""
    _, pi, _ = slots
    _slot1(services, pi)
    got = _bundles(services, monkeypatch)
    msg = FakeMessage(text="/start gwcfg-1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_start(msg, services, FakeState(), command=CommandObject(prefix="/", command="start", args="gwcfg-1"))
    assert got == [1], "агенту без документа файл не выдан"
    services.gwlink_session_opened(1, "3.3.0", 2, ["cfg1", "confirm1"])
    got.clear()
    msg = FakeMessage(text="/start gwcfg-1", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await admin_start(msg, services, FakeState(), command=CommandObject(prefix="/", command="start", args="gwcfg-1"))
    assert got == [], "агенту с документом ушёл файл"
    shown = [t for k, t, _ in msg.sent if k in ("answer", "edit_text")] + \
        [r[2] for r in fake_bot.records if r[0] in ("send_message", "edit_message_text")]
    assert any(t.startswith("🛰 <b>NASPi</b>") for t in shown), shown
