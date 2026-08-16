FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ARG TRANSFORMERS_VERSION=4.51.3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRANSFORMERS_VERSION=${TRANSFORMERS_VERSION}

WORKDIR /opt/memoryos

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./

RUN ["python", "-c", "import os, subprocess, sys, tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); requirements = [*data['build-system']['requires'], *data['project']['dependencies'], 'transformers==' + os.environ['TRANSFORMERS_VERSION']]; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *requirements])"]

RUN groupadd --gid 65532 benchmark \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin benchmark

COPY memoryos ./memoryos

RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps . \
    && python -c "from memoryos.evaluation.openai_compatible_coding_agent import OpenAICompatibleCodingAgent"

USER 65532:65532
ENTRYPOINT []
