# Стенд измерений

PostgreSQL 16.4 и Trino 483 на Iceberg в одном `docker compose`. На стенде считаются эталоны направления trino2gp, проходит self-check генератора и замеряется стоимость кандидата.

## Требования к VM
Ubuntu 24.04, 2 vCPU, 8 ГБ RAM, 50 ГБ SSD, swap 4 ГБ. На такой VM (Scaleway `BASIC3-X2C-8G`) 8.11 прошли self-check, baseline и приемка арены: пик памяти без контейнера исследователя - 3.4 ГБ, swap не использовался. Память исследователя пока не замерена. Прерываемую VM не используем: она останавливается в течение суток.

Trino ограничен 4 ГБ: куча 2.4 ГБ (`stand/trino/jvm.config`, образ по умолчанию отдает куче 80% лимита) и около 0.8 ГБ вне кучи. Лимит памяти нельзя снижать без проверки: ошибка нехватки памяти в запросе Trino дает score 0, а не сбой, поэтому после любого изменения повторяется self-check.

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
- Ошибка записи в хранилище: проверить, что сервис `warehouse-init` завершился успешно (`docker compose ps -a`), он отдает том пользователю Trino.
- Каталог не грузится из-за `local.location`: значение должно быть обычным путем `/data/warehouse`, без `local://`.
