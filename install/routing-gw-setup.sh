#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# routing-gw-setup.sh — сторона ШЛЮЗА (устройства) для условной маршрутизации.
# Запускается НА ШЛЮЗЕ.
#
# КОНТЕКСТ. Линк поднимается awg/awg-quick хоста — модуль ядра amneziawg
# живёт на хосте, и версия утилит обязана совпадать с ним.
#
# Интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ сами — переопределяются
# переменными WAN_IF / HOST_CONF_DIR. WAN_IF — токен после dev в маршруте по
# умолчанию.
#
# ЧТО ДЕЛАЕТ:
#   1) кладёт конфиг линка, грузит прежнюю таблицу защиты (если есть — окно
#      между подъёмом линка и свежей таблицей закрыто) и поднимает интерфейс;
#   2) ставит ОДНУ nft-таблицу inet awg_gw_guard — всю обвязку разом:
#      MASQUERADE клиентов в WAN_IF (ради этого всё и затевалось: российские
#      сервисы увидят российский адрес шлюза; WAN_IF — интерфейс маршрута по
#      умолчанию на момент прогона, обычно eth0: маршрут переехал — агент видит
#      это проверкой «маскарад клиентов» и перевыставляет обвязку); изоляцию клиентов от локальной
#      сети (нужны НОВЫЕ соединения из туннеля, а это открывает путь к NAS,
#      роутеру, торрент-клиенту — закрываем); метки Telegram для самого агента;
#      MSS-кламп в аплинк (UPLINK_IF) — своей цепочкой postrouting_mss (hook
#      postrouting, priority mangle), НЕ в forward: в postrouting виден и
#      собственный трафик устройства (агент к Telegram и GitHub по метке),
#      которому без клампа нужен теряющийся ICMP «fragmentation needed»; и защиту
#      САМОГО УСТРОЙСТВА от туннеля: с адресов туннеля на него пускается только ВПС
#      по линку (SSH, ICMP), всё прочее дропается. Исключение — УСТРОЙСТВА
#      АДМИНА (ADMIN_IPS, приезжают с ВПС документом конфигурации по каналу —
#      агент пишет их в юнит): им открыто всё, и шлюз, и локальная сеть через него.
#      При LAN_MODE=1 и непустом PEER_LAN_NETS — ещё маскарад соседей к
#      роутеру: трафик из подсетей других шлюзов к адресу роутера (ROUTER4 —
#      шлюз маршрута по умолчанию, `via`, если он внутри LAN_SUBNETS; в файле
#      статуса — ROUTER=) уходит с
#      адресом шлюза в локальной сети, и роутер отвечает шлюзу, а не мимо
#      заворота провайдеру; маршрутов на соседей на роутере не нужно. Адрес
#      сменился (DHCP шлюза) — подтягивается следующим прогоном, агент
#      сверяет правило с маршрутом по умолчанию проверкой «маскарад к роутеру»
#      и перевыставляет обвязку; шлюза у маршрута нет или он вне своих
#      подсетей — правила нет;
#   3) снимает прежнюю обвязку в iptables (AWGLINK_FWD, MASQUERADE, метки);
#      при политике DROP в чужой ip filter FORWARD вставляет в неё
#      ACCEPT своим интерфейсам (линк, аплинк, локальная сеть) — п.3a; по ним
#      агент и судит, что красной проверки «политика FORWARD» нет;
#   4) автозапуск: юнит зовёт этот же скрипт, таблица ставится ДО подъёма линка;
#      юнит переписывается целиком на каждом запуске и ставится с правами 0600
#      (TimeoutStartSec=15min — полный прогон с apt на загрузке идёт минуты;
#      RestartSec=60 — постоянный отказ не крутит apt/nft каждые 10 с);
#      daemon-reload — только если текст юнита изменился, enable — только если
#      юнит ещё не включён. Аплинку — оверрайд awg-quick@<аплинк>.service.d/
#      awg-gw.conf (сверяется по содержимому): StartLimitIntervalSec=0,
#      Restart=on-failure и ExecStartPost=try-restart dnsmasq — перечитать
#      апстрим @<аплинк> после его подъёма;
#   5) при LAN_MODE=1 — VPN-транзит (локальная сеть за шлюзом без VPN на
#      устройствах): dnsmasq на адресе шлюза в локальной сети (bind-dynamic, апстрим
#      через АПЛИНК, NXDOMAIN на DoH-эндпоинты), ВТОРАЯ таблица inet awg_lan
#      (наборы lan_vpn4/lan_vpn_nets4/lan_ru4, метка в туннель, маскарад в
#      локальную сеть) и скрипты списков в /usr/local/sbin — awg-lan-lists.sh
#      (фиды; исходник до вычитания исключений — /var/lib/awg-gw/vpn-feed.src),
#      awg-lan-domain.sh (свои списки: add|ru|del|list — их же зовёт awg-bot lan;
#      AWG_HTML=1 — домены в строках итога в <code>, его ставит агент для чата,
#      в терминале — голый текст;
#      sync <файл> — полный список, fill <файл> — адреса новых доменов «в
#      туннель» в lan_vpn4 без блокировки, оба зовёт только агент; одна блокировка
#      lists.lock с фидами, исключения «напрямую» вычитаются из фида заново,
#      наборы чистятся) и
#      awg-lan-services.sh (записи сервисов соседних сетей для dnsmasq —
#      сервисы соседних сетей: SMB-серверы сети другого шлюза видны
#      в Finder через домен обзора awg.internal; PEER_LAN_NETS пуст — файл
#      awg-gw-peer-services.conf снимается). Обзор своей сети — вне режима:
#      при своих подсетях (LAN_SUBNETS) и живом avahi-daemon ставится
#      avahi-utils (после apt-get update) — имена этой сети нужны и соседним
#      шлюзам, и устройствам админа в туннеле («SMB в туннеле» на ВПС).
#      awg-lan-services.sh <файл> ставит записи, собранные агентом из данных
#      канала: каждая строка — по белому списку шаблонов (LINE_RES в
#      awgbot/domain/gwservices.py), dnsmasq --test, рестарт с откатом, та же
#      блокировка lists.lock; без аргумента — снять; rc=2 — строка вне списка.
#      Фид доменов принимается только строками nftset=, где каждый сегмент —
#      имя домена (одиночный `#` или `.` у dnsmasq значат «все домены»);
#      подсети — с префиксом не короче /8.
#      awg-lan-lists.sh с AWG_LAN_FROM=<каталог> фиды не качает, а берёт
#      готовыми (domains.lst, nets.lst) — их привозит канал линка в
#      /var/lib/awg-gw/feed; в lists.status тогда source=channel, иначе net.
#      Скрипты awg-lan-*.sh пишутся во временный файл и подменяются mv:
#      запущенный экземпляр дочитывает свой прежний inode. Копии для отката
#      конфигов dnsmasq все три держат в /var/lib/awg-gw/rollback, НЕ в
#      conf-dir: Debian читает оттуда всё, кроме .dpkg-*, и копия грузилась бы
#      вместе с новым файлом.
#      Таблица пишется БЕЗ delete table: наборы наполняет dnsmasq на лету, и
#      реассерт обязан их сохранить — пересобираются только цепочки. Таблица
#      прежнего имени (inet awg_home, обвязка 3.3.0 и раньше) переезжает в
#      inet awg_lan на первом прогоне: наборы — слепками, строки nftset= — на
#      новое имя (снимается со следующим сдвигом минимума). Личные
#      списки при снятии уходят в /var/lib/awg-gw/restore, и следующее
#      включение берёт их оттуда; нет — пустые заготовки. Занят :53
#      не-dnsmasq — честный отказ ДО
#      apt, причина в статусе; остальная обвязка при этом стоит. Юниту
#      dnsmasq — оверрайд dnsmasq.service.d/awg-gw.conf: Restart=on-failure и
#      выключенный хук resolvconf Debian (если он в юните); БЕЗ After/Wants
#      awg-quick@<аплинк> — у dnsmasq Before=nss-lookup.target, у awg-quick@
#      After=nss-lookup.target, цикл упорядочивания; dnsmasq после аплинка
#      перечитывает оверрайд аплинка (п.4). Имена Endpoint аплинка и линка
#      резолвятся через DNS роутера (VIA4, шлюз маршрута по умолчанию) строками
#      server=/<имя>/<адрес> в awg-gw-base.conf — мимо ещё не поднятого
#      туннеля. Метка «dnsmasq ставили мы» (при снятии режима гасит демон) —
#      только если dnsmasq до этого не было. Включение и снятие режима идут
#      под lists.lock (ждут до 120 с): скрипт фидов пишет те же файлы. Фид
#      подсетей скачался не весь — набор lan_vpn_nets4 только дополняется, без
#      flush. apt — с
#      DPkg::Lock::Timeout=120 (OMV может держать dpkg) и force-confdef/confold
#      (терминала у юнита нет). awg-lan-lists.sh — один запуск за раз: ждёт
#      блокировку до 120 с, не дождался — код 75 «занято» (агент ждёт скрипты
#      списков и записей SMB 150 с — LAN_SCRIPT_TIMEOUT в infra/gwguard/common.py).
#   6) --link-apply <конфиг> — конфиг линка из документа конфигурации, который
#      сервер шлёт по каналу конфигурации (зовёт агент, domain/gateway/cfg/txn.py, после
#      того как переписал строки юнита). Второй рубеж после проверки агентом,
#      из того же источника: wg_conf_check читает белые списки ключей и пределы
#      правила AllowedIPs из install/gw-wg-allow.list поставки (его же читает
#      awgbot/util/wgconf.py); нет файла — отказ. AllowedIPs — только
#      CLIENT_SUBNET, /30 линка (по Address), PEER_LAN_NETS и /32 RESOLVER из
#      окружения; все частные, не шире allowed_min_prefix, не больше
#      allowed_max, без пересечений с LAN_SUBNETS и подсетями интерфейсов
#      шлюза. Разница с установленным только в AllowedIPs и PersistentKeepalive —
#      awg syncconf без перезапуска линка (сессия канала живёт) и правка
#      маршрутов в линк; иначе down/up с прежней таблицей защиты до подъёма.
#      Итог — последней строкой «link: unchanged|syncconf|restarted». Таблицы и
#      юнит этот режим не трогает: реассерт, пробу хендшейком и откат делает агент.
#      Тем же режимом агент ставит новые ключи линка при смене ключей каналом
#      (domain/gateway/cfg/rekey.py: /etc/awg-gw/rekey.next и rekey.prev).
#      Шлюз в сопряжении (юнита ещё нет): агент передаёт CLIENT_SUBNET,
#      PEER_LAN_NETS и RESOLVER из AllowedIPs нового конфига (их собрал сервер),
#      а возврат на конфиг по токену (только /30) — с LINK_CHECK_KIND=join:
#      проверка вида join, как у --join (п.8).
#   7) --uplink <конфиг> — конфиг аплинка из документа конфигурации (зовёт
#      агент, domain/gateway/cfg/uplinktxn.py: человек нажал «Применить» в чате
#      бота шлюза или сработало аварийное правило — агент сам 10 минут подряд
#      видит аплинк без хендшейка, а ключ устройства в конфиге — ключ признания).
#      Аплинк называет окружение UPLINK_IF (агент
#      его знает; пусто — отказ, код 2). Проверка — wg_conf_check по разделу
#      uplink того же gw-wg-allow.list и обязательный Table = off; затем то же,
#      что с UPLINK_B64 (ниже): PostUp/PostDown политики
#      «метка → таблица» дописываются в [Interface], подъём юнитом awg-quick@<аплинк>,
#      не поднялся — прежний конфиг назад, код 1. Итог — последней строкой
#      «uplink: unchanged|installed»; отказ до установки — «uplink: refused»
#      (проверка не прошла, копию прежнего не снять или новый не записать —
#      cp/install; прежний на месте). Копия прежнего — одна, <аплинк>.conf.before:
#      после пробы хендшейком (60 с, делает агент) --uplink-commit её убирает,
#      --uplink-revert ставит прежний назад байт в байт (без белого списка —
#      это файл с этого же диска) и поднимает аплинк; итог «uplink: committed|
#      reverted|unchanged». Новый не поднялся, и прежний при этом не вернулся
#      (запись) — копия .before остаётся для --uplink-revert. Оба режима итога
#      идут до определения WAN_IF: откату выход не нужен, и без маршрута по
#      умолчанию (аренда DHCP потеряна) они не отказывают. Порядок у агента:
#      журнал /etc/awg-gw/uplink.txn → uplink.next → ключи признания
#      (GATEWAY_PUBKEY, GATEWAY_PREV_PUBKEY) в юнит → --uplink → проба →
#      журнал прочь → --uplink-commit; прерванную транзакцию агент откатывает
#      сам. Всю транзакцию он держит замок обвязки (ниже) и зовёт режимы с
#      AWG_GW_LOCK_HELD=1.
#
# ЧЕГО НЕ ДЕЛАЕТ: не трогает существующие интерфейсы, прежнюю ручную схему
# маршрутизации и чужие правила iptables — они работают как
# работали (исключение — ACCEPT из п.3a). Политика INPUT для локальной сети
# остаётся accept. avahi-daemon не ставит (он начал бы объявлять шлюз) —
# только avahi-utils рядом с уже запущенным демоном и только при заданных своих
# подсетях (LAN_SUBNETS).
# /etc/default/dnsmasq не трогает: DNSMASQ_EXCEPT init-скрипт Debian превращает
# в except-interface, а тот перекрывает listen-address; файл, созданный до
# пакета, ронял бы dpkg.
#
# ОКРУЖЕНИЕ. Первую группу закрепляет юнит awg-link-gw строками Environment= —
# значения приезжают с ВПС. Первый полный прогон (конец подключения командой,
# JOIN_FINISH=1) зовёт агент с LINK_IF, LINK_CHANNEL_PORT и CLIENT_SUBNET,
# PEER_LAN_NETS, RESOLVER из конфига линка, который поставила смена ключей
# после кода; дальше сервер доставляет CLIENT_SUBNET, ADMIN_IPS, LAN_MODE,
# LAN_SUBNETS, RESOLVER, PEER_LAN_NETS и LINK_CHANNEL_PORT документом
# конфигурации по каналу (domain/gwcfg, ENV_KEYS — те же имена). Агент
# переписывает эти строки прямо в юните и перезапускает его. Ключи признания GATEWAY_PUBKEY /
# GATEWAY_PREV_PUBKEY агент с документом тоже пишет в юнит сам — вместе с
# конфигом аплинка (п.7).
# Отдельного файла для них нет намеренно —
# юнит единственный источник, и следующий полный прогон перепишет его
# целиком:
#   CLIENT_SUBNET   подсеть клиентов ВПС (MASQUERADE, изоляция)
#   LINK_IF         интерфейс линка (awglink, у второго слота awglink2)
#   ADMIN_IPS       устройства админа — полный доступ с туннеля
#   GATEWAY_PUBKEY / GATEWAY_PREV_PUBKEY
#                   ключ аплинка помеченного шлюза (и прежний ключ пары в окне
#                   переезда): по нему находится аплинк и проверяется, та ли
#                   это устройство — с чужим аплинком линк не поднимается
#   UPLINK_B64      конфиг аплинка из файла конфигурации прежних выпусков (base64;
#                   агент его не передаёт — аплинк ставит --uplink, п.7) — ТОЛЬКО на время
#                   применения: в юнит не закрепляется (внутри приватный
#                   ключ, а поставленный конфиг и так лежит в HOST_CONF_DIR с
#                   0600). Поднимается юнитом awg-quick@<аплинк> (не голым
#                   awg-quick); не поднялся — откат на прежний конфиг. Временный
#                   uplink.new — под umask 077, убирается при любом выходе.
#                   Реассерт из юнита аплинк не переставляет
#   LAN_MODE        1 — поднять VPN-транзит (см. п.5), 0 — снять своё
#   LAN_SUBNETS     локальные подсети шлюза: по первой он находит свой
#                   интерфейс и адрес в локальной сети
#   RESOLVER        адрес резолвера ВПС для апстрима; пусто — запасной 1.1.1.1
#   PEER_LAN_NETS   подсети других шлюзов — их префиксы в туннеле (/16 на
#                   слот, 10.241.0.0/16 и т. д.; переводит адреса только ВПС,
#                   таблица awg_lan_map), только у слотов с VPN-транзитом:
#                   им из линка открыт транзит в локальную сеть.
#                   Те же префиксы стоят в AllowedIPs конфига линка, поэтому
#                   они едут каналом вместе с конфигом линка одной
#                   транзакцией (п.6).
#                   Непуст — действуют и сервисы соседних сетей (п.5), а
#                   при LAN_MODE=1 — и маскарад их трафика к роутеру (п.2)
#   LINK_CHANNEL_PORT  порт канала на адресе ВПС в /30 линка (8787); канал
#                   обязателен — агент держит его, пока есть конфиг линка
#   BUNDLE_ISSUED_AT   метка выпуска файла конфигурации прежних выпусков
#                   (unix-время; новых файлов нет — шлюз подключается командой) —
#                   ТОЛЬКО при применении файла, в юнит не закрепляется: файл старее
#                   применённого (/etc/awg-gw/issued_at) — отказ, код 3, до
#                   первого шага; AWG_GW_ALLOW_OLD=1 — применить всё равно
#   JOIN_FINISH     1 — этот --apply завершает подключение командой (агент после
#                   смены ключей по коду, infra/gwguard/join.py): карантинная
#                   таблица --join (inet awg_gw_join, join.nft) снимается; без
#                   него карантин остаётся и перегружается из join.nft
#                   (прежний юнит посреди сопряжения). В юнит не закрепляется
# Юнит, записанный скриптом 3.3.0 и раньше, держит подсети под прежними
# именами HOME_SUBNETS / PEER_HOME_NETS: их скрипт читает, только если новых
# в окружении нет вовсе, и переписывает юнит уже с новыми — снимается со следующим сдвигом
# минимума.
# Значения окружения (юнит, агент, руками) проходят фильтр формы до первого использования:
# CLIENT_SUBNET — только цифры, точки и «/», вид a.b.c.d/n, иначе отказ;
# LINK_IF — [A-Za-z0-9_.-], не длиннее 15 символов; ADMIN_IPS и
# ADMIN_IPS_EXTRA — цифры, точки, «/» и пробелы; LAN_MODE — «1» или 0 (всё
# прочее — 0).
# В юните их нет — дефолт в скрипте, переопределяются окружением при ручном
# запуске: TG_MARK (метка трафика в туннель, 0x1), UPLINK_TABLE (таблица
# политики «метка → аплинк», 100), UPLINK_IF (имя аплинка на чистом устройстве,
# пока его не найти по ключу, awg0; в режиме --uplink обязателен), WAN_IF,
# HOST_CONF_DIR.
# Вторую группу юнит читает строкой EnvironmentFile=-/etc/awg-gw/firewall.env —
# это локальное состояние шлюза, его пишут агент и awg-bot (разделы файервола и
# доступа по SSH), документ с сервера его не трогает: ADMIN_IPS_EXTRA
# (доверенные из туннеля сверх ADMIN_IPS), SSH_PORT (факт: порт, который слушает
# sshd; 22), SSH_FILTER (1 — фильтр снаружи включён), SSH_ALLOW (адреса
# снаружи: IP/CIDR/имена), SSH_ALLOW_RESOLVED (последний резолв имён — агент).
# Фильтр снаружи (цепочка ssh_in) касается только порта SSH и только
# не-туннельных источников: локальная сеть, сервер и подсети других шлюзов
# (peer_nets4 — они приходят линком, но адрес у них не из tunnel_nets4)
# открыты всегда.
#
# ЗАПУСК:
#   sudo sh routing-gw-setup.sh                    # показать план
#   sudo sh routing-gw-setup.sh --apply gw-awglink.conf
#   sudo sh routing-gw-setup.sh --link-apply link.next   # только конфиг линка (п.6)
#   sudo UPLINK_IF=awg0 sh routing-gw-setup.sh --uplink uplink.next   # только аплинк (п.7)
#   sudo UPLINK_IF=awg0 sh routing-gw-setup.sh --uplink-commit|--uplink-revert  # итог пробы (п.7)
#   sudo sh routing-gw-setup.sh --rollback
#   sudo sh routing-gw-setup.sh --join link.conf   # линк по команде подключения (п.8)
# 8) --join <конфиг> — шлюз, подключаемый командой из бота сервера AWG (зовёт
#    `awg-bot join`, tools/join.py, скриптом из поставки — копии в
#    /usr/local/sbin ещё нет, режим её кладёт): конфиг линка по токену (тот же
#    второй рубеж wg_conf_check, вид join: ключи — по разделу link
#    gw-wg-allow.list, AllowedIPs — только /30 линка), карантинная таблица
#    inet awg_gw_join (/etc/awg-gw/join.nft) — пересылки из
#    линка и в линк нет, на сам шлюз с линка — только ответы на свои
#    соединения (канал открывает шлюз) — до подъёма линка, затем подъём. Ни
#    аплинка, ни VPN-транзита, ни dnsmasq, ни юнита. Полная обвязка — обычным
#    --apply после сопряжения (его зовёт агент на постоянных ключах линка с
#    JOIN_FINISH=1): он снимает карантинную таблицу; --apply без JOIN_FINISH
#    (юнит прежней установки посреди сопряжения) её оставляет. Итог —
#    последней строкой «join: linked».
# --rollback снимает и карантинную таблицу --join, и ACCEPT п.3a — со всех интерфейсов, кроме lo: имя
# интерфейса со времён применения могло смениться (br0, bond после OMV).
# Один прогон за раз: --apply, --link-apply, --uplink*, --join и --rollback (из юнита,
# от агента, руками) берут flock на /run/awg-gw-apply.lock и ждут соседа до
# 5 минут; не дождался — код 75. План (без аргументов) блокировку не берёт.
# AWG_GW_LOCK_HELD=1 — замок уже держит вызывающий (агент на транзакцию аплинка).
# Прогон юнита (AWG_GW_UNIT_RUN=1) после замка сверяет строки, с которыми его
# запустил systemd, с юнитом на диске: разошлись — перезапускается с ними.
# Строку AWG_GW_UNIT_RUN=1 пишет в юнит и этот скрипт, и агент сам (юниту
# прежнего образца — не дожидаясь первого полного прогона после обновления).
# ─────────────────────────────────────────────────────────────────────────────

