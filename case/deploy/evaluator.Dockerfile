# Alternative layout (not used in the single-VM plan, see README.md): evaluator inside the arena container.
# Evaluator image with the metric's engine drivers. Build after the arena's own evaluator image:
#   docker build -f /path/to/sqltr/case/deploy/evaluator.Dockerfile -t sqltr-evaluator:1 .
FROM arena-evaluator:local
USER root
RUN /opt/arena/.venv/bin/python -m pip install --no-cache-dir "psycopg[binary]==3.3.6" "trino==0.340.0" "sqlglot==30.27.0" \
 || (uv pip install --python /opt/arena/.venv/bin/python "psycopg[binary]==3.3.6" "trino==0.340.0" "sqlglot==30.27.0")
USER 10001:10001
