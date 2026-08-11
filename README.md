# CICD-Pipeline-Lab

**A DevSecOps pipeline built around a real security application — containerized,
hardened, and deployed through Kubernetes, Terraform, and CI/CD.**

The application is a phishing URL classifier. It is a genuine security tool, but
it is also deliberately the *smallest* part of this repository. The pipeline
around it is the artifact: how the image is built and hardened, how configuration
is separated from code, how the workload is deployed and constrained, and how all
of it is automated and verified.

> **Status:** Phases 1–2 complete and verified. Phase 3 (Kubernetes) in progress.
> Every claim below was checked by running it — output is reproduced verbatim.

---

## What it does

`phishing-detector` scores a URL for phishing using a tokenized Naive Bayes
classifier trained on ~507,000 labeled URLs.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/healthz` | Liveness — the process is up |
| GET | `/readyz` | Readiness — model loaded and threshold valid; 503 if not |
| GET | `/info` | Model path, classifier, vocabulary size, current threshold |
| GET | `/predict?url=<url>` | Score one URL; 400 on empty or oversized input |

```console
$ curl -s --get --data-urlencode "url=www.wikipedia.org/wiki/Cat" localhost:8000/predict
{"url":"www.wikipedia.org/wiki/Cat","phishing":false,"probability":0.0004,"threshold":0.5}

$ curl -s --get --data-urlencode "url=github.com/torvalds/linux" localhost:8000/predict
{"url":"github.com/torvalds/linux","phishing":false,"probability":0.0187,"threshold":0.5}

$ curl -s --get --data-urlencode "url=paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr?cmd=_login" localhost:8000/predict
{"url":"paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr?cmd=_login","phishing":true,"probability":1.0,"threshold":0.5}
```

---

## Quick start

```bash
git clone https://github.com/jameslrivera/CICD-Pipeline-Lab.git && cd CICD-Pipeline-Lab/phishing-detector
```

```bash
docker build -t phishing-detector:0.1.0 . && docker run --rm --read-only -p 8000:8000 phishing-detector:0.1.0
```

Run the tests:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```

---

## The design decision the whole lab is built on

The service holds two pieces of state and loads them in deliberately different
ways.

The **model** is an immutable artifact baked into the image and deserialized once
at startup. Changing it is a real release — new image, new scan, new signature.

The **threshold** is mutable policy read from disk on *every request*. It decides
where the cut between "phishing" and "legitimate" falls, so a Kubernetes
ConfigMap can retune detection sensitivity without redeploying the model.

This is the operational reality of running a detector: an analyst drowning in
false positives needs to move the threshold, not ship a new model.

Proven by running the same image twice, once with a different threshold mounted
over its config:

```bash
printf 'threshold: 0.30\n' > /tmp/aggressive.yaml && docker run -d --name pd-aggressive --read-only -v /tmp/aggressive.yaml:/app/config/detector.yaml:ro -p 8002:8000 phishing-detector:0.1.0
```

The probability never moves; only the policy applied to it does:

```console
$ curl -s --get --data-urlencode "url=google.com" localhost:8000/predict   # baked-in threshold 0.5
{"url":"google.com","phishing":false,"probability":0.3861,"threshold":0.5}

$ curl -s --get --data-urlencode "url=google.com" localhost:8002/predict   # mounted threshold 0.3
{"url":"google.com","phishing":true,"probability":0.3861,"threshold":0.3}
```

That second result is a **false positive**, and it is in this README on purpose.
Tuning a detector more aggressively catches more phishing and burns more analyst
time on legitimate traffic. Showing the cost is more honest than showing a clean
demo.

---

## Container hardening

Every item below is verified, not asserted.

