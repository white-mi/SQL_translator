# Харнес решателя

Немодельный харнес `sqltr-program`: образ `sqltr-runner:1` = адаптер [main.py](main.py) + закреплённый SQLGlot 30.27.0. Протокол stdin/stdout тот же, что у комплектного `runner/main.py` арены; отличие одно - падение кандидата превращается в ответ-маркер, который метрика оценивает нулём ([ADR 0008](../../docs/adr/0008-crash-tolerant-harness.md)).

Кандидат - каталог с `solve.py` (и своими модулями). Запуск: `python -E -s /candidate/solve.py`, cwd `/tmp`, без сети, stdin `{"input", "assets_dir", "parameters"?}`, stdout `{"answer": "..."}`, не дольше 120 с на пример.

Сборка (из корня репозитория):

```bash
docker build -f case/harness/runner.Dockerfile -t sqltr-runner:1 .
```

Регистрация у оператора (`ARENA_HARNESSES`, замораживается до кампании); этот же образ - харнес по умолчанию (`ARENA_RUNNER_IMAGE`):

```json
{"sqltr-program": {"image": "sqltr-runner:1", "uses_model": false, "network": "none"}}
```

Модельный харнес (LLM-фоллбэк через gateway) добавляется отдельной записью после приёмки gateway - см. ADR 0007.
