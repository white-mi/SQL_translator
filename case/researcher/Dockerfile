# Researcher image: Hermes plus the arena CLI, without any arena or Hermes code in this repository.
# The build context is the pinned arena checkout; the second context brings in our launcher and settings.
#
#   docker build -f case/researcher/Dockerfile --build-context sqltr=case/researcher -t sqltr-researcher:1 /opt/arena
ARG PYTHON_IMAGE=python:3.14-slim
FROM ${PYTHON_IMAGE}
ARG HERMES_REVISION=86fa9e6ca1de7ee71132d2be0103f3e37d1fa872
ARG UV_VERSION=0.8.13
RUN apt-get update && apt-get install -y --no-install-recommends git curl ripgrep ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==${UV_VERSION}

# Hermes at a fixed commit, the one the arena's own campaigns ran on
RUN mkdir /opt/hermes && git -C /opt/hermes init -q \
    && git -C /opt/hermes remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git -C /opt/hermes fetch -q --depth 1 origin ${HERMES_REVISION} \
    && git -C /opt/hermes checkout -q --detach FETCH_HEAD \
    && test "$(git -C /opt/hermes rev-parse HEAD)" = "${HERMES_REVISION}"
WORKDIR /opt/hermes
# the Anthropic SDK must be in the image: Hermes would fetch it at run time, and the researcher has no internet
RUN uv sync --frozen --no-dev --extra anthropic

# The arena CLI's packages go into the system Python, not a venv: the agent's terminal is a login shell,
# which resets PATH, so `python` there is always this one. Hermes keeps its own venv.
WORKDIR /opt/arena
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/arena-requirements.txt \
    && uv pip install --system --no-deps -r /tmp/arena-requirements.txt && rm /tmp/arena-requirements.txt
COPY evaluator ./evaluator
COPY integrations ./integrations
COPY common ./common
COPY prompts ./prompts
COPY cases/template ./cases/template
COPY README.md AGENTS.md ./

COPY --from=sqltr run.py hermes-config.yaml /opt/researcher/

ENV PYTHONPATH="/opt/arena"
ENV PYTHONDONTWRITEBYTECODE="1"
ENV HOME="/tmp/researcher-home"
ENV HERMES_HOME="/tmp/hermes-home"
ENV TERMINAL_TIMEOUT="7200"
ENV TERMINAL_MAX_FOREGROUND_TIMEOUT="7200"
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/researcher/run.py"]