set -e

# Строки юнита, которые переписывает агент (ключи признания, подсети, порт
# канала), — как их дал systemd при старте процесса: после замка ниже они
# сверяются с юнитом на диске (агент мог переписать его, пока прогон ждал).
UNIT_ENV_KEYS="CLIENT_SUBNET LINK_IF ADMIN_IPS LAN_MODE LAN_SUBNETS RESOLVER PEER_LAN_NETS LINK_CHANNEL_PORT GATEWAY_PUBKEY GATEWAY_PREV_PUBKEY"
UNIT_ENV0="$(for _k in $UNIT_ENV_KEYS; do eval "printf '%s=%s\n' \"\$_k\" \"\${$_k-}\""; done)"

LINK_IF="$(printf '%s' "${LINK_IF:-awglink}" | tr -cd 'A-Za-z0-9_.-' | cut -c1-15)"
# Боевое значение приезжает с ВПС (первый полный прогон — от агента из конфига
# линка после кода, дальше — документом конфигурации) и закрепляется в
# юните строкой Environment: на шлюзе нет app.yaml, и после ребута юнит обязан
# реассертить ту подсеть, которую назвал сервер, а не хардкод-дефолт.
CLIENT_SUBNET="$(printf '%s' "${CLIENT_SUBNET:-10.8.1.0/24}" | tr -cd '0-9./')"
case "$CLIENT_SUBNET" in
    [0-9]*.[0-9]*.[0-9]*.[0-9]*/[0-9]*) ;;
    *) printf '%s\n' "ОШИБКА: CLIENT_SUBNET не подсеть: $CLIENT_SUBNET"; exit 1 ;;
esac
FWD_CHAIN="AWGLINK_FWD"                  # прежняя цепочка iptables — только снятие
UNIT="/etc/systemd/system/awg-link-gw.service"
SYSCTL_CONF="/etc/sysctl.d/99-awgbot-gw.conf"
NETWORKD_UNMANAGED="/etc/systemd/network/99-awg-gw-unmanaged.network"   # networkd не трогает awg*
GW_ETC="/etc/awg-gw"
GUARD_FILE="$GW_ETC/guard.nft"           # таблица — источник для nft -f при каждом старте
FW_ENV="$GW_ETC/firewall.env"            # ADMIN_IPS_EXTRA, правится на шлюзе (awg-bot firewall)
GUARD_TABLE="inet awg_gw_guard"
JOIN_TABLE="inet awg_gw_join"             # карантин линка до кода подключения (--join)
SSH_PORT="${SSH_PORT:-22}"
TG_MARK="${TG_MARK:-0x1}"
# Диапазоны Telegram (AS62014/62041/59930/44907) — стабильны годами; тот же
# список знает агент (domain/gateway/tgmark.py TG_RANGES) и сверяет с таблицей.
TG_NETS="91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 91.108.20.0/22 91.108.56.0/22 149.154.160.0/20 185.76.151.0/24"
# GitHub — той же меткой в аплинк: агент обновляется с releases, а в юрисдикции
# шлюза GitHub без туннеля недоступен. Четыре сети из api.github.com/meta
# (web/api/git), в них github.com, api.github.com и *.githubusercontent.com
# (release-assets, objects, raw); региональные /32 Azure оттуда же — не для
# нас. Тот же список знает агент (domain/gateway/tgmark.py GH_RANGES).
GH_NETS="140.82.112.0/20 143.55.64.0/20 185.199.108.0/22 192.30.252.0/22"
PRIVATE_NETS="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10"
# Устройства админа (с ВПС, документом конфигурации) — им с туннеля открыто ВСЁ: сама машина и
# локальная сеть через неё. Прочим клиентам — изоляция и drop. Старое имя
# переменной (SSH_ALLOW) принимается для файлов конфигурации прежнего выпуска.
# Без двоеточия: запасное имя — только когда ADMIN_IPS НЕ ЗАДАН (старый
# файл конфигурации); юнит всегда задаёт его, пусть и пустым, а SSH_ALLOW из firewall.env
# к этому моменту уже в окружении (EnvironmentFile) — это список снаружи.
ADMIN_IPS="${ADMIN_IPS-${SSH_ALLOW:-}}"
ADMIN_IPS="$(printf '%s' "$ADMIN_IPS" | tr -cd '0-9./ ')"
ADMIN_IPS_EXTRA="$(printf '%s' "${ADMIN_IPS_EXTRA:-}" | tr -cd '0-9./ ')"
# Старое имя SSH_ALLOW отработало выше как ADMIN_IPS; дальше SSH_ALLOW — список
# адресов СНАРУЖИ из firewall.env, и значение из файла конфигурации ему не должно достаться.
SSH_ALLOW=""; SSH_ALLOW_RESOLVED=""; SSH_FILTER=""
[ -f "$FW_ENV" ] && . "$FW_ENV"
case "$SSH_PORT" in ''|*[!0-9]*) SSH_PORT=22 ;; esac
[ "$SSH_FILTER" = "1" ] || SSH_FILTER=0
# Шлюзовое устройство (пометка в основном боте): ключ аплинка помеченного
# шлюза, старый ключ в окне переезда, конфиг аплинка в base64. Пусто — шлюз
# в основном боте не помечен.
GATEWAY_PUBKEY="${GATEWAY_PUBKEY:-}"
GATEWAY_PREV_PUBKEY="${GATEWAY_PREV_PUBKEY:-}"
UPLINK_B64="${UPLINK_B64:-}"
UPLINK_TABLE="${UPLINK_TABLE:-100}"          # таблица политики «метка → аплинк»
UPLINK_IF_DEFAULT="${UPLINK_IF:-awg0}"        # имя аплинка на чистой машине
GW_FOREIGN=0                                  # 1 = слот помечен другому устройству, линк не поднимаем
GW_UNCONFIRMED=0                              # 1 = аплинка не видно, шлюз не подтверждён — линк не трогаем
GW_STATUS_FILE="$GW_ETC/gateway.status"       # что решил скрипт — читает агент
# локальная сеть без VPN: вторая таблица nft, dnsmasq, списки
LAN_TABLE="inet awg_lan"
LAN_FILE="$GW_ETC/lan.nft"            # после GW_ETC — иначе уедет в корень ФС
LAN_SYSCTL="/etc/sysctl.d/98-awg-gw-lan.conf"
DNSMASQ_D="/etc/dnsmasq.d"
DNSMASQ_OVR="/etc/systemd/system/dnsmasq.service.d/awg-gw.conf"
LAN_DUMP="/var/lib/awg-gw"                # слепки наборов: грузятся при старте до фидов
DNSMASQ_DEFAULT="/etc/default/dnsmasq"
DNSMASQ_MARK="$GW_ETC/dnsmasq.installed"  # dnsmasq ставили мы — при снятии выключаем
# Локальные подсети и резолвер — те же фильтры, что у чужих подсетей ниже.
# Прежнее имя (юнит 3.3.0 и раньше) — только когда нового в окружении нет
# вовсе: намеренно пустое новое значение прежним не подменяется. Снимается
# со следующим сдвигом минимума.
[ -n "${LAN_SUBNETS+x}" ] || LAN_SUBNETS="${HOME_SUBNETS:-}"
LAN_SUBNETS="$(printf '%s' "${LAN_SUBNETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"
RESOLVER="$(printf '%s' "${RESOLVER:-}" | tr -cd '0-9.' | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || true)"
LAN_LISTS="/usr/local/sbin/awg-lan-lists.sh"
LAN_DOMAIN="/usr/local/sbin/awg-lan-domain.sh"
LAN_SERVICES="/usr/local/sbin/awg-lan-services.sh"      # записи сервисов соседних сетей → dnsmasq
PEER_SVC_CONF="$DNSMASQ_D/awg-gw-peer-services.conf"
[ "${LAN_MODE:-0}" = "1" ] && LAN_MODE=1 || LAN_MODE=0
LINK_CHANNEL_PORT="$(printf '%s' "${LINK_CHANNEL_PORT:-8787}" | tr -cd '0-9' | cut -c1-5)"
case "$LINK_CHANNEL_PORT" in ''|0) LINK_CHANNEL_PORT=8787 ;; esac
# Подсети за другими шлюзами: им из линка
# открыт транзит в локальную сеть — по источнику, выше drop по приватным.
# Прежнее имя — как у LAN_SUBNETS выше (снимается со следующим сдвигом минимума).
[ -n "${PEER_LAN_NETS+x}" ] || PEER_LAN_NETS="${PEER_HOME_NETS:-}"
PEER_LAN_NETS="$(printf '%s' "${PEER_LAN_NETS:-}" | tr -cd '0-9./ ' | tr ' ' '\n' \
    | grep -E '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' | paste -sd' ' - 2>/dev/null || true)"

# Публичный ключ интерфейса: `awg show <if> public-key`.
iface_pubkey() { "$AWG_BIN" show "$1" public-key 2>/dev/null || true; }
conf_pubkey() {                # $1 = конфиг; публичный ключ из PrivateKey или ничего
    _pk="$(awk -F'= *' '/^PrivateKey/{print $2; exit}' "$1" 2>/dev/null | tr -d ' \r')"
    [ -n "$_pk" ] || return 0
    printf '%s' "$_pk" | "$AWG_BIN" pubkey 2>/dev/null || true
}
# Аплинки ЭТОЙ машины строками «имя ключ»: сначала живые интерфейсы, затем
# конфиги тех, что ещё не поднялись. Линки до ВПС (свой слот и слот соседа —
# awglink*) аплинками не считаются: их ключ принадлежит паре ВПС↔шлюз, а не
# устройству, и сравнивать пометку слота с ним нельзя.
uplink_list() {
    _live=" $("$AWG_BIN" show interfaces 2>/dev/null | tr '\n' ' ')"
    for _i in $_live; do
        case "$_i" in "$LINK_IF"|awglink*) continue ;; esac
        printf '%s %s\n' "$_i" "$(iface_pubkey "$_i")"
    done
    # Конфиги: при загрузке юнит обвязки стартует РАНЬШЕ awg-quick@ аплинка, и
    # по одним живым интерфейсам машина выглядит чужой — линк ложится, юнит
    # гасит сам себя (наступили при ребуте второго шлюза 19.09.2026). Ключ
    # устройства лежит в конфиге и до подъёма интерфейса.
    for _c in "$HOST_CONF_DIR"/*.conf; do
        [ -f "$_c" ] || continue
        _n="$(basename "$_c" .conf)"
        case "$_n" in "$LINK_IF"|awglink*) continue ;; esac
        case "$_live" in *" $_n "*) continue ;; esac
        printf '%s %s\n' "$_n" "$(conf_pubkey "$_c")"
    done
}
iface_by_pubkey() {            # $1 = ключ УСТРОЙСТВА; печатает имя аплинка или ничего
    [ -n "$1" ] || return 0
    uplink_list | while read -r _n _k; do
        [ -n "$_k" ] && [ "$_k" = "$1" ] && { printf '%s' "$_n"; break; }
    done
}

# Прежняя обвязка в iptables: снимаем идемпотентно и при --apply (переезд на
# таблицу), и при --rollback. Чужих правил (прежняя схема) не касаемся.
legacy_cleanup() {
    # в режиме показа циклы «пока правило есть — снимай» не сходятся: -D не
    # исполняется. Показ — одной строкой.
    if [ "$MODE" = "plan" ]; then
        printf '  would: снять прежние правила iptables (%s, MASQUERADE, метки Telegram), если есть\n' "$FWD_CHAIN"
        return 0
    fi
    while iptables -C FORWARD -i "$LINK_IF" -j "$FWD_CHAIN" 2>/dev/null; do
        run "iptables -D FORWARD -i $LINK_IF -j $FWD_CHAIN"
    done
    while iptables -C FORWARD -o "$LINK_IF" -m state --state RELATED,ESTABLISHED \
          -j ACCEPT 2>/dev/null; do
        run "iptables -D FORWARD -o $LINK_IF -m state --state RELATED,ESTABLISHED -j ACCEPT"
    done
    while iptables -t nat -C POSTROUTING -s "$CLIENT_SUBNET" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $CLIENT_SUBNET -o $WAN_IF -j MASQUERADE"
    done
    while [ -n "${LINK_CIDR:-}" ] && iptables -t nat -C POSTROUTING -s "$LINK_CIDR" -o "$WAN_IF" \
          -j MASQUERADE 2>/dev/null; do
        run "iptables -t nat -D POSTROUTING -s $LINK_CIDR -o $WAN_IF -j MASQUERADE"
    done
    if iptables -S "$FWD_CHAIN" >/dev/null 2>&1; then
        run "iptables -F $FWD_CHAIN && iptables -X $FWD_CHAIN"
    fi
    for n in $TG_NETS; do
        while iptables -t mangle -C OUTPUT -d "$n" -j MARK --set-mark "$TG_MARK" 2>/dev/null; do
            run "iptables -t mangle -D OUTPUT -d $n -j MARK --set-mark $TG_MARK"
        done
    done
}

# ── локальная сеть без VPN: помощники ────────
LAN_IF=""; LAN_ADDR=""; LAN_ERROR=""
dn_set_elsewhere() {           # $1 = regex: ключ dnsmasq уже задан в ДРУГОМ файле?
    # Debian запускает демон с conf-dir=/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,
    # .dpkg-new: читается ВСЁ, кроме этих суффиксов — и .bak, и .tmp тоже.
    for _f in /etc/dnsmasq.conf "$DNSMASQ_D"/*; do
        [ -f "$_f" ] || continue
        case "$_f" in */awg-gw-base.conf|*.dpkg-dist|*.dpkg-old|*.dpkg-new) continue ;; esac
        grep -qsE "$1" "$_f" && return 0
    done
    return 1
}
port53_busy() {                # слушатель :53, который не dnsmasq и мешает нам → строка или пусто
    ss -Hlnup 'sport = :53' 2>/dev/null | grep -v '"dnsmasq"' \
        | grep -E "(^|[[:space:]])(0\.0\.0\.0|\*|\[::\]|127\.0\.0\.1|${LAN_ADDR:-NOADDR}):53([[:space:]]|$)" | head -n1
}
lan_iface_for() {              # $1 = подсеть a.b.c.d/n → «iface addr» или ничего
    ip -4 -o addr show 2>/dev/null | awk -v net="$1" '
        function ip2n(s, a) { split(s, a, "."); return ((a[1]*256+a[2])*256+a[3])*256+a[4] }
        BEGIN { split(net, p, "/"); h = 2^(32-(p[2]+0)); want = int(ip2n(p[1])/h) }
        $2 != "lo" && $2 !~ /^(awg|veth|br-|docker)/ { split($4, q, "/"); if (int(ip2n(q[1])/h) == want) { print $2, q[1]; exit } }'
}
lan_remove() {                 # снять всё своё; личные списки — в $LAN_DUMP/restore (данные человека)
    _lan_lock=0
    if [ -d "$LAN_DUMP" ] && command -v flock >/dev/null 2>&1; then
        exec 8>"$LAN_DUMP/lists.lock"
        if flock -w 120 8; then _lan_lock=1; else
            say "  списки ещё обновляются — снимаю режим, не дождавшись"
        fi
    fi
    _lan_remove_locked; _rc=$?
    [ "$_lan_lock" = "1" ] && flock -u 8
    [ -d "$LAN_DUMP" ] && command -v flock >/dev/null 2>&1 && exec 8>&-
    return $_rc
}
_lan_remove_locked() {
    _changed=0
    for _f in "$DNSMASQ_D/awg-gw-base.conf" "$DNSMASQ_D/awg-gw-doh.conf" "$DNSMASQ_D/awg-gw-vpn-feed.conf" "$PEER_SVC_CONF"; do
        [ -f "$_f" ] && { run "rm -f $_f"; _changed=1; }
    done
    # личные списки — данные человека: не в архив с датой, а в restore/, откуда
    # следующее включение режима вернёт их на место
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] || continue
        run "mkdir -p $LAN_DUMP/restore"
        run "mv -f $DNSMASQ_D/$_f $LAN_DUMP/restore/$_f"; _changed=1
    done
    [ -f "$DNSMASQ_OVR" ] && { run "rm -f $DNSMASQ_OVR"; run "systemctl daemon-reload"; _changed=1; }
    if [ -f "$DNSMASQ_MARK" ]; then
        # ставили мы — снимаем: иначе на всех интерфейсах остаётся резолвер с дефолтами Debian
        run "systemctl disable --now dnsmasq 2>/dev/null || true"
        run "rm -f $DNSMASQ_MARK"
    elif [ "$_changed" = "1" ] && systemctl is-active --quiet dnsmasq 2>/dev/null; then
        run "systemctl restart dnsmasq"
    fi
    nft list table $LAN_TABLE >/dev/null 2>&1 && run "nft delete table $LAN_TABLE"
    [ -f "$LAN_FILE" ] && run "rm -f $LAN_FILE"
    lan_table_old_drop          # прежнее имя таблицы — снимается со следующим сдвигом минимума
    [ -f "$LAN_SYSCTL" ] && run "rm -f $LAN_SYSCTL"
    [ -f "$LAN_LISTS" ] && run "rm -f $LAN_LISTS"
    [ -f "$LAN_DOMAIN" ] && run "rm -f $LAN_DOMAIN"
    [ -f "$LAN_SERVICES" ] && run "rm -f $LAN_SERVICES"
    return 0
}
# Переход таблицы локальной сети с прежнего имени (inet awg_home — обвязка
# 3.3.0 и раньше) на $LAN_TABLE. Строки nftset= своих списков, фида, слепков
# и копий для отката — на новое имя ДО конфигов dnsmasq и по самим файлам, а
# не по живой таблице: после перезагрузки, при выключенном режиме или откате
# прежней таблицы в ядре нет, а строки с ней в файлах есть. Наборы живой
# прежней таблицы — слепками в новую и снятие прежней ПОСЛЕ перезапуска
# dnsmasq: до него демон пишет адреса в прежнюю, и они не теряются.
# Снимается со следующим сдвигом минимума.
LAN_TABLE_OLD="inet awg_home"
LAN_FILE_OLD="$GW_ETC/home.nft"
lan_table_move_prepare() {     # до конфигов dnsmasq: строки с прежним именем — на новое
    for _f in "$DNSMASQ_D"/awg-gw-*.conf "$LAN_DUMP"/restore/*.conf "$LAN_DUMP"/rollback/*.prev \
              "$LAN_DUMP"/vpn-feed.src "$LAN_DUMP"/lan_vpn4.nft "$LAN_DUMP"/lan_vpn_nets4.nft; do
        [ -f "$_f" ] && grep -q -e 'inet#awg_home#' -e "^table $LAN_TABLE_OLD {" "$_f" || continue
        # временный файл рядом (тот же раздел — mv атомарен) с суффиксом,
        # который conf-dir Debian не читает; права и владелец — копией прежнего
        _t="$_f.dpkg-new"
        run "cp -p $_f $_t && sed 's|inet#awg_home#|inet#awg_lan#|g; s|^table $LAN_TABLE_OLD {|table $LAN_TABLE {|' $_f > $_t && mv -f $_t $_f" \
            || { rm -f "$_t"; say "  $_f: строки прежней таблицы не переписаны"; continue; }
        _dn_changed=1
    done
    return 0
}
lan_table_move_finish() {      # после перезапуска dnsmasq: наборы прежней — в новую, прежняя — вон
    if nft list table $LAN_TABLE_OLD >/dev/null 2>&1; then
        say "  таблица локальной сети переезжает: $LAN_TABLE_OLD → $LAN_TABLE (наборы и свои списки на месте)"
        for _s in lan_vpn4 lan_vpn_nets4 lan_ru4; do
            _m="$LAN_DUMP/$_s.move.nft"
            nft list set $LAN_TABLE_OLD $_s 2>/dev/null \
                | sed "s|^table $LAN_TABLE_OLD {|table $LAN_TABLE {|" > "$_m" || true
            [ -s "$_m" ] && run "nft -f $_m 2>/dev/null || true"
            rm -f "$_m"
        done
    fi
    lan_table_old_drop
}
lan_table_old_drop() {
    nft list table $LAN_TABLE_OLD >/dev/null 2>&1 && run "nft delete table $LAN_TABLE_OLD"
    [ -f "$LAN_FILE_OLD" ] && run "rm -f $LAN_FILE_OLD"
    return 0
}
write_lan_scripts() {          # скрипты списков — из этого же файла, чтобы файл конфигурации был самодостаточен
    # каждый — во временный файл и mv: работающий экземпляр (dash читает по
    # 8 КБ, скрипты длиннее) дочитывает свой прежний inode, а не новый текст
cat > "$LAN_LISTS.new" <<'LISTSEOF'
#!/bin/sh
# awg-lan-lists.sh — списки локальной сети без VPN.
# Зовёт агент по расписанию (с джиттером), `awg-bot lan update` и агент же, когда
# фиды привёз канал (с AWG_LAN_FROM). Идемпотентно.
# AWG_LAN_FROM=<каталог> — фиды не качать, а взять готовыми из domains.lst и
# nets.lst в этом каталоге: их привозит сервер по каналу линка, и локальный адрес тогда не ходит за ними ни на GitHub, ни в
# Google. Проверки те же, что для скачанного: формат, длина, dnsmasq --test
# (с conf-dir, как у init-скрипта Debian); отказ --test или рестарта dnsmasq —
# откат прежнего фида и rc=1. Копия для отката — $DUMP/rollback/vpn-feed.conf.prev.
# Один запуск за раз: ждём блокировку lists.lock до 120 с, не дождались —
# выход 75 («занято»), не успех: иначе фиды из канала пропали бы молча.
#   фид доменов  → /etc/dnsmasq.d/awg-gw-vpn-feed.conf (nftset= в lan_vpn4), минус исключения
#   фиды подсетей → набор lan_vpn_nets4 (атомарно: flush + add)
#   слепки наборов → /var/lib/awg-gw/*.nft (грузятся при старте до фидов)
#   итог → /var/lib/awg-gw/lists.status: updated_at, domains, nets, rc,
#          source (net — скачано, channel — привёз канал)
set -u
TABLE="inet awg_lan"
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
FEED="$D/awg-gw-vpn-feed.conf"; RU="$D/awg-gw-ru-user.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; RB="$DUMP/rollback"
mkdir -p "$DUMP"
# один запуск за раз: кнопка «Обновить» и задача агента могут совпасть, а два
# параллельных — двойная запись фида и двойной рестарт dnsmasq
exec 9>"$DUMP/lists.lock"
# Ждём до двух минут, а не выходим сразу: фиды из канала, пришедшие во время
# ручного «Обновить», иначе молча не применились бы, а агент считал бы их
# применёнными. Не дождались — код 75 («занято»), не успех.
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление уже идёт" >&2; exit 75; fi
ITDOG="https://raw.githubusercontent.com/itdoginfo/allow-domains/main"
DOMAINS_URL="${AWG_LAN_DOMAINS_URL:-$ITDOG/Russia/inside-dnsmasq-ipset.lst}"
SUBNET_SERVICES="${AWG_LAN_SUBNET_SERVICES:-telegram meta twitter cloudflare discord}"
GOOG_URL="https://www.gstatic.com/ipranges/goog.json"
TMP="$(mktemp)"; TMP2="$(mktemp)"; NETS="$(mktemp)"
trap 'rm -f "$TMP" "$TMP2" "$NETS"' EXIT
rc=0

FROM="${AWG_LAN_FROM:-}"
get_domains() {
    if [ -n "$FROM" ]; then cp "$FROM/domains.lst" "$TMP" 2>/dev/null
    else curl -4 -sf --connect-timeout 5 --speed-time 10 --speed-limit 1024 "$DOMAINS_URL" -o "$TMP"; fi
}
# ── домены: во временный файл (недокачанный .new в conf-dir читался бы демоном)
if get_domains && [ -s "$TMP" ]; then
    # sed -i без суффикса — GNU-изм: правим через временный файл
    sed 's|^ipset=\(/.*/\)vpn_domains$|nftset=\1inet#awg_lan#lan_vpn4|' "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    # исходник до вычитания — awg-lan-domain.sh пересобирает фид по нему, когда
    # меняются исключения «напрямую» (иначе новое исключение ждало бы новых фидов);
    # кладётся только после проверки длины ниже, чтобы обрезанный фид не стал исходником
    cp "$TMP" "$DUMP/vpn-feed.src.tmp"
    # исключения побеждают: dnsmasq применяет одну директиву на домен, и какая
    # из двух победит, зависело бы от порядка чтения каталога — вычитаем
    if [ -s "$RU" ]; then
        awk -F/ '
            NR==FNR { if ($0 ~ /^nftset=/) for (i=2; i<NF; i++) skip[$i]=1; next }
            $0 !~ /^nftset=/ { print; next }
            { out=$1; n=0; for (i=2; i<NF; i++) if (!($i in skip)) { out=out "/" $i; n++ }
              if (n) print out "/" $(NF) }' "$RU" "$TMP" > "$TMP2" && mv "$TMP2" "$TMP"
    fi
    # Только директивы нашего набора и комментарии: HTTP 200 с HTML (заглушка
    # провайдера, страница блокировки) иначе уехал бы в conf-dir и уронил бы
    # dnsmasq на «bad option» — локальная сеть без DNS до следующего удачного фида.
    # сегмент — только имя домена: `#` или `.` у dnsmasq значат «все домены»
    grep -Ei '^(#.*|nftset=(/([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59}))+/inet#awg_lan#lan_vpn4)$' "$TMP" > "$TMP2"
    mv "$TMP2" "$TMP"
    feed_ok=0
    if [ "$(grep -c '^nftset=' "$TMP")" -lt 10 ]; then
        echo "домены: фид подозрительно короткий — не применяю" >&2; rc=1
    # дифф-скип: рестарт роняет кэш всей сети, а список меняется не каждые 6 ч
    elif ! cmp -s "$TMP" "$FEED"; then
        # копия для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
        mkdir -p "$RB"; [ -f "$FEED" ] && cp -p "$FEED" "$RB/vpn-feed.conf.prev" 2>/dev/null || true
        install -m 644 "$TMP" "$FEED"
        # conf-dir Debian подключает ключом из init-скрипта (CONFIG_DIR в
        # /etc/default/dnsmasq), и голый --test наш фид не видел вовсе
        if dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; then
            # именно restart: SIGHUP конфиги не перечитывает; отказ — откат фида
            if systemctl restart dnsmasq; then
                rm -f "$RB/vpn-feed.conf.prev"; feed_ok=1
            else
                echo "домены: dnsmasq не поднялся с новым фидом — откатываю" >&2; rc=1
                if [ -f "$RB/vpn-feed.conf.prev" ]; then mv -f "$RB/vpn-feed.conf.prev" "$FEED"; else rm -f "$FEED"; fi
                systemctl restart dnsmasq || true
            fi
        else
            echo "домены: dnsmasq --test отверг новый фид — откатываю" >&2; rc=1
            if [ -f "$RB/vpn-feed.conf.prev" ]; then mv -f "$RB/vpn-feed.conf.prev" "$FEED"; else rm -f "$FEED"; fi
        fi
    else
        feed_ok=1
    fi
    # исходник — только от принятого (или не изменившегося) фида: отвергнутый
    # исходником не становится, иначе каждая правка «напрямую» падала бы на нём
    if [ "$feed_ok" = 1 ]; then mv -f "$DUMP/vpn-feed.src.tmp" "$DUMP/vpn-feed.src"; else rm -f "$DUMP/vpn-feed.src.tmp"; fi
