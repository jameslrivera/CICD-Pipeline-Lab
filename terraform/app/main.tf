# The APP layer. Nothing in this directory knows what kind of cluster it is
# talking to — it takes a kubeconfig path and a context name and works against
# anything that answers the Kubernetes API. Swapping the cluster-local layer for
# an AKS or EKS one should require changing the two input variables and nothing
# else.
#
# That is why this does NOT use a terraform_remote_state data source to read the
# cluster layer's outputs. Doing so would be tidier Terraform and would also
# hard-wire this layer to kind, defeating the split.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.17"
    }
  }
}

provider "kubernetes" {
  config_path    = pathexpand(var.kubeconfig_path)
  config_context = var.kube_context
}

provider "helm" {
  kubernetes {
    config_path    = pathexpand(var.kubeconfig_path)
    config_context = var.kube_context
  }
}

# The namespace is managed here rather than by the chart. Helm's own
# --create-namespace makes a bare namespace with no labels, and these Pod
# Security Admission labels are the control that makes every securityContext in
# the chart mandatory instead of voluntary. Managing it in Terraform keeps the
# policy attached to the environment rather than the application package.
resource "kubernetes_namespace_v1" "app" {
  metadata {
    name = var.namespace

    labels = {
      # A NetworkPolicy can only select a namespace by label, never by name.
      name = var.namespace

      # Without these, the API server admits a pod with privileged: true,
      # hostNetwork: true, and hostPath: / mounted — node root plus the
      # kubelet's credentials. Verified against this cluster before the labels
      # existed: such a pod was accepted.
      #
      # "restricted" is the strictest built-in profile, and the chart already
      # complies with it, so enforcement costs nothing and converts
      # documentation into a control.
      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
    }
  }
}

# A namespace permitted to call the API, so the ingress NetworkPolicy has
# something to admit. Without it the policy denies everyone, which is the safe
# default but makes the service unreachable and therefore untestable.
resource "kubernetes_namespace_v1" "clients" {
  count = var.create_client_namespace ? 1 : 0

  metadata {
    name = var.client_namespace
    labels = {
      # Matches networkPolicy.clientNamespaceSelector in the chart's values.
      "detector-client" = "allowed"
    }
  }
}

resource "helm_release" "phishing_detector" {
  name      = var.release_name
  chart     = "${path.module}/../../charts/phishing-detector"
  namespace = kubernetes_namespace_v1.app.metadata[0].name

  # The chart's defaults, then the environment's overrides. Only this list
  # changes between environments; no template does.
  values = [
    file("${path.module}/../../charts/phishing-detector/values-local.yaml"),
  ]

  # Threshold is surfaced as a Terraform variable so detection can be retuned
  # from the IaC layer. The app re-reads it every request, so a helm upgrade
  # changes behaviour without restarting a pod.
  set {
    name  = "detector.threshold"
    value = var.detector_threshold
  }

  set {
    name  = "image.tag"
    value = var.image_tag
  }

  # Wait for the rollout rather than returning as soon as the objects are
  # created, so `terraform apply` finishing actually means the service is
  # serving.
  wait    = true
  timeout = 300

  # Roll back automatically if the release fails to become ready, instead of
  # leaving a half-applied release behind.
  atomic = true
}
