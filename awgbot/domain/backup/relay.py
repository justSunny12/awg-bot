"""
relay.py — часть пакета backup (см. __init__): бэкап сервера AWG, присланный в
чат бота шлюза, едет серверу по каналу конфигурации — частями, с контрольной
суммой и пределом размера. Общее обеих сторон: нарезка на агенте и сборка на
сервере обязаны совпадать.

Части — сообщения `rst_part` {"id", "i", "n", "size", "sha", "name", "z"}
(z — base64 части); ответ сервера — `rst_done` {"id", "ok", "error"}. Строка
канала не длиннее gwlink.MAX_LINE: часть в 96 КБ — 128 КБ base64 с конвертом.
Повтор части не ломает сборку (та же часть — та же), чужой id начинает сборку
заново, обрыв сессии стирает собранное. На шлюзе ничего не хранится: в файле
ключи — обрыв значит «пришли файл снова».
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets

CAP = "restore1"                       # сервер принимает бэкап через шлюз
MAX_BYTES = 20 * 1024 * 1024           # как у файла из чата сервера
PART = 96 * 1024
MIN_BYTES = 2048                       # короче настоящего бэкапа (подпись, соль, база) — отказ до осмотра
MAX_PARTS = -(-MAX_BYTES // PART)
_ID_RE = re.compile(r"[0-9a-f]{12}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")


class RelayError(ValueError):
    """Часть или сборка не годится — причина словами для ответа шлюзу."""


def split(blob: bytes, name: str) -> list[dict]:
    """Тела частей одной передачи."""
    if not blob or len(blob) > MAX_BYTES:
        raise RelayError("бэкап больше 20 МБ" if blob else "файл пуст")
    tid = secrets.token_hex(6)
    sha = hashlib.sha256(blob).hexdigest()
    chunks = [blob[i:i + PART] for i in range(0, len(blob), PART)]
    return [{"id": tid, "i": i, "n": len(chunks), "size": len(blob), "sha": sha, "name": str(name or "")[:128],
             "z": base64.b64encode(c).decode()} for i, c in enumerate(chunks)]


class Assembly:
    """Сборка одной передачи на сервере. add — часть принята; done — всё на
    месте и сумма сошлась: blob и name."""

    def __init__(self, first: dict):
        self.id = str(first.get("id") or "")
        self.n = first.get("n")
        self.size = first.get("size")
        self.sha = str(first.get("sha") or "")
        self.name = str(first.get("name") or "")[:128]
        if not _ID_RE.fullmatch(self.id) or not _SHA_RE.fullmatch(self.sha):
            raise RelayError("передача без опознавательных знаков")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or not 0 < self.size <= MAX_BYTES:
            raise RelayError("бэкап больше 20 МБ")
        if not isinstance(self.n, int) or isinstance(self.n, bool) or self.n != -(-self.size // PART):
            raise RelayError("число частей не сходится с размером")
        self.parts: dict[int, bytes] = {}

    def same(self, msg: dict) -> bool:
        return str(msg.get("id") or "") == self.id

    def add(self, msg: dict) -> None:
        i = msg.get("i")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < self.n:
            raise RelayError("часть вне передачи")
        if msg.get("n") != self.n or msg.get("size") != self.size or msg.get("sha") != self.sha:
            raise RelayError("части разных передач")
        try:
            raw = base64.b64decode(str(msg.get("z") or ""), validate=True)
        except ValueError as e:
            raise RelayError("часть повреждена") from e
        want = PART if i < self.n - 1 else self.size - PART * (self.n - 1)
        if len(raw) != want:
            raise RelayError("часть не того размера")
        if i in self.parts and self.parts[i] != raw:
            raise RelayError("повтор части с другим содержимым")
        self.parts[i] = raw

    @property
    def done(self) -> bool:
        return len(self.parts) == self.n

    def blob(self) -> bytes:
        data = b"".join(self.parts[i] for i in range(self.n))
        if len(data) != self.size or hashlib.sha256(data).hexdigest() != self.sha:
            raise RelayError("бэкап пришёл повреждённым")
        return data