else
    if [ -n "$FROM" ]; then echo "домены: фида нет в $FROM (привозит канал)" >&2
    else echo "домены: фид не скачался ($DOMAINS_URL)" >&2; fi
    rc=1
fi

# ── подсети: itdoginfo по сервисам + официальный фид Google
: > "$NETS"
if [ -n "$FROM" ]; then
    cat "$FROM/nets.lst" >> "$NETS" 2>/dev/null || { echo "подсети: нет $FROM/nets.lst" >&2; rc=1; }
else
    nets_rc=0
    for svc in $SUBNET_SERVICES; do
        curl -4 -sf --connect-timeout 5 --speed-time 10 --speed-limit 1024 "$ITDOG/Subnets/IPv4/${svc}.lst" >> "$NETS" 2>/dev/null \
            || { echo "подсети: $svc не скачался" >&2; rc=1; nets_rc=1; }
        echo >> "$NETS"
    done
    curl -4 -sf --connect-timeout 5 --speed-time 10 --speed-limit 1024 "$GOOG_URL" 2>/dev/null \
        | grep -oE '"ipv4Prefix":[[:space:]]*"[0-9./]+"' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' >> "$NETS" \
        || { echo "подсети: goog.json не скачался" >&2; rc=1; nets_rc=1; }
fi
# префикс не короче /8: «0.0.0.0/1» из битого фида увёл бы в туннель полсети
elems="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/([89]|[12][0-9]|3[0-2])$' "$NETS" | sort -u | paste -sd, -)"
if [ -n "$elems" ]; then
    if [ "${nets_rc:-0}" = "0" ]; then
        # одной транзакцией: между flush и add окна нет
        printf 'flush set %s lan_vpn_nets4\nadd element %s lan_vpn_nets4 { %s }\n' "$TABLE" "$TABLE" "$elems" \
            | nft -f - || { echo "подсети: nft отклонил набор" >&2; rc=1; }
    else
        # один из фидов не скачался: только ДОБАВИТЬ — flush стёр бы подсети
        # сервиса, чей фид не пришёл, до следующего похода
        printf 'add element %s lan_vpn_nets4 { %s }\n' "$TABLE" "$elems" \
            | nft -f - || { echo "подсети: nft отклонил набор" >&2; rc=1; }
    fi
fi

# ── слепки: при старте таблица пуста, пока агент не сходит за фидами
mkdir -p "$DUMP"
for s in lan_vpn4 lan_vpn_nets4; do
    nft list set $TABLE $s > "$DUMP/$s.nft.tmp" 2>/dev/null && mv -f "$DUMP/$s.nft.tmp" "$DUMP/$s.nft"
done
_dom="$(grep -c '^nftset=' "$FEED" 2>/dev/null)"; _dom="${_dom:-0}"
_nets="$(printf '%s' "$elems" | tr ',' '\n' | grep -c .)"; _nets="${_nets:-0}"
printf 'updated_at=%s\ndomains=%s\nnets=%s\nrc=%s\nsource=%s\n' "$(date -Iseconds)" "$_dom" "$_nets" "$rc" \
    "$([ -n "$FROM" ] && echo channel || echo net)" > "$DUMP/lists.status"
exit $rc
LISTSEOF
chmod 0755 "$LAN_LISTS.new" && mv -f "$LAN_LISTS.new" "$LAN_LISTS"
cat > "$LAN_DOMAIN.new" <<'DOMEOF'
#!/bin/sh
# awg-lan-domain.sh — свои списки локальной сети без VPN.
#   add <домен…>   — в туннель (awg-gw-vpn-user.conf, набор lan_vpn4)
#   ru  <домен…>   — напрямую, российский адрес (awg-gw-ru-user.conf, набор lan_ru4)
#   del <домен…>   — убрать из обоих
#   list           — показать: «vpn <домен>» / «ru <домен>»
#   sync <файл>    — полный список строками «vpn <домен>» / «ru <домен>» (его
#                    собирает агент из канона сервера AWG): оба файла целиком,
#                    один рестарт dnsmasq; чужая строка — отказ целиком, rc=2.
#                    Адреса новых доменов «в туннель» в набор НЕ кладёт — это
#                    делает fill, которым агент зовёт скрипт фоном после sync
#   fill <файл>    — домены по одному на строку: их адреса (dig) — в lan_vpn4,
#                    затем слепок набора; без блокировки списков (набор пополняет
#                    и сам dnsmasq первым запросом клиента, это лишь ускорение)
# Домен накрывает поддомены. Схема и www. отбрасываются. Хост сервера AWG
# (Endpoint аплинка) добавить нельзя: увести туннель в туннель — запереть себя.
# Одна блокировка со скриптом фидов (lists.lock): два писателя одних файлов и
# два рестарта dnsmasq разом не бывает; занято дольше 120 с — код 75. list —
# без блокировки. Исключения «напрямую» вычитаются из фида доменов заново по
# сохранённому исходнику vpn-feed.src (его кладёт awg-lan-lists.sh; нет
# исходника — фид выправит ближайшая сборка). Домен, ушедший из «напрямую»,
# уходит и из набора lan_ru4 (flush + dig оставшихся: набор маленький и
# наполняется только отсюда); ушедший из «в туннель» вынимается из lan_vpn4 по
# адресам dig, ошибки глушатся (адрес мог слиться в интервал или принадлежать
# домену фида). Копии для отката обоих файлов и фида — $DUMP/rollback/<имя>.prev.
# Правило домена — DOMAIN_RE (зона буквами или punycode «xn--»);
# то же правило применит сервер AWG к общему списку (этап 2 синхронизации).
# AWG_HTML=1 (ставит агент: вывод уходит в чат) — домены в строках итога в
# <code>; без него — голым текстом, для терминала и агентов прежних выпусков.
# awg-lan-domain: sync
set -u
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
VPN="$D/awg-gw-vpn-user.conf"; RU="$D/awg-gw-ru-user.conf"; FEED="$D/awg-gw-vpn-feed.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; SRC="$DUMP/vpn-feed.src"; RB="$DUMP/rollback"
TABLE="inet awg_lan"
DOMAIN_RE='([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})'
# Аплинк — тот, что нашёл скрипт обвязки (UPLINK_IF в статусе): на машине с
# аплинком не awg0 иначе хост ВПС не был бы под запретом
_up="$(sed -n 's/^UPLINK_IF=//p' /etc/awg-gw/gateway.status 2>/dev/null | head -n1 | tr -cd 'A-Za-z0-9_.-')"
UPLINK_CONF="${AWG_UPLINK_CONF:-/etc/amnezia/amneziawg/${_up:-awg0}.conf}"
cmd="${1:-}"; [ $# -gt 0 ] && shift
[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }
mkdir -p "$DUMP"
touch "$VPN" "$RU"
list_of() {                    # $1 = файл → домены по одному на строку, по алфавиту
    # строка может нести несколько доменов (nftset=/a/b/набор — мигрированный ручной слой)
    sed -n 's|^nftset=\(/.*\)/inet#.*|\1|p' "$1" | tr '/' '\n' | grep . | sort -u
}
case "$cmd" in
    list)
        list_of "$VPN" | sed 's|^|vpn |'
        list_of "$RU" | sed 's|^|ru |'
        exit 0 ;;
    add|ru|del) [ $# -gt 0 ] || { echo "usage: $0 $cmd <домен…>"; exit 1; } ;;
    sync|fill) [ $# -eq 1 ] && [ -f "${1:-}" ] || { echo "usage: $0 $cmd <файл>"; exit 1; } ;;
    *) echo "usage: $0 add|ru|del <домен…> | list | sync <файл> | fill <файл>"; exit 1 ;;
esac
valid() { [ "${#1}" -le 253 ] && [ "$(printf '%s' "$1" | grep -c '')" -le 1 ] && printf '%s' "$1" | grep -Eq "^$DOMAIN_RE$"; }
resolve() { dig +short +time=3 +tries=1 @127.0.0.1 "$1" A 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; }
set_op() {                     # set_op add|delete <набор> <домен> — адреса домена в набор / из набора
    for ip in $(resolve "$3"); do nft "$1" element $TABLE "$2" "{ $ip }" 2>/dev/null || true; done
}
fill_set() {                   # fill_set <набор> <файл доменов> — параллельный dig, один nft на все адреса
    # 8 запросов разом: dnsmasq и апстрим через аплинк держат это легко, а
    # 500 доменов по одному шли бы минуты; nft — одним вызовом со всем списком
    _ips="$(xargs -r -P 8 -I{} sh -c 'dig +short +time=3 +tries=1 @127.0.0.1 "$1" A 2>/dev/null' _ {} < "$2" \
           | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sort -u | paste -sd, -)"
    [ -n "$_ips" ] && nft add element $TABLE "$1" "{ $_ips }" 2>/dev/null || true
}
snapshot_vpn() {               # слепок набора грузится при старте: без него снятый адрес вернулся бы с загрузкой
    nft list set $TABLE lan_vpn4 > "$DUMP/lan_vpn4.nft.tmp" 2>/dev/null && mv -f "$DUMP/lan_vpn4.nft.tmp" "$DUMP/lan_vpn4.nft" || rm -f "$DUMP/lan_vpn4.nft.tmp"
}
plural_dom() {                 # адреса 1 домена, 2 доменов, 21 домена
    case "$1" in *1[0-9]) echo доменов;; *1) echo домена;; *) echo доменов;; esac
}
if [ "$cmd" = "fill" ]; then
    # без блокировки: файлы dnsmasq не трогаем, только набор и его слепок
    _ok="$(mktemp)"; trap 'rm -f "$_ok"' EXIT
    while read -r d; do valid "$d" && printf '%s\n' "$d"; done < "$1" > "$_ok"
    n="$(grep -c . "$_ok")"
    fill_set lan_vpn4 "$_ok"
    snapshot_vpn
    echo "набор lan_vpn4: адреса $n $(plural_dom "$n") взяты в обработку"
    exit 0
