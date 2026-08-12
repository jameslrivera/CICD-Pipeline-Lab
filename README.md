# CICD-Pipeline-Lab

**A DevSecOps pipeline built end to end around a real security application —
containerized, hardened, orchestrated, provisioned as code, and automated.**

---

## Background

Modern security work is increasingly about the pipeline that delivers software,
not just the software itself. An application can be written perfectly and still
ship as a container running as root, deployed by a manifest nobody reviewed,
onto a cluster with no policy enforcement, by a pipeline that nobody can audit.
Most of the meaningful controls — least privilege, immutability, network
segmentation, provenance, reproducibility — live in that delivery path.

This project builds that delivery path once, deliberately, and documents what
broke along the way.

The application is a phishing URL classifier: a FastAPI service that scores a URL
using a Naive Bayes model trained on roughly 507,000 labeled URLs. It is a
genuine security tool, and it is also intentionally the *smallest* part of this
repository. It exists to be something worth protecting, so that every control
applied to it is applied for a real reason.

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

Federal and defense cybersecurity roles converge on a common technical core:

- **Containerization** — Docker, Podman, Buildah
- **Container orchestration** — Kubernetes, OpenShift, Helm
- **CI/CD automation** — GitLab, Jenkins, AWS CodeBuild
- **Infrastructure as Code** — Terraform, Ansible, CloudFormation
- **Linux systems** — RHEL, CentOS, Ubuntu
- **Automation and scripting** — Bash, Python, PowerShell

Reading about that stack and operating it are different things. This project
exists to close that gap by building the whole chain — application, image,
cluster, infrastructure code, pipeline — and verifying each layer rather than
assuming it works.

That verification habit turned out to be the most valuable part. Several controls
in this repository looked correct, applied without error, and did nothing at all.
A NetworkPolicy was accepted by the API server and filtered no traffic. A test
suite passed while a liveness endpoint returned 503 to every probe. A detector
failed open on a one-word configuration typo while still reporting healthy. Each
was found by testing the control rather than trusting it, and each is documented
here rather than quietly fixed.

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

<!-- The FastAPI service and the model behind it.
     Source material: docs/technical-notes.md — "What it does", "Model",
     "Liveness and readiness deliberately disagree".
     Worth covering: the config-read-per-request design; the three forms of
     leakage found in the original notebook; honest metrics against the
     majority-class baseline; the documented ASCII-tokenizer limitation. -->

---

## 2. Containerization

<!-- The image and how it is hardened.
     Source material: docs/technical-notes.md — "Container hardening".
     Worth covering: multi-stage build; numeric UID 10001 and why Kubernetes
     requires it to be numeric; pip existing in two places; read-only root
     filesystem; exact version pinning and why the scikit-learn pin is
     load-bearing for a pickled model. -->

---

## 3. Kubernetes

<!-- The cluster, the manifests, and the policy.
     Source material: docs/technical-notes.md — "Kubernetes".
     Worth covering: liveness vs readiness and what Kubernetes does with each;
     the ConfigMap mounted as a directory rather than subPath; Pod Security
     Admission turning voluntary settings into enforced ones; and the
     NetworkPolicy that stored cleanly and enforced nothing. -->

---

## 4. Terraform and Helm

<!-- Infrastructure as code, split so the cluster layer is replaceable.
     Source material: docs/technical-notes.md — "Terraform and Helm".
     Worth covering: the cluster-local / app split and why the app layer does
     not read the cluster layer's state; the Terraform loop; and the drift
     result — Terraform detected a changed namespace label but not a
     hand-scaled Deployment behind a helm_release. -->

---

## 5. CI/CD

<!-- Two pipelines, same stages, different platforms.
     Source material: docs/technical-notes.md — "CI/CD".
     Worth covering: what the smoke tests assert and why each one exists;
     grepping the rendered chart for security controls; deploy being an honest
     dry run; and the four concrete GitHub Actions vs GitLab CI differences. -->

---

## Conclusion

<!-- What the project demonstrates and what was learned.
     Draft below — rewrite in your own voice. -->

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
| 5. CI/CD | GitHub Actions verified green; GitLab CI written but not yet executed |

Optional follow-on work: supply-chain controls (Trivy, SBOM, Checkov, Cosign),
a Podman/Buildah rebuild on Rocky Linux, one deliberate cloud deployment, and
in-cluster policy enforcement with Kyverno and Falco.
