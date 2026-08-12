# CICD-Pipeline-Lab

[![CI](https://github.com/jameslrivera/CICD-Pipeline-Lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jameslrivera/CICD-Pipeline-Lab/actions/workflows/ci.yml)

**Personal DevOps pipeline project built using Docker, Kubernetes, Terraform,
Helm, and GitHub Actions.**

---

## Background

As the software engineering lifecycle is updated and teams work together more
efficiently, it's a necessity to understand the tools and procedures involved in
Software Development and IT Operations. This includes containerization, container
orchestration, CI/CD automation, Infrastructure as Code, and automation and
scripting. This project tackles each of those concepts and their associated
tools.

The application at the center of it is a phishing URL classifier: a FastAPI
service that scores a URL using a Naive Bayes model trained on roughly 507,000
labeled URLs. It is a genuine security tool, and it is also intentionally the
*smallest* part of this repository. It exists to be something worth protecting,
so that every control applied to it is applied for a real reason.

The architectural decision the whole lab is built around is the separation of the
**model** from the **policy**. The trained model is an immutable artifact baked
into the container image; changing it is a real release, with a new build, a new
scan, and a new signature. The decision threshold — the probability at which a
URL is called phishing — is mutable configuration, read from disk on every
request. That means a Kubernetes ConfigMap can retune detection sensitivity
without redeploying the model, which is exactly what an analyst needs when false
positives spike at 2am.

---

## Inspiration

Reading about a stack and operating it are different things. Building the whole
chain end to end — application, image, cluster, infrastructure code, pipeline —
forces every layer to actually work together, and that is where the details that
documentation skips over turn up.

The architecture is organised around one decision: separating the **model** from
the **policy**. The trained model is an immutable artifact baked into the
container image, so changing it is a real release with a new build and a new
scan. The decision threshold — the probability at which a URL is called
phishing — is mutable configuration, re-read from disk on every request. That
means a Kubernetes ConfigMap can retune detection sensitivity without redeploying
the model, which is exactly what an analyst needs when false positives spike at
2am. Almost every later phase exists to deliver, protect, or prove that split.

The habit that turned out to matter most was verifying each layer instead of
assuming it worked. Several controls in this repository looked correct, applied
without error, and did nothing at all. A NetworkPolicy was accepted by the API
server and filtered no traffic. A test suite passed while a liveness endpoint
returned 503 to every probe. A detector failed open on a one-word configuration
typo while still reporting healthy. Each was found by testing the control rather
than trusting it, and each is documented here rather than quietly fixed.

The bias throughout is toward proving things: commands that were actually run,
output that was actually captured, and honest notes where something does not work
or is not finished.

---

## Contents

1. [Application](#1-application)
2. [Containerization](#2-containerization)
3. [Kubernetes](#3-kubernetes)
4. [Terraform and Helm](#4-terraform-and-helm)
5. [CI/CD](#5-cicd)

[Conclusion](#conclusion)

---

## 1. Application

A FastAPI service exposing four endpoints: `/healthz` for liveness, `/readyz` for
readiness, `/info` for what the instance is running, and `/predict?url=` to score
a URL.

The model is a `TfidfVectorizer` feeding a `MultinomialNB` classifier. URLs are
split on runs of alphanumerics, so `paypal.co.uk/cgi-bin/webscr` becomes
`[paypal, co, uk, cgi, bin, webscr]`. That is where the signal lives — brand
names appearing in paths where they do not belong, unusual TLDs, long hex blobs.

Held-out results on 101,439 URLs the model never saw:

| Metric | Value |
| ------ | ----- |
| **Majority-class baseline accuracy** | **0.7746** |
| Accuracy | 0.9648 |
| Precision | 0.9617 |
| Recall | 0.8786 |
| F1 | 0.9183 |
| ROC AUC | 0.9898 |

The baseline is listed first because it is what makes the rest meaningful.
Always guessing "legitimate" scores 77.5% on this class balance, so the model
beats doing nothing by 19 points — not by 96.

### Three forms of leakage, found and corrected

An earlier version of this model used eight hand-crafted numeric features and
scored F1 0.342 — barely above the do-nothing baseline. Three separate problems
were inflating even that:

The **scaler was fitted before the train/test split**, so test-set statistics
leaked into training. The vectorizer now lives inside a `Pipeline`, which by
construction only ever fits on the training split.

**Duplicate URLs were never removed.** The raw dataset is 7.7% duplicates —
42,151 rows of 549,346. Without deduplication the same URL lands in both splits
and the model is scored on rows it memorized.

**Two features could not be computed at inference time.** They were group means
of URL length by domain, calculated across the whole dataset. Scoring a single
URL, there is no group to average over. That is training/serving skew: the
features were leaky *and* unservable. Tokenization replaced them.

The split is now stratified and seeded, so retraining reproduces the artifact
exactly — verified by training twice and comparing.

### Liveness and readiness deliberately disagree

`/healthz` returns 200 whenever the process is serving, **even with a completely
broken model**. `/readyz` returns 503 when the model or the threshold will not
load.

The distinction is what Kubernetes does with each answer. A liveness failure
makes the kubelet kill and restart the container. A readiness failure removes the
pod from the Service's endpoints but leaves it running.

So if liveness checked the model, one bad artifact would restart-loop every pod
in the cluster — turning a configuration problem into an outage, and destroying
the evidence, because containers would die before anyone could exec in. Splitting
them means a bad artifact degrades the service in a controlled, debuggable way.

**Liveness answers "should I be killed?" Readiness answers "should I get
traffic?"** Anything an operator could fix without a restart belongs in readiness.

### Known limitations

The tokenizer is `[A-Za-z0-9]+`, so it discards every non-ASCII character before
the model sees anything. Internationalized domains are stripped to almost nothing
and score as phishing — `пример.рф/login` returns 0.9934. That matters more than
an ordinary accuracy gap, because IDN homograph attacks are themselves a phishing
technique, so the model is blind in a phishing-relevant area.

The same cause makes short URLs score high, since there is little evidence either
way: `google.com` is 0.3861, but `google.com/search?q=weather` drops to 0.0036.

Neither is fixed. Both are documented, because shipping a detector without
knowing where it fails is worse than the failures.

**42 tests** cover the service. They were validated by mutation testing —
deliberately breaking the code and confirming the suite catches it.

---

## 2. Containerization

A multi-stage image: the builder installs dependencies into a virtualenv, and the
runtime stage receives only the finished result, so pip's caches and build
artifacts never ship. Final size is 594MB, most of it scikit-learn, scipy, and
numpy.

| Control | Why |
| ------- | --- |
| Runs as UID 10001 | Root in a container is root on the host kernel if anything escapes the namespace |
| No package manager | A working `pip` beside an attacker with a foothold is an install tool |
| Read-only root filesystem | An attacker cannot drop a binary, webshell, or cron entry anywhere persistent |
| Exact version pins | A build that resolves different versions on different days is not reproducible |
| Thread pools pinned to 1 | scikit-learn sizes pools from the *host* CPU count and oversubscribes against a cgroup limit |

### The UID is numeric on purpose

Kubernetes `runAsNonRoot` must verify the user is not root *before* starting the
container, and it cannot resolve a username against `/etc/passwd` without running
the image first. A named user fails with `container has runAsNonRoot and image
has non-numeric user`. So the Dockerfile writes `USER 10001:10001` as a number,
and the Deployment asserts the same number.

### pip was in the image twice

The Dockerfile originally claimed pip never shipped. It did — `python -m venv`
installs pip into the virtualenv, and the whole virtualenv was being copied.

After removing that copy, `pip --version` **still worked**, because
`python:3.12-slim` carries a second installation at
`/usr/local/lib/python3.12/site-packages/pip` that remains on `PATH`. Both had to
go. It now reports `pip: not found` and `No module named pip`.

This was only found by checking rather than trusting the comment. The check is
one command; the assumption had been confidently wrong.

### The scikit-learn pin is load-bearing

The model artifact is a pickle of fitted scikit-learn objects. Deserializing
under a different version warns at best and breaks at worst, so the pin must
match the version that produced the file — which the training script now records
in `metrics.json`.

Relatedly, `joblib.load()` executes arbitrary code during unpickling. Loading an
untrusted model file is equivalent to running an untrusted binary. Here the
artifact is built by this repository's own training script and travels inside the
image, but that is a property to protect deliberately rather than assume.

---

## 3. Kubernetes

A three-node kind cluster — one control-plane, two workers — with the node image
pinned by digest rather than tag. A tag can be repointed at new content; a digest
*is* the content.

The workload runs as a Deployment of two replicas with three probes, a hardened
`securityContext`, a ClusterIP Service, and NetworkPolicies.

### Proving the ConfigMap is what gets read

The image ships `threshold: 0.5`. The ConfigMap supplies `0.30`. A running pod
reports `0.30`, which is the proof — not an assertion — that it reads the
ConfigMap and not its own image layer. `google.com` scores 0.3861, so it is
classified as legitimate by the image's default and as phishing in-cluster, from
the same image, with no rebuild.

The ConfigMap is mounted as a **directory**, not as a single file via `subPath`.
That distinction is the difference between a live-updating config and one needing
a restart: `subPath` mounts are copied once and never resynced by the kubelet,
while a directory mount is kept in sync. Since the app re-reads the threshold on
every request, a directory mount means retuning detection requires no rollout at
all.

### The NetworkPolicy was silently doing nothing

The first version of this cluster used kind's default CNI, `kindnet`. The
NetworkPolicy applied cleanly, `kubectl get networkpolicy` listed it, and
`kubectl describe` printed the rules. It filtered nothing.

`kindnet` provides pod networking but ships **no NetworkPolicy controller**. The
API server accepts and stores the object regardless, because `networkpolicies` is
a built-in Kubernetes resource — enforcement is the CNI's job, and if the CNI
does not implement it, nothing anywhere reports a problem.

This was caught by testing enforcement rather than trusting the manifest: running
the same egress attempt in a namespace with a default-deny policy and in one
without, then comparing.

```console
1. BASELINE - no policy            → CONNECTED - egress allowed
2. EGRESS - default-deny namespace → BLOCKED - TimeoutError
3. DNS - explicitly allowed        → DNS OK -> 10.96.0.1
```

Under `kindnet`, test 2 returned `CONNECTED` — identical to the unrestricted
baseline. The cluster now disables the default CNI and installs Calico instead,
and the results above come from that cluster.

**A security control that does not work is worse than no control**, because it
produces confidence without protection. "The manifest applied successfully" is
not evidence that traffic is being filtered.

### Hardening that was purely advisory

Every `securityContext` setting was voluntary until the namespace carried Pod
Security Admission labels. Verified against the live API server: a pod requesting
`privileged: true`, `hostNetwork: true`, and `hostPath: /` was **admitted**. With
`pod-security.kubernetes.io/enforce: restricted`, the same pod is rejected with a
list of every violation.

Alongside that: a dedicated ServiceAccount with the API token mount disabled (the
app never calls the Kubernetes API but was being handed a credential for it), a
PodDisruptionBudget, topology spread across nodes, a tmpfs `/tmp` so the
read-only root does not break anything reaching for temporary files, and a
`preStop` pause so rollouts stop resetting in-flight connections.

### Two rules that are easy to get wrong

**DNS egress must be explicitly allowed.** With egress denied and no exception,
every hostname lookup hangs until it times out — presenting as a service that
stalls and then fails resolving a name, which looks exactly like an application
bug.

**A comment I wrote was wrong, and testing caught it.** The ingress rule
originally admitted everyone, justified by a claim that a narrower rule would
block kubelet probes since they come from the node's IP. That was tested and is
false on Calico, which does not apply workload ingress policy to host-sourced
traffic. Ingress is now restricted to namespaces labelled
`detector-client=allowed`, verified three ways: an unlabelled namespace is
blocked, a labelled one succeeds, and both pods stay Ready with zero restarts.

### A pod CIDR that swallowed the LAN

Calico's documented default pool is `192.168.0.0/16`. On the machine running this
lab — sitting at `192.168.1.235` behind a `192.168.1.1` gateway — that meant the
pod network consumed the operator's own subnet. Pods could reach `1.1.1.1` but
not the laptop or the router.

The cluster now uses `100.64.0.0/16`, RFC 6598 carrier-grade NAT space chosen
precisely because it is almost never used on a LAN. On a corporate or DoD network
where both `192.168/16` and `10/8` are in heavy use, the original setting would
present as a private registry or syslog collector being unreachable from pods and
nothing else — a genuinely maddening failure to diagnose.

---

## 4. Terraform and Helm

The manifests became a Helm chart whose templates contain no environment-specific
facts, and the infrastructure moved into two Terraform layers:

```
terraform/
├── cluster-local/   # kind provider — expected to be replaced wholesale for cloud
└── app/             # kubernetes + helm providers — must NOT change when it is
```

The app layer takes a kubeconfig path and a context name and nothing else. It
deliberately does **not** read the cluster layer's state through a
`terraform_remote_state` data source — that would be tidier Terraform and would
also weld it to kind, defeating the entire purpose of the split. Point those two
variables at AKS and the directory works unchanged.

Calico is installed by a `null_resource` shelling out to the same script a human
would run. The alternatives are worse: a `kubernetes_manifest` per Calico object
means vendoring thousands of lines of CRDs, and it fails on the first plan
because those CRDs do not exist yet.

The chart deliberately **omits** the `checksum/config` annotation most charts
add. That annotation forces a rollout when a ConfigMap changes, which would
defeat the live-retune design the application was built around.

### The Terraform loop

`init` downloads providers and writes the dependency lock. `fmt` rewrites to
canonical style. `validate` checks syntax and types without touching the cluster.
`plan -out=tfplan` saves a decision to a file, and `apply tfplan` replays exactly
that decision — no re-planning, so nothing can change between review and
execution. Running `plan` again afterwards should report **"No changes. Your
infrastructure matches the configuration."** That final check is the point: a
configuration that cannot converge will fight you forever.

`.terraform.lock.hcl` is committed deliberately — it pins provider versions *and*
hashes, and it is what makes `terraform init` reproducible for anyone else. State
files and `.tfvars` are gitignored, because state holds resource attributes in
plaintext including anything marked sensitive.

### Drift detection does not work the way you would assume

The most interesting result of this phase. Two drift experiments, opposite
outcomes:

```console
$ kubectl scale deployment/phishing-detector --replicas=4
$ terraform plan
  No changes. Your infrastructure matches the configuration.    # ← running 4, config says 2

$ kubectl label namespace phishing-detector pod-security.kubernetes.io/enforce-
$ terraform plan
  # kubernetes_namespace_v1.app will be updated in-place
      + "pod-security.kubernetes.io/enforce" = "restricted"
  Plan: 0 to add, 1 to change, 0 to destroy.
```

Terraform caught the namespace label because `kubernetes_namespace_v1` is a
resource it manages **directly** — it reads the live object and compares. It
missed the replica count because `helm_release` tracks the *release*: its chart
version, its values, its revision, not the Kubernetes objects the release
produced. Nobody changed the values, so from Terraform's point of view nothing
drifted.

This matters beyond the lab. "We manage it in Terraform" is widely taken to mean
"Terraform will correct anything that changes," and for anything behind a
`helm_release` that is false. A `kubectl scale`, a `kubectl edit`, or a sidecar
injected by a mutating webhook all survive `terraform plan` in silence.
Reconciling requires `terraform apply -replace=helm_release.phishing_detector`; a
plain apply will not do it.

### A near-miss worth recording

The `tehcyx/kind` provider writes a kubeconfig named `<cluster>-config` into the
module directory as an undeclared side effect. It contains a client certificate,
client key, and CA data — working cluster-admin credentials — and it was staged
for commit before a pre-commit scan caught it. Nothing in the Terraform
configuration mentions that the file exists, which is exactly why it is worth
checking what a new provider leaves behind.

---

## 5. CI/CD

The same pipeline expressed twice —
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) and
[`.gitlab-ci.yml`](.gitlab-ci.yml). Identical stages: lint, test, build, push,
deploy dry-run. What differs is the platform, and the differences are annotated
inline in both files rather than in a separate document.

GitLab is included because it self-hosts and can run inside an air-gapped enclave
with no egress, which is why it tends to be the CI platform in defense
environments.

The Actions pipeline runs four jobs. Lint and test cover the application; chart
validation and Terraform validation run in parallel with them, since a template
or HCL error has nothing to do with the Python code. The image job depends on
both passing.

### What the pipeline actually checks

Anyone can write a pipeline that runs `pytest`. These steps exist because each
one corresponds to something that has already gone wrong in this project:

**The image must run as uid 10001** — asserted against the built artifact, not
claimed in prose.

**The image must contain no package manager** — because `pip` was present twice
and removing only one copy left `pip install` fully working.

**The image must serve under a read-only root filesystem**, which is what the
Deployment imposes.

**A known phishing URL must be classified as phishing.** This is the check that
would catch a corrupt or wrong model artifact shipping inside an otherwise
perfectly healthy image, where every other check passes.

**The rendered chart must still contain its security controls.** The chart job
greps `helm template` output for `runAsNonRoot`, `readOnlyRootFilesystem`,
`allowPrivilegeEscalation`, `runAsUser: 10001`, and
`automountServiceAccountToken: false`. A template can render perfectly valid YAML
that quietly dropped one of those, and nothing else would notice.

**The integration tests must run, not skip.** They are written to *fail* when the
model artifact is missing, and the pipeline separately asserts the file is
non-empty. A green run that silently tested no model is worse than a red one.

Verified output from the run:

```console
runs as uid 10001
no pip present
{"status":"ready", ...}                    ← served under --read-only
pushing manifest for ghcr.io/jameslrivera/phishing-detector:0.1.0@sha256:b97e2953...
```

Images are tagged with both the version and the commit SHA, so any deployed image
traces back to the commit that built it.

### Deploy is a dry run, and says so

The only cluster is a kind cluster on a laptop, which a hosted runner cannot
reach. The deploy stage renders the chart and stops. Rendering proves the
manifests are valid; it does not prove they apply, and a pipeline claiming to
deploy when it does not would be the same class of untruth as a NetworkPolicy
that stores cleanly and filters nothing.

On a self-hosted runner inside the cluster's network — the normal defense
arrangement — that job becomes a real `helm upgrade --install`.

### Four differences worth being able to explain

**Execution environment.** Actions hands you a VM with a large pre-installed
toolchain and you add languages with `setup-*` actions. GitLab gives you a
container per job and you name the image. GitLab's model is more explicit and
ports more cleanly into an air-gapped registry mirror.

**Building images.** Actions has a first-party buildx action and a Docker daemon
already running. GitLab jobs *are* containers, so building means
Docker-in-Docker or a daemonless builder. In an enclave you would reach for
Buildah or Kaniko, which build without a privileged daemon — which matters when
cluster policy forbids privileged containers, exactly the policy this project
enforces.

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

### Both pipelines are verified green

The Actions pipeline runs on GitHub and publishes to ghcr.io. The GitLab pipeline
runs at [jameslrivera-group/cicd-pipeline-lab](https://gitlab.com/jameslrivera-group/cicd-pipeline-lab)
and publishes to the GitLab container registry. All six GitLab jobs pass.

Getting the GitLab side to run was worth more than writing it. The file was
YAML-valid, mirrored the working Actions pipeline stage for stage, and had been
committed as correct — and it could not create a single job.

The cause was a GitLab-specific trap with no GitHub Actions equivalent: the build
job was named `image`, which is a **reserved top-level keyword**. GitLab parsed
the job as the global image directive, found a map where it expected a string,
and rejected the entire file with `image name should be a string`. The result was
a pipeline with zero jobs that failed in the same millisecond it was created —
and the API reported `yaml_errors: null`, so the real message existed only on the
pipeline page.

Renaming it to `build:image` fixed it. Nothing about that is discoverable by
reading the file, which is the whole argument for running a pipeline rather than
shipping one that looks right.

---

## Conclusion

The through-line across all five phases is the difference between a control being
*configured* and a control being *effective*.

Three findings make the point better than any description of the architecture:

**A NetworkPolicy that filtered nothing.** kind's default CNI ships no policy
controller, but the Kubernetes API server accepts and stores a NetworkPolicy
regardless. `kubectl apply`, `kubectl get`, and `kubectl describe` all reported
success while traffic the policy claimed to deny flowed freely. It was found by
running the same connection attempt in a restricted namespace and an
unrestricted one and comparing the results.

**A test suite that proved nothing.** Deliberately breaking the application in
six different ways — including making the liveness endpoint return 503 to every
probe — left the entire suite green. The tests asserted on response bodies and
never on status codes.

**Hardening that was purely advisory.** Every `securityContext` setting in the
Deployment was voluntary until the namespace carried Pod Security Admission
labels. A pod requesting `privileged: true` with the host root filesystem mounted
was admitted by the API server without complaint.

The pattern is the same each time: the artifact existed, the tooling reported
success, and nothing was actually enforced. Silence is not evidence. The habit
worth carrying out of this project is asking "how would I know if this were not
working?" before considering something done — and then answering it with a test
rather than an assumption.

A secondary theme is being honest about limits. The classifier's tokenizer is
ASCII-only, so internationalized domains are stripped of signal and score as
phishing — which matters because homograph attacks are themselves a phishing
technique. Short URLs regress toward the class prior. The deploy stage of both
pipelines renders manifests rather than applying them, because a hosted runner
cannot reach a local cluster. None of that is hidden, because a portfolio that
overstates what it proves is worth less than one that states plainly what it
does.

---

## Repository layout

```
CICD-Pipeline-Lab/
├── phishing-detector/       # the application, tests, Dockerfile, training script
├── charts/                  # Helm chart — environment-agnostic templates
├── k8s/                     # raw manifests (superseded by the chart, kept for reference)
├── terraform/
│   ├── cluster-local/       # kind provider — replaced wholesale for cloud
│   └── app/                 # kubernetes + helm providers — unchanged by that swap
├── scripts/                 # Calico install, keeping pod CIDRs in agreement
├── docs/technical-notes.md  # detailed verified notes and captured output
├── .github/workflows/ci.yml # GitHub Actions pipeline
└── .gitlab-ci.yml           # GitLab CI pipeline
```

## Status

| Phase | State |
| ----- | ----- |
| 1. Application | Complete — 42 tests passing |
| 2. Containerization | Complete — verified under a read-only root filesystem |
| 3. Kubernetes | Complete — NetworkPolicy enforcement verified, not assumed |
| 4. Terraform and Helm | Complete — both layers converge cleanly |
| 5. CI/CD | Complete — both pipelines verified green and publishing to their registries |

Optional follow-on work: supply-chain controls (Trivy, SBOM, Checkov, Cosign),
a Podman/Buildah rebuild on Rocky Linux, one deliberate cloud deployment, and
in-cluster policy enforcement with Kyverno and Falco.
