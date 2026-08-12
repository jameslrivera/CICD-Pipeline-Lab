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

Install dependencies:

```bash
cd phishing-detector && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```
Run the server:
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

<img width="1143" height="356" alt="Screenshot 2026-08-12 at 5 51 44 PM" src="https://github.com/user-attachments/assets/91bc9aec-b54d-477d-9e99-022af92f90a7" />

### Building and running it

```bash
cd phishing-detector && docker build -t phishing-detector:0.1.0 .
docker run --rm --read-only -p 8091:8000 phishing-detector:0.1.0
```

`--read-only` makes the container filesystem immutable. The app is built to
tolerate it, verified here rather than discovered later in Kubernetes.

The endpoints answer exactly as they did locally — containerizing changed how it
ships, not what it does:

```bash
curl -s --get --data-urlencode "url=paypal.co.uk.secure-login.verify-account.tk/cgi-bin/webscr" localhost:8091/predict
# {"phishing":true,"probability":1.0,"threshold":0.5}
```

### Verifying the hardening

Each control is checked against the built image rather than assumed from the
Dockerfile:

<img width="1110" height="134" alt="Screenshot 2026-08-12 at 6 45 27 PM" src="https://github.com/user-attachments/assets/54c847fd-8a7d-4aa6-b3c7-20a70ac637a2" />




| Control | Why |
| ------- | --- |
| Runs as UID 10001 | Root in a container is root on the host kernel if anything escapes the namespace |
| No package manager | A working `pip` beside an attacker with a foothold is an install tool |
| Read-only root filesystem | An attacker cannot drop a binary, webshell, or cron entry anywhere persistent |
| Exact version pins | A build that resolves different versions on different days is not reproducible |

---

## 3. Kubernetes

Docker runs one container. Kubernetes runs and supervises many of them across
machines — it keeps a set number alive, health-checks them, restarts what hangs,
and enforces security and network rules on every pod.

I used **kind** (Kubernetes IN Docker) to run a three-node cluster locally: one
control-plane and two workers, each one a Docker container.

<img width="1160" height="375" alt="Screenshot 2026-08-12 at 6 56 36 PM" src="https://github.com/user-attachments/assets/25211f6e-67e1-437e-942b-88bbfd4836e4" />


### Verifying

All three nodes report `Ready` — one control-plane and two workers, running
Kubernetes v1.36.1:

```bash
kubectl get nodes
```

<img width="457" height="106" alt="Screenshot 2026-08-12 at 6 58 09 PM" src="https://github.com/user-attachments/assets/2a13948f-1203-4f85-a695-ac965c4f5201" />




The Deployment asked for two replicas, and the scheduler placed one on each
worker:

```bash
kubectl get pods -n phishing-detector -o wide
```

<img width="1034" height="148" alt="Screenshot 2026-08-12 at 7 07 53 PM" src="https://github.com/user-attachments/assets/357c214e-0c60-4eb0-a234-508e3a5ef6af" />



The Service is internal to the cluster, so reaching it from a laptop needs a
tunnel:

```bash
kubectl port-forward -n phishing-detector svc/phishing-detector 8080:8000
```

### Injecting configuration into the running container

A ConfigMap lets Kubernetes push configuration into a container that is already
running, without touching the image. The image ships a detection threshold of
`0.5`; the ConfigMap supplies `0.30`, and the pod uses the ConfigMap's value.

This is why the app was built to re-read its threshold on every request. The
model stays immutable inside the image, while the policy applied to it is owned
by the cluster and can change at any time.

The difference is not cosmetic. Below is a real phishing URL from the dataset —
a car parts site hosting a fake Google Mail login — scoring just under the
default cutoff:

```bash
# Locally, using the image's own 0.5 — missed
curl -s --get --data-urlencode "url=car-accessories.co.in/googlemail.htm" localhost:8090/predict
# {"phishing":false,"probability":0.4899,"threshold":0.5}

# In the cluster, using the ConfigMap's 0.3 — caught
curl -s --get --data-urlencode "url=car-accessories.co.in/googlemail.htm" localhost:8080/predict
# {"phishing":true,"probability":0.4899,"threshold":0.3}
```

