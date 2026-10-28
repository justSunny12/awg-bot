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

from .event import HEAD, RecordError, head

HEAD_SIZE = HEAD.size

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


def _scan(data: bytes) -> tuple[list[bytes], int, bool]:
    """(целые записи, длина целой части, хвост оборван): оборванный хвост — заголовок цел,
    а файл короче заявленной длины (пропало питание посреди дописки); мусор посреди
    файла — тоже конец чтения, но не «оборван»: его не обрезают."""
    out, p = [], 0
    while p < len(data):
        try:
            h = head(data, p)
        except RecordError:
            return out, p, len(data) - p < HEAD_SIZE
        if p + h.size > len(data):
            return out, p, True
        out.append(data[p:p + h.size])
        p += h.size
    return out, p, False


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
                out.append((int(e), int(first.split(".")[0]), self.path / n))   # «.N» — запасной файл рядом с испорченным
            except ValueError:
                continue
        return sorted(out, key=lambda x: (x[1], x[0]))

    @staticmethod
    def _read(path: Path) -> list[bytes]:
        """Целые записи файла (оборванный хвост и мусор — не записи)."""
        try:
            data = path.read_bytes()
        except OSError:
            return []
        return _scan(data)[0]

    def _append(self, path: Path, records: list[bytes]) -> Path:
        """Дописать в path; вернуть файл, куда легло. Оборванный хвост — обрезать до него;
        мусор посреди файла — не трогать (там могут быть подтверждённые записи), писать
        в новый файл рядом."""
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            try:
                data = path.read_bytes()
            except OSError:
                data = b""
            recs, good, torn = _scan(data)
            if good != len(data):
                if torn:
                    with open(path, "r+b") as f:
                        f.truncate(good)
                else:
                    path = path.with_name(f"{path.stem}.{len(data)}{path.suffix}")
        new = not path.exists()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as f:
            f.write(b"".join(records))
            f.flush()
            os.fsync(f.fileno())
        if new:
            _fsync_dir(self.path)
        return path

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
        """Записи событий с номером больше after — по порядку номеров, без повторов
        (повтор дописки после пропажи питания: первая запись номера остаётся)."""
        seen: dict[int, bytes] = {}
        for _e, _first, path in self._segments():
            for r in self._read(path):
                n = head(r).n
                if n > after and n not in seen:
                    seen[n] = r
        return [seen[n] for n in sorted(seen)]

    def last(self) -> tuple[int, int]:
        """(эпоха, номер) последнего события; пусто — (0, 0)."""
        segs = self._segments()
        for _e, _first, path in reversed(segs):
            recs = self._read(path)
            if recs:
                h = head(recs[-1])
                return h.e, h.n
        return 0, 0

    def drop_events_upto(self, upto: int) -> int:
        """Снимок держит всё по номер upto включительно — события до него прочь; число снятых."""
        dropped = 0
        for _e, _first, path in self._segments():
            recs = self._read(path)
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

    def _cnt_files(self, side: str) -> list[Path]:
        """Файл пачек стороны и запасные рядом с испорченным (`cnt-a.N.seg`) — по порядку."""
        try:
            names = os.listdir(self.path)
        except OSError:
            return []
        files = [n for n in names if n.startswith(f"cnt-{side}") and n.endswith(".seg")]
        return [self.path / n for n in sorted(files, key=lambda n: (len(n), n))]

    def append_counts(self, side: str, records: list[bytes]) -> None:
        if records:
            files = self._cnt_files(side)
            self._append(files[-1] if files else self._cnt(side), records)

    def counts(self, side: str, after: int = 0) -> list[bytes]:
        """Пачки стороны с номером больше after — по порядку номеров, без повторов."""
        seen: dict[int, bytes] = {}
        for path in self._cnt_files(side):
            for r in self._read(path):
                n = head(r).n
                if n > after and n not in seen:
                    seen[n] = r
        return [seen[n] for n in sorted(seen)]

    def counts_last(self, side: str) -> int:
        return max((head(r).n for r in self.counts(side)), default=0)

    def drop_counts_upto(self, side: str, upto: int) -> None:
        keep = self.counts(side, after=upto)
        files = self._cnt_files(side)
        if not files or len(keep) == sum(len(self._read(f)) for f in files):
            return
        if keep:
            _write_atomic(self._cnt(side), b"".join(keep))
        for f in files:
            if f != self._cnt(side) or not keep:
                try:
                    f.unlink()
                except OSError:
                    pass
        _fsync_dir(self.path)

    # ── целиком ────────────────────────────────────────────────────────────
    def wipe(self) -> None:
        """Всё прочь (пара снята, возврат, новая линия), кроме `meta.json`: эпоха линии,
        известная резерву, только растёт — гость не откроется с эпохой ниже уже виденной
        (секретов в ней нет; новый снимок перепишет)."""
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
