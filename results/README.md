# Результаты

Все цифры ниже получены 8.11 на VM (2 vCPU, 8 ГБ RAM) исполнением на настоящих PostgreSQL 16.4 и Trino 483, если не сказано иное. Score - доля примеров, где все выходные таблицы совпали с эталоном на всех четырех состояниях данных.

Выращенного решения пока нет: кампания не начиналась, а public и private еще не строились с секретными сидами. Поэтому здесь baseline и проверки стенда. Проба подхода вне кампании описана в [docs/poc.md](../docs/poc.md).

## Главное

| Что | Результат | Файл |
|---|---|---|
| Baseline на train, всего | **0.064** (32 из 500) | [baseline/train/summary.json](baseline/train/summary.json) |
| Baseline на train, gp2trino | **0.012** (3 из 250) | там же |
| Baseline на train, trino2gp | **0.116** (29 из 250) | там же |
| Baseline на holdout, всего | **1 из 24** (gp2trino 1 из 12, trino2gp 0 из 12) | [baseline/holdout/summary.json](baseline/holdout/summary.json) |
| Self-check: парные рендеры дают равные результаты на двух движках | train 250 из 250, holdout 12 из 12 | [stand/self_check.json](stand/self_check.json), [stand/holdout_self_check.json](stand/holdout_self_check.json) |
| Эталоны train | 500 построено, 0 пропущено, 685 с | [stand/references_report.json](stand/references_report.json) |
| Приемка развертывания арены | 19 из 19 за 302 с | [acceptance/vm-2026-11-08.json](acceptance/vm-2026-11-08.json) |
| Кандидат с эталонными переводами через арену, 500 примеров | ровно **1.0** за 17 минут: цепочка верна от начала до конца | [stand/full-train-2026-11-08.json](stand/full-train-2026-11-08.json) |
| Baseline через арену, 500 примеров | 0.064 за 11 минут, то же число, что и без арены; повтор с кэшем метрики - 9 минут | там же |
| Генеральная репетиция с фальшивым провайдером модели | 22 из 22, агенту хватило ~200 МБ | [researcher/rehearsal-2026-11-08.json](researcher/rehearsal-2026-11-08.json) |

Почему baseline так низок: `sql_error` на 444 примерах из 468 провальных, гейт `MERGE` для Greenplum - на 21, несовпадение таблиц - на 3. Разбивка по шаблонам и классам расхождений - в [сводке](baseline/train/summary.json), разбор - в [docs/domain.md](../docs/domain.md).

## Что где лежит

| Каталог | Что внутри |
|---|---|
| `baseline/train/` | Baseline SQLGlot на train, оба направления: score, разбивка по шаблонам и классам расхождений, причины провалов, время |
| `baseline/holdout/` | То же на 12 парах, написанных вручную, вне генератора |
| `stand/` | Свидетельства стенда: self-check, эталоны, лог `bootstrap.sh`, замер стоимости кандидата |
| `acceptance/` | Приемка развертывания арены: на VM с Trino и локальный прогон 4.11 только на PostgreSQL |
| `researcher/` | Отчет генеральной репетиции (`case/researcher/rehearsal.sh`) |
| `archive/2026-11-04-local/` | Первые замеры без Trino, заменены замерами на стенде, см. ниже |

## Архив

В `archive/2026-11-04-local/` лежат замеры 4.11, сделанные локально на одном PostgreSQL. Они оставлены как история и больше не используются:

- `baseline-gp2trino-offline.json` - офлайн-оценка gp2trino, когда Trino еще не было: верхняя граница 0.024 по числу переводов без конструкций Greenplum. Настоящий замер дал 0.012.
- `baseline-trino2gp-summary.json` - trino2gp на PostgreSQL с эталонами из парного рендера: 0.116, настоящий замер дал то же число.
- `holdout-baseline-trino2gp-summary.json` - holdout, только trino2gp: 0 из 12, настоящий замер дал то же число.

Сырые результаты по примерам лежат в `raw/` и в git не попадают. Любая цифра из сводок воспроизводится командами из [README](../README.md) и [stand/README.md](../stand/README.md).