fi
# одна блокировка со скриптом фидов: он читает ru-user.conf и тоже перезапускает dnsmasq
exec 9>"$DUMP/lists.lock"
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление списков ещё идёт" >&2; exit 75; fi
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
deny="$(sed -n 's/^Endpoint *= *\([^:]*\):.*/\1/p' "$UPLINK_CONF" 2>/dev/null | head -n1 | tr 'A-Z' 'a-z')"
in_list() { grep -qxF "$1" "$TMPD/want_$2"; }             # $1 домен, $2 vpn|ru
list_of "$VPN" > "$TMPD/before_vpn"; list_of "$RU" > "$TMPD/before_ru"
cp "$TMPD/before_vpn" "$TMPD/want_vpn"; cp "$TMPD/before_ru" "$TMPD/want_ru"
drop_from() { grep -vxF "$1" "$TMPD/want_$2" > "$TMPD/x" || true; mv "$TMPD/x" "$TMPD/want_$2"; }
add_to() { printf '%s\n' "$1" >> "$TMPD/want_$2"; }
added=""                       # «домен:набор» — наполнить после рестарта
# «добавлен»/«убран» — только после удачного рестарта: при откате человек видел
# бы «добавлен» рядом с «откатываю»
# Домен в строке итога: боту — моноширинным (<code>, AWG_HTML=1 ставит агент),
# в терминале и у агента прежних выпусков — голым текстом
tag() { if [ "${AWG_HTML:-0}" = "1" ]; then printf '<code>%s</code>' "$1"; else printf '%s' "$1"; fi; }
say() { printf '%s\n' "$1" >> "$TMPD/said"; }
: > "$TMPD/said"
if [ "$cmd" = "sync" ]; then
    if grep -Ev "^(vpn|ru) $DOMAIN_RE\$" "$1" | grep -q . || ! awk 'length($2) > 253 { bad=1 } END { exit bad }' "$1"; then
        echo "в файле есть строка не вида «vpn <домен>» / «ru <домен>» — не применяю" >&2; exit 2
    fi
    sed -n 's/^ru //p' "$1" | sort -u > "$TMPD/want_ru"
    # домен в обоих видах — «напрямую», как решает nft; адрес сервера AWG — пропущен с пометкой
    sed -n 's/^vpn //p' "$1" | sort -u | grep -vxF -f "$TMPD/want_ru" > "$TMPD/want_vpn" || true
    if [ -n "$deny" ]; then
        for f in want_vpn want_ru; do
            if grep -qxF "$deny" "$TMPD/$f"; then echo "$deny: это адрес самого сервера AWG — пропущен"; drop_from "$deny" "${f#want_}"; fi
        done
    fi
else
    for raw in "$@"; do
        d="$(printf '%s' "$raw" | sed -E 's|^[a-zA-Z]+://||; s|/.*$||; s|^www\.||' | tr 'A-Z' 'a-z')"
        valid "$d" || { echo "$d: не похоже на домен, пропущен"; continue; }
        if [ "$cmd" = "del" ]; then
            if in_list "$d" vpn || in_list "$d" ru; then
                drop_from "$d" vpn; drop_from "$d" ru; say "$(tag "$d"): убран"
            else
                echo "$(tag "$d"): в списках нет"
            fi
            continue
        fi
        [ -n "$deny" ] && [ "$d" = "$deny" ] && { echo "$(tag "$d"): это адрес самого сервера AWG — в туннель его не направить"; continue; }
        if [ "$cmd" = "add" ]; then k=vpn; other=ru; set_="lan_vpn4"; else k=ru; other=vpn; set_="lan_ru4"; fi
        in_list "$d" "$k" && { echo "$(tag "$d"): уже в списке"; continue; }
        drop_from "$d" "$other"; add_to "$d" "$k"
        added="$added $d:$set_"
        say "$(tag "$d"): добавлен"
    done
fi
sort -u -o "$TMPD/want_vpn" "$TMPD/want_vpn"; sort -u -o "$TMPD/want_ru" "$TMPD/want_ru"
if cmp -s "$TMPD/want_vpn" "$TMPD/before_vpn" && cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    [ "$cmd" = "sync" ] && echo "свои списки без изменений"
    exit 0
fi
# ── файлы: по домену на строку, по алфавиту (sync и кнопка пишут одинаково)
sed 's|.*|nftset=/&/inet#awg_lan#lan_vpn4|' "$TMPD/want_vpn" > "$TMPD/vpn.conf"
sed 's|.*|nftset=/&/inet#awg_lan#lan_ru4|' "$TMPD/want_ru" > "$TMPD/ru.conf"
changed="$VPN $RU"
# копии для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
mkdir -p "$RB"
prev() { printf '%s/%s.prev' "$RB" "$(basename "$1")"; }
for f in "$VPN" "$RU"; do cp -p "$f" "$(prev "$f")" 2>/dev/null || : > "$(prev "$f")"; done
install -m 644 "$TMPD/vpn.conf" "$VPN"; install -m 644 "$TMPD/ru.conf" "$RU"
# ── фид доменов: исключения «напрямую» вычитаются заново по исходнику (тот же
# awk, что в awg-lan-lists.sh); исходника нет — фид выправит ближайшая сборка
if [ -s "$SRC" ] && ! cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    # пустой список «напрямую» — первому файлу awk нужна хоть одна строка, иначе
    # NR==FNR сработает уже на исходнике и вычтет из фида всё
    { echo "# ru"; cat "$RU"; } > "$TMPD/ru_awk"
    awk -F/ '
        NR==FNR { if ($0 ~ /^nftset=/) for (i=2; i<NF; i++) skip[$i]=1; next }
        $0 !~ /^nftset=/ { print; next }
        { out=$1; n=0; for (i=2; i<NF; i++) if (!($i in skip)) { out=out "/" $i; n++ }
          if (n) print out "/" $(NF) }' "$TMPD/ru_awk" "$SRC" \
        | grep -E '^(#.*|nftset=/[^/[:space:]]+(/[^/[:space:]]+)*/inet#awg_lan#lan_vpn4)$' > "$TMPD/feed.conf" || true
    if [ -s "$TMPD/feed.conf" ] && ! cmp -s "$TMPD/feed.conf" "$FEED"; then
        cp -p "$FEED" "$(prev "$FEED")" 2>/dev/null || : > "$(prev "$FEED")"
        install -m 644 "$TMPD/feed.conf" "$FEED"; changed="$changed $FEED"
    fi
fi
rollback() { for f in $changed; do mv -f "$(prev "$f")" "$f"; done; systemctl restart dnsmasq || true; }
# conf-dir Debian подключает ключом из init-скрипта — голый --test файлы не видит
if command -v dnsmasq >/dev/null 2>&1 && ! dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; then
    echo "dnsmasq --test отверг списки — откатываю" >&2; rollback; exit 1
fi
if ! systemctl restart dnsmasq; then
    echo "dnsmasq не поднялся со своими списками — откатываю" >&2; rollback; exit 1
fi
for f in $changed; do rm -f "$(prev "$f")"; done
cat "$TMPD/said"
sleep 1
# ── наборы nft
# ушедшие из «в туннель» — вынуть адреса (ошибки глушатся: интервал auto-merge, домен фида)
grep -vxF -f "$TMPD/want_vpn" "$TMPD/before_vpn" 2>/dev/null | while read -r d; do
    [ -n "$d" ] && set_op delete lan_vpn4 "$d"
done
# «напрямую» изменилось — набор целиком заново: он маленький и наполняется только отсюда
if ! cmp -s "$TMPD/want_ru" "$TMPD/before_ru"; then
    nft flush set $TABLE lan_ru4 2>/dev/null || true
    fill_set lan_ru4 "$TMPD/want_ru"
fi
if [ "$cmd" = "sync" ]; then
    grep -vxF -f "$TMPD/before_vpn" "$TMPD/want_vpn" 2>/dev/null | sed 's|^|+ |; s|$| (в туннель)|'
    grep -vxF -f "$TMPD/before_ru" "$TMPD/want_ru" 2>/dev/null | sed 's|^|+ |; s|$| (напрямую)|'
    # «−» — только ушедшие из обоих видов; сменившие вид уже названы строкой «+»
    grep -vxF -f "$TMPD/want_ru" "$TMPD/before_ru" 2>/dev/null | grep -vxF -f "$TMPD/want_vpn" | sed 's|^|− |'
    grep -vxF -f "$TMPD/want_vpn" "$TMPD/before_vpn" 2>/dev/null | grep -vxF -f "$TMPD/want_ru" | sed 's|^|− |'
    # адреса новых доменов «в туннель» в набор кладёт fill — агент зовёт его
    # фоном: dig по каждому (первая синхронизация — до 500) не должен держать
    # ни блокировку списков, ни агента. Слепок — здесь: снятые выше адреса без
    # него вернулись бы с перезагрузкой; fill после своих добавлений снимет свой
    snapshot_vpn
    echo "свои списки: $(grep -c . "$TMPD/want_vpn") в туннель, $(grep -c . "$TMPD/want_ru") напрямую"
    exit 0
fi
for pair in $added; do
    d="${pair%%:*}"; set_="${pair##*:}"; n=0
    for ip in $(resolve "$d"); do
        # «напрямую» уже наполнен целиком выше — только считаем
        if [ "$set_" = "lan_ru4" ] || nft add element $TABLE $set_ "{ $ip }" 2>/dev/null; then n=$((n+1)); fi
    done
    echo "  $d → $n адрес(а) в наборе $set_"
done
snapshot_vpn
DOMEOF
chmod 0755 "$LAN_DOMAIN.new" && mv -f "$LAN_DOMAIN.new" "$LAN_DOMAIN"
cat > "$LAN_SERVICES.new" <<'SVCEOF'
#!/bin/sh
# awg-lan-services.sh — записи сервисов соседних сетей для dnsmasq. Зовёт агент: с путём к файлу — установить, без
# аргумента — снять. Содержимое агент собирает из данных, пришедших каналом
# линка с сервера AWG, поэтому файл проверяется построчно по белому списку
# шаблонов (тот же список — LINE_RES в awgbot/domain/gwservices.py): ни
# server=, ни address=, ни conf-file= сюда не пролезут. Дальше как у списков:
# тот же файл — выход без рестарта; dnsmasq --test с conf-dir; рестарт; отказ —
# откат прежнего файла (копия — $DUMP/rollback/peer-services.conf.prev) и
# rc=1. Блокировка общая с awg-lan-lists.sh: двух
# рестартов dnsmasq разом не бывает. rc=2 — файл не прошёл проверку.
set -u
D="${AWG_DNSMASQ_D:-/etc/dnsmasq.d}"
CONF="$D/awg-gw-peer-services.conf"
DUMP="${AWG_LAN_DUMP:-/var/lib/awg-gw}"; RB="$DUMP/rollback"; PREV="$RB/peer-services.conf.prev"
DOMAIN='awg\.internal'
[ "$(id -u)" = "0" ] || { echo "нужен root" >&2; exit 1; }
mkdir -p "$DUMP" "$RB"
exec 9>"$DUMP/lists.lock"
if command -v flock >/dev/null 2>&1 && ! flock -w 120 9; then echo "обновление списков или записей SMB ещё идёт" >&2; exit 75; fi
# conf-dir Debian подключает ключом из init-скрипта — голый --test файл не видит
test_conf() { dnsmasq --test "--conf-dir=$D,.dpkg-dist,.dpkg-old,.dpkg-new" >/dev/null 2>&1; }
restart_dnsmasq() { test_conf && systemctl restart dnsmasq; }
rollback() { if [ -f "$PREV" ]; then mv -f "$PREV" "$CONF"; else rm -f "$CONF"; fi; }
if [ $# -eq 0 ]; then
    [ -f "$CONF" ] || exit 0
    rm -f "$CONF"
    restart_dnsmasq || { echo "dnsmasq не поднялся после снятия записей SMB: journalctl -u dnsmasq -o short-iso -e" >&2; exit 1; }
    echo "записи SMB подсетей других шлюзов сняты"
    exit 0
fi
SRC="$1"
[ -s "$SRC" ] || { echo "файл записей пуст или не найден: $SRC" >&2; exit 2; }
if grep -Ev -e '^#.*$' -e '^$' \
    -e "^local=/$DOMAIN/\$" \
    -e "^ptr-record=l?b\\._dns-sd\\._udp\\.([0-9]{1,3}\\.){4}in-addr\\.arpa,$DOMAIN\$" \
    -e "^ptr-record=l?b\\._dns-sd\\._udp\\.$DOMAIN,$DOMAIN\$" \
    -e "^ptr-record=_services\\._dns-sd\\._udp\\.$DOMAIN,_smb\\._tcp\\.$DOMAIN\$" \
    -e "^ptr-record=_smb\\._tcp\\.$DOMAIN,\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\"\$" \
    -e "^srv-host=\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\",[A-Za-z0-9-]{1,63}\\.$DOMAIN,[0-9]{1,5}\$" \
    -e "^txt-record=\"[A-Za-z0-9 _-]{1,63}\\._smb\\._tcp\\.$DOMAIN\",\"\"\$" \
    -e "^host-record=[A-Za-z0-9-]{1,63}\\.$DOMAIN,[0-9]{1,3}(\\.[0-9]{1,3}){3}\$" \
    "$SRC" >/dev/null; then
    echo "в файле записей есть строка вне белого списка — не применяю" >&2; exit 2
fi
if cmp -s "$SRC" "$CONF" 2>/dev/null; then echo "записи SMB подсетей других шлюзов без изменений"; exit 0; fi
# копия для отката — вне conf-dir: оттуда dnsmasq читает всё, кроме .dpkg-*
if [ -f "$CONF" ]; then cp -p "$CONF" "$PREV" 2>/dev/null || true; fi
install -m 0644 "$SRC" "$CONF"
if ! test_conf; then
    # демон с плохим файлом не перезапускался — откат без рестарта, кэш сети цел
    echo "dnsmasq --test отверг записи SMB — откатываю" >&2; rollback; exit 1
fi
if systemctl restart dnsmasq; then
    rm -f "$PREV"
    echo "записи SMB подсетей других шлюзов применены: $(grep -c '^srv-host=' "$CONF")"
    exit 0
fi
echo "dnsmasq не поднялся с записями SMB — откатываю" >&2
rollback
systemctl restart dnsmasq || true
exit 1
SVCEOF
chmod 0755 "$LAN_SERVICES.new" && mv -f "$LAN_SERVICES.new" "$LAN_SERVICES"
}


# Подсеть линка — У ЯДРА (/30 ⇒ сеть считается из адреса), сосед по /30 — ВПС.
link_cidr_of() {
    ip -4 -o addr show dev "$1" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}' \
        | awk -F'[./]' '$5==30{ printf "%d.%d.%d.%d/%d\n", $1, $2, $3, int($4/4)*4, $5 }'
}
link_peer_of() {
    ip -4 -o addr show dev "$1" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="inet"){print $(i+1); exit}}' \
        | awk -F'[./]' '$5==30{ b=int($4/4)*4; p=(($4-b)==1)?b+2:b+1; printf "%d.%d.%d.%d\n", $1, $2, $3, p }'
}

# Элементы set из строки: только адреса и CIDR IPv4 — чужое в файл nft не
# попадает (значение приезжает из файла конфигурации и env, доверять форме нельзя).
ipv4_list() {
    for t in "$@"; do
        case "$t" in
            *[!0-9./]*|"") ;;
            *) printf '%s\n' "$t" ;;
        esac
    done | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g'
}

# Адрес сервера снаружи — хост Endpoint из конфига линка: IP как есть, имя —
# один резолв (при отказе пусто: набор server4 наполнит агент по тику).
# v6-литерал ([…]:порт) снаружи не поддерживается — пусто.
server_ipv4() {
    _h="$(awk '/^[[:space:]]*Endpoint[[:space:]]*=/ {sub(/^[^=]*=[[:space:]]*/, ""); print; exit}' \
        "${1:-/dev/null}" 2>/dev/null | sed 's/:[0-9]*$//')"
    case "$_h" in
        ""|\[*) return 0 ;;
        *[!0-9.]*) getent ahostsv4 "$_h" 2>/dev/null | awk '{print $1; exit}' ;;
        *) printf '%s\n' "$_h" ;;
    esac
}

# Локальная сеть — подсети локального интерфейса (маршруты scope link): именно
# они, а не RFC1918 целиком — за CGNAT провайдера (100.64/10) соседи по пулу
# приходили бы через проброс как «свои». Пусто — RFC1918 (запас).
lan_ipv4() {
    ip -4 route show dev "$1" scope link 2>/dev/null | awk '$1 ~ /\// {print $1}' \
        | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g'
}

# Интерфейс выхода и каталог конфигов ОПРЕДЕЛЯЮТСЯ, а не задаются дефолтом:
# чужие имена в поставке — источник тихих ошибок «скрипт отработал, но не
# там». Любое можно переопределить переменной окружения.
detect_wan() {
    [ -n "${WAN_IF:-}" ] && { printf '%s' "$WAN_IF"; return; }
    # токен после dev: формы «default via X dev Y», «default dev Y», с nhid —
    # позиция колонки плавает, имя нет
    ip route show default 2>/dev/null | awk '/^default/{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'
}
# Шлюз маршрута по умолчанию (токен после первого via) — только форма IPv4:
# четыре октета 0–255 без ведущих нулей; via inet6, маршрут без via или мусор —
# пусто. Без интервальных повторов в регэкспе: mawk 1.3.4 (Debian 12, Ubuntu
# 22.04) их не знает, и адрес пропадал бы молча.
detect_via() {
    ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="via"){print $(i+1); exit}}' \
        | awk -F. 'NF == 4 {
              for (i = 1; i <= 4; i++) if ($i !~ /^(0|[1-9][0-9]?[0-9]?)$/ || $i + 0 > 255) exit
              print; exit }'
}
# Адрес $1 внутри хотя бы одной из подсетей a.b.c.d/n, переданных дальше.
ip_in_nets() {
    printf '%s\n' "$@" | awk -F'[./]' '
        NR == 1 { if (NF != 4) exit; a = (($1 * 256 + $2) * 256 + $3) * 256 + $4; next }
        NF == 5 && $5 >= 0 && $5 <= 32 {
            n = (($1 * 256 + $2) * 256 + $3) * 256 + $4; s = 2 ^ (32 - $5)
            if (int(a / s) == int(n / s)) f = 1 }
        END { exit !f }'
}
# Адрес роутера — шлюз маршрута по умолчанию, если он внутри своих подсетей
# (LAN_SUBNETS): к нему маскарад соседей (п.2). Роутер вне локальной сети
# (нестандартная схема) — пусто, правила нет. DNS имён Endpoint (п.5) берёт
# шлюз маршрута по умолчанию как есть (VIA4).
detect_router() {
    [ -n "$1" ] && [ -n "$LAN_SUBNETS" ] && ip_in_nets "$1" $LAN_SUBNETS && printf '%s' "$1"
    return 0
}

MODE="plan"; SRC_CONF=""
case "${1:-}" in
    --apply)    MODE="apply"; SRC_CONF="${2:-}" ;;
    # конфиг линка из канала конфигурации: проверка белым списком и правилом AllowedIPs
    # (gw-wg-allow.list), затем syncconf без перезапуска или down/up — зовёт агент
    --link-apply) MODE="link-apply"; SRC_CONF="${2:-}" ;;
    # конфиг аплинка из канала конфигурации (человек подтвердил в чате агента или
    # аварийное правило): белый список, PostUp политики, подъём юнитом, откат
    --uplink)   MODE="uplink"; SRC_CONF="${2:-}" ;;
    # итог пробы аплинка (агент): прежний конфиг назад байт в байт / копию прочь
    --uplink-revert) MODE="uplink-revert" ;;
    --uplink-commit) MODE="uplink-commit" ;;
    --rollback) MODE="rollback" ;;
    # шлюз, подключаемый командой (awg-bot join): только линк по токену и
    # карантинная таблица — ни аплинка, ни транзита, ни юнита обвязки
    --join)     MODE="join"; SRC_CONF="${2:-}" ;;
    ""|--plan)  MODE="plan" ;;
    -h|--help)  awk 'NR>1 && !/^#/{exit} NR>1' "$0"; exit 0 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
esac

[ "$(id -u)" = "0" ] || { echo "нужен root"; exit 1; }

