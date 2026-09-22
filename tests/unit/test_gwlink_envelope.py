"""
Конверт канала ВПС ↔ шлюз (util/gwlink): подпись, окно времени, порядок,
предел строки, добивка до кратности.

Канал живёт внутри туннеля, шифрование даёт туннель — конверт отвечает ровно
за две вещи: сообщение одного слота нельзя выдать за сообщение другого, и
пересланный руками claim нельзя скормить каналу. Если хоть одна проверка
здесь ослабнет, клиент туннеля, дотянувшийся до порта канала, начнёт
рассказывать серверу про состояние чужого шлюза.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

from awgbot.util import bundlecrypt, gwlink, gwsign


def _priv() -> str:
    return base64.b64encode(os.urandom(32)).decode()


@pytest.fixture()
def key() -> bytes:
    return gwlink.channel_key(_priv())


# ── норма ────────────────────────────────────────────────────────────────────

def test_a_packed_message_comes_back_with_its_fields_kind_and_order(key):
    """Базовый круг: что упаковали, то и разобрали. Поля вида едут рядом с
    видом, номером и меткой времени — без них принимающая сторона не отличит
    снимок от дельты и не заметит повтора."""
    line = gwlink.pack(key, "snap", {"rev": 1, "agent_version": "3.1.0"}, seq=7)
    assert line.endswith(b"\n"), "сообщение — строка: поток разбирается readline(), без длин и состояний"
    msg = gwlink.unpack(key, line)
    assert msg["t"] == "snap" and msg["seq"] == 7 and msg["rev"] == 1
    assert msg["agent_version"] == "3.1.0"
    assert isinstance(msg["ts"], int) and msg["ts"] > 0


def test_an_empty_body_is_a_valid_message(key):
    """`ask snap` едет без единого поля — конверт обязан пережить пустое тело:
    иначе кнопка «Обновить» на ВПС не доедет до шлюза."""
    msg = gwlink.unpack(key, gwlink.pack(key, "ask", None, seq=1))
    assert msg["t"] == "ask" and msg["seq"] == 1


# ── подпись и разделение доменов ─────────────────────────────────────────────

def test_a_message_signed_by_another_link_is_refused(key):
    """Ключ канала выводится из приватного ключа линка: у каждого слота свой.
    Пройди чужая подпись — снимок первого шлюза лёг бы в карточку второго."""
    other = gwlink.channel_key(_priv())
    line = gwlink.pack(other, "snap", {"rev": 1}, seq=1)
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, line)


def test_one_flipped_byte_of_the_body_breaks_the_signature(key):
    """Подпись покрывает всё тело: правка значения на лету обязана ломать её,
    иначе посредник внутри туннеля правил бы снимок по дороге."""
    line = gwlink.pack(key, "snap", {"agent_version": "3.1.0"}, seq=1)
    head, _, tail = line.decode().strip()[len(gwlink.PREFIX):].partition(".")
    raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
    body = json.loads(raw)
    body["agent_version"] = "9.9.9"
    forged = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, f"{gwlink.PREFIX}{forged}.{tail}")


def test_the_channel_key_is_a_domain_of_its_own_apart_from_the_claim_key():
    """Ключ канала и ключ пересылаемого claim выводятся из одного секрета, но
    разными доменами. Совпади они — пересланный человеком claim можно было бы
    скормить каналу как сообщение, а перехваченное сообщение канала выдать за
    claim."""
    priv = _priv()
    assert gwlink.channel_key(priv) != gwsign._key(priv), "домены подписи слиплись"
    assert gwlink.channel_key(priv) != bundlecrypt.derive_key(priv), (
        "ключ канала совпал с ключом шифрования бандла")
    # сообщение, подписанное ключом claim, каналом не принимается
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(gwlink.channel_key(priv),
                      gwlink.pack(gwsign._key(priv), "snap", {"rev": 1}, seq=1))


def test_a_forwarded_claim_token_is_not_a_channel_message():
    """У claim свой префикс и своя семидневная давность. Прими его канал —
    старое пересланное сообщение снова стало бы «живым»."""
    priv = _priv()
    token = gwsign.sign(priv, "claim", "PUB==", "pi")
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(gwlink.channel_key(priv), token)


# ── окно времени и порядок ───────────────────────────────────────────────────

@pytest.mark.parametrize("shift", [-301, 301, -10_000])
def test_a_message_outside_the_five_minute_window_is_refused(key, shift):
    """Окно ±300 с: обе стороны живы и в одной сети, а широкое окно помогало бы
    тому, кто записал сообщение и шлёт его заново."""
    line = gwlink.pack(key, "snap", {"rev": 1}, seq=1, now=1_000_000 + shift)
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, line, now=1_000_000)


@pytest.mark.parametrize("shift", [-299, 0, 299])
def test_a_message_inside_the_window_passes(key, shift):
    """Край окна не должен отсекать честное сообщение: часы малины без RTC
    уезжают на секунды, и рвать из-за этого сессию — значит рвать её постоянно."""
    line = gwlink.pack(key, "snap", {"rev": 1}, seq=1, now=1_000_000 + shift)
    assert gwlink.unpack(key, line, now=1_000_000)["t"] == "snap"


def test_a_repeated_or_backwards_seq_is_refused_and_the_next_one_passes(key):
    """Номер в сессии растёт строго. Повтор — это либо записанное сообщение,
    поданное заново, либо поломка; принять его значит наложить старую дельту
    поверх свежего снимка."""
    for seq in (5, 4, 0):
        with pytest.raises(gwlink.ProtocolError):
            gwlink.unpack(key, gwlink.pack(key, "delta", {"rev": 2}, seq=seq), last_seq=5)
    assert gwlink.unpack(key, gwlink.pack(key, "delta", {"rev": 2}, seq=6), last_seq=5)["seq"] == 6


# ── размер ───────────────────────────────────────────────────────────────────

def test_a_line_over_the_limit_is_refused(key):
    """Единственный источник на том конце — наш же агент. Мегабайтная строка
    означает либо поломку, либо попытку засадить нам память: разбирать её
    незачем."""
    line = gwlink.PREFIX + "A" * (gwlink.MAX_LINE + 10) + ".BBBB"
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, line)


@pytest.mark.parametrize("junk", ["", "\n", "просто текст", "GL1:", "GL1:no-dot-here",
                                  "GL1:@@@@.@@@@", "GL2:AAAA.BBBB"])
def test_garbage_raises_only_protocol_error(key, junk):
    """Сессию рвёт разбор, а не падение процесса: всё, что пришло по каналу, —
    недоверенные данные, и единственный их итог — ProtocolError."""
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, junk)


def test_a_valid_signature_over_a_non_object_or_kindless_body_is_refused(key):
    """Подпись сошлась — это ещё не сообщение канала: без вида принимающая
    сторона не знает, что с ним делать, и молча приняла бы что угодно."""
    for raw in (b"[1,2,3]", b'{"seq":1,"ts":0}', b'{"t":5,"seq":1}'):
        import hashlib
        import hmac
        mac = hmac.new(key, raw, hashlib.sha256).digest()[:20]

        def b64(data):
            return base64.urlsafe_b64encode(data).decode().rstrip("=")
        with pytest.raises(gwlink.ProtocolError):
            gwlink.unpack(key, f"{gwlink.PREFIX}{b64(raw)}.{b64(mac)}", now=0)


# ── добивка ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload_len", [0, 1, 17, 200, 511, 700, 2000])
def test_snap_is_padded_to_512_and_delta_to_256(key, payload_len):
    """Внутри туннеля наблюдателю не видно содержимое, но видны длины. «Всегда
    ровно 317 байт» — такая же подпись события, как ровный период; поэтому
    длина сообщения обязана быть кратной, что бы ни лежало в теле."""
    body = {"x": "y" * payload_len}
    assert len(gwlink.pack(key, "snap", body, seq=1, pad=gwlink.PAD_SNAP)) % gwlink.PAD_SNAP == 0
    assert len(gwlink.pack(key, "delta", body, seq=1, pad=gwlink.PAD_DELTA)) % gwlink.PAD_DELTA == 0


def test_padding_lives_inside_the_signature_and_never_reaches_the_reader(key):
    """Добивка подписана вместе с телом: срезать её по дороге, оставив
    сообщение годным, нельзя. Разобранному сообщению она при этом не видна —
    иначе поле «_» доехало бы до снимка и до экрана."""
    line = gwlink.pack(key, "snap", {"rev": 1}, seq=1, pad=gwlink.PAD_SNAP)
    assert "_" not in gwlink.unpack(key, line), "служебная добивка протекла в разобранное сообщение"
    head, _, tail = line.decode().strip()[len(gwlink.PREFIX):].partition(".")
    raw = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
    body = json.loads(raw)
    assert body["_"].strip() == "", "добивка — пробелы внутри подписанного тела"
    del body["_"]
    stripped = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    with pytest.raises(gwlink.ProtocolError):
        gwlink.unpack(key, f"{gwlink.PREFIX}{stripped}.{tail}")


def test_without_padding_the_message_is_as_short_as_its_body(key):
    """Служебные сообщения (`hello`, `ask`) добивкой не гоняем: они и так не
    несут события, а лишние полкилобайта в линке — трафик из ничего."""
    assert len(gwlink.pack(key, "hello", {"proto": 1}, seq=1)) < gwlink.PAD_DELTA
