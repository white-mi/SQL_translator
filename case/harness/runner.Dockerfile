# Runner image for SQLTR candidates (model-free harness, network=none).
# Build from the root of the pinned arena checkout, so the arena's own runner/main.py is used:
#   docker build -f /path/to/sqltr/case/harness/runner.Dockerfile -t sqltr-runner:1 .
FROM python:3.12-slim
RUN pip install --no-cache-dir sqlglot==30.27.0
WORKDIR /opt/runner
COPY runner/main.py /opt/runner/main.py
USER 10001:10001
ENTRYPOINT ["python", "-I", "/opt/runner/main.py"]
