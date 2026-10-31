# Стенд измерений

PostgreSQL 16.4 и Trino 483 на Iceberg в одном `docker compose`. На стенде считаются эталоны направления trino2gp, проходит self-check генератора и замеряется стоимость кандидата.

## Требования к VM
Ubuntu 24.04, 8 vCPU, 32 ГБ RAM, 100 ГБ SSD (минимум 4 vCPU и 16 ГБ). Прерываемую VM не используем: она останавливается в течение суток.

## Шаги
1. `bash stand/vm-setup.sh` - Docker Engine, compose, uv. Затем перелогиниться.
2. `cp stand/.env.example stand/.env` и задать пароли.
3. `bash stand/bootstrap.sh` - поднимает движки, создает роль раннера, грузит 4 состояния данных в оба движка, строит train с эталонами на Trino, запускает self-check, тесты метрики и baseline по обоим направлениям.
4. Закоммитить `results/stand/` и `results/baseline/train/summary.json`.

Повторить один шаг: `STEP=baseline bash stand/bootstrap.sh` (шаги: engines, load, refs, selfcheck, baseline).

## Что проверить в результатах
- `results/stand/self_check.json`: `equal` должно быть равно `checked`; расхождения - повод поправить пару рендеров в `sqltr/gen/render.py`.
- `results/stand/baseline_summary.json`: score по направлениям и `metric_seconds_mean` - время метрики на пример; из него считается стоимость кандидата и лимит решений.

## Если Trino не стартует
- Ошибка JDBC-каталога (нет драйвера или таблиц каталога): проверить, что `stand/init/01-iceberg-catalog.sql` выполнился (`docker compose logs postgres`). Init-скрипты выполняются только на пустом volume: `docker compose down -v` и снова `up`.
- Нет поддержки локального хранилища: заменить его на MinIO (свойства `fs.native-s3.enabled`, `s3.*`) - вариант описан в ADR 0004.