| Control | Why | Verification |
| ------- | --- | ------------ |
| Non-root, fixed numeric UID 10001 | Root in a container is root on the host kernel if anything escapes the namespace | `docker run --rm phishing-detector:0.1.0 id` |
| Package manager removed | A working `pip` next to an attacker who already has a foothold is an install tool | `pip --version` → `not found` |
| Multi-stage build | Build caches and tooling never reach the shipped image | runtime stage copies only `/opt/venv` |
| Read-only root filesystem tolerated | Phase 3 sets `readOnlyRootFilesystem: true` | container runs under `docker run --read-only` |
| Exact version pins | A pipeline that resolves different versions on different days is not reproducible | `requirements.txt` |
| Thread pools pinned to 1 | scikit-learn sizes pools from the *host* CPU count and oversubscribes against a cgroup limit | `OMP_NUM_THREADS=1` |

```console
$ docker run --rm phishing-detector:0.1.0 id
uid=10001(app) gid=10001(app) groups=10001(app)

$ docker run --rm phishing-detector:0.1.0 sh -c 'pip --version'
sh: 1: pip: not found

$ docker images phishing-detector:0.1.0 --format '{{.Repository}}:{{.Tag}}   {{.Size}}'
phishing-detector:0.1.0   594MB
```

The UID is **numeric on purpose**. Kubernetes `runAsNonRoot` has to verify the
user is not root before starting the container, and it cannot resolve a username
against `/etc/passwd` without running the image first. A named user fails with
`container has runAsNonRoot and image has non-numeric user`.

`pip` is removed from **two** locations. Deleting it from the virtualenv is not
enough — `python:3.12-slim` ships a second copy at `/usr/local`, and leaving that
one behind means `pip install` still works inside a running container.

---

## Liveness and readiness deliberately disagree

`/healthz` returns 200 whenever the process is serving, **even with a broken
model**. `/readyz` returns 503 when the model or threshold will not load.

The distinction is what Kubernetes does with each answer. A liveness failure
makes the kubelet kill and restart the container. A readiness failure removes the
pod from the Service's endpoints but leaves it running.

So if liveness checked the model, one bad artifact would restart-loop every pod
in the cluster — turning a config problem into an outage, and destroying the
evidence, because containers would die before anyone could exec in. Splitting
them means a bad artifact produces a controlled degradation you can still debug.

**Liveness answers "should I be killed?" Readiness answers "should I get
traffic?"** Anything an operator could fix without a restart belongs in readiness.

---

## Model

`TfidfVectorizer` splits each URL on runs of alphanumerics —
`paypal.co.uk/cgi-bin/webscr` becomes `[paypal, co, uk, cgi, bin, webscr]` —
feeding `MultinomialNB`. The signal lives in those substrings: brand names
appearing in paths where they do not belong, unusual TLDs, long hex blobs.

Held-out test set of 101,439 URLs, threshold 0.5:

| Metric | Value |
| ------ | ----- |
| **Majority-class baseline accuracy** | **0.7746** |
| Accuracy | 0.9648 |
| Precision | 0.9617 |
| Recall | 0.8786 |
| F1 | 0.9183 |
| ROC AUC | 0.9898 |

Confusion matrix: TN 77,780 · FP 799 · FN 2,776 · TP 20,084

The baseline row is listed first because it is the one that makes the others
mean something. Always guessing "legitimate" scores 77.5% on this class balance,
so the model beats doing nothing by 19 points — not by 96.

### Leakage found and corrected

An earlier version of this model used eight hand-crafted features and scored F1
0.342, barely above the do-nothing baseline. Three separate problems were
inflating its numbers:

**Preprocessing was fitted before the split.** `StandardScaler` was fitted on the
full dataset, leaking test-set statistics into training. The vectorizer now lives
inside a `Pipeline`, which by construction only fits on the training split.

**Duplicate URLs were never removed.** The raw dataset is 7.7% duplicates —
42,151 of 549,346. Without deduplication the same URL lands in both splits and
the model is scored on rows it memorized.

**Two features could not be computed at inference time.** `feat1` was a group
mean of URL length by domain across the entire dataset, and `feat2` derived from
it. Serving a single URL, there is no group to average over. That is
training/serving skew — the features were leaky *and* unservable. Tokenization
replaced them.

