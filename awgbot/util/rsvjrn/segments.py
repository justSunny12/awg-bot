"""
segments.py — часть пакета rsvjrn (см. __init__): записи журнала на диске —
сегменты событий и файлы пачек счётчиков.

Каталог (у резерва `reserve/jrn/`, у бота основы на резерве — `jrn/` его
каталога на диске) хранит только шифротекст под `K` с открытыми заголовками
(event): `ev-<эпоха>-<первый номер>.seg` — события подряд, новый сегмент на
смене эпохи и после SEG_MAX байт; `cnt-<сторона>.seg` — пачки счётчиков
стороны. Дописка — с fsync файла (и каталога при новом файле): подтверждение
(`jack`) уходит только после него. Оборванная последняя запись (пропало
питание посреди записи) при чтении отбрасывается, при следующей дописке
файл до неё обрезается.

Компактизация по снимку: события с номером не больше номера снимка и пачки,
вошедшие в снимок, — прочь (сегмент целиком или переписью остатка через
tmp + replace).
"""
from __future__ import annotations

import os
from pathlib import Path

from .event import RecordError, head

SEG_MAX = 1024 * 1024


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _scan(data: bytes) -> tuple[list[bytes], int]:
    """(целые записи, длина целой части): оборванный или испорченный хвост — не запись."""
    out, p = [], 0
    while p < len(data):
        try:
            h = head(data, p)
        except RecordError:
            break
        if p + h.size > len(data):
            break
        out.append(data[p:p + h.size])
        p += h.size
    return out, p


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class Store:
    """Каталог записей журнала одной линии."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ── файлы ──────────────────────────────────────────────────────────────
    def _segments(self) -> list[tuple[int, int, Path]]:
        """[(эпоха, первый номер, путь)] по порядку номеров."""
        out = []
        try:
            names = os.listdir(self.path)
        except OSError:
            return []
        for n in names:
            if not (n.startswith("ev-") and n.endswith(".seg")):
                continue
            try:
                e, first = n[3:-4].split("-")
                out.append((int(e), int(first), self.path / n))
            except ValueError:
                continue
        return sorted(out, key=lambda x: (x[1], x[0]))

    @staticmethod
    def _read(path: Path) -> tuple[list[bytes], int, int]:
        """(целые записи, длина целой части, длина файла)."""
        try:
            data = path.read_bytes()
        except OSError:
            return [], 0, 0
        recs, good = _scan(data)
        return recs, good, len(data)

    def _append(self, path: Path, records: list[bytes]) -> None:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not path.exists()
        if not new:
            _recs, good, size = self._read(path)
            if good != size:                                 # оборванный хвост прошлой дописки — до него
                with open(path, "r+b") as f:
                    f.truncate(good)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(b"".join(records))
            f.flush()
            os.fsync(f.fileno())
        if new:
            _fsync_dir(self.path)

    # ── события ────────────────────────────────────────────────────────────
    def append_events(self, records: list[bytes]) -> None:
        """События подряд, номера по возрастанию (проверку порядка делает вызывающий)."""
        if not records:
            return
        e = head(records[0]).e
        if any(head(r).e != e for r in records):
            raise RecordError("в одной дописке события разных эпох")
        segs = self._segments()
        last = segs[-1] if segs else None
        if last is None or last[0] != e or last[2].stat().st_size >= SEG_MAX:
            path = self.path / f"ev-{e}-{head(records[0]).n}.seg"
        else:
            path = last[2]
        self._append(path, records)

    def events(self, after: int = 0) -> list[bytes]:
        """Записи событий с номером больше after — по порядку номеров."""
        out = []
        for _e, _first, path in self._segments():
            recs, _good, _size = self._read(path)
            out.extend(r for r in recs if head(r).n > after)
        return sorted(out, key=lambda r: head(r).n)

    def last(self) -> tuple[int, int]:
        """(эпоха, номер) последнего события; пусто — (0, 0)."""
        segs = self._segments()
        for _e, _first, path in reversed(segs):
            recs, _good, _size = self._read(path)
            if recs:
                h = head(recs[-1])
                return h.e, h.n
        return 0, 0

    def drop_events_upto(self, upto: int) -> int:
        """Снимок держит всё по номер upto включительно — события до него прочь; число снятых."""
        dropped = 0
        for _e, _first, path in self._segments():
            recs, _good, _size = self._read(path)
            keep = [r for r in recs if head(r).n > upto]
            dropped += len(recs) - len(keep)
            if not keep:
                try:
                    path.unlink()
                except OSError:
                    pass
            elif len(keep) != len(recs):
                newp = path.with_name(f"ev-{head(keep[0]).e}-{head(keep[0]).n}.seg")
                _write_atomic(newp, b"".join(keep))
                if newp != path:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        _fsync_dir(self.path)
        return dropped

    def events_size(self) -> int:
        size = 0
        for _e, _first, path in self._segments():
            try:
                size += path.stat().st_size
            except OSError:
                pass
        return size

    # ── пачки счётчиков ────────────────────────────────────────────────────
    def _cnt(self, side: str) -> Path:
        return self.path / f"cnt-{side}.seg"

    def append_counts(self, side: str, records: list[bytes]) -> None:
        if records:
            self._append(self._cnt(side), records)

    def counts(self, side: str, after: int = 0) -> list[bytes]:
        recs, _good, _size = self._read(self._cnt(side))
        return [r for r in recs if head(r).n > after]

    def counts_last(self, side: str) -> int:
        recs = self.counts(side)
        return max((head(r).n for r in recs), default=0)

    def drop_counts_upto(self, side: str, upto: int) -> None:
        path = self._cnt(side)
        recs, _good, _size = self._read(path)
        keep = [r for r in recs if head(r).n > upto]
        if len(keep) == len(recs):
            return
        if keep:
            _write_atomic(path, b"".join(keep))
        else:
            try:
                path.unlink()
            except OSError:
                pass
            _fsync_dir(self.path)

    # ── целиком ────────────────────────────────────────────────────────────
    def wipe(self) -> None:
        """Всё прочь (пара снята, возврат, новая линия)."""
        try:
            names = os.listdir(self.path)
        except OSError:
            return
        for n in names:
            if n.endswith((".seg", ".tmp")):
                try:
                    os.unlink(self.path / n)
                except OSError:
                    pass
        _fsync_dir(self.path)