# Один прогон за раз: подключение командой (--join и полный прогон в конце), юнит
# (реассерт с тика, порт и фильтр SSH, восстановление) делят одни временные файлы и одну таблицу —
# второй ждёт первого, а не переписывает его .tmp и не побеждает последним nft -f.
# AWG_GW_LOCK_HELD=1 — замок уже держит вызывающий (агент держит его на всю
# транзакцию аплинка: юнит, конфиг, проба, откат) или этот же прогон перед
# перезапуском себя; второй раз не берём — иначе ждали бы сами себя.
unit_file_env() {    # $1 = ключ: значение строки Environment юнита на диске
    sed -n "s/^Environment=\"\{0,1\}$1=\([^\"]*\)\"\{0,1\}\$/\1/p" "$UNIT" 2>/dev/null | head -n 1
}
if [ "$MODE" != "plan" ] && command -v flock >/dev/null 2>&1 && [ "${AWG_GW_LOCK_HELD:-}" != "1" ]; then
    exec 7>/run/awg-gw-apply.lock
    flock -w 300 7 || { echo "обвязка уже применяется другим процессом — не дождался за 5 минут" >&2; exit 75; }
    # Прогон юнита стартовал со строками, прочитанными ДО замка. Пока он ждал,
    # агент мог переписать юнит (новые ключи признания, подсети) — тогда в
    # конце этот прогон записал бы юнит со старыми значениями поверх новых.
    # Разошлись — перезапуск себя со строками с диска, замок остаётся у нас.
    if [ "${AWG_GW_UNIT_RUN:-}" = "1" ] && [ -f "$UNIT" ]; then
        _stale=""
        for _k in $UNIT_ENV_KEYS; do
            _was="$(printf '%s\n' "$UNIT_ENV0" | sed -n "s/^$_k=//p" | head -n 1)"
            _now="$(unit_file_env "$_k")"
            [ "$_was" = "$_now" ] || _stale="$_stale $_k"
        done
        if [ -n "$_stale" ]; then
            echo "юнит переписан, пока прогон ждал замка (${_stale# }) — перезапускаюсь со строками юнита" >&2
            for _k in $UNIT_ENV_KEYS; do
                eval "$_k=\"\$(unit_file_env $_k)\""; export "$_k"
            done
            AWG_GW_LOCK_HELD=1; export AWG_GW_LOCK_HELD
            exec sh "$0" "$@"
        fi
    fi
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n── %s\n' "$*"; }
run()  {
    if [ "$MODE" = "plan" ]; then printf '  would: %s\n' "$*"
    else printf '  $ %s\n' "$*"; sh -c "$*"; fi
}

# Юнит обязан ссылаться на ПОСТОЯННЫЙ путь, поэтому скрипт сначала ставит себя
# рядом с прочими локальными админскими командами и только потом пишет ExecStart.
#
# Прежде туда уходил `readlink -f "$0"` — каталог, ОТКУДА запустили. Файл конфигурации
# шлюза распаковывается во временный, а systemd-tmpfiles вычищает его через
# десять дней. Автозапуск умирал молча: интерфейс уже стоял, RemainAfterExit
# держал юнит «активным», и обнаруживалось это только при первой перезагрузке —
# уже в виде «за шлюзом нет интернета», без связи с каким-либо действием.
install_self() {
    _src="$(readlink -f "$0")"
    _dst="/usr/local/sbin/$(basename "$_src")"
    if [ "$_src" != "$_dst" ]; then
        mkdir -p /usr/local/sbin
        install -m 0755 "$_src" "$_dst" || return 1
        printf '  $ install -m 0755 %s %s\n' "$_src" "$_dst" >&2
    fi
    printf '%s' "$_dst"          # только путь в stdout — его подхватит SELF
}

# Линк поднимаем утилитами хоста. Причина не в красоте: модуль ядра amneziawg
# живёт на хосте, и его протокол netlink обязан совпадать с версией awg.
# Утилиты другой сборки живут своей жизнью и
# однажды разъезжаются с модулем — тогда `awg setconf` падает с Invalid
# argument, и линк не поднимается ВОВСЕ (откатиться в userspace он уже не может,
# раз модуль есть).
AWG_QUICK="$(command -v awg-quick || true)"
AWG_BIN="$(command -v awg || true)"
HOST_CONF_DIR="${HOST_CONF_DIR:-/etc/amnezia/amneziawg}"

# Итог пробы аплинка — до определения выхода: откату WAN не нужен, а без
# маршрута по умолчанию (аренда DHCP потеряна) он не должен отказывать.
if [ "$MODE" = "uplink-revert" ] || [ "$MODE" = "uplink-commit" ]; then
    [ -n "${UPLINK_IF:-}" ] || { say "ОШИБКА: не назван аплинк (UPLINK_IF)"; exit 2; }
    _dst="$HOST_CONF_DIR/$UPLINK_IF.conf"
    _before="$_dst.before"
    if [ "$MODE" = "uplink-commit" ]; then
        rm -f "$_before"
        say "uplink: committed"
        exit 0
    fi
    # Прежний конфиг — файл с этого же диска, а не данные из канала: назад
    # без проверки белым списком (стоящий мог её и не проходить), байт в байт.
    if [ ! -f "$_before" ]; then
        say "uplink: unchanged"
        exit 0
    fi
    run "install -m 600 $_before $_dst" || { say "ОШИБКА: прежний конфиг аплинка не вернулся (запись)"; exit 1; }
    run "rm -f $_before"
    run "systemctl stop awg-quick@$UPLINK_IF 2>/dev/null || true"
    run "$AWG_QUICK down $UPLINK_IF 2>/dev/null || true"
    systemctl start "awg-quick@$UPLINK_IF" || { say "ОШИБКА: аплинк на прежнем конфиге не поднялся"; exit 1; }
    say "uplink: reverted"
    exit 0
fi

WAN_IF="$(detect_wan)"
[ -n "$WAN_IF" ] || { say "ОШИБКА: не определил интерфейс выхода. Укажи: WAN_IF=eth0 $0 ..."; exit 1; }
VIA4="$(detect_via)"
ROUTER4="$(detect_router "$VIA4")"

say "Параметры (определены автоматически, переопределяются переменными):"
say "  интерфейс линка    : $LINK_IF"
say "  конфиг на хосте    : $HOST_CONF_DIR/$LINK_IF.conf"
say "  клиенты сервера AWG: $CLIENT_SUBNET"
say "  выход в интернет   : $WAN_IF"
if [ -n "$ROUTER4" ]; then _rt="$ROUTER4"
elif [ -z "$VIA4" ]; then _rt="— (у маршрута по умолчанию нет шлюза)"
elif [ -z "$LAN_SUBNETS" ]; then _rt="— (своих подсетей нет; шлюз маршрута $VIA4)"
else _rt="— ($VIA4 вне своих подсетей)"; fi
say "  роутер             : $_rt"
say "  локальные сети     : будут ЗАКРЫТЫ для клиентов (все приватные диапазоны)"

# ── конфиг линка из канала конфигурации ──────────────────────────────────────
# Второй рубеж после агента, из того же источника: белые списки ключей и
# пределы правила AllowedIPs читаются из gw-wg-allow.list поставки (его же
# читает awgbot/util/wgconf.py на ВПС и на агенте). Нет файла — отказ: закрыто
# по умолчанию. Правило строго: записи AllowedIPs — только подсеть клиентов,
# /30 линка (по Address конфига), подсети других шлюзов и /32 резолвера из
# юнита; все — частные, не шире allowed_min_prefix, не больше allowed_max, без
# пересечений с локальными подсетями и подсетями интерфейсов шлюза.
uplink_render() {     # $1 = конфиг аплинка (клиентский, Table = off), $2 = куда: с PostUp политики
    # PostUp/PostDown — ВНУТРИ [Interface], сразу за Table = off:
    # awg-quick вырезает их только из этой секции, в [Peer] они
    # уходят в setconf и роняют подъём («Line unrecognized»)
    awk -v mark="$TG_MARK" -v tbl="$UPLINK_TABLE" '
        { print }
        /^Table = off$/ {
            printf "PostUp = ip rule list | grep -q \"fwmark %s lookup %s\" || ip rule add fwmark %s lookup %s\n", mark, tbl, mark, tbl
            printf "PostUp = ip route replace default dev %%i table %s\n", tbl
            printf "PostUp = sysctl -qw net.ipv4.conf.%%i.rp_filter=2\n"
            printf "PostDown = ip route del default dev %%i table %s 2>/dev/null || true\n", tbl
        }' "$1" > "$2"
}
uplink_install() {    # $1 = готовый конфиг (uplink_render), $2 = куда копию прежнего (пусто — .bak-<время>); 1 — не установлен, прежний на месте
    # Зовётся из `if !`: там set -e внутри функции не действует (POSIX), поэтому
    # каждый шаг проверяется явно — молча проглоченный отказ записи дал бы
    # «installed» при старом конфиге на диске.
    _dst="$HOST_CONF_DIR/$UPLINK_IF.conf"
    if [ -f "$_dst" ] && cmp -s "$1" "$_dst"; then
        say "  конфиг аплинка не изменился"
        UPLINK_STATE="unchanged"
        return 0
    fi
    _bak=""
    if [ -f "$_dst" ]; then
        _bak="${2:-$_dst.bak-$(date +%Y%m%d%H%M%S)}"
        if ! run "cp -p $_dst $_bak"; then
            say "ОШИБКА: копия прежнего конфига аплинка не снята — не установлен"
            UPLINK_STATE="refused"
            return 1
        fi
    fi
    if ! run "install -m 600 $1 $_dst"; then
        say "ОШИБКА: конфиг аплинка не записан — не установлен"
        [ -n "$_bak" ] && { run "cp -p $_bak $_dst" || true; [ -n "${2:-}" ] && run "rm -f $_bak"; }
        UPLINK_STATE="refused"
        return 1
    fi
    UPLINK_STATE="installed"
    say "  конфиг аплинка обновлён (Table = off, политика по метке $TG_MARK → таблица $UPLINK_TABLE)"
    run "systemctl stop awg-quick@$UPLINK_IF 2>/dev/null || true"
    run "$AWG_QUICK down $UPLINK_IF 2>/dev/null || true"
    # Поднимаем ЮНИТОМ, а не голым awg-quick: иначе юнит на
    # старте (Wants у обвязки) натыкался на «already exists»
    # и с Restart=on-failure перезапускался каждые 10 с без предела.
    # Аплинк — связь самого агента с Telegram. Не поднялся —
    # немедленно назад на прежний конфиг, иначе шлюз режет себе
    # связь и чинить его придётся руками.
    if ! systemctl start "awg-quick@$UPLINK_IF"; then
        say "  ОШИБКА: аплинк с новым конфигом не поднялся — откатываю на прежний"
        if [ -n "$_bak" ]; then
            if run "cp -p $_bak $_dst"; then
                run "systemctl start awg-quick@$UPLINK_IF || true"
                [ -n "${2:-}" ] && run "rm -f $_bak"
            else
                # копию не трогаем: --uplink-revert вернёт её позже
                say "  ОШИБКА: прежний конфиг аплинка не вернулся — копия: $_bak"
            fi
        fi
        UPLINK_STATE="failed"
        return 1
    fi
    run "systemctl enable awg-quick@$UPLINK_IF 2>/dev/null || true"
    return 0
}
allow_list_path() {
    _d="$(dirname "$(readlink -f "$0")")"
    for _p in "$_d/gw-wg-allow.list" "$_d/../install/gw-wg-allow.list" /opt/awg-bot/install/gw-wg-allow.list; do
        [ -f "$_p" ] && { printf '%s' "$_p"; return 0; }
    done
    return 1
}
wg_conf_check() {     # $1 = файл конфига, $2 = link|uplink|join; 0 — принят, 1 — отказ (причина в stderr)
    # join — конфиг линка по команде подключения: ключи линка, а AllowedIPs —
    # только /30 линка (подсеть клиентов шлюз узнает из документа после кода)
    _allow="$(allow_list_path)" || { echo "нет gw-wg-allow.list в поставке — конфиги каналом не принимаются" >&2; return 1; }
    awk -v kind="$2" -v client="${CLIENT_SUBNET:-}" -v peers="${PEER_LAN_NETS:-}" -v resolver="${RESOLVER:-}" \
        -v lan="${LAN_SUBNETS:-} ${IFACE_NETS:-}" '
    function lc(s) { return tolower(s) }
    function fail(m) { print m > "/dev/stderr"; bad = 1; exit 1 }
    function ip2n(a,  p) { split(a, p, "."); return ((p[1] * 256 + p[2]) * 256 + p[3]) * 256 + p[4] }
    function ntoa(n) { return int(n / 16777216) "." int(n / 65536) % 256 "." int(n / 256) % 256 "." n % 256 }
    function base(a, n) { return int(a / (2 ^ (32 - n))) * (2 ^ (32 - n)) }
    function net_parse(c,  q, o, i) {           # a.b.c.d/n → NET_A, NET_P; 0 — не подсеть
        if (c !~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/[0-9]+$/) return 0
        split(c, q, "/"); if (q[2] + 0 > 32) return 0
        split(q[1], o, "."); for (i = 1; i <= 4; i++) if (o[i] + 0 > 255) return 0
        NET_A = ip2n(q[1]); NET_P = q[2] + 0; return 1
    }
    function net_ok(c) { if (!net_parse(c)) return 0; return base(NET_A, NET_P) == NET_A }
    function inside(a, n, A, N) { return n >= N && base(a, N) == A }
    function overlap(a1, n1, a2, n2,  m) { m = (n1 < n2) ? n1 : n2; return base(a1, m) == base(a2, m) }
    FNR == NR {                                  # gw-wg-allow.list
        sub(/#.*/, ""); if (NF == 0) next
        if ($1 == "allowed_max") { amax = $2 + 0; next }
        if ($1 == "allowed_min_prefix") { aminp = $2 + 0; next }
        if ($1 == "allowed_nets") { for (i = 2; i <= NF; i++) if (net_parse($i)) { nn++; anet_a[nn] = NET_A; anet_p[nn] = NET_P }; next }
        if (NF == 3 && $1 == (kind == "join" ? "link" : kind)) ok[$2 "\t" lc($3)] = $3
        next
    }
    { line = $0; sub(/^[ \t]+/, "", line); sub(/[ \t]+$/, "", line) }
    line == "" { next }
    line ~ /^#/ { if (kind != "uplink" && line !~ /^#[ \t]*awg-bot:[ \t]*контракт линка[ \t]+[0-9]+$/) fail("в конфиге линка допустим только штамп контракта"); next }
    line ~ /^\[.*\]$/ { sec = substr(line, 2, length(line) - 2); if (sec != "Interface" && sec != "Peer") fail("неизвестная секция " sec); cnt[sec]++; next }
    sec == "" { fail("строка вне секции") }
    index(line, "=") == 0 { fail("строка без «=»: " line) }
    {
        key = line; sub(/[ \t]*=.*$/, "", key); val = line; sub(/^[^=]*=[ \t]*/, "", val)
        k = sec "\t" lc(key)
        if (!(k in ok)) fail("[" sec "] " key ": ключ вне белого списка")
        if (seen[k]++) fail("[" sec "] " key ": ключ повторяется")
        if (sec == "Peer" && lc(key) == "allowedips") allowed = val
        if (sec == "Interface" && lc(key) == "address") address = val
    }
    END {
        if (bad) exit 1
        if (!amax || !aminp || !nn) fail("gw-wg-allow.list: не заданы пределы правила AllowedIPs")
        if (cnt["Interface"] != 1 || cnt["Peer"] != 1) fail("нужны ровно одна секция [Interface] и один [Peer]")
        if (kind == "uplink") exit 0
        if (!net_parse(address) || NET_P != 30) fail("Address линка — не /30")
        link_c = ntoa(base(NET_A, 30)) "/30"
        nw = 0; want[++nw] = (kind == "join") ? link_c : client; want[++nw] = link_c
        np = split(peers, pl, /[ ,]+/); for (i = 1; i <= np; i++) if (pl[i] != "") want[++nw] = pl[i]
        if (resolver != "") want[++nw] = resolver "/32"
        ne = split(allowed, el, /[ ,]+/); n = 0
        for (i = 1; i <= ne; i++) if (el[i] != "") e[++n] = el[i]
        if (n > amax) fail("AllowedIPs: записей " n ", предел " amax)
        for (i = 1; i <= n; i++) {
            if (!net_ok(e[i])) fail("AllowedIPs: " e[i] " — не подсеть")
            ea[i] = NET_A; ep[i] = NET_P
            if (ep[i] < aminp) fail("AllowedIPs: " e[i] " шире /" aminp)
            inpriv = 0; for (j = 1; j <= nn; j++) if (inside(ea[i], ep[i], anet_a[j], anet_p[j])) inpriv = 1
            if (!inpriv) fail("AllowedIPs: " e[i] " вне частных диапазонов")
            found = 0; for (j = 1; j <= nw; j++) if (e[i] == want[j]) found = 1
            if (!found) fail("AllowedIPs: " e[i] " — записи нет в документе")
        }
        for (j = 1; j <= 2; j++) { found = 0; for (i = 1; i <= n; i++) if (e[i] == want[j]) found = 1
            if (!found) fail("AllowedIPs: нет обязательной записи " want[j]) }
        nl = split(lan, ll, /[ ,]+/)
        for (j = 1; j <= nl; j++) { if (ll[j] == "" || !net_parse(ll[j])) continue; la = NET_A; lp = NET_P
            for (i = 1; i <= n; i++) if (overlap(ea[i], ep[i], la, lp)) fail("AllowedIPs: " e[i] " пересекается с подсетью шлюза " ll[j]) }
        exit 0
    }' "$_allow" "$1"
}
conf_allowed_ips() {  # записи AllowedIPs конфига — по одной на строку
    awk -F'=' 'tolower($1) ~ /^[ \t]*allowedips[ \t]*$/ { print $2 }' "$1" 2>/dev/null | tr ', ' '\n\n' | grep . || true
}
conf_core() {         # конфиг без AllowedIPs, PersistentKeepalive, комментариев и пробелов — для сравнения «только лёгкие правки»
    grep -vE '^[ \t]*(AllowedIPs|PersistentKeepalive)[ \t]*=|^[ \t]*#|^[ \t]*$' "$1" 2>/dev/null | sed 's/[ \t]*=[ \t]*/ = /; s/^[ \t]*//; s/[ \t]*$//'
}
if [ "$MODE" = "link-apply" ]; then
    step "Конфиг линка из канала конфигурации → $HOST_CONF_DIR/$LINK_IF.conf"
    [ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || { say "ОШИБКА: нужен файл: $0 --link-apply <конфиг>"; exit 2; }
    # подсети интерфейсов шлюза: с ними AllowedIPs пересекаться не вправе. Агент
    # передаёт тот же список, по которому судил сам (IFACE_NETS, пусть и пустой), —
    # оба рубежа решают по одному; вручную без него — маршруты scope link кроме туннелей
    if [ -z "${IFACE_NETS+x}" ]; then
        IFACE_NETS="$(ip -4 route show scope link 2>/dev/null | awk -v l="$LINK_IF" '$1 ~ /\// && $3 != l && $3 !~ /^awg/ { print $1 }' | paste -sd' ' - 2>/dev/null || true)"
    fi
    # LINK_CHECK_KIND=join — шлюз в сопряжении возвращается на конфиг по токену
    # (AllowedIPs — только /30): проверка вида join, как у --join
    _lk=link; [ "${LINK_CHECK_KIND:-}" = "join" ] && _lk=join
    if ! wg_conf_check "$SRC_CONF" "$_lk"; then
        say "ОШИБКА: конфиг линка не прошёл проверку — не установлен"
        exit 1
    fi
    _dst="$HOST_CONF_DIR/$LINK_IF.conf"
    if [ -f "$_dst" ] && cmp -s "$SRC_CONF" "$_dst"; then
        say "link: unchanged"
        exit 0
    fi
    _light=0
    if [ -f "$_dst" ] && ip link show "$LINK_IF" >/dev/null 2>&1 \
        && [ "$(conf_core "$SRC_CONF")" = "$(conf_core "$_dst")" ]; then
        _light=1                               # разница только в AllowedIPs и PersistentKeepalive
    fi
    _old_allowed="$(conf_allowed_ips "$_dst")"
    _new_allowed="$(conf_allowed_ips "$SRC_CONF")"
    mkdir -p "$HOST_CONF_DIR"
    # прежний конфиг — рядом, 0600: не поднялся новый — он возвращается на место
    # и поднимается, а не остаётся на диске до ручной правки
    _before="$_dst.before"
    _has_before=0
    if [ -f "$_dst" ]; then
        _um="$(umask)"; umask 077
        cp "$_dst" "$_before"; _has_before=1
        umask "$_um"
    fi
    link_restore() {   # $1 — поднять ли прежний после возврата
        if [ "$_has_before" = "1" ]; then
            install -m 600 "$_before" "$_dst"; rm -f "$_before"
            if [ "${1:-0}" = "1" ]; then "$AWG_QUICK" up "$LINK_IF" >/dev/null 2>&1 || true; fi
            say "  прежний конфиг линка возвращён"
        fi
    }
    run "install -m 600 '$SRC_CONF' $_dst"
    if [ "$_light" = "1" ]; then
        # syncconf: ключи и Endpoint те же — линк не перезапускается, сессия
        # канала переживает применение; маршруты AllowedIPs ставит/снимает сам
        _strip="$(mktemp)"
        if "$AWG_QUICK" strip "$LINK_IF" > "$_strip" 2>/dev/null && "$AWG_BIN" syncconf "$LINK_IF" "$_strip"; then
            rm -f "$_strip"
        else
            rm -f "$_strip"
            say "ОШИБКА: awg syncconf отказал"
            link_restore 0          # интерфейс жив на прежних значениях — вернуть только файл
            exit 1
        fi
        for _n in $_new_allowed; do run "ip route replace $_n dev $LINK_IF 2>/dev/null || true"; done
        for _n in $_old_allowed; do
            case " $(printf '%s' "$_new_allowed" | tr '\n' ' ') " in
                *" $_n "*) ;;
                *) run "ip route del $_n dev $LINK_IF 2>/dev/null || true" ;;
            esac
        done
        say "link: syncconf"
    else
        # Endpoint или обфускация: только down/up; таблица защиты — до подъёма,
        # как при применении файла (окно между up и nft -f закрыто прежней)
        if ip link show "$LINK_IF" >/dev/null 2>&1; then
            run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
        fi
        if [ -f "$GUARD_FILE" ]; then
            run "nft -f $GUARD_FILE || echo '  прежняя таблица защиты не загрузилась'"
        fi
        if ! "$AWG_QUICK" up "${LINK_IF}"; then
            say "ОШИБКА: линк на новом конфиге не поднялся"
            link_restore 1
            exit 1
        fi
        say "link: restarted"
    fi
    rm -f "$_before"
    exit 0
