# CICD-Pipeline-Lab

A DevSecOps lab built around a deliberately trivial application. The app is the
payload; the pipeline around it is the point.

`allowlist-api` is a small FastAPI service that checks whether an IP address
falls inside an approved CIDR block. Approved blocks are read from
`config/allowlist.yaml` at request time rather than baked into the code, so a
Kubernetes ConfigMap can supply them and the allowlist can change without an
image rebuild.

## The API

| Method | Path             | Purpose                                  |
| ------ | ---------------- | ---------------------------------------- |
| GET    | `/healthz`       | Liveness — the process is up             |
| GET    | `/readyz`        | Readiness — config loads; 503 if not     |
| GET    | `/blocks`        | List the currently loaded allowlist      |
| GET    | `/check?ip=<addr>` | Check one IP; 400 on malformed input   |

CIDR matching uses Python's stdlib `ipaddress`. FastAPI is only the web layer.

The `/readyz` 503-on-missing-config behavior is intentional: it gives the
Kubernetes readiness probe something real to detect.

## Roadmap

- [x] **Phase 1 — Application.** FastAPI service, config-driven allowlist, 8 tests.
- [x] **Phase 2 — Container.** Multi-stage Dockerfile, non-root UID 10001, pinned
      deps, package manager stripped from the runtime image. Verified against a
      live container, including a `--read-only` root filesystem.
- [ ] **Phase 3 — Kubernetes (local).** kind cluster, ConfigMap, Deployment with
      probes and a hardened securityContext, Service, NetworkPolicy.
- [ ] **Phase 4 — Terraform.** Split `cluster-local/` from `app/` so the cluster
      layer can be swapped without touching the app layer. Convert `k8s/` into a
      Helm chart.
- [ ] **Phase 5 — CI/CD.** GitHub Actions and GitLab CI, same stages, side by side.
- [ ] **Phase 6 — Supply chain.** Trivy gate, SBOM, Checkov, Cosign. *(optional)*
- [ ] **Phase 7 — Podman/Buildah** on Rocky Linux. *(optional)*
- [ ] **Phase 8 — One deliberate cloud run**, then destroy. *(optional)*
- [ ] **Phase 9 — Kyverno + Falco** in-cluster. *(optional)*

## Running it locally

```bash
cd allowlist-api
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

To serve it:

```bash
cd allowlist-api && .venv/bin/uvicorn app.main:app --reload --port 8000
```

## Installing Docker

Docker Desktop's Homebrew cask needs `sudo` to create `/usr/local/bin`, so it has
to be run from an interactive terminal where it can prompt for a password:

```bash
brew install --cask docker-desktop
```

Then launch Docker Desktop once from Applications — the first launch installs a
privileged helper and will ask for the password again. After that, `docker
version` should report both a Client and a Server.

## Running the container

```bash
cd allowlist-api && docker build -t allowlist-api:0.1.0 .
```

```bash
docker run --rm -p 8000:8000 allowlist-api:0.1.0
```

Confirm it is not running as root — this should print `uid=10001(app)`:

```bash
docker run --rm allowlist-api:0.1.0 id
```

Confirm the config really is read from disk rather than baked into the image.
Mounting a different file over `/app/config/allowlist.yaml` changes what the API
serves with no rebuild, which is exactly what the Phase 3 ConfigMap will do:

```bash
docker run --rm --read-only -v "$PWD/config/allowlist.yaml:/app/config/allowlist.yaml:ro" -p 8000:8000 allowlist-api:0.1.0
```

## Repository layout

```
CICD-Pipeline-Lab/
├── CLAUDE.md              # project brief — read this first
├── README.md
├── .gitignore             # blocks tfstate/tfvars; keeps .terraform.lock.hcl
└── allowlist-api/
    ├── app/main.py        # the service
    ├── config/            # allowlist.yaml — ConfigMap mounts over this later
    ├── tests/
    ├── Dockerfile         # multi-stage, non-root
    ├── pyproject.toml     # ruff + pytest config
    ├── requirements.txt      # runtime, pinned
    └── requirements-dev.txt  # test/lint tooling, not shipped in the image
```
