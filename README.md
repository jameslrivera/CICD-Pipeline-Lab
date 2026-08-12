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

### Known limitations

Probing the deployed service surfaced two systematic weaknesses, both from the
tokenizer being ASCII-only (`[A-Za-z0-9]+`).

Non-ASCII domains are stripped to almost nothing before the model sees them, and
score as phishing: `пример.рф/login` returns 0.9934, `münchen.de/willkommen`
0.4984. That is worse than a normal accuracy gap, because IDN homograph attacks
are themselves a phishing technique — the model is blind in a phishing-relevant
area. Punycode-normalizing before tokenizing would fix it.

Short URLs score high because there is little evidence either way: `google.com`
is 0.3861, but `google.com/search?q=weather` is 0.0036, and a bare `http://`
reaches 0.9812 on the single token `http`.

Neither is fixed yet — both are model work, and this phase is about the pipeline.
They are documented because shipping a detector without knowing where it fails is
worse than the failures.

Full detail on the service, the model, and the container is in
[phishing-detector/README.md](phishing-detector/README.md).

---

## Kubernetes

A 3-node kind cluster (one control-plane, two workers), pinned by image digest.
`k8s/` is numbered so `kubectl apply -f k8s/` is order-safe — kubectl processes a
directory alphabetically, so an unnumbered `namespace.yaml` gets applied *after*
the ConfigMap that needs it.

```bash
kind create cluster --config kind-config.yaml && ./scripts/install-calico.sh && kind load docker-image phishing-detector:0.1.0 --name cicd-lab && kubectl apply -f k8s/
```

Calico is installed by a script rather than a plain `kubectl apply` because its
IP pool and kind's `podSubnet` are independent settings that have to agree, and
the pool is fixed when `calico-node` first starts.

The pod CIDR is `100.64.0.0/16` (RFC 6598), and that choice is a bug fix. Calico
defaults to `192.168.0.0/16`, which on this machine swallowed the operator's own
LAN — the laptop sits on `192.168.1.235` behind a `192.168.1.1` gateway, so pods
could reach `1.1.1.1` but could not reach the laptop or the router. On a
corporate or DoD network, where both `192.168/16` and `10/8` are in heavy use,
that presents as a private registry or syslog collector being unreachable from
pods and nothing else.

`kind load` is not optional. kind nodes are containers with their own image
store and cannot see the host's Docker images — without it the pods sit in
`ErrImagePull` for an image that is sitting right there on the laptop.

The ConfigMap is mounted as a **directory**, not as a single file via `subPath`.
That distinction is the difference between a live-updating config and one that
needs a restart: `subPath` mounts are copied once and never resynced, while a
directory mount is kept in sync by the kubelet. Since the app re-reads the
threshold every request, editing the ConfigMap retunes detection with no rollout.

### Workload hardening

| Control | Why it is there |
| ------- | --------------- |
| `pod-security.kubernetes.io/enforce: restricted` on the namespace | Without it every `securityContext` setting is voluntary. Verified: before the label, a pod with `privileged: true` and `hostPath: /` was **admitted** by the API server. After it, the same pod is rejected. |
| Dedicated ServiceAccount, `automountServiceAccountToken: false` | The app never calls the Kubernetes API, but was being handed a JWT in its filesystem. Verified gone from the running container. |
| `topologySpreadConstraints` with `DoNotSchedule` | The replicas were landing on separate nodes by luck. The cluster default is `ScheduleAnyway`, which is a preference, not a rule. |
| PodDisruptionBudget `minAvailable: 1` | `maxUnavailable: 0` only governs rollouts. A `kubectl drain` is not a rollout and could evict both replicas at once. |
| tmpfs `emptyDir` at `/tmp` | `readOnlyRootFilesystem` left the process nowhere to write. Nothing writes today, but `tempfile`, upload spooling, and joblib memmaps all reach for `/tmp`, and the failure looks like an opaque 500. |
| `preStop` sleep + `terminationGracePeriodSeconds` | Endpoint removal and SIGTERM are concurrent, so rollouts were resetting in-flight connections. |

### The NetworkPolicy was silently doing nothing

The first version of this cluster used kind's default CNI, `kindnet`. The
NetworkPolicy applied cleanly, `kubectl get networkpolicy` listed it, and
`kubectl describe` showed the rules. It filtered nothing.

kindnet provides pod networking but ships **no NetworkPolicy controller**. The
API server accepts and stores the object regardless, because `networkpolicies`
is a built-in Kubernetes resource — enforcement is the CNI's job, and if the CNI
does not implement it, nothing anywhere reports a problem.

This was caught by testing enforcement instead of trusting the manifest — running
the same egress attempt in a namespace with a default-deny policy and in one
without, and comparing:

