# Multi-stage build. The builder stage does the installing; the runtime stage
# receives only the finished virtualenv, so pip's caches and build artifacts
# never reach the shipped image. That matters more here than it did for a
# three-package app — scikit-learn, scipy and numpy pull in a lot.
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
           /opt/venv/lib/python3.12/site-packages/setuptools* \
           /opt/venv/lib/python3.12/site-packages/pkg_resources \
           /opt/venv/bin/pip*


FROM python:3.12-slim-bookworm AS runtime

# Fixed numeric UID with no home directory and no login shell. The number is
# pinned because Kubernetes runAsNonRoot cannot resolve a username against
# /etc/passwd before starting the container — give it a name instead of a number
# and the pod fails with "image has non-numeric user".
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

# The base image ships its own pip at /usr/local, separate from the venv copy
# removed in the builder stage. Both have to go, or `pip install` still works
# inside a running container.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/pkg_resources \
           /usr/local/bin/pip*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# scikit-learn and numpy size their thread pools from the host CPU count, which
# ignores the container's CPU limit. Left alone they oversubscribe and then get
# throttled by the cgroup. One thread is right for per-request scoring, and it
# makes latency predictable once Phase 3 sets a CPU limit.
ENV OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

COPY --chown=10001:10001 app/ ./app/
COPY --chown=10001:10001 config/ ./config/
# The model artifact ships in the image. It is immutable and versioned with the
# image; only the threshold in config/ is meant to change at runtime.
COPY --chown=10001:10001 model/ ./model/

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
