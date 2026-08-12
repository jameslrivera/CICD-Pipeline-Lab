# CICD-Pipeline-Lab

[![CI](https://github.com/jameslrivera/CICD-Pipeline-Lab/actions/workflows/ci.yml/badge.svg)](https://github.com/jameslrivera/CICD-Pipeline-Lab/actions/workflows/ci.yml)

**Personal DevOps pipeline project built using Docker, Kubernetes, Terraform,
Helm, and GitLab CI.**

---

## Background

   As the software engineering lifecycle is updated and teams need to work more efficiently together it’s a necessity to understand the tools and procedures involved in Software Development and IT Operations. This includes Containerization, Container Orchestration, CI/CD Automation, Infrastructure as Code and Automating and Scripting. This project tackles each of those concepts and their associated tools.

---

## Inspiration

I was inspired to build a project that encompasses front to end of a DevOps Pipeline.

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

I thought it would be fitting if I used an application that I have already made in the past to build the DevOps pipeline around. So I took a pretrained model that I made from scratch from a past research project. The model classifies URLs as either phishing or safe. I trained the model on over 500,000 URLs from a URL dataset from Kaggle. The model vectorizes the URLs into tokens and then runs the Naive Bayes algorithm to score the URL's likelihood of being phishing. In tests the model performed well;

| Metric | Value |
| ------ | ----- |
| Accuracy | 0.9648 |
| Precision | 0.9617 |
| Recall | 0.8786 |
| F1 | 0.9183 |
| ROC AUC | 0.9898 |

The vectorization works by splitting the URLs into runs of alphanumerics or tokens, so `paypal.co.uk/cgi-bin/webscr` becomes
`[paypal, co, uk, cgi, bin, webscr]`.


I used the Python FastAPI framework to define the endpoints and a uvicorn server to host them. These give you the status of the app and let you run it:

- `/healthz` — health status of the app
- `/readyz` — whether the model loaded and the app is ready to serve traffic
- `/info` — what the instance is running (model, classifier, current threshold)
- `/predict?url=` — score a URL

### Running it locally

Install dependencies and start the server:

```bash
cd phishing-detector && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

```bash
.venv/bin/uvicorn app.main:app --port 8090
```

In a second terminal:

```bash
curl -s localhost:8090/info
```
```json
{"model":"phishing_nb.joblib","classifier":"MultinomialNB","vocabulary_size":57702,"threshold":0.5}
```

Score a known phishing URL:

```bash
curl -s --get --data-urlencode "url=paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr" localhost:8090/predict
```
Results:
```json
{"url":"paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr","phishing":true,"probability":1.0,"threshold":0.5}
```

And a legitimate one:

```bash
curl -s --get --data-urlencode "url=www.wikipedia.org/wiki/Cat" localhost:8090/predict
```
Results:
```json
{"url":"www.wikipedia.org/wiki/Cat","phishing":false,"probability":0.0004,"threshold":0.5}
```






---

## 2. Containerization

I used Docker to package the model, libraries, and dependencies into one image,
so the service runs identically on any machine without installing Python or
anything else.

The build is multi-stage: the first stage installs dependencies into a
virtualenv, and the second copies only the finished result. Pip's caches and
build tooling never reach the shipped image. Final size is 594MB, most of it
scikit-learn, scipy, and numpy.

### Building and running it

```bash
cd phishing-detector && docker build -t phishing-detector:0.1.0 .
```

```bash
docker run --rm --read-only -p 8091:8000 phishing-detector:0.1.0
```

`--read-only` makes the container filesystem immutable. The app is built to
tolerate it, which is verified here rather than discovered later in Kubernetes.

The endpoints answer exactly as they did running locally — the container changed
how it ships, not what it does:

```bash
curl -s localhost:8091/readyz
```
```json
{"status":"ready","threshold":0.5}
```

```bash
curl -s --get --data-urlencode "url=paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr" localhost:8091/predict
```
```json
{"url":"paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr","phishing":true,"probability":1.0,"threshold":0.5}
```

### Verifying the hardening

Each control is checked against the built image rather than assumed from the
Dockerfile:

```bash
docker run --rm phishing-detector:0.1.0 id
```
```
uid=10001(app) gid=10001(app) groups=10001(app)
```

```bash
docker run --rm phishing-detector:0.1.0 sh -c 'pip --version; python -m pip --version'
```
```
sh: 1: pip: not found
/opt/venv/bin/python: No module named pip
```

```bash
docker exec <container> touch /app/test
```
```
touch: cannot touch '/app/test': Read-only file system
```

| Control | Why |
| ------- | --- |
| Runs as UID 10001 | Root in a container is root on the host kernel if anything escapes the namespace |
| No package manager | A working `pip` beside an attacker with a foothold is an install tool |
| Read-only root filesystem | An attacker cannot drop a binary, webshell, or cron entry anywhere persistent |
| Exact version pins | A build that resolves different versions on different days is not reproducible |
| Thread pools pinned to 1 | scikit-learn sizes pools from the *host* CPU count and oversubscribes against a cgroup limit |

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
