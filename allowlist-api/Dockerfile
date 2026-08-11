# Multi-stage build. The builder stage does the installing; the runtime stage
# receives only the finished virtualenv, so pip's caches and build artifacts
# never reach the shipped image.
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt is copied on its own so this layer stays cached until the
# dependencies actually change — application edits do not trigger a reinstall.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
           /opt/venv/lib/python3.12/site-packages/setuptools* \
           /opt/venv/lib/python3.12/site-packages/pkg_resources \
           /opt/venv/bin/pip*
# pip is deliberately removed after the install. A package manager inside a
# running container is an attacker's install tool; nothing at runtime needs it.


FROM python:3.12-slim-bookworm AS runtime

# A fixed high UID with no home directory and no login shell. The number is
# pinned because Kubernetes will assert runAsUser: 10001 later, and that has to
# match a real account in the image.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

# The base image ships its own pip at /usr/local, separate from the venv copy
# removed in the builder stage. Both have to go, or `pip install` still works
# inside a running container. Nothing at runtime needs a package manager.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip* \
           /usr/local/lib/python3.12/site-packages/setuptools* \
           /usr/local/lib/python3.12/site-packages/pkg_resources \
           /usr/local/bin/pip*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --chown=10001:10001 app/ ./app/
COPY --chown=10001:10001 config/ ./config/

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
