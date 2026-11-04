# Развёртывание арены для кейса на одной VM

Схема: движки - в `stand/compose.yaml` (порты только на 127.0.0.1); три оценщика арены (train, public, private) - процессы на хосте под пользователем `arena` (режим арены «сервис без модели», `deploy/README.md` закреплённой ревизии арены, раздел «Сервис без модели»); кандидаты - одноразовые контейнеры `sqltr-runner:1` без сети; исследователь - контейнер в сети `arena-research`, видит только API оценщиков и общий журнал.

```
 researcher (uid 10001, сеть arena-research) ──HTTP+токен──> 172.30.0.1:8091 train / :8092 public / :8093 private
        │ rw /search (журнал)                                    │ (процессы хоста, пользователь arena)
        └──────────────── /srv/sqltr-arena/search ◄── читает ────┤
                                                                 ├─ docker run sqltr-runner:1 --network none (кандидат)
                                                                 └─ python score.py (метрика) ─> 127.0.0.1:5432 PG, :8080 Trino
```

Почему процессы на хосте, а не evaluator-контейнер из compose арены: пути state и assets совпадают на хосте и у Docker без настройки path parity, метрике не нужна общая сеть со стендом, а Docker socket получает только пользователь `arena`. Арена описывает этот режим как штатный для программных решателя и метрики.

## Первое развёртывание

Предусловия: `stand/README.md` выполнен (движки подняты, train построен, self-check пройден), арена склонирована в `/opt/arena` на закреплённой ревизии (ADR 0001) и её venv собран (`uv sync --frozen --no-dev`).

```bash
sudo install -d -m 0700 /etc/sqltr-arena
sudo cp case/deploy/operator.env.example /etc/sqltr-arena/operator.env   # заполнить, chmod 600
sudo cp operator/seeds.json /etc/sqltr-arena/seeds.json                  # секретные сиды public/private, не в git

sudo bash case/deploy/arena.sh layout     # пользователь arena, каталоги, токены, сеть arena-research, драйверы в venv арены
sudo bash case/deploy/arena.sh image      # sqltr-runner:1 (харнес ADR 0008); записать digest в campaign.md
sudo bash case/deploy/arena.sh accept     # синтетическая приёмка: 19 проверок, ~5 минут, отдельный каталог
sudo bash case/deploy/arena.sh freeze     # заморозка метрики, sha256 lock-файла -> campaign.md
sudo bash case/deploy/arena.sh data       # public/private из сидов оператора, эталоны на Trino
sudo bash case/deploy/arena.sh journal    # журнал кампании с MAX_SOLUTIONS
sudo bash case/deploy/arena.sh start      # три оценщика (systemd transient units)
sudo bash case/deploy/arena.sh readiness  # должен вернуть mode=campaign, gate_enabled=true, journal_ready=true
sudo bash case/deploy/arena.sh researcher-env   # URL для контейнера исследователя
```

Порядок важен: приёмка идёт до заморозки и до private-данных, она не трогает журнал и state кампании. `freeze` и `journal` выполняются один раз: повторный вызов откажет, чтобы не подменить условия работающей кампании.

## Каталоги и права

| Путь | Владелец, права | Кто видит |
| --- | --- | --- |
| `/etc/sqltr-arena/operator.env`, `seeds.json` | root, 0600 | только оператор |
| `/srv/sqltr-arena/metric` | arena, read-only | оценщики |
| `/srv/sqltr-arena/data/{train,public,private}` | arena, 0700 | оценщики; исследователь получает только train из репозитория |
| `/srv/sqltr-arena/state-*`, `usage-*` | arena, 0700 | оценщики; `state-private/*/record.json` - итог private для оператора |
| `/srv/sqltr-arena/search` | 10001:arena, 0770 | исследователь пишет, private-оценщик читает |
| `/srv/sqltr-arena/search-control` | arena, 0700 | свидетельство досрочной остановки - только оператор |
| `/srv/sqltr-arena/secrets/*-token` | arena, 0600 | оператор копирует исследователю в read-only mount |
| `/srv/sqltr-arena/env/*.env` | root, 0600 | systemd; содержит пароли движков |

## Контейнер исследователя

```bash
docker run -it --name sqltr-researcher --network arena-research --user 10001:10001 \
  --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,size=1g \
  --mount type=bind,src=/srv/sqltr-workspace,dst=/workspace \
  --mount type=bind,src=/srv/sqltr-arena/search,dst=/search \
  --mount type=bind,src=/srv/sqltr-research-secrets,dst=/run/secrets,readonly \
  --env-file <(sudo bash case/deploy/arena.sh researcher-env | grep -v '^#') \
  arena-researcher:local
```

`/srv/sqltr-workspace` - чистая копия репозитория с train (без `operator/`, `.env`, `data/public`, `data/private`). Агентный харнес (Hermes) и ключ модели добавляются по ответу руководителя (ADR 0007). Локальная проверка показала: из такого контейнера доступны журнал, train/public/private API; PostgreSQL и Trino недоступны (слушают только 127.0.0.1), Docker socket и каталоги оператора не видны.

Firewall на хосте: из `172.30.0.0/24` разрешить только порты 8091-8093 адреса 172.30.0.1, остальное входящее с этой сети запретить; исходящий интернет исследователя - по ответу руководителя о модели.

```bash
sudo iptables -I INPUT -s 172.30.0.0/24 -d 172.30.0.1 -p tcp -m multiport --dports 8091:8093 -j ACCEPT
sudo iptables -I INPUT 2 -s 172.30.0.0/24 -j DROP
```

## Эксплуатация

- Перед каждым стартом или возобновлением поиска: `arena.sh status` и `arena.sh readiness`.
- Логи: `journalctl -u sqltr-arena-<split>` (на хосте без systemd - `/srv/sqltr-arena/logs/<split>.log`).
- Рестарт оценщика не теряет private: принятый пакет и статус хранятся в `state-private` (проверено приёмкой, A16).
- Сбой движка во время private даёт `failed` без частичных оценок (A18). Разбор - по `record.json` и логам; новую серию назначает только руководитель (вопрос 5).
- Train-сервис считает примеры одного запроса последовательно (~1.9 с на пример с метрикой на PostgreSQL), параллелятся разные запросы до `EXECUTION_SLOTS`. Полный train из 500 примеров ≈ 16 минут; для быстрых итераций исследователь берёт фиксированную подвыборку (`--example-id`), полный прогон - для кандидатов в финалисты.
