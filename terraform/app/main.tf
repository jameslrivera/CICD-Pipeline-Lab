
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

resource "kubernetes_namespace_v1" "app" {
  metadata {
    name = var.namespace

    labels = {
      name = var.namespace

      "pod-security.kubernetes.io/enforce"         = "restricted"
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
    }
  }
}

resource "kubernetes_namespace_v1" "clients" {
  count = var.create_client_namespace ? 1 : 0

  metadata {
    name = var.client_namespace
    labels = {
      "detector-client" = "allowed"
    }
  }
}

resource "helm_release" "phishing_detector" {
  name      = var.release_name
  chart     = "${path.module}/../../charts/phishing-detector"
  namespace = kubernetes_namespace_v1.app.metadata[0].name

  values = [
    file("${path.module}/../../charts/phishing-detector/values-local.yaml"),
  ]

  set {
    name  = "detector.threshold"
    value = var.detector_threshold
  }

  set {
    name  = "image.tag"
    value = var.image_tag
  }

  wait    = true
  timeout = 300

  atomic = true
}