fi

if [ "$MODE" = "join" ]; then
    step "Линк по команде подключения → $HOST_CONF_DIR/$LINK_IF.conf"
    [ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || { say "ОШИБКА: нужен файл: $0 --join <конфиг>"; exit 2; }
    if [ -z "${IFACE_NETS+x}" ]; then
        IFACE_NETS="$(ip -4 route show scope link 2>/dev/null | awk -v l="$LINK_IF" '$1 ~ /\// && $3 != l && $3 !~ /^awg/ { print $1 }' | paste -sd' ' - 2>/dev/null || true)"
    fi
    wg_conf_check "$SRC_CONF" join || { say "ОШИБКА: конфиг линка не прошёл проверку — не установлен"; exit 1; }
    [ -n "$AWG_QUICK" ] || { say "ОШИБКА: awg-quick не найден на ХОСТЕ."; exit 1; }
    mkdir -p "$HOST_CONF_DIR" "$GW_ETC"
    _jt="$GW_ETC/join.nft"
    cat > "$_jt.tmp" <<JOINEOF
#!/usr/sbin/nft -f
# awg-bot (шлюз): карантин линка до подтверждения кода подключения.
# Снимает полный прогон обвязки (--apply) после сопряжения.
table $JOIN_TABLE
delete table $JOIN_TABLE
table $JOIN_TABLE {
    chain input {
        type filter hook input priority -1; policy accept;
        iifname "$LINK_IF" ct state established,related accept
        iifname "$LINK_IF" drop
    }
    chain forward {
        type filter hook forward priority -1; policy accept;
        iifname "$LINK_IF" drop
        oifname "$LINK_IF" drop
    }
}
JOINEOF
    nft -c -f "$_jt.tmp" || { say "ОШИБКА: nft отклонил карантинную таблицу — $_jt.tmp"; exit 1; }
    mv "$_jt.tmp" "$_jt"
    run "nft -f $_jt"
    install_self >/dev/null || true             # копия в /usr/local/sbin — её зовёт агент
    run "install -m 600 '$SRC_CONF' $HOST_CONF_DIR/$LINK_IF.conf"
    if ip link show "$LINK_IF" >/dev/null 2>&1; then
        run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    fi
    "$AWG_QUICK" up "$LINK_IF" || { say "ОШИБКА: линк по команде подключения не поднялся"; exit 1; }
    say "join: linked"
    exit 0
fi

if [ "$MODE" = "uplink" ]; then
    step "Конфиг аплинка из канала конфигурации"
    # отказ до установки — итог «uplink: refused»: на диске ничего не менялось
    [ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || { say "ОШИБКА: нужен файл: $0 --uplink <конфиг>"; say "uplink: refused"; exit 2; }
    [ -n "${UPLINK_IF:-}" ] || { say "ОШИБКА: не назван аплинк (UPLINK_IF)"; say "uplink: refused"; exit 2; }
    if ! wg_conf_check "$SRC_CONF" uplink; then
        say "ОШИБКА: конфиг аплинка не прошёл проверку — не установлен"
        say "uplink: refused"
        exit 1
    fi
    grep -q '^Table = off$' "$SRC_CONF" || { say "ОШИБКА: в конфиге аплинка нет Table = off — не установлен"; say "uplink: refused"; exit 1; }
    _um="$(umask)"; umask 077
    _tmp="$GW_ETC/uplink.render"
    mkdir -p "$GW_ETC"
    trap 'rm -f "$_tmp"' EXIT
    uplink_render "$SRC_CONF" "$_tmp"
    # копия прежнего — одна, .before: живёт до итога пробы агента
    # (--uplink-commit убирает, --uplink-revert ставит назад байт в байт)
    _before="$HOST_CONF_DIR/$UPLINK_IF.conf.before"
    rm -f "$_before"
    if ! uplink_install "$_tmp" "$_before"; then
        [ "$UPLINK_STATE" = "refused" ] && say "uplink: refused"
        exit 1
    fi
    rm -f "$_tmp"; trap - EXIT; umask "$_um"
    say "uplink: $UPLINK_STATE"
    exit 0
fi


# ── откат ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "rollback" ]; then
    step "Снятие"
    LINK_CIDR_PRE="$(link_cidr_of "$LINK_IF")"
    run "nft delete table $JOIN_TABLE 2>/dev/null || true"
    run "rm -f $GW_ETC/join.nft"
    run "systemctl disable --now awg-link-gw.service 2>/dev/null || true"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    # подсеть линка — пока интерфейс ещё жив (до down он выше уже снят, но
    # адрес мог остаться в старом iptables-правиле — снимаем по нему)
    LINK_CIDR="${LINK_CIDR_PRE:-$(link_cidr_of "$LINK_IF")}"
    legacy_cleanup
    lan_remove
    if command -v iptables >/dev/null 2>&1; then
        for _i in "$LINK_IF" $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' | grep -v '^lo$'); do
            for _d in -i -o; do
                while iptables -w -C FORWARD $_d "$_i" -j ACCEPT 2>/dev/null; do
                    run "iptables -w -D FORWARD $_d $_i -j ACCEPT"
                done
            done
        done
    fi
    run "nft delete table $GUARD_TABLE 2>/dev/null || true"
    # firewall.env — данные человека (адреса, порт): не удаляем, а откладываем.
    [ -f "$FW_ENV" ] && run "mv -f $FW_ENV $FW_ENV.bak"
    run "rm -f $GUARD_FILE $GW_STATUS_FILE"
    run "rmdir $GW_ETC 2>/dev/null || true"
    run "rm -f $HOST_CONF_DIR/$LINK_IF.conf $UNIT $SYSCTL_CONF /etc/systemd/networkd.conf.d/awg-gw.conf $NETWORKD_UNMANAGED"
    run "rm -f /etc/systemd/system/awg-quick@*.service.d/awg-gw.conf"
    run "systemctl daemon-reload"
    say ""
    say "Готово. Существующие интерфейсы и маршрутизация шлюза не тронуты."
    exit 0
fi

if [ "$MODE" = "plan" ]; then
    say ""
    say "(режим показа — добавь: --apply <файл-конфига-с-сервера-AWG>)"
    say ""
    say "Будет сделано:"
    say "  1. конфиг → $HOST_CONF_DIR/$LINK_IF.conf, прежняя таблица защиты, awg-quick up хостовыми утилитами"
    say "  2. таблица nft $GUARD_TABLE: MASQUERADE $CLIENT_SUBNET → $WAN_IF, изоляция"
    say "     клиентов от приватных сетей, метки Telegram и GitHub → аплинк, защита шлюза от туннеля"
    say "     (с туннеля на шлюз: сервер AWG по линку; полный доступ — ADMIN_IPS=${ADMIN_IPS:-—})"
    say "     SSH снаружи (порт $SSH_PORT): фильтр $([ "$SSH_FILTER" = 1 ] && echo включён || echo выключен); адреса: ${SSH_ALLOW:-—}"
    say "  3. снятие прежних правил iptables ($FWD_CHAIN, MASQUERADE, метки)"
    say "  0. шлюзовое устройство: ${GATEWAY_PUBKEY:+помечен, конфиг аплинка ставится устройству с тем же ключом}${GATEWAY_PUBKEY:-не помечен}"
    say "  4. юнит awg-link-gw.service"
    say "  5. VPN-транзит: ${LAN_MODE:-0} (подсети: ${LAN_SUBNETS:-—}; резолвер: ${RESOLVER:-запасной через аплинк})"
    say "     SMB подсетей других шлюзов (в Finder: «Сеть» → awg.internal): $([ -n "${PEER_LAN_NETS:-}" ] && [ "${LAN_MODE:-0}" = "1" ] && echo включены || echo нет)"
    say "     локальные подсети других шлюзов (транзит из линка): ${PEER_LAN_NETS:-—}"
    say "     сам роутер им (маскарад к адресу роутера): $([ -n "${PEER_LAN_NETS:-}" ] && [ "${LAN_MODE:-0}" = "1" ] && [ -n "$ROUTER4" ] && echo "открыт, $ROUTER4" || echo нет)"
    exit 0
fi

# ── 0. шлюзовое устройство: аплинк и чей это шлюз ───────────────────────────
# Аплинк — клиентский туннель этой машины к ВПС, которым она является
# устройством админа. Ключ аплинка и его конфиг приезжают документом
# конфигурации по каналу (прежние выпуски — файлом конфигурации). Конфиг
# ставим ТОЛЬКО машине с тем же ключом (или со старым ключом пары в окне
# переезда): чужая машина шлюзом не становится, пока основной бот её не
# пометит, — она попросит переслать сообщение и линк не поднимет.
# Файл конфигурации прежних выпусков не старее применённого. Старый файл из
# загрузок, запущенный руками мимо агента, вернул бы ключи линка до их смены —
# и линк лёг бы. Новых файлов нет: шлюзы подключаются командой из бота.
# Метку выпуска файл передаёт окружением (BUNDLE_ISSUED_AT); применённую
# хранит $ISSUED_FILE — её же сдвигает агент, когда линк закреплён на новых
# ключах. Прогоны юнита и агента без файла метки не несут — сверять нечего.
ISSUED_FILE="$GW_ETC/issued_at"
_bundle_issued="$(printf '%s' "${BUNDLE_ISSUED_AT:-}" | tr -cd '0-9')"
# Время для человека — с меткой системного пояса (скрипт идёт в терминале хоста):
# +0300 → UTC+3, +0530 → UTC+5:30, -0330 → UTC-3:30, +0000 → UTC.
tz_label() {
    case "$1" in
        [+-]0000|"") printf 'UTC' ;;
        *) printf 'UTC%s' "$(printf '%s' "$1" | sed -E 's/^([+-])0?([0-9]+)([0-9]{2})$/\1\2:\3/; s/:00$//')" ;;
    esac
}
issued_when() {
    _iw_z="$(date -d "@$1" +%z 2>/dev/null)" \
        && date -d "@$1" "+%d.%m.%y %H:%M ($(tz_label "$_iw_z"))" 2>/dev/null || printf '%s' "$1"
}
issued_check() {     # 1 — файл старее применённого (и не велено применять всё равно)
    [ -n "$_bundle_issued" ] && [ "${AWG_GW_ALLOW_OLD:-}" != "1" ] || return 0
    _have="$(tr -cd '0-9' < "$ISSUED_FILE" 2>/dev/null || true)"
    [ -n "$_have" ] && [ "$_bundle_issued" -lt "$_have" ] || return 0
    say "ОТКАЗ: файл конфигурации выпущен $(issued_when "$_bundle_issued"), текущая конфигурация от $(issued_when "$_have")"
    say "Файлы конфигурации шлюза больше не нужны: настройки приходят от сервера AWG по каналу конфигурации, неполадки закрывает «🚑 Восстановить» в чате бота шлюза, новое устройство подключается командой из бота сервера AWG; всё равно применить этот: sudo AWG_GW_ALLOW_OLD=1 sh <файл>"
    return 1
}
issued_record() {    # применённое — не старее метки этого файла
    [ -n "$_bundle_issued" ] || return 0
    _have="$(tr -cd '0-9' < "$ISSUED_FILE" 2>/dev/null || true)"
    [ -n "$_have" ] && [ "$_have" -gt "$_bundle_issued" ] && return 0
    printf '%s\n' "$_bundle_issued" > "$ISSUED_FILE"
}
issued_check || exit 3

step "0. Шлюзовое устройство"
mkdir -p "$GW_ETC"
UPLINK_STATE="none"
if [ -z "$GATEWAY_PUBKEY" ]; then
    say "  в боте сервера AWG шлюз не подключён — после применения агент отправит запрос по каналу конфигурации (канала нет — подключи устройство командой из бота сервера AWG: «🛰 Шлюзы» → «🛰 Подключить шлюз»)"
    GW_STATUS="unmarked"
else
    UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PUBKEY")"
    [ -z "$UPLINK_IF" ] && [ -n "$GATEWAY_PREV_PUBKEY" ] && UPLINK_IF="$(iface_by_pubkey "$GATEWAY_PREV_PUBKEY")"
    # ЧИСТАЯ машина: аплинка нет вовсе. Документ конфигурации сервер AWG выпустил
    # под это устройство, и он несёт его ключи — кому пришёл, тот и шлюз. Ставим
    # аплинк под именем UPLINK_IF (по умолчанию awg0). Машина с ЧУЖИМ аплинком
    # шлюзом не становится — это ветка «foreign» ниже.
    _others="$(uplink_list | cut -d' ' -f1)"
    if [ -z "$UPLINK_IF" ] && [ -z "$_others" ] && [ -n "$UPLINK_B64" ] \
       && [ ! -f "$HOST_CONF_DIR/${UPLINK_IF_DEFAULT}.conf" ]; then
        UPLINK_IF="$UPLINK_IF_DEFAULT"
        say "  чистое устройство: аплинк $UPLINK_IF ставится из документа сервера AWG"
    fi
    if [ -n "$UPLINK_IF" ]; then
        say "  это помеченный шлюз: аплинк $UPLINK_IF"
        GW_STATUS="confirmed"
        # Аплинк на загрузке поднимается дольше, чем systemd готов ждать: пять
        # отказов за десять секунд (DNS ещё не резолвит Endpoint) — и юнит
        # сдаётся навсегда, шлюз остаётся без Telegram и без списков.
        # Снимаем лимит и перезапускаем до победы.
        # ExecStartPost: dnsmasq (апстрим server=…@аплинк) перечитывается после
        # подъёма аплинка отсюда, а не через After=/Wants= в drop-in dnsmasq —
        # то давало цикл упорядочивания с nss-lookup.target, и systemd выкидывал
        # из него произвольную единицу (в худшем случае — сам аплинк).
        _ovr="/etc/systemd/system/awg-quick@$UPLINK_IF.service.d"
        _ovr_txt="$(printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=on-failure\nRestartSec=10\nExecStartPost=-/bin/systemctl --no-block try-restart dnsmasq.service\n')"
        if [ "$(cat "$_ovr/awg-gw.conf" 2>/dev/null)" != "$_ovr_txt" ]; then
            run "mkdir -p $_ovr"
            run "printf '[Unit]\nStartLimitIntervalSec=0\n\n[Service]\nRestart=on-failure\nRestartSec=10\nExecStartPost=-/bin/systemctl --no-block try-restart dnsmasq.service\n' > $_ovr/awg-gw.conf"
            run "systemctl daemon-reload"
        else
            say "  оверрайд awg-quick@$UPLINK_IF (перезапуск до победы, dnsmasq после подъёма) уже есть"
        fi
        if [ -n "$UPLINK_B64" ]; then
            # временный каталог тут ни к чему: файл живёт рядом с
            # прочим состоянием шлюза и сразу удаляется
            _tmp="$GW_ETC/uplink.new"
            # внутри приватный ключ и PSK: файл только root, и уходит при любом
            # выходе (set -e не даёт дойти до rm -f внизу)
            _um="$(umask)"; umask 077
            trap 'rm -f "$_tmp" "$_tmp.conf"' EXIT
            printf '%s' "$UPLINK_B64" | base64 -d > "$_tmp" 2>/dev/null || : > "$_tmp"
            if grep -q '^PrivateKey' "$_tmp"; then
                uplink_render "$_tmp" "$_tmp.conf"
                if ! uplink_install "$_tmp.conf"; then
                    rm -f "$_tmp" "$_tmp.conf"
                    exit 1
                fi
            else
                say "  конфиг аплинка в файле конфигурации не разобрался — не трогаю"
            fi
            rm -f "$_tmp" "$_tmp.conf"
            trap - EXIT; umask "$_um"
        fi
    elif [ -n "$_others" ]; then
        say "  ВНИМАНИЕ: конфигурация слота ($LINK_IF) выпущена ДРУГОМУ устройству"
        say "  (ключ ${GATEWAY_PUBKEY%%????????????????????????????????}…), у этого устройства аплинк: $(printf '%s' "$_others" | tr '\n' ' ')."
        say "  Линк слота НЕ поднимаю: трафик пошёл бы мимо помеченного шлюза."
        say "  Агент запросит пометку у бота сервера AWG (по каналу конфигурации или пересылкой"
        say "  сообщения); после пометки примени конфигурацию шлюза ещё раз."
        GW_FOREIGN=1
        GW_STATUS="foreign"
    else
        # Аплинка нет ни живого, ни в конфигах — сказать «шлюз чужой» не по чему.
        # Раньше эта ветка сливалась с «чужим»: линк опускался, юнит выключал
        # сам себя, и машина не вставала обратно даже когда аплинк поднимался.
        say "  ВНИМАНИЕ: аплинка этого устройства не видно — ни интерфейса, ни конфига в $HOST_CONF_DIR."
        say "  Подтвердить, что шлюз слота ($LINK_IF) — это устройство, нечем: линк не трогаю."
        say "  Подними аплинк (awg-quick up <имя>) и примени конфигурацию слота ещё раз."
        GW_UNCONFIRMED=1
        GW_STATUS="unconfirmed"
    fi
fi
write_status() {   # $1 = состояние линка
    [ "$MODE" = "plan" ] && return 0
    printf 'GW_STATUS=%s\nGATEWAY_PUBKEY=%s\nUPLINK=%s\nUPLINK_IF=%s\nLINK=%s\nLAN=%s\nLAN_IF=%s\nLAN_ADDR=%s\nLAN_ERROR=%s\nSSH_PORT=%s\nSSH_FILTER=%s\nSSH_ALLOW_COUNT=%s\nROUTER=%s\n' \
        "$GW_STATUS" "$GATEWAY_PUBKEY" "$UPLINK_STATE" "${UPLINK_IF:-}" "$1" \
        "${LAN_MODE:-0}" "${LAN_IF:-}" "${LAN_ADDR:-}" "${LAN_ERROR:-}" \
        "$SSH_PORT" "$SSH_FILTER" "$(set -- $SSH_ALLOW; echo $#)" "${ROUTER4:-}" > "$GW_STATUS_FILE"
}
write_status "pending"

# ── 1. конфиг и подъём ───────────────────────────────────────────────────────
[ -n "$SRC_CONF" ] && [ -f "$SRC_CONF" ] || {
    say ""
    say "ОШИБКА: укажи файл конфига, полученный с сервера AWG:"
    say "  $0 --apply gw-awglink.conf"
    exit 1; }

step "1. Конфиг и подъём $LINK_IF"
mkdir -p "$HOST_CONF_DIR"
# Источник может СОВПАДАТЬ с назначением: так бывает при повторном прогоне
# «поверх» уже установленного конфига. `install` в этом случае падает с «are the
# same file», а при set -e уносит с собой весь остальной обвяз — который как раз
# и надо доставить.
# Конфиг не изменился и линк поднят — НЕ трогаем: рестарт линка рвёт РФ-доступ
# у всех, а ради того же самого конфига рвать нечего (правила ниже идемпотентны).
LINK_SAME=0
if [ -f "$HOST_CONF_DIR/$LINK_IF.conf" ] && cmp -s "$SRC_CONF" "$HOST_CONF_DIR/$LINK_IF.conf"; then
    LINK_SAME=1