```console
### 1. BASELINE - default namespace, no policy (expect: CONNECTED)
RESULT: CONNECTED - egress allowed
### 2. EGRESS - phishing-detector namespace, default-deny (expect: BLOCKED)
RESULT: BLOCKED - TimeoutError
### 3. DNS - phishing-detector namespace, explicitly allowed (expect: DNS OK)
RESULT: DNS OK -> 10.96.0.1
```

Under kindnet, test 2 returned `CONNECTED` — identical to the unrestricted
baseline. The cluster is now built with `disableDefaultCNI: true` and Calico
installed instead, and the results above are from that cluster.

**A security control that does not work is worse than no control**, because it
produces confidence without protection. "The manifest applied successfully" is
not evidence that traffic is being filtered.

### Two rules that are easy to get wrong

**DNS egress has to be explicitly allowed.** With egress denied and no DNS
exception, every hostname lookup hangs until it times out. The symptom is not
"connection refused" — it is a service that appears to hang for seconds and then
fails resolving a name, which looks exactly like an application bug.

**A claim I got wrong, and how it was caught.** The first version of the ingress
rule had no `from:` at all — meaning any pod in any namespace could call
`/predict` and `/info` unauthenticated. The comment justifying that said a
narrower rule would block the kubelet's probes, since probes originate from the
node's IP rather than the pod network.

That was tested and it is false on Calico. A pod matching no ingress-allow policy
at all still passed its HTTP readiness probe and reached `Ready`, because Calico
does not apply workload ingress policy to traffic from the local host — which is
exactly where probes come from. Ingress is now restricted to namespaces labelled
`detector-client=allowed`, verified three ways: an unlabelled namespace is
blocked, a labelled one succeeds, and both pods stay `1/1 Ready` with 0 restarts.

Worth flagging that this is CNI-specific behaviour, not a guarantee of the
NetworkPolicy spec. On a CNI that does subject host traffic to workload policy,
probes would need an explicit allowance — so it is a thing to verify on the CNI
you actually run, which is the general lesson.

---

## Terraform and Helm

Two layers, deliberately separate:

```
terraform/
├── cluster-local/   # kind provider — expected to be replaced wholesale for cloud
└── app/             # kubernetes + helm providers — must NOT change when it is
charts/
└── phishing-detector/   # environment-agnostic templates; only values differ
```

```bash
terraform -chdir=terraform/cluster-local init && terraform -chdir=terraform/cluster-local apply
```

```bash
kind load docker-image phishing-detector:0.1.0 --name cicd-lab
```

```bash
terraform -chdir=terraform/app init && terraform -chdir=terraform/app apply
```

The app layer takes a kubeconfig path and a context name and nothing else. It
deliberately does **not** read the cluster layer's state with a
`terraform_remote_state` data source — that would be tidier Terraform and would
also weld it to kind, defeating the split. Point those two variables at AKS and
the directory works unchanged.

The `kind load` between the two applies is a genuine manual step: kind nodes have
their own image store and cannot see the host's Docker images. On a cloud cluster
this is replaced by a registry push, which is Phase 5's job.

### Drift detection does not work the way you would assume

The interesting result from this phase. Two drift experiments, opposite outcomes:

```console
$ kubectl scale deployment/phishing-detector -n phishing-detector --replicas=4
$ terraform plan
  No changes. Your infrastructure matches the configuration.     # ← running 4, config says 2

$ kubectl label namespace phishing-detector pod-security.kubernetes.io/enforce-
$ terraform plan
  # kubernetes_namespace_v1.app will be updated in-place
      + "pod-security.kubernetes.io/enforce" = "restricted"
  Plan: 0 to add, 1 to change, 0 to destroy.
```

Terraform detected the namespace label because `kubernetes_namespace_v1` is a
resource it manages **directly** — it reads the live object and compares. It did
not detect the replica count because `helm_release` tracks the *release* — its
chart version, its values, its revision — not the Kubernetes objects the release
produced. Nobody changed the values, so from Terraform's point of view nothing
drifted.

This matters more than a lab curiosity. "We manage it in Terraform" is often
taken to mean "Terraform will correct anything that changes," and for anything
behind a `helm_release` that is false. A `kubectl scale`, a `kubectl edit`, or a
sidecar injected by a mutating webhook all survive `terraform plan` silently.

Reconciling the two is different work:

```bash
terraform -chdir=terraform/app apply -replace=helm_release.phishing_detector
```

That forces the release to be recreated, which restored the replica count to 2.
A plain `apply` will not do it.

### The Terraform loop

`init` downloads providers and writes the dependency lock. `fmt` rewrites to
canonical style. `validate` checks syntax and types without touching the cluster.
`plan -out=tfplan` saves a decision to a file, and `apply tfplan` replays exactly
that decision — no re-planning, so nothing can have changed between the review
and the execution. Running `plan` again afterwards should report **"No changes.
Your infrastructure matches the configuration."** That final check is the point:
a config that cannot converge is a config that will fight you forever.

`.terraform.lock.hcl` is committed on purpose — it pins provider versions *and*
hashes, and it is what makes `terraform init` reproducible for anyone else.
State files and `.tfvars` are gitignored: state holds resource attributes in
plaintext, including anything marked sensitive.

