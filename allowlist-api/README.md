# allowlist-api

A FastAPI service that answers one question: is this IP address inside an
approved CIDR block?

## Why it is built this way

The allowlist is loaded from `config/allowlist.yaml` **on every request**, not
cached at import time. That is the single design decision the rest of the lab
depends on — it means a Kubernetes ConfigMap can mount over that path and change
the approved blocks without rebuilding or re-pushing the image.

Liveness and readiness are deliberately different:

- `/healthz` returns 200 as long as the process is serving, even when the config
  is missing. If liveness depended on config, a bad ConfigMap would make
  Kubernetes restart-loop pods that are running perfectly well.
- `/readyz` returns 503 when the config cannot be loaded. That pulls the pod out
  of the Service's endpoints without killing it, which is the correct response to
  "running but cannot do useful work."

## Configuration

`ALLOWLIST_CONFIG` overrides the config path. Unset, it resolves relative to the
package, which is `/app/config/allowlist.yaml` inside the container.

```yaml
blocks:
  - cidr: 10.20.0.0/16
    label: scada-field-devices
    owner: ot-engineering
```

`label` and `owner` are free-form and optional; `cidr` is required and validated
at load time. A malformed CIDR fails the whole config, which surfaces as a 503 on
`/readyz` rather than as a silently half-loaded allowlist.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check .
```

## Container notes

The image is multi-stage: the builder holds pip and the virtualenv build, the
runtime stage receives only the finished `/opt/venv`, so pip never ships. It runs
as UID 10001 — a fixed number, because the Phase 3 Deployment asserts
`runAsUser: 10001` and that has to match a real account in the image.

`PYTHONDONTWRITEBYTECODE=1` matters more than it looks: Phase 3 sets
`readOnlyRootFilesystem: true`, and an interpreter trying to write `.pyc` files
into a read-only filesystem is a real source of confusing startup failures.
