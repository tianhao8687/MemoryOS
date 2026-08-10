FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/memoryos

COPY pyproject.toml README.md ./

RUN ["python", "-c", "import subprocess, sys, tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); requirements = [*data['build-system']['requires'], *data['project']['dependencies']]; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *requirements])"]

RUN groupadd --gid 65532 benchmark \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin benchmark

COPY memoryos ./memoryos

RUN python -m pip install --no-cache-dir --no-build-isolation --no-deps .

USER 65532:65532
ENTRYPOINT []
