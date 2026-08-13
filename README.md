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

I used Helm to package the Kubernetes manifests into one chart, and Terraform to
create the cluster and install that chart.

This puts the whole setup in files instead of commands I ran by hand.
`terraform apply` builds the cluster and deploys the app, and `terraform destroy`
tears it all down.

I split the Terraform into two layers:

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

<img width="1191" height="184" alt="Screenshot 2026-08-12 at 7 46 24 PM" src="https://github.com/user-attachments/assets/5e95e9b5-8300-475f-872d-277557077296" />


Terraform owns the kind cluster, the Calico install, the namespace, and the Helm
release. Helm owns the seven Kubernetes objects inside it.

### What they do in the CI/CD pipeline

The pipeline checks them instead of running them. Every push runs
`terraform fmt -check` and `terraform validate` on both layers, and `helm lint`
and `helm template` on the chart, so broken syntax never reaches a cluster. The
actual deploy stays manual, since a hosted runner cannot reach my laptop.

### Drift detection has a blind spot

Terraform only notices changes to resources it manages directly. I scaled the
deployment by hand and it saw nothing, but removing a namespace label was caught
straight away:

```bash
kubectl scale deployment/phishing-detector -n phishing-detector --replicas=4
terraform -chdir=terraform/app plan
# No changes. Your infrastructure matches the configuration.

kubectl label namespace phishing-detector pod-security.kubernetes.io/enforce-
terraform -chdir=terraform/app plan
# Plan: 0 to add, 1 to change, 0 to destroy.
```

The namespace is a Terraform resource. The deployment sits behind a Helm release,
and Terraform only tracks the release's values — not the objects it created.

So "it is managed in Terraform" does not mean Terraform will fix it. Anything
behind a Helm release needs `terraform apply -replace=helm_release.<name>`.

---

## 5. CI/CD

I built the same pipeline twice — once in GitHub Actions
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) and once in GitLab CI
([`.gitlab-ci.yml`](.gitlab-ci.yml)) — so I could compare the two platforms
directly.

GitLab is included because it can be self-hosted and run in an air-gapped
network, which is why it shows up in defense environments.

### Pipeline Flow

I run `git push` and both pipelines start on their own. Nothing else to click.
Each one then:

1. **Lints** the Python with ruff
2. **Runs the 42 tests**
3. **Checks the Helm chart and Terraform** still render and validate
4. **Builds the container image**
5. **Smoke-tests that image** — runs as uid 10001, has no package manager, serves
   under a read-only filesystem, and still flags a known phishing URL
6. **Pushes the image** to a registry, but only from `main`
7. **Renders the manifests** as a deploy dry run

If any step fails the pipeline stops and the commit is marked red. A pull request
runs everything except the push, so an image gets built and tested but never
published.

<!-- paste GitHub Actions run screenshot here -->

### Quality Gates

Anything can run `pytest`. These checks exist because each one caught something
real in this project:

- the image runs as uid 10001, not root
- the image has no package manager
- the image serves under a read-only filesystem
- a known phishing URL is still classified as phishing
- the rendered Helm chart still contains its security settings
- the model artifact is present, so the tests cannot silently skip

The phishing check is the important one — it would catch a corrupt or wrong model
shipping inside an image where everything else looks fine.

### Deployment Stage

A hosted runner cannot reach a cluster on my laptop, so the deploy stage renders
the manifests and stops. That proves they are valid; it does not prove they
apply. On a self-hosted runner inside the cluster's network this becomes a real
`helm upgrade --install`.

### Platform Comparison

| | GitHub Actions | GitLab CI |
| --- | --- | --- |
| Environment | A VM with tools preinstalled | A container per job, image named by you |
| Building images | Docker daemon already running | Needs Docker-in-Docker or a daemonless builder |
| Test results | Needs a third-party action | Parses JUnit XML natively |
| Conditions | `if:` on a step | `rules:` on a job |
| Credentials | `GITHUB_TOKEN`, scoped to the run | `CI_JOB_TOKEN`, dies with the job |

<!-- paste GitLab pipeline screenshot here -->

Neither stores a registry credential.

### Debugging the GitLab Pipeline

It was valid YAML, mirrored the working Actions pipeline stage for stage, and I
had committed it as correct. It produced zero jobs.

I had named the build job `image`, which is a reserved word in GitLab CI. GitLab
read it as a global setting instead of a job and rejected the whole file. Nothing
about that is visible from reading it — renaming the job to `build:image` fixed
it.

Both pipelines now pass and push to their registries.

## Conclusion

In this project I got hands-on experience building a DevOps pipeline from start
to finish — Docker for containerization, Kubernetes for container orchestration,
Terraform and Helm for Infrastructure as Code, and GitHub Actions and GitLab CI
for CI/CD automation.

The biggest thing I took away was the difference between a control being
configured and a control being effective. A NetworkPolicy that filtered nothing,
a test suite that passed while the health endpoint was broken, and security
settings that were purely advisory all looked correct and reported success. Each
one was only found by testing it instead of trusting it.
