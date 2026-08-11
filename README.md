# CICD-Pipeline-Lab

A DevSecOps lab built around a real security application. The application is the
payload; the pipeline around it is the point.

`phishing-detector` is a FastAPI service that scores a URL for phishing using a
tokenized Naive Bayes classifier trained on ~507k labeled URLs. The trained model
is an immutable artifact baked into the image. The decision threshold is mutable
policy read from disk at request time, so a Kubernetes ConfigMap can retune
detection sensitivity without redeploying the model.

That split — immutable artifact, mutable policy — is what the Kubernetes,
Terraform, and CI/CD phases are built to exercise.

## The API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/healthz` | Liveness — the process is up |
| GET | `/readyz` | Readiness — model loaded and threshold valid; 503 if not |
| GET | `/info` | Model path, classifier, vocabulary size, current threshold |
| GET | `/predict?url=<url>` | Score one URL; 400 on empty or oversized input |

Liveness and readiness deliberately disagree. `/healthz` stays 200 whenever the
process is serving, even with a broken model, so a bad artifact cannot trigger a
cluster-wide restart loop. `/readyz` returns 503 when the model or threshold will
not load, which pulls the pod out of the Service's endpoints without killing it —
a controlled degradation instead of an outage, and a pod you can still debug.

## Model performance

Held-out test set of 101,439 URLs, threshold 0.5:

| Metric | Value |
| ------ | ----- |
| Majority-class baseline accuracy | 0.7746 |
| Accuracy | 0.9648 |
| F1 | 0.9183 |
| ROC AUC | 0.9898 |

Always guessing "legitimate" gets 77.5% accuracy on this class balance, so the
model beats the do-nothing baseline by 19 points. Details and the leakage
corrections made to the original notebook are in
[phishing-detector/README.md](phishing-detector/README.md).

## Roadmap

- [x] **Phase 1 — Application.** FastAPI service over a trained classifier, 13 tests.
- [x] **Phase 2 — Container.** Multi-stage Dockerfile, non-root UID 10001, package
      manager stripped, pinned deps. Verified against a live container under a
      `--read-only` root filesystem.
- [ ] **Phase 3 — Kubernetes (local).** kind cluster, ConfigMap supplying the
      threshold, Deployment with probes and a hardened securityContext, Service,
      NetworkPolicy.
- [ ] **Phase 4 — Terraform.** Split `cluster-local/` from `app/` so the cluster
      layer can be swapped without touching the app layer. Convert `k8s/` into a
      Helm chart.
- [ ] **Phase 5 — CI/CD.** GitHub Actions and GitLab CI, same stages, side by side.
- [ ] **Phase 6 — Supply chain.** Trivy gate, SBOM, Checkov, Cosign — including
      signing the model artifact. *(optional)*
- [ ] **Phase 7 — Podman/Buildah** on Rocky Linux. *(optional)*
- [ ] **Phase 8 — One deliberate cloud run**, then destroy. *(optional)*
- [ ] **Phase 9 — Kyverno + Falco** in-cluster. *(optional)*

## Running it locally

```bash
cd phishing-detector && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
cd phishing-detector && .venv/bin/python -m pytest -q
```

## Running the container

```bash
cd phishing-detector && docker build -t phishing-detector:0.1.0 .
```

```bash
docker run --rm --read-only -p 8000:8000 phishing-detector:0.1.0
```

Score a URL:

```bash
curl -s --get --data-urlencode "url=paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr" http://127.0.0.1:8000/predict
```

Confirm it is not running as root — this should print `uid=10001(app)`:

```bash
docker run --rm phishing-detector:0.1.0 id
```

Confirm the threshold really is read from disk rather than baked in. Mounting a
different config changes the verdict with no rebuild, which is exactly what the
Phase 3 ConfigMap will do — at 0.30, `google.com` (probability 0.3861) flips from
legitimate to phishing while the probability itself never moves:

```bash
printf 'threshold: 0.30\n' > /tmp/aggressive.yaml && docker run --rm --read-only -v /tmp/aggressive.yaml:/app/config/detector.yaml:ro -p 8000:8000 phishing-detector:0.1.0
```

## Repository layout

```
CICD-Pipeline-Lab/
├── CLAUDE.md                  # project brief — read this first
├── README.md
├── .gitignore                 # blocks tfstate/tfvars; keeps .terraform.lock.hcl
└── phishing-detector/
    ├── app/main.py            # the service
    ├── config/detector.yaml   # threshold — ConfigMap mounts over this
    ├── model/                 # trained artifact (1.8MB) + metrics.json
    ├── training/train.py      # offline training; not in the image
    ├── tests/
    ├── Dockerfile             # multi-stage, non-root, no package manager
    ├── requirements.txt       # runtime, pinned
    ├── requirements-dev.txt   # test/lint tooling
    └── requirements-train.txt # adds pandas; training only
```

The training dataset (~30MB, Kaggle-sourced) is deliberately not committed. The
shipped artifact is 1.8MB and lives in `phishing-detector/model/`.