The split is stratified and seeded, so retraining reproduces the artifact exactly.

Full detail on the service, the model, and the container is in
[phishing-detector/README.md](phishing-detector/README.md).

---

## Roadmap

- [x] **Phase 1 — Application.** FastAPI service over a trained classifier, 13 tests.
- [x] **Phase 2 — Container.** Multi-stage build, non-root UID 10001, package
      manager stripped, pinned deps, verified under a read-only root filesystem.
- [ ] **Phase 3 — Kubernetes (local).** kind cluster, ConfigMap supplying the
      threshold, Deployment with probes and a hardened `securityContext`,
      Service, NetworkPolicy with default-deny egress.
- [ ] **Phase 4 — Terraform.** `cluster-local/` split from `app/` so the cluster
      layer can be swapped for AKS or EKS without touching the app layer. `k8s/`
      becomes a Helm chart.
- [ ] **Phase 5 — CI/CD.** GitHub Actions and GitLab CI side by side, same
      stages, so the platform differences are explicit.
- [ ] **Phase 6 — Supply chain.** Trivy gate on HIGH/CRITICAL, CycloneDX SBOM,
      Checkov on the Terraform, Cosign signing — including the model artifact.
- [ ] **Phase 7 — Podman/Buildah** on Rocky Linux (the RHEL-native toolchain).
- [ ] **Phase 8 — One deliberate cloud run**, verified then destroyed.
- [ ] **Phase 9 — Kyverno + Falco** for admission policy and runtime detection.

No CI badges yet — there is no pipeline to report on until Phase 5, and a badge
for a pipeline that does not exist is worse than no badge.

---

## Repository layout

```
CICD-Pipeline-Lab/
├── CLAUDE.md                    # project brief and working agreement
├── README.md
├── .gitignore                   # blocks tfstate/tfvars and datasets
└── phishing-detector/
    ├── app/main.py              # the service
    ├── config/detector.yaml     # threshold — the ConfigMap mounts over this
    ├── model/
    │   ├── phishing_nb.joblib   # fitted artifact, 1.8MB
    │   └── metrics.json         # held-out metrics, written by training
    ├── training/train.py        # offline; ships in neither image nor CI
    ├── tests/test_api.py        # 13 tests
    ├── Dockerfile               # multi-stage, non-root, no package manager
    ├── requirements.txt         # runtime, pinned exactly
    ├── requirements-dev.txt     # pytest, httpx, ruff
    └── requirements-train.txt   # adds pandas — training only
```

Training runs offline and ships nowhere near the serving image. `pandas` is a
training dependency and is deliberately absent from `requirements.txt` — a
package that only training needs has no business in something that only has to
answer `/predict`.

The ~30MB training dataset is not committed. Only the 1.8MB fitted artifact ships.

---

## Notes on things that are easy to get wrong

**The scikit-learn pin is load-bearing.** The model artifact is a pickle of
fitted scikit-learn objects. Deserializing under a different version warns at
best and breaks at worst, so the pin must match the version that produced the
artifact.

**`joblib.load()` executes arbitrary code during unpickling.** Loading an
untrusted model file is equivalent to running an untrusted binary. Here the
artifact is built by this repo's own training script and travels inside the
image, but that property has to be protected deliberately — which is why signing
the model with Cosign is on the Phase 6 list rather than treated as optional
polish.

**`/predict` takes the URL as a query parameter.** Convenient for `curl`, but it
means a URL under investigation lands in every access log and proxy log along the
path. A POST body is the better choice in production. It is a GET here for demo
ergonomics, and that is a known tradeoff rather than an oversight.

---

## Environment

macOS on Apple Silicon, Docker Desktop, Python 3.12. Phases 3–4 add kind,
kubectl, Helm, and Terraform. Everything runs locally; no cloud resources are
provisioned until Phase 8, and that run is destroyed the same day.
