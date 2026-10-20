"""
netns.py — сетевые пространства Linux из процесса бота; основной бот (резерв).

Два приёма, которые нужны гостевой зоне (`awg-guest`), не переводя сам процесс
бота в пространство:

- `listen_in(path, make)` — создать слушающий сокет внутри пространства.
  Переход `setns` действует только на вызвавший поток, поэтому сокет создаёт
  отдельный поток: входит в пространство, зовёт `make()` (там — `socket()` и
  `bind()`), возвращается в своё пространство и отдаёт сокет. Сокет остаётся
  принадлежать пространству, где создан, — так же, как UDP-сокет интерфейса
  WireGuard остаётся у хоста после переноса интерфейса в зону.
- `run_in(path, argv, …)` — команда внутри пространства (`ip netns exec`
  без зависимости от имени: по пути к файлу пространства через `nsenter`).

На не-Linux (тесты на macOS) `listen_in` недоступен: `NetnsError`.
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Callable, TypeVar

T = TypeVar("T")

NETNS_DIR = "/run/netns"
_CLONE_NEWNET = 0x40000000


class NetnsError(RuntimeError):
    """Пространство недоступно: нет файла, нет права, не Linux."""


def path_of(name: str) -> str:
    """Путь к файлу именованного пространства (`ip netns add`)."""
    return os.path.join(NETNS_DIR, str(name))


def listen_in(path: str, make: Callable[[], T]) -> T:
    """Выполнить `make()` в пространстве `path` и вернуть результат (сокет).
    Поток возвращается в своё пространство в любом случае."""
    setns = getattr(os, "setns", None)
    if setns is None:
        raise NetnsError("сетевые пространства — только Linux")
    result: dict = {}

    def body() -> None:
        try:
            own = os.open("/proc/self/task/%d/ns/net" % threading.get_native_id(), os.O_RDONLY)
        except OSError as e:
            result["error"] = NetnsError(f"своё пространство не открылось: {e}")
            return
        try:
            try:
                target = os.open(path, os.O_RDONLY)
            except OSError as e:
                result["error"] = NetnsError(f"пространство {path} недоступно: {e}")
                return
            try:
                setns(target, _CLONE_NEWNET)
            except OSError as e:
                result["error"] = NetnsError(f"вход в {path} не удался: {e}")
                return
            finally:
                os.close(target)
            try:
                result["value"] = make()
            except Exception as e:  # noqa: BLE001 — любая ошибка make — наружу как есть
                result["error"] = e
            finally:
                setns(own, _CLONE_NEWNET)
        finally:
            os.close(own)

    t = threading.Thread(target=body, name="netns-listen", daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def run_in(path: str, argv: list[str], timeout: float = 10, input_data: bytes | None = None,
           check: bool = False) -> subprocess.CompletedProcess:
    """Команда внутри пространства по пути к его файлу."""
    return subprocess.run(["nsenter", f"--net={path}", *argv], capture_output=True, timeout=timeout,
                          input=input_data, check=check)


def exists(path: str) -> bool:
    return os.path.exists(path)
