#!/usr/bin/env python3
"""
tools/gwkit.py — `awg-bot gw-kit --stdout` на сервере AWG: поставка для
шлюза без доступа к GitHub (§3.11.5 концепта «единые обновления»).

    python -m tools.gwkit --stdout     кит одним sh-файлом — в stdout

Внутри — код агента той же версии, что у сервера (util/dist: тот же состав,
что у выпуска), и исходники AmneziaWG из кэша сервера
(/var/lib/awg-bot/awg-src, их туда кладёт awg-kernel-install.sh) — ровно те,
что прибиты к поставке install/awg.lock, со сверкой sha256. Ни ключей, ни
токенов, ни адресов в ките нет: трубу можно гнать через любой компьютер.
Подключение — той же командой из бота сервера и тем же кодом:

    sudo awg-bot gw-kit --stdout | ssh root@ШЛЮЗ \\
        'cat > /root/awg-gw-kit.sh && sh /root/awg-gw-kit.sh --join <токен AWGJ1…>'

На шлюзе кит раскладывает исходники AmneziaWG в тот же кэш (установщик ядра
сверит их с awg.lock ещё раз и в сеть не пойдёт), распаковывает поставку во
временный каталог и зовёт её установщик с `--skip-verify --join <токен>`
(своя сборка: sha256 выпуска к ней не подходит). Передаётся только токен
`AWGJ1.…` из команды бота: вся строка с `| sudo bash` в кавычках не
сработает. Терминала у такой трубы нет (stdin занят китом) — вопросов
установщик не задаёт: агент уже стоит — нужен `--yes`.
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

SRC_CACHE = Path("/var/lib/awg-bot/awg-src")
MARK = "#__AWG_KIT_FILE__ "
END = "#__AWG_KIT_END__"


class KitError(RuntimeError):
    pass


def _lock(root: Path) -> dict:
    """install/awg.lock поставки: KEY=VALUE."""
    out = {}
    try:
        text = (root / "install" / "awg.lock").read_text(encoding="utf-8")
    except OSError as e:
        raise KitError(f"нет install/awg.lock в установке: {e}")
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def awg_sources(root: Path, cache: Path = SRC_CACHE) -> list[tuple[str, bytes]]:
    """[(имя тарбола в кэше, байты)] — модуль и тулзы по awg.lock, sha256 сверен."""
    lock = _lock(root)
    want = []
    try:
        want.append((f"amneziawg-tools-{lock['AWG_TOOLS_TAG'].lstrip('v')}.tar.gz", lock["AWG_TOOLS_SHA256"]))
        want.append((f"amneziawg-linux-kernel-module-{lock['AWG_MODULE_TAG'].lstrip('v')}.tar.gz",
                     lock["AWG_MODULE_SHA256"]))
    except KeyError as e:
        raise KitError(f"в awg.lock нет {e}")
    out = []
    for name, sha in want:
        try:
            blob = (cache / name).read_bytes()
        except OSError:
            raise KitError(f"исходников AmneziaWG нет в кэше сервера ({cache / name}) — "
                           "собери ядро на сервере: sudo awg-bot awg install")
        if hashlib.sha256(blob).hexdigest() != sha.lower():
            raise KitError(f"{name} в кэше сервера не совпал с awg.lock по sha256 — "
                           "пересобери ядро на сервере: sudo awg-bot awg install")
        out.append((name, blob))
    return out


def _payload(name: str, blob: bytes) -> str:
    return MARK + name + "\n" + base64.encodebytes(blob).decode("ascii") + END + "\n"


HEAD = r'''#!/bin/sh
# awg-gw-kit.sh — поставка awg-bot v{version} для шлюза без доступа к GitHub:
# код агента и исходники AmneziaWG (по install/awg.lock). Ключей и токенов
# внутри нет. Запуск:  sudo sh awg-gw-kit.sh --join <токен AWGJ1… из команды бота сервера AWG> [--yes]
set -eu
[ "$(id -u)" = "0" ] || {{ echo "нужен root: sudo sh $0 --join <токен AWGJ1…>"; exit 1; }}
_token=""; _yes=""
for _a in "$@"; do
    case "$_a" in AWGJ1.*) _token="$_a" ;; --yes) _yes="--yes" ;; esac
done
[ -n "$_token" ] || {{ echo "нужен токен подключения: sudo sh $0 --join <токен AWGJ1… из команды бота сервера AWG>"; exit 2; }}
for _t in base64 tar awk; do
    command -v "$_t" >/dev/null 2>&1 || {{ echo "нужен $_t"; exit 1; }}
done
_self="$0"
_take() {{  # _take ИМЯ — блок кита в stdout
    awk -v n="{mark}$1" '$0 == n {{ f = 1; next }} $0 == "{end}" {{ f = 0 }} f' "$_self" | base64 -d
}}
_cache="{cache}"
mkdir -p "$_cache"; chmod 700 "$_cache"
for _n in {sources}; do
    _take "$_n" > "$_cache/$_n.part" && mv -f "$_cache/$_n.part" "$_cache/$_n"
done
_d="$(mktemp -d /tmp/awg-bot-install.XXXXXX)"
trap 'rm -rf "$_d"' EXIT
_take awg-bot.tgz > "$_d/awg-bot.tgz"
tar xzf "$_d/awg-bot.tgz" -C "$_d" --no-same-owner
rm -f "$_d/awg-bot.tgz"
[ -f "$_d/install/awg-bot-install.sh" ] || {{ echo "кит повреждён: в поставке нет установщика"; exit 1; }}
set +e
bash "$_d/install/awg-bot-install.sh" --skip-verify $_yes --join "$_token"
_rc=$?
exit "$_rc"
'''


def build(root: Path | None = None, cache: Path = SRC_CACHE) -> bytes:
    """Кит одним sh-файлом."""
    from awgbot.util import dist
    root = (root or dist.install_root()).resolve()
    sources = awg_sources(root, cache)
    try:
        code = dist.archive(root)
    except dist.DistError as e:
        raise KitError(str(e))
    ver = ""
    try:
        import re
        m = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "awgbot" / "__version__.py").read_text(encoding="utf-8"))
        ver = m.group(1) if m else ""
    except OSError:
        pass
    head = HEAD.format(version=ver or "?", mark=MARK, end=END, cache=str(cache),
                       sources=" ".join(name for name, _ in sources))
    body = [head, "exit 0\n"]
    body += [_payload(name, blob) for name, blob in sources]
    body.append(_payload("awg-bot.tgz", code))
    return "".join(body).encode("utf-8")


def join_version() -> str:
    """Выпуск, который назовёт токен команды подключения (Р14): версия агента в
    выпуске набора, без набора — версия сервера; база не читается — пусто
    (сверка остаётся установщику на шлюзе)."""
    from awgbot.core import config
    if not Path(config.DB_PATH).exists():
        return ""
    try:
        from awgbot.domain.services import Services
        from awgbot.infra.db import Database
        return Services(Database(config.DB_PATH))._gwjoin_version()
    except Exception:                                     # noqa: BLE001
        return ""


def check_version(kit: str, want: str) -> None:
    """Кит везёт код сервера: команда подключения называет другой выпуск (набор
    ведёт шлюзы на свою версию агента) — шлюз встал бы не на неё (flow-5)."""
    if want and kit and want != kit:
        raise KitError(f"команда подключения ставит агента v{want} (выпуск набора), а кит собирается из кода "
                       f"сервера v{kit} — шлюз встал бы не на версию набора. Кит годится, когда сервер и "
                       "шлюзы на одной версии: доведи обновление набора и собери кит заново")


def main(argv: list[str]) -> int:
    if argv[:1] != ["--stdout"]:
        print("usage: awg-bot gw-kit --stdout | ssh root@ШЛЮЗ 'cat > /root/awg-gw-kit.sh && "
              "sh /root/awg-gw-kit.sh --join <токен AWGJ1…>'", file=sys.stderr)
        return 2
    if sys.stdout.isatty():
        print("[awg-bot:ОШИБКА] кит — двоичный поток: направь его в ssh или в файл", file=sys.stderr)
        return 2
    try:
        from awgbot.core import config
        check_version(str(config.INSTALLED_VERSION).lstrip("v"), join_version())
        blob = build()
    except KitError as e:
        print(f"[awg-bot:ОШИБКА] {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(blob)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
