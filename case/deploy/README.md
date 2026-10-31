# Развертывание кейса в арене (неделя 2)

1. Образ оценщика с драйверами движков: `case/deploy/evaluator.Dockerfile` поверх `arena-evaluator:local`.
2. Сеть: сервисы оценщиков арены подключаются к внешней сети `sqltr` стенда и видят движки как `postgres:5432` и `trino:8080` (значения по умолчанию в `case/metric/metric_config.json`).
3. Пароли движков передаются оценщику переменными `SQLTR_PG_ADMIN_PASSWORD` и `SQLTR_PG_RUNNER_PASSWORD`, в пакет метрики они не попадают.
4. Пакет метрики замораживается командой арены `python -m evaluator.prepare freeze --metric case/metric --output <campaign_dir>/metric`.
5. Train, public и private: `dataset.jsonl` соответствующих разделов; public и private лежат у оператора.
