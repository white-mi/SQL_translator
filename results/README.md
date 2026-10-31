# Результаты

| Файл | Что внутри |
|---|---|
| `baseline/train-trino2gp/summary.json` | Baseline SQLGlot на train, направление trino2gp: score, разбивка по шаблонам и классам расхождений, причины провалов, время |
| `baseline/train-gp2trino-offline.json` | Офлайн-оценка gp2trino без Trino: сколько переводов содержат конструкции Greenplum |
| `stand/` | Свидетельства стенда с VM: self-check, эталоны, baseline по обоим направлениям (после запуска bootstrap.sh) |

Сырые результаты по примерам лежат в `raw/` и в git не попадают; любая цифра из сводок воспроизводится командой из README.