---

## CI/CD

The same pipeline expressed twice — [`.github/workflows/ci.yml`](.github/workflows/ci.yml)
and [`.gitlab-ci.yml`](.gitlab-ci.yml). Stages are identical: lint, test, build,
push, deploy dry-run. What differs is the platform, and the differences are
annotated inline in both files rather than in a separate document.

GitLab is here because it self-hosts, so the whole pipeline can run inside an
air-gapped enclave with no egress. That is why it tends to be the CI platform in
DoD environments.

### What the pipeline actually checks

Anyone can write a pipeline that runs `pytest`. These steps exist because each
one corresponds to something that has already gone wrong in this project:

**The image must run as uid 10001.** Asserted against the built artifact, not
claimed in prose.

**The image must contain no package manager.** `pip` was in the image twice —
once in the virtualenv, once in the base image's `/usr/local` — and removing only
the first left `pip install` fully working inside a running container.

**The image must serve under a read-only root filesystem**, which is what the
Deployment imposes.

**A known phishing URL must be classified as phishing.** This is the check that
would catch a corrupt or wrong model artifact shipping inside an otherwise
perfectly healthy image — every other check would pass.

**The rendered chart must still contain its security controls.** The chart job
greps the `helm template` output for `runAsNonRoot`, `readOnlyRootFilesystem`,
`allowPrivilegeEscalation`, `runAsUser: 10001`, and
`automountServiceAccountToken: false`. A template can render perfectly valid YAML
that quietly dropped one of those, and nothing else in the pipeline would notice.

**The integration tests must run, not skip.** They are written to *fail* when the
model artifact is missing, and the pipeline separately asserts the file is
non-empty. A green run that silently tested no model is worse than a red one.

### Deploy is a dry run, and says so

The only cluster is a kind cluster on a laptop, which a hosted runner cannot
reach. The deploy stage renders the chart and stops. Rendering proves the
manifests are valid; it does not prove they apply, and a pipeline claiming to
deploy when it does not would be the same class of untruth as a NetworkPolicy
that stores cleanly and filters nothing.

On a self-hosted runner inside the cluster's network — the normal DoD
arrangement — that job becomes a real `helm upgrade --install`.

### Four differences worth being able to explain

**Execution environment.** GitHub Actions hands you a VM with a large
pre-installed toolchain and you add languages with `setup-*` actions. GitLab
gives you a container per job and you name the image. GitLab's model is more
explicit and ports more cleanly into an air-gapped registry mirror.

**Building images.** Actions has a first-party buildx action and a Docker daemon
already running. GitLab jobs *are* containers, so building means Docker-in-Docker
or a daemonless builder. In an enclave you would reach for Buildah or Kaniko,
which build without a privileged daemon — which matters when cluster policy
forbids privileged containers, exactly the policy this project enforces.

**Test reporting.** GitLab parses JUnit XML natively and renders failures in the
merge request. Actions needs a third-party action for the same thing.

**Conditionals.** Actions uses `if:` on a step; GitLab uses `rules:` on a job.
Both express the same intent here — a pull request builds and smoke-tests the
image but must never publish it, or anyone who can open a PR can publish to the
registry namespace.

### Credentials

Neither pipeline stores a registry credential. Actions uses the run-scoped
`GITHUB_TOKEN` with `packages: write` granted to the image job alone; GitLab uses
`CI_JOB_TOKEN`, which dies with the job. Nothing long-lived exists to leak.

---

## Roadmap

- [x] **Phase 1 — Application.** FastAPI service over a trained classifier, 13 tests.
- [x] **Phase 2 — Container.** Multi-stage build, non-root UID 10001, package
      manager stripped, pinned deps, verified under a read-only root filesystem.
- [x] **Phase 3 — Kubernetes (local).** 3-node kind cluster, ConfigMap supplying
      the threshold, Deployment with three probes and a hardened
      `securityContext`, ClusterIP Service, and a default-deny NetworkPolicy
      whose enforcement is verified rather than assumed.
- [x] **Phase 4 — Terraform + Helm.** `cluster-local/` split from `app/` so the
      cluster layer can be swapped for AKS or EKS without touching the app
      layer. `k8s/` converted to a Helm chart with environment-agnostic
      templates.
- [x] **Phase 5 — CI/CD.** GitHub Actions and GitLab CI side by side, same
      stages, so the platform differences are explicit. The Actions pipeline is
      verified green and publishing to ghcr.io. The GitLab pipeline is written
      and YAML-valid but **has not been executed** — it is marked as such rather
      than assumed to work, for the same reason the NetworkPolicy in Phase 3 is
      documented as having been silently unenforced.
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
├── README.md
├── kind-config.yaml             # 3-node cluster, Calico, digests pinned
├── k8s/                         # numbered so `apply -f k8s/` is order-safe
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