fi
if [ "$(readlink -f "$SRC_CONF")" = "$(readlink -f "$HOST_CONF_DIR/$LINK_IF.conf")" ]; then
    say "  конфиг уже на месте — копировать не нужно"
elif [ "$LINK_SAME" = "1" ]; then
    say "  конфиг не изменился — копировать не нужно"
else
    run "install -m 600 '$SRC_CONF' $HOST_CONF_DIR/$LINK_IF.conf"
fi
[ -n "$AWG_QUICK" ] || { say "ОШИБКА: awg-quick не найден на ХОСТЕ."; \
    say "  Собери amneziawg-tools той же версии, что и модуль ядра."; exit 1; }
if [ "$GW_FOREIGN" = "1" ]; then
    # Юнит обвязки НЕ выключаем: он идемпотентен и при следующем старте
    # перепроверит пометку сам. Выключенный юнит означал ручное вмешательство
    # даже там, где машина шлюзом осталась.
    say "  линк не поднимаю (слот помечен другому устройству); если был поднят — опускаю"
    run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    write_status "foreign"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк лежит до пометки этого устройства шлюзом слота."
    exit 0
fi
if [ "$GW_UNCONFIRMED" = "1" ]; then
    say "  линк не трогаю (аплинк не найден — шлюз слота не подтверждён)"
    write_status "unconfirmed"
    say ""
    say "Готово частично: конфиг и скрипт на месте, линк — как был, до подъёма аплинка."
    exit 0
fi
if ip link show "$LINK_IF" >/dev/null 2>&1 && [ "$LINK_SAME" = "1" ]; then
    say "  интерфейс поднят, конфиг тот же — линк не перезапускаю"
else
    if ip link show "$LINK_IF" >/dev/null 2>&1; then
        say "  интерфейс уже поднят — перезапускаю, чтобы подхватить конфиг"
        run "$AWG_QUICK down $LINK_IF 2>/dev/null || true"
    fi
    # Таблица защиты — ДО подъёма линка. Свежая собирается на шаге 2 по подсети
    # линка у ядра, а между up и nft -f клиенты сервера AWG доходили бы до
    # самого устройства (SSH, OMV). Прежняя таблица с прошлого прогона держит
    # окно закрытым — свежая ляжет поверх (у самого первого прогона файла нет).
    if [ -f "$GUARD_FILE" ]; then
        run "nft -f $GUARD_FILE || echo '  прежняя таблица защиты не загрузилась — свежая встанет после подъёма'"
    fi
    run "$AWG_QUICK up $LINK_IF"
fi

# Проверяем ДЕЛОМ, а не по коду возврата: при расхождении версий awg-quick
# создаёт интерфейс, спотыкается на setconf и молча удаляет его обратно —
# завершаясь успешно. Снаружи это выглядит как «скрипт отработал», а линка нет.
if ! ip link show "$LINK_IF" >/dev/null 2>&1; then
    say ""
    say "ОШИБКА: интерфейс $LINK_IF не поднялся."
    say "  Почти всегда это РАСХОЖДЕНИЕ ВЕРСИЙ: модуль ядра и amneziawg-tools"
    say "  из разных поколений. Начиная с v3 параметры H1..H4 передаются как"
    say "  64-битные диапазоны, утилиты v1 шлют 32 бита — netlink отвергает."
    say "  Проверь:  awg --version   и   modinfo amneziawg | head -3"
    exit 1
fi
say "  Интерфейс поднят: $($AWG_BIN show "$LINK_IF" 2>/dev/null | head -1)"

# Подсеть линка берём У ЯДРА, а не из конфига: конфиг мог быть не применён, а
# нам нужно то, что реально назначено. /30 ⇒ сеть считается из адреса.
LINK_CIDR="$(link_cidr_of "$LINK_IF")"
LINK_PEER="$(link_peer_of "$LINK_IF")"
[ -n "$LINK_CIDR" ] && [ -n "$LINK_PEER" ] || { say "ОШИБКА: не удалось определить подсеть $LINK_IF"; exit 1; }
say "  Подсеть линка: $LINK_CIDR, сервер AWG на линке: $LINK_PEER"

# ── 1a. форвардинг в ядре ────────────────────────────────────────────────────
# Без него правила ниже стоят и не работают: пакет не выйдет из шлюза наружу, а
# ошибки не будет ни одной. Полагаться на то, что ip_forward включил кто-то
# другой, — значит на удаче: не запустился тот сервис, или его убрали, и шлюз
# молча перестаёт быть шлюзом.
step "1a. net.ipv4.ip_forward"
if [ "$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)" = "1" ]; then
    say "  уже 1"
else
    run "sysctl -w net.ipv4.ip_forward=1"
fi
# Отдельным файлом — иначе значение живёт до перезагрузки, а вернувшийся из
# ребута шлюз выглядит исправным и не пропускает ни пакета.
# rp_filter=2 (loose): ответы из интернета приходят в аплинк, а обратный путь
# до их источника по main лежит через локальный интерфейс — строгая проверка
# такое роняет. Loose пропускает, если маршрут к источнику есть хоть где-то;
# выключать проверку совсем незачем. Аплинк — PostUp его конфига (интерфейса
# на момент применения sysctl ещё нет).
run "printf 'net.ipv4.ip_forward = 1\\nnet.ipv4.conf.all.rp_filter = 2\\nnet.ipv4.conf.default.rp_filter = 2\\n' > $SYSCTL_CONF"
run "sysctl -qw net.ipv4.conf.all.rp_filter=2 net.ipv4.conf.default.rp_filter=2"

# ── 1b. политика «Telegram → аплинк»: правило по метке и таблица ─────────────
# Ставит PostUp аплинка при подъёме, но systemd-networkd при (пере)запуске по
# умолчанию сносит чужие ip rule и маршруты: аплинк жив, метка стоит, а
# Telegram агента уходит локальному провайдеру. Запрещаем networkd их трогать
# (drop-in читается при его следующем старте — ровно тогда, когда он и снёс
# бы) и перевыставляем политику здесь: реассерт юнита её тоже чинит.
step "1b. Политика Telegram → аплинк"
NETWORKD_DROPIN="/etc/systemd/networkd.conf.d/awg-gw.conf"
if [ -d /etc/systemd ] && command -v networkctl >/dev/null 2>&1; then
    if [ -f "$NETWORKD_DROPIN" ]; then
        say "  drop-in networkd уже есть"
    else
        run "mkdir -p /etc/systemd/networkd.conf.d"
        run "printf '[Network]\nManageForeignRoutes=no\nManageForeignRoutingPolicyRules=no\n' > $NETWORKD_DROPIN"
    fi
    # Сами интерфейсы awg* networkd тоже не должен подхватывать: с ним на
    # каждом перезапуске (любой Apply в OMV) оба туннеля переставали слать
    # пакеты — хендшейк застывал, sent не рос, юниты active. Снаружи это
    # выглядело отказом ВПС.
    if [ -f "$NETWORKD_UNMANAGED" ]; then
        say "  awg* для networkd unmanaged — уже"
    else
        run "mkdir -p /etc/systemd/network"
        run "printf '[Match]\nName=awg*\n\n[Link]\nUnmanaged=yes\n' > $NETWORKD_UNMANAGED"
        # .network читается при старте networkd и по reload; рестарт делать нельзя —
        # уронит LAN. reload безопасен: конфиг локального интерфейса не менялся.
        run "networkctl reload 2>/dev/null || true"
    fi
else
    say "  systemd-networkd не используется — drop-in не нужен"
fi
if [ -n "${UPLINK_IF:-}" ] && [ "$GW_STATUS" = "confirmed" ] && ip link show "$UPLINK_IF" >/dev/null 2>&1; then
    if ip rule list | grep -q "fwmark $TG_MARK lookup $UPLINK_TABLE"; then
        say "  правило по метке есть"
    else
        run "ip rule add fwmark $TG_MARK lookup $UPLINK_TABLE"
    fi
    run "ip route replace default dev $UPLINK_IF table $UPLINK_TABLE"
else
    say "  аплинк не поднят или шлюз не помечен — политику ставит PostUp при подъёме"
fi

# ── 2. таблица nft — вся обвязка одним атомарным файлом ──────────────────────
step "2. Таблица $GUARD_TABLE"
say "  MASQUERADE $CLIENT_SUBNET и $LINK_CIDR → $WAN_IF: российские сервисы увидят"
say "  локальный адрес; с адреса линка ходит зонд живости с сервера AWG."
say "  Изоляция: клиентам из туннеля закрыты все приватные сети (NAS, роутер,"
say "  link-local), остальное — транзит наружу."
say "  Защита шлюза: с адресов туннеля на само устройство пускаем только сервер AWG по"
say "  линку ($LINK_PEER: SSH, ICMP); устройствам админа открыто всё, включая"
say "  локальную сеть; прочее дропается."
say "  Метки Telegram ($TG_MARK): агенту нужен Telegram через сервер AWG."
command -v nft >/dev/null 2>&1 || { say "ОШИБКА: нет nft — apt install nftables"; exit 1; }
ADMIN_ELEMS="$(ipv4_list $ADMIN_IPS $ADMIN_IPS_EXTRA)"
PEER_ELEMS="$(ipv4_list $PEER_LAN_NETS)"
say "  Устройства админа: ${ADMIN_ELEMS:-— (никому, кроме сервера AWG по линку)}"
# Маскарад соседей к роутеру (VPN-транзит): роутер отвечает со своего адреса по
# основной таблице, мимо заворота на шлюз, — адрес соседа в туннеле для него
# чужой, и ответ ушёл бы провайдеру. Трафик соседей к адресу роутера уходит с
# адресом шлюза в локальной сети, и роутер отвечает шлюзу. Только к роутеру:
# прочие устройства сети видят адрес соседа в туннеле, их ответ возвращает
# заворот на роутере. Нет VPN-транзита, соседей или адреса роутера — правила нет.
ROUTER_MASQ=""
if [ "$LAN_MODE" = "1" ] && [ -n "$PEER_ELEMS" ] && [ -n "$ROUTER4" ]; then
    ROUTER_MASQ="        iifname \"$LINK_IF\" ip saddr @peer_nets4 ip daddr $ROUTER4 masquerade"
    say "  Подсети других шлюзов → роутер $ROUTER4: маскарад адресом шлюза (роутер отвечает шлюзу)"
fi
# SSH снаружи (через проброс на роутере): при SSH_FILTER=1 на порт sshd пускаются
# только локальная сеть, подсети других шлюзов, сервер и адреса из SSH_ALLOW (имена — по последнему
# резолву агента, SSH_ALLOW_RESOLVED; ipv4_list имена отсеивает сам).
SSH_ALLOW_ELEMS="$(ipv4_list $SSH_ALLOW $SSH_ALLOW_RESOLVED)"
SERVER_ELEMS="$(server_ipv4 "$SRC_CONF")"
say "  SSH снаружи (порт $SSH_PORT): фильтр $([ "$SSH_FILTER" = 1 ] && echo включён || echo выключен);"
say "  адреса: ${SSH_ALLOW_ELEMS:-—}; сервер снаружи: ${SERVER_ELEMS:-— (адрес не определён)}"
SSH_JUMP=""
# Только IPv4: таблица inet, а accept'ы в ssh_in — по ip saddr; без nfproto
# v6-пакет доходил бы до drop, и «из локальной сети всегда» ломалось бы.
[ "$SSH_FILTER" = 1 ] && SSH_JUMP="        meta nfproto ipv4 tcp dport $SSH_PORT jump ssh_in"
LAN_ELEMS="$(lan_ipv4 "$WAN_IF")"
say "  локальная сеть (SSH снаружи не фильтруется): ${LAN_ELEMS:-RFC1918 целиком — подсеть $WAN_IF не определена}"
[ -n "$LAN_ELEMS" ] || LAN_ELEMS="10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"
if [ "$MODE" = "plan" ]; then
    say "  would: записать $GUARD_FILE и применить: nft -f $GUARD_FILE"
else
mkdir -p "$GW_ETC"
# Маскарад в АПЛИНК — для самого агента. Локально рождённый пакет выбирает
# исходный адрес до метки в output: берёт адрес локального интерфейса, а после
# перемаршрутизации по метке улетает в аплинк с чужим src — ВПС его отбросит
# (у пира разрешён только адрес аплинка). Маскарад подменяет src на адрес
# аплинка. Без него агент на чистой машине нем: на прежнем шлюзе это делало
# чужое правило прежней схемы, и отсутствие своего не было видно.
UPLINK_MASQ=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MASQ="        oifname \"$UPLINK_IF\" masquerade"
# MSS-кламп в аплинк: TCP в туннель без него упирается в MTU туннеля и виснет
# на больших ответах (TLS-рукопожатие с сертификатом). Ставится в postrouting:
# там виден и транзит из локальной сети, и СОБСТВЕННЫЙ трафик устройства
# (агент: Telegram, GitHub) — SYN агента строится под MTU end0 и лишь потом
# по метке уходит в аплинк; в forward его нет, и без клампа агент зависит от
# ICMP «fragmentation needed», который по дороге теряется.
UPLINK_MSS=""
[ -n "${UPLINK_IF:-}" ] && UPLINK_MSS="        oifname \"$UPLINK_IF\" tcp flags syn / syn,rst tcp option maxseg size set rt mtu"
{
cat <<GUARDEOF
#!/usr/sbin/nft -f
# awg-bot (шлюз): обвязка условной маршрутизации и защита самого устройства от туннеля.
# Генерирует routing-gw-setup.sh при каждом старте юнита awg-link-gw.service —
# правки руками перезапишутся. Устройства админа (полный доступ с туннеля):
# ADMIN_IPS из файла конфигурации с ВПС плюс ADMIN_IPS_EXTRA из $FW_ENV (awg-bot firewall).
table $GUARD_TABLE
delete table $GUARD_TABLE
table $GUARD_TABLE {
    set tunnel_nets4 {
        type ipv4_addr
        flags interval
        elements = { $CLIENT_SUBNET, $LINK_CIDR }
    }
    set private4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $PRIVATE_NETS) }
    }
    set tg_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $TG_NETS) }
    }
    set gh_nets4 {
        type ipv4_addr
        flags interval
        elements = { $(ipv4_list $GH_NETS) }
    }
    set admin4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$ADMIN_ELEMS" ] && printf '        elements = { %s }\n' "$ADMIN_ELEMS"
cat <<GUARDEOF
    }
    set peer_nets4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$PEER_ELEMS" ] && printf '        elements = { %s }\n' "$PEER_ELEMS"
cat <<GUARDEOF
    }
    set ssh_allow4 {
        type ipv4_addr
        flags interval
GUARDEOF
[ -n "$SSH_ALLOW_ELEMS" ] && printf '        elements = { %s }\n' "$SSH_ALLOW_ELEMS"
cat <<GUARDEOF
    }
    set server4 {
        type ipv4_addr
GUARDEOF
[ -n "$SERVER_ELEMS" ] && printf '        elements = { %s }\n' "$SERVER_ELEMS"
cat <<GUARDEOF
    }
    set lan4 {
        type ipv4_addr
        flags interval
        elements = { $LAN_ELEMS }
    }

    chain input {
        type filter hook input priority filter; policy accept;
        iifname "lo" accept
        ip saddr @tunnel_nets4 jump tunnel_in
$SSH_JUMP
    }
    chain tunnel_in {
        ct state established,related accept
        ip protocol icmp accept
        ip saddr @admin4 accept
        ip saddr $LINK_PEER tcp dport $SSH_PORT accept
        drop
    }
    chain ssh_in {
        ct state established,related accept
        ip saddr @lan4 accept
        ip saddr @peer_nets4 accept
        ip saddr @server4 accept
        ip saddr @ssh_allow4 accept
        drop
    }

    chain postrouting_mss {
        type filter hook postrouting priority mangle; policy accept;
$UPLINK_MSS
    }

    chain forward {
        type filter hook forward priority filter; policy accept;
        oifname "$LINK_IF" ct state established,related accept
        iifname "$LINK_IF" ip saddr @admin4 accept
        iifname "$LINK_IF" ip saddr @peer_nets4 accept
        iifname "$LINK_IF" ip daddr @private4 drop
        iifname "$LINK_IF" accept
    }

    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr @tunnel_nets4 oifname "$WAN_IF" masquerade
$UPLINK_MASQ
$ROUTER_MASQ
    }

    chain output {
        type route hook output priority mangle; policy accept;
        ip daddr @tg_nets4 meta mark set $TG_MARK
        ip daddr @gh_nets4 meta mark set $TG_MARK
    }
}
GUARDEOF
} > "$GUARD_FILE.tmp"
chmod 0644 "$GUARD_FILE.tmp"
nft -c -f "$GUARD_FILE.tmp" || { say "ОШИБКА: nft отклонил таблицу — $GUARD_FILE.tmp"; exit 1; }
mv "$GUARD_FILE.tmp" "$GUARD_FILE"
run "nft -f $GUARD_FILE"
# карантин подключения командой (--join) снимает только конец сопряжения
# (JOIN_FINISH=1 от агента после смены ключей по коду): прежний юнит
# установленного шлюза, перезапущенный посреди сопряжения, его не трогает
if [ -f "$GW_ETC/join.nft" ] || nft list table $JOIN_TABLE >/dev/null 2>&1; then
    if [ "${JOIN_FINISH:-}" = "1" ]; then
        run "nft delete table $JOIN_TABLE 2>/dev/null || true"
        run "rm -f $GW_ETC/join.nft"
    else
        say "  карантин подключения командой остаётся до конца сопряжения"
        [ -f "$GW_ETC/join.nft" ] && run "nft -f $GW_ETC/join.nft"
    fi
fi
fi

# ── 3. прежняя обвязка в iptables — снять ────────────────────────────────────
step "3. Снятие прежних правил iptables"
say "  Таблица держит то же самое; два владельца одних правил не нужны."
legacy_cleanup

LAN_IF_PRE=""
if [ "$LAN_MODE" = "1" ] && [ -n "${LAN_SUBNETS%% *}" ]; then
    _r="$(lan_iface_for "${LAN_SUBNETS%% *}")"; LAN_IF_PRE="${_r%% *}"
fi
# ── 3a. чужая политика FORWARD ───────────────────────────────────────────────
# accept в нашей inet-таблице не отменяет DROP в чужой ip filter FORWARD: её
# ставит чужой файервол, включивший ip_forward, и транзит из линка,
# из аплинка и из локальной сети умирает молча. Правила — в его же цепочке,
# идемпотентно; снимаются при откате. Только когда политика действительно DROP.
step "3a. Чужая политика FORWARD"
fwd_allow() {                  # $1 = -i|-o, $2 = интерфейс
    [ -n "$2" ] || return 0
    iptables -w -C FORWARD "$1" "$2" -j ACCEPT 2>/dev/null \
        || run "iptables -w -I FORWARD 1 $1 $2 -j ACCEPT"
}
if command -v iptables >/dev/null 2>&1 && iptables -w -S FORWARD 2>/dev/null | grep -q '^-P FORWARD DROP'; then
    say "  ip filter FORWARD: DROP (чужой файервол) — открываю транзит своим интерфейсам"
    for _i in "$LINK_IF" "${UPLINK_IF:-}" "${LAN_IF_PRE:-}"; do fwd_allow -i "$_i"; fwd_allow -o "$_i"; done
else
    say "  ip filter FORWARD не DROP — ничего не нужно"
fi

# ── 4. автозапуск ────────────────────────────────────────────────────────────
step "4. Автозапуск"
SELF="$(install_self)"
_unit_old="$(cat "$UNIT" 2>/dev/null || true)"
cat > "$UNIT" <<UNITEOF
[Unit]
Description=awg-bot: линк до сервера AWG и изоляция клиентов (шлюз)
After=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}
Wants=network-online.target${UPLINK_IF:+ awg-quick@$UPLINK_IF.service}

