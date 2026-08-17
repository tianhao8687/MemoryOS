# Local base tag resolves to
# sha256:32359ca8e79047f42582daf6153af39a4a7c2e60e0a88fa6529d5a0bb6beff2d.
# The frozen runtime records the derived image by its own immutable digest.
FROM memoryos-deepseek-lab:hardened-ab-r8

# pytest 5.2.x is loaded from the task checkout, not installed. Freeze only
# the historical runtime dependencies that checkout declares and that the
# general DeepSeek lab image does not already provide. Model tools stay
# networkless at execution time.
RUN /opt/memoryos/task-venv/bin/python -m pip install \
      --no-cache-dir --only-binary=:all: \
      atomicwrites==1.4.0 \
      more-itertools==8.14.0 \
      pluggy==0.13.1 \
      wcwidth==0.2.13 \
    && /opt/memoryos/task-venv/bin/python -m pip check \
    && /opt/memoryos/task-venv/bin/python -c \
       'import atomicwrites, more_itertools, pluggy, wcwidth; assert pluggy.__version__ == "0.13.1"'

COPY docker/deepseek-pytest6197-calibration/task-shell /opt/memoryos/libexec/task-shell
COPY docker/deepseek-pytest6197-calibration/sitecustomize.py /opt/memoryos/task-venv/lib/python3.12/site-packages/sitecustomize.py
RUN chmod 0755 /opt/memoryos/libexec/task-shell \
    && /bin/bash -n /opt/memoryos/libexec/task-shell