Same image, same model, same probability. The cluster changed the cutoff, and a
real phishing page went from missed to caught — no rebuild, no redeploy, no
restart.

---

## 4. Terraform and Helm

Everything up to this point was built by typing commands. That works until you
need to rebuild it, hand it to someone else, or prove what is actually deployed.
Terraform and Helm turn those commands into files.

**Helm** packages the Kubernetes manifests into a chart — templates plus a values
file. The templates never change between environments; only the values do.

**Terraform** creates the infrastructure itself and installs that chart. The
cluster and the deployment are described in code, so `terraform apply` builds
them and `terraform destroy` removes them.

I split it into two layers on purpose:

```
terraform/
├── cluster-local/   # creates the kind cluster — replaced entirely for cloud
└── app/             # namespace + Helm release — unchanged by that swap
```

The app layer takes only a kubeconfig path and a context name. Point those two
variables at a cloud cluster and the directory works unchanged, which is the
whole reason for the split.

### Running it

```bash
terraform -chdir=terraform/cluster-local init
terraform -chdir=terraform/cluster-local apply

kind load docker-image phishing-detector:0.1.0 --name cicd-lab

terraform -chdir=terraform/app init
terraform -chdir=terraform/app apply
```

The `kind load` between them is a real manual step: kind nodes cannot see images
on the host machine. On a cloud cluster this becomes a registry push.

### The Terraform loop

`init` downloads providers. `fmt` formats. `validate` checks syntax without
touching the cluster. `plan` shows what would change. `apply` makes it happen.

Then `plan` again — a correct configuration should report no changes:

```bash
terraform -chdir=terraform/app plan
```
```
Terraform has compared your real infrastructure against your configuration
and found no differences, so no changes are needed.
```

That final check is the point. Configuration that cannot converge will fight you
forever.

### Proof it is actually managing the cluster

To show that Terraform and Helm are really running the app and not just sitting
in the repo, I asked each of them what they own. Helm lists the release it
deployed, and Terraform lists every resource it created:

```bash
helm list -n phishing-detector
terraform -chdir=terraform/app state list
terraform -chdir=terraform/cluster-local state list
```

<!-- paste your screenshot here -->

Terraform owns the kind cluster, the Calico install, the namespace, and the Helm
release. Helm owns the seven Kubernetes objects inside it.

### What they do in the CI/CD pipeline

In the pipeline I check them instead of running them. Every push runs
`terraform fmt -check` and `terraform validate` on both layers, and `helm lint`
and `helm template` on the chart. That catches broken syntax and templates that
render invalid YAML before they ever reach a cluster. The actual deploy stays
manual, since a hosted runner cannot reach a cluster running on my laptop.

### Drift detection has a blind spot

Terraform is supposed to notice when reality stops matching the code. It does —
but only for resources it manages directly.

I scaled the deployment by hand to 4 replicas and asked Terraform to check:

```bash
kubectl scale deployment/phishing-detector -n phishing-detector --replicas=4
terraform -chdir=terraform/app plan
# No changes. Your infrastructure matches the configuration.
```

Nothing. But removing a label from the namespace was caught immediately:

```bash
kubectl label namespace phishing-detector pod-security.kubernetes.io/enforce-
terraform -chdir=terraform/app plan
# + "pod-security.kubernetes.io/enforce" = "restricted"
# Plan: 0 to add, 1 to change, 0 to destroy.
```

The namespace is a Terraform resource, so it reads the live object and compares.
The deployment sits behind a Helm release, and Terraform only tracks the
release's chart and values — not the objects it produced.

**"We manage it in Terraform" does not mean Terraform will fix it.** Anything
behind a Helm release needs `terraform apply -replace=helm_release.<name>` to
reconcile.

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