[Service]
Type=oneshot
RemainAfterExit=yes
# Подсеть вшита: app.yaml на шлюзе нет, смена подсети = новый файл конфигурации с ВПС.
Environment=CLIENT_SUBNET=$CLIENT_SUBNET
Environment=LINK_IF=$LINK_IF
# Устройства админа (полный доступ с туннеля) — из файла конфигурации с ВПС. Добавленное
# на самом шлюзе (awg-bot firewall allow) — в $FW_ENV.
Environment="ADMIN_IPS=$ADMIN_IPS"
Environment=GATEWAY_PUBKEY=$GATEWAY_PUBKEY
Environment=GATEWAY_PREV_PUBKEY=$GATEWAY_PREV_PUBKEY
# Локальная сеть без VPN: флаг, подсети, резолвер ВПС.
Environment=LAN_MODE=$LAN_MODE
Environment="LAN_SUBNETS=$LAN_SUBNETS"
Environment=RESOLVER=$RESOLVER
Environment="PEER_LAN_NETS=$PEER_LAN_NETS"
# Канал до ВПС внутри линка обязателен; порт канала агент берёт отсюда.
Environment=LINK_CHANNEL_PORT=$LINK_CHANNEL_PORT
# прогон самого юнита: после замка сверяет строки выше с юнитом на диске
Environment=AWG_GW_UNIT_RUN=1
EnvironmentFile=-$FW_ENV
# Зовём этот же скрипт: он идемпотентен, источник истины один.
ExecStart=$SELF --apply $HOST_CONF_DIR/$LINK_IF.conf
# Полный прогон (apt, dnsmasq, nft) на загрузке идёт минуты — oneshot без
# предела времени висел бы на зависшем apt вечно.
TimeoutStartSec=15min
Restart=on-failure
# 60, не 10: при постоянном отказе (нет конфига, чужой :53) полный прогон
# каждые десять секунд — это сотни apt/nft в час
RestartSec=60

[Install]
WantedBy=multi-user.target
UNITEOF
# 0600: прежние выпуски закрепляли в юните UPLINK_B64 с приватным ключом
# аплинка и оставляли 0644 — любой локальный пользователь шлюза (OMV, шары)
# мог его прочитать. Секрета в юните больше нет, права держим строгими:
# юнит читает только systemd.
chmod 0600 "$UNIT"
if [ "$_unit_old" = "$(cat "$UNIT")" ]; then
    say "  юнит без изменений — daemon-reload не нужен"
else
    run "systemctl daemon-reload"
fi
systemctl is-enabled -q awg-link-gw.service 2>/dev/null || run "systemctl enable awg-link-gw.service"
# ── 5. локальная сеть: «за шлюзом — без VPN» ─
# Роутер заворачивает весь трафик локальной сети на шлюз, шлюз делит его
# сама: заблокированное — по метке аплинка в туннель, остальное — напрямую.
# Резолвер dnsmasq на LAN-адресе с апстримом через аплинк наполняет наборы
# доменами, скрипт списков — подсетями из фидов. Владелец всего ниже — этот
# скрипт: любая ручная правка перезапишется при следующем применении.
#
# Две таблицы nft — не случайность: awg_gw_guard перевыставляется атомарно
# (delete table), а наборы awg_lan наполняет dnsmasq на лету, и терять их при
# каждом реассерте нельзя — таблица создаётся БЕЗ delete, меняются только
# цепочки. Метка — та же, что у агента: смысл один («в аплинк»), правило и
# маршрут уже ставит PostUp аплинка, а чинит агент.
step "5. VPN-транзит"
LAN_ERROR=""
lan_fail() { LAN_ERROR="$1"; say "  ОШИБКА: $1"; return 1; }
lan_apply() {                  # отказ — return 1 с LAN_ERROR: юнит уже включён, ронять скрипт нельзя
    # Под lists.lock: скрипт фидов и свои списки пишут те же файлы и
    # перезапускают dnsmasq — выключение режима посреди скачивания иначе
    # получало фид обратно и nftset в таблицу, которой уже нет.
    [ -d "$LAN_DUMP" ] || mkdir -p "$LAN_DUMP" 2>/dev/null || true
    _lan_lock=0
    if [ -d "$LAN_DUMP" ] && command -v flock >/dev/null 2>&1; then
        exec 8>"$LAN_DUMP/lists.lock"
        if flock -w 120 8; then _lan_lock=1; else
            exec 8>&-
            lan_fail "списки локальной сети ещё обновляются — не дождался за 2 минуты"; return 1
        fi
    fi
    _lan_apply_locked; _rc=$?
    [ "$_lan_lock" = "1" ] && { flock -u 8; exec 8>&-; }
    return $_rc
}
_lan_apply_locked() {
    _net="${LAN_SUBNETS%% *}"
    [ -n "$_net" ] || { lan_fail "LAN_MODE=1 без локальной подсети — задай её в боте сервера AWG: «🗺 Подсети» в карточке шлюза"; return 1; }
    _r="$(lan_iface_for "$_net")"; LAN_IF="${_r%% *}"; LAN_ADDR="${_r#* }"; [ "$LAN_ADDR" = "$_r" ] && LAN_ADDR=""
    [ -n "$LAN_IF" ] || { lan_fail "на этом устройстве нет адреса из подсети $_net — не та подсеть или не то устройство"; return 1; }
    say "  локальная сеть $_net: интерфейс $LAN_IF, адрес шлюза $LAN_ADDR"
    [ -n "$UPLINK_IF" ] || { lan_fail "аплинк не известен — VPN-транзит без него не работает"; return 1; }
    # :53 занят кем-то, кроме dnsmasq (systemd-resolved на 0.0.0.0, Pi-hole,
    # второй dnsmasq)? Честный отказ ДО установки пакета: postinst dnsmasq
    # стартует демон, тот не сможет забиндить порт, и apt-get упадёт посреди дела.
    _busy="$(port53_busy)"
    [ -z "$_busy" ] || { lan_fail "порт 53 занят: $_busy — освободи его (systemd-resolved на 0.0.0.0, Pi-hole?) и примени ещё раз"; return 1; }
    _upstream="${RESOLVER:-1.1.1.1}"
    [ -n "$RESOLVER" ] || say "  резолвера сервера AWG нет — апстрим 1.1.1.1 через аплинк (без защиты от DoH и общего кэша)"
    # Имена Endpoint аплинка и линка — через DNS роутера, мимо туннеля: DNS
    # устройства = оно само (так требует рецепт роутера), и имя иначе
    # резолвилось бы только через ещё не поднятый туннель — взаимная
    # блокировка на загрузке (awg-quick@ ждёт резолва бесконечно).
    _ep_dns="$VIA4"
    _ep_names="$(for _c in "$HOST_CONF_DIR/$UPLINK_IF.conf" "$HOST_CONF_DIR/$LINK_IF.conf"; do
        sed -n 's/^Endpoint *= *\(.*\):[0-9][0-9]*$/\1/p' "$_c" 2>/dev/null; done | grep -Ev '^[0-9.]+$|^\[' | sort -u)"
    _dn_changed=0
    run "mkdir -p $DNSMASQ_D $LAN_DUMP"
    lan_table_move_prepare      # переход с прежнего имени таблицы — снимается со следующим сдвигом минимума
    # ── конфиги dnsmasq ДО установки пакета: первый старт демона сразу с нашим
    # listen-address, а не wildcard на :53. Временные файлы — вне conf-dir.
    _tmp="$(mktemp "$LAN_DUMP/dnsmasq.XXXXXX")"
    {
        printf '# awg-bot (шлюз): резолвер локальной сети без VPN. Владелец — routing-gw-setup.sh.\n'
        # bind-dynamic: с bind-interfaces демон падает, пока аплинк не поднялся.
        # Однократные ключи (bind-*, cache-size) — только если их нет в других файлах.
        dn_set_elsewhere '^(bind-interfaces|bind-dynamic)$' || printf 'bind-dynamic\n'
        printf 'listen-address=127.0.0.1,%s\n' "$LAN_ADDR"
        printf 'no-resolv\nno-hosts\n'
        # апстрим ЧЕРЕЗ АПЛИНК: без @iface запрос ушёл бы линком с адресом линка
        printf 'server=%s@%s\n' "$_upstream" "$UPLINK_IF"
        if [ -n "$_ep_dns" ]; then
            for _n in $_ep_names; do printf 'server=/%s/%s\n' "$_n" "$_ep_dns"; done
        fi
        dn_set_elsewhere '^cache-size=' || printf 'cache-size=10000\n'
        printf 'stop-dns-rebind\nrebind-localhost-ok\n'
        # IPv6 в локальной сети выключить со шлюза нельзя (RA раздаёт роутер, networkd
        # OMV включает v6 на интерфейсе при каждом Apply) — зато AAAA можно не
        # отдавать: без адреса v6 трафик мимо туннеля не уйдёт. Пакета ещё нет
        # (первое применение: конфиг пишется ДО apt) — ключ кладём: apt ставит
        # dnsmasq ≥ 2.89 (Debian 12+), а без него первый же старт раздавал бы
        # AAAA до следующего реассерта — днями
        if ! command -v dnsmasq >/dev/null 2>&1 || dnsmasq --help 2>&1 | grep -q 'filter-AAAA'; then
            printf 'filter-AAAA\n'
        fi
    } > "$_tmp"
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-base.conf"; then rm -f "$_tmp"; else
        run "install -m 0644 $_tmp $DNSMASQ_D/awg-gw-base.conf"; rm -f "$_tmp"; _dn_changed=1; fi
    _tmp="$(mktemp "$LAN_DUMP/dnsmasq.XXXXXX")"
    cat > "$_tmp" <<'DOHEOF'
# awg-bot (шлюз): DoH мимо резолвера — иначе адрес не попадает в набор и
# заблокированное идёт напрямую («часть сайтов через раз»). Тот же список,
# что у резолвера ВПС. Канареечный домен Firefox: NXDOMAIN выключает DoH.
address=/use-application-dns.net/
address=/cloudflare-dns.com/
address=/chrome.cloudflare-dns.com/
address=/mozilla.cloudflare-dns.com/
address=/one.one.one.one/
address=/dns.google/
address=/dns.quad9.net/
address=/dns.adguard-dns.com/
address=/doh.opendns.com/
address=/dns.nextdns.io/
address=/firefox.dns.nextdns.io/
DOHEOF
    if cmp -s "$_tmp" "$DNSMASQ_D/awg-gw-doh.conf"; then rm -f "$_tmp"; else
        run "install -m 0644 $_tmp $DNSMASQ_D/awg-gw-doh.conf"; rm -f "$_tmp"; _dn_changed=1; fi
    for _f in awg-gw-vpn-user.conf awg-gw-ru-user.conf; do
        [ -f "$DNSMASQ_D/$_f" ] && continue
        # снятие режима откладывает личные списки сюда: подхватываем их при
        # следующем включении
        if [ -f "$LAN_DUMP/restore/$_f" ]; then
            run "mv -f $LAN_DUMP/restore/$_f $DNSMASQ_D/$_f"
        else
            run "printf '# awg-bot (шлюз): личный список — awg-bot lan add/ru/del\\n' > $DNSMASQ_D/$_f"
        fi
        _dn_changed=1
    done
    # /etc/default/dnsmasq НЕ трогаем: DNSMASQ_EXCEPT init-скрипт Debian
    # превращает в except-interface, а тот по man перекрывает listen-address;
    # на чистом шлюзе файл, созданный до установки пакета, — его conffile:
    # dpkg без терминала падает на вопросе о нём.
    # Оверрайд юнита: перезапуск при отказе; старт ПОСЛЕ аплинка — апстрим
    # server=…@<аплинк> привязывается к интерфейсу, и dnsmasq, стартовавший
    # раньше awg0 (так и было на живых шлюзах), оставался без апстрима до
    # ручного рестарта; хук resolvconf Debian выключен — он вписал бы 127.0.0.1
    # системным резолвером шлюза, и его собственный DNS (агент, awg-quick, apt)
    # зависел бы от аплинка. Хук снимаем, только если он в юните и есть.
    _ovr_want="$(mktemp)"
    {
        printf '# awg-bot (шлюз): оверрайд dnsmasq под локальную сеть без VPN.\n'
        # без After=/Wants=awg-quick@аплинк: у dnsmasq штатно Before=nss-lookup.target,
        # у awg-quick@ After=nss-lookup.target — цикл; перечитывание после
        # подъёма аплинка делает ExecStartPost в оверрайде самого аплинка
        printf '[Service]\nRestart=on-failure\nRestartSec=5\n'
        if systemctl cat dnsmasq.service 2>/dev/null | grep -q 'start-resolvconf'; then
            printf 'ExecStartPost=\nExecStop=\n'
        fi
    } > "$_ovr_want"
    if ! cmp -s "$_ovr_want" "$DNSMASQ_OVR"; then
        run "mkdir -p $(dirname "$DNSMASQ_OVR")"
        run "install -m 0644 $_ovr_want $DNSMASQ_OVR"
        run "systemctl daemon-reload"; _dn_changed=1
    fi
    rm -f "$_ovr_want"
    # dnsmasq и dig (наполнение набора после ручного добавления домена)
    if ! command -v dnsmasq >/dev/null 2>&1 || ! command -v dig >/dev/null 2>&1; then
        say "  ставлю dnsmasq и dnsutils"
        _had_dnsmasq=0; command -v dnsmasq >/dev/null 2>&1 && _had_dnsmasq=1
        run "apt-get update -q >/dev/null 2>&1 || true"
        # confdef/confold — на вопрос о конфигах отвечать самим, без терминала
        # (юнит его не даёт); Lock::Timeout — OMV мог держать dpkg своим apt
        run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dnsmasq dnsutils" \
            || { lan_fail "dnsmasq не установился — смотри вывод apt выше"; return 1; }
        # метка «dnsmasq ставили мы» — только если его не было: ставили один
        # dnsutils ради dig — выключение режима иначе гасило бы чужой dnsmasq
        [ "$_had_dnsmasq" = "1" ] || run "touch $DNSMASQ_MARK"
        _dn_changed=1
    fi
    # ── sysctl: rp_filter loose на LAN
    run "printf 'net.ipv4.conf.$(printf '%s' "$LAN_IF" | tr . /).rp_filter = 2\\n' > $LAN_SYSCTL"
    run "sysctl -qw net.ipv4.conf.$(printf '%s' "$LAN_IF" | tr . /).rp_filter=2 || true"
    # ── таблица awg_lan: наборы без delete (переживают реассерт), цепочки заново
    {
        cat <<LANEOF
# awg-bot (шлюз): локальная сеть без VPN. Владелец — routing-gw-setup.sh.
# Без «delete table»: наборы наполняет dnsmasq на лету, их нельзя терять при реассерте.
table $LAN_TABLE {
    set lan_vpn4      { type ipv4_addr; flags interval; auto-merge; }
    set lan_vpn_nets4 { type ipv4_addr; flags interval; auto-merge; }
    set lan_ru4       { type ipv4_addr; flags interval; auto-merge; }
    chain prerouting  { type filter hook prerouting priority mangle; policy accept; }
    chain input       { type filter hook input priority filter; policy accept; }
    chain forward     { type filter hook forward priority filter; policy accept; }
    chain postrouting { type nat hook postrouting priority srcnat; policy accept; }
}
flush chain $LAN_TABLE prerouting
flush chain $LAN_TABLE input
flush chain $LAN_TABLE forward
flush chain $LAN_TABLE postrouting
# из линка и аплинка — мимо маркировки; исключения ВЫШЕ метки (accept обрывает обход)
add rule $LAN_TABLE prerouting iifname != "$LAN_IF" accept
# счётчик для агента: заворот с роутера — только unicast на MAC шлюза (до любых
# вердиктов); mDNS/SSDP/DHCP-бродкасты фонят круглые сутки и считаться не должны
add rule $LAN_TABLE prerouting meta pkttype host ip daddr != $_net counter
add rule $LAN_TABLE prerouting ip daddr @lan_ru4 accept
add rule $LAN_TABLE prerouting ip daddr @lan_vpn_nets4 meta mark set $TG_MARK
add rule $LAN_TABLE prerouting ip daddr @lan_vpn4 meta mark set $TG_MARK
# счётчик для агента: DNS с роутера (раздаёт ли DHCP адрес шлюза)
add rule $LAN_TABLE input iifname "$LAN_IF" udp dport 53 counter
# DoT мимо резолвера — как на ВПС для клиентов
add rule $LAN_TABLE forward iifname "$LAN_IF" tcp dport 853 drop
# ответ на прямой трафик должен вернуться на шлюз, а не на клиента мимо него
add rule $LAN_TABLE postrouting ip saddr $_net oifname "$LAN_IF" masquerade
LANEOF
    } > "$LAN_FILE.tmp"
    chmod 0644 "$LAN_FILE.tmp"
    nft -c -f "$LAN_FILE.tmp" || { lan_fail "nft отклонил таблицу локальной сети — $LAN_FILE.tmp"; return 1; }
    mv "$LAN_FILE.tmp" "$LAN_FILE"
    run "nft -f $LAN_FILE" || { lan_fail "таблица локальной сети не применилась"; return 1; }
    # слепки наборов — до фидов: первая минута после загрузки не остаётся без списков
    for _s in lan_vpn4 lan_vpn_nets4; do
        [ -s "$LAN_DUMP/$_s.nft" ] && run "nft -f $LAN_DUMP/$_s.nft 2>/dev/null || true"
    done
    write_lan_scripts
    # ── сервисы соседних сетей: обзор своей
    # сети — avahi-browse при живом avahi-daemon (сам демон не ставим: он начал
    # бы объявлять шлюз); соседей нет — записи соседей снять
    # (обзор своей сети ставится ниже, вне режима: условие — свои подсети)
    if [ -z "${PEER_LAN_NETS:-}" ] && [ -f "$PEER_SVC_CONF" ]; then
        run "rm -f $PEER_SVC_CONF"; _dn_changed=1
    fi
    if [ "$_dn_changed" = "1" ] || ! systemctl is-active --quiet dnsmasq; then
        run "systemctl enable dnsmasq 2>/dev/null || true"
        run "systemctl restart dnsmasq" || { lan_fail "dnsmasq не запустился: journalctl -u dnsmasq -o short-iso -e"; return 1; }
    else
        say "  dnsmasq: конфиги не изменились"
    fi
    lan_table_move_finish       # переход с прежнего имени таблицы — снимается со следующим сдвигом минимума
    say "  списки — обновляет агент (первый раз через несколько минут); руками: $LAN_LISTS"
    return 0
}
if [ "$LAN_MODE" = "1" ]; then
    # В условии if set -e внутри функции не действует — потому каждый шаг с
    # последствиями сам решает, продолжать ли (|| return 1). Отказ — в статус,
    # его покажет агент; остальная обвязка стоит, юнит не уходит в цикл рестартов.
    if lan_apply; then :; else
        say "  VPN-транзит НЕ применён: ${LAN_ERROR:-см. выше}"
    fi
else
    if [ -f "$LAN_FILE" ] || [ -f "$DNSMASQ_D/awg-gw-base.conf" ]; then
        say "  выключено — снимаю своё"
        lan_remove
    else
        say "  выключено"
    fi
fi

# ── обзор SMB своей сети: имена уходят серверу AWG — соседним шлюзам (связь
# подсетей) и устройствам админа в туннеле («SMB в туннеле»); условие — свои
# подсети заданы. Сам avahi-daemon не ставим (он начал бы объявлять шлюз) —
# только avahi-utils рядом с уже запущенным демоном; списки apt на шлюзе могут
# быть старыми (404 на зеркале) — сначала update
if [ -n "${LAN_SUBNETS:-}" ] && systemctl is-active --quiet avahi-daemon 2>/dev/null \
        && ! command -v avahi-browse >/dev/null 2>&1; then
    say "  ставлю avahi-utils (обзор SMB-серверов этой подсети: для подсетей других шлюзов и устройств админа в туннеле)"
    run "apt-get update -q >/dev/null 2>&1 || true"
    run "DEBIAN_FRONTEND=noninteractive apt-get install -y -q -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold avahi-utils" \
        || say "  avahi-utils не установился — SMB-серверы этой подсети по именам не видны, остальное работает"
fi

write_status "up"
issued_record
step "Проверка"
say "  awg show $LINK_IF                            # есть ли хендшейк"
say "  ip -br addr show $LINK_IF"
say "  nft list table $GUARD_TABLE                  # вся обвязка одним взглядом"
say ""
say "Хендшейк появится, когда сервер AWG ответит; статус — в панели агента."
