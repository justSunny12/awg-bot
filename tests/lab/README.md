# Стенд резерва серверов AWG

Полная картина пары серверов на одной Linux-машине: сервер A, сервер B, шлюзы
основного сервера G1 и G2, шлюз резерва GR, «интернет» (маршрутизатор с NAT),
«РФ-сайт» и клиенты cA и cB — каждый в своём сетевом пространстве (`lab-<узел>`).
«Публичные» адреса — 203.0.113.0/24 и 198.51.100.0/24, сети шлюзов и клиентов —
за NAT маршрутизатора; топология — `topo.sh`.

На стенде проверяют то, чего не проверить юнит-тестами: модуль AmneziaWG в
пространствах, аварии сети, переезд. В поставку хостов стенд не входит и
установщиком не ставится; его прогон — перед предрелизной сборкой.

## Виртуальная машина

Нужен Linux с тем же ядром и модулем, что на хостах: Debian 13, модуль
AmneziaWG — сборкой по манифесту `install/awg.lock`. На macOS — Lima:

```sh
limactl create --name=awg-lab --cpus=4 --memory=6 --disk=40 \
    --mount=<путь к репозиторию>:w template:debian-13
limactl start awg-lab
limactl shell awg-lab
```

Каталог репозитория виден в ВМ по тому же пути. Один раз внутри ВМ:

```sh
cd <путь к репозиторию>
sudo install/awg-kernel-install.sh install     # модуль и amneziawg-tools по awg.lock
python3 -m venv ~/venv
~/venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Прогон

```sh
sudo -E tests/lab/run.sh                    # самопроверка, топология, сценарии, набор тестов
sudo -E tests/lab/run.sh --no-suite         # без набора тестов
sudo -E tests/lab/run.sh --only s00_topology
```

`run.sh` по порядку: `install/reserve-zone-setup.sh --selftest`, подъём
топологии (`lab.sh up`), сценарии `scenarios/*.sh` (перед каждым — `net.sh
heal`; сценарий печатает причину и выходит не нулём, если не прошёл), снятие
топологии и затем весь набор тестов из `~/venv` того, кто запустил `sudo`
(`TMPDIR` — `/var/tmp/awglab-tmp`, не tmpfs): обвязки идут на GNU sed и
coreutils, как на хостах. Итог — «пройдено N, провалено M», код не ноль при
любом провале.

## Скрипт зоны

`install/reserve-zone-setup.sh` можно звать и отдельно:

- `--plan` (без root) — что умеет хост: `store` (tmpfs без свопа с ядра 6.4,
  иначе ramfs), `cake`, `netns`, `awg`, свободный порт вебхука `webhook_port`
  из 443/80/88/8443, `zone` — есть ли уже зона;
- `--selftest` (root) — на установленном модуле проверяет две опоры схемы
  зоны: UDP-сокет интерфейса остаётся у хоста после переноса интерфейса в
  пространство и переименования (`RESULT_SOCKET_AT_HOST`), и `awg set …
  persistent-keepalive 25-35` принимает диапазон (`RESULT_KEEPALIVE_RANGE`);
  `RESULT_TUNNEL_PING` — туннель через перенесённый интерфейс живой. Код 0 —
  обе опоры на месте. Временные пространства и интерфейсы снимаются при любом
  выходе.

## Ручная работа

```sh
sudo tests/lab/lab.sh up | down | status
sudo tests/lab/lab.sh exec G1 ping -c1 203.0.113.5
```

Рычаги аварий (`net.sh`) — правила nft в пространстве маршрутизатора, узлы о
них не знают:

| команда | что делает |
|---|---|
| `net.sh kill <узел>` | узел «умер»: весь его трафик отброшен |
| `net.sh cut <узел1> <узел2>` | разрезать путь между двумя узлами в обе стороны |
| `net.sh block-rf <узел>` | узел недоступен из частных сетей (блокировка из РФ) |
| `net.sh heal` | снять все рычаги |
| `net.sh show` | действующие рычаги |

Макет Bot API (`botapi.py`) — несколько ботов, `getUpdates` с подтверждениями,
вебхук с `secret_token` и сертификатом, отказ 409 при активном вебхуке,
отправки — в журнал JSONL; боты стенда ходят в него через
`TelegramAPIServer.from_base`:

```sh
~/venv/bin/python -m tests.lab.botapi --listen 127.0.0.1:8081 --journal /var/tmp/botapi.jsonl
```

Служебное: `POST /inject/<токен>` — положить боту обновление, `GET
/journal/<токен>` — его журнал.
