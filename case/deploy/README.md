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
sudo bash case/deploy/arena.sh image      # sqltr-runner:1 (харнес ADR 0008); записать digest в preparation.md
sudo bash case/deploy/arena.sh accept     # синтетическая приёмка: 19 проверок, ~5 минут, отдельный каталог
sudo bash case/deploy/arena.sh freeze     # заморозка метрики, sha256 lock-файла -> preparation.md
sudo bash case/deploy/arena.sh data       # public/private из сидов оператора, эталоны на Trino
sudo bash case/deploy/arena.sh journal    # журнал кампании с MAX_SOLUTIONS
sudo bash case/deploy/arena.sh start      # три оценщика (systemd transient units)
sudo bash case/deploy/arena.sh readiness  # должен вернуть mode=campaign, gate_enabled=true, journal_ready=true
sudo bash case/deploy/arena.sh workspace        # workspace исследователя по разрешенному списку (ADR 0009)
sudo bash case/deploy/arena.sh researcher-image # образ sqltr-researcher:1 (ADR 0007)
sudo bash case/deploy/arena.sh gateway          # шлюз модели; нужен файл с ключом, см. case/researcher/README.md
sudo bash case/deploy/arena.sh researcher --prompt-file <файл в /workspace>   # запуск исследователя
```

Перед настоящей кампанией все эти шаги можно пройти в отдельном каталоге с фальшивым провайдером: `case/researcher/rehearsal.sh`.

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
| `/srv/sqltr-arena/secrets/{train,public,private,model}-token` | arena, 0600 | команда `researcher` монтирует копии исследователю, read-only |
| `/srv/sqltr-arena/secrets/model-api-key` | arena, 0600 | только шлюз модели |
| `/srv/sqltr-arena/model-usage.jsonl` | arena | журнал вызовов модели: токены и стоимость; сумма по нему - потраченное |
| `/srv/sqltr-arena/env/*.env` | root, 0600 | systemd; содержит пароли движков |

## Контейнер исследователя

Запускает команда `arena.sh researcher`: контейнер `sqltr-researcher:1` в сети `arena-research`, без привилегий, с корнем только для чтения, лимитом 3 ГБ памяти и монтированием workspace, журнала и токенов. Образ и настройки описаны в [case/researcher/README.md](../researcher/README.md).

`/srv/sqltr-workspace` - чистая копия по разрешенному списку (ADR 0009): без генератора, эталонов и holdout. Репетиция 8.11 ([отчет](../../results/researcher/rehearsal-2026-11-08.json)) проверила изнутри контейнера: доступны журнал, API оценщиков и шлюз модели; PostgreSQL, Trino, SSH и интернет недоступны; ключа провайдера, Docker socket и каталогов оператора не видно.

Брандмауэр применяет команда `arena.sh firewall`, а `researcher` вызывает ее при каждом запуске, потому что правила не переживают перезагрузку: из сети исследователя открыты только порты оценщиков и шлюза на `172.30.0.1`, пересылка наружу запрещена. На интерфейс `lo` правила не действуют: собственные обращения хоста к этому адресу (`status`, `readiness`) идут с адресом из той же сети.

## Эксплуатация

- Перед каждым стартом или возобновлением поиска: `arena.sh status` и `arena.sh readiness`.
- Логи: `journalctl -u sqltr-arena-<split>` (на хосте без systemd - `/srv/sqltr-arena/logs/<split>.log`).
- Рестарт оценщика не теряет private: принятый пакет и статус хранятся в `state-private` (проверено приёмкой, A16).
- Сбой движка во время private даёт `failed` без частичных оценок (A18). Разбор - по `record.json` и логам; новую серию назначает только руководитель (вопрос 5).
- Train-сервис считает примеры одного запроса последовательно, параллелятся разные запросы до `EXECUTION_SLOTS`. Замер на VM 8.11 (2 vCPU): решатель в контейнере ~0.6 с на пример, метрика на Trino ~2.5 с. Полный train из 500 примеров - 17 минут, если все переводы верные (без кэша метрики), 11 минут у baseline, 9 минут при тех же переводах с кэшем ([отчет](../../results/stand/full-train-2026-11-08.json)). Для быстрых итераций исследователь берёт фиксированную подвыборку (`--example-id`), основную кривую дает полный прогон каждого кандидата (`campaign.md`).
