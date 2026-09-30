"""nft одним способом: запуск с таймаутом и разбор элементов `nft -j`.

Три владельца таблиц (сторож ВПС, обвязка шлюза, учёт РФ-трафика) держали по
копии запуска и разбора элемента; ошибки у каждого свои — класс передаётся.
"""
from __future__ import annotations

import subprocess


def run(args: list[str], *, timeout: int = 10, stdin: str = "",
        error: type[Exception] = RuntimeError) -> subprocess.CompletedProcess:
    """`nft <args>`; нет бинарника, таймаут или ошибка ОС — исключение error."""
    try:
        return subprocess.run(["nft", *args], input=stdin.encode() if stdin else None,
                              capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise error("nft не найден — поставь пакет nftables")
    except subprocess.TimeoutExpired:
        raise error(f"таймаут nft {' '.join(args[:3])}")
    except OSError as e:
        raise error(str(e))


def elem_str(el) -> str:
    """Элемент набора из JSON nft → строка: «1.2.3.0/24», имя интерфейса, порт."""
    if isinstance(el, str):
        return el
    if isinstance(el, dict):
        if "prefix" in el:
            return f"{el['prefix']['addr']}/{el['prefix']['len']}"
        if "elem" in el:                       # {"elem": {"val": ..., ...}}
            inner = el["elem"]
            return elem_str(inner.get("val") if isinstance(inner, dict) else inner)
        if "val" in el:
            return elem_str(el["val"])
    return str(el)


def ifs(names: list[str]) -> str:
    """Список имён интерфейсов литералом набора: { "awg0", "awglink" }."""
    return "{ " + ", ".join(f'"{n}"' for n in names) + " }"
