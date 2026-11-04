# Runner image for SQLTR candidates: model-free, network=none, crash-tolerant adapter (ADR 0008).
# Build from the root of the sqltr repository:
#   docker build -f case/harness/runner.Dockerfile -t sqltr-runner:1 .
# Pin the base image by digest on the VM before the campaign (docker images --digests).
FROM python:3.12-slim
RUN pip install --no-cache-dir sqlglot==30.27.0
WORKDIR /opt/runner
COPY case/harness/main.py /opt/runner/main.py
USER 10001:10001
ENTRYPOINT ["python", "-I", "/opt/runner/main.py"]
