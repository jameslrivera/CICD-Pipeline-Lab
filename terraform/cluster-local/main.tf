# The CLUSTER layer. This is the half that is expected to be thrown away and
# rewritten when the target changes: swapping kind for AKS means replacing this
# directory with one using the azurerm provider, and the app layer next door
# should not need a single edit. That separation is the entire reason the two
# directories exist.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.9"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "kind" {}

resource "kind_cluster" "this" {
  name       = var.cluster_name
  node_image = var.node_image

  # MUST be false here. With the default CNI disabled, nodes stay NotReady
  # until Calico is installed, so waiting for readiness would block forever on
  # a cluster that is behaving exactly as intended.
  wait_for_ready = false

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    networking {
      # kind's built-in CNI, kindnet, provides pod networking but ships no
      # NetworkPolicy controller. The API server still accepts and stores a
      # NetworkPolicy, so every kubectl command reports success while traffic
      # the policy claims to deny flows freely.
      disable_default_cni = true

      # RFC 6598 space, chosen to avoid colliding with a real LAN. Calico's
      # documented default is 192.168.0.0/16, which on a typical home or office
      # network swallows the operator's own subnet — pods can reach the internet
      # but not the laptop, the router, or an on-prem registry.
      pod_subnet = var.pod_subnet
    }

    node { role = "control-plane" }
    node { role = "worker" }
    node { role = "worker" }
  }
}

# Terraform has no first-class way to install a CNI, and the alternatives are
# worse: a kubernetes_manifest resource per Calico object would mean vendoring
# thousands of lines of CRDs into this repo, and it would fail on the first plan
# because the CRDs do not exist yet. Shelling out to the same script a human
# would run is honest about what is happening.
resource "null_resource" "calico" {
  depends_on = [kind_cluster.this]

  # Re-runs if the cluster is replaced or the pod CIDR changes. Without these
  # triggers, changing pod_subnet would rebuild the cluster and leave Calico
  # configured for the old range.
  triggers = {
    cluster_id = kind_cluster.this.id
    pod_subnet = var.pod_subnet
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../scripts/install-calico.sh"
    interpreter = ["/bin/bash", "-c"]

    environment = {
      POD_CIDR        = var.pod_subnet
      CALICO_VERSION  = var.calico_version
      KUBECONFIG      = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT = "kind-${var.cluster_name}"
    }
  }
}
