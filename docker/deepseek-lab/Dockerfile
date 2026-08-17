FROM memoryos-deepseek-lab@sha256:1647a9c02c4c74487695c89f5b5db16ab3ef26c396fe72229f1142cf101b6c46

# RC5 heals each profile's flat module fallback but its source-mode launcher
# omits the Loader base URL that makes those packages importable. Apply the
# narrow upstream-equivalent fix to both the checked-in source and built CLI.
COPY docker/deepseek-lab/harness-profile-loader-base.patch /tmp/harness-profile-loader-base.patch
RUN cd /opt/deepseek-harness \
    && patch --batch --forward -p1 < /tmp/harness-profile-loader-base.patch \
    && grep -Fq "pathToFileURL(join(composed.profile.dir, \"entry.mjs\")).href" \
       apps/cli/lib/profile-boot-DG5t9aNs.js \
    && rm -f /tmp/harness-profile-loader-base.patch

# Keep plugin packaging identical in both A/B arms instead of letting Corepack
# resolve whichever pnpm release is current when the experiment is executed.
ENV COREPACK_HOME=/opt/corepack \
    COREPACK_DEFAULT_TO_LATEST=0 \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack prepare pnpm@11.7.0 --activate \
    && test "$(pnpm --version)" = "11.7.0"

# Freeze the scientific-Python stack and the old pytest checkout's dependencies
# in a task-only venv. Deliberately do not install pytest itself: the checkout is
# the target under repair, so an installed newer copy would leak future behavior.
RUN python -m venv /opt/memoryos/task-venv \
    && /opt/memoryos/task-venv/bin/python -m pip install \
       --no-cache-dir --only-binary=:all: \
       attrs==22.2.0 \
       iniconfig==1.1.1 \
       packaging==24.2 \
       pluggy==1.0.0 \
       py==1.11.0 \
       Pygments==2.13.0 \
       setuptools==75.8.0 \
       setuptools-scm==8.1.0 \
       numpy==1.26.4 \
       pandas==2.2.3 \
       matplotlib==3.8.4 \
       scipy==1.12.0 \
    && /opt/memoryos/task-venv/bin/python -m pip check \
    && /opt/memoryos/task-venv/bin/python -c \
       'import importlib.util; assert importlib.util.find_spec("pytest") is None; assert importlib.util.find_spec("_pytest") is None'

# The command resolves pytest from the current checkout through the shell's
# src-layout PYTHONPATH instead of creating an editable install or using a wheel.
COPY docker/deepseek-lab/pytest /opt/memoryos/task-venv/bin/pytest
RUN chmod 0755 /opt/memoryos/task-venv/bin/pytest

# The pinned Harness source carries the audited Landlock launcher source but
# deliberately excludes release binaries. Build it inside the Linux image so
# workspace-write remains fail-closed and usable under Docker Desktop's WSL2 VM.
RUN mkdir -p /opt/deepseek-harness/native/landlock-run/packages/linux-x64/bin \
    && gcc -std=c11 -Os -Wall -Wextra -Werror -static -s \
       -o /opt/deepseek-harness/native/landlock-run/packages/linux-x64/bin/landlock-run \
       /opt/deepseek-harness/native/landlock-run/packages/entry/src/main.c \
    && /opt/deepseek-harness/native/landlock-run/packages/linux-x64/bin/landlock-run --probe

# Every model-issued bash command receives a second, narrower sandbox. It can
# read the Linux runtime, read/write only its dedicated condition mount and /tmp, and
# cannot create or use sockets. The parent Node process remains online for the
# DeepSeek API while curl/pip/git/docker inside the tool are fail-closed.
COPY docker/deepseek-lab/seccomp-network-off.c /tmp/seccomp-network-off.c
COPY docker/deepseek-lab/bash /opt/memoryos/bin/bash
COPY docker/deepseek-lab/dsh /opt/memoryos/bin/dsh
COPY docker/deepseek-lab/task-shell /opt/memoryos/libexec/task-shell
RUN mkdir -p /opt/memoryos/libexec \
    && gcc -std=c11 -Os -Wall -Wextra -Werror -static -s \
       -o /opt/memoryos/libexec/network-off /tmp/seccomp-network-off.c \
    && chmod 0755 /opt/memoryos/bin/bash /opt/memoryos/bin/dsh \
       /opt/memoryos/libexec/network-off \
       /opt/memoryos/libexec/task-shell \
    && /bin/bash -n /opt/memoryos/bin/bash /opt/memoryos/libexec/task-shell \
    && ! /opt/memoryos/libexec/network-off \
       python3 -c 'import socket; socket.socket()' \
    && rm -f /tmp/seccomp-network-off.c
